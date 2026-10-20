"""Monitoring orchestration (Phase 15).

``run_monitor(settings, now, scope)``:
- scope "step" (every integrated step): data, calendar, AI, execution,
  versions and storage checks;
- scope "full" (end of session, or on demand): the above plus performance
  and data drift.

Then it syncs alerts (it raises current findings and resolves vanished ones
with a recovery notification), updates the system state, writes a snapshot,
and prunes snapshots older than the retention period. Monitoring reads
trading records only; a monitoring failure never touches a recommendation,
order or outcome.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from app.monitoring import checks, state
from app.monitoring.alerts import AlertCenter, MonitorStore

STEP_PREFIXES = {"data", "calendar", "ai", "exec", "version", "storage"}


def _open(path) -> object | None:
    from app.paper.store import PaperStore

    return PaperStore(path) if Path(path).exists() else None


def run_monitor(settings, now: datetime, scope: str = "step", stores: dict | None = None,
                notifiers=None) -> dict:
    from app.utils.market_calendar import MarketCalendar

    cal = MarketCalendar(settings.market)
    now = now.astimezone(cal.tz)
    own = stores is None
    stores = stores or {"A": _open(settings.paper.db_path),
                        "B": _open(settings.integrated.ai_db_path),
                        "canary": _open(settings.experiments.canary_db_path)}
    ms = MonitorStore(settings.monitoring.db_path)
    try:
        findings, metrics = [], {"scope": scope}
        version_f, version_m, comp = checks.check_versions(settings, now)
        for name, (f, m) in {
            "data": checks.check_data(settings, cal, stores.get("A"), now),
            "calendar": checks.check_calendar(settings, cal, stores.get("A"), now),
            "ai": checks.check_ai(settings, stores.get("B"), now, comp),
            "execution": checks.check_execution(settings, stores, now),
            "storage": checks.check_storage(settings, now),
        }.items():
            findings += f
            metrics[name] = m
        findings += version_f
        metrics["versions"] = version_m
        prefixes = set(STEP_PREFIXES)
        if scope == "full":
            f, m = checks.check_performance(settings, stores.get("A"), stores.get("B"), now)
            findings += f
            metrics["performance"] = m
            prefixes.add("perf")
        alerts = AlertCenter(ms, settings, notifiers).sync(findings, prefixes, now)
        last = ms.all("SELECT body FROM monitor_snapshots ORDER BY ts DESC LIMIT ?",
                      (settings.monitoring.recovery_passes,))
        blocking_now = [f["key"] for f in findings if f.get("effect", {}).get("state")]
        passes = 0
        if not blocking_now:
            passes = 1
            for s in last:
                if json.loads(s["body"]).get("blocking"):
                    break
                passes += 1
        st = state.update(ms, settings, findings, now, passes)
        snap = {"ts": now.isoformat(), "scope": scope, "state": st["state"],
                "paused_arms": st.get("paused_arms", {}), "blocking": blocking_now,
                "findings": [{k: f[k] for k in ("key", "severity", "component", "rule")}
                             for f in findings], "alerts": alerts, "metrics": metrics}
        with ms.store.transaction():
            ms.store._insert("monitor_snapshots", {"ts": now, "scope": scope,
                                                   "state": st["state"],
                                                   "body": json.dumps(snap, default=str)},
                             or_ignore=True)
            cutoff = now - timedelta(days=settings.monitoring.snapshot_retention_days)
            ms.store.conn.execute("DELETE FROM monitor_snapshots WHERE ts < ?",
                                  (cutoff.isoformat(),))
        return snap
    finally:
        ms.close()
        if own:
            for s in stores.values():
                if s is not None:
                    s.close()


class SessionMonitor:
    """Hooks for the integrated engine: run checks after each step, and report
    which arms are paused. Never raises into the trading loop."""

    def __init__(self, settings, stores: dict, notifiers=None):
        self.settings, self.stores, self.notifiers = settings, stores, notifiers
        self.last: dict | None = None
        self.errors: list[str] = []

    def after_step(self, now: datetime, scope: str = "step") -> None:
        try:
            self.last = run_monitor(self.settings, now, scope, self.stores, self.notifiers)
        except Exception as exc:  # noqa: BLE001 - monitoring must never break trading
            self.errors.append(f"{now.isoformat()}: {type(exc).__name__}: {exc}"[:300])

    def paused(self, now: datetime) -> dict:
        try:
            ms = MonitorStore(self.settings.monitoring.db_path)
            try:
                return state.paused_arms(ms, now)
            finally:
                ms.close()
        except Exception as exc:  # noqa: BLE001 - unreadable state: fail safe = pause AI arms
            self.errors.append(f"state unreadable: {exc}"[:300])
            return {"B": ["monitoring state unreadable"], "canary": ["monitoring state "
                                                                    "unreadable"]}
