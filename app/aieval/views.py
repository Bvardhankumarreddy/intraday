"""Dashboard view: AI vs baseline evaluation (read-only, Phase 11)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.aieval.report import ARM_ROWS, KNOWN_LIMITATIONS


def render(st, settings) -> None:
    st.subheader("AI evaluation: does the AI add value? (research only)")
    st.warning("Evaluation of research decisions. Nothing here is an order. A negative or "
               "inconclusive result is a valid outcome.")
    db = Path(settings.ai_eval.ledger_db)
    if not db.exists():
        st.info("No evaluation yet: `python -m app.aieval prospective` or "
                "`python -m app.aieval replay --data DIR --split development`.")
        return
    from app.aieval.ledger import Ledger

    ledger = Ledger(db)
    runs = ledger.runs()
    st.markdown("**Evaluation runs and version history**")
    st.dataframe(pd.DataFrame([{
        "run": r["run_id"], "mode": r["mode"], "split": r["split"],
        "period": f"{r['period_start']}..{r['period_end']}", "status": r["status"],
        "conclusion": r["conclusion"], "protocol": r["protocol_version"],
        "prompt": json.loads(r["ai_versions"]).get("prompt_version"),
        "model": json.loads(r["ai_versions"]).get("model"), "labels": r["labels"]}
        for r in runs]), width="stretch")
    done = [r for r in runs if r["results_path"]]
    if not done:
        st.info("No completed run with results yet.")
        return
    pick = st.sidebar.selectbox("Evaluation run", [r["run_id"] for r in reversed(done)])
    row = next(r for r in done if r["run_id"] == pick)
    res = json.loads(Path(row["results_path"]).read_text())
    c = res["conclusion"]
    st.markdown(f"### {c['conclusion']}")
    st.write(c.get("reason", ""))
    for label in res.get("labels", []):
        st.caption(label)
    if c.get("structural_note"):
        st.error(c["structural_note"])
    unmet = [k for k, v in c.get("minimum_evidence", {}).items() if not v["met"]]
    if unmet:
        st.warning("Sample-size warning: below the predeclared minimum for " + ", ".join(unmet))
    a, b = res["arms"]["A"], res["arms"]["B"]
    st.markdown("**Side by side**")
    st.table(pd.DataFrame([{"metric": label, "A baseline": a.get(k), "B AI-assisted": b.get(k)}
                           for k, label in ARM_ROWS]).astype(str))
    eq = res.get("equity", {})
    if eq.get("A"):
        frame = pd.DataFrame({"A baseline": dict(eq["A"]), "B AI-assisted": dict(eq["B"])})
        st.markdown("**Equity (INR)**")
        st.line_chart(frame)
        dd = frame - frame.cummax()
        st.markdown("**Drawdown (INR)**")
        st.line_chart(dd)
    inc = res["incremental"]
    st.markdown("**Incremental value (B - A) and model cost**")
    st.json({k: v for k, v in inc.items() if k != "decomposition"})
    st.json(inc["decomposition"])
    q = res["quality"]
    st.markdown("**Recommendation quality and rule compliance**")
    cols = st.columns(4)
    cols[0].metric("Rule violations", q["rule_compliance"]["candidate_rule_violations"])
    cols[1].metric("Unsupported claims", q["evidence_quality"]["unsupported_claims"])
    cols[2].metric("Invalid outputs", q["operational"]["invalid_structured_outputs"])
    cols[3].metric("Mean latency (s)", q["operational"]["latency_mean_seconds"] or "n/a")
    st.json(q, expanded=False)
    st.markdown("**By session**")
    st.dataframe(pd.DataFrame(res["per_session"]), width="stretch")
    st.markdown("**By predefined market regime**")
    st.dataframe(pd.DataFrame(res["regimes"]), width="stretch")
    st.markdown("**Uncertainty**")
    st.json(res["statistics"])
    with st.expander("Known biases and limitations"):
        for x in KNOWN_LIMITATIONS:
            st.write("- " + x)
    report = Path(row["results_path"]).parent / "report.md"
    if report.exists():
        st.download_button("Download report (Markdown)", report.read_text(),
                           file_name=f"{pick}.md", mime="text/markdown")
