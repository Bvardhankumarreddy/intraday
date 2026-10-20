"""Reproducible experiment report (Phase 14): a pure function of the stored
results JSON (no wall-clock time added)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.utils.fs import private_file, write_preserving


def write_report(result: dict, run_dir: Path) -> tuple[Path, str]:
    text = json.dumps(result, indent=2, sort_keys=True, default=str) + "\n"
    write_preserving(Path(run_dir) / "results.json", text)
    private_file(Path(run_dir) / "results.json")
    path = Path(run_dir) / "report.md"
    write_preserving(path, markdown(json.loads(text)))
    return path, hashlib.sha256(text.encode()).hexdigest()


def _v(x):
    return "n/a" if x is None else x


def markdown(r: dict, experiment: dict | None = None) -> str:
    L = [f"# Experiment {r['experiment_id']} - {r['split']} - **{r['classification']}**", "",
         f"Evaluation kind: {r['evaluation_kind']}. Simulated results; never real trading "
         "profits and no guarantee of future performance.", ""]
    if experiment:
        L += [f"- Hypothesis: {experiment['hypothesis']}",
              f"- Baseline (approved) {experiment['baseline_version']} -> candidate "
              f"{experiment['candidate_version']}",
              f"- Acceptance rules {experiment['acceptance_version']} "
              f"({experiment['acceptance_sha256'][:12]})", ""]
    L += ["## Periods and data", "",
          f"- {r['split']} period {r['period'][0]} to {r['period'][1]}, {r['sessions']} "
          f"sessions; dataset {r['dataset_id']} ({r['data_dir']})",
          f"- Splits (start, end, sessions): {r['plan']}",
          f"- Multiple testing: {r['multiple_testing']}", ""]
    if r["invalid"]:
        L += ["## INVALID", ""] + [f"- {x}" for x in r["invalid"]] + [""]
    L += ["## Results by arm", "", "| Metric | A deterministic | APPROVED AI | CANDIDATE |",
          "|---|---|---|---|"]
    for k in ("closed_trades", "gross_pnl", "total_costs", "net_pnl", "expectancy",
              "win_rate_pct", "avg_win", "avg_loss", "profit_factor", "max_drawdown_inr",
              "exposure_buy_turnover", "net_return_pct"):
        L.append(f"| {k} | " + " | ".join(str(_v(r["metrics"][a].get(k)))
                                          for a in ("A", "APPROVED", "CANDIDATE")) + " |")
    st = r["statistics"]
    L += ["", "## Uncertainty and robustness", "",
          f"- Per-session difference (candidate - approved): mean "
          f"{_v(st.get('mean_session_difference'))}, CI [{_v(st.get('ci_low'))}, "
          f"{_v(st.get('ci_high'))}] at adjusted confidence "
          f"{r['multiple_testing']['adjusted_confidence']}",
          f"- Folds: {r['folds']}",
          f"- Difference under doubled spread and slippage (AI decisions fixed): "
          f"{r['conservative_costs_difference']}",
          f"- Difference without the best sessions: {r['difference_without_best_sessions']}",
          f"- Attribution: {r['decomposition']}", "", "## AI quality and operating cost", ""]
    for arm, q in r["quality"].items():
        L.append(f"- {arm}: {q}")
    L += ["", "## Acceptance criteria", "", "| Criterion | Mandatory | Status | Evidence |",
          "|---|---|---|---|"]
    for c in r["criteria"]:
        L.append(f"| {c['criterion']} | {c['mandatory']} | {c['status']} | "
                 f"{json.dumps(c['evidence'], default=str)[:300]} |")
    L += ["", "## Data limitations", ""]
    for lim in r.get("limitations") or []:
        L.append(f"- {lim['issue']}: {lim['effect']}" if isinstance(lim, dict)
                 and 'issue' in lim else f"- {lim}")
    L += ["", "## Classification and promotion eligibility", "",
          f"**{r['classification']}**. "
          + ("Eligible to continue the promotion workflow (approval, shadow, canary)."
             if r["classification"] == "PASS" else "Not eligible; the experiment is kept for "
                                                   "the record."),
          "", "Historical backtest results are secondary evidence; prospective shadow and "
              "paper-canary sessions follow before any activation."]
    return "\n".join(L) + "\n"
