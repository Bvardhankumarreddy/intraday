"""Leakage-resistant validation of a candidate experiment (Phase 14).

Data and splits:
- The frozen baseline's historical settings and the Phase 7 dataset loader
  (benchmark separation, corporate-action policy, point-in-time membership,
  data audit) are used.
- After the indicator warm-up, sessions are split chronologically into
  development, validation and final sets, with ``embargo_sessions`` dropped
  between them. Nothing is shuffled. Each window is simulated from fresh
  capital, with earlier sessions visible only as indicator history.
- Every AI decision is made at its signal time with point-in-time tools
  (Phase 11 replay). Nothing is fitted, so no preprocessing can learn from a
  later period.

Arms, all on identical sessions, data, fills, costs and risk engine:
- **A**: the deterministic baseline (frozen settings);
- **APPROVED**: the currently active AI system version;
- **CANDIDATE**: the candidate version (its prompt, model and tools, and any
  research settings).

The acceptance rules (``config/experiments/acceptance.yaml``, version
recorded at experiment creation) are applied to the VALIDATION set:
minimum evidence; a paired-bootstrap CI of the per-session difference vs
APPROVED above zero, at a confidence Bonferroni-adjusted for every
experiment validated on an overlapping period; drawdown; fold consistency;
doubled spread and slippage with AI decisions held fixed; removal of the
best sessions; not worse than A; and AI quality. If validation passes, the
FINAL set is run exactly once and must pass the same rules.

Classification: INVALID (leakage, irreproducible, weakened controls, changed
code, baseline or acceptance rules, changed dataset, final set reuse) >
FAIL (a mandatory criterion failed) > INCONCLUSIVE (insufficient evidence)
> PASS.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import yaml

from app.aieval import metrics as M
from app.experiments.candidate import (agent_kwargs, apply_components, current_components,
                                       weakened_controls)
from app.experiments.registry import Registry, RegistryError, canonical


def load_acceptance(path: Path) -> tuple[dict, str, str]:
    text = Path(path).read_text()
    return yaml.safe_load(text), text, hashlib.sha256(text.encode()).hexdigest()


def code_versions() -> dict:
    from app.aieval.protocol import agent_code_sha, tool_schema_sha
    from app.robustness.baseline import code_hash

    return {"decision_code_hash": code_hash()[0], "agent_code_sha256": agent_code_sha(),
            "tool_schema_sha256": tool_schema_sha()}


class CachedDecider:
    """Records a decider's decisions by signal ID; ``frozen()`` replays them
    without any model call (cost-sensitivity reruns, reproducibility)."""

    def __init__(self, inner=None, decisions: dict | None = None):
        self.inner, self.decisions = inner, dict(decisions or {})
        self.store = getattr(inner, "store", None)
        self.runs = getattr(inner, "runs", 0)
        self.failures = getattr(inner, "failures", [])

    def decide(self, cand, now):
        sid = cand.setup.signal_id
        if sid not in self.decisions:
            if self.inner is None:
                from app.aieval.replay import AIDecision

                return AIDecision(False, "no recorded decision for this signal (replay)")
            self.decisions[sid] = self.inner.decide(cand, now)
            self.runs = getattr(self.inner, "runs", 0)
        return self.decisions[sid]

    def frozen(self) -> CachedDecider:
        return CachedDecider(None, self.decisions)


def splits(dataset, settings, calendar, acc: dict) -> dict:
    from app.robustness.walk_forward import warmup_length

    sp = acc["splits"]
    warm, why = warmup_length(dataset, settings, calendar)
    sessions = [d for d in dataset.sessions() if calendar.is_regular_session(d)][warm:]
    n, e = len(sessions), sp["embargo_sessions"]
    k_dev, k_val = int(n * sp["development_fraction"]), int(n * sp["validation_fraction"])
    dev = sessions[:k_dev]
    val = sessions[k_dev + e:k_dev + k_val]
    final = sessions[k_dev + k_val + e:]
    folds = [list(x) for x in _chunks(val, sp["validation_folds"])] if val else []
    return {"development": dev, "validation": val, "final": final, "folds": folds,
            "warmup": warm, "warmup_reason": why, "embargo_sessions": e,
            "embargoed": [d for d in sessions if d not in dev + val + final]}


def _chunks(seq, k):
    n = len(seq)
    bounds = [round(i * n / k) for i in range(k + 1)]
    return [seq[bounds[i]:bounds[i + 1]] for i in range(k) if bounds[i] < bounds[i + 1]]


def _trades(result):
    from app.aieval.replay import trade_dict

    return [trade_dict(t) for t in result.trades]


def _arm(dataset, settings, calendar, start, end, membership, decider):
    from app.aieval.replay import run_arm

    res, log = run_arm(dataset, settings, calendar, start, end, membership, decider)
    trades = _trades(res)
    return {"result": res, "log": log, "trades": trades,
            "hash": hashlib.sha256(canonical([(t["signal_id"], t["net"]) for t in trades])
                                   .encode()).hexdigest()}


def _quality(decider) -> dict:
    from app.aieval.quality import decision_quality, load_agent_records

    if decider is None or decider.store is None:
        return {}
    records = load_agent_records(decider.store)
    q = decision_quality(*records)
    n = q["symbol_recommendations"]
    unsupported = sum(1 for r in records[0] if r.get("symbol") and r.get("unsupported_claims"))
    usd = q["operational"]["cost_usd"]
    return {"recommendations": n,
            "unsupported_claim_rate": round(unsupported / n, 4) if n else None,
            "invalid_output_rate": round(q["operational"]["invalid_structured_outputs"] / n, 4)
            if n else None,
            "rule_violations": q["rule_compliance"]["candidate_rule_violations"],
            "cost_usd": usd, "cost_per_recommendation_usd": round(usd / n, 6)
            if usd is not None and n else None,
            "model_requests": q["operational"]["model_requests"],
            "model_errors": q["operational"]["model_errors"],
            "latency_mean_seconds": q["operational"]["latency_mean_seconds"],
            "tokens": [q["operational"]["input_tokens"], q["operational"]["output_tokens"]]}


def evaluate_window(sessions, dataset, hist, calendar, membership, approved_comp, cand_comp,
                    deciders: dict, acc: dict, adj_confidence: float) -> dict:
    """Run the three arms on one window and evaluate every acceptance criterion."""
    start, end = sessions[0], sessions[-1]
    s_app = apply_components(hist, approved_comp)
    s_cand = apply_components(hist, cand_comp)
    arms = {"A": _arm(dataset, hist, calendar, start, end, membership, None),
            "APPROVED": _arm(dataset, s_app, calendar, start, end, membership,
                             deciders["APPROVED"]),
            "CANDIDATE": _arm(dataset, s_cand, calendar, start, end, membership,
                              deciders["CANDIDATE"])}
    initial = Decimal(str(hist.capital.initial_capital))
    metrics = {k: M.arm_metrics(v["trades"], sessions, initial,
                                [(p.time.isoformat(), p.equity) for p in v["result"].equity])
               for k, v in arms.items()}
    tables = {k: M.session_table(v["trades"], sessions) for k, v in arms.items()}
    st, fin = acc["statistics"], acc["financial"]
    boot = M.paired_bootstrap(tables["APPROVED"], tables["CANDIDATE"], sessions,
                              st["resamples"], adj_confidence, st["seed"], None)
    diffs = {d: float(tables["CANDIDATE"][d][0] - tables["APPROVED"][d][0]) for d in sessions}

    # fold consistency (consecutive chunks of this window)
    folds = _chunks(sessions, acc["splits"]["validation_folds"])
    fold_rows = [{"fold": i, "start": f[0], "end": f[-1],
                  "difference": round(sum(diffs[d] for d in f), 2)} for i, f in enumerate(folds)]
    consistency = sum(1 for f in fold_rows if f["difference"] >= 0) / len(fold_rows) \
        if fold_rows else None

    # cost sensitivity: AI decisions held fixed, only costs change
    cc = fin["conservative_costs"]

    def conservative(s):
        ex = s.execution.model_copy(update={
            "assumed_spread_pct": s.execution.assumed_spread_pct * cc["spread_multiplier"],
            "slippage_pct_per_side": s.execution.slippage_pct_per_side
            * cc["slippage_multiplier"]})
        return s.model_copy(update={"execution": ex})

    cons = {k: _arm(dataset, conservative(s), calendar, start, end, membership,
                    deciders[k].frozen() if k != "A" else None)
            for k, s in (("APPROVED", s_app), ("CANDIDATE", s_cand))}
    cons_diff = float(sum((t["net"] for t in cons["CANDIDATE"]["trades"]), Decimal(0))
                      - sum((t["net"] for t in cons["APPROVED"]["trades"]), Decimal(0)))
    k_top = fin["concentration_top_sessions_removed"]
    best = sorted(diffs, key=diffs.get, reverse=True)[:k_top]
    without_best = round(sum(v for d, v in diffs.items() if d not in best), 2)

    q_app, q_cand = _quality(deciders["APPROVED"]), _quality(deciders["CANDIDATE"])
    me, aq = acc["minimum_evidence"], acc["ai_quality"]
    c, a = metrics["CANDIDATE"], metrics["APPROVED"]

    def crit(name, mandatory, ok, evidence, insufficient=False):
        return {"criterion": name, "mandatory": mandatory,
                "status": "INSUFFICIENT" if insufficient else ("PASS" if ok else "FAIL"),
                "evidence": evidence}

    enough = (len(sessions) >= me["sessions"] and c["closed_trades"] >=
              me["closed_trades_candidate"] and a["closed_trades"] >= me["closed_trades_approved"])
    lo = boot.get("ci_low")
    criteria = [
        crit("minimum_evidence", True, enough,
             {"sessions": len(sessions), "candidate_trades": c["closed_trades"],
              "approved_trades": a["closed_trades"], "required": me}, not enough),
        crit("net_difference_ci_above_zero", True, lo is not None and lo > 0,
             {"ci": [lo, boot.get("ci_high")], "confidence_adjusted": adj_confidence,
              "mean_session_difference": boot.get("mean_session_difference")},
             lo is None),
        crit("drawdown_not_worse", True, c["max_drawdown_inr"] - a["max_drawdown_inr"]
             <= fin["max_drawdown_worsening_inr"],
             {"candidate": c["max_drawdown_inr"], "approved": a["max_drawdown_inr"],
              "limit": fin["max_drawdown_worsening_inr"]}),
        crit("fold_consistency", True, consistency is not None and consistency
             >= fin["min_fold_consistency"], {"folds": fold_rows, "share": consistency},
             consistency is None),
        crit("survives_conservative_costs", True, cons_diff > 0,
             {"difference_inr": round(cons_diff, 2), "scenario": cc,
              "note": "AI decisions held fixed; only costs changed"}),
        crit("not_carried_by_best_sessions", True, without_best > 0,
             {"removed_sessions": best, "difference_without_them": without_best}),
        crit("not_worse_than_deterministic", fin["require_not_worse_than_deterministic"],
             (c["net_pnl"] or 0) >= (metrics["A"]["net_pnl"] or 0),
             {"candidate": c["net_pnl"], "deterministic": metrics["A"]["net_pnl"]}),
        crit("no_rule_violations", True, (q_cand.get("rule_violations") or 0)
             <= aq["max_rule_violations"], {"candidate": q_cand.get("rule_violations")}),
        crit("unsupported_claims_not_higher", True,
             q_cand.get("unsupported_claim_rate") is None or (
                 (q_cand["unsupported_claim_rate"] - (q_app.get("unsupported_claim_rate") or 0))
                 <= aq["max_unsupported_claim_rate_increase"]),
             {"candidate": q_cand.get("unsupported_claim_rate"),
              "approved": q_app.get("unsupported_claim_rate")}),
        crit("invalid_output_rate", True, (q_cand.get("invalid_output_rate") or 0)
             <= aq["max_invalid_output_rate"], {"candidate": q_cand.get("invalid_output_rate")}),
    ]
    if aq.get("max_api_cost_per_recommendation_usd") is not None:
        cost = q_cand.get("cost_per_recommendation_usd")
        criteria.append(crit("api_cost_per_recommendation", True, cost is not None and cost
                             <= aq["max_api_cost_per_recommendation_usd"], {"usd": cost},
                             cost is None))
    return {"period": [start, end], "sessions": len(sessions), "metrics": metrics,
            "statistics": boot, "folds": fold_rows,
            "conservative_costs_difference": round(cons_diff, 2),
            "difference_without_best_sessions": without_best,
            "quality": {"APPROVED": q_app, "CANDIDATE": q_cand},
            "decomposition": M.decompose(arms["APPROVED"]["trades"], arms["CANDIDATE"]["trades"]),
            "per_session": [{"session": d, **{k: M._f(tables[k][d][0]) for k in tables}}
                            for d in sessions],
            "arm_hashes": {k: v["hash"] for k, v in arms.items()},
            "criteria": criteria, "_arms": arms}


def classify(criteria: list[dict], invalid: list[str]) -> str:
    """INVALID > INCONCLUSIVE when the minimum evidence is not met (the other
    criteria cannot be judged on too small a sample) > FAIL > INCONCLUSIVE > PASS."""
    if invalid:
        return "INVALID"
    mand = [c for c in criteria if c["mandatory"]]
    if any(c["criterion"] == "minimum_evidence" and c["status"] != "PASS" for c in mand):
        return "INCONCLUSIVE"
    if any(c["status"] == "FAIL" for c in mand):
        return "FAIL"
    if any(c["status"] == "INSUFFICIENT" for c in mand):
        return "INCONCLUSIVE"
    return "PASS"


def _overlapping_validations(reg: Registry, eid: str, period: list) -> int:
    n = 0
    for e in reg.all("SELECT subject, evidence FROM registry_events WHERE kind = 'VALIDATION_RESULT'"):
        ev = json.loads(e["evidence"])
        p = ev.get("period")
        if e["subject"] != eid and p and not (p[1] < str(period[0]) or str(period[1]) < p[0]):
            n += 1
    return n


def validate(reg: Registry, eid: str, live, data_dir: Path, *, final: bool = False,
             llm_factory=None, now: datetime | None = None) -> dict:
    from app.agent.llm import build_llm
    from app.aieval.protocol import load_protocol
    from app.aieval.replay import AgentDecider
    from app.robustness.baseline import load_frozen
    from app.robustness.data_audit import membership_fn
    from app.robustness.runner import RunOptions, _missing_data, load_data
    from app.utils.fs import private_dir
    from app.utils.market_calendar import MarketCalendar

    reg.require_valid()
    x = reg.experiment(eid)
    want = "VALIDATING" if final else "DRAFT"
    if x["status"] != want:
        raise RegistryError(f"{eid} is {x['status']}; "
                            + ("the final set runs once, after a passing validation"
                               if final else "only a DRAFT experiment can be validated"))
    if final and not any(e["kind"] == "VALIDATION_RESULT" and
                         e["evidence"].get("classification") == "PASS" and
                         e["evidence"].get("split") == "validation" for e in x["events"]):
        raise RegistryError(f"{eid}: the final set needs a passing validation first")
    acc = yaml.safe_load(x["acceptance_text"])
    protocol, _, _ = load_protocol(live.ai_eval.protocol_file)
    frozen = load_frozen(protocol.baseline_path())
    hist = frozen.historical_settings()
    calendar = MarketCalendar(hist.market)
    now = (now or datetime.now(calendar.tz)).astimezone(calendar.tz)
    approved = reg.version(x["baseline_version"])["components"]
    cand = reg.version(x["candidate_version"])["components"]

    invalid = []
    if frozen.code_drift:
        invalid.append("decision-path code differs from the frozen baseline")
    if code_versions() != x["code_versions"]:
        invalid.append(f"code changed since the experiment was created: {code_versions()} vs "
                       f"{x['code_versions']}")
    if reg.active_version(x["context"]) != x["baseline_version"]:
        invalid.append("the approved version changed since the experiment was created; "
                       "create a new experiment against the current version")
    weak = weakened_controls(apply_components(hist, approved), apply_components(hist, cand))
    invalid += [f"weakens a mandatory control: {w}" for w in weak]

    missing = _missing_data(data_dir)
    if missing:
        raise RegistryError("no usable data: " + "; ".join(missing))
    dataset, bench, rows, audit, limitations = load_data(
        RunOptions(data_dir=Path(data_dir)), hist, calendar,
        frozen.protocol.regimes.benchmark_symbol)
    plan = splits(dataset, hist, calendar, acc)
    split = "final" if final else "validation"
    sessions = plan[split]
    if not sessions:
        raise RegistryError(f"the {split} set is empty ({len(dataset.sessions())} sessions)")
    prior = [e for e in x["events"] if e["kind"] == "VALIDATION_RESULT"]
    if final and prior and prior[-1]["evidence"].get("dataset_id") != dataset.dataset_id:
        invalid.append("the final run uses a different dataset than the validation run")
    if not final:
        reg.transition(eid, "VALIDATING", "validator", f"validation on {data_dir}", now)

    run_dir = private_dir(Path(live.experiments.output_dir) / eid / split)
    slots = len({e["time"] for e in _arm(dataset, hist, calendar, sessions[0], sessions[-1],
                                           membership_fn(rows), None)["log"]})
    if llm_factory is None and slots * 2 > live.experiments.max_agent_runs:
        raise RegistryError(f"needs about {slots * 2} research runs, above "
                            f"experiments.max_agent_runs={live.experiments.max_agent_runs}")

    def decider(name, comp):
        s = apply_components(hist, comp).model_copy(update={
            "agent": apply_components(hist, comp).agent.model_copy(update={
                "reports_dir": run_dir / f"{name}_reports"})})
        llm = llm_factory(name, comp) if llm_factory else build_llm(apply_components(live, comp))
        return CachedDecider(AgentDecider(s, calendar, dataset.provider(calendar),
                                          run_dir / f"{name}.db", llm,
                                          live.experiments.max_agent_runs,
                                          **agent_kwargs(comp)))

    m = _overlapping_validations(reg, eid, [sessions[0], sessions[-1]]) + 1
    conf = acc["financial"]["confidence"]
    adj = 1 - (1 - conf) / m
    deciders = {"APPROVED": decider("approved", approved), "CANDIDATE": decider("candidate",
                                                                                cand)}
    res = evaluate_window(sessions, dataset, hist, calendar, membership_fn(rows), approved,
                          cand, deciders, acc, adj)
    again = _arm(dataset, hist, calendar, sessions[0], sessions[-1], membership_fn(rows), None)
    if again["hash"] != res["arm_hashes"]["A"]:
        invalid.append("irreproducible: the deterministic arm gave a different result on rerun")
    replayed = _arm(dataset, apply_components(hist, cand), calendar, sessions[0], sessions[-1],
                    membership_fn(rows), deciders["CANDIDATE"].frozen())
    if replayed["hash"] != res["arm_hashes"]["CANDIDATE"]:
        invalid.append("irreproducible: the candidate arm differs when replayed from its "
                       "recorded decisions")
    res.pop("_arms")
    cls = classify(res["criteria"], invalid)
    out = {"experiment_id": eid, "split": split, "classification": cls, "invalid": invalid,
           "multiple_testing": {"experiments_on_overlapping_period": m,
                                "confidence": conf, "adjusted_confidence": round(adj, 6)},
           "dataset_id": dataset.dataset_id, "data_dir": str(data_dir),
           "plan": {k: ([v[0], v[-1], len(v)] if isinstance(v, list) and v and
                        not isinstance(v[0], list) else v)
                    for k, v in plan.items() if k != "folds"},
           "audit": audit, "limitations": limitations, **res,
           "evaluation_kind": "HISTORICAL_BACKTEST (secondary evidence; prospective shadow "
                              "and canary follow)"}
    from app.experiments.report import write_report

    path, digest = write_report(out, run_dir)
    reg.record(eid, "VALIDATION_RESULT", "validator", f"{split}: {cls}",
               {"split": split, "classification": cls, "period": out["period"],
                "dataset_id": dataset.dataset_id, "report": str(path), "sha256": digest,
                "invalid": invalid}, now)
    if cls != "PASS":
        reg.transition(eid, "REJECTED", "validator", f"{split} classification {cls}", now,
                       {"report": str(path)})
    elif final:
        reg.transition(eid, "VALIDATED", "validator", "validation and final set passed", now,
                       {"report": str(path)})
    out["report"] = str(path)
    return out


def create(reg: Registry, live, spec, actor: str, now: datetime) -> str:
    from app.experiments.candidate import candidate_components

    reg.require_valid()
    approved = reg.active_version(spec.context)
    if approved is None:
        raise RegistryError(f"no active version for {spec.context}; register the current "
                            "system first: `python -m app.experiments init`")
    comp = candidate_components(spec, reg.version(approved)["components"])
    acc, text, digest = load_acceptance(live.experiments.acceptance_file)
    vid = reg.add_version(f"candidate: {spec.name}", comp, actor, now)
    if vid == approved:
        raise RegistryError("the candidate is identical to the approved version")
    return reg.create_experiment({
        "name": spec.name, "hypothesis": spec.hypothesis, "reason": spec.reason,
        "context": spec.context, "baseline_version": approved, "candidate_version": vid,
        "spec": canonical(spec.model_dump()), "acceptance_version": acc["acceptance_version"],
        "acceptance_sha256": digest, "acceptance_text": text,
        "code_versions": canonical(code_versions())}, actor, now)


def init_registry(reg: Registry, live, actor: str, confirm: str, now: datetime) -> str:
    return reg.bootstrap("paper-ai", current_components(live), actor, confirm, now)
