"""Immutable decision and outcome dataset (Phase 13).

**Decision records** (``learn_records``) are copied from the agent's immutable
recommendations (``agent_recommendations``) plus the decision-time tool
outputs of the same run (indicators). They contain ONLY information
available at the decision timestamp. Ingestion refuses a feature whose as-of
time is after the decision (``LeakageError``). Records are hashed, and SQLite
triggers abort any UPDATE or DELETE.

**Outcomes** (``learn_outcomes``) are computed later and appended, one per
(record, kind). A horizon outcome is written only once its window has fully
passed and its candles are complete. Until then it is PENDING (not stored),
so a delayed outcome is never guessed. Kinds:

- ``H<minutes>m`` / ``EOD``: price path from the decision reference price
  (entry reference for a setup, otherwise the last close at the decision):
  close return, maximum favourable / adverse excursion, and the
  equal-weight universe return over the same window as a benchmark;
- ``TRADE_PATH``: the hypothetical trade under the Phase 4 candle rules with
  the recommendation's whole-share quantity and Phase 3 costs (setups only);
- ``PAPER_EXECUTION``: what the paper engine actually did with the signal
  (order state, fill vs reference, quantity, actual costs and net P&L).

**Corrections** are append-only events (field, old, new, reason, who, when).
The original record is never changed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from app.paper.store import PaperStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS learn_sources (
    source_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    label TEXT NOT NULL,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learn_records (
    record_id TEXT PRIMARY KEY,
    recommendation_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    decision_ts_ist TEXT NOT NULL,
    decision_ts_utc TEXT NOT NULL,
    market_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    recommendation TEXT NOT NULL,      -- CONSIDER | WAIT | NO_TRADE | UNAVAILABLE
    system_status TEXT NOT NULL,       -- the recommendation's gated status
    strategy_version TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT NOT NULL,
    body TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    UNIQUE (source_id, recommendation_id)
);
CREATE TABLE IF NOT EXISTS learn_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    available_at TEXT NOT NULL,        -- when this outcome became knowable
    computed_at TEXT NOT NULL,
    body TEXT NOT NULL,
    UNIQUE (record_id, kind)
);
CREATE TABLE IF NOT EXISTS learn_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learn_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL,
    rules_version TEXT NOT NULL,       -- review-rules-vN, or "human:<name>"
    process_grade TEXT NOT NULL,       -- SOUND | FLAWED | UNKNOWN
    process_categories TEXT NOT NULL,
    outcome_grade TEXT NOT NULL,
    outcome_category TEXT NOT NULL,
    evidence TEXT NOT NULL,
    ts TEXT NOT NULL,
    UNIQUE (record_id, rules_version)
);
CREATE TRIGGER IF NOT EXISTS learn_rec_no_update BEFORE UPDATE ON learn_records
BEGIN SELECT RAISE(ABORT, 'learning records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS learn_rec_no_delete BEFORE DELETE ON learn_records
BEGIN SELECT RAISE(ABORT, 'learning records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS learn_out_no_update BEFORE UPDATE ON learn_outcomes
BEGIN SELECT RAISE(ABORT, 'learning outcomes are append-only'); END;
CREATE TRIGGER IF NOT EXISTS learn_out_no_delete BEFORE DELETE ON learn_outcomes
BEGIN SELECT RAISE(ABORT, 'learning outcomes are append-only'); END;
CREATE TRIGGER IF NOT EXISTS learn_cor_no_update BEFORE UPDATE ON learn_corrections
BEGIN SELECT RAISE(ABORT, 'corrections are append-only'); END;
CREATE TRIGGER IF NOT EXISTS learn_cor_no_delete BEFORE DELETE ON learn_corrections
BEGIN SELECT RAISE(ABORT, 'corrections are append-only'); END;
CREATE TRIGGER IF NOT EXISTS learn_rev_no_update BEFORE UPDATE ON learn_reviews
BEGIN SELECT RAISE(ABORT, 'reviews are append-only'); END;
CREATE TRIGGER IF NOT EXISTS learn_rev_no_delete BEFORE DELETE ON learn_reviews
BEGIN SELECT RAISE(ABORT, 'reviews are append-only'); END;
"""

REC_MAP = {"consider": "CONSIDER", "wait_for_confirmation": "WAIT", "no_trade": "NO_TRADE"}


class LeakageError(RuntimeError):
    pass


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class LearningStore:
    def __init__(self, path: Path | str):
        from app.learning.proposals import PROPOSAL_SCHEMA

        self.store = PaperStore(path)
        self.store.conn.executescript(SCHEMA)
        self.store.conn.executescript(PROPOSAL_SCHEMA)

    def close(self) -> None:
        self.store.close()

    def one(self, sql, params=()):
        return self.store.one(sql, params)

    def all(self, sql, params=()) -> list[dict]:
        return [dict(r) for r in self.store.all(sql, params)]

    # -- sources ---------------------------------------------------------------------
    def add_source(self, path: Path, label: str, now: datetime) -> str:
        sid = "SRC-" + sha(str(Path(path).resolve()))[:10]
        with self.store.transaction():
            self.store._insert("learn_sources", {"source_id": sid, "path": str(path),
                                                 "label": label, "added_at": now},
                               or_ignore=True)
        return sid

    # -- records ---------------------------------------------------------------------
    def add_record(self, source_id: str, body: dict, now: datetime) -> bool:
        text = canonical(body)
        rid = "LR-" + sha(f"{source_id}|{body['recommendation_id']}")[:16]
        with self.store.transaction():
            return self.store._insert("learn_records", {
                "record_id": rid, "recommendation_id": body["recommendation_id"],
                "source_id": source_id, "decision_ts_ist": body["decision_ts_ist"],
                "decision_ts_utc": body["decision_ts_utc"], "market_date": body["market_date"],
                "symbol": body["symbol"], "recommendation": body["recommendation"],
                "system_status": body["system_status"],
                "strategy_version": body["strategy_version"], "model": body["model"],
                "prompt_version": body["prompt_version"], "body": text, "sha256": sha(text),
                "ingested_at": now}, or_ignore=True)

    def records(self) -> list[dict]:
        out = []
        for r in self.all("SELECT * FROM learn_records ORDER BY decision_ts_utc, symbol"):
            r["integrity_ok"] = sha(r["body"]) == r["sha256"]
            r["body"] = json.loads(r["body"])
            out.append(r)
        return out

    # -- outcomes, corrections ---------------------------------------------------------
    def add_outcome(self, record_id: str, kind: str, available_at: datetime, now: datetime,
                    body: dict) -> bool:
        with self.store.transaction():
            return self.store._insert("learn_outcomes", {
                "record_id": record_id, "kind": kind, "available_at": available_at,
                "computed_at": now, "body": canonical(body)}, or_ignore=True)

    def outcomes(self, record_id: str | None = None) -> dict[str, dict[str, dict]]:
        sql, params = "SELECT * FROM learn_outcomes", ()
        if record_id:
            sql, params = sql + " WHERE record_id = ?", (record_id,)
        out: dict[str, dict[str, dict]] = {}
        for r in self.all(sql, params):
            out.setdefault(r["record_id"], {})[r["kind"]] = {
                **json.loads(r["body"]), "_available_at": r["available_at"]}
        return out

    def correct(self, record_id: str, field: str, new_value, reason: str, actor: str,
                now: datetime) -> None:
        if not reason.strip() or not actor.strip():
            raise ValueError("a correction needs a reason and the person making it")
        row = self.one("SELECT body FROM learn_records WHERE record_id = ?", (record_id,))
        if row is None:
            raise KeyError(f"no learning record {record_id}")
        old = json.loads(row["body"]).get(field)
        with self.store.transaction():
            self.store._insert("learn_corrections", {
                "record_id": record_id, "field": field, "old_value": canonical(old),
                "new_value": canonical(new_value), "reason": reason, "actor": actor,
                "ts": now})

    def corrections(self, record_id: str) -> list[dict]:
        return self.all("SELECT * FROM learn_corrections WHERE record_id = ? ORDER BY id",
                        (record_id,))


# -- ingestion --------------------------------------------------------------------------


def _indicators(agent_store, run_id: str, symbol: str) -> dict | None:
    row = agent_store.one(
        "SELECT output, args FROM agent_tool_calls WHERE run_id = ? AND tool = "
        "'get_indicators' AND ok = 1 AND args LIKE ?", (run_id, f'%"{symbol}"%'))
    if row is None:
        return None
    return json.loads(row["output"]).get("result", {}).get("indicators")


def decision_features(rec: dict, indicators: dict | None, step_minutes: int) -> dict:
    """Decision-time features. Raises LeakageError if any input postdates the
    decision."""
    decided = datetime.fromisoformat(rec["created_at"])
    if indicators and indicators.get("as_of_candle"):
        candle_end = datetime.fromisoformat(indicators["as_of_candle"]) + \
            timedelta(minutes=step_minutes)
        if candle_end > decided:
            raise LeakageError(f"{rec['recommendation_id']}: indicator candle ends "
                               f"{candle_end.isoformat()}, after the decision {decided}")
    for src in rec.get("data_sources", []):
        if src.get("as_of") and datetime.fromisoformat(src["as_of"]) > decided:
            raise LeakageError(f"{rec['recommendation_id']}: source {src['tool_call_id']} "
                               f"is as of {src['as_of']}, after the decision")
    open_minutes = decided.hour * 60 + decided.minute - (9 * 60 + 15)
    ind = indicators or {}
    rvol = ind.get("relative_volume")
    return {
        "screen_status": rec.get("screen_status"), "signal_status": rec.get("signal_status"),
        "has_setup": rec.get("signal_id") is not None, "score": rec.get("score"),
        "entry_reference": rec.get("entry_reference"), "stop": rec.get("stop"),
        "target": rec.get("target"), "quantity": rec.get("quantity"),
        "estimated_costs_at_target": rec.get("estimated_costs_at_target"),
        "planned_net_loss_at_stop": rec.get("planned_net_loss_at_stop"),
        "net_reward_risk": rec.get("net_reward_risk"), "risk_status": rec.get("risk_status"),
        "risk_reasons": rec.get("risk_reasons", []), "data_fresh": rec["data_fresh"],
        "valid_until": rec.get("valid_until"), "minutes_since_open": open_minutes,
        "close": ind.get("close"), "vwap": ind.get("vwap"),
        "opening_range_high": ind.get("opening_range_high"),
        "opening_range_low": ind.get("opening_range_low"),
        "opening_range_width_pct": ind.get("opening_range_width_pct"),
        "relative_volume": rvol,
        "rvol_bucket": None if rvol is None else ("<1.5" if rvol < 1.5 else "1.5-2.5"
                                                  if rvol < 2.5 else ">=2.5"),
        "indicators_as_of": ind.get("as_of_candle"),
    }


def ingest(lstore: LearningStore, agent_db: Path, label: str, settings, now: datetime) -> dict:
    """Copy every intraday symbol recommendation (with decision-time features)
    from an agent database. Idempotent; leakage is refused, never ingested."""
    from app.data.models import parse_interval

    src = lstore.add_source(agent_db, label, now)
    agent = PaperStore(agent_db)
    step = int(parse_interval(settings.strategy.candle_interval).timedelta.total_seconds()
               // 60)
    added = skipped = 0
    refused: list[str] = []
    try:
        rows = agent.all("SELECT body FROM agent_recommendations WHERE symbol IS NOT NULL AND "
                         "window = 'intraday' ORDER BY created_at")
    except Exception:  # noqa: BLE001 - no agent tables in this database
        rows = []
    for row in rows:
        rec = json.loads(row["body"])
        try:
            feats = decision_features(rec, _indicators(agent, rec["run_id"], rec["symbol"]),
                                      step)
        except LeakageError as exc:
            refused.append(str(exc))
            continue
        decided = datetime.fromisoformat(rec["created_at"])
        a = rec.get("ai_assessment") or {}
        body = {
            "recommendation_id": rec["recommendation_id"], "run_id": rec["run_id"],
            "decision_ts_ist": decided.isoformat(),
            "decision_ts_utc": decided.astimezone(UTC).isoformat(),
            "market_date": rec["market_date"], "market_session": f"NSE {rec['market_date']}",
            "symbol": rec["symbol"], "exchange": rec.get("exchange", "NSE"),
            "signal_id": rec.get("signal_id"), "strategy_version": rec["strategy_version"],
            "config_version": rec["config_version"],
            "data_snapshot_ts": max((s.get("as_of") or "" for s in rec.get("data_sources", [])),
                                    default=None) or None,
            "data_fresh": rec["data_fresh"], "features": feats,
            "model": rec.get("model"), "prompt_version": rec["prompt_version"],
            "tool_call_ids": rec.get("tool_call_ids", []),
            "model_call_ids": rec.get("model_call_ids", []),
            "ai_status": rec["ai_status"],
            "recommendation": REC_MAP.get(a.get("recommendation"), "UNAVAILABLE")
            if rec["ai_status"] == "OK" else "UNAVAILABLE",
            "ai_output": a or None, "unsupported_claims": rec.get("unsupported_claims", []),
            "untrusted_content_flags": rec.get("untrusted_content_flags", []),
            "system_status": rec["status"], "status_reasons": rec["status_reasons"],
            "rule_compliant": not (rec["status"] == "CANDIDATE" and not (
                rec.get("risk_status") == "QUALIFIED_FOR_REVIEW" and rec["data_fresh"]
                and (rec.get("quantity") or 0) >= 1)),
        }
        if lstore.add_record(src, body, now):
            added += 1
        else:
            skipped += 1
    agent.close()
    return {"source_id": src, "added": added, "already_present": skipped,
            "refused_leakage": refused}


# -- outcomes ------------------------------------------------------------------------------


def _frame(provider, symbol, day, settings):
    from app.data.models import parse_interval

    iv = parse_interval(settings.strategy.candle_interval)
    return provider.get_candles(symbol, iv.label, day, day).frame, iv.timedelta


def _window_stats(frame, start, end, ref):
    w = frame[(frame["timestamp"] >= pd.Timestamp(start)) & (frame["timestamp"] < pd.Timestamp(
        end))]
    if w.empty:
        return None
    close = float(w.iloc[-1]["close"])
    return {"close": close, "return_pct": round((close / ref - 1) * 100, 4),
            "mfe_pct": round((float(w["high"].max()) / ref - 1) * 100, 4),
            "mae_pct": round((float(w["low"].min()) / ref - 1) * 100, 4),
            "candles": len(w)}


def _benchmark(provider, day, start, end, settings, exclude: str) -> float | None:
    rets = []
    for inst in provider.list_instruments()[:30]:
        if inst.symbol == exclude:
            continue
        try:
            frame, _ = _frame(provider, inst.symbol, day, settings)
        except Exception:  # noqa: BLE001 - a missing symbol just drops out
            continue
        before = frame[frame["timestamp"] < pd.Timestamp(start)]
        w = frame[(frame["timestamp"] >= pd.Timestamp(start))
                  & (frame["timestamp"] < pd.Timestamp(end))]
        if before.empty or w.empty:
            continue
        rets.append(float(w.iloc[-1]["close"]) / float(before.iloc[-1]["close"]) - 1)
    return round(sum(rets) / len(rets) * 100, 4) if rets else None


def compute_outcomes(lstore: LearningStore, provider, settings, calendar, now: datetime,
                     paper_stores: dict[str, PaperStore] | None = None) -> dict:
    """Append every outcome that has become knowable by ``now``."""
    from app.agent.compare import simulate

    now = now.astimezone(calendar.tz)
    done = lstore.outcomes()
    added, pending = 0, 0
    for r in lstore.records():
        b = r["body"]
        decided = datetime.fromisoformat(b["decision_ts_ist"]).astimezone(calendar.tz)
        day = decided.date()
        eod = datetime.combine(day, settings.strategy.eod_exit_time, tzinfo=calendar.tz)
        have = done.get(r["record_id"], {})
        f = b["features"]
        ref = f.get("entry_reference") or f.get("close")
        windows = [(f"H{h}m", decided + timedelta(minutes=h))
                   for h in settings.learning.horizons_minutes] + [("EOD", eod)]
        frame = step = None
        for kind, end in windows:
            if kind in have:
                continue
            end = min(end, eod)
            if now < end:
                pending += 1  # not knowable yet: never guessed
                continue
            if ref is None:
                lstore.add_outcome(r["record_id"], kind, end, now, {"status": "NO_REFERENCE"})
                added += 1
                continue
            try:
                if frame is None:
                    frame, step = _frame(provider, b["symbol"], day, settings)
                complete = frame[frame["timestamp"] + step <= pd.Timestamp(now)]
                stats = _window_stats(complete, decided, end, float(ref))
                body = ({"status": "NO_DATA"} if stats is None else
                        {"status": "OK", "reference": ref, **stats,
                         "benchmark_return_pct": _benchmark(provider, day, decided, end,
                                                            settings, b["symbol"])})
            except Exception as exc:  # noqa: BLE001 - recorded as missing, not invented
                body = {"status": "NO_DATA", "error": str(exc)[:200]}
            if body["status"] == "OK" and body["benchmark_return_pct"] is not None:
                body["excess_return_pct"] = round(body["return_pct"]
                                                  - body["benchmark_return_pct"], 4)
            lstore.add_outcome(r["record_id"], kind, end, now, body)
            added += 1
        if "TRADE_PATH" not in have and f.get("has_setup") and now >= eod:
            sim = simulate({"entry_reference": f["entry_reference"], "stop": f["stop"],
                            "target": f["target"], "quantity": f.get("quantity") or 0,
                            "symbol": b["symbol"], "created_at": b["decision_ts_ist"]},
                           provider, settings, calendar, until=now)
            sim["hypothetical"] = True
            lstore.add_outcome(r["record_id"], "TRADE_PATH", eod, now, sim)
            added += 1
        store = (paper_stores or {}).get(r["source_id"])
        if "PAPER_EXECUTION" not in have and store is not None and b.get("signal_id") \
                and now >= eod:
            lstore.add_outcome(r["record_id"], "PAPER_EXECUTION", eod, now,
                               paper_execution(store, b["signal_id"]))
            added += 1
    return {"added": added, "pending": pending}


def paper_execution(store: PaperStore, signal_id: str) -> dict:
    sig = store.one("SELECT review_status, quantity, entry_reference, reasons FROM signals "
                    "WHERE signal_id = ?", (signal_id,))
    if sig is None:
        return {"status": "NOT_SEEN_BY_PAPER_ENGINE"}
    order = store.one("SELECT state, fill_price, fill_quantity, quantity, reason FROM orders "
                      "WHERE signal_id = ? AND side = 'BUY'", (signal_id,))
    pos = store.one("SELECT state, entry_price, exit_price, exit_reason, gross_pnl, "
                    "total_costs, net_pnl, quantity FROM positions WHERE signal_id = ?",
                    (signal_id,))
    out = {"status": "ORDERED" if order else "NOT_ORDERED",
           "review_status": sig["review_status"], "signal_quantity": sig["quantity"],
           "entry_reference": sig["entry_reference"]}
    if order:
        out["order"] = dict(order)
    if pos:
        out["position"] = dict(pos)
        if pos["entry_price"] and sig["entry_reference"]:
            ref = float(sig["entry_reference"])
            out["fill_deviation_pct"] = round((float(pos["entry_price"]) / ref - 1) * 100, 4)
    return out
