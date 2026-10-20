"""Operations CLI (Phase 8). Read-only towards the broker; never places orders.

    python -m app.ops health [--probe] [--json] [--db PATH]
        Health of config, database, disk, permissions, reconciliation, session,
        market data, reports, alerts and risk lock. Exit 0 HEALTHY, 1 DEGRADED,
        2 UNHEALTHY. --probe makes ONE read-only market-data call.
    python -m app.ops preflight
        Pre-session check = health --probe.
    python -m app.ops reconcile [--db PATH]
        Portfolio-wide reconciliation; writes a new timestamped report. Exit 1
        on CRITICAL/ERROR findings. Never changes a record.
    python -m app.ops backup [--db PATH]
    python -m app.ops verify-backup PATH
    python -m app.ops restore PATH --to NEW_PATH
        Restore to a NEW path only (never over the active database).
    python -m app.ops alerts [--resolve KEY]
    python -m app.ops prune [--apply]
        Old logs and surplus backups only; dry run unless --apply.
    python -m app.ops harden
        Owner-only permissions on .env, data folders, databases, logs, reports.
    python -m app.ops gate [--skip-tests] [--json]
        Release and regression gate.
"""

from __future__ import annotations

import argparse
import json
import stat
import sys
from datetime import datetime
from pathlib import Path

from app.config import PROJECT_ROOT, load_settings
from app.utils.logging import configure_logging
from app.utils.market_calendar import MarketCalendar


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str, sort_keys=True))


def harden(settings) -> list[str]:
    changed = []
    dirs = [PROJECT_ROOT / "data", Path(settings.paper.db_path).parent, settings.ops.log_dir,
            settings.ops.backups_dir, settings.evaluation.output_dir, settings.backtest.output_dir,
            settings.robustness.output_dir, settings.ops.reconciliation_dir]
    for d in dirs:
        d = Path(d)
        if not d.exists():
            continue
        for p in [d, *d.rglob("*")]:
            mode = stat.S_IMODE(p.stat().st_mode)
            # Strip group/other access only; read-only files stay read-only.
            want = 0o700 if p.is_dir() else (mode & 0o700) | 0o400
            if mode != want:
                p.chmod(want)
                changed.append(f"{p} {oct(mode)} -> {oct(want)}")
    env = PROJECT_ROOT / ".env"
    if env.exists() and stat.S_IMODE(env.stat().st_mode) != 0o600:
        env.chmod(0o600)
        changed.append(f"{env} -> 0o600")
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.ops", description="Operations (Phase 8)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("health", "preflight"):
        h = sub.add_parser(name)
        h.add_argument("--db", type=Path)
        h.add_argument("--json", action="store_true")
        if name == "health":
            h.add_argument("--probe", action="store_true")
    r = sub.add_parser("reconcile")
    r.add_argument("--db", type=Path)
    b = sub.add_parser("backup")
    b.add_argument("--db", type=Path)
    v = sub.add_parser("verify-backup")
    v.add_argument("path", type=Path)
    rs = sub.add_parser("restore")
    rs.add_argument("path", type=Path)
    rs.add_argument("--to", type=Path, required=True)
    al = sub.add_parser("alerts")
    al.add_argument("--resolve")
    pr = sub.add_parser("prune")
    pr.add_argument("--apply", action="store_true")
    sub.add_parser("harden")
    g = sub.add_parser("gate")
    g.add_argument("--skip-tests", action="store_true")
    g.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 - invalid config is a clean, explicit failure
        print(f"CONFIG INVALID: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level, settings)
    now = datetime.now(MarketCalendar(settings.market).tz)
    db = Path(getattr(args, "db", None) or settings.paper.db_path)

    if args.cmd in ("health", "preflight"):
        from app.ops.health import health_report

        provider = None
        if args.cmd == "preflight" or args.probe:
            from app.data.factory import build_provider
            from app.data.models import DataError

            try:
                provider = build_provider(settings, MarketCalendar(settings.market))
            except DataError as exc:
                provider = _FailedProvider(str(exc))
        rep = health_report(settings, now, db, provider)
        if args.json:
            _print(rep)
        else:
            print(f"STATUS: {rep['status']}  ({rep['checked_at']}, {rep['database']})")
            for c in rep["checks"]:
                print(f"  {c['status']:5} {c['check']:15} {c['detail']}")
        return {"HEALTHY": 0, "DEGRADED": 1}.get(rep["status"], 2)

    if args.cmd == "reconcile":
        from app.costs.schedule import load_fee_schedule
        from app.ops.reconciliation import full_reconciliation, write_report
        from app.paper.store import PaperStore

        store = PaperStore(db)
        try:
            rec = full_reconciliation(store, settings, load_fee_schedule(settings), now.date(),
                                      now)
        finally:
            store.close()
        path = write_report(rec, settings.ops.reconciliation_dir, now)
        print(f"Reconciliation {rec['status']} {rec['counts']} -> {path}")
        for f in rec["findings"][:30]:
            print(f"  {f['severity']:8} {f['code']} ({f['ref']}): {f['message']}")
        return 0 if rec["status"] in ("OK", "WARN") else 1

    if args.cmd == "backup":
        from app.ops.backup import create_backup

        _print(create_backup(db, settings.ops.backups_dir, now))
        return 0
    if args.cmd == "verify-backup":
        from app.ops.backup import verify_backup

        rep = verify_backup(args.path, settings, now.date(), active_db=settings.paper.db_path)
        _print(rep)
        return 0 if rep["status"] == "PASS" else 1
    if args.cmd == "restore":
        from app.ops.backup import restore_backup

        _print(restore_backup(args.path, args.to, settings.paper.db_path, settings))
        print("Restored to a NEW file. The application still uses "
              f"{settings.paper.db_path}; switch over manually (docs/runbooks.md).")
        return 0
    if args.cmd == "alerts":
        from app.ops.alerts import AlertManager
        from app.paper.store import PaperStore

        store = PaperStore(db)
        try:
            mgr = AlertManager(store, settings)
            if args.resolve:
                print("resolved" if mgr.resolve(args.resolve, now) else "no open alert by that key")
            _print(mgr.open_alerts())
        finally:
            store.close()
        return 0
    if args.cmd == "prune":
        from app.ops.retention import prune

        _print(prune(settings, now, apply=args.apply))
        return 0
    if args.cmd == "harden":
        changed = harden(settings)
        print(f"{len(changed)} permission change(s)")
        for c in changed:
            print("  " + c)
        return 0
    if args.cmd == "gate":
        from app.ops.gate import format_gate, run_gate

        rep = run_gate(skip_tests=args.skip_tests)
        print(json.dumps(rep, indent=2) if args.json else format_gate(rep))
        return 0 if rep["verdict"].startswith("RELEASABLE") else 1
    return 0


class _FailedProvider:
    """Stands in for a provider that could not be built, so the probe check
    reports the failure instead of crashing the health command."""

    def __init__(self, error: str):
        self.error = error

    def list_instruments(self):
        raise RuntimeError(self.error)


def run(argv: list[str] | None = None) -> int:
    from app.ops.backup import BackupError
    from app.paper.store import StoreError
    from app.utils.fs import UnsafePathError

    try:
        return main(argv)
    except (BackupError, StoreError, UnsafePathError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
