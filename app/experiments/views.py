"""Dashboard view: versions, experiments, validation and promotion (Phase 14).
Read-only. Approvals, activation and rollback happen only through the CLI with
an explicit confirmation phrase."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def render(st, settings) -> None:
    st.subheader("Experiments and controlled promotion")
    st.warning("Nothing becomes active without validation, shadow and paper-canary evidence, "
               "and a person's explicit approval. Historical results are simulated.")
    db = Path(settings.experiments.registry_db)
    if not db.exists():
        st.info("No registry yet: `python -m app.experiments init --by NAME --confirm "
                "\"INIT paper-ai\"` registers the current system as the approved version.")
        return
    from app.experiments.registry import Registry
    from app.experiments.runtime import CONTEXT

    reg = Registry(db)
    problems = reg.verify()
    if problems:
        st.error("REGISTRY INTEGRITY FAILURE - the AI arm will not start: " + "; ".join(problems))
    else:
        st.success("Registry hash chain verified.")
    active = reg.active_version_unchecked(CONTEXT)
    st.metric("Active AI-system version", active or "none")
    hist = reg.history(CONTEXT)
    if hist:
        st.markdown("**Activation and rollback history**")
        st.dataframe(pd.DataFrame(hist), width="stretch")
    exps = reg.all("SELECT experiment_id, created_at, name, status, baseline_version, "
                   "candidate_version, acceptance_version FROM experiments ORDER BY created_at")
    if not exps:
        st.info("No experiment yet.")
        return
    st.markdown("**Experiments**")
    st.dataframe(pd.DataFrame(exps), width="stretch")
    waiting = [e["experiment_id"] for e in exps if e["status"] == "AWAITING_APPROVAL"]
    if waiting:
        st.warning("Awaiting approval: " + ", ".join(waiting))
    for label in ("SHADOW", "PAPER_CANARY"):
        live = [e["experiment_id"] for e in exps if e["status"] == label]
        st.caption(f"{label}: {', '.join(live) or 'none'}")
    eid = st.sidebar.selectbox("Experiment", [e["experiment_id"] for e in reversed(exps)])
    x = reg.experiment(eid)
    st.markdown(f"**{eid}** - {x['status']}: {x['hypothesis']}")
    results = [e for e in x["events"] if e["kind"] == "VALIDATION_RESULT"]
    for r in results:
        ev = r["evidence"]
        st.markdown(f"Validation ({ev['split']}): **{ev['classification']}**")
        path = Path(ev["report"]).parent / "results.json"
        if path.exists():
            body = json.loads(path.read_text())
            st.dataframe(pd.DataFrame([{"criterion": c["criterion"], "status": c["status"],
                                        "mandatory": c["mandatory"]}
                                       for c in body["criteria"]]), width="stretch")
            st.json({k: body[k] for k in ("multiple_testing", "invalid", "statistics")},
                    expanded=False)
    st.markdown("**Audit trail**")
    st.dataframe(pd.DataFrame([{k: e[k] for k in ("seq", "ts", "kind", "from_status",
                                                  "to_status", "actor", "reason")}
                               for e in x["events"]]), width="stretch")
