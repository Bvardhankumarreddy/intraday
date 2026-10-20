"""WhatsApp notifications CLI.

    python -m app.notify check          # configuration status (no message sent)
    python -m app.notify test           # send ONE test message through the template
    python -m app.notify preview        # what today's events would send (nothing sent)
    python -m app.notify daily-summary  # send today's summary (post-session job)
    python -m app.notify log            # recent notifications and their status
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from app.config import load_settings
from app.utils.market_calendar import MarketCalendar


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.notify")
    ap.add_argument("command", choices=["check", "test", "preview", "daily-summary", "log"])
    args = ap.parse_args(argv)

    from app.monitoring.alerts import MonitorStore
    from app.notify import events
    from app.notify.whatsapp import Notifications, WhatsAppSender
    from app.paper.store import PaperStore
    from pathlib import Path

    from app.utils.logging import configure_logging

    s = load_settings()
    configure_logging(s.log_level, s)  # also registers every configured secret for redaction
    now = datetime.now(MarketCalendar(s.market).tz)
    sender = WhatsAppSender(s)
    missing = sender.configured()
    if args.command == "check":
        print("WhatsApp:", "READY" if not missing else "NOT READY - " + "; ".join(missing))
        print(f"template {s.whatsapp.template_name} ({s.whatsapp.template_language}), "
              f"api {s.whatsapp.api_version}, recipients "
              f"{['...' + r[-4:] for r in s.whatsapp.recipients]}")
        return 0 if not missing else 1
    ms = MonitorStore(s.monitoring.db_path)
    n = Notifications(s, ms, sender)
    if args.command == "log":
        for r in ms.all("SELECT created_at, kind, status, text FROM notify_log ORDER BY "
                        "created_at DESC LIMIT 30"):
            print(f"{r['created_at'][:19]} {r['kind']:7} {r['status']:9} {r['text'][:110]}")
        return 0
    if args.command == "test":
        res = n.deliver(f"test:{now.isoformat()}", "test",
                        "Test message from the NSE paper-trading system. If you can read this, "
                        "WhatsApp alerts work", now)
        print(res["status"], res.get("detail"))
        return 0 if res["status"] == "ACCEPTED" else 1
    stores = {k: PaperStore(p) for k, p in (("A", s.paper.db_path),
                                            ("B", s.integrated.ai_db_path)) if Path(p).exists()}
    day = now.date().isoformat()
    if args.command == "preview":
        evs = events.signal_events(stores.get("B"), day) + events.trade_events(
            stores, day, True, True)
        for key, text in evs:
            sent = ms.one("SELECT status FROM notify_log WHERE key = ?", (key,))
            print(f"[{sent['status'] if sent else 'not sent'}] {text}")
        print(events.daily_summary(stores, stores.get("B"), now.date()))
        return 0
    if not s.whatsapp.send_daily_summary:
        print("daily summary disabled")
        return 0
    res = n.deliver(f"summary:{day}", "summary",
                    events.daily_summary(stores, stores.get("B"), now.date()), now)
    print(res["status"], res.get("detail") if res["status"] != "ACCEPTED" else "")
    return 0


def main() -> int:
    try:
        return run()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
