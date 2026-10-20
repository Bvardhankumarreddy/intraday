"""Transaction-cost scenarios and parameter sensitivity (predeclared in the
protocol; nothing is ever selected or deployed from these results).

Cost scenarios scale the frozen spread/slippage assumptions and optionally
require targets to trade through. The fee schedule is always the frozen
broker tariff. Each scenario is re-simulated on every out-of-sample test
window from fresh capital, exactly like the base case, so the risk engine
also sizes and screens with the scenario's costs. Results are reported gross
and net.

Break-even: from the base out-of-sample trades, with E = net expectancy per
trade and T = mean traded value per round trip (buy + sell turnover), the
extra cost per trade that brings E to zero is E rupees, i.e. E / T x 100 %
of traded value (equivalently, extra slippage of E / T x 100 % per side).
This holds the trade set fixed: in reality higher costs also change sizing
and rejections, so it is an approximation. It is reported only when E > 0.
Otherwise the cost reduction needed to break even is reported.

Parameter sensitivity: every one-at-a-time value and every 2-D grid cell
runs as its own configuration, once, continuously over the whole evaluated
range (first training session to last test session). Trades are then
attributed to each fold's train / validation / test windows by session
date. Continuous runs keep a 30-configuration grid tractable. Unlike the
primary evaluation, capital is carried across windows.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import numpy as np

from app.backtesting.dataset import HistoricalDataset
from app.backtesting.engine import BacktestResult, TradeRecord, run_backtest
from app.config import Settings, settings_from_values
from app.robustness.data_audit import membership_fn
from app.robustness.protocol import CostScenario, Protocol
from app.robustness.stats import expected_max_null, spearman, t_stat
from app.robustness.walk_forward import Plan
from app.utils.market_calendar import MarketCalendar


def scenario_settings(base: Settings, scenario: CostScenario) -> Settings:
    data = base.model_dump()
    data["execution"]["slippage_pct_per_side"] *= scenario.slippage_multiplier
    data["execution"]["assumed_spread_pct"] *= scenario.spread_multiplier
    data["backtest"]["target_trade_through_pct"] = scenario.target_trade_through_pct
    return settings_from_values(data)


def gross_net(trades: list[TradeRecord]) -> dict:
    n = len(trades)
    gross = sum((t.gross_pnl for t in trades), Decimal(0))
    costs = sum((t.total_costs for t in trades), Decimal(0))
    net = sum((t.net_pnl for t in trades), Decimal(0))
    return {"trades": n, "gross_pnl": float(gross), "total_costs": float(costs),
            "net_pnl": float(net),
            "gross_expectancy": round(float(gross) / n, 4) if n else None,
            "cost_per_trade": round(float(costs) / n, 4) if n else None,
            "net_expectancy": round(float(net) / n, 4) if n else None}


def break_even(trades: list[TradeRecord]) -> dict:
    if not trades:
        return {"status": "NOT_MEANINGFUL", "note": "no out-of-sample trades"}
    n = len(trades)
    e = float(sum((t.net_pnl for t in trades), Decimal(0))) / n
    g = float(sum((t.gross_pnl for t in trades), Decimal(0))) / n
    turnover = float(sum((t.buy_turnover + t.sell_turnover for t in trades), Decimal(0))) / n
    pct = e / turnover * 100 if turnover else None
    base = {"net_expectancy": round(e, 4), "gross_expectancy": round(g, 4),
            "mean_traded_value_per_trade": round(turnover, 2),
            "assumption": "trade set held fixed (sizing/rejections would also change)"}
    if e > 0:
        return {**base, "status": "OK", "extra_cost_per_trade_inr": round(e, 4),
                "extra_cost_pct_of_traded_value": round(pct, 5) if pct is not None else None}
    return {**base, "status": "NOT_MEANINGFUL",
            "note": (f"net expectancy is already {e:.2f} <= 0 at base costs; costs would have to "
                     f"fall by Rs {abs(e):.2f} per trade ({abs(pct or 0):.4f}% of traded value) "
                     "to break even")
            if g > 0 else "gross expectancy is <= 0: no cost level makes it break even"}


# -- parameter sensitivity ----------------------------------------------------


@dataclass(frozen=True)
class Variant:
    label: str
    changes: tuple[tuple[str, Any], ...]  # ((section.key, value), ...)
    kinds: tuple[str, ...]  # base / oat:<param> / grid
    untestable: str | None = None

    @property
    def key(self) -> str:
        return hashlib.sha256(json.dumps(sorted(self.changes), default=str).encode()).hexdigest()


def _get(settings: Settings, dotted: str):
    section, key = dotted.split(".")
    value = getattr(getattr(settings, section), key)
    return getattr(value, "value", value)


def _same(a, b) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def build_variants(protocol: Protocol, base: Settings) -> list[Variant]:
    """The predeclared configurations, deduplicated; the frozen baseline
    first. A change equal to the frozen value is dropped."""
    found: dict[str, Variant] = {}

    def add(changes: list[tuple[str, Any]], kind: str, untestable: str | None = None):
        changes = tuple((p, v) for p, v in changes if not _same(_get(base, p), v))
        label = "baseline" if not changes else ", ".join(f"{p}={v}" for p, v in changes)
        v = Variant(label, changes, (kind,), untestable if changes else None)
        if v.key in found:
            old = found[v.key]
            found[v.key] = Variant(old.label, old.changes, old.kinds + (kind,), old.untestable)
        else:
            found[v.key] = v

    add([], "base")
    for spec in protocol.sensitivity.one_at_a_time:
        for value in spec.values:
            add([(spec.parameter, value)], f"oat:{spec.parameter}", spec.untestable)
    grid = protocol.sensitivity.grid_2d
    if grid is not None:
        for x in grid.x.values:
            for y in grid.y.values:
                add([(grid.x.parameter, x), (grid.y.parameter, y)], "grid")
    variants = list(found.values())
    return sorted(variants, key=lambda v: (v.label != "baseline", v.label))


def variant_settings(base: Settings, variant: Variant) -> Settings:
    data = base.model_dump()
    for dotted, value in variant.changes:
        section, key = dotted.split(".")
        data[section][key] = value
    return settings_from_values(data)


# -- running simulations (optionally in parallel) --------------------------------
# Each job is (settings, start, end, shadow). The dataset, calendar and
# membership go to every worker once. Results do not depend on the number of
# workers: every job is an independent, deterministic simulation.

_SHARED: dict = {}


def _init_worker(dataset, calendar, membership_rows) -> None:
    _SHARED.update(dataset=dataset, calendar=calendar, membership=membership_fn(membership_rows))


def _run_job(job) -> tuple[str, BacktestResult | None, str]:
    settings, start, end, shadow = job
    try:
        return "OK", run_backtest(_SHARED["dataset"], settings, _SHARED["calendar"], start, end,
                                  _SHARED["membership"], shadow=shadow), ""
    except Exception as exc:  # reported per job, never hidden
        return "ERROR", None, f"{type(exc).__name__}: {exc}"


def run_jobs(dataset: HistoricalDataset, calendar: MarketCalendar, membership_rows,
             jobs: list[tuple], workers: int, labels: list[str] | None = None,
             progress=None) -> list[tuple[str, BacktestResult | None, str]]:
    labels = labels or [str(i) for i in range(len(jobs))]
    out = []
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=min(workers, len(jobs)), initializer=_init_worker,
                                 initargs=(dataset, calendar, membership_rows)) as pool:
            for label, res in zip(labels, pool.map(_run_job, jobs)):
                out.append(res)
                if progress:
                    progress(label)
    else:
        _init_worker(dataset, calendar, membership_rows)
        for label, job in zip(labels, jobs):
            out.append(_run_job(job))
            if progress:
                progress(label)
    return out


def run_variants(dataset: HistoricalDataset, base: Settings, calendar: MarketCalendar,
                 plan: Plan, variants: list[Variant], membership_rows, workers: int,
                 progress=None) -> dict[str, tuple[str, BacktestResult | None, str]]:
    start, end = plan.evaluated_range
    todo = [v for v in variants if v.untestable is None]
    jobs = [(variant_settings(base, v), start, end, False) for v in todo]
    results = run_jobs(dataset, calendar, membership_rows, jobs, workers,
                       [v.label for v in todo], progress)
    return {v.key: r for v, r in zip(todo, results)}


def _window_metrics(trades: list[TradeRecord], sessions: set[date]) -> dict:
    sel = [t for t in trades if t.session in sessions]
    n = len(sel)
    net = float(sum((t.net_pnl for t in sel), Decimal(0)))
    return {"trades": n, "net_pnl": round(net, 2),
            "net_expectancy": round(net / n, 4) if n else None,
            "win_rate_pct": round(100 * sum(t.net_pnl > 0 for t in sel) / n, 2) if n else None}


def sensitivity_tables(plan: Plan, variants: list[Variant], runs: dict,
                       protocol: Protocol, base: Settings) -> dict:
    oos = set(plan.oos_sessions)
    long_rows, summary = [], []
    fold_pairs: dict[int, list[tuple[float, float]]] = {}
    tstats = []
    for v in variants:
        status, result, error = runs.get(v.key, ("UNTESTABLE", None, v.untestable or ""))
        row = {"configuration": v.label, "kinds": "; ".join(v.kinds),
               "is_frozen_baseline": v.label == "baseline", "status": status,
               "note": error, **{p: val for p, val in v.changes}}
        if result is None:
            summary.append(row)
            continue
        trades = result.trades
        pooled = _window_metrics(trades, oos)
        fold_exp = {"train": [], "validation": [], "test": []}
        for f in plan.folds:
            per = {}
            for w in f.windows():
                m = _window_metrics(trades, set(w.sessions))
                per[w.partition] = m
                long_rows.append({"configuration": v.label, "fold": f.index,
                                  "partition": w.partition, "start": w.start, "end": w.end, **m})
                if m["net_expectancy"] is not None:
                    fold_exp[w.partition].append(m["net_expectancy"])
            if per["train"]["net_expectancy"] is not None and \
                    per["test"]["net_expectancy"] is not None:
                fold_pairs.setdefault(f.index, []).append(
                    (per["train"]["net_expectancy"], per["test"]["net_expectancy"]))
        t = t_stat([float(x.net_pnl) for x in trades])
        tstats.append((v.label, t))
        summary.append({
            **row, "trades_total": len(trades),
            "oos_trades": pooled["trades"], "oos_net_pnl": pooled["net_pnl"],
            "oos_net_expectancy": pooled["net_expectancy"], "oos_win_rate_pct":
                pooled["win_rate_pct"],
            **{f"mean_fold_{p}_expectancy": round(float(np.mean(x)), 4) if x else None
               for p, x in fold_exp.items()},
            "t_stat_all_trades": round(t, 3) if t is not None else None,
        })

    find = _row_lookup(base, variants, summary)
    flags = _instability(protocol, find)
    tested = [r for r in summary if r["status"] == "OK"]
    best = max(((lbl, t) for lbl, t in tstats if t is not None), key=lambda x: x[1],
               default=(None, None))
    null_max = expected_max_null(len(tested))
    rank_corr = {f: spearman([a for a, _ in p], [b for _, b in p]) for f, p in fold_pairs.items()}
    return {
        "summary": summary, "by_fold": long_rows, "instability": flags,
        "heatmap": _heatmap(protocol, find),
        "configurations_listed": len(summary),
        "configurations_tested": len(tested),
        "configurations_untestable": sum(1 for r in summary if r["status"] == "UNTESTABLE"),
        "configurations_failed": sum(1 for r in summary if r["status"] == "ERROR"),
        "selection_bias": {
            "best_configuration_by_t": best[0], "best_t_stat": round(best[1], 3)
            if best[1] is not None else None,
            "expected_max_t_under_no_edge": round(null_max, 3) if null_max else None,
            "note": "With N configurations and no real edge, the best t-statistic is expected "
                    "to reach about the value shown by chance alone. A best result below it is "
                    "consistent with luck. Nothing is selected from this table.",
        },
        "train_test_rank_correlation_by_fold": rank_corr,
        "rank_correlation_note": "Spearman correlation across configurations between training "
                                 "and test expectancy within each fold. Near zero or negative "
                                 "means in-sample ranking does not carry over (overfitting "
                                 "risk). None = fewer than 3 configurations with trades in both.",
    }


def _row_lookup(base: Settings, variants: list[Variant], summary: list[dict]):
    """Find the summary row of the configuration with exactly these changes
    (values equal to the frozen baseline count as unchanged)."""
    rows = {v.key: r for v, r in zip(variants, summary)}

    def find(changes: list[tuple[str, Any]]) -> dict | None:
        kept = tuple((p, v) for p, v in changes if not _same(_get(base, p), v))
        return rows.get(Variant("", kept, ()).key)

    return find


def _instability(protocol: Protocol, find) -> list[dict]:
    limit = protocol.sensitivity.unstable_trade_count_change
    flags = []
    for spec in protocol.sensitivity.one_at_a_time:
        if spec.untestable:
            continue
        rows = [(value, find([(spec.parameter, value)])) for value in spec.values]
        for (va, ra), (vb, rb) in zip(rows, rows[1:]):
            if not ra or not rb or ra.get("status") != "OK" or rb.get("status") != "OK":
                continue
            ea, eb = ra.get("oos_net_expectancy"), rb.get("oos_net_expectancy")
            na, nb = ra.get("oos_trades", 0), rb.get("oos_trades", 0)
            reasons = []
            if ea is not None and eb is not None and ea * eb < 0:
                reasons.append(f"net expectancy changes sign ({ea} -> {eb})")
            if max(na, nb) and abs(na - nb) / max(na, nb) > limit:
                reasons.append(f"trade count changes {na} -> {nb}")
            if reasons:
                flags.append({"parameter": spec.parameter, "from": va, "to": vb,
                              "reasons": "; ".join(reasons)})
    return flags


def _heatmap(protocol: Protocol, find) -> dict | None:
    grid = protocol.sensitivity.grid_2d
    if grid is None:
        return None
    xs, ys = grid.x, grid.y
    cells = []
    for y in ys.values:
        for x in xs.values:
            row = find([(xs.parameter, x), (ys.parameter, y)])
            ok = row is not None and row.get("status") == "OK"
            cells.append({"x": x, "y": y,
                          "oos_net_expectancy": row.get("oos_net_expectancy") if ok else None,
                          "oos_trades": row.get("oos_trades") if ok else None})
    return {"x": xs.parameter, "y": ys.parameter, "x_values": xs.values, "y_values": ys.values,
            "cells": cells}
