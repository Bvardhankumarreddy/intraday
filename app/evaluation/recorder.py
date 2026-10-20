"""Evaluation sessions, data-quality ledger and signal audit trail.

An *evaluation* freezes one configuration (``settings.version()``) and a
start date. From then on every regular NSE trading day is a scheduled
session, whether or not the application ran:

    SCHEDULED -> RUNNING -> COMPLETED | INCOMPLETE | FAILED
    SCHEDULED (day passed, never started)           -> INCOMPLETE

- COMPLETED: the session ran to the end, monitoring and data coverage met
  the thresholds, nothing unresolved, and reconciliation found no issues.
- INCOMPLETE: application not running, interrupted, coverage below
  threshold, unresolved positions or reconciliation issues. Such sessions are
  never counted as successful no-trade sessions.
- FAILED: preflight failed or the scheduler aborted.

Session kind comes from the data mode: LIVE, DELAYED, REPLAY (historical)
or SIMULATED (mock). Only LIVE sessions on the frozen configuration form
the primary evaluation; everything else is reported separately.

Tables live in the same SQLite file as the paper store, so updates share
its transactions. Rows are keyed deterministically, so re-recording is
idempotent.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import StrEnum

from app.config import Settings
from app.data.models import DataMode, parse_interval
from app.evaluation.quality import BatchQuality
from app.paper.store import PaperStore, _s
from app.utils.market_calendar import MarketCalendar

EVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    start_date TEXT NOT NULL,
    frozen_config_version TEXT NOT NULL,
    frozen_settings TEXT NOT NULL,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS eval_sessions (
    session_id TEXT PRIMARY KEY,
    evaluation_id TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    status_reason TEXT,
    primary_eval INTEGER NOT NULL DEFAULT 0,
    provider TEXT,
    data_mode TEXT,
    config_version TEXT,
    strategy_version TEXT,
    execution_model_version TEXT,
    fee_schedule_id TEXT,
    fee_verified_on TEXT,
    fee_model_flag TEXT,
    started_at TEXT,
    ended_at TEXT,
    steps_expected INTEGER NOT NULL DEFAULT 0,
    steps_executed INTEGER NOT NULL DEFAULT 0,
    UNIQUE (evaluation_id, trading_date, kind)
);
CREATE TABLE IF NOT EXISTS eval_steps (
    trading_date TEXT NOT NULL,
    ts TEXT NOT NULL,
    slot INTEGER NOT NULL,  -- completed candles at ts; coverage counts distinct slots
    provider_state TEXT,
    provider_retries INTEGER,
    symbols_total INTEGER NOT NULL,
    symbols_ok INTEGER NOT NULL,
    signals_blocked INTEGER NOT NULL,
    blocked_reason TEXT,
    PRIMARY KEY (trading_date, ts)
);
CREATE TABLE IF NOT EXISTS dq_batches (
    trading_date TEXT NOT NULL,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    expected INTEGER NOT NULL,
    received INTEGER NOT NULL,
    missing INTEGER NOT NULL,
    duplicates INTEGER NOT NULL,
    out_of_order INTEGER NOT NULL,
    invalid_ohlc INTEGER NOT NULL,
    invalid_volume INTEGER NOT NULL,
    gaps INTEGER NOT NULL,
    last_candle_end TEXT,
    stale INTEGER NOT NULL,
    error TEXT,
    PRIMARY KEY (trading_date, ts, symbol)
);
CREATE TABLE IF NOT EXISTS signal_audit (
    audit_id TEXT PRIMARY KEY,
    trading_date TEXT NOT NULL,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    data_source TEXT NOT NULL,
    data_mode TEXT NOT NULL,
    screen_status TEXT NOT NULL,
    signal_status TEXT NOT NULL,
    signal_id TEXT,
    close TEXT, opening_range_high TEXT, opening_range_low TEXT, vwap TEXT, ema TEXT,
    relative_volume TEXT, relative_volume_today TEXT, relative_volume_baseline TEXT,
    relative_volume_sessions INTEGER,
    entry_reference TEXT, stop TEXT, target TEXT,
    quantity INTEGER, est_round_trip_costs TEXT, net_reward_risk TEXT,
    risk_status TEXT, risk_reasons TEXT,
    decision TEXT NOT NULL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS config_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    ts TEXT NOT NULL,
    from_version TEXT NOT NULL,
    to_version TEXT NOT NULL,
    changed_keys TEXT NOT NULL,
    UNIQUE (evaluation_id, trading_date, to_version)
);
CREATE TABLE IF NOT EXISTS reconciliation_issues (
    trading_date TEXT NOT NULL,
    code TEXT NOT NULL,
    ref TEXT NOT NULL,
    message TEXT NOT NULL,
    found_at TEXT NOT NULL,
    PRIMARY KEY (trading_date, code, ref)
);
"""


class SessionKind(StrEnum):
    LIVE = "LIVE"
    DELAYED = "DELAYED"
    REPLAY = "REPLAY"
    SIMULATED = "SIMULATED"


class EvalStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    INCOMPLETE = "INCOMPLETE"
    FAILED = "FAILED"


KIND_FOR_MODE = {
    DataMode.LIVE: SessionKind.LIVE,
    DataMode.DELAYED: SessionKind.DELAYED,
    DataMode.HISTORICAL: SessionKind.REPLAY,
    DataMode.SIMULATED: SessionKind.SIMULATED,
}


class EvaluationError(RuntimeError):
    pass


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def settings_snapshot(settings: Settings) -> dict:
    return settings.model_dump(mode="json", exclude={
        "broker_api_key", "broker_api_secret", "broker_totp_secret", "llm_api_key",
        "whatsapp_token", "whatsapp"})


class EvaluationRecorder:
    def __init__(self, store: PaperStore, settings: Settings, calendar: MarketCalendar):
        self.store = store
        self.settings = settings
        self.calendar = calendar
        store.conn.executescript(EVAL_SCHEMA)

    # -- evaluations ------------------------------------------------------------

    def create_evaluation(self, name: str, start: date, now: datetime) -> str:
        if self.active() is not None:
            raise EvaluationError("an evaluation is already active; finish it first")
        eid = "EVAL-" + hashlib.sha256(f"{name}|{start}|{self.settings.version()}".encode()) \
            .hexdigest()[:10]
        with self.store.transaction():
            self.store._insert("evaluations", {
                "evaluation_id": eid, "name": name, "start_date": start,
                "frozen_config_version": self.settings.version(),
                "frozen_settings": json.dumps(settings_snapshot(self.settings), sort_keys=True),
                "created_at": now, "active": 1})
            self.store.event(now, None, "EVALUATION", "INFO",
                             f"evaluation {eid} '{name}' started from {start}; config "
                             f"{self.settings.version()} frozen")
        return eid

    def active(self):
        return self.store.one("SELECT * FROM evaluations WHERE active = 1")

    def finish_evaluation(self, now: datetime) -> None:
        ev = self._require()
        with self.store.transaction():
            self.store._update("evaluations", "evaluation_id", ev["evaluation_id"], {"active": 0})
            self.store.event(now, None, "EVALUATION", "INFO",
                             f"evaluation {ev['evaluation_id']} finished")

    def _require(self):
        ev = self.active()
        if ev is None:
            raise EvaluationError("no active evaluation; start one with "
                                  "`python -m app.evaluation start`")
        return ev

    # -- sessions ---------------------------------------------------------------

    @staticmethod
    def session_id(evaluation_id: str, day: date, kind: SessionKind) -> str:
        return f"{evaluation_id}:{day.isoformat()}:{kind.value}"

    def session(self, day: date, kind: SessionKind = SessionKind.LIVE):
        ev = self._require()
        return self.store.one("SELECT * FROM eval_sessions WHERE session_id = ?",
                              (self.session_id(ev["evaluation_id"], day, kind),))

    def sync_schedule(self, today: date, now: datetime) -> list[date]:
        """Ensure a LIVE session row for every regular trading day from the
        evaluation start to ``today``. Past days that never ran become
        INCOMPLETE ("application did not run"). Returns newly-missed days."""
        ev = self._require()
        start = date.fromisoformat(ev["start_date"])
        missed = []
        with self.store.transaction():
            for day in self.calendar.trading_days(start, today):
                if not self.calendar.is_regular_session(day):
                    continue
                sid = self.session_id(ev["evaluation_id"], day, SessionKind.LIVE)
                self.store._insert("eval_sessions", {
                    "session_id": sid, "evaluation_id": ev["evaluation_id"],
                    "trading_date": day, "kind": SessionKind.LIVE.value,
                    "status": EvalStatus.SCHEDULED.value, "primary_eval": 1,
                    "steps_expected": self._steps_expected(day)}, or_ignore=True)
                row = self.store.one("SELECT status FROM eval_sessions WHERE session_id = ?",
                                     (sid,))
                _, close_dt = self.calendar.session_bounds(day)
                if row["status"] == EvalStatus.SCHEDULED.value and now >= close_dt:
                    self.store._update("eval_sessions", "session_id", sid, {
                        "status": EvalStatus.INCOMPLETE.value,
                        "status_reason": "application did not run during this session"})
                    self.store.event(now, day, "EVALUATION", "WARNING",
                                     "session missed: application did not run")
                    missed.append(day)
                elif row["status"] == EvalStatus.RUNNING.value and day < today:
                    self.store._update("eval_sessions", "session_id", sid, {
                        "status": EvalStatus.INCOMPLETE.value,
                        "status_reason": "session was interrupted and never finished"})
                    self.store.event(now, day, "EVALUATION", "WARNING",
                                     "session interrupted: never finished")
        return missed

    def _steps_expected(self, day: date) -> int:
        open_dt, close_dt = self.calendar.session_bounds(day)
        step = parse_interval(self.settings.strategy.candle_interval).timedelta
        return int((close_dt - open_dt) / step)

    def session_started(self, day: date, now: datetime, provider_name: str, mode: DataMode,
                        fee_schedule, strategy_version: str, execution_model: str) -> str:
        ev = self._require()
        kind = KIND_FOR_MODE[mode]
        sid = self.session_id(ev["evaluation_id"], day, kind)
        version = self.settings.version()
        primary = int(kind is SessionKind.LIVE and version == ev["frozen_config_version"])
        flag = self.fee_model_flag(fee_schedule, day)
        with self.store.transaction():
            self.store._insert("eval_sessions", {
                "session_id": sid, "evaluation_id": ev["evaluation_id"], "trading_date": day,
                "kind": kind.value, "status": EvalStatus.SCHEDULED.value,
                "primary_eval": primary, "steps_expected": self._steps_expected(day)},
                or_ignore=True)
            self.store._update("eval_sessions", "session_id", sid, {
                "status": EvalStatus.RUNNING.value, "status_reason": None,
                "primary_eval": primary, "provider": provider_name, "data_mode": mode.value,
                "config_version": version, "strategy_version": strategy_version,
                "execution_model_version": execution_model,
                "fee_schedule_id": fee_schedule.schedule_id if fee_schedule else None,
                "fee_verified_on": fee_schedule.verified_on if fee_schedule else None,
                "fee_model_flag": flag, "started_at": now})
            self.store.event(now, day, "EVALUATION", "INFO",
                             f"session {sid} RUNNING ({kind.value}"
                             f"{', primary' if primary else ', not primary'})")
            if flag:
                self.store.event(now, day, "EVALUATION", "WARNING", flag)
            self._record_config_change(ev, day, now, version)
        return sid

    def fee_model_flag(self, fee_schedule, day: date) -> str | None:
        if fee_schedule is None:
            return "fee schedule unavailable"
        age = (day - fee_schedule.verified_on).days
        limit = self.settings.evaluation.max_fee_schedule_age_days
        if age > limit:
            return (f"fee schedule {fee_schedule.schedule_id} last verified "
                    f"{fee_schedule.verified_on} ({age} days ago, limit {limit}); re-verify "
                    "the broker tariff")
        return None

    def _record_config_change(self, ev, day: date, now: datetime, version: str) -> None:
        if version == ev["frozen_config_version"]:
            return
        frozen = _flatten(json.loads(ev["frozen_settings"]))
        current = _flatten(settings_snapshot(self.settings))
        changed = sorted(k for k in set(frozen) | set(current) if frozen.get(k) != current.get(k))
        self.store._insert("config_changes", {
            "evaluation_id": ev["evaluation_id"], "trading_date": day, "ts": now,
            "from_version": ev["frozen_config_version"], "to_version": version,
            "changed_keys": json.dumps(changed)}, or_ignore=True)
        self.store.event(now, day, "EVALUATION", "WARNING",
                         f"configuration differs from the frozen evaluation config "
                         f"({len(changed)} setting(s)); session excluded from the primary "
                         "evaluation")

    def session_failed(self, day: date, now: datetime, reason: str,
                       kind: SessionKind = SessionKind.LIVE) -> None:
        ev = self._require()
        sid = self.session_id(ev["evaluation_id"], day, kind)
        with self.store.transaction():
            self.store._insert("eval_sessions", {
                "session_id": sid, "evaluation_id": ev["evaluation_id"], "trading_date": day,
                "kind": kind.value, "status": EvalStatus.FAILED.value,
                "primary_eval": int(kind is SessionKind.LIVE),
                "steps_expected": self._steps_expected(day)}, or_ignore=True)
            self.store._update("eval_sessions", "session_id", sid, {
                "status": EvalStatus.FAILED.value, "status_reason": reason, "ended_at": now})
            self.store.event(now, day, "EVALUATION", "ERROR", f"session FAILED: {reason}")

    def session_incomplete(self, day: date, now: datetime, reason: str,
                           kind: SessionKind = SessionKind.LIVE) -> None:
        ev = self._require()
        sid = self.session_id(ev["evaluation_id"], day, kind)
        with self.store.transaction():
            self.store._update("eval_sessions", "session_id", sid, {
                "status": EvalStatus.INCOMPLETE.value, "status_reason": reason, "ended_at": now})
            self.store.event(now, day, "EVALUATION", "WARNING", f"session INCOMPLETE: {reason}")

    def session_finished(self, day: date, now: datetime, mode: DataMode,
                         recon_issues: list) -> EvalStatus:
        """Decide COMPLETED vs INCOMPLETE from coverage, unresolved positions and
        reconciliation issues. Never silently promotes an incomplete session."""
        ev = self._require()
        kind = KIND_FOR_MODE[mode]
        sid = self.session_id(ev["evaluation_id"], day, kind)
        row = self.store.one("SELECT * FROM eval_sessions WHERE session_id = ?", (sid,))
        cfg = self.settings.evaluation
        reasons = []
        executed = row["steps_executed"]
        step_cov = executed / row["steps_expected"] * 100 if row["steps_expected"] else 0
        if step_cov < cfg.min_step_coverage_pct:
            reasons.append(f"monitoring coverage {step_cov:.0f}% < {cfg.min_step_coverage_pct}%")
        data_cov = self.data_coverage(day)
        if data_cov is None:
            reasons.append("no market data was received")
        elif data_cov < cfg.min_data_coverage_pct:
            reasons.append(f"data coverage {data_cov:.0f}% < {cfg.min_data_coverage_pct}%")
        paper = self.store.session(day)
        if paper is not None and paper["state"] == "NEEDS_ATTENTION":
            reasons.append("unresolved paper positions at session end")
        if recon_issues:
            reasons.append(f"{len(recon_issues)} reconciliation issue(s)")
        status = EvalStatus.INCOMPLETE if reasons else EvalStatus.COMPLETED
        with self.store.transaction():
            self.store._update("eval_sessions", "session_id", sid, {
                "status": status.value, "status_reason": "; ".join(reasons) or None,
                "ended_at": now})
            self.store.event(now, day, "EVALUATION", "INFO" if not reasons else "WARNING",
                             f"session {status.value}" + (f": {'; '.join(reasons)}"
                                                          if reasons else ""))
        return status

    # -- per-step recording -----------------------------------------------------

    def record_step(self, day: date, now: datetime, batches: list[BatchQuality],
                    provider_health: dict | None, blocked: bool, blocked_reason: str | None,
                    mode: DataMode) -> None:
        ev = self.active()
        open_dt, _ = self.calendar.session_bounds(day)
        step = parse_interval(self.settings.strategy.candle_interval).timedelta
        slot = max(int((now - open_dt) // step), 0)
        with self.store.transaction():
            new_slot = self.store.one("SELECT 1 FROM eval_steps WHERE trading_date = ? AND "
                                      "slot = ?", (day.isoformat(), slot)) is None
            self.store._insert("eval_steps", {
                "trading_date": day, "ts": now, "slot": slot,
                "provider_state": (provider_health or {}).get("state"),
                "provider_retries": (provider_health or {}).get("retries"),
                "symbols_total": len(batches), "symbols_ok": sum(b.ok for b in batches),
                "signals_blocked": int(blocked), "blocked_reason": blocked_reason},
                or_ignore=True)
            for b in batches:
                row = b.as_row()
                row["stale"] = int(row["stale"])
                self.store._insert("dq_batches", {"trading_date": day, "ts": now, **row},
                                   or_ignore=True)
            if new_slot and ev is not None:
                sid = self.session_id(ev["evaluation_id"], day, KIND_FOR_MODE[mode])
                self.store.conn.execute(
                    "UPDATE eval_sessions SET steps_executed = steps_executed + 1 "
                    "WHERE session_id = ?", (sid,))

    def data_coverage(self, day: date) -> float | None:
        """Received / expected candles over all symbols at the session's last step."""
        last = self.store.one("SELECT MAX(ts) AS ts FROM dq_batches WHERE trading_date = ?",
                              (day.isoformat(),))
        if last is None or last["ts"] is None:
            return None
        rows = self.store.all("SELECT expected, received FROM dq_batches WHERE "
                              "trading_date = ? AND ts = ?", (day.isoformat(), last["ts"]))
        expected = sum(r["expected"] for r in rows)
        return 100.0 if expected == 0 else sum(r["received"] for r in rows) / expected * 100

    def record_audit(self, day: date, now: datetime, rows: list[dict]) -> None:
        with self.store.transaction():
            for row in rows:
                aid = hashlib.sha256(f"{day}|{row['symbol']}|{now.isoformat()}".encode()) \
                    .hexdigest()[:20]
                clean = {k: _s(v) if not isinstance(v, float) else str(v)
                         for k, v in row.items()}
                self.store._insert("signal_audit", {"audit_id": aid, "trading_date": day,
                                                    "ts": now, **clean}, or_ignore=True)

    def record_issues(self, day: date, now: datetime, issues: list) -> None:
        with self.store.transaction():
            for i in issues:
                self.store._insert("reconciliation_issues", {
                    "trading_date": day, "code": i.code, "ref": i.ref, "message": i.message,
                    "found_at": now}, or_ignore=True)
