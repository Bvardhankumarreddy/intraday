"""Paper trading CLI (simulated orders only - never real orders).

    python -m app.paper replay --date 2025-03-12 [--data DIR] [--db PATH]
        Replay one session from mock data (default) or historical CSVs
        (--data) on a simulated clock. Uses paper.replay_db_path by default,
        never the live paper portfolio.
    python -m app.paper run [--db PATH]
        Live session with the configured provider (data.provider). Blocks
        until the session ends. Needs an authorized live data source.
    python -m app.paper status [--db PATH] [--date D]
    python -m app.paper report --date D [--db PATH] [--export]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from app.config import load_settings
from app.data.mock_provider import MockMarketDataProvider
from app.paper import views
from app.paper.engine import PaperEngine
from app.paper.report import LABEL, export_report
from app.paper.scheduler import replay_session
from app.paper.store import PaperStore
from app.utils.logging import configure_logging
from app.utils.market_calendar import MarketCalendar


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="NSE paper trading (simulated)")
    ap.add_argument("command", choices=["replay", "run", "status", "report"])
    ap.add_argument("--date", type=date.fromisoformat)
    ap.add_argument("--data", type=Path, help="historical CSV directory for replay")
    ap.add_argument("--db", type=Path)
    ap.add_argument("--config")
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--regenerate", action="store_true",
                    help="rebuild the day's report from persisted records")
    args = ap.parse_args(argv)

    settings = load_settings(args.config)
    configure_logging(settings.log_level)
    calendar = MarketCalendar(settings.market)
    default_db = settings.paper.replay_db_path if args.command == "replay" \
        else settings.paper.db_path
    store = PaperStore(args.db or default_db)
    print(LABEL)

    if args.command == "replay":
        if args.date is None:
            ap.error("replay needs --date")
        if args.data:
            from app.backtesting.dataset import load_csv_dataset

            provider = load_csv_dataset(args.data, settings, calendar).provider(calendar)
            hook = None
        else:
            provider = MockMarketDataProvider(calendar, seed=settings.data.mock_seed)
            hook = lambda t: setattr(provider, "as_of", t)  # noqa: E731
        engine = PaperEngine(provider, settings, calendar, store)
        problems = engine.preflight(args.date)
        if problems:
            print("Cannot start:", *problems, sep="\n  ")
            return 1
        replay_session(engine, args.date, before_step=hook)
        _print_status(store, args.date)
        return 0
    if args.command == "run":
        from app.paper.scheduler import provider_or_fail, run_scheduled

        provider = provider_or_fail(settings, calendar, store)
        if provider is None:
            return 1
        hook = (lambda t: setattr(provider, "as_of", t)) \
            if isinstance(provider, MockMarketDataProvider) else None
        return run_scheduled(PaperEngine(provider, settings, calendar, store), hook)
    day = args.date or _latest(store)
    if day is None:
        print("No paper sessions recorded.")
        return 0
    if args.command == "status":
        _print_status(store, day)
        return 0
    report = store.report(day)
    if report is None and not args.regenerate:
        print(f"No report for {day}. Rebuild it with --regenerate.")
        return 1
    if args.regenerate:
        from datetime import datetime

        from app.paper.report import regenerate_report

        regenerate_report(store, day, settings, datetime.now(calendar.tz))
        print(f"Report for {day} regenerated (earlier versions kept in report_history).")
        report = store.report(day)
    if args.export:
        print("Exported to", export_report(store, day))
    else:
        print(json.dumps({k: report[k] for k in ("trading_date", "totals", "statistics")},
                         indent=2))
    return 0


def _latest(store: PaperStore) -> date | None:
    s = views.sessions(store)
    return None if s.empty else date.fromisoformat(s["trading_date"].iloc[0])


def _print_status(store: PaperStore, day: date) -> None:
    s = views.portfolio_summary(store, day)
    session = store.session(day)
    print(f"Session {day}: {session['state']}  data {session['data_source']} "
          f"[{session['data_mode'].upper()}]")
    for k in ("cash", "reserved", "equity", "realized_pnl_today", "unrealized_pnl", "daily_pnl"):
        print(f"  {k:<20} {s[k] if s[k] is not None else 'unknown'}")
    print(f"  signals {len(store.signals(day))}, orders {len(store.orders(day))}, "
          f"positions {len(store.positions(day))}")


if __name__ == "__main__":
    sys.exit(main())
