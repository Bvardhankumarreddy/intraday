"""AI decision quality, measured from the agent's immutable records (Phase 11).

Inputs: recommendation bodies (``agent_recommendations``), model-call and
tool-call audit rows, and run summaries. Nothing is re-asked of the model.
Failed and invalid recommendations are counted, never dropped.

Rule compliance (system level): a CANDIDATE must have screen PASS, signal
QUALIFIED_PRE_COST, risk engine QUALIFIED_FOR_REVIEW, fresh data and a whole
quantity >= 1. Any CANDIDATE that misses one counts as a violation (expected
0, enforced by the gates). AI level: how often the model's own judgement was
``consider`` on a candidate that the deterministic checks rejected or whose
data was stale (the gates override it, but it shows judgement quality).

Evidence quality uses the post-validation the workflow already stores:
- facts or calculations citing no evidence or invalid evidence IDs;
- numbers found in no tool output;
- probability or guarantee language;
- source references whose as-of time is after the decision time.
Contradicting evidence, missing information and the fact/interpretation
split are counted from the structured assessment.

Consistency: the same signal reviewed in several runs, and the model's
recommendation changing while every deterministic input stayed the same.
Similar inputs: the distribution of AI recommendations within groups that
share the same deterministic state.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime

import numpy as np


def _rate(k: int, n: int) -> float | None:
    return round(k / n, 4) if n else None


def _det_key(r: dict) -> tuple:
    return (r.get("screen_status"), r.get("signal_status"), r.get("risk_status"),
            bool(r.get("data_fresh")), r.get("signal_id") is not None)


def decision_quality(recs: list[dict], model_calls: list[dict], tool_calls: list[dict],
                     runs: list[dict]) -> dict:
    sym = [r for r in recs if r.get("symbol") and r.get("window") == "intraday"]
    assessed = [r for r in sym if r.get("ai_assessment")]

    # -- rule compliance ---------------------------------------------------------------
    cands = [r for r in sym if r["status"] == "CANDIDATE"]
    violations = [r["recommendation_id"] for r in cands if not (
        r.get("screen_status") == "PASS" and r.get("signal_status") == "QUALIFIED_PRE_COST"
        and r.get("risk_status") == "QUALIFIED_FOR_REVIEW" and r.get("data_fresh")
        and (r.get("quantity") or 0) >= 1)]
    consider = [r for r in assessed if r["ai_assessment"]["recommendation"] == "consider"]
    ai_consider_risk_rejected = [r for r in consider
                                 if r.get("risk_status") not in (None, "QUALIFIED_FOR_REVIEW")]
    ai_consider_stale = [r for r in consider if not r.get("data_fresh")]
    eligible = [r for r in sym if r.get("risk_status") == "QUALIFIED_FOR_REVIEW"
                and r.get("data_fresh") and r.get("signal_status") == "QUALIFIED_PRE_COST"]

    # -- evidence quality ------------------------------------------------------------
    claims = [c for r in assessed for c in (
        r["ai_assessment"]["observed_facts"] + r["ai_assessment"]["supporting_evidence"]
        + r["ai_assessment"]["contradicting_evidence"])]
    claims += [c for r in sym for c in r.get("ai_market_context", [])]
    basis = Counter(c["basis"] for c in claims)
    unsupported = [u for r in recs for u in r.get("unsupported_claims", [])]
    kinds = Counter("probability_language" if "probability" in u else
                    "number_not_in_any_tool_output" if "appears in no tool output" in u else
                    "missing_or_invalid_evidence_ids" if "cites" in u else "other"
                    for u in unsupported)
    late_refs = 0
    for r in recs:
        created = datetime.fromisoformat(r["created_at"])
        for s in r.get("data_sources", []):
            if s.get("as_of") and datetime.fromisoformat(s["as_of"]) > created:
                late_refs += 1
    n_a = len(assessed)

    # -- consistency ------------------------------------------------------------------
    by_signal: dict[str, list[dict]] = defaultdict(list)
    for r in sorted(assessed, key=lambda r: r["created_at"]):
        if r.get("signal_id"):
            by_signal[r["signal_id"]].append(r)
    changed_same_inputs = changed_new_info = 0
    for seq in by_signal.values():
        for prev, cur in zip(seq, seq[1:]):
            if prev["ai_assessment"]["recommendation"] != cur["ai_assessment"]["recommendation"]:
                if _det_key(prev) == _det_key(cur):
                    changed_same_inputs += 1
                else:
                    changed_new_info += 1
    groups: dict[tuple, Counter] = defaultdict(Counter)
    for r in assessed:
        groups[_det_key(r)][r["ai_assessment"]["recommendation"]] += 1
    similar = [{"screen": k[0], "signal": k[1], "risk": k[2], "data_fresh": k[3],
                "has_setup": k[4], "recommendations": dict(v), "assessments": sum(v.values()),
                "mixed": len(v) > 1} for k, v in sorted(groups.items(), key=str)]
    abstain = [r for r in assessed if r["ai_assessment"]["recommendation"] != "consider"]

    # -- operational ------------------------------------------------------------------
    lat = [m["latency_seconds"] for m in model_calls if m.get("latency_seconds") is not None]
    cost = [m["cost_usd"] for m in model_calls if m.get("cost_usd") is not None]
    notes = [n for run in runs for n in (json.loads(run["summary"]).get("notes", [])
                                         if run.get("summary") else [])]
    dup = Counter(r["signal_id"] for r in cands if r.get("signal_id"))
    news = [t for t in tool_calls if t["tool"] == "get_news"]

    return {
        "recommendations_total": len(recs),
        "symbol_recommendations": len(sym),
        "ai_status_counts": dict(Counter(r["ai_status"] for r in sym)),
        "status_counts": dict(Counter(r["status"] for r in recs)),
        "rule_compliance": {
            "candidates": len(cands),
            "candidate_rule_violations": len(violations),
            "violating_ids": violations[:20],
            "deterministically_eligible": len(eligible),
            "ai_consider_on_risk_rejected": len(ai_consider_risk_rejected),
            "ai_consider_on_stale_data": len(ai_consider_stale),
            "note": "violations are system-level (expected 0); the two ai_consider_* counts "
                    "are the model's own judgement, which the deterministic gates overrode",
        },
        "evidence_quality": {
            "assessments": n_a,
            "claims": len(claims), "claims_by_basis": dict(basis),
            "unsupported_claims": len(unsupported),
            "unsupported_by_kind": dict(kinds),
            "assessments_with_contradicting_evidence": sum(
                1 for r in assessed if r["ai_assessment"]["contradicting_evidence"]),
            "rate_with_contradicting_evidence": _rate(sum(
                1 for r in assessed if r["ai_assessment"]["contradicting_evidence"]), n_a),
            "rate_disclosing_missing_information": _rate(sum(
                1 for r in assessed if r["ai_assessment"]["missing_information"]), n_a),
            "evidence_strength": dict(Counter(r["ai_assessment"]["evidence_strength"]
                                              for r in assessed)),
            "source_refs_after_decision_time": late_refs,
            "note": "facts and calculations must cite tool-call IDs; numbers must appear in a "
                    "tool output (integers up to 10 are not checked); probability language is "
                    "not allowed",
        },
        "consistency": {
            "signals_reviewed_more_than_once": sum(1 for s in by_signal.values() if len(s) > 1),
            "changed_without_new_deterministic_information": changed_same_inputs,
            "changed_with_new_deterministic_information": changed_new_info,
            "similar_input_groups": similar,
            "mixed_groups": sum(1 for g in similar if g["mixed"]),
            "invented_probabilities": kinds.get("probability_language", 0),
            "abstentions_with_reason": sum(1 for r in abstain
                                           if r["ai_assessment"]["rationale"].strip()),
            "abstentions": len(abstain),
        },
        "operational": {
            "research_runs": len(runs),
            "runs_by_status": dict(Counter(r["status"] for r in runs)),
            "model_requests": len(model_calls),
            "model_errors": dict(Counter(m["error_kind"] for m in model_calls
                                         if m.get("error_kind"))),
            "invalid_structured_outputs": sum(1 for r in sym
                                              if r["ai_status"] == "INVALID_OUTPUT"),
            "truncated_outputs_rejected": sum(1 for n in notes if "truncated" in n),
            "schema_validation_failures": sum(1 for n in notes if "schema validation" in n),
            "tool_calls": len(tool_calls),
            "tool_errors": sum(1 for t in tool_calls if not t["ok"]),
            "latency_mean_seconds": round(float(np.mean(lat)), 3) if lat else None,
            "latency_p95_seconds": round(float(np.quantile(lat, 0.95)), 3) if lat else None,
            "input_tokens": sum(m.get("input_tokens") or 0 for m in model_calls),
            "output_tokens": sum(m.get("output_tokens") or 0 for m in model_calls),
            "cost_usd": round(sum(cost), 6) if cost else None,
            "cost_usd_note": None if cost else "model prices not configured "
                                               "(llm.*_cost_per_mtok); not guessed",
            "duplicate_candidates_same_signal": sum(1 for v in dup.values() if v > 1),
            "unavailable_evidence": {"news_calls": len(news),
                                     "news_unavailable": sum(
                                         1 for t in news if t["ok"] and json.loads(
                                             t["output"]).get("result", {}).get("status")
                                         == "UNAVAILABLE")},
        },
    }


def load_agent_records(store, market_dates: list[str] | None = None) -> tuple:
    """(recs, model_calls, tool_calls, runs) from an agent store's tables."""
    where, params = "", ()
    if market_dates is not None:
        if not market_dates:
            return [], [], [], []
        marks = ", ".join("?" for _ in market_dates)
        where, params = f" WHERE market_date IN ({marks})", tuple(market_dates)
    try:
        recs = [json.loads(r["body"]) for r in store.all(
            f"SELECT body FROM agent_recommendations{where} ORDER BY created_at", params)]
        runs = [dict(r) for r in store.all(f"SELECT * FROM agent_runs{where}", params)]
    except Exception:  # noqa: BLE001 - no agent tables yet: nothing recorded
        return [], [], [], []
    ids = tuple(r["run_id"] for r in runs)
    if not ids:
        return recs, [], [], runs
    marks = ", ".join("?" for _ in ids)
    models = [dict(r) for r in store.all(
        f"SELECT * FROM agent_model_calls WHERE run_id IN ({marks})", ids)]
    tools = [dict(r) for r in store.all(
        f"SELECT run_id, call_id, tool, ok, output, error FROM agent_tool_calls WHERE run_id "
        f"IN ({marks})", ids)]
    return recs, models, tools, runs
