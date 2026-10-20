"""Final evidence-based assessment, by the rules predeclared in the protocol
(``acceptance``). The rules are applied mechanically; nothing here changes a
parameter.

Criteria (each PASS, FAIL or NOT_EVALUABLE):
  C1 executable    - out-of-sample trades exist under the frozen capital and
                     risk rules (FAIL if signals exist but none could be taken)
  C2 ci_above_zero - bootstrap CI lower bound of net expectancy > 0
  C3 fold_consistency - share of folds (with trades) whose test window has
                     positive net expectancy >= min_positive_fold_fraction
  C4 conservative_costs - net expectancy under the conservative scenario > 0

Verdict:
  PENDING_DATA                 no usable data or too few sessions for one fold
  FAILED_PREDEFINED_CRITERIA   C1 FAIL, or the CI's upper bound < 0
  SUPPORTS_FURTHER_EVALUATION  C1-C4 all PASS (further evaluation, never a
                               claim of future profit)
  INCONCLUSIVE                 anything else
"""

from __future__ import annotations

from app.robustness.protocol import Protocol

VERDICTS = {
    "PENDING_DATA": "Empirical conclusions are pending: the required historical data is not "
                    "available.",
    "FAILED_PREDEFINED_CRITERIA": "The tested strategy failed the predefined criteria.",
    "SUPPORTS_FURTHER_EVALUATION": "The evidence supports further evaluation (forward paper "
                                   "trading). It does not show that the strategy will be "
                                   "profitable.",
    "INCONCLUSIVE": "The evidence is inconclusive.",
}


def pending(reason: str) -> dict:
    return {"verdict": "PENDING_DATA", "statement": VERDICTS["PENDING_DATA"],
            "reasons": [reason], "criteria": []}


def assess(protocol: Protocol, oos: dict, boot: dict, folds: list[dict],
           scenarios: dict, diagnostics: dict) -> dict:
    a = protocol.acceptance
    crit = []
    trades, signals = oos["trades"], diagnostics["signals_total"]
    if trades > 0:
        crit.append(("C1 executable", "PASS", f"{trades} out-of-sample trades"))
    elif signals > 0:
        crit.append(("C1 executable", "FAIL",
                     f"{signals} technical signal(s) out of sample, none executable under the "
                     f"frozen capital/risk rules (rejections: "
                     f"{diagnostics['risk_rejected_by_status'] or 'none'})"))
    else:
        crit.append(("C1 executable", "NOT_EVALUABLE", "no technical signals out of sample"))

    if boot.get("status") == "OK":
        ok = boot["ci_low"] > 0
        crit.append(("C2 ci_above_zero", "PASS" if ok else "FAIL",
                     f"{boot['confidence']:.0%} CI [{boot['ci_low']}, {boot['ci_high']}] Rs per "
                     "trade"))
    else:
        crit.append(("C2 ci_above_zero", "NOT_EVALUABLE", boot.get("reason", "no interval")))

    with_trades = [f for f in folds if f["trades"]]
    if with_trades:
        pos = sum(1 for f in with_trades if (f["net_expectancy"] or 0) > 0)
        share = pos / len(with_trades)
        crit.append(("C3 fold_consistency",
                     "PASS" if share >= a.min_positive_fold_fraction else "FAIL",
                     f"{pos} of {len(with_trades)} folds with trades positive ({share:.0%}; "
                     f"need {a.min_positive_fold_fraction:.0%})"))
    else:
        crit.append(("C3 fold_consistency", "NOT_EVALUABLE", "no fold has trades"))

    cons = scenarios.get(a.conservative_scenario)
    if cons and cons.get("net_expectancy") is not None:
        crit.append(("C4 conservative_costs", "PASS" if cons["net_expectancy"] > 0 else "FAIL",
                     f"net expectancy Rs {cons['net_expectancy']} per trade under "
                     f"'{a.conservative_scenario}'"))
    else:
        crit.append(("C4 conservative_costs", "NOT_EVALUABLE",
                     f"no trades under '{a.conservative_scenario}'"))

    status = {name.split()[0]: s for name, s, _ in crit}
    reasons = []
    if status["C1"] == "FAIL":
        verdict = "FAILED_PREDEFINED_CRITERIA"
        reasons.append("signals occurred but none was executable at the frozen capital and "
                       "risk settings")
    elif boot.get("status") == "OK" and boot["ci_high"] < 0:
        verdict = "FAILED_PREDEFINED_CRITERIA"
        reasons.append("the whole confidence interval of net expectancy is below zero")
    elif all(s == "PASS" for s in status.values()):
        verdict = "SUPPORTS_FURTHER_EVALUATION"
        reasons.append("every predefined criterion passed")
    else:
        verdict = "INCONCLUSIVE"
        reasons += [f"{n}: {s} ({d})" for n, s, d in crit if s != "PASS"]
    return {"verdict": verdict, "statement": VERDICTS[verdict], "reasons": reasons,
            "criteria": [{"criterion": n, "status": s, "detail": d} for n, s, d in crit]}
