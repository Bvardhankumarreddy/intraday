"""Run a backtest on historical CSV data.

Usage:
    python -m app.backtesting --data data/historical --start 2025-01-01 --end 2025-06-30
        [--config PATH] [--out DIR]
        [--periods dev:2025-01-01:2025-02-28,val:2025-03-01:2025-04-30,test:2025-05-01:2025-06-30]
        [--sensitivity] [--walk-forward 40:20]

SIMULATED results only. Never places orders.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from app.backtesting.analysis import Period, run_periods, sensitivity, walk_forward
from app.backtesting.dataset import load_csv_dataset
from app.backtesting.engine import run_backtest
from app.backtesting.metrics import compute_metrics
from app.backtesting.report import DISCLAIMER, _json_default, write_report
from app.config import load_settings
from app.utils.logging import configure_logging
from app.utils.market_calendar import MarketCalendar


def parse_periods(text: str) -> list[Period]:
    out = []
    for part in text.split(","):
        name, start, end = part.split(":")
        out.append(Period(name, date.fromisoformat(start), date.fromisoformat(end)))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="NSE ORB backtest (simulated)")
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--start", required=True, type=date.fromisoformat)
    ap.add_argument("--end", required=True, type=date.fromisoformat)
    ap.add_argument("--config")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--periods", type=parse_periods)
    ap.add_argument("--sensitivity", action="store_true")
    ap.add_argument("--walk-forward", help="TRAIN:TEST session counts, e.g. 40:20")
    args = ap.parse_args(argv)

    settings = load_settings(args.config)
    configure_logging(settings.log_level)
    calendar = MarketCalendar(settings.market)
    dataset = load_csv_dataset(args.data, settings, calendar)
    result = run_backtest(dataset, settings, calendar, args.start, args.end)
    metrics = compute_metrics(result, settings.backtest.min_trades_for_evidence)

    oos = None
    if args.periods:
        oos = {name: m for name, (_, m) in
               run_periods(dataset, settings, calendar, args.periods).items()}
    sens = sensitivity(dataset, settings, calendar, args.start, args.end) \
        if args.sensitivity else None
    wf = None
    if args.walk_forward:
        train, test = (int(x) for x in args.walk_forward.split(":"))
        wf = walk_forward(dataset, settings, calendar, train, test)

    path = write_report(result, metrics, dataset, settings, args.out, oos, sens, wf)
    print(DISCLAIMER)
    print(json.dumps({k: metrics[k] for k in (
        "trades", "total_net_pnl", "net_return_pct", "win_rate_pct", "profit_factor",
        "max_drawdown", "rejected_signals", "evidence")}, indent=2, default=_json_default))
    print(f"Report written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
