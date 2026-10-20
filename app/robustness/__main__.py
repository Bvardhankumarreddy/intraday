"""Phase 7: strategy robustness validation (SIMULATED research; never places orders).

Usage:
    python -m app.robustness freeze --name orb-baseline-v1 [--config PATH]
    python -m app.robustness verify --baseline config/baselines/orb-baseline-v1.json
    python -m app.robustness fetch [--symbols config/universe.csv] [--days 90]
                                   [--out data/historical] [--no-benchmark]
    python -m app.robustness plan --baseline ... [--data data/historical]
    python -m app.robustness run  --baseline ... [--data data/historical]
                                  [--grid full|oat|none] [--workers N]
                                  [--train N --validation N --test N] [--allow-code-drift]

`run` always writes a report. Without usable data it is a PENDING report that
lists exactly what is missing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from app.config import load_settings
from app.robustness.baseline import BaselineError, freeze, load_frozen
from app.robustness.runner import CodeDriftError, RunOptions, evaluate
from app.utils.logging import configure_logging
from app.utils.market_calendar import MarketCalendar


def _default_baseline(settings) -> Path:
    files = sorted(Path(settings.robustness.baselines_dir).glob("*.json"))
    if len(files) == 1:
        return files[0]
    raise SystemExit("pass --baseline (found "
                     f"{len(files)} frozen baselines in {settings.robustness.baselines_dir})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.robustness",
                                 description="Phase 7 robustness validation (simulated)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze", help="freeze the current configuration as a baseline")
    f.add_argument("--name", required=True)
    f.add_argument("--config")
    v = sub.add_parser("verify", help="verify a frozen baseline")
    v.add_argument("--baseline", type=Path)
    g = sub.add_parser("fetch", help="download Groww historical candles (read-only)")
    g.add_argument("--symbols", type=Path, help="CSV with symbol,name (default: universe file)")
    g.add_argument("--days", type=int, default=90)
    g.add_argument("--end", type=date.fromisoformat)
    g.add_argument("--out", type=Path)
    g.add_argument("--no-benchmark", action="store_true")
    for name in ("plan", "run"):
        p = sub.add_parser(name)
        p.add_argument("--baseline", type=Path)
        p.add_argument("--data", type=Path)
        p.add_argument("--train", type=int)
        p.add_argument("--validation", type=int)
        p.add_argument("--test", type=int)
        p.add_argument("--allow-code-drift", action="store_true")
        if name == "run":
            p.add_argument("--grid", choices=["full", "oat", "none"], default="full")
            p.add_argument("--workers", type=int)
            p.add_argument("--out", type=Path)
    args = ap.parse_args(argv)

    settings = load_settings(getattr(args, "config", None))
    configure_logging(settings.log_level)

    if args.cmd == "freeze":
        tz = MarketCalendar(settings.market).tz
        path = freeze(args.name, settings, settings.robustness.protocol_file,
                      settings.robustness.baselines_dir, datetime.now(tz))
        body = json.loads(path.read_text())
        print(f"Frozen baseline written (read-only): {path}")
        print(f"  strategy fingerprint {body['strategy_fingerprint']}")
        print(f"  code hash            {body['code_hash']}")
        print(f"  protocol             {body['protocol_sha256'][:16]}...")
        return 0

    if args.cmd == "verify":
        fb = load_frozen(args.baseline or _default_baseline(settings))
        print(f"OK: {fb.path} verified (fingerprint {fb.fingerprint[:16]}...).")
        if fb.code_drift:
            print("WARNING: decision-path code changed since freezing - a new strategy version.")
            return 3
        return 0

    if args.cmd == "fetch":
        from app.data.factory import build_groww_provider, load_universe
        from app.robustness.fetch import fetch_history
        from app.robustness.protocol import load_protocol

        calendar = MarketCalendar(settings.market)
        symbols_file = args.symbols or settings.data.universe_file
        instruments = load_universe(Path(symbols_file))
        protocol, _, _ = load_protocol(settings.robustness.protocol_file)
        bench = protocol.regimes.benchmark_symbol
        if bench and not args.no_benchmark and bench not in {i.symbol for i in instruments}:
            from app.data.models import Instrument
            instruments.append(Instrument(bench, f"{bench} (benchmark, not traded)"))
        now = datetime.now(calendar.tz)
        end = args.end or calendar.previous_trading_day(now.date() + timedelta(days=1)
                                                         if now.time() >= settings.market.session_close
                                                         else now.date())
        start = end - timedelta(days=args.days)
        provider = build_groww_provider(settings, calendar)
        out = args.out or settings.data.historical_dir
        print(f"Fetching {len(instruments)} symbols {start}..{end} into {out} (read-only data)")
        manifest = fetch_history(provider, instruments, start, end, out, calendar,
                                 settings.screening.liquidity_lookback_sessions,
                                 Path(symbols_file), now)
        print(f"Done. Failed: {len(manifest['failed_symbols'])}. Manifest: {Path(out) / 'manifest.json'}")
        return 0

    baseline = load_frozen(args.baseline or _default_baseline(settings))
    opts = RunOptions(data_dir=args.data or settings.data.historical_dir,
                      train=args.train, validation=args.validation, test=args.test,
                      allow_code_drift=args.allow_code_drift)
    if args.cmd == "plan":
        from app.robustness.runner import _missing_data, load_data
        from app.robustness.walk_forward import make_plan

        missing = _missing_data(opts.data_dir)
        if missing:
            print("PENDING_DATA:", "; ".join(missing))
            return 2
        hs = baseline.historical_settings()
        cal = MarketCalendar(hs.market)
        dataset, *_ = load_data(opts, hs, cal, baseline.protocol.regimes.benchmark_symbol)
        plan = make_plan(dataset, hs, cal, baseline.protocol.walk_forward,
                         opts.train, opts.validation, opts.test)
        print(json.dumps(plan.summary(), indent=2, default=str))
        return 0 if plan.status == "OK" else 2

    opts.grid = args.grid
    opts.workers = args.workers or settings.robustness.workers or max(1, (os.cpu_count() or 2) - 1)
    from app.robustness.report import DISCLAIMER, write_report

    try:
        results = evaluate(baseline, opts)
    except CodeDriftError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    path = write_report(results, baseline.path, args.out or settings.robustness.output_dir)
    print(DISCLAIMER)
    print(f"Verdict: {results['assessment']['verdict']} - {results['assessment']['statement']}")
    extra = "report.html, summary.json, CSVs" if results["status"] == "OK" else \
        "report.html, summary.json; no result tables because data is missing"
    print(f"Report: {path / 'report.md'} (also {extra})")
    return 0 if results["status"] == "OK" else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaselineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
