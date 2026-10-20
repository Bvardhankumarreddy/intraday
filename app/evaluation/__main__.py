"""Live paper-trading evaluation CLI (Phase 6). SIMULATED orders only.

    python -m app.evaluation start --name "ORB 20-session" [--from 2026-10-21]
        Create the evaluation and freeze the current configuration.
    python -m app.evaluation run
        Run today's session with the configured live provider (blocks until the
        session ends; Ctrl+C stops it - restart to resume, state is persisted).
    python -m app.evaluation status
    python -m app.evaluation sync
        Mark past trading days the application missed as INCOMPLETE.
    python -m app.evaluation report --date 2026-10-21
    python -m app.evaluation cumulative
    python -m app.evaluation reconcile --date 2026-10-21
    python -m app.evaluation replay --date 2026-10-19 [--data DIR]
        Replay into the separate replay database (never primary results).
    python -m app.evaluation finish
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from app.config import load_settings
from app.data.mock_provider import MockMarketDataProvider
from app.evaluation.metrics import as_json, cumulative_metrics
from app.evaluation.reconcile import reconcile
from app.evaluation.recorder import EvaluationRecorder
from app.evaluation.report import write_cumulative, write_daily
from app.paper.engine import PaperEngine
from app.paper.report import LABEL
from app.paper.scheduler import replay_session
from app.paper.store import PaperStore
from app.risk.engine import RiskCostEngine
from app.utils.logging import configure_logging
from app.utils.market_calendar import MarketCalendar


# Distinct exit code so automation stops instead of retrying a run that
# cannot succeed until the user starts an evaluation.
EXIT_NO_EVALUATION = 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Live paper-trading evaluation (simulated)")
    ap.add_argument("command", choices=["start", "run", "status", "sync", "report",
                                        "cumulative", "reconcile", "replay", "finish"])
    ap.add_argument("--name")
    ap.add_argument("--from", dest="start", type=date.fromisoformat)
    ap.add_argument("--date", type=date.fromisoformat)
    ap.add_argument("--data", type=Path)
    ap.add_argument("--db", type=Path)
    ap.add_argument("--config")
    args = ap.parse_args(argv)

    settings = load_settings(args.config)
    configure_logging(settings.log_level)
    calendar = MarketCalendar(settings.market)
    now = datetime.now(calendar.tz)
    replay = args.command == "replay"
    store = PaperStore(args.db or (settings.paper.replay_db_path if replay
                                   else settings.paper.db_path))
    recorder = EvaluationRecorder(store, settings, calendar)
    print(LABEL)

    if args.command == "start":
        if not args.name:
            ap.error("start needs --name")
        start = args.start or now.date()
        eid = recorder.create_evaluation(args.name, start, now)
        print(f"Evaluation {eid} created from {start}; configuration {settings.version()} "
              "is frozen. Changing settings later excludes those sessions from the primary "
              "results.")
        return 0

    if recorder.active() is None and args.command not in ("status",):
        if replay:
            recorder.create_evaluation(args.name or "replay", args.date or now.date(), now)
        else:
            print("No active evaluation. Start one: python -m app.evaluation start --name ...")
            return EXIT_NO_EVALUATION
    ev = recorder.active()

    if args.command == "run":
        from app.paper.scheduler import provider_or_fail, run_scheduled

        provider = provider_or_fail(settings, calendar, store, recorder)
        if provider is None:
            return 1
        hook = (lambda t: setattr(provider, "as_of", t)) \
            if isinstance(provider, MockMarketDataProvider) else None
        print(f"Data: {provider.name} [{provider.mode.value.upper()}]"
              + ("" if provider.mode.value == "live" else
                 " - NOT live: this session will not count towards the primary evaluation"))
        engine = PaperEngine(provider, settings, calendar, store, recorder=recorder)
        return run_scheduled(engine, hook, recorder)
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
        engine = PaperEngine(provider, settings, calendar, store, recorder=recorder)
        problems = engine.preflight(args.date)
        if problems:
            print("Cannot start:", *problems, sep="\n  ")
            return 1
        replay_session(engine, args.date, before_step=hook)
        print("Daily report:", write_daily(store, args.date, settings, ev["evaluation_id"]))
        return 0
    if args.command == "sync":
        missed = recorder.sync_schedule(now.date(), now)
        print(f"Missed sessions marked INCOMPLETE: {[d.isoformat() for d in missed] or 'none'}")
        return 0
    if args.command == "status":
        if ev is None:
            print("No active evaluation.")
            return 0
        m = cumulative_metrics(store, ev["evaluation_id"], settings)
        print(f"Evaluation {ev['evaluation_id']} '{ev['name']}' from {ev['start_date']}, "
              f"config {ev['frozen_config_version']}"
              + ("" if ev["frozen_config_version"] == settings.version() else
                 f" (CURRENT CONFIG {settings.version()} DIFFERS - sessions won't be primary)"))
        print(json.dumps({"sessions": m["sessions"], "funnel": m["funnel"],
                          "net_pnl": m["trades"]["net_pnl"], "evidence": m["evidence"]},
                         indent=2))
        return 0
    if args.command == "report":
        day = args.date or now.date()
        print("Daily report:", write_daily(store, day, settings, ev["evaluation_id"]))
        return 0
    if args.command == "cumulative":
        path = write_cumulative(store, settings, ev["evaluation_id"])
        m = cumulative_metrics(store, ev["evaluation_id"], settings)
        print(as_json({"sessions": m["sessions"], "trades": m["trades"],
                       "evidence": m["evidence"]}))
        print("Cumulative report and CSVs:", path.parent)
        return 0
    if args.command == "reconcile":
        day = args.date or now.date()
        issues = reconcile(store, day, settings, RiskCostEngine(settings).schedule)
        recorder.record_issues(day, now, issues)
        print(f"{len(issues)} issue(s)" + "".join(f"\n  {i.code} ({i.ref}): {i.message}"
                                                 for i in issues))
        return 1 if issues else 0
    if args.command == "finish":
        recorder.finish_evaluation(now)
        print(f"Evaluation {ev['evaluation_id']} finished. It never switches to real trading.")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
