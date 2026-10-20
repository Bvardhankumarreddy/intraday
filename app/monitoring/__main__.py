"""Monitoring CLI (Phase 15). Observes and alerts; never changes parameters,
never promotes, never touches a broker.

    python -m app.monitoring run [--full]          # one evaluation (full = + drift)
    python -m app.monitoring status                # state, paused arms, open alerts
    python -m app.monitoring alerts [--all]
    python -m app.monitoring history --alert AL-..  # alert audit trail
    python -m app.monitoring ack --alert AL-.. --by NAME --note ".."
    python -m app.monitoring override --to NORMAL --minutes 60 --by NAME --reason ".." \\
                                      --confirm "OVERRIDE <current state>"
    python -m app.monitoring clear-review --by NAME --reason ".." \\
                                          --confirm "CLEAR MANUAL_REVIEW_REQUIRED"
    python -m app.monitoring states                # state transition log
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from app.config import load_settings
from app.utils.market_calendar import MarketCalendar


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.monitoring")
    ap.add_argument("command", choices=["run", "status", "alerts", "history", "ack", "override",
                                        "clear-review", "states"])
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--alert")
    ap.add_argument("--by", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--to", default="NORMAL")
    ap.add_argument("--minutes", type=int, default=60)
    ap.add_argument("--confirm")
    args = ap.parse_args(argv)

    from app.monitoring import state
    from app.monitoring.alerts import AlertCenter, MonitorStore
    from app.monitoring.monitor import run_monitor

    settings = load_settings()
    now = datetime.now(MarketCalendar(settings.market).tz)
    print("MONITORING (observes and alerts only; paper trading; never changes the strategy).")
    if args.command == "run":
        snap = run_monitor(settings, now, "full" if args.full else "step")
        print(f"state {snap['state']}; paused {snap['paused_arms'] or 'none'}; "
              f"{len(snap['findings'])} finding(s)")
        for f in snap["findings"]:
            print(f"  {f['severity']:8} {f['key']}: {f['rule']}")
        return 0
    ms = MonitorStore(settings.monitoring.db_path)
    ac = AlertCenter(ms, settings)
    c = args.command
    if c == "status":
        cur = state.current(ms)
        print(json.dumps({k: cur.get(k) for k in ("state", "since", "reason", "paused_arms",
                                                  "expires_at")}, indent=2, default=str))
        for a in ac.open():
            print(f"  {a['severity']:8} {a['alert_id']} {a['alert_key']} x{a['occurrences']}")
    elif c == "alerts":
        rows = ms.all("SELECT * FROM monitor_alerts ORDER BY detected_at DESC LIMIT 200") \
            if args.all else ac.open()
        for a in rows:
            print(f"{a['alert_id']} {a['status']:8} {a['severity']:8} {a['component']:12} "
                  f"{a['rule']} (threshold {a['threshold']})")
    elif c == "history":
        for e in ac.history(args.alert):
            print(f"{e['ts']} {e['kind']:13} {e['detail']}")
    elif c == "ack":
        ac.acknowledge(args.alert, args.by, args.note, now)
        print("acknowledged (recorded)")
    elif c == "override":
        print(json.dumps(state.override(ms, settings, args.to, args.by, args.reason,
                                        args.confirm or "", args.minutes, now), default=str))
    elif c == "clear-review":
        print(json.dumps(state.clear_review(ms, args.by, args.reason, args.confirm or "", now),
                         default=str))
    elif c == "states":
        for e in ms.all("SELECT * FROM monitor_state_events ORDER BY id"):
            print(f"{e['ts']} {e['from_state']} -> {e['to_state']} by {e['actor']}: "
                  f"{e['reason']}")
    return 0


def main() -> int:
    try:
        return run()
    except (ValueError, KeyError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
