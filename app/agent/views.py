"""Dashboard view of AI research runs (read-only)."""

from __future__ import annotations

import json

import pandas as pd

from app.agent.report import AI_LABEL, run_record


def render(st, settings, store) -> None:
    from app.agent.store import AgentStore

    st.subheader("AI research (advisory only)")
    st.warning("Research only. Recommendations are not orders. Real orders are placed manually "
               f"by you. Provider: {settings.llm.provider.value} ({settings.llm.model}).")
    ag = AgentStore(store)
    runs = ag.runs()
    if not runs:
        st.info("No research runs yet: `python -m app.agent run --window intraday` or "
                "`python -m app.agent schedule`.")
        return
    labels = [f"{r['market_date']} {r['window']} {r['status']} ({r['run_id']})"
              for r in reversed(runs)]
    run_id = list(reversed(runs))[labels.index(st.sidebar.selectbox("Research run",
                                                                    labels))]["run_id"]
    data = run_record(ag, run_id)
    run = data["run"]
    st.markdown(f"**Run {run_id}** - {run['status']}" + (f" - {run['reason']}"
                                                         if run.get("reason") else ""))
    st.caption(f"model {run['model']} - prompt {run['prompt_version']} - "
               f"{len(data['tool_calls'])} tool calls - {len(data['model_calls'])} model calls")
    recs = data["recommendations"]
    if run["window"] != "post" and not any(r["status"] == "CANDIDATE" for r in recs):
        st.error("NO TRADE: no candidate passed every deterministic check. This is a valid "
                 "outcome.")
    if recs:
        st.table(pd.DataFrame([{
            "symbol": r["symbol"] or "(run)", "status": r["status"],
            "entry ref": r.get("entry_reference"), "stop": r.get("stop"),
            "target": r.get("target"), "qty": r.get("quantity"),
            "costs@target": r.get("estimated_costs_at_target"),
            "net loss@stop": r.get("planned_net_loss_at_stop"),
            "net R:R": r.get("net_reward_risk"), "risk engine": r.get("risk_status"),
            "data fresh": r["data_fresh"], "AI": r["ai_status"],
            "valid until": r.get("valid_until")} for r in recs]))
    for r in recs:
        with st.expander(f"{r['symbol'] or 'Run summary'}: {r['status']}"):
            st.write("Reasons: " + "; ".join(r["status_reasons"]))
            st.caption(f"created {r['created_at']} - strategy {r['strategy_version']} - "
                       f"config {r['config_version']} - integrity "
                       f"{'OK' if r.get('_integrity_ok') else 'MISMATCH'}")
            if r.get("ai_assessment"):
                st.markdown(f"**{AI_LABEL}**")
                st.json(r["ai_assessment"])
            if r["unsupported_claims"]:
                st.error("Unsupported claims: " + "; ".join(r["unsupported_claims"]))
            if r["untrusted_content_flags"]:
                st.warning(f"Untrusted content flagged: {r['untrusted_content_flags']}")
            if r.get("risk_reasons"):
                st.write("Risk engine: " + "; ".join(r["risk_reasons"]))
            for o in r.get("_outcomes", []):
                st.write(f"Outcome (appended {o['recorded_at']}): {o['kind']}")
                st.json(o["body"])
    with st.expander("Tool-call audit (inputs and numerical outputs)"):
        for t in data["tool_calls"]:
            st.markdown(f"`{t['call_id']}` **{t['tool']}** by {t['requested_by']} - "
                        f"{'ok' if t['ok'] else 'ERROR: ' + str(t['error'])}")
            st.json({"args": t["args"], "output": t["output"]}, expanded=False)
    if data["model_calls"]:
        st.dataframe(pd.DataFrame(data["model_calls"]), width="stretch")
    summary = run.get("summary") or {}
    if summary.get("review"):
        st.markdown(f"**Post-session review - {AI_LABEL}**")
        st.json(summary["review"])
    st.download_button("Download run (JSON)", json.dumps(data, indent=2, default=str),
                       file_name=f"{run_id}.json", mime="application/json")
