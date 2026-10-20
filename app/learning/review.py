"""Structured post-trade analysis (Phase 13), rules ``review-rules-v1``.

The decision PROCESS is judged from decision-time evidence only. The OUTCOME
is graded separately. A losing trade is not a bad decision by itself, and a
winning trade does not prove the decision was sound.

Process categories (any number; none = SOUND):

| Category | Evidence (decision time) |
|---|---|
| DATA_QUALITY | the recommendation's data were not fresh |
| UNSUPPORTED_CLAIM | the model cited missing or invalid evidence, used a number found in no tool output, or used probability language |
| STRATEGY_RULE_VIOLATION | a CANDIDATE that missed a deterministic requirement, or the AI said "consider" on a risk-rejected or stale signal |
| RISK_SIZING_ERROR | the paper engine sized the same signal differently from the recommendation |

Outcome-informed categories (one; they describe what happened, not blame):

| Category | Evidence (after the decision) |
|---|---|
| WEAK_BREAKOUT | a setup whose stop was hit within the first horizon, or whose price closed back below the entry reference by the first horizon |
| EXECUTION_DEVIATION | the paper fill differed from the reference by more than half the assumed spread plus slippage plus ``learning.execution_tolerance_pct``, or the entry order was not filled |
| COST_ESTIMATE_ERROR | at a target exit, actual round-trip costs differed from the estimate by more than ``learning.cost_tolerance_inr`` |
| NORMAL_UNCERTAINTY | a sound process with an adverse outcome and no identified failure |
| INSUFFICIENT_EVIDENCE | outcomes are not available yet |
| NONE | none of the above |

INCORRECT_INTERPRETATION (a verified fact that was misread) cannot be detected
mechanically. A person records it with ``annotate`` as a human review, which
is stored alongside the rule-based one and never replaces it.

Outcome grade:
- CONSIDER: WIN / LOSS / FLAT from the trade path's net result (``_PER_SHARE``,
  gross, when the recorded quantity is 0, e.g. a later re-review);
- WAIT or NO_TRADE: AVOIDED_LOSS / MISSED_GAIN / NEUTRAL from the
  hypothetical trade path;
- NO_SETUP, or PENDING when outcomes are not available yet.
"""

from __future__ import annotations

from datetime import datetime

from app.learning.dataset import LearningStore, canonical

RULES_VERSION = "review-rules-v1"
PROCESS = ("DATA_QUALITY", "UNSUPPORTED_CLAIM", "STRATEGY_RULE_VIOLATION", "RISK_SIZING_ERROR")
OUTCOME = ("WEAK_BREAKOUT", "EXECUTION_DEVIATION", "COST_ESTIMATE_ERROR",
           "NORMAL_UNCERTAINTY", "INSUFFICIENT_EVIDENCE", "NONE")
HUMAN = PROCESS + OUTCOME + ("INCORRECT_INTERPRETATION",)


def classify(body: dict, outcomes: dict, settings) -> dict:
    f, ev = body["features"], {}
    process = []
    if not body["data_fresh"]:
        process.append("DATA_QUALITY")
        ev["DATA_QUALITY"] = "data_fresh false at decision"
    if body["unsupported_claims"]:
        process.append("UNSUPPORTED_CLAIM")
        ev["UNSUPPORTED_CLAIM"] = body["unsupported_claims"][:5]
    ai_consider_bad = body["recommendation"] == "CONSIDER" and (
        f.get("risk_status") not in (None, "QUALIFIED_FOR_REVIEW") or not body["data_fresh"])
    if not body["rule_compliant"] or ai_consider_bad:
        process.append("STRATEGY_RULE_VIOLATION")
        ev["STRATEGY_RULE_VIOLATION"] = ("system CANDIDATE missed a requirement"
                                         if not body["rule_compliant"] else
                                         f"AI consider on risk status {f.get('risk_status')} / "
                                         f"fresh {body['data_fresh']} (overridden by gates)")
    paper = outcomes.get("PAPER_EXECUTION") or {}
    # Only the CANDIDATE record that led to the order is compared (later reviews of
    # the same signal see it as already taken and carry quantity 0).
    if body["system_status"] == "CANDIDATE" and paper.get("status") == "ORDERED" \
            and f.get("quantity") is not None and paper.get("signal_quantity") is not None \
            and paper["signal_quantity"] != f["quantity"]:
        process.append("RISK_SIZING_ERROR")
        ev["RISK_SIZING_ERROR"] = (f"recommendation qty {f['quantity']}, paper qty "
                                   f"{paper['signal_quantity']}")
    if body["ai_status"] != "OK":
        grade = "UNKNOWN"
    else:
        grade = "FLAWED" if process else "SOUND"

    trade = outcomes.get("TRADE_PATH") or {}
    first = next((outcomes[k] for k in sorted(outcomes) if k.startswith("H")), None)
    outcome_grade, net = "PENDING", None
    if not f.get("has_setup"):
        outcome_grade = "NO_SETUP" if outcomes else "PENDING"
    elif trade.get("status") == "SIMULATED" and trade.get("net_pnl") is not None:
        net = float(trade["net_pnl"])
        if body["recommendation"] == "CONSIDER":
            outcome_grade = "WIN" if net > 0 else "LOSS" if net < 0 else "FLAT"
        else:
            outcome_grade = "AVOIDED_LOSS" if net < 0 else "MISSED_GAIN" if net > 0 else \
                "NEUTRAL"
    elif trade.get("status") == "SIMULATED":  # no whole share: graded per share, gross
        g = float(trade["gross_per_share"])
        if body["recommendation"] == "CONSIDER":
            outcome_grade = ("WIN" if g > 0 else "LOSS" if g < 0 else "FLAT") + "_PER_SHARE"
        else:
            outcome_grade = ("AVOIDED_LOSS" if g < 0 else "MISSED_GAIN" if g > 0
                             else "NEUTRAL") + "_PER_SHARE"
    elif trade:
        outcome_grade = f"NOT_EXECUTABLE:{trade.get('status')}"

    category = "NONE"
    # Expected adverse fill: half the assumed spread plus slippage (paper model).
    exec_tol = settings.execution.assumed_spread_pct / 2 + \
        settings.execution.slippage_pct_per_side + settings.learning.execution_tolerance_pct
    pos = paper.get("position") or {}
    if not outcomes:
        category = "INSUFFICIENT_EVIDENCE"
    elif f.get("has_setup") and first and first.get("status") == "OK" and (
            first["mae_pct"] <= (float(f["stop"]) / float(f["entry_reference"]) - 1) * 100
            or first["return_pct"] < 0):
        category = "WEAK_BREAKOUT"
        ev[category] = {k: first.get(k) for k in ("return_pct", "mae_pct", "candles")}
    elif paper.get("status") == "ORDERED" and (
            (paper.get("order") or {}).get("state") in ("REJECTED", "EXPIRED", "CANCELLED")
            or abs(paper.get("fill_deviation_pct") or 0) > exec_tol):
        category = "EXECUTION_DEVIATION"
        ev[category] = {"order": paper.get("order"),
                        "fill_deviation_pct": paper.get("fill_deviation_pct"),
                        "tolerance_pct": exec_tol}
    elif pos.get("exit_reason") == "TARGET" and f.get("estimated_costs_at_target") and \
            pos.get("total_costs") and abs(float(pos["total_costs"]) - float(
                f["estimated_costs_at_target"])) > settings.learning.cost_tolerance_inr:
        category = "COST_ESTIMATE_ERROR"
        ev[category] = {"estimated": f["estimated_costs_at_target"],
                        "actual": pos["total_costs"]}
    elif grade == "SOUND" and outcome_grade.split("_PER_SHARE")[0] in ("LOSS",
                                                                       "MISSED_GAIN"):
        category = "NORMAL_UNCERTAINTY"
    if outcome_grade == "PENDING" and category == "NONE":
        category = "INSUFFICIENT_EVIDENCE"
    return {"process_grade": grade, "process_categories": process,
            "outcome_grade": outcome_grade, "outcome_category": category,
            "trade_path_net": net, "evidence": ev}


def review_all(lstore: LearningStore, settings, now: datetime) -> dict:
    """Append a rules review for every record whose outcomes are complete (or
    whose session ended). A record is reviewed once per rules version."""
    outs = lstore.outcomes()
    reviewed = {r["record_id"] for r in lstore.all(
        "SELECT record_id FROM learn_reviews WHERE rules_version = ?", (RULES_VERSION,))}
    added = waiting = 0
    for r in lstore.records():
        if r["record_id"] in reviewed:
            continue
        o = outs.get(r["record_id"], {})
        if "EOD" not in o:  # the day's outcomes are not complete yet
            waiting += 1
            continue
        c = classify(r["body"], o, settings)
        with lstore.store.transaction():
            lstore.store._insert("learn_reviews", {
                "record_id": r["record_id"], "rules_version": RULES_VERSION,
                "process_grade": c["process_grade"],
                "process_categories": canonical(c["process_categories"]),
                "outcome_grade": c["outcome_grade"],
                "outcome_category": c["outcome_category"],
                "evidence": canonical({**c["evidence"], "trade_path_net": c["trade_path_net"]}),
                "ts": now}, or_ignore=True)
        added += 1
    return {"reviewed": added, "waiting_for_outcomes": waiting}


def annotate(lstore: LearningStore, record_id: str, category: str, reason: str, actor: str,
             now: datetime) -> None:
    """A person's review (e.g. INCORRECT_INTERPRETATION). Stored next to, never
    instead of, the rules review."""
    if category not in HUMAN:
        raise ValueError(f"unknown category {category!r}; one of {', '.join(HUMAN)}")
    if not reason.strip() or not actor.strip():
        raise ValueError("an annotation needs a reason and the reviewer's name")
    if lstore.one("SELECT 1 FROM learn_records WHERE record_id = ?", (record_id,)) is None:
        raise KeyError(f"no learning record {record_id}")
    is_process = category in PROCESS or category == "INCORRECT_INTERPRETATION"
    with lstore.store.transaction():
        lstore.store._insert("learn_reviews", {
            "record_id": record_id, "rules_version": f"human:{actor}:{now.isoformat()}",
            "process_grade": "FLAWED" if is_process else "UNKNOWN",
            "process_categories": canonical([category] if is_process else []),
            "outcome_grade": "HUMAN_REVIEW",
            "outcome_category": category if not is_process else "NONE",
            "evidence": canonical({"reason": reason}), "ts": now})


def latest_reviews(lstore: LearningStore) -> dict[str, dict]:
    out = {}
    for r in lstore.all("SELECT * FROM learn_reviews WHERE rules_version = ? ORDER BY id",
                        (RULES_VERSION,)):
        out[r["record_id"]] = r
    return out
