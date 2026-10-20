"""Read-only dashboard view of Phase 7 robustness reports (files only; no
simulation runs from the dashboard)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def list_runs(output_dir: Path) -> list[Path]:
    """Report folders (newest first by file time; reports hold no timestamps)."""
    runs = [p.parent for p in Path(output_dir).glob("*/*/summary.json")]
    return sorted(runs, key=lambda p: (p / "summary.json").stat().st_mtime, reverse=True)


def render(st, settings) -> None:
    st.subheader("Strategy robustness (Phase 7)")
    st.warning("SIMULATED research on historical candles - not trading results and not a "
               "forecast. Nothing here selects or changes strategy parameters.")
    runs = list_runs(settings.robustness.output_dir)
    if not runs:
        st.info("No robustness report yet. Freeze a baseline and run "
                "`python -m app.robustness run` (see docs/robustness.md).")
        return
    labels = [f"{p.parent.name} / {p.name}" for p in runs]
    run = runs[labels.index(st.sidebar.selectbox("Robustness report", labels))]
    summary = json.loads((run / "summary.json").read_text())
    verdict = summary["assessment"]
    meta = summary["meta"]
    st.markdown(f"### Verdict: `{verdict['verdict']}`")
    st.write(verdict["statement"])
    for r in verdict["reasons"]:
        st.markdown(f"- {r}")
    if verdict["criteria"]:
        st.table(pd.DataFrame(verdict["criteria"]))
    st.caption(f"Baseline {meta['baseline_name']} - fingerprint "
               f"{meta['strategy_fingerprint'][:12]} - code {meta['code_hash_current'][:12]}"
               f"{' (DRIFT)' if meta['code_drift'] else ''} - dataset "
               f"{meta.get('dataset_id', 'n/a')} - report folder {run}")
    if summary["status"] != "OK":
        st.error("Empirical results pending. Missing:")
        for m in summary.get("missing", []):
            st.markdown(f"- {m}")
        for m in summary.get("data_needed", []):
            st.markdown(f"- needed: {m}")
        return
    tabs = st.tabs(["Folds", "Costs", "Sensitivity", "Regimes", "Baselines", "Charts"])
    for tab, name in zip(tabs[:5], ["folds.csv", "cost_scenarios.csv", "sensitivity_summary.csv",
                                    "regimes.csv"]):
        with tab:
            f = run / name
            if f.exists() and f.stat().st_size > 1:
                st.dataframe(pd.read_csv(f), width="stretch")
            else:
                st.info("Not available for this run.")
    with tabs[4]:
        st.json(summary.get("comparison", {}))
    with tabs[5]:
        for svg in sorted((run / "charts").glob("*.svg")):
            st.markdown(svg.read_text(), unsafe_allow_html=True)
    st.download_button("Download report (HTML)", (run / "report.html").read_text(),
                       file_name=f"robustness_{run.name}.html", mime="text/html")
