"""Phase 7 statistics. Formulas and assumptions (all money in INR, net of
every cost unless labelled gross):

Trade metrics reuse ``app.backtesting.metrics.compute_metrics``:
expectancy, win rate, average win/loss, profit factor (None with no losing
trades, never infinity), drawdown, holding time and cost ratios. Phase 4's
fixed "evidence" label (a trade-count cut-off) is dropped here in favour of
the uncertainty measures below.

Extra measures:
- ``max_consecutive_losses``: longest run of net_pnl < 0 in exit order.
- Return distribution: quantiles of net P&L and of net return per trade
  (net / buy turnover x 100), plus a histogram.
- Per-session P&L: the net P&L of every eligible session, including
  sessions with no trade (0). Per-session mean / standard deviation is
  reported as a plain ratio. It is **not** annualised: intraday trades are
  few, irregular and not normally distributed, so a sqrt(250) scaling would
  overstate precision.

Uncertainty (``bootstrap``): a **moving-block bootstrap over sessions**
(circular, block length b = ceil(n_sessions^(1/3)) unless predeclared).
Whole sessions, and runs of b consecutive sessions, are resampled together,
so trades on the same day or in nearby days are not treated as independent.
The statistic is net expectancy per trade (sum of net / number of trades in
the resample). Resamples with no trades are dropped and counted. The CI is
the percentile interval. The seed is fixed, so reruns are identical.

Sample size (``sample_size``): sd = standard deviation of trade net P&L;
naive SE = sd / sqrt(n); design effect = (bootstrap SE / naive SE)^2;
effective n = n / design effect. Minimum detectable effect at the
predeclared alpha and power = (z(1-alpha/2) + z(power)) x bootstrap SE.
Trades needed for an effect d ~= design effect x ((z_a + z_b) x sd / d)^2.
These are approximations that assume the future resembles the sample.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import date
from decimal import Decimal
from statistics import NormalDist

import numpy as np
import pandas as pd

from app.backtesting.engine import BacktestResult, ShadowOutcome, TradeRecord
from app.backtesting.metrics import compute_metrics

_N = NormalDist()


def max_consecutive_losses(trades: list[TradeRecord]) -> int:
    best = run = 0
    for t in sorted(trades, key=lambda t: t.exit_time):
        run = run + 1 if t.net_pnl < 0 else 0
        best = max(best, run)
    return best


def session_pnl(result: BacktestResult) -> list[tuple[date, float, int]]:
    """(session, net P&L, closed trades) for every eligible session."""
    by_day: dict[date, list[Decimal]] = {d: [] for d in result.sessions}
    for t in result.trades:
        by_day.setdefault(t.session, []).append(t.net_pnl)
    return [(d, float(sum(v, Decimal(0))), len(v)) for d, v in sorted(by_day.items())]


def _quantiles(values: list[float]) -> dict | None:
    if not values:
        return None
    a = np.asarray(values, dtype=float)
    q = np.quantile(a, [0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0])
    return {k: round(float(v), 4) for k, v in zip(
        ["min", "p10", "p25", "median", "p75", "p90", "max"], q)}


def histogram(values: list[float], bins: int = 12) -> list[dict]:
    if not values:
        return []
    counts, edges = np.histogram(np.asarray(values, dtype=float), bins=bins)
    return [{"from": round(float(edges[i]), 2), "to": round(float(edges[i + 1]), 2),
             "count": int(c)} for i, c in enumerate(counts)]


def summarize(result: BacktestResult) -> dict:
    """Phase 4 metrics plus the Phase 7 extras for one (possibly chained) result."""
    m = compute_metrics(result, 1)
    m.pop("evidence", None)
    m.pop("min_trades_for_evidence", None)
    trades = result.trades
    nets = [float(t.net_pnl) for t in trades]
    rets = [float(t.net_pnl / t.buy_turnover * 100) for t in trades if t.buy_turnover]
    days = session_pnl(result)
    day_values = [v for _, v, _ in days]
    sd = float(np.std(day_values, ddof=1)) if len(day_values) > 1 else None
    mean = float(np.mean(day_values)) if day_values else None
    m.update({
        "eligible_sessions": len(result.sessions),
        "sessions_with_trades": sum(1 for _, _, n in days if n),
        "max_consecutive_losses": max_consecutive_losses(trades),
        "net_pnl_quantiles": _quantiles(nets),
        "net_return_pct_quantiles": _quantiles(rets),
        "net_pnl_histogram": histogram(nets),
        "session_mean_net_pnl": round(mean, 4) if mean is not None else None,
        "session_sd_net_pnl": round(sd, 4) if sd is not None else None,
        "session_mean_over_sd": round(mean / sd, 4) if sd else None,
        "session_ratio_note": "per-session mean / sd of net P&L, incl. zero-trade sessions; "
                              "not annualised",
        "total_gross_profit_trades": float(sum((t.gross_pnl for t in trades if t.gross_pnl > 0),
                                               Decimal(0))),
    })
    return m


def block_length(n_sessions: int, fixed: int | None) -> int:
    return fixed or max(1, math.ceil(n_sessions ** (1 / 3)))


def bootstrap(days: list[tuple[date, float, int]], resamples: int, confidence: float,
              seed: int, fixed_block: int | None = None) -> dict:
    """Moving-block (circular) bootstrap of net expectancy per trade."""
    n = len(days)
    trades = sum(k for _, _, k in days)
    base = {"method": "circular moving-block bootstrap over sessions; statistic = net "
                      "expectancy per trade", "sessions": n, "trades": trades,
            "resamples": resamples, "confidence": confidence, "seed": seed}
    if n < 2 or trades < 2:
        return {**base, "status": "INSUFFICIENT",
                "reason": f"{trades} trade(s) over {n} session(s): too few for an interval",
                "estimate": (sum(v for _, v, _ in days) / trades) if trades else None,
                "ci_low": None, "ci_high": None, "se": None, "block_length": None}
    b = block_length(n, fixed_block)
    pnl = np.array([v for _, v, _ in days], dtype=float)
    cnt = np.array([k for _, _, k in days], dtype=float)
    rng = np.random.default_rng(seed)
    blocks = math.ceil(n / b)
    starts = rng.integers(0, n, size=(resamples, blocks))
    idx = ((starts[:, :, None] + np.arange(b)[None, None, :]) % n).reshape(resamples, -1)[:, :n]
    tot_pnl, tot_cnt = pnl[idx].sum(axis=1), cnt[idx].sum(axis=1)
    ok = tot_cnt > 0
    stats = tot_pnl[ok] / tot_cnt[ok]
    alpha = (1 - confidence) / 2
    lo, hi = np.quantile(stats, [alpha, 1 - alpha])
    return {**base, "status": "OK", "block_length": b,
            "estimate": round(float(pnl.sum() / cnt.sum()), 4),
            "ci_low": round(float(lo), 4), "ci_high": round(float(hi), 4),
            "se": round(float(np.std(stats, ddof=1)), 4),
            "resamples_without_trades": int((~ok).sum())}


def sample_size(trades: list[TradeRecord], boot: dict, alpha: float, power: float,
                reference_effects: list[float]) -> dict:
    nets = np.array([float(t.net_pnl) for t in trades], dtype=float)
    n = len(nets)
    z = _N.inv_cdf(1 - alpha / 2) + _N.inv_cdf(power)
    out = {"trades": n, "alpha": alpha, "power": power}
    if n < 2 or boot.get("se") is None:
        return {**out, "status": "INSUFFICIENT",
                "note": "fewer than 2 closed trades: no variance estimate, so the sample "
                        "cannot distinguish any performance level"}
    sd = float(np.std(nets, ddof=1))
    naive_se = sd / math.sqrt(n)
    deff = (boot["se"] / naive_se) ** 2 if naive_se > 0 else 1.0
    deff = max(deff, 1e-9)
    mean = float(nets.mean())
    effects = sorted({round(abs(mean), 2)} | {float(e) for e in reference_effects} - {0.0})
    needed = {f"{e:g}": (math.ceil(deff * (z * sd / e) ** 2) if e > 0 else None)
              for e in effects}
    mde = z * boot["se"]
    lo, hi = boot["ci_low"], boot["ci_high"]
    smallest_ref = min(reference_effects) if reference_effects else None
    if lo > 0:
        reading = "evidence of positive net expectancy at the stated confidence"
    elif hi < 0:
        reading = "evidence of negative net expectancy at the stated confidence"
    elif smallest_ref and -smallest_ref < lo and hi < smallest_ref:
        reading = (f"evidence of no economically meaningful edge: the interval excludes "
                   f"expectancies beyond +/- Rs {smallest_ref:g} per trade")
    else:
        reading = ("lack of evidence: the interval includes zero and is too wide to rule "
                   "out meaningful positive or negative expectancy")
    return {**out, "status": "OK", "sd_net_pnl": round(sd, 4), "naive_se": round(naive_se, 4),
            "bootstrap_se": boot["se"], "design_effect": round(deff, 3),
            "effective_n": round(n / deff, 1), "min_detectable_effect": round(mde, 4),
            "trades_needed_for_effect": needed, "reading": reading}


def expected_max_null(n: int) -> float | None:
    """Approximate expected maximum of n independent standard-normal
    statistics (Bailey & Lopez de Prado). The best t-statistic of n
    configurations with no edge would be about this large by chance."""
    if n < 2:
        return None
    g = 0.5772156649
    return (1 - g) * _N.inv_cdf(1 - 1 / n) + g * _N.inv_cdf(1 - 1 / (n * math.e))


def t_stat(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    sd = float(np.std(values, ddof=1))
    return float(np.mean(values)) / (sd / math.sqrt(len(values))) if sd > 0 else None


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3:
        return None
    r = pd.Series(x).rank().corr(pd.Series(y).rank())
    return None if pd.isna(r) else round(float(r), 4)


def r_multiple(t: TradeRecord) -> Decimal | None:
    risk = t.entry_price - t.stop
    return round((t.exit_price - t.entry_price) / risk, 4) if risk > 0 else None


def signal_diagnostics(result: BacktestResult, stats_plan) -> dict:
    """DIAGNOSTIC, not account performance: every technically qualified
    signal (executed trades plus risk-rejected shadows), measured in gross R
    (price move / per-share risk at the fill) before costs."""
    shadow: list[ShadowOutcome] = result.shadow
    executed = [(t.session, float(r)) for t in result.trades
                if (r := r_multiple(t)) is not None]
    shadowed = [(s.session, float(s.r_multiple)) for s in shadow if s.r_multiple is not None]
    rows = executed + shadowed
    by_day: dict[date, list[float]] = {d: [] for d in result.sessions}
    for d, r in rows:
        by_day.setdefault(d, []).append(r)
    days = [(d, float(sum(v)), len(v)) for d, v in sorted(by_day.items())]
    boot = bootstrap(days, stats_plan.bootstrap_resamples, stats_plan.confidence,
                     stats_plan.seed, stats_plan.block_length)
    boot["method"] = boot["method"].replace("net expectancy per trade", "mean gross R per signal")
    afford = [s for s in shadow if s.affordable_net_pnl is not None]
    rs = [r for _, r in rows]
    return {
        "label": "DIAGNOSTIC - signal quality before capital and risk limits; not account "
                 "performance",
        "signals_total": len(result.trades) + len(shadow),
        "executed": len(result.trades),
        "risk_rejected": len(shadow),
        "risk_rejected_by_status": dict(sorted(Counter(s.review_status for s in shadow).items())),
        "rejected_not_filled": sum(1 for s in shadow if not s.filled),
        "with_outcome": len(rows),
        "mean_gross_r": round(float(np.mean(rs)), 4) if rs else None,
        "median_gross_r": round(float(np.median(rs)), 4) if rs else None,
        "share_positive_gross": round(sum(r > 0 for r in rs) / len(rs), 4) if rs else None,
        "gross_r_bootstrap": boot,
        "shadow_affordable_trades": len(afford),
        "shadow_affordable_net_total": float(sum((s.affordable_net_pnl for s in afford),
                                                 Decimal(0))),
        "shadow_affordable_note": "risk-rejected signals sized at the whole shares the "
                                  "session's cash could buy, ignoring the risk-per-trade rule, "
                                  "after all costs. Shows the cost drag, not a tradable result",
    }
