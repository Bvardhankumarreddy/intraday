"""Monitoring store, alert center and local notifiers (Phase 15).

Database ``monitoring.db_path`` (WAL, owner-only, via ``PaperStore``):
- ``monitor_alerts``: one row per incident (``alert_id``). ``alert_key`` is the
  deduplication key; while an incident is OPEN, repeats update it instead of
  opening a new one. Fields: detection time, severity, component, rule,
  threshold, metric values and evidence, version, session, recommended
  investigation steps, group, status (OPEN / RESOLVED).
- ``monitor_alert_events``: append-only audit history (RAISED, REPEATED,
  ESCALATED, NOTIFIED, NOTIFY_FAILED, RESOLVED, ACKNOWLEDGED).
- ``monitor_state_events``: append-only system-state transitions (state.py).
- ``monitor_snapshots``: periodic metric snapshots for dashboards (retention
  bounded by ``monitoring.snapshot_retention_days``; audit tables are kept).

Delivery:
- A notification is sent on a new incident, on escalation, after
  ``alert_cooldown_minutes`` while still open, and on recovery (RESOLVED).
- Notifiers: ``LogNotifier`` (structured log line) and ``OutboxNotifier``
  (appends a JSON line to ``monitoring.alert_outbox`` and fsyncs it). The
  recorded status is exactly what happened: LOGGED, WRITTEN or FAILED. No
  external channel is configured, so nothing is ever recorded as
  "delivered" to a person.

Input hygiene: every text field is redacted for secrets, stripped of control
characters and newlines (no log injection), and length-bounded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from app.paper.store import PaperStore
from app.utils.logging import get_logger, redact_text

log = get_logger("app.monitoring")
SEVERITIES = ("INFO", "WARNING", "CRITICAL")
_CTRL = re.compile(r"[\x00-\x1f\x7f]+")

SCHEMA = """
CREATE TABLE IF NOT EXISTS monitor_alerts (
    alert_id TEXT PRIMARY KEY,
    alert_key TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    occurrences INTEGER NOT NULL,
    severity TEXT NOT NULL,
    component TEXT NOT NULL,
    grp TEXT NOT NULL,
    rule TEXT NOT NULL,
    threshold TEXT NOT NULL,
    evidence TEXT NOT NULL,
    version TEXT,
    session TEXT,
    steps TEXT NOT NULL,
    status TEXT NOT NULL,
    last_notified_at TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS monitor_alerts_open ON monitor_alerts (alert_key, status);
CREATE TABLE IF NOT EXISTS monitor_alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monitor_state_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    findings TEXT NOT NULL,
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS monitor_snapshots (
    ts TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    state TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS mon_alert_events_frozen BEFORE UPDATE ON monitor_alert_events
BEGIN SELECT RAISE(ABORT, 'alert history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS mon_alert_events_kept BEFORE DELETE ON monitor_alert_events
BEGIN SELECT RAISE(ABORT, 'alert history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS mon_state_frozen BEFORE UPDATE ON monitor_state_events
BEGIN SELECT RAISE(ABORT, 'state history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS mon_state_kept BEFORE DELETE ON monitor_state_events
BEGIN SELECT RAISE(ABORT, 'state history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS mon_alerts_kept BEFORE DELETE ON monitor_alerts
BEGIN SELECT RAISE(ABORT, 'alerts are kept'); END;
"""


def clean(text, limit: int = 500) -> str:
    """Redact secrets, remove control characters/newlines, bound length."""
    return _CTRL.sub(" ", redact_text(str(text)))[:limit]


def _j(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


class LogNotifier:
    name, status = "log", "LOGGED"

    def send(self, payload: dict) -> None:
        line = (f"MONITOR {payload['event']} [{payload['severity']}] {payload['component']}: "
                f"{payload['rule']} ({payload['alert_id']})")
        (log.error if payload["severity"] == "CRITICAL" else
         log.warning if payload["severity"] == "WARNING" else log.info)(clean(line, 400))


class OutboxNotifier:
    """Local alert interface: one JSON line per notification, fsynced. An
    external channel can later read this file (see docs/monitoring.md)."""

    name, status = "outbox", "WRITTEN"

    def __init__(self, path: Path):
        self.path = Path(path)

    def send(self, payload: dict) -> None:
        from app.utils.fs import private_dir, private_file

        private_dir(self.path.parent)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        private_file(self.path)


class MonitorStore:
    def __init__(self, path: Path | str):
        self.store = PaperStore(path)
        self.store.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.store.close()

    def all(self, sql, params=()) -> list[dict]:
        return [dict(r) for r in self.store.all(sql, params)]

    def one(self, sql, params=()):
        r = self.store.one(sql, params)
        return dict(r) if r else None


class AlertCenter:
    def __init__(self, ms: MonitorStore, settings, notifiers=None):
        self.ms, self.settings = ms, settings
        self.notifiers = notifiers if notifiers is not None else [
            LogNotifier(), OutboxNotifier(settings.monitoring.alert_outbox)]
        self.cooldown = timedelta(minutes=settings.monitoring.alert_cooldown_minutes)

    def _event(self, alert_id: str, kind: str, now: datetime, detail: dict) -> None:
        self.ms.store._insert("monitor_alert_events", {"alert_id": alert_id, "ts": now,
                                                       "kind": kind, "detail": _j(detail)})

    def _notify(self, alert: dict, event: str, now: datetime) -> list[str]:
        payload = {"event": event, "alert_id": alert["alert_id"],
                   "severity": alert["severity"], "component": alert["component"],
                   "group": alert["grp"], "rule": alert["rule"],
                   "threshold": alert["threshold"], "evidence": json.loads(alert["evidence"]),
                   "version": alert["version"], "session": alert["session"],
                   "steps": json.loads(alert["steps"]), "detected_at": alert["detected_at"],
                   "status": alert["status"], "ts": now.isoformat()}
        results = []
        for n in self.notifiers:
            try:
                n.send(payload)
                results.append(f"{n.name}:{n.status}")
                ok = True
            except Exception as exc:  # noqa: BLE001 - a notifier never breaks monitoring
                results.append(f"{n.name}:FAILED")
                ok = False
                err = clean(f"{type(exc).__name__}: {exc}", 300)
            self._event(alert["alert_id"], "NOTIFIED" if ok else "NOTIFY_FAILED", now,
                        {"notifier": n.name, "event": event,
                         **({"status": n.status} if ok else {"error": err})})
        return results

    def raise_(self, f: dict, now: datetime) -> dict:
        """``f``: key, severity, component, rule, threshold, evidence, steps,
        optional version, session, group."""
        if f["severity"] not in SEVERITIES:
            raise ValueError(f"unknown severity {f['severity']}")
        key = clean(f["key"], 120)
        with self.ms.store.transaction():
            old = self.ms.one("SELECT * FROM monitor_alerts WHERE alert_key = ? AND status = "
                              "'OPEN'", (key,))
            if old is None:
                aid = "AL-" + hashlib.sha256(f"{key}|{now.isoformat()}".encode()).hexdigest()[:14]
                row = {"alert_id": aid, "alert_key": key, "detected_at": now, "last_seen": now,
                       "occurrences": 1, "severity": f["severity"],
                       "component": clean(f["component"], 60),
                       "grp": clean(f.get("group") or f["component"], 60),
                       "rule": clean(f["rule"], 300), "threshold": clean(f["threshold"], 200),
                       "evidence": clean(_j(f.get("evidence", {})), 4000),
                       "version": clean(f.get("version") or "", 120) or None,
                       "session": clean(f.get("session") or "", 40) or None,
                       "steps": _j([clean(s, 300) for s in f.get("steps", [])]),
                       "status": "OPEN"}
                self.ms.store._insert("monitor_alerts", row)
                self._event(aid, "RAISED", now, {"severity": f["severity"]})
                event = "RAISED"
            else:
                aid = old["alert_id"]
                escalated = SEVERITIES.index(f["severity"]) > SEVERITIES.index(old["severity"])
                self.ms.store.conn.execute(
                    "UPDATE monitor_alerts SET last_seen = ?, occurrences = occurrences + 1, "
                    "severity = ?, evidence = ? WHERE alert_id = ?",
                    (now.isoformat(), f["severity"] if escalated else old["severity"],
                     clean(_j(f.get("evidence", {})), 4000), aid))
                last = datetime.fromisoformat(old["last_notified_at"]) \
                    if old["last_notified_at"] else None
                event = "ESCALATED" if escalated else (
                    "REPEATED" if last is None or now - last >= self.cooldown else None)
                self._event(aid, "ESCALATED" if escalated else "SEEN_AGAIN", now,
                            {"severity": f["severity"]})
            alert = self.ms.one("SELECT * FROM monitor_alerts WHERE alert_id = ?", (aid,))
        if event:
            alert["notify"] = self._notify(alert, event, now)
            with self.ms.store.transaction():
                self.ms.store.conn.execute("UPDATE monitor_alerts SET last_notified_at = ? "
                                           "WHERE alert_id = ?", (now.isoformat(), aid))
        return alert

    def resolve(self, key: str, now: datetime, reason: str = "condition cleared") -> bool:
        with self.ms.store.transaction():
            old = self.ms.one("SELECT * FROM monitor_alerts WHERE alert_key = ? AND status = "
                              "'OPEN'", (key,))
            if old is None:
                return False
            self.ms.store.conn.execute("UPDATE monitor_alerts SET status = 'RESOLVED', "
                                       "resolved_at = ? WHERE alert_id = ?",
                                       (now.isoformat(), old["alert_id"]))
            self._event(old["alert_id"], "RESOLVED", now, {"reason": clean(reason)})
            old.update(status="RESOLVED")
        self._notify(old, "RECOVERED", now)  # recovery notification
        return True

    def acknowledge(self, alert_id: str, actor: str, note: str, now: datetime) -> None:
        if not actor.strip():
            raise ValueError("acknowledgement needs the person's name")
        with self.ms.store.transaction():
            if self.ms.one("SELECT 1 FROM monitor_alerts WHERE alert_id = ?", (alert_id,)) is None:
                raise KeyError(f"no alert {alert_id}")
            self._event(alert_id, "ACKNOWLEDGED", now, {"by": clean(actor, 60),
                                                        "note": clean(note)})

    def sync(self, findings: list[dict], monitored_prefixes: set[str], now: datetime) -> dict:
        """Raise every finding; resolve open alerts (within the monitored scopes)
        whose condition is no longer present."""
        raised = [self.raise_(f, now) for f in findings]
        keys = {clean(f["key"], 120) for f in findings}
        resolved = []
        for a in self.ms.all("SELECT alert_key FROM monitor_alerts WHERE status = 'OPEN'"):
            k = a["alert_key"]
            if k not in keys and k.split(".")[0] in monitored_prefixes:
                if self.resolve(k, now):
                    resolved.append(k)
        return {"raised": [a["alert_id"] for a in raised], "resolved": resolved}

    def open(self) -> list[dict]:
        rows = self.ms.all("SELECT * FROM monitor_alerts WHERE status = 'OPEN' ORDER BY CASE "
                           "severity WHEN 'CRITICAL' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END, "
                           "last_seen DESC")
        for r in rows:
            r["evidence"], r["steps"] = json.loads(r["evidence"]), json.loads(r["steps"])
        return rows

    def history(self, alert_id: str) -> list[dict]:
        return self.ms.all("SELECT * FROM monitor_alert_events WHERE alert_id = ? ORDER BY id",
                           (alert_id,))
