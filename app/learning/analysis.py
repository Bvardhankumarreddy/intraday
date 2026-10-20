"""Learning and review engine (Phase 13): aggregates, recurring errors, period
changes, findings and improvement proposals. Fully deterministic. The
language model invents no metric and writes no proposal.

Counting rules:
- A signal reviewed in several research runs is counted ONCE for
  performance: its earliest record, the decision the system actually acted
  on first. Every record still counts for AI-quality measures.
- Trade-path results are HYPOTHETICAL: Phase 4 candle rules, the
  recommendation's whole-share quantity and Phase 3 costs. Paper results are
  reported separately as actual simulated fills.
- Baseline = every deterministically eligible setup (screen PASS, risk
  QUALIFIED_FOR_REVIEW, fresh data). AI-assisted = the eligible setups the AI
  marked CONSIDER.

Statistics:
- Rates use Wilson score intervals.
- Mean differences between two periods use a bootstrap over sessions
  (sessions resampled within each period, seeded).
- Groups below ``learning.min_sample`` are shown with "insufficient sample"
  and never interpreted.
- A finding is a HYPOTHESIS unless its sample and interval support it
  (ESTABLISHED_PATTERN). Even then it is only a reason to test a change,
  never proof that a change will help.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import date
from statistics import NormalDist

import numpy as np

from app.learning.dataset import LearningStore
from app.learning.review import latest_reviews


def wilson(k: int, n: int, confidence: float) -> tuple[float | None, float | None]:
    if n == 0:
        return None, None
    z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return round(c - h, 4), round(c + h, 4)


def _stats(nets: list[float], min_n: int) -> dict:
    n = len(nets)
    if not n:
        return {"n": 0, "interpretable": False, "note": "no outcome"}
    arr = np.array(nets)
    cum = np.cumsum(arr)
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0], cum]))[1:] - cum)) if n else 0
    wins, losses = arr[arr > 0], arr[arr < 0]
    out = {"n": n, "net_total": round(float(arr.sum()), 2),
           "net_expectancy": round(float(arr.mean()), 4),
           "win_rate": round(float((arr > 0).mean()), 4),
           "avg_win": round(float(wins.mean()), 2) if len(wins) else None,
           "avg_loss": round(float(losses.mean()), 2) if len(losses) else None,
           "max_drawdown": round(dd, 2),
           "interpretable": n >= min_n,
           "note": None if n >= min_n else f"insufficient sample ({n} < {min_n})"}
    if n >= min_n and arr.std(ddof=1) > 0:
        out["mean_over_sd"] = round(float(arr.mean() / arr.std(ddof=1)), 4)
    return out


def _boot_diff(a: dict, b: dict, resamples: int = 2000, seed: int = 13,
               confidence: float = 0.95) -> tuple[float | None, float | None]:
    """CI for mean(b) - mean(a); a and b map session -> list of values."""
    if len(a) < 2 or len(b) < 2:
        return None, None
    rng = np.random.default_rng(seed)
    ka, kb = list(a), list(b)
    diffs = []
    for _ in range(resamples):
        sa = [v for s in rng.choice(ka, len(ka)) for v in a[s]]
        sb = [v for s in rng.choice(kb, len(kb)) for v in b[s]]
        if sa and sb:
            diffs.append(np.mean(sb) - np.mean(sa))
    if len(diffs) < resamples // 2:
        return None, None
    lo, hi = np.quantile(diffs, [(1 - confidence) / 2, 1 - (1 - confidence) / 2])
    return round(float(lo), 4), round(float(hi), 4)


def _eligible(f: dict, data_fresh: bool) -> bool:
    return bool(f.get("has_setup") and f.get("screen_status") == "PASS"
                and f.get("risk_status") == "QUALIFIED_FOR_REVIEW" and data_fresh)


def _trend(bench_eod: float | None, threshold: float) -> str:
    if bench_eod is None:
        return "unclassified"
    return "strong_up" if bench_eod >= threshold else "strong_down" if bench_eod <= -threshold \
        else "range_bound"


def analyse(lstore: LearningStore, settings) -> dict:
    cfg = settings.learning
    recs = lstore.records()
    outs = lstore.outcomes()
    reviews = latest_reviews(lstore)
    trend_t = 0.75  # Phase 7 predeclared trend threshold (config/robustness/protocol.yaml)
    rows = []
    for r in recs:
        b, o = r["body"], outs.get(r["record_id"], {})
        tp = o.get("TRADE_PATH") or {}
        eod = o.get("EOD") or {}
        rev = reviews.get(r["record_id"])
        rows.append({
            "record_id": r["record_id"], "signal_id": b.get("signal_id"),
            "session": b["market_date"], "decided": b["decision_ts_utc"],
            "symbol": b["symbol"], "recommendation": b["recommendation"],
            "strategy": b["strategy_version"], "ai_version": f"{b['model']}/{b['prompt_version']}",
            "setup": b["features"].get("rvol_bucket") or "n/a",
            "eligible": _eligible(b["features"], b["data_fresh"]),
            "trade_net": float(tp["net_pnl"]) if tp.get("net_pnl") is not None else None,
            "trade_status": tp.get("status"),
            "paper_net": (o.get("PAPER_EXECUTION") or {}).get("position", {}).get("net_pnl")
            if o.get("PAPER_EXECUTION") else None,
            "regime": _trend(eod.get("benchmark_return_pct"), trend_t),
            "week": "{}-W{:02d}".format(*date.fromisoformat(b["market_date"]).isocalendar()[:2]),
            "process_grade": rev["process_grade"] if rev else None,
            "process_categories": json.loads(rev["process_categories"])
            if rev else [],
            "outcome_grade": rev["outcome_grade"] if rev else None,
            "outcome_category": rev["outcome_category"] if rev else None,
            "unsupported": len(b["unsupported_claims"]), "ai_status": b["ai_status"],
            "risk_status": b["features"].get("risk_status"),
            "risk_reasons": b["features"].get("risk_reasons", []),
            "rule_compliant": b["rule_compliant"],
        })
    # one row per signal for performance (earliest decision)
    first: dict[str, dict] = {}
    for x in sorted(rows, key=lambda x: x["decided"]):
        if x["signal_id"] and x["signal_id"] not in first:
            first[x["signal_id"]] = x
    signals = list(first.values())
    traded = [x for x in signals if x["trade_net"] is not None]
    base = [x for x in traded if x["eligible"]]
    ai = [x for x in base if x["recommendation"] == "CONSIDER"]
    vetoed = [x for x in base if x["recommendation"] in ("WAIT", "NO_TRADE")]

    def groups(dim: str) -> list[dict]:
        g: dict[str, list[float]] = defaultdict(list)
        for x in traded:
            g[str(x[dim])].append(x["trade_net"])
        return [{"dimension": dim, "group": k, **_stats(v, cfg.min_sample)}
                for k, v in sorted(g.items())]

    sessions = sorted({x["session"] for x in rows})
    reviewed = [x for x in rows if x["process_grade"]]
    n_rev = len(reviewed)
    # Process categories: every reviewed decision record. Outcome categories: once
    # per signal (its first record), so repeated reviews do not inflate rates.
    sig_reviewed = [x for x in signals if x["process_grade"]]
    recurring = []
    for scope, pool, counts in (
            ("records", reviewed, Counter(c for x in reviewed for c in x["process_categories"])),
            ("signals", sig_reviewed, Counter(
                x["outcome_category"] for x in sig_reviewed
                if x["outcome_category"] not in ("NONE", "INSUFFICIENT_EVIDENCE")))):
        n = len(pool)
        for cat, k in sorted(counts.items()):
            lo, hi = wilson(k, n, cfg.confidence)
            recurring.append({"category": cat, "scope": scope, "count": k, "of_reviewed": n,
                              "rate": round(k / n, 4) if n else None, "rate_ci": [lo, hi],
                              "recurring": k >= cfg.min_recurring_count and lo is not None
                              and lo >= cfg.recurring_rate,
                              "supporting_ids": [x["record_id"] for x in pool if cat in
                                                 x["process_categories"] or
                                                 x["outcome_category"] == cat][:20]})

    # period change: first half of sessions vs second half (trade paths, per session)
    change = {"status": "INSUFFICIENT", "reason": "fewer than 4 sessions with outcomes"}
    tsess = sorted({x["session"] for x in traded})
    if len(tsess) >= 4:
        half = len(tsess) // 2
        pa = defaultdict(list)
        pb = defaultdict(list)
        for x in traded:
            (pa if x["session"] in tsess[:half] else pb)[x["session"]].append(x["trade_net"])
        lo, hi = _boot_diff(pa, pb, confidence=cfg.confidence)
        na, nb = sum(map(len, pa.values())), sum(map(len, pb.values()))
        meaningful = lo is not None and min(na, nb) >= cfg.min_sample and (lo > 0 or hi < 0)
        change = {"status": "OK", "earlier": [tsess[0], tsess[half - 1], na],
                  "later": [tsess[half], tsess[-1], nb], "diff_ci": [lo, hi],
                  "meaningful": meaningful,
                  "reading": ("statistically meaningful change in hypothetical net expectancy"
                              if meaningful else "no meaningful change shown (small sample or "
                                                 "interval includes zero)")}

    claims_bad = [x for x in rows if x["unsupported"]]
    out = {
        "records": len(rows), "signals": len(signals), "sessions": len(sessions),
        "period": [sessions[0], sessions[-1]] if sessions else None,
        "reviewed": n_rev, "with_trade_outcome": len(traded),
        "pending_review": len(rows) - n_rev,
        "recommendations": dict(Counter(x["recommendation"] for x in rows)),
        "performance_hypothetical": {
            "baseline_all_eligible": _stats([x["trade_net"] for x in base], cfg.min_sample),
            "ai_assisted_consider": _stats([x["trade_net"] for x in ai], cfg.min_sample),
            "ai_vetoed_eligible": _stats([x["trade_net"] for x in vetoed], cfg.min_sample),
            "note": "hypothetical Phase 4 trade paths with Phase 3 costs, one per signal; "
                    "never actual profits",
        },
        "paper_actual": {"records_with_paper_fill": sum(1 for x in rows if x["paper_net"])},
        "groups": sum((groups(d) for d in ("strategy", "symbol", "regime", "setup",
                                           "recommendation", "ai_version", "week")), []),
        "ai_quality": {
            "records_with_unsupported_claims": len(claims_bad),
            "unsupported_rate_ci": list(wilson(len(claims_bad), len(rows), cfg.confidence)),
            "ai_unavailable": sum(1 for x in rows if x["ai_status"] != "OK"),
            "rule_noncompliant_candidates": sum(1 for x in rows if not x["rule_compliant"]),
        },
        "process_grades": dict(Counter(x["process_grade"] for x in reviewed)),
        "outcome_grades": dict(Counter(x["outcome_grade"] for x in reviewed)),
        "error_categories": recurring, "period_change": change,
        "risk_rejections": dict(Counter(x["risk_status"] for x in signals
                                        if x["risk_status"] not in (None,
                                                                    "QUALIFIED_FOR_REVIEW"))),
        "min_sample": cfg.min_sample,
    }
    out["findings"] = findings(out, signals, base, vetoed, cfg)
    return out


TEMPLATES = {
    "UNSUPPORTED_CLAIM": ("prompt", "evidence requirements in the research prompt",
                          "Requiring an evidence ID for every number and fact reduces "
                          "unsupported claims without lowering assessment coverage."),
    "DATA_QUALITY": ("market-data", "data-provider health and freshness handling",
                     "Data-quality failures come from the provider or freshness limits, not "
                     "from the strategy."),
    "STRATEGY_RULE_VIOLATION": ("agent-gating", "agent status gates and prompt rules",
                                "Clarifying the deterministic requirements in the prompt "
                                "reduces 'consider' on signals the gates must reject."),
    "RISK_SIZING_ERROR": ("risk-integration", "agent risk-tool portfolio state",
                          "The agent's risk view differs from the paper engine's; aligning "
                          "the state removes sizing discrepancies."),
    "WEAK_BREAKOUT": ("strategy", "breakout confirmation filter (e.g. relative-volume "
                                  "threshold) - a NEW strategy version and frozen baseline",
                      "A stricter confirmation filter removes breakouts that fail within "
                      "the first horizon without removing more net profit than it saves."),
    "EXECUTION_DEVIATION": ("execution-model", "paper execution assumptions (spread, "
                                               "slippage, gap rule)",
                            "Recorded fills deviate from the assumed spread and slippage; "
                            "the cost assumptions understate execution costs."),
    "COST_ESTIMATE_ERROR": ("cost-model", "pre-trade cost estimate",
                            "The pre-trade estimate differs from recorded costs at target "
                            "exits; the estimator misses a charge or rounding."),
}


def findings(a: dict, signals, base, vetoed, cfg) -> list[dict]:
    out = []
    for e in a["error_categories"]:
        if e["category"] not in TEMPLATES or e["count"] < cfg.min_recurring_count:
            continue
        comp, change, hyp = TEMPLATES[e["category"]]
        out.append({
            "finding_key": f"recurring:{e['category']}", "component": comp, "change": change,
            "issue": f"{e['category']} in {e['count']} of {e['of_reviewed']} reviewed records "
                     f"(rate CI {e['rate_ci']})",
            "hypothesis": hyp, "supporting_ids": e["supporting_ids"],
            "sample_size": e["of_reviewed"],
            "evidence_level": "ESTABLISHED_PATTERN" if e["recurring"] and e["of_reviewed"]
            >= cfg.min_sample else "HYPOTHESIS"})
    if len(vetoed) >= cfg.min_recurring_count:
        st = _stats([x["trade_net"] for x in vetoed], cfg.min_sample)
        nets = [x["trade_net"] for x in vetoed]
        se = float(np.std(nets, ddof=1) / math.sqrt(len(nets))) if len(nets) > 1 else None
        z = NormalDist().inv_cdf(1 - (1 - cfg.confidence) / 2)
        lo = st["net_expectancy"] - z * se if se else None
        if st["net_expectancy"] > 0:
            out.append({
                "finding_key": "ai:vetoes_positive_expectancy", "component": "prompt",
                "change": "AI review instructions for deterministically eligible setups",
                "issue": f"the AI vetoed {len(vetoed)} of {len(base)} eligible setups; their "
                         f"hypothetical net expectancy was Rs {st['net_expectancy']} per trade",
                "hypothesis": "The AI is more conservative than the evidence supports: a "
                              "revised prompt that vetoes fewer eligible setups would raise "
                              "net P&L after costs and model cost on a fresh period.",
                "supporting_ids": [x["record_id"] for x in vetoed][:20],
                "sample_size": len(vetoed),
                "evidence_level": "ESTABLISHED_PATTERN" if st["interpretable"] and lo
                and lo > 0 else "HYPOTHESIS"})
    rej = a["risk_rejections"]
    infeasible = rej.get("REJECTED_COSTS", 0) + rej.get("REJECTED_CAPITAL", 0)
    if infeasible >= cfg.min_recurring_count and infeasible >= 0.5 * max(1, len(signals)):
        out.append({
            "finding_key": "capital:cost_infeasible", "component": "capital-and-strategy",
            "change": "capital or setup-selection constraints (a human business decision; "
                      "risk controls are never relaxed to pass a test)",
            "issue": f"{infeasible} of {len(signals)} signals rejected because costs or "
                     "capital made them infeasible",
            "hypothesis": "At the configured capital, transaction costs exceed the expected "
                          "gross move of typical setups, so neither system can trade "
                          "profitably; this is a capital/strategy constraint, not an AI "
                          "issue.",
            "supporting_ids": [x["record_id"] for x in signals if x["risk_status"] in
                               ("REJECTED_COSTS", "REJECTED_CAPITAL")][:20],
            "sample_size": len(signals),
            "evidence_level": "ESTABLISHED_PATTERN" if len(signals) >= cfg.min_sample
            else "HYPOTHESIS"})
    return out


def proposals_from_findings(lstore: LearningStore, analysis: dict, now) -> list[str]:
    from app.agent.prompts import PROMPT_VERSION
    from app.backtesting.engine import STRATEGY_VERSION
    from app.learning import proposals as P

    created = []
    for f in analysis["findings"]:
        baseline = (f"prompt {PROMPT_VERSION}" if f["component"] in ("prompt", "agent-gating")
                    else f"strategy {STRATEGY_VERSION}")
        pid = P.create(lstore, {
            **{k: f[k] for k in ("finding_key", "issue", "hypothesis", "change", "component",
                                 "supporting_ids", "sample_size", "evidence_level")},
            "baseline_version": baseline,
            "candidate_version": f"{f['component']}-candidate (to be built as a new version)",
            "expected_benefit": "see hypothesis; to be measured, not assumed",
            "possible_downside": "fewer trades or new failure modes; may remove profitable "
                                 "setups; may raise model cost",
            "required_tests": ["build the candidate as a new, separately versioned component",
                               "Phase 11 replay on development and validation splits",
                               "prospective paper sessions on a fresh period (Phase 12)",
                               "full regression suite and release gate"],
            "acceptance_criteria": {"required_conclusions": ["EVIDENCE_OF_IMPROVEMENT"],
                                    "max_rule_violations": 0,
                                    "fresh_period_required": True},
            "limitations": ["based on the records listed; correlation, not causation",
                            f"sample size {f['sample_size']}",
                            "hypothetical trade paths, not actual fills"]}, now)
        if pid:
            created.append(pid)
    return created
