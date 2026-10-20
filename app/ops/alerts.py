"""Persistent, deduplicated operational alerts.

No notification provider existed before Phase 8, so the default notifier
writes a structured log line (``LogNotifier``). Alerts are also stored in the
paper database (``alerts`` table) and shown by ``python -m app.ops health``
and the dashboard's Operations view.

- An alert is identified by a stable ``key`` (e.g. ``data.blocked``). Raising
  an alert that is already open increments ``occurrences`` and updates
  ``last_seen``. The notifier is called again only after
  ``ops.alert_repeat_minutes``, or when the severity escalates. This
  prevents alert storms.
- ``resolve(key)`` closes an alert; raising it again reopens it.
- Notifier failures never break the caller. The failure is stored
  (``notify_status = FAILED``, error text redacted) and logged, and the
  alert stays visible in the database.
- If the database itself is unavailable, the alert is logged only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from app.utils.logging import get_logger, redact_text

log = get_logger("app.ops.alerts")
SEVERITIES = ("INFO", "WARNING", "ERROR", "CRITICAL")


class Notifier(Protocol):
    name: str

    def send(self, alert: dict) -> None: ...


class LogNotifier:
    name = "log"

    def send(self, alert: dict) -> None:
        level = {"INFO": log.info, "WARNING": log.warning}.get(alert["severity"], log.error)
        level(f"ALERT [{alert['severity']}] {alert['component']}: {alert['message']}",
              extra={"event_type": "alert", "alert_key": alert["alert_key"],
                     "occurrences": alert["occurrences"]})


class AlertManager:
    def __init__(self, store, settings, notifier: Notifier | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.store = store
        self.settings = settings
        self.notifier = notifier or LogNotifier()
        self.clock = clock
        self.repeat = timedelta(minutes=settings.ops.alert_repeat_minutes)

    def raise_alert(self, key: str, severity: str, component: str, message: str,
                    now: datetime) -> dict | None:
        if severity not in SEVERITIES:
            raise ValueError(f"unknown severity {severity}")
        message = redact_text(message)
        try:
            row = self._upsert(key, severity, component, message, now)
        except (sqlite3.Error, RuntimeError) as exc:  # DB down: log-only fallback
            log.error(f"ALERT (not persisted: {exc}) [{severity}] {component}: {message}",
                      extra={"event_type": "alert", "alert_key": key})
            return None
        if row["notify"]:
            self._notify(row["alert"], now)
        return row["alert"]

    def _upsert(self, key, severity, component, message, now) -> dict:
        with self.store.transaction():
            old = self.store.one("SELECT * FROM alerts WHERE alert_key = ?", (key,))
            if old is None or old["status"] == "RESOLVED":
                if old is not None:
                    self.store.conn.execute("DELETE FROM alerts WHERE alert_key = ?", (key,))
                self.store._insert("alerts", {
                    "alert_key": key, "severity": severity, "component": component,
                    "message": message, "status": "OPEN", "first_seen": now, "last_seen": now,
                    "occurrences": 1})
                notify = True
            else:
                escalated = SEVERITIES.index(severity) > SEVERITIES.index(old["severity"])
                last = datetime.fromisoformat(old["last_notified_at"]) \
                    if old["last_notified_at"] else None
                notify = escalated or last is None or now - last >= self.repeat
                self.store.conn.execute(
                    "UPDATE alerts SET severity = ?, component = ?, message = ?, last_seen = ?, "
                    "occurrences = occurrences + 1 WHERE alert_key = ?",
                    (severity if escalated else old["severity"], component, message,
                     now.isoformat(), key))
            alert = dict(self.store.one("SELECT * FROM alerts WHERE alert_key = ?", (key,)))
        return {"alert": alert, "notify": notify}

    def _notify(self, alert: dict, now: datetime) -> None:
        try:
            self.notifier.send(alert)
            status, error = "SENT", None
        except Exception as exc:  # noqa: BLE001 - a notifier must never break the app
            status, error = "FAILED", redact_text(f"{type(exc).__name__}: {exc}")
            log.error(f"notifier {self.notifier.name} failed for alert {alert['alert_key']}: "
                      f"{error}", extra={"event_type": "notify_failed"})
        try:
            with self.store.transaction():
                self.store.conn.execute(
                    "UPDATE alerts SET last_notified_at = ?, notify_status = ?, notify_error = ? "
                    "WHERE alert_key = ?", (now.isoformat(), status, error, alert["alert_key"]))
        except (sqlite3.Error, RuntimeError):
            pass
        alert.update(notify_status=status, notify_error=error)

    def resolve(self, key: str, now: datetime) -> bool:
        try:
            with self.store.transaction():
                cur = self.store.conn.execute(
                    "UPDATE alerts SET status = 'RESOLVED', resolved_at = ? "
                    "WHERE alert_key = ? AND status = 'OPEN'", (now.isoformat(), key))
            return cur.rowcount == 1
        except (sqlite3.Error, RuntimeError):
            return False

    def open_alerts(self) -> list[dict]:
        return [dict(r) for r in self.store.all(
            "SELECT * FROM alerts WHERE status = 'OPEN' ORDER BY "
            "CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'ERROR' THEN 1 WHEN 'WARNING' THEN 2 "
            "ELSE 3 END, last_seen DESC")]


def safe_alert(store, settings, key: str, severity: str, component: str, message: str,
               now: datetime, notifier: Notifier | None = None) -> None:
    """Raise an alert without ever propagating an error to the caller."""
    try:
        AlertManager(store, settings, notifier).raise_alert(key, severity, component, message,
                                                            now)
    except Exception as exc:  # noqa: BLE001
        log.error(f"could not raise alert {key}: {exc}")
