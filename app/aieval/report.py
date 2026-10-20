"""Phase 11 evaluation report (JSON + Markdown), reproducible from records.

``results.json`` is written once per run and its SHA-256 is stored in the
ledger. ``render_markdown`` is a pure function of that JSON: regenerating it
gives identical bytes. No wall-clock time is added at render time.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.utils.fs import private_file, write_preserving

# Known, unresolved limitations of the evaluation itself (Phase 11 audit).
KNOWN_LIMITATIONS = [
    "The AI is veto-only by design: it cannot propose trades, so it can only remove baseline "
    "trades (and, in replay, let a later signal use the freed capacity).",
    "Historical replay uses a model that may have been trained on text covering the replayed "
    "dates: hindsight contamination cannot be excluded, so replay is secondary evidence.",
    "In replay the agent's risk tool sees a fresh-capital portfolio, while the replay engine "
    "re-checks with the arm's actual portfolio; a veto can therefore reflect a risk view that "
    "differs from the engine's.",
    "Only the top agent.max_candidates setups are reviewed per run; others count as "
    "not reviewed (not taken by arm B).",
    "No verified news or corporate-event source is configured: the AI sees market data and "
    "deterministic outputs only.",
    "Numbers in model text are checked against tool outputs, but integers up to 10 and numbers "
    "written as words are not checked.",
    "Prospective arm B is a filter on the paper engine's trades; capacity freed by a veto is "
    "not re-used.",
    "Model cost is not converted to INR unless llm.*_cost_per_mtok and ai_eval.usd_inr_rate "
    "are configured; it is never guessed.",
]


def _j(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n"


def write_reports(results: dict, run_dir: Path) -> tuple[Path, str]:
    text = _j(results)
    path = Path(run_dir) / "results.json"
    write_preserving(path, text)
    private_file(path)
    md = Path(run_dir) / "report.md"
    write_preserving(md, render_markdown(json.loads(text)))
    private_file(md)
    return path, hashlib.sha256(text.encode()).hexdigest()


def regenerate(run_dir: Path) -> Path:
    body = json.loads((Path(run_dir) / "results.json").read_text())
    md = Path(run_dir) / "report.md"
    write_preserving(md, render_markdown(body))
    return md


def _v(x) -> str:
    return "n/a" if x is None else (f"{x:g}" if isinstance(x, float) else str(x))


def _table(rows: list[dict], cols: list[str]) -> list[str]:
    if not rows:
        return ["(none)"]
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(_v(r.get(c)) for c in cols) + " |" for r in rows]
    return out


ARM_ROWS = [
    ("eligible_sessions", "Eligible sessions"), ("complete_sessions", "Complete sessions"),
    ("incomplete_sessions", "Incomplete sessions"), ("failed_sessions", "Failed sessions"),
    ("signals_evaluated", "Candidates evaluated"),
    ("recommendations_taken", "Recommendations taken"),
    ("rejected_candidates", "Rejected candidates"), ("no_trade_decisions", "No-trade decisions"),
    ("paper_orders", "Paper orders"), ("orders_filled", "Filled"),
    ("orders_partially_filled", "Partially filled"), ("orders_unfilled", "Unfilled"),
    ("closed_trades", "Closed trades"), ("gross_pnl", "Gross P&L (INR)"),
    ("total_costs", "Transaction costs (INR)"), ("net_pnl", "Net P&L (INR)"),
    ("avg_net_per_trade", "Avg net per closed trade"), ("win_rate_pct", "Win rate %"),
    ("avg_win", "Avg win"), ("avg_loss", "Avg loss"), ("expectancy", "Expectancy"),
    ("profit_factor", "Profit factor"), ("max_drawdown_inr", "Max drawdown (INR)"),
    ("exposure_buy_turnover", "Exposure (buy turnover)"),
    ("incomplete_trades", "Incomplete trades"), ("data_quality_failures",
                                                 "Data-quality failures"),
]


def render_markdown(r: dict) -> str:
    m, c = r["meta"], r["conclusion"]
    L = [f"# AI vs baseline evaluation - {r.get('run_id', m['mode'])}", "",
         "**Research evaluation only. Nothing here is an order or trading advice. Real orders "
         "are placed manually by the user. Results may be negative or inconclusive.**", "",
         "Labels: " + "; ".join(r.get("labels", [])), ""]

    L += ["## 1. Executive summary", "",
          f"- Conclusion: **{c['conclusion']}**: {c.get('reason', '')}"]
    if c.get("structural_note"):
        L.append(f"- Structural: {c['structural_note']}")
    if r["status"] == "OK":
        inc, a, b = r["incremental"], r["arms"]["A"], r["arms"]["B"]
        L += [f"- Sessions: {r['coverage']['sessions']}; closed trades A {a['closed_trades']}, "
              f"B {b['closed_trades']}; signals where the arms differed: "
              f"{r['discordant_signals']}",
              f"- Net P&L A {_v(a['net_pnl'])}, B {_v(b['net_pnl'])} INR; difference "
              f"{_v(inc['net_pnl_difference'])} INR; after model cost "
              f"{_v(inc['net_incremental_after_model_cost_inr'])} INR"]
    else:
        L.append("- Missing: " + "; ".join(r.get("missing", [c.get("reason", "")])))
    L.append("")

    L += ["## 2. Evaluation protocol and version", "",
          f"- Protocol {m['protocol_version']} (sha256 {m['protocol_sha256'][:16]}...), "
          "predeclared in config/ai_eval/protocol.yaml",
          f"- Mode {m['mode']}" + (f", split {m['split']}" if m.get("split") else ""),
          f"- Baseline {m['baseline_name']} (fingerprint {m['baseline_fingerprint'][:16]}...)",
          f"- AI: prompt {m['ai_versions']['prompt_version']}, model "
          f"{m['ai_versions']['model']} ({m['ai_versions']['llm_provider']}), tools "
          f"{m['ai_versions']['tool_schema_sha256'][:12]}, agent code "
          f"{m['ai_versions']['agent_code_sha256'][:12]}",
          f"- AI-system drift from the protocol pins: {m['ai_system_drift'] or 'none'}", ""]

    if r["status"] != "OK":
        L += ["## 3-12.", "", "Not computed: no eligible data. The evaluation infrastructure "
              "ran and recorded this run; empirical conclusions are PENDING.", "",
              "## 10. Known biases and limitations", ""] + [f"- {x}" for x in KNOWN_LIMITATIONS]
        return "\n".join(L) + "\n"

    s = r["systems"]
    L += ["## 3. Systems compared", "", f"- **A** {s['A']}", f"- **B** {s['B']}",
          f"- The AI may change: {s['ai_may_change']}",
          f"- The AI may not change: {s['ai_may_not_change']}", ""]

    cov = r["coverage"]
    L += ["## 4. Data coverage and quality", "",
          f"- Period {cov['period'][0]} to {cov['period'][1]}, {cov['sessions']} eligible "
          "sessions"]
    if "symbols" in cov:
        L.append(f"- Symbols {cov['symbols']}; regime proxy: {cov.get('regime_proxy')}")
        L.append(f"- Data provenance: {m.get('data_provenance')}")
    if "session_status_counts" in cov:
        L.append(f"- Session statuses: {cov['session_status_counts']} (only COMPLETED count)")
    for lim in cov.get("limitations") or []:
        L.append(f"- Limitation: {lim['issue']}: {lim['effect']}" if isinstance(lim, dict)
                 and 'issue' in lim else f"- Limitation: {lim}")
    L.append("")

    L += ["## 5. Results for both systems", "", "| Metric | A baseline | B AI-assisted |",
          "|---|---|---|"]
    for k, label in ARM_ROWS:
        L.append(f"| {label} | {_v(r['arms']['A'].get(k))} | {_v(r['arms']['B'].get(k))} |")
    L += ["", f"Profit factor notes: A '{r['arms']['A']['profit_factor_note']}', B "
          f"'{r['arms']['B']['profit_factor_note']}'.", "", "Per session:", ""]
    L += _table(r["per_session"], list(r["per_session"][0].keys()) if r["per_session"] else [])
    L += ["", "By predefined market regime:", ""]
    L += _table(r["regimes"], ["dimension", "group", "sessions", "A_trades", "A_net_pnl",
                               "B_trades", "B_net_pnl"])
    L.append("")

    inc = r["incremental"]
    L += ["## 6. Incremental-value analysis", "", "| Measure (B - A) | Value |", "|---|---|"]
    for k in ("net_pnl_difference", "expectancy_difference", "max_drawdown_difference",
              "transaction_cost_difference", "trade_count_difference", "exposure_difference",
              "model_cost_usd", "model_cost_inr", "net_incremental_after_model_cost_inr"):
        L.append(f"| {k} | {_v(inc.get(k))} |")
    if inc.get("net_incremental_note"):
        L.append(f"\n{inc['net_incremental_note']}")
    d = inc["decomposition"]
    L += ["", "Attribution on matched signals:", "",
          f"- Selection (baseline trades the AI vetoed): {d['trades_a_only_vetoed_by_ai']} "
          f"trades, effect {_v(d['selection_effect_inr'])} INR",
          f"- Capacity (trades only B took): {d['trades_b_only_capacity']} trades, effect "
          f"{_v(d['capacity_effect_inr'])} INR",
          f"- Shared trades with different quantity: "
          f"{d['shared_trades_with_different_quantity']}, effect "
          f"{_v(d['shared_trade_effect_inr'])} INR", f"- {d['note']}", ""]
    rv = r.get("random_veto_benchmark")
    if rv:
        L += [f"Random-veto benchmark ({rv['status']}): AI selection effect "
              f"{_v(rv['ai_selection_effect'])} INR vs random expected "
              f"{_v(rv.get('random_expected_effect'))} INR; {rv['reason']}. Beyond random: "
              f"{'yes' if rv['beyond_random'] else 'no'}.", ""]
    if r.get("selection_diagnostic"):
        sd = r["selection_diagnostic"]
        L += ["Selection diagnostic (per share, gross, no capital):", ""]
        L += _table(sd["groups"], ["ai_view", "signals", "with_price_path", "mean_r_multiple",
                                   "share_positive", "interpretable"])
        L += ["", sd["note"], ""]
    L += [f"Explanatory value: {inc['explanatory_value']}.", ""]

    q = r["quality"]
    rc, ev, co, op = q["rule_compliance"], q["evidence_quality"], q["consistency"], \
        q["operational"]
    L += ["## 7. Rule-compliance and factual-accuracy results", "",
          f"- CANDIDATE recommendations {rc['candidates']}; rule violations "
          f"**{rc['candidate_rule_violations']}**",
          f"- AI judged 'consider' on risk-rejected signals {rc['ai_consider_on_risk_rejected']}"
          f" times and on stale data {rc['ai_consider_on_stale_data']} times (overridden)",
          f"- Assessments {ev['assessments']}; claims {ev['claims']} by basis "
          f"{ev['claims_by_basis']}",
          f"- Unsupported claims {ev['unsupported_claims']} {ev['unsupported_by_kind']}",
          f"- With contradicting evidence: {_v(ev['rate_with_contradicting_evidence'])}; "
          f"disclosing missing information: {_v(ev['rate_disclosing_missing_information'])}",
          f"- Source references dated after the decision: "
          f"{ev['source_refs_after_decision_time']}",
          f"- Consistency: changed without new deterministic information "
          f"{co['changed_without_new_deterministic_information']}; mixed similar-input groups "
          f"{co['mixed_groups']}; invented probabilities {co['invented_probabilities']}; "
          f"abstentions with a reason {co['abstentions_with_reason']}/{co['abstentions']}",
          f"- Operations: {op['research_runs']} runs, {op['model_requests']} model requests, "
          f"errors {op['model_errors'] or 'none'}, invalid outputs "
          f"{op['invalid_structured_outputs']}, truncated outputs rejected "
          f"{op['truncated_outputs_rejected']}, tool errors {op['tool_errors']}, latency mean "
          f"{_v(op['latency_mean_seconds'])}s p95 {_v(op['latency_p95_seconds'])}s, tokens "
          f"{op['input_tokens']} in / {op['output_tokens']} out, duplicates "
          f"{op['duplicate_candidates_same_signal']}", ""]

    cap = r["capital"]
    L += ["## 8. Cost and capital-feasibility analysis", "",
          f"- Initial capital Rs {cap['initial_capital']:g}; executable baseline signals "
          f"{cap['executable_baseline_signals']}",
          f"- Risk results: {cap.get('indicative_risk_status_counts') or cap.get('risk_status_counts')}",
          f"- Transaction costs A {_v(r['arms']['A']['total_costs'])}, B "
          f"{_v(r['arms']['B']['total_costs'])} INR; model/API cost {_v(inc['model_cost_usd'])}"
          f" USD = {_v(inc['model_cost_inr'])} INR (separate from trading costs)", ""]
    if cap.get("note"):
        L += [cap["note"], ""]

    st = r["statistics"]
    L += ["## 9. Statistical uncertainty", "",
          f"- {st['method']}; {st['sessions']} sessions, {st['resamples']} resamples, seed "
          f"{st['seed']}, block length {_v(st.get('block_length'))}",
          f"- Mean per-session difference {_v(st['mean_session_difference'])} INR, "
          f"{int(st['confidence'] * 100)}% CI [{_v(st['ci_low'])}, {_v(st['ci_high'])}]",
          f"- Expectancy difference {_v(st['expectancy_difference'])} INR/trade, CI "
          f"[{_v(st.get('expectancy_ci_low'))}, {_v(st.get('expectancy_ci_high'))}]"]
    for k, v in c.get("minimum_evidence", {}).items():
        L.append(f"- Minimum {k}: {v['actual']} of {v['required']} "
                 f"({'met' if v['met'] else 'NOT met'})")
    L += ["- An interval including zero is lack of evidence, not proof that the systems are "
          "equivalent.", ""]

    L += ["## 10. Known biases and limitations", ""] + [f"- {x}" for x in KNOWN_LIMITATIONS]
    L.append("")
    L += ["## 11. Outstanding defects", ""]
    defects = [f"agent run failure: {f}" for f in (r.get("agent") or {}).get("failures", [])]
    if m.get("ai_system_drift"):
        defects.append("AI system differs from the protocol pins: " + "; ".join(
            m["ai_system_drift"]))
    L += [f"- {x}" for x in defects] or ["- none recorded in this run"]
    L.append("")
    L += ["## 12. Final evidence-based conclusion", "", f"**{c['conclusion']}**: {c['reason']}",
          "", "This conclusion follows the predeclared rule mechanically. It does not promise "
          "profitability and does not authorise real-money trading."]
    return "\n".join(L) + "\n"
