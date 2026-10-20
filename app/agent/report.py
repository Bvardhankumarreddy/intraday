"""Research reports: ``<agent.reports_dir>/<date>/<run_id>.json`` and ``.md``.

Built only from persisted records (run, recommendations, appended outcomes,
tool and model audit), so a report can be regenerated at any time and gives
the same content. AI commentary is labelled as such. Numbers come from tool
outputs. Files are written with ``write_preserving``, so an earlier
different version is archived, not lost.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.utils.fs import private_dir, private_file, write_preserving

AI_LABEL = "AI-GENERATED COMMENTARY (language model; interpretation, not verified fact)"


def run_record(agent_store, run_id: str) -> dict:
    st = agent_store.store
    run = dict(st.one("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)))
    run["summary"] = json.loads(run["summary"]) if run.get("summary") else None
    recs = []
    for r in agent_store.recommendations(run_id=run_id):
        body = json.loads(r["body"])
        body["_integrity_ok"] = agent_store.verify(r)
        body["_outcomes"] = [{"kind": o["kind"], "recorded_at": o["recorded_at"],
                              "body": json.loads(o["body"])}
                             for o in agent_store.outcomes(r["recommendation_id"])]
        recs.append(body)
    tools = [dict(t) for t in st.all("SELECT run_id, call_id, tool, requested_by, args, ok, "
                                     "error, untrusted_flags, started_at, duration_ms, output "
                                     "FROM agent_tool_calls WHERE run_id = ? ORDER BY rowid",
                                     (run_id,))]
    for t in tools:
        t["args"] = json.loads(t["args"])
        t["output"] = json.loads(t["output"]) if t["output"] else None
    models = [{k: v for k, v in dict(m).items() if k != "raw_response"} for m in st.all(
        "SELECT * FROM agent_model_calls WHERE run_id = ? ORDER BY rowid", (run_id,))]
    return {"run": run, "recommendations": recs, "tool_calls": tools, "model_calls": models,
            "disclaimer": "Research only. No orders are placed by this application. Real "
                          "orders are placed manually by the user."}


def _md(rec_set: dict) -> str:
    run = rec_set["run"]
    out = [f"# AI research report — {run['market_date']} ({run['window']})", "",
           f"> {rec_set['disclaimer']}", "",
           f"- Run `{run['run_id']}`: **{run['status']}**"
           + (f" — {run['reason']}" if run.get("reason") else ""),
           f"- Provider `{run['llm_provider']}`, model `{run['model']}`, prompt "
           f"`{run['prompt_version']}`",
           f"- Tool calls: {len(rec_set['tool_calls'])}; model calls: "
           f"{len(rec_set['model_calls'])}", ""]
    if run["window"] != "post" and not any(r["status"] == "CANDIDATE"
                                           for r in rec_set["recommendations"]):
        out += ["## NO TRADE", "", "No candidate passed every deterministic check. This is a "
                "valid outcome.", ""]
    for r in rec_set["recommendations"]:
        title = r["symbol"] or "Run summary"
        out += [f"## {title}: {r['status']}", "",
                f"- Created {r['created_at']}" + (f", valid until {r['valid_until']}"
                                                   if r.get("valid_until") else ""),
                f"- Reasons: {'; '.join(r['status_reasons'])}",
                f"- Versions: strategy {r['strategy_version']}, config {r['config_version']}, "
                f"model {r['model']}, prompt {r['prompt_version']}",
                f"- Data fresh: {r['data_fresh']}; record integrity: "
                f"{'OK' if r.get('_integrity_ok') else 'MISMATCH'}"]
        if r["symbol"]:
            out += ["", "**Deterministic (tool outputs)**", "",
                    f"- Screen {r['screen_status']}, signal {r['signal_status']}, score "
                    f"{r['score']}",
                    f"- Entry condition: {r['entry_condition']}",
                    f"- Entry ref {r['entry_reference']}, stop {r['stop']}, target "
                    f"{r['target']} ({r['proposal_basis']})",
                    f"- Quantity {r['quantity']}; costs at target {r['estimated_costs_at_target']}"
                    f"; {r['estimated_spread_slippage']}",
                    f"- Planned net loss at stop {r['planned_net_loss_at_stop']}; net "
                    f"reward:risk {r['net_reward_risk']}; risk engine {r['risk_status']}"]
            if r["risk_reasons"]:
                out.append(f"- Risk reasons: {'; '.join(r['risk_reasons'][:4])}")
        a = r.get("ai_assessment")
        out += ["", f"**{AI_LABEL}** — status {r['ai_status']}", ""]
        if a:
            for label, key in (("Observed facts", "observed_facts"),
                               ("Supporting", "supporting_evidence"),
                               ("Against", "contradicting_evidence")):
                for c in a[key]:
                    out.append(f"- {label} [{c['basis']}; {', '.join(c['evidence_ids'])
                                                         or 'no evidence'}]: {c['text']}")
            for label, key in (("Entry conditions", "entry_conditions"),
                               ("Invalidation", "invalidation_conditions"),
                               ("Alternative explanations", "alternative_explanations"),
                               ("Missing information", "missing_information")):
                if a[key]:
                    out.append(f"- {label}: {'; '.join(a[key])}")
            out.append(f"- Evidence strength: {a['evidence_strength']}; AI recommendation: "
                       f"{a['recommendation']} — {a['rationale']}")
        if r["unsupported_claims"]:
            out.append(f"- **Unsupported claims flagged:** {'; '.join(r['unsupported_claims'])}")
        if r["untrusted_content_flags"]:
            out.append(f"- **Untrusted content flagged:** {r['untrusted_content_flags']}")
        for o in r.get("_outcomes", []):
            out.append(f"- Outcome (appended {o['recorded_at']}): {o['kind']} "
                       f"{json.dumps(o['body'], default=str)[:300]}")
        out.append("")
    summary = run.get("summary") or {}
    if summary.get("review"):
        rv = summary["review"]
        out += [f"## Post-session review — {AI_LABEL}", "", rv["process_vs_outcome"], ""]
        out += [f"- [{c['basis']}; {', '.join(c['evidence_ids'])}] {c['text']}"
                for c in rv["process_observations"] + rv["outcome_observations"]]
    out += ["", "## Tool-call audit", "", "| id | tool | by | ok | error |", "|---|---|---|---|---|"]
    out += [f"| {t['call_id']} | {t['tool']} | {t['requested_by']} | {bool(t['ok'])} | "
            f"{(t['error'] or '')[:80]} |" for t in rec_set["tool_calls"]]
    out += ["", "## Model calls", "", "| id | model | latency s | tokens in/out | cost USD | "
            "error |", "|---|---|---|---|---|---|"]
    out += [f"| {m['call_id']} | {m['model']} | {m['latency_seconds']} | {m['input_tokens']}/"
            f"{m['output_tokens']} | {m['cost_usd'] if m['cost_usd'] is not None else 'n/a'} | "
            f"{m['error_kind'] or ''} |" for m in rec_set["model_calls"]]
    return "\n".join(out) + "\n"


def write_run_report(agent, run_id: str) -> Path:
    data = run_record(agent.agent_store, run_id)
    root = private_dir(Path(agent.settings.agent.reports_dir) / data["run"]["market_date"])
    private_dir(root.parent)
    jpath = root / f"{run_id}.json"
    write_preserving(jpath, json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")
    write_preserving(root / f"{run_id}.md", _md(data))
    for p in (jpath, root / f"{run_id}.md"):
        private_file(p)
    return jpath
