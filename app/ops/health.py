"""Operational health: is the application able to do its job right now?

Health is derived from dependencies and persisted state, never from "the
process is running". Each check returns PASS, WARN, FAIL or SKIP with a
reason:

| check | FAIL when | WARN when |
|---|---|---|
| config | settings invalid / not paper mode | live provider without credentials, holiday list missing the year, fee tariff older than the limit |
| database | cannot open, corrupt, newer schema | - |
| permissions | - | DB, data folder or .env readable by group/others |
| disk | free space < limit / 4 | free space < ``ops.min_free_disk_mb`` |
| reconciliation | CRITICAL/ERROR findings | WARNING findings |
| session | in market hours: session stalled (no step for ``max_step_gap_minutes``) or scheduler aborted | session not started in market hours, NEEDS_ATTENTION sessions, unresolved positions, interrupted |
| market_data | latest step blocked signals (no fresh data) or provider circuit OPEN | stale/missing/invalid candles at the latest step, provider DEGRADED/DISCONNECTED |
| reports | - | a finished session has no report, or report generation failed |
| alerts | an open CRITICAL alert | an open ERROR alert |
| risk | - | daily-loss lock active (informational; trading correctly stopped) |
| provider_probe | probe call failed (``--probe`` only; one read-only quote) | - |

Overall status: UNHEALTHY if any FAIL, DEGRADED if any WARN, else HEALTHY.
"""

from __future__ import annotations

import stat
from datetime import date, datetime, timedelta
from pathlib import Path

from app.config import PROJECT_ROOT, Settings
from app.paper.store import PaperStore, StoreError
from app.utils.fs import free_disk_mb
from app.utils.market_calendar import MarketCalendar


def _check(name: str, status: str, detail: str, **data) -> dict:
    return {"check": name, "status": status, "detail": detail, **({"data": data} if data else {})}


def _loose(path: Path) -> bool:
    return path.exists() and bool(path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def health_report(settings: Settings, now: datetime, db_path: Path | None = None,
                  probe_provider=None) -> dict:
    cal = MarketCalendar(settings.market)
    now = now.astimezone(cal.tz)
    today = now.date()
    checks: list[dict] = []

    # -- config ------------------------------------------------------------------
    warns = []
    if settings.execution_mode != "paper":  # unreachable: rejected at load
        checks.append(_check("config", "FAIL", "execution mode is not paper"))
    if settings.data.provider.value == "groww" and (
            settings.broker_api_key is None or (settings.broker_totp_secret is None
                                                and settings.broker_api_secret is None)):
        warns.append("Groww provider configured but credentials are missing")
    fails = []
    if settings.data.provider.value == "groww":
        # A live provider trades only the configured universe; without it no
        # session can run, so the system is not able to do its job (Phase 9 fix).
        uni = Path(settings.data.universe_file)
        lines = [l for l in uni.read_text().splitlines()[1:] if l.strip()
                 and not l.lstrip().startswith("#")] if uni.exists() else []
        if not lines:
            fails.append(f"live provider configured but {uni.name} lists no symbols; no "
                         "session can run")
    if not cal.covers_year(today.year):
        warns.append(f"holiday list has no entries for {today.year}")
    try:
        from app.costs.schedule import load_fee_schedule

        fee = load_fee_schedule(settings)
        age = (today - fee.verified_on).days
        if age > settings.evaluation.max_fee_schedule_age_days:
            warns.append(f"fee schedule verified {age} days ago")
    except Exception as exc:  # noqa: BLE001
        warns.append(f"fee schedule unavailable: {exc}")
    if not any(c["check"] == "config" for c in checks):
        checks.append(_check("config", "FAIL" if fails else ("WARN" if warns else "PASS"),
                             "; ".join(fails + warns) or "valid; execution mode paper",
                             provider=settings.data.provider.value))

    # -- database ----------------------------------------------------------------
    db = Path(db_path or settings.paper.db_path)
    # Permissions are checked before the database is opened (opening the store
    # applies owner-only permissions, which would hide the finding).
    loose = [str(p) for p in (db, db.parent, PROJECT_ROOT / ".env") if _loose(p)]
    checks.append(_check("permissions", "WARN" if loose else "PASS",
                         ("readable by group/others: " + ", ".join(loose) +
                          " (run `python -m app.ops harden`)") if loose else "owner-only"))
    store = None
    if not db.exists():
        checks.append(_check("database", "WARN", f"no paper database yet at {db}"))
    else:
        try:
            store = PaperStore(db)
            store.ping()
            checks.append(_check("database", "PASS",
                                 f"open, integrity ok, schema v{store.schema_version()}"))
        except (StoreError, Exception) as exc:  # noqa: BLE001
            checks.append(_check("database", "FAIL", f"unavailable: {exc}"))
            store = None

    # -- disk -------------------------------------------------------------------------
    free = free_disk_mb(db.parent)
    need = settings.ops.min_free_disk_mb
    checks.append(_check("disk", "FAIL" if free < need / 4 else ("WARN" if free < need
                                                                  else "PASS"),
                         f"{free:,.0f} MB free at {db.parent} (minimum {need} MB)",
                         free_mb=round(free)))

    if store is not None:
        try:
            checks += _state_checks(store, settings, cal, now, today)
        finally:
            store.close()

    if probe_provider is not None:
        checks.append(_probe(probe_provider))

    statuses = {c["status"] for c in checks}
    overall = "UNHEALTHY" if "FAIL" in statuses else ("DEGRADED" if "WARN" in statuses
                                                      else "HEALTHY")
    return {"status": overall, "checked_at": now.isoformat(), "database": str(db),
            "checks": checks}


def _state_checks(store: PaperStore, settings: Settings, cal: MarketCalendar, now: datetime,
                  today: date) -> list[dict]:
    from app.costs.schedule import load_fee_schedule
    from app.ops.alerts import AlertManager
    from app.ops.reconciliation import full_reconciliation

    out = []
    # -- reconciliation ------------------------------------------------------------
    try:
        schedule = load_fee_schedule(settings)
    except Exception:  # noqa: BLE001
        schedule = None
    rec = full_reconciliation(store, settings, schedule, today, now)
    c = rec["counts"]
    out.append(_check("reconciliation", {"OK": "PASS", "WARN": "WARN"}.get(rec["status"], "FAIL"),
                      f"{c['CRITICAL']} critical, {c['ERROR']} error, {c['WARNING']} warning "
                      "finding(s)", findings=rec["findings"][:20]))

    # -- session / scheduler ---------------------------------------------------------
    sched = store.get_meta("scheduler") or {}
    beat = store.get_meta("heartbeat") or {}
    session = store.session(today)
    in_hours = cal.is_regular_session(today) and \
        cal.session_bounds(today)[0] <= now <= cal.session_bounds(today)[1]
    problems, status = [], "PASS"
    if sched.get("state") == "ABORTED" and sched.get("day") == today.isoformat():
        status, problems = "FAIL", [f"scheduler aborted: {sched.get('reason')}"]
    elif in_hours:
        if session is None:
            status, problems = "WARN", ["market is open but no paper session has started"]
        elif session["state"] == "ACTIVE":
            last = datetime.fromisoformat(beat["ts"]) if beat.get("ts") else None
            gap = timedelta(minutes=settings.ops.max_step_gap_minutes)
            if last is None or now - last > gap:
                status = "FAIL"
                problems.append(f"session stalled: last step {beat.get('ts', 'never')}, more "
                                f"than {settings.ops.max_step_gap_minutes} min ago")
    if sched.get("state") == "INTERRUPTED" and sched.get("day") == today.isoformat():
        status = "WARN" if status == "PASS" else status
        problems.append(f"session interrupted: {sched.get('reason')}; restart to resume")
    attention = [r["trading_date"] for r in store.all(
        "SELECT trading_date FROM sessions WHERE state = 'NEEDS_ATTENTION'")]
    if attention:
        status = "WARN" if status == "PASS" else status
        problems.append(f"sessions needing attention: {attention}")
    out.append(_check("session", status, "; ".join(problems) or (
        f"session {today}: {session['state']}" if session else "no session today"),
        scheduler=sched, last_step=beat.get("ts")))

    # -- market data ----------------------------------------------------------------
    if beat:
        prov = beat.get("provider") or {}
        md_status, md = "PASS", []
        if beat.get("signals_blocked"):
            md_status = "FAIL"
            md.append(f"new signals blocked: {beat.get('blocked_reason')}")
        if prov.get("circuit") == "OPEN":
            md_status = "FAIL"
            md.append(f"provider circuit OPEN ({prov.get('consecutive_failures')} failures)")
        elif prov.get("state") in ("DEGRADED", "DISCONNECTED") and md_status == "PASS":
            md_status = "WARN"
            md.append(f"provider {prov.get('state')}: {prov.get('last_error')}")
        dq = store.one("SELECT SUM(missing) AS missing, SUM(invalid_ohlc + invalid_volume) AS "
                       "invalid, SUM(stale) AS stale, SUM(error IS NOT NULL) AS errors FROM "
                       "dq_batches WHERE trading_date = ? AND ts = (SELECT MAX(ts) FROM "
                       "dq_batches WHERE trading_date = ?)", (today.isoformat(),
                                                             today.isoformat())) \
            if store.one("SELECT 1 FROM sqlite_master WHERE name = 'dq_batches'") else None
        if dq is not None and dq["missing"] is not None and \
                (dq["missing"] or dq["invalid"] or dq["stale"] or dq["errors"]) \
                and md_status == "PASS":
            md_status = "WARN"
            md.append(f"latest step: {dq['missing']} missing, {dq['invalid']} invalid "
                      f"candles, {dq['stale']} stale, {dq['errors']} failed symbol(s)")
        out.append(_check("market_data", md_status,
                          "; ".join(md) or f"{beat.get('symbols_ok')}/{beat.get('symbols_total')} "
                                           f"symbols fresh at {beat.get('ts')}",
                          provider=prov))
    else:
        out.append(_check("market_data", "SKIP", "no step has run yet"))

    # -- reports ------------------------------------------------------------------------
    finished = [r["trading_date"] for r in store.all(
        "SELECT trading_date FROM sessions WHERE state IN ('CLOSED', 'NEEDS_ATTENTION')")]
    missing = [d for d in finished if store.report(date.fromisoformat(d)) is None]
    failed = [r["trading_date"] for r in store.all(
        "SELECT e.trading_date FROM events e WHERE e.kind = 'REPORT' AND e.level = 'ERROR' "
        "AND NOT EXISTS (SELECT 1 FROM events s WHERE s.kind = 'REPORT' AND s.level = 'INFO' "
        "AND s.trading_date = e.trading_date AND s.id > e.id)")]
    out.append(_check("reports", "WARN" if (missing or failed) else "PASS",
                      (f"missing: {missing} " if missing else "")
                      + (f"generation failed: {sorted(set(failed))}" if failed else "")
                      or f"{len(finished)} finished session(s) reported"))

    # -- alerts / risk --------------------------------------------------------------------
    open_alerts = AlertManager(store, settings).open_alerts()
    sev = {a["severity"] for a in open_alerts}
    out.append(_check("alerts", "FAIL" if "CRITICAL" in sev else ("WARN" if "ERROR" in sev
                                                                  else "PASS"),
                      f"{len(open_alerts)} open alert(s)",
                      open=[{k: a[k] for k in ("alert_key", "severity", "message", "occurrences",
                                               "last_seen", "notify_status")}
                            for a in open_alerts[:20]]))
    locked = session is not None and session["locked"]
    out.append(_check("risk", "WARN" if locked else "PASS",
                      f"daily-loss lock active: {session['lock_reason']}" if locked
                      else "no risk lock today"))
    return out


def _probe(provider) -> dict:
    """One read-only quote for the first instrument (never an order call).
    Outside market hours the quote is usually rejected as stale; that still
    proves authentication and connectivity, and is reported as such."""
    from app.data.models import DataQualityError

    try:
        sym = provider.list_instruments()[0].symbol
        q = provider.get_quote(sym)
        return _check("provider_probe", "PASS", f"quote {sym} {q.last_price} at "
                                                f"{q.timestamp.isoformat()}")
    except DataQualityError as exc:
        return _check("provider_probe", "PASS", f"authenticated and connected; data rejected "
                                                f"({exc}) - expected outside market hours")
    except Exception as exc:  # noqa: BLE001
        return _check("provider_probe", "FAIL", f"{type(exc).__name__}: {exc}")
