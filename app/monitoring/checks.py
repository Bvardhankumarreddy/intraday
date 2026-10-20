"""Monitoring checks (Phase 15). Each check reads existing records only and
returns ``(findings, metrics)``.

A finding has ``key`` (category prefix: data, calendar, ai, exec, version,
storage, perf), severity, component, rule, threshold, evidence, investigation
steps, and ``effect``: which safe mode it implies and for which arms.
Metrics that cannot be measured are ``None`` (unavailable), never 0.
"""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np


def _rows(store, sql, params=()) -> list[dict]:
    if store is None:
        return []
    try:
        return [dict(r) for r in store.all(sql, params)]
    except Exception:  # noqa: BLE001 - absent table = nothing recorded
        return []


def F(key, severity, component, rule, threshold, evidence, steps, effect=None, **kw) -> dict:
    return {"key": key, "severity": severity, "component": component, "rule": rule,
            "threshold": threshold, "evidence": evidence, "steps": steps,
            "effect": effect or {}, **kw}


# -- market data and calendar ---------------------------------------------------------


def check_data(settings, cal, a_store, now: datetime) -> tuple[list, dict]:
    cfg, day = settings.monitoring, now.date()
    hb = a_store.get_meta("heartbeat") if a_store is not None else None
    in_session = cal.is_trading_day(day) and cal.session_bounds(day)[0] <= now <= \
        cal.session_bounds(day)[1]
    out, m = [], {"in_session": in_session, "heartbeat": hb}
    if not cal.is_trading_day(day):
        m["market"] = f"closed ({cal.holidays.get(day) or 'weekend'})"  # an official closure
        return out, m
    if in_session:
        age = (now - datetime.fromisoformat(hb["ts"])).total_seconds() if hb else None
        m["heartbeat_age_seconds"] = age
        limit = settings.ops.max_step_gap_minutes * 60
        if age is None or age > limit:
            out.append(F("data.no_step", "CRITICAL", "market-data",
                         "no processing step during an open session",
                         f"heartbeat age <= {limit}s", {"heartbeat_age_seconds": age},
                         ["check the scheduler process and its log",
                          "python -m app.ops health"], {"state": "DEGRADED_DATA"},
                         session=str(day)))
        elif hb.get("signals_blocked"):
            out.append(F("data.stale", "CRITICAL", "market-data",
                         "no symbol has fresh, valid completed candles",
                         f"freshness <= {settings.data.max_staleness_seconds}s",
                         {"symbols_ok": hb.get("symbols_ok"),
                          "symbols_total": hb.get("symbols_total"),
                          "provider": hb.get("provider")},
                         ["inspect provider health in the heartbeat",
                          "check the data subscription and network"],
                         {"state": "DEGRADED_DATA"}, session=str(day)))
        prov = (hb or {}).get("provider") or {}
        if prov.get("circuit") == "open" or (prov.get("consecutive_failures") or 0) >= 3:
            out.append(F("data.provider_failing", "WARNING", "market-data",
                         "provider circuit open or repeated request failures",
                         "consecutive failures < 3",
                         {k: prov.get(k) for k in ("state", "circuit", "consecutive_failures",
                                                   "last_error", "retries")},
                         ["check rate limits and credentials (never logged)",
                          "wait for the provider circuit to half-open"], session=str(day)))
    dq = _rows(a_store, "SELECT SUM(expected) AS exp, SUM(received) AS rec, SUM(missing) AS "
                        "miss, SUM(duplicates) AS dup, SUM(out_of_order) AS ooo, "
                        "SUM(invalid_ohlc) AS ohlc, SUM(invalid_volume) AS vol, SUM(gaps) AS "
                        "gaps, COUNT(*) AS batches FROM dq_batches WHERE trading_date = ?",
               (day.isoformat(),))
    q = dq[0] if dq else {}
    m["data_quality"] = q or None
    if q and q.get("batches"):
        bad = sum((q.get(k) or 0) for k in ("dup", "ooo", "ohlc", "vol"))
        if bad > cfg.max_invalid_records:
            out.append(F("data.invalid_records", "CRITICAL", "market-data",
                         "duplicate, out-of-order or invalid OHLCV records received",
                         f"<= {cfg.max_invalid_records}",
                         {k: q.get(k) for k in ("dup", "ooo", "ohlc", "vol")},
                         ["inspect dq_batches for the symbols and timestamps",
                          "the affected symbols are rejected by validation"],
                         {"state": "DEGRADED_DATA"}, session=str(day)))
        miss_pct = 100 * (q.get("miss") or 0) / q["exp"] if q.get("exp") else None
        m["missing_candle_pct"] = miss_pct
        if miss_pct is not None and miss_pct > cfg.max_missing_candle_pct:
            out.append(F("data.missing_candles", "WARNING", "market-data",
                         "missing or delayed candles during the session",
                         f"<= {cfg.max_missing_candle_pct}%", {"missing_pct": round(miss_pct, 2)},
                         ["compare dq_batches expected vs received per symbol"],
                         session=str(day)))
    # universe consistency: configured symbols that produced no data today
    uni = Path(settings.data.universe_file)
    symbols = [line.split(",")[0].strip() for line in uni.read_text().splitlines()[1:]
               if line.strip() and not line.startswith("#")] if uni.exists() else []
    seen = {r["symbol"] for r in _rows(a_store, "SELECT DISTINCT symbol FROM dq_batches WHERE "
                                                "trading_date = ?", (day.isoformat(),))}
    if in_session and symbols and seen:
        silent = sorted(set(symbols) - seen)
        if silent:
            out.append(F("data.symbol_mapping", "WARNING", "market-data",
                         "configured symbols produced no data (mapping or availability)",
                         "every configured symbol has data", {"symbols": silent[:20]},
                         ["check instrument mapping for these symbols"], session=str(day)))
    # quote freshness and spread validity
    quotes = _rows(a_store, "SELECT symbol, last_price_time, spread_pct FROM watchlist WHERE "
                            "trading_date = ?", (day.isoformat(),))
    stale_q, bad_spread = [], []
    for qrow in quotes:
        if qrow["last_price_time"] and in_session:
            age = (now - datetime.fromisoformat(qrow["last_price_time"])).total_seconds()
            if age > cfg.max_quote_age_seconds:
                stale_q.append(qrow["symbol"])
        if qrow["spread_pct"] not in (None, ""):
            sp = float(qrow["spread_pct"])
            if sp <= 0 or sp > settings.screening.max_spread_pct * cfg.max_spread_multiple:
                bad_spread.append([qrow["symbol"], sp])
    m["quotes"] = {"symbols": len(quotes), "stale": len(stale_q), "invalid_spread":
                   len(bad_spread)} if quotes else None
    if bad_spread:
        out.append(F("data.spread_invalid", "WARNING", "market-data",
                     "bid-ask spread missing, non-positive or far above the screen limit",
                     f"0 < spread <= {settings.screening.max_spread_pct * cfg.max_spread_multiple}%",
                     {"symbols": bad_spread[:10]}, ["check quote feed; screen rejects these"],
                     session=str(day)))
    if stale_q and len(stale_q) == len(quotes):
        out.append(F("data.quotes_stale", "WARNING", "market-data", "all quotes stale",
                     f"quote age <= {cfg.max_quote_age_seconds}s", {"symbols": stale_q[:10]},
                     ["check the quote subscription"], session=str(day)))
    return out, m


def check_calendar(settings, cal, a_store, now: datetime) -> tuple[list, dict]:
    out, day = [], now.date()
    if not cal.covers_year(day.year):
        out.append(F("calendar.year_missing", "WARNING", "calendar",
                     f"holiday list has no entries for {day.year}", "verified list per year", {},
                     ["update config/nse_holidays.yaml from the official NSE circular"]))
    data_on_closed = _rows(a_store, "SELECT DISTINCT trading_date FROM dq_batches")
    wrong = [r["trading_date"] for r in data_on_closed
             if not cal.is_trading_day(date.fromisoformat(r["trading_date"]))]
    if wrong:
        out.append(F("calendar.inconsistent", "CRITICAL", "calendar",
                     "market data recorded on a day the calendar marks closed",
                     "data only on trading days", {"dates": wrong[:10]},
                     ["verify the holiday list against the exchange circular"],
                     {"state": "PAPER_TRADING_PAUSED", "arms": ["A", "B", "canary"]}))
    return out, {"calendar_covers_year": cal.covers_year(day.year)}


# -- AI agent -------------------------------------------------------------------------


def check_ai(settings, b_store, now: datetime, expected: dict | None) -> tuple[list, dict]:
    cfg, out = settings.monitoring, []
    runs = _rows(b_store, "SELECT * FROM agent_runs WHERE window = 'intraday' ORDER BY "
                          "started_at DESC LIMIT ?", (cfg.ai_window_runs * 4,))
    calls = _rows(b_store, "SELECT m.* FROM agent_model_calls m ORDER BY m.started_at DESC "
                           "LIMIT ?", (cfg.ai_window_runs * 6,))
    model_runs = [r for r in runs if r["status"] in ("COMPLETED", "FAILED")][:cfg.ai_window_runs]
    m: dict = {"runs_considered": len(model_runs), "model_requests": len(calls)}
    if not calls:
        m.update(failure_rate=None, latency_p95=None)
        return out, m
    errs = [c for c in calls if c["error_kind"]]
    rate = len(errs) / len(calls)
    lat = [c["latency_seconds"] for c in calls if c["latency_seconds"] is not None]
    kinds: dict[str, int] = {}
    for c in errs:
        kinds[c["error_kind"]] = kinds.get(c["error_kind"], 0) + 1
    retries = sum(1 for c in calls if (c["attempts"] or 1) > 1)
    cost = [c["cost_usd"] for c in calls if c["cost_usd"] is not None]
    today = now.date().isoformat()
    cost_today = sum(c["cost_usd"] or 0 for c in calls if c["started_at"][:10] == today) \
        if cost else None
    m.update(failure_rate=round(rate, 4), error_kinds=kinds, retried_requests=retries,
             latency_p95=round(float(np.quantile(lat, 0.95)), 3) if lat else None,
             tokens=[sum(c["input_tokens"] or 0 for c in calls),
                     sum(c["output_tokens"] or 0 for c in calls)],
             cost_usd_today=cost_today)
    enough = len(calls) >= cfg.ai_min_runs_for_rates
    if enough and rate >= cfg.ai_failure_rate_warning:
        crit = rate >= cfg.ai_failure_rate_critical
        out.append(F("ai.failure_rate", "CRITICAL" if crit else "WARNING", "llm",
                     "model request failure rate (timeouts, rate limits, errors)",
                     f"< {cfg.ai_failure_rate_warning} (critical {cfg.ai_failure_rate_critical})",
                     {"rate": round(rate, 4), "requests": len(calls), "kinds": kinds},
                     ["check provider status and the API key (never logged)",
                      "deterministic baseline continues; AI arm opens no new positions"],
                     {"state": "DEGRADED_AI"} if crit else None))
    if lat and m["latency_p95"] > cfg.ai_latency_p95_warning_seconds:
        out.append(F("ai.latency", "WARNING", "llm", "model latency p95 high",
                     f"<= {cfg.ai_latency_p95_warning_seconds}s", {"p95": m["latency_p95"]},
                     ["check provider status; consider fewer tool calls"]))
    if cfg.ai_daily_cost_usd_warning and cost_today is not None and \
            cost_today > cfg.ai_daily_cost_usd_warning:
        out.append(F("ai.cost", "WARNING", "llm", "API spending today above threshold",
                     f"<= {cfg.ai_daily_cost_usd_warning} USD", {"usd_today": cost_today},
                     ["review research cadence (agent_every_n_candles) and retries"]))
    recs = [json.loads(r["body"]) for r in _rows(
        b_store, "SELECT body FROM agent_recommendations WHERE symbol IS NOT NULL AND window = "
                 "'intraday' ORDER BY created_at DESC LIMIT 200")]
    if recs:
        invalid = sum(1 for r in recs if r["ai_status"] == "INVALID_OUTPUT") / len(recs)
        unsup = sum(1 for r in recs if r["unsupported_claims"]) / len(recs)
        rejected = sum(1 for r in recs if (r.get("ai_assessment") or {}).get("recommendation")
                       == "consider" and r["status"] != "CANDIDATE")
        cands = [r["signal_id"] for r in recs if r["status"] == "CANDIDATE" and r["signal_id"]]
        m.update(invalid_rate=round(invalid, 4), unsupported_rate=round(unsup, 4),
                 deterministic_rejections=rejected,
                 duplicate_candidates=len(cands) - len(set(cands)))
        if len(recs) >= cfg.ai_min_runs_for_rates and invalid >= cfg.ai_invalid_rate_warning:
            out.append(F("ai.invalid_outputs", "WARNING", "llm", "invalid structured responses",
                         f"< {cfg.ai_invalid_rate_warning}", {"rate": round(invalid, 4)},
                         ["inspect raw responses in agent_model_calls"]))
        if len(recs) >= cfg.ai_min_runs_for_rates and unsup >= cfg.ai_unsupported_rate_warning:
            out.append(F("ai.unsupported_claims", "WARNING", "llm",
                         "evidence-grounding failures (unsupported claims)",
                         f"< {cfg.ai_unsupported_rate_warning}", {"rate": round(unsup, 4)},
                         ["review flagged claims; a prompt change needs Phase 14"]))
        if m["duplicate_candidates"]:
            out.append(F("ai.duplicates", "WARNING", "llm", "duplicate CANDIDATE recommendations "
                         "for one signal", "0", {"count": m["duplicate_candidates"]},
                         ["check the research cadence and duplicate guard"]))
        if expected:
            want = (expected["prompt"]["version"], expected["llm"]["model"])
            got = {(r["prompt_version"], r.get("model")) for r in recs[:20]
                   if r.get("model") and not r["model"].startswith("mock")}
            if got and got != {want}:
                out.append(F("version.ai_mismatch", "CRITICAL", "versions",
                             "recommendations come from a different prompt/model than the "
                             "approved version", f"only {want}", {"seen": sorted(got)},
                             ["python -m app.experiments verify",
                              "compare the running configuration with the active version"],
                             {"state": "PAPER_TRADING_PAUSED", "arms": ["B"]}))
        # behaviour change: NO_TRADE share / volume, latest session vs earlier ones
        by_day: dict[str, list] = {}
        for r in recs:
            by_day.setdefault(r["market_date"], []).append(r)
        if len(by_day) >= 3:
            days = sorted(by_day)
            last, prior = by_day[days[-1]], [x for d in days[:-1] for x in by_day[d]]

            def nt(rs):
                return sum(1 for r in rs if r["status"] in ("NO_TRADE", "WATCH")) / len(rs)

            vol_ref = len(prior) / (len(days) - 1)
            m.update(no_trade_share_last=round(nt(last), 4), no_trade_share_ref=round(nt(prior), 4),
                     volume_last=len(last), volume_ref=round(vol_ref, 2))
            if len(last) >= cfg.ai_min_runs_for_rates and abs(nt(last) - nt(prior)) >= \
                    cfg.behaviour_change_abs:
                out.append(F("ai.no_trade_shift", "INFO", "llm",
                             "abnormal change in NO_TRADE/WATCH share",
                             f"|change| < {cfg.behaviour_change_abs}",
                             {"last": round(nt(last), 4), "reference": round(nt(prior), 4)},
                             ["check data and market conditions before suspecting the model"]))
            if vol_ref and (len(last) > vol_ref * cfg.volume_change_factor or
                            len(last) * cfg.volume_change_factor < vol_ref):
                out.append(F("ai.volume_shift", "INFO", "llm",
                             "unexpected recommendation volume",
                             f"within x{cfg.volume_change_factor} of reference",
                             {"last": len(last), "reference_per_session": round(vol_ref, 2)},
                             ["check universe size, data health and cadence"]))
    return out, m


# -- execution, versions, storage -----------------------------------------------------------


def check_execution(settings, stores: dict, now: datetime) -> tuple[list, dict]:
    from app.ops.reconciliation import consistency_checks

    out, m = [], {}
    for arm, store in stores.items():
        if store is None:
            continue
        if not _rows(store, "SELECT 1 FROM portfolio"):
            m[arm] = "not initialised (no paper session has run)"  # nothing to check yet
            continue
        try:
            found = consistency_checks(store, settings, now.date(), now)
        except Exception as exc:  # noqa: BLE001
            found = []
            out.append(F(f"exec.check_failed.{arm}", "WARNING", "paper", "consistency check "
                         "could not run", "runs", {"error": str(exc)[:200]},
                         ["python -m app.ops health"]))
        crit = [f for f in found if f.severity == "CRITICAL"]
        m[arm] = {"findings": len(found), "critical": len(crit)}
        if crit:
            out.append(F(f"exec.inconsistent.{arm}", "CRITICAL", "paper",
                         "paper-trading records inconsistent (ledger, duplicates or accounting)",
                         "0 critical findings",
                         {"findings": [[f.code, f.ref, f.message] for f in crit[:10]]},
                         ["python -m app.ops reconcile; never edit records by hand",
                          "new paper entries for this arm are paused until resolved"],
                         {"state": "PAPER_TRADING_PAUSED", "arms": [arm]}))
        issues = _rows(store, "SELECT code, ref, message FROM reconciliation_issues WHERE "
                              "trading_date = ?", (now.date().isoformat(),))
        if issues:
            out.append(F(f"exec.reconciliation.{arm}", "WARNING", "paper",
                         "paper execution discrepancies found by reconciliation", "0",
                         {"issues": issues[:10]}, ["review the daily reconciliation"]))
    return out, m


def check_versions(settings, now: datetime) -> tuple[list, dict, dict | None]:
    path = Path(settings.experiments.registry_db)
    if not path.exists():
        return [], {"registry": "not initialised (built-in system runs)"}, None
    from app.experiments.registry import Registry
    from app.experiments.runtime import CONTEXT, VersionMismatch, resolve

    out, m, comp = [], {}, None
    reg = Registry(path)
    try:
        problems = reg.verify()
        m["integrity_problems"] = problems
        if problems:
            out.append(F("version.integrity", "CRITICAL", "versions",
                         "registry hash chain or status integrity failure", "0 problems",
                         {"problems": problems[:5]},
                         ["python -m app.experiments verify; restore the registry from backup",
                          "the AI arms will not start until resolved"],
                         {"state": "PAPER_TRADING_PAUSED", "arms": ["B", "canary"]}))
            return out, m, None
        active = reg.active_version_unchecked(CONTEXT)
        m["active_version"] = active
        actives = reg.all("SELECT experiment_id FROM experiments WHERE status = 'ACTIVE'")
        if len(actives) > 1:
            out.append(F("version.multiple_active", "CRITICAL", "versions",
                         "more than one ACTIVE experiment", "exactly one",
                         {"experiments": actives}, ["python -m app.experiments status"],
                         {"state": "PAPER_TRADING_PAUSED", "arms": ["B"]}))
        last = reg.all("SELECT * FROM registry_events WHERE kind = 'ROLLED_BACK_TO' ORDER BY "
                       "seq DESC LIMIT 1")
        if last and last[0]["seq"] == max(e["seq"] for e in reg.all(
                "SELECT seq FROM registry_events WHERE kind IN ('ACTIVATED', 'ROLLED_BACK_TO')")) \
                and active != last[0]["subject"]:
            out.append(F("version.rollback_not_restored", "CRITICAL", "versions",
                         "rollback did not restore the expected version",
                         f"active == {last[0]['subject']}", {"active": active},
                         ["python -m app.experiments history"],
                         {"state": "PAPER_TRADING_PAUSED", "arms": ["B"]}))
        rejected_late = reg.all("SELECT subject, reason FROM registry_events WHERE kind = "
                                "'TRANSITION' AND to_status = 'REJECTED' AND from_status IN "
                                "('SHADOW', 'PAPER_CANARY') ORDER BY seq DESC LIMIT 5")
        m["late_rejections"] = rejected_late
        try:
            r = resolve(settings)
            comp = r["components"]
            m["shadow"] = r["shadow"] and r["shadow"]["experiment"]
            m["canary"] = r["canary"] and r["canary"]["experiment"]
        except VersionMismatch as exc:
            out.append(F("version.code_mismatch", "CRITICAL", "versions", str(exc)[:300],
                         "running code == approved version", {}, [
                             "register and promote a new version (Phase 14)"],
                         {"state": "PAPER_TRADING_PAUSED", "arms": ["B"]}))
    finally:
        reg.close()
    return out, m, comp


def check_storage(settings, now: datetime) -> tuple[list, dict]:
    import sqlite3

    out, m = [], {}
    dbs = {"paper": settings.paper.db_path, "ai_paper": settings.integrated.ai_db_path,
           "ledger": settings.ai_eval.ledger_db, "learning": settings.learning.db_path,
           "registry": settings.experiments.registry_db, "monitoring": settings.monitoring.db_path}
    for name, p in dbs.items():
        p = Path(p)
        if not p.exists():
            m[name] = "absent"
            continue
        try:
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            ok = c.execute("PRAGMA quick_check").fetchone()[0]
            c.close()
        except sqlite3.Error as exc:
            ok = str(exc)
        m[name] = ok
        if ok != "ok":
            out.append(F(f"storage.db.{name}", "CRITICAL", "storage",
                         f"database {name} failed its integrity check", "ok", {"result": ok},
                         ["stop the scheduler; restore from the latest verified backup"],
                         {"state": "PAPER_TRADING_PAUSED",
                          "arms": {"paper": ["A"], "ai_paper": ["B"]}.get(name, [])}))
    free = shutil.disk_usage(Path(settings.paper.db_path).parent if Path(
        settings.paper.db_path).parent.exists() else Path(".")).free / 2 ** 20
    m["free_disk_mb"] = round(free)
    if free < settings.ops.min_free_disk_mb:
        out.append(F("storage.disk", "CRITICAL", "storage", "low free disk space",
                     f">= {settings.ops.min_free_disk_mb} MB", {"free_mb": round(free)},
                     ["free space; run retention (python -m app.ops retention)"],
                     {"state": "PAPER_TRADING_PAUSED", "arms": ["A", "B", "canary"]}))
    backups = sorted(Path(settings.ops.backups_dir).glob("paper-*.db")) \
        if Path(settings.ops.backups_dir).exists() else []
    if backups:
        age_h = (now.timestamp() - backups[-1].stat().st_mtime) / 3600
        m["latest_backup_age_hours"] = round(age_h, 1)
        if age_h > settings.monitoring.max_backup_age_hours:
            out.append(F("storage.backup_age", "WARNING", "storage", "latest backup is old",
                         f"<= {settings.monitoring.max_backup_age_hours} h",
                         {"age_hours": round(age_h, 1)}, ["python -m app.ops backup"]))
    else:
        m["latest_backup_age_hours"] = None
    return out, m


# -- performance drift (full scope only) -------------------------------------------------


def check_performance(settings, a_store, b_store, now: datetime) -> tuple[list, dict]:
    from app.monitoring import drift

    cfg, out, m = settings.monitoring, [], {}
    for arm, store in (("A", a_store), ("B", b_store)):
        pos = _rows(store, "SELECT trading_date, net_pnl, entry_price, exit_price FROM positions "
                           "WHERE state = 'CLOSED' ORDER BY exit_time")
        if not pos:
            m[arm] = {"closed_trades": 0, "kind": "INSUFFICIENT_EVIDENCE"}
            continue
        days = sorted({p["trading_date"] for p in pos})
        cut = days[-cfg.recent_window_sessions] if len(days) > cfg.recent_window_sessions \
            else None
        ref = [float(p["net_pnl"]) for p in pos if cut and p["trading_date"] < cut]
        rec = [float(p["net_pnl"]) for p in pos if not cut or p["trading_date"] >= cut]
        perf = drift.performance_drift(ref, rec, cfg)
        feats = {"relative_volume": [], "opening_range_width": []}
        ref_f = {k: [] for k in feats}
        for r in _rows(store, "SELECT trading_date, relative_volume, opening_range_high, "
                              "opening_range_low FROM signal_audit"):
            target = ref_f if cut and r["trading_date"] < cut else feats
            if r["relative_volume"]:
                target["relative_volume"].append(float(r["relative_volume"]))
            if r["opening_range_high"] and r["opening_range_low"]:
                target["opening_range_width"].append(float(r["opening_range_high"])
                                                     - float(r["opening_range_low"]))
        data = drift.data_drift(ref_f, feats, cfg) if cut else []
        nets = [float(p["net_pnl"]) for p in pos]
        streak = drift.longest_losing_streak(nets[-50:])
        kind = drift.classify(perf, data)
        m[arm] = {"closed_trades": len(pos), "sessions": len(days), "performance": perf,
                  "data_drift": data, "classification": kind, "recent_losing_streak": streak,
                  "period": [days[0], days[-1]]}
        if perf["kind"] == "PERFORMANCE_DRIFT":
            out.append(F(f"perf.drift.{arm}", "WARNING", "performance",
                         "performance deterioration supported by sufficient evidence",
                         f"recent CI entirely below reference CI; >= {cfg.min_trades_for_drift} "
                         "trades each", {**perf, "classification": kind},
                         ["review recent sessions and data quality first",
                          "no parameter is changed automatically; candidate changes go through "
                          "Phase 13/14"], {"state": "MANUAL_REVIEW_REQUIRED", "arms": [arm]}))
        for d in data:
            if d["kind"] == "DATA_DRIFT":
                out.append(F(f"perf.data_drift.{arm}.{d['feature']}", "INFO", "performance",
                             f"input distribution changed: {d['feature']}",
                             f"KS p >= {cfg.data_drift_p_value}", d,
                             ["inputs changed; this alone does not mean the strategy stopped "
                              "working"]))
        if streak >= cfg.losing_streak_info:
            out.append(F(f"perf.losing_streak.{arm}", "INFO", "performance",
                         "losing streak (not drift by itself)", f"< {cfg.losing_streak_info}",
                         {"streak": streak, "closed_trades": len(pos)},
                         ["normal variance is possible; see drift classification"]))
    return out, m
