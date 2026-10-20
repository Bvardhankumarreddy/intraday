"""Phase 7 orchestration: frozen baseline + historical data -> results.

Order of work (each step reuses the existing project modules):
 1. Load the frozen baseline (hash-verified). Refuse on code drift unless
    explicitly allowed (results are then labelled).
 2. Load the historical CSV dataset (Phase 4 validation), separate the
    benchmark symbol, apply the corporate-action policy, load the optional
    point-in-time universe, and audit data quality.
 3. Build the chronological walk-forward plan.
 4. Primary evaluation: the frozen baseline on every train / validation /
    test window, each from fresh capital (``run_backtest``, with risk-rejected
    signals tracked as diagnostics). The test windows are chained into the
    aggregated out-of-sample record.
 5. Statistics, bootstrap uncertainty and sample-size assessment (OOS).
 6. Cost scenarios on every test window; gross vs net; break-even.
 7. Parameter sensitivity (predeclared grid), never selecting anything.
 8. Regime and stock-level grouping of OOS trades.
 9. Predeclared comparison baselines on the same OOS sessions.
10. Mechanical assessment against the predeclared criteria.

If data is missing or too short, a PENDING report says exactly what is
missing. Nothing is estimated or filled in.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from app.backtesting.dataset import DatasetError, HistoricalDataset, load_csv_dataset
from app.backtesting.engine import BacktestResult
from app.config import PROJECT_ROOT, Settings
from app.costs.money import D
from app.data.models import DataError
from app.robustness import assessment, benchmarks, regimes, sensitivity, stats
from app.robustness.baseline import FrozenBaseline
from app.robustness.data_audit import (apply_corporate_action_policy, audit_dataset,
                                       corporate_action_suspects, known_limitations,
                                       load_membership)
from app.robustness.walk_forward import Plan, chain_results, make_plan
from app.utils.market_calendar import MarketCalendar

MANIFEST = "manifest.json"


class CodeDriftError(RuntimeError):
    pass


@dataclass
class RunOptions:
    data_dir: Path
    grid: str = "full"  # none | oat | full
    workers: int = 1
    train: int | None = None
    validation: int | None = None
    test: int | None = None
    allow_code_drift: bool = False


def _rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def meta(frozen: FrozenBaseline, opts: RunOptions) -> dict:
    b = frozen.body
    return {
        "baseline_name": b["name"], "baseline_file": _rel(frozen.path),
        "baseline_created_at": b["created_at"], "frozen_sha256": b["frozen_sha256"],
        "strategy_version": b["strategy_version"],
        "execution_model_version": b["execution_model_version"],
        "strategy_fingerprint": b["strategy_fingerprint"], "code_hash_frozen": b["code_hash"],
        "code_hash_current": frozen.current_code_hash, "code_drift": frozen.code_drift,
        "fee_schedule": b["fee_schedule"], "historical_adaptations": b["historical_adaptations"],
        "protocol_version": frozen.protocol.protocol_version,
        "protocol_sha256": b["protocol_sha256"], "data_dir": _rel(opts.data_dir),
        "sensitivity_grid": opts.grid,
        "window_overrides": {"train": opts.train, "validation": opts.validation,
                             "test": opts.test},
    }


def _missing_data(data_dir: Path) -> list[str]:
    d = Path(data_dir)
    if not d.exists():
        return [f"data folder {_rel(d)} does not exist"]
    out = []
    if not (d / "instruments.csv").exists():
        out.append(f"{_rel(d)}/instruments.csv is missing")
    if not list(d.glob("*_5m.csv")):
        out.append(f"no 5-minute candle files (<SYMBOL>_5m.csv) in {_rel(d)}")
    return out


def pending_results(frozen: FrozenBaseline, opts: RunOptions, missing: list[str],
                    audit: dict | None = None, plan: dict | None = None) -> dict:
    needed = [
        "5-minute OHLCV candles (<SYMBOL>_5m.csv) for a broad NSE EQ universe, not chosen by "
        "current price: `python -m app.robustness fetch` (Groww keeps ~3 months)",
        "daily candles (<SYMBOL>_1d.csv) covering the 20-session liquidity lookback before "
        "the first intraday session (fetched by the same command)",
        f"enough sessions for one fold: warm-up + train + validation + test (default rule "
        f"needs at least {frozen.protocol.walk_forward.min_test_sessions * 4} tradable "
        "sessions after warm-up)",
        "optional: universe_membership.csv (point-in-time constituents) and the benchmark "
        f"symbol {frozen.protocol.regimes.benchmark_symbol} in the same folder",
    ]
    return {"status": "PENDING_DATA", "meta": meta(frozen, opts), "missing": missing,
            "data_needed": needed, "audit": audit, "plan": plan,
            "limitations": known_limitations(audit or {}, False),
            "assessment": assessment.pending("; ".join(missing))}


def load_data(opts: RunOptions, settings: Settings, calendar: MarketCalendar,
              benchmark_symbol: str | None):
    dataset = load_csv_dataset(opts.data_dir, settings, calendar)
    manifest_path = Path(opts.data_dir) / MANIFEST
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    bench = dataset.symbols.get(benchmark_symbol) if benchmark_symbol else None
    if bench is not None:
        rest = {k: v for k, v in dataset.symbols.items() if k != benchmark_symbol}
        if not rest:
            raise DatasetError("the dataset holds only the benchmark symbol")
        new_id = hashlib.sha256(f"{dataset.dataset_id}|minus|{benchmark_symbol}".encode()) \
            .hexdigest()[:16]
        dataset = replace(dataset, symbols=rest, dataset_id=new_id)
    suspects = corporate_action_suspects(dataset)
    dataset, actions = apply_corporate_action_policy(dataset, suspects)
    rows, info = load_membership(opts.data_dir)
    audit = audit_dataset(dataset, calendar, manifest, info, actions, suspects,
                          benchmark_symbol if bench is not None else None)
    has_daily = all(d.daily_source == "1d file" for d in dataset.symbols.values())
    return dataset, bench, rows, audit, known_limitations(audit, has_daily)


def _window_row(fold: int, partition: str, r: BacktestResult) -> dict:
    s = stats.summarize(r)
    return {"fold": fold, "partition": partition, "start": r.start, "end": r.end,
            "sessions": len(r.sessions), "signals": len(r.trades) + len(r.shadow),
            "trades": s["trades"], "gross_pnl": float(s["total_gross_pnl"]),
            "total_costs": float(s["total_costs"]), "net_pnl": float(s["total_net_pnl"]),
            "net_expectancy": float(s["expectancy_per_trade"])
            if s["expectancy_per_trade"] is not None else None,
            "win_rate_pct": float(s["win_rate_pct"]) if s["win_rate_pct"] is not None else None,
            "profit_factor": float(s["profit_factor"]) if s["profit_factor"] is not None
            else None, "profit_factor_note": s["profit_factor_note"],
            "max_drawdown_inr": float(s["max_drawdown"]),
            "rejected_signals": s["rejected_signals"]}


def evaluate(frozen: FrozenBaseline, opts: RunOptions, progress=_say) -> dict:
    if frozen.code_drift and not opts.allow_code_drift:
        raise CodeDriftError(
            "decision-path code has changed since the baseline was frozen (code hash "
            f"{frozen.body['code_hash'][:12]} -> {frozen.current_code_hash[:12]}). That is a "
            "new strategy version: freeze a new baseline, or pass --allow-code-drift to run "
            "anyway with the results labelled.")
    settings = frozen.historical_settings()
    calendar = MarketCalendar(settings.market)
    protocol = frozen.protocol

    missing = _missing_data(opts.data_dir)
    if missing:
        return pending_results(frozen, opts, missing)
    try:
        dataset, bench, membership_rows, audit, limitations = load_data(
            opts, settings, calendar, protocol.regimes.benchmark_symbol)
    except (DatasetError, DataError) as exc:
        return pending_results(frozen, opts, [f"dataset unusable: {exc}"])

    plan = make_plan(dataset, settings, calendar, protocol.walk_forward,
                     opts.train, opts.validation, opts.test)
    if plan.status != "OK":
        return pending_results(frozen, opts, [plan.reason], audit, plan.summary())

    # -- 4 + 6. primary windows and cost scenarios (one batch of jobs) -----------
    jobs, labels, keys = [], [], []
    for f in plan.folds:
        for w in f.windows():
            jobs.append((settings, w.start, w.end, True))
            labels.append(f"baseline fold {f.index} {w.partition}")
            keys.append(("base", f.index, w.partition))
    scenario_list = [sc for sc in protocol.cost_scenarios if sc.name != "base"]
    for sc in scenario_list:
        s2 = sensitivity.scenario_settings(settings, sc)
        for f in plan.folds:
            jobs.append((s2, f.test.start, f.test.end, False))
            labels.append(f"cost scenario {sc.name} fold {f.index}")
            keys.append((sc.name, f.index, "test"))
    progress(f"{len(jobs)} window simulations, {opts.workers} worker(s)")
    done = dict(zip(keys, sensitivity.run_jobs(dataset, calendar, membership_rows, jobs,
                                               opts.workers, labels,
                                               lambda lbl: progress(f"  done: {lbl}"))))
    errors = [f"{k}: {e}" for k, (st, _, e) in done.items() if st != "OK"]
    if errors:
        return pending_results(frozen, opts, [f"simulation failed: {errors[0]}"], audit,
                               plan.summary())
    folds, tests = [], []
    for f in plan.folds:
        for w in f.windows():
            r = done[("base", f.index, w.partition)][1]
            folds.append(_window_row(f.index, w.partition, r))
            if w.partition == "test":
                tests.append(r)
    initial = D(settings.capital.initial_capital)
    oos = chain_results(tests, initial)
    oos_summary = stats.summarize(oos)
    days = stats.session_pnl(oos)
    sp = protocol.statistics
    boot = stats.bootstrap(days, sp.bootstrap_resamples, sp.confidence, sp.seed, sp.block_length)
    sample = stats.sample_size(oos.trades, boot, sp.alpha, sp.power, sp.reference_effects_inr)
    diagnostics = stats.signal_diagnostics(oos, sp)

    scenarios = {}
    for sc in protocol.cost_scenarios:
        res = oos if sc.name == "base" else chain_results(
            [done[(sc.name, f.index, "test")][1] for f in plan.folds], initial)
        scenarios[sc.name] = {**sc.model_dump(), **sensitivity.gross_net(res.trades),
                              "rejected_signals": len(res.rejected),
                              "execution_model": res.execution_model_version}
    be = sensitivity.break_even(oos.trades)

    # -- 7. parameter sensitivity -------------------------------------------------
    if opts.grid == "none":
        sens = {"status": "SKIPPED", "reason": "--grid none"}
    else:
        variants = sensitivity.build_variants(protocol, settings)
        if opts.grid == "oat":
            variants = [v for v in variants if any(k != "grid" for k in v.kinds)]
        progress(f"sensitivity: {len(variants)} configurations, {opts.workers} worker(s)")
        runs = sensitivity.run_variants(dataset, settings, calendar, plan, variants,
                                        membership_rows, opts.workers,
                                        progress=lambda lbl: progress(f"  done: {lbl}"))
        sens = {"status": "OK", **sensitivity.sensitivity_tables(plan, variants, runs, protocol,
                                                                 settings)}

    # -- 8. regimes ----------------------------------------------------------------
    proxy, proxy_label = regimes.proxy_series(dataset, bench)
    classes = regimes.classify_sessions(proxy, protocol.regimes)
    regime_rows = regimes.group_trades(oos.trades, classes, protocol.regimes, oos.sessions)
    oos_classes = classes.loc[[d for d in oos.sessions if d in classes.index]]

    # -- 9. comparison baselines ---------------------------------------------------
    progress("comparison baselines")
    comparison = benchmarks.comparison_baselines(
        dataset, bench, settings, calendar, [list(f.test.sessions) for f in plan.folds],
        protocol.comparison_baselines.entry_time)

    test_rows = [r for r in folds if r["partition"] == "test"]
    verdict = assessment.assess(protocol, {"trades": len(oos.trades)}, boot, test_rows,
                                scenarios, diagnostics)
    m = meta(frozen, opts)
    m["dataset_id"] = dataset.dataset_id
    return {
        "status": "OK", "meta": m, "audit": audit, "limitations": limitations,
        "plan": plan.summary(), "folds": folds,
        "oos": oos_summary, "oos_result": oos,
        "bootstrap": boot, "sample_size": sample, "diagnostics": diagnostics,
        "gross_net": sensitivity.gross_net(oos.trades), "cost_scenarios": scenarios,
        "break_even": be, "sensitivity": sens,
        "regimes": {"proxy": proxy_label, "rules": protocol.regimes.model_dump(mode="json"),
                    "groups": regime_rows,
                    "sessions": oos_classes.reset_index(names="session").to_dict("records")},
        "comparison": comparison, "assessment": verdict,
    }


def run_id(results: dict) -> str:
    m = results["meta"]
    key = json.dumps({k: m.get(k) for k in ("frozen_sha256", "code_hash_current", "dataset_id",
                                            "sensitivity_grid", "window_overrides",
                                            "data_dir")}, sort_keys=True, default=str)
    key += json.dumps(results.get("plan"), sort_keys=True, default=str)
    key += json.dumps((results.get("audit") or {}).get("point_in_time_universe"),
                      sort_keys=True, default=str)
    return hashlib.sha256(key.encode()).hexdigest()[:12]

