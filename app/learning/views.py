"""Dashboard view: learning review and improvement proposals (Phase 13).
Read-only: approvals happen only through the CLI with explicit confirmation."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def render(st, settings) -> None:
    st.subheader("Learning review and improvement proposals")
    st.warning("Analysis only. Hypotheses are not proven improvements. Nothing here changes "
               "the active system; approval needs an explicit CLI confirmation.")
    db = Path(settings.learning.db_path)
    if not db.exists():
        st.info("No learning data yet: `python -m app.learning ingest`, then `outcomes`, "
                "`review`, `analyse`.")
        return
    from app.learning.dataset import LearningStore
    from app.learning.report import build

    r = build(LearningStore(db), settings)
    a = r["analysis"]
    cols = st.columns(4)
    cols[0].metric("Records", a["records"])
    cols[1].metric("Reviewed", a["reviewed"])
    cols[2].metric("Sessions", a["sessions"])
    cols[3].metric("Proposals", len(r["proposals"]))
    if a["reviewed"] < a["min_sample"]:
        st.warning(f"Insufficient evidence: {a['reviewed']} reviewed records (< "
                   f"{a['min_sample']}).")
    st.markdown("**Baseline vs AI-assisted (hypothetical trade paths)**")
    st.table(pd.DataFrame(a["performance_hypothetical"]).drop(columns=["note"]).T.astype(str))
    st.markdown("**Error categories**")
    st.dataframe(pd.DataFrame(a["error_categories"]).drop(columns=["supporting_ids"],
                                                          errors="ignore"), width="stretch")
    st.markdown("**Findings**")
    for f in a["findings"]:
        st.write(f"**{f['evidence_level']}** {f['issue']} - {f['hypothesis']}")
    st.markdown("**Proposals**")
    if r["proposals"]:
        st.dataframe(pd.DataFrame([{k: p[k] for k in ("proposal_id", "status",
                                                      "evidence_level", "component",
                                                      "sample_size", "issue")}
                                   for p in r["proposals"]]), width="stretch")
    st.json(a["ai_quality"])
