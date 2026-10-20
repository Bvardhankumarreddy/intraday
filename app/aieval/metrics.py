"""Phase 11 metrics: per-arm performance, incremental value, paired
uncertainty and the predeclared decision rule.

Both arms are measured by the same functions on the same eligible sessions.
All money is INR, net of every transaction cost unless labelled gross.

- Closed trades only. An unfilled order is never a trade. A position closed by
  data end counts as a trade but is flagged ``incomplete``.
- win / loss: net P&L > 0 / < 0. Expectancy: mean net P&L per closed trade.
  Profit factor: sum of wins / |sum of losses|. It is None when undefined (no
  trades, or no losing trade), never infinity.
- Max drawdown: largest peak-to-trough fall of the arm's equity, starting
  from initial capital. Replay uses the mark-to-market curve at every candle
  close; prospective uses realized equity at each exit (for both arms alike).
- Exposure: total buy turnover (rupees committed to entries).

Incremental value, B minus A, is split by source on matched signals:
- selection: trades A took and B vetoed (the AI's veto removed them);
- capacity: trades only B took. A veto freed cash or a trade slot that a
  later signal used, so this is a change in exposure, not a better pick;
- shared: the same signal traded by both, with a different quantity because
  the cash paths differ.
These three add up to the total net P&L difference.

Uncertainty: a paired circular moving-block bootstrap over sessions. Both
arms of a session are resampled together, so common market conditions
cancel instead of being counted as independent evidence.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal

import numpy as np

from app.robustness.stats import block_length

ZERO = Decimal(0)


def _f(x: Decimal | None, nd: int = 2) -> float | None:
    return None if x is None else round(float(x), nd)


def arm_metrics(trades: list[dict], sessions: list[date], initial: Decimal,
                equity: list[tuple[str, Decimal]] | None = None) -> dict:
    """``trades``: dicts with net, gross, costs, buy_turnover, exit_time,
    session, exit_reason, incomplete, ambiguous."""
    nets = [t["net"] for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    n = len(trades)
    if not n:
        pf, pf_note = None, "undefined: no closed trades"
    elif not losses:
        pf, pf_note = None, "undefined: no losing trades"
    else:
        pf, pf_note = sum(wins, ZERO) / -sum(losses, ZERO), ""
    if equity is None:  # realized equity at each exit
        cum, equity = initial, []
        for t in sorted(trades, key=lambda t: t["exit_time"]):
            cum += t["net"]
            equity.append((t["exit_time"], cum))
    peak, dd, dd_pct = initial, ZERO, ZERO
    for _, eq in equity:
        peak = max(peak, eq)
        if peak - eq > dd:
            dd, dd_pct = peak - eq, (peak - eq) / peak * 100 if peak > 0 else ZERO
    total = sum(nets, ZERO)
    return {
        "eligible_sessions": len(sessions),
        "sessions_with_trades": len({t["session"] for t in trades}),
        "closed_trades": n, "wins": len(wins), "losses": len(losses),
        "gross_pnl": _f(sum((t["gross"] for t in trades), ZERO)),
        "total_costs": _f(sum((t["costs"] for t in trades), ZERO)),
        "net_pnl": _f(total),
        "avg_net_per_trade": _f(total / n) if n else None,
        "expectancy": _f(total / n) if n else None,
        "win_rate_pct": round(100 * len(wins) / n, 2) if n else None,
        "avg_win": _f(sum(wins, ZERO) / len(wins)) if wins else None,
        "avg_loss": _f(sum(losses, ZERO) / len(losses)) if losses else None,
        "profit_factor": _f(pf), "profit_factor_note": pf_note,
        "max_drawdown_inr": _f(dd), "max_drawdown_pct": _f(dd_pct),
        "exposure_buy_turnover": _f(sum((t["buy_turnover"] for t in trades), ZERO)),
        "incomplete_trades": sum(bool(t.get("incomplete")) for t in trades),
        "ambiguous_executions": sum(bool(t.get("ambiguous")) for t in trades),
        "exit_reasons": dict(sorted(Counter(t["exit_reason"] for t in trades).items())),
        "net_return_pct": _f(total / initial * 100) if initial else None,
    }


def session_table(trades: list[dict], sessions: list[date]) -> dict[date, tuple[Decimal, int]]:
    out = {d: (ZERO, 0) for d in sessions}
    for t in trades:
        net, k = out.get(t["session"], (ZERO, 0))
        out[t["session"]] = (net + t["net"], k + 1)
    return out


def decompose(a_trades: list[dict], b_trades: list[dict]) -> dict:
    a = {t["signal_id"]: t for t in a_trades}
    b = {t["signal_id"]: t for t in b_trades}
    a_only = [a[s] for s in a if s not in b]
    b_only = [b[s] for s in b if s not in a]
    both = [s for s in a if s in b]
    sel = -sum((t["net"] for t in a_only), ZERO)
    cap = sum((t["net"] for t in b_only), ZERO)
    shared = sum((b[s]["net"] - a[s]["net"] for s in both), ZERO)
    return {
        "trades_both": len(both), "trades_a_only_vetoed_by_ai": len(a_only),
        "trades_b_only_capacity": len(b_only),
        "selection_effect_inr": _f(sel),
        "capacity_effect_inr": _f(cap),
        "shared_trade_effect_inr": _f(shared),
        "total_difference_inr": _f(sel + cap + shared),
        "vetoed_trades_net_pnl_in_a": _f(-sel),
        "shared_trades_with_different_quantity": sum(
            1 for s in both if a[s]["quantity"] != b[s]["quantity"]),
        "note": "selection = minus the net P&L of baseline trades the AI vetoed; capacity = "
                "trades only the AI arm took because a veto freed cash or a slot (an "
                "exposure change, not a better pick); shared = quantity differences on the "
                "same signals. Two different equity curves do not show what caused the "
                "difference; this split attributes it on matched signals.",
    }


def random_veto_benchmark(a_trades: list[dict], b_trades: list[dict], resamples: int,
                          seed: int, confidence: float) -> dict:
    """Is the AI's veto better than vetoing the same NUMBER of baseline trades at
    random? A veto-everything AI "improves" on any losing baseline just by
    trading less; that is lower exposure, not selection skill. Compares the
    selection effect only (capacity effects excluded)."""
    nets = np.array([float(t["net"]) for t in a_trades], dtype=float)
    kept = {t["signal_id"] for t in b_trades}
    vetoed = np.array([t["signal_id"] not in kept for t in a_trades], dtype=bool)
    n, k = len(nets), int(vetoed.sum())
    ai_effect = float(-nets[vetoed].sum()) if n else 0.0
    base = {"baseline_trades": n, "vetoed": k, "ai_selection_effect": round(ai_effect, 2),
            "method": f"{resamples} random vetoes of {k} of the {n} baseline trades (seed "
                      f"{seed}); one-sided share of random vetoes doing at least as well"}
    if n == 0:
        return {**base, "status": "NO_BASELINE_TRADES", "beyond_random": False,
                "reason": "no baseline trade to select from"}
    if k == 0:
        return {**base, "status": "NO_VETOES", "beyond_random": False,
                "reason": "the AI vetoed no baseline trade: no selection to assess"}
    if k == n:
        return {**base, "status": "VETOED_ALL", "beyond_random": False,
                "random_expected_effect": round(ai_effect, 2),
                "reason": "the AI vetoed every baseline trade: equivalent to not trading, so "
                          "no selection skill can be shown"}
    rng = np.random.default_rng(seed)
    idx = np.argsort(rng.random((resamples, n)), axis=1)[:, :k]
    effects = -nets[idx].sum(axis=1)
    p = float((effects >= ai_effect - 1e-9).mean())
    return {**base, "status": "OK", "random_expected_effect": round(-(k / n) * nets.sum(), 2),
            "random_effect_quantiles": [round(float(x), 2) for x in
                                        np.quantile(effects, [0.05, 0.5, 0.95])],
            "share_random_at_least_as_good": round(p, 4),
            "beyond_random": p <= 1 - confidence,
            "reason": f"{p:.1%} of random vetoes of {k} trades did at least as well"}


def paired_bootstrap(table_a: dict, table_b: dict, sessions: list[date], resamples: int,
                     confidence: float, seed: int, fixed_block: int | None) -> dict:
    """CI for the mean per-session net P&L difference (B - A) and for the
    expectancy difference (B per trade - A per trade)."""
    n = len(sessions)
    pa = np.array([float(table_a[d][0]) for d in sessions], dtype=float)
    ca = np.array([table_a[d][1] for d in sessions], dtype=float)
    pb = np.array([float(table_b[d][0]) for d in sessions], dtype=float)
    cb = np.array([table_b[d][1] for d in sessions], dtype=float)
    diff = pb - pa
    base = {"method": "paired circular moving-block bootstrap over sessions (both arms of a "
                      "session resampled together)", "sessions": n, "resamples": resamples,
            "confidence": confidence, "seed": seed,
            "sessions_with_different_pnl": int((np.abs(diff) > 1e-9).sum())}
    est_diff = float(diff.mean()) if n else None
    exp_a = float(pa.sum() / ca.sum()) if n and ca.sum() else None
    exp_b = float(pb.sum() / cb.sum()) if n and cb.sum() else None
    point = {"mean_session_difference": round(est_diff, 4) if est_diff is not None else None,
             "total_difference": round(float(diff.sum()), 2) if n else None,
             "expectancy_difference": round(exp_b - exp_a, 4)
             if exp_a is not None and exp_b is not None else None}
    if n < 2:
        return {**base, **point, "status": "INSUFFICIENT",
                "reason": f"{n} session(s): no interval can be estimated",
                "ci_low": None, "ci_high": None, "se": None, "block_length": None,
                "expectancy_ci_low": None, "expectancy_ci_high": None}
    b = block_length(n, fixed_block)
    rng = np.random.default_rng(seed)
    blocks = math.ceil(n / b)
    starts = rng.integers(0, n, size=(resamples, blocks))
    idx = ((starts[:, :, None] + np.arange(b)[None, None, :]) % n).reshape(resamples, -1)[:, :n]
    means = diff[idx].mean(axis=1)
    alpha = (1 - confidence) / 2
    lo, hi = np.quantile(means, [alpha, 1 - alpha])
    sa, sb, ka, kb = pa[idx].sum(1), pb[idx].sum(1), ca[idx].sum(1), cb[idx].sum(1)
    ok = (ka > 0) & (kb > 0)
    exp_ci = (None, None)
    if ok.sum() >= max(100, resamples // 2):
        e = sb[ok] / kb[ok] - sa[ok] / ka[ok]
        exp_ci = tuple(round(float(x), 4) for x in np.quantile(e, [alpha, 1 - alpha]))
    return {**base, **point, "status": "OK", "block_length": b,
            "ci_low": round(float(lo), 4), "ci_high": round(float(hi), 4),
            "se": round(float(np.std(means, ddof=1)), 4),
            "expectancy_ci_low": exp_ci[0], "expectancy_ci_high": exp_ci[1],
            "expectancy_resamples_used": int(ok.sum()),
            "expectancy_note": None if exp_ci[0] is not None else
            "too few resamples with trades in both arms for an expectancy interval"}


def assess(protocol, n_sessions: int, a: dict, b: dict, discordant: int, boot: dict,
           model_cost_inr: float | None, structural_note: str | None = None,
           selection: dict | None = None) -> dict:
    """Mechanical application of the predeclared decision rule."""
    me, st = protocol.minimum_evidence, protocol.statistics
    margin = st.equivalence_margin_inr_per_session
    checks = {
        "sessions": [n_sessions, me.min_sessions, n_sessions >= me.min_sessions],
        "closed_trades_A": [a["closed_trades"], me.min_closed_trades_per_arm,
                            a["closed_trades"] >= me.min_closed_trades_per_arm],
        "closed_trades_B": [b["closed_trades"], me.min_closed_trades_per_arm,
                            b["closed_trades"] >= me.min_closed_trades_per_arm],
        "discordant_signals": [discordant, me.min_discordant_signals,
                               discordant >= me.min_discordant_signals],
    }
    out = {"minimum_evidence": {k: {"actual": v[0], "required": v[1], "met": v[2]}
                                for k, v in checks.items()},
           "equivalence_margin_inr_per_session": margin,
           "rule": "predeclared (config/ai_eval/protocol.yaml decision_rule)"}
    if structural_note:
        out["structural_note"] = structural_note
    if n_sessions == 0:
        return {**out, "conclusion": "PENDING_DATA",
                "reason": "no eligible session: nothing has been observed yet"}
    unmet = [k for k, v in checks.items() if not v[2]]
    lo, hi = boot.get("ci_low"), boot.get("ci_high")
    if unmet:
        return {**out, "conclusion": "INCONCLUSIVE",
                "reason": "insufficient evidence (not evidence of no effect): below the "
                          f"predeclared minimum for {', '.join(unmet)}"}
    if lo is None:
        return {**out, "conclusion": "INCONCLUSIVE", "reason": boot.get("reason", "no interval")}
    if lo > 0:
        if protocol.decision_rule.require_selection_beyond_random_veto and not (
                selection or {}).get("beyond_random"):
            return {**out, "conclusion": "INCONCLUSIVE",
                    "reason": f"trading difference CI [{lo}, {hi}] INR/session is above zero, "
                              "but the AI's vetoes do no better than random vetoes of the same "
                              "number of trades ("
                              + (selection or {}).get("reason", "not assessed")
                              + "): explained by lower exposure, not selection"}
        need = protocol.decision_rule.require_known_model_cost_for_improvement
        if model_cost_inr is None and need:
            return {**out, "conclusion": "INCONCLUSIVE",
                    "reason": f"trading difference CI [{lo}, {hi}] INR/session is above zero, "
                              "but the model/API cost in INR is unknown (prices or FX not "
                              "configured), so net incremental value cannot be shown"}
        per_session_cost = (model_cost_inr or 0) / n_sessions
        if lo - per_session_cost > 0:
            return {**out, "conclusion": "EVIDENCE_OF_IMPROVEMENT",
                    "reason": f"CI [{lo}, {hi}] INR/session above zero after model cost of "
                              f"{per_session_cost:.4f} INR/session"}
        return {**out, "conclusion": "INCONCLUSIVE",
                "reason": f"trading difference CI [{lo}, {hi}] is above zero but does not "
                          f"exceed the model cost of {per_session_cost:.4f} INR/session"}
    if hi < 0:
        return {**out, "conclusion": "NO_DEMONSTRATED_IMPROVEMENT",
                "reason": f"CI [{lo}, {hi}] INR/session is below zero: the AI-assisted arm "
                          "did worse"}
    if -margin < lo and hi < margin:
        return {**out, "conclusion": "NO_DEMONSTRATED_IMPROVEMENT",
                "reason": f"CI [{lo}, {hi}] lies inside the predeclared +/-{margin} INR/session "
                          "margin: no economically meaningful difference"}
    return {**out, "conclusion": "INCONCLUSIVE",
            "reason": f"CI [{lo}, {hi}] INR/session includes zero and extends beyond the "
                      f"+/-{margin} margin: too uncertain to conclude either way"}


def regime_table(classes, sessions: list[date], table_a: dict, table_b: dict) -> list[dict]:
    rows = []
    for dim in ("trend", "volatility", "liquidity"):
        groups: dict[str, list[date]] = defaultdict(list)
        for d in sessions:
            label = classes[dim].get(d, "unclassified") if classes is not None else \
                "unclassified"
            groups[label].append(d)
        for label, days in sorted(groups.items()):
            rows.append({"dimension": dim, "group": label, "sessions": len(days),
                         "A_trades": sum(table_a[d][1] for d in days),
                         "A_net_pnl": _f(sum((table_a[d][0] for d in days), ZERO)),
                         "B_trades": sum(table_b[d][1] for d in days),
                         "B_net_pnl": _f(sum((table_b[d][0] for d in days), ZERO))})
    return rows
