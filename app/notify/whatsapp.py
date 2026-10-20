"""Meta WhatsApp Cloud API sender and the notification ledger.

Sending:
- ``POST {base_url}/{api_version}/{phone_number_id}/messages`` with a Bearer
  token (secret ``NSE_WHATSAPP_TOKEN``). A business-initiated message must use
  an APPROVED template, so every message is the template
  ``whatsapp.template_name`` with ONE body variable carrying the alert text.
  Example body to register in Meta (category Utility):
  ``NSE paper-trading alert: {{1}}. Research only, not investment advice.``
- Meta rejects variables containing newlines, tabs or more than 4 consecutive
  spaces, or longer than 1024 characters. ``clean_param`` enforces that and
  redacts secrets.
- Bounded retries for timeouts, connection errors, HTTP 429 and 5xx only.
  Other errors (bad token, unknown template, invalid number) are recorded and
  not retried.
- "ACCEPTED" means Meta accepted the request and returned a message ID. It
  does NOT confirm delivery to the phone; delivery receipts need a webhook,
  which is not configured.
- ``send`` never raises. A notification can never break the trading loop.

Ledger (table ``notify_log`` in the monitoring database): one row per
notification key (UNIQUE), so the same event is never sent twice, even after a
restart. A per-day cap (``max_messages_per_day``) guards cost and spam.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime

from app.utils.logging import get_logger, redact_text

log = get_logger("app.notify")
_WS = re.compile(r"[\r\n\t]+")
_SPACES = re.compile(r" {4,}")

LEDGER = """
CREATE TABLE IF NOT EXISTS notify_log (
    key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    day TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT
);
CREATE TRIGGER IF NOT EXISTS notify_log_kept BEFORE DELETE ON notify_log
BEGIN SELECT RAISE(ABORT, 'notification log is kept'); END;
"""


def clean_param(text: str, limit: int = 1000) -> str:
    t = _WS.sub(" | ", redact_text(str(text)))
    t = _SPACES.sub("   ", t).strip()
    return t[:limit]


class WhatsAppSender:
    def __init__(self, settings, session=None, sleep=time.sleep):
        self.cfg = settings.whatsapp
        tok = settings.whatsapp_token
        self._token = tok.get_secret_value() if tok is not None else None
        self.session = session
        self.sleep = sleep

    def configured(self) -> list[str]:
        """Missing prerequisites (empty = ready)."""
        missing = []
        if not self.cfg.enabled:
            missing.append("whatsapp.enabled is false")
        if not self._token:
            missing.append("NSE_WHATSAPP_TOKEN not set")
        if not self.cfg.phone_number_id:
            missing.append("whatsapp.phone_number_id not set")
        if not self.cfg.recipients:
            missing.append("whatsapp.recipients is empty")
        return missing

    def _post(self, url: str, body: dict):
        if self.session is None:
            import requests

            self.session = requests.Session()
        return self.session.post(url, data=json.dumps(body), timeout=self.cfg.timeout_seconds,
                                 headers={"Authorization": f"Bearer {self._token}",
                                          "Content-Type": "application/json"})

    def send(self, text: str) -> dict:
        missing = self.configured()
        if missing:
            return {"status": "SKIPPED", "detail": "; ".join(missing)}
        c = self.cfg
        url = f"{c.base_url.rstrip('/')}/{c.api_version}/{c.phone_number_id}/messages"
        param = clean_param(text)
        results = []
        for to in c.recipients:
            body = {"messaging_product": "whatsapp", "to": to, "type": "template",
                    "template": {"name": c.template_name,
                                 "language": {"code": c.template_language},
                                 "components": [{"type": "body", "parameters": [
                                     {"type": "text", "text": param}]}]}}
            results.append(self._send_one(url, body, to))
        ok = all(r["status"] == "ACCEPTED" for r in results)
        return {"status": "ACCEPTED" if ok else "FAILED", "detail": results}

    def _send_one(self, url: str, body: dict, to: str) -> dict:
        masked = f"...{to[-4:]}"
        last = None
        for attempt in range(1, self.cfg.max_retries + 2):
            try:
                r = self._post(url, body)
            except Exception as exc:  # noqa: BLE001 - timeouts / connection: retried
                last = f"{type(exc).__name__}"
                if attempt <= self.cfg.max_retries:
                    self.sleep(min(8, 2 ** attempt))
                    continue
                return {"to": masked, "status": "FAILED", "error": last}
            if r.status_code == 200:
                try:
                    mid = r.json()["messages"][0]["id"]
                except Exception:  # noqa: BLE001
                    mid = None
                return {"to": masked, "status": "ACCEPTED", "message_id": mid}
            try:
                err = r.json().get("error", {})
            except Exception:  # noqa: BLE001
                err = {}
            last = redact_text(f"HTTP {r.status_code} code {err.get('code')}: "
                               f"{err.get('message', '')}"[:300])
            if (r.status_code == 429 or r.status_code >= 500) and attempt <= self.cfg.max_retries:
                self.sleep(min(8, 2 ** attempt))
                continue
            return {"to": masked, "status": "FAILED", "error": last}
        return {"to": masked, "status": "FAILED", "error": last}


class Notifications:
    """Deduplicated, capped, logged delivery. ``deliver`` never raises."""

    def __init__(self, settings, ms, sender: WhatsAppSender | None = None):
        self.settings, self.ms = settings, ms
        self.sender = sender or WhatsAppSender(settings)
        ms.store.conn.executescript(LEDGER)

    def sent_today(self, day: str) -> int:
        row = self.ms.one("SELECT COUNT(*) AS n FROM notify_log WHERE day = ? AND status = "
                          "'ACCEPTED'", (day,))
        return row["n"] if row else 0

    def deliver(self, key: str, kind: str, text: str, now: datetime) -> dict:
        try:
            if self.ms.one("SELECT status FROM notify_log WHERE key = ?", (key,)):
                return {"status": "DUPLICATE"}
            day = now.date().isoformat()
            if self.sender.configured():
                res = {"status": "SKIPPED", "detail": "; ".join(self.sender.configured())}
            elif self.sent_today(day) >= self.settings.whatsapp.max_messages_per_day:
                res = {"status": "SKIPPED", "detail": "daily message cap reached"}
            else:
                res = self.sender.send(text)
            with self.ms.store.transaction():
                self.ms.store._insert("notify_log", {
                    "key": key[:200], "kind": kind, "created_at": now, "day": day,
                    "text": clean_param(text), "status": res["status"],
                    "detail": json.dumps(res.get("detail"), default=str)[:2000]},
                    or_ignore=True)
            if res["status"] == "FAILED":
                log.warning(f"whatsapp {kind} not accepted: {clean_param(str(res['detail']), 300)}")
            return res
        except Exception as exc:  # noqa: BLE001 - notifications never break the caller
            log.error(f"whatsapp notification error ({kind}): {type(exc).__name__}")
            return {"status": "ERROR", "detail": type(exc).__name__}


class WhatsAppAlertNotifier:
    """Monitoring notifier (``app.monitoring.alerts`` protocol): sends alerts at
    or above ``whatsapp.min_alert_severity`` and their recoveries."""

    name = "whatsapp"
    status = "ACCEPTED"
    ORDER = ("INFO", "WARNING", "CRITICAL")

    def __init__(self, notifications: Notifications):
        self.n = notifications

    def send(self, payload: dict) -> None:
        cfg = self.n.settings.whatsapp
        if not cfg.send_alerts or self.ORDER.index(payload["severity"]) < \
                self.ORDER.index(cfg.min_alert_severity):
            return
        icon = {"RECOVERED": "RESOLVED", "ESCALATED": "ESCALATED"}.get(payload["event"],
                                                                     payload["severity"])
        text = (f"SYSTEM {icon}: {payload['component']} - {payload['rule']}"
                + (f" (session {payload['session']})" if payload.get("session") else "")
                + (f". Next step: {payload['steps'][0]}" if payload.get("steps") and
                   payload["event"] != "RECOVERED" else ""))
        res = self.n.deliver(f"alert:{payload['alert_id']}:{payload['event']}:{payload['ts']}",
                             "alert", text, datetime.fromisoformat(payload["ts"]))
        if res["status"] not in ("ACCEPTED", "DUPLICATE", "SKIPPED"):
            raise RuntimeError(f"whatsapp {res['status']}")
