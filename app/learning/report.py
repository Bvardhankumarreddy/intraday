"""Periodic learning review report (Phase 13). A pure function of the
learning database; no wall-clock time is added, so regenerating gives
identical bytes. Sample size and period appear beside every metric."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.utils.fs import private_dir, write_preserving


def build(lstore, settings) -> dict:
    from app.learning.analysis import analyse
    from app.learning.proposals import get

    a = analyse(lstore, settings)
    props = [get(lstore, r["proposal_id"]) for r in lstore.all(
        "SELECT proposal_id FROM proposals ORDER BY created_at, proposal_id")]
    for p in props:
        p.pop("events", None)
    return {"analysis": a, "proposals": props,
            "label": "Research and paper-trading analysis only. Hypotheses are not proven "
                     "improvements; nothing here changes the active system. AI confidence is "
                     "never a probability of profit."}


def _v(x):
    return "n/a" if x is None else x


def markdown(r: dict) -> str:
    a = r["analysis"]
    per = a["period"] or ["n/a", "n/a"]
    L = [f"# Learning review - {per[0]} to {per[1]}", "", f"**{r['label']}**", "",
         "## Coverage", "",
         f"- {a['records']} recommendation records ({a['signals']} distinct signals) over "
         f"{a['sessions']} session(s); {a['reviewed']} reviewed, {a['pending_review']} waiting "
         f"for outcomes; {a['with_trade_outcome']} signals with a trade-path outcome",
         f"- Recommendations: {a['recommendations']}",
         f"- Groups below {a['min_sample']} observations are marked insufficient and not "
         "interpreted", "",
         "## AI-assisted vs deterministic baseline (hypothetical trade paths, one per signal)",
         "", "| Set | n | net expectancy | win rate | max drawdown | note |", "|---|---|---|---|---|---|"]
    for k in ("baseline_all_eligible", "ai_assisted_consider", "ai_vetoed_eligible"):
        s = a["performance_hypothetical"][k]
        L.append(f"| {k} | {s['n']} | {_v(s.get('net_expectancy'))} | {_v(s.get('win_rate'))} "
                 f"| {_v(s.get('max_drawdown'))} | {_v(s.get('note'))} |")
    L += ["", a["performance_hypothetical"]["note"], "", "## By group", "",
          "| Dimension | Group | n | net expectancy | win rate | interpretable |",
          "|---|---|---|---|---|---|"]
    for g in a["groups"]:
        L.append(f"| {g['dimension']} | {g['group']} | {g['n']} | "
                 f"{_v(g.get('net_expectancy'))} | {_v(g.get('win_rate'))} | "
                 f"{g['interpretable']} |")
    q = a["ai_quality"]
    L += ["", "## AI factual accuracy, grounding and rule compliance", "",
          f"- Records with unsupported claims: {q['records_with_unsupported_claims']} of "
          f"{a['records']} (rate CI {q['unsupported_rate_ci']})",
          f"- AI unavailable or invalid: {q['ai_unavailable']}",
          f"- Rule-noncompliant CANDIDATE records: {q['rule_noncompliant_candidates']}",
          f"- Process grades: {a['process_grades']}; outcome grades: {a['outcome_grades']}",
          "", "## Error categories and recurring patterns", "",
          "| Category | count | of reviewed | rate CI | recurring |", "|---|---|---|---|---|"]
    for e in a["error_categories"]:
        L.append(f"| {e['category']} | {e['count']} | {e['of_reviewed']} | {e['rate_ci']} | "
                 f"{e['recurring']} |")
    pc = a["period_change"]
    L += ["", "## Change between periods", "", f"- {pc.get('reading', pc.get('reason'))}"
          + (f": earlier {pc['earlier']}, later {pc['later']}, difference CI {pc['diff_ci']}"
             if pc["status"] == "OK" else ""), "", "## Findings", ""]
    for f in a["findings"]:
        L.append(f"- **{f['evidence_level']}** [{f['finding_key']}] {f['issue']}. "
                 f"Hypothesis: {f['hypothesis']}")
    if not a["findings"]:
        L.append("- none (no category met the recurrence threshold)")
    L += ["", "## Improvement proposals", ""]
    for p in r["proposals"]:
        L += [f"### {p['proposal_id']} - {p['status']} ({p['evidence_level']})",
              f"- Issue: {p['issue']}", f"- Hypothesis: {p['hypothesis']}",
              f"- Change: {p['change']} (component {p['component']})",
              f"- Baseline {p['baseline_version']} -> candidate {p['candidate_version']}",
              f"- Expected benefit: {p['expected_benefit']}; possible downside: "
              f"{p['possible_downside']}",
              f"- Sample size {p['sample_size']}; supporting records "
              f"{', '.join(p['supporting_ids'][:5])}{' ...' if len(p['supporting_ids']) > 5 else ''}",
              f"- Required tests: {'; '.join(p['required_tests'])}",
              f"- Acceptance: {p['acceptance_criteria']}",
              f"- Tests recorded: {[(t['kind'], t['result'], t['evaluation_run_id']) for t in p['tests']] or 'none'}",
              f"- Limitations: {'; '.join(p['limitations'])}", ""]
    if not r["proposals"]:
        L.append("No proposal.")
    L += ["", "## Missing data, uncertainty and limitations", "",
          "- Outcomes are written only after their horizon has passed; pending ones are not "
          "estimated.",
          "- Trade paths are hypothetical; paper fills are reported separately.",
          "- Interpretation errors need a human annotation (not detectable mechanically).",
          "- Nothing in this report changes prompts, models, settings or the strategy."]
    return "\n".join(L) + "\n"


def write(lstore, settings) -> Path:
    body = build(lstore, settings)
    text = json.dumps(body, indent=2, sort_keys=True, default=str) + "\n"
    per = body["analysis"]["period"] or ["none", "none"]
    out = private_dir(Path(settings.learning.reports_dir))
    stem = f"review_{per[0]}_{per[1]}"
    write_preserving(out / f"{stem}.json", text)
    path = out / f"{stem}.md"
    write_preserving(path, markdown(json.loads(text)))
    (out / f"{stem}.sha256").write_text(hashlib.sha256(text.encode()).hexdigest() + "\n")
    return path
