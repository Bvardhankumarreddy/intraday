"""Improvement-proposal workflow (Phase 13).

    PROPOSED ──start_test──> UNDER_TEST ──approve*──> APPROVED ──deploy*──> DEPLOYED
       │                        │                        │                     │
       └──reject──> REJECTED <──┘<──────reject───────────┘        rollback*──> ROLLED_BACK

    (* = explicit human action: the approver's name, a reason, and the exact
     confirmation phrase "<ACTION> <proposal_id>")

Rules (enforced here and by SQLite triggers):
- A proposal's content (hypothesis, change, criteria, evidence) is immutable
  after creation. Only ``status`` may change, and only through a recorded,
  validated transition (``proposal_events``, append-only).
- ``approve`` needs at least one recorded test that PASSED its predeclared
  acceptance criteria. A test result is never typed in by hand: it is derived
  from a completed evaluation run in the Phase 11 ledger (``record_test``).
- ``deploy`` only RECORDS that a person released the change as a new,
  separately versioned component (for example a new prompt version and
  protocol). This module never edits configuration, prompts or strategy
  files, and has no code path that could.
- Selection-bias guards: at most ``learning.max_tests_per_validation_period``
  distinct proposals may be tested on overlapping validation periods (after
  that, fresh data is required). Each proposal may use the final evaluation
  set (from ``learning.final_evaluation_start``) at most once. Every test,
  passed or failed, is kept, and so are rejected proposals.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

PROPOSAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    finding_key TEXT NOT NULL,
    issue TEXT NOT NULL,
    supporting_ids TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    change TEXT NOT NULL,
    component TEXT NOT NULL,
    baseline_version TEXT NOT NULL,
    candidate_version TEXT NOT NULL,
    expected_benefit TEXT NOT NULL,
    possible_downside TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    evidence_level TEXT NOT NULL,     -- HYPOTHESIS | ESTABLISHED_PATTERN (never "proven")
    required_tests TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL,
    limitations TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposal_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,               -- PLAN | RESULT
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    dataset TEXT NOT NULL,
    final_set INTEGER NOT NULL,
    evaluation_run_id TEXT,
    result TEXT,                      -- PASS | FAIL | INCONCLUSIVE (RESULT rows)
    details TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS proposal_content_frozen BEFORE UPDATE ON proposals
WHEN NEW.proposal_id IS NOT OLD.proposal_id OR NEW.created_at IS NOT OLD.created_at
  OR NEW.finding_key IS NOT OLD.finding_key OR NEW.issue IS NOT OLD.issue
  OR NEW.supporting_ids IS NOT OLD.supporting_ids OR NEW.hypothesis IS NOT OLD.hypothesis
  OR NEW.change IS NOT OLD.change OR NEW.component IS NOT OLD.component
  OR NEW.baseline_version IS NOT OLD.baseline_version
  OR NEW.candidate_version IS NOT OLD.candidate_version
  OR NEW.expected_benefit IS NOT OLD.expected_benefit
  OR NEW.possible_downside IS NOT OLD.possible_downside
  OR NEW.sample_size IS NOT OLD.sample_size OR NEW.evidence_level IS NOT OLD.evidence_level
  OR NEW.required_tests IS NOT OLD.required_tests
  OR NEW.acceptance_criteria IS NOT OLD.acceptance_criteria
  OR NEW.limitations IS NOT OLD.limitations
BEGIN SELECT RAISE(ABORT, 'proposal content is immutable; only status changes'); END;
CREATE TRIGGER IF NOT EXISTS proposal_no_delete BEFORE DELETE ON proposals
BEGIN SELECT RAISE(ABORT, 'proposals are kept, including rejected ones'); END;
CREATE TRIGGER IF NOT EXISTS proposal_events_frozen BEFORE UPDATE ON proposal_events
BEGIN SELECT RAISE(ABORT, 'proposal events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS proposal_events_kept BEFORE DELETE ON proposal_events
BEGIN SELECT RAISE(ABORT, 'proposal events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS proposal_tests_frozen BEFORE UPDATE ON proposal_tests
BEGIN SELECT RAISE(ABORT, 'proposal tests are append-only'); END;
CREATE TRIGGER IF NOT EXISTS proposal_tests_kept BEFORE DELETE ON proposal_tests
BEGIN SELECT RAISE(ABORT, 'proposal tests are append-only'); END;
"""

STATUSES = ("PROPOSED", "UNDER_TEST", "REJECTED", "APPROVED", "DEPLOYED", "ROLLED_BACK")
TRANSITIONS = {
    "PROPOSED": {"UNDER_TEST", "REJECTED"},
    "UNDER_TEST": {"APPROVED", "REJECTED"},
    "APPROVED": {"DEPLOYED", "REJECTED"},
    "DEPLOYED": {"ROLLED_BACK"},
    "REJECTED": set(),
    "ROLLED_BACK": set(),
}
OPEN = ("PROPOSED", "UNDER_TEST", "APPROVED")
HUMAN_CONFIRM = {"APPROVED": "APPROVE", "DEPLOYED": "DEPLOY", "ROLLED_BACK": "ROLLBACK"}


class ProposalError(RuntimeError):
    pass


def _j(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def create(lstore, p: dict, now: datetime) -> str | None:
    """Create a proposal unless an open one exists for the same finding."""
    if lstore.one("SELECT 1 FROM proposals WHERE finding_key = ? AND status IN "
                  f"({', '.join('?' for _ in OPEN)})", (p["finding_key"], *OPEN)):
        return None
    n = lstore.one("SELECT COUNT(*) AS n FROM proposals")["n"] + 1
    pid = f"P-{now:%Y%m%d}-{n:03d}"
    with lstore.store.transaction():
        lstore.store._insert("proposals", {
            "proposal_id": pid, "created_at": now, "finding_key": p["finding_key"],
            "issue": p["issue"], "supporting_ids": _j(p["supporting_ids"]),
            "hypothesis": p["hypothesis"], "change": p["change"], "component": p["component"],
            "baseline_version": p["baseline_version"],
            "candidate_version": p["candidate_version"],
            "expected_benefit": p["expected_benefit"],
            "possible_downside": p["possible_downside"], "sample_size": p["sample_size"],
            "evidence_level": p["evidence_level"], "required_tests": _j(p["required_tests"]),
            "acceptance_criteria": _j(p["acceptance_criteria"]),
            "limitations": _j(p["limitations"]), "status": "PROPOSED"})
        lstore.store._insert("proposal_events", {
            "proposal_id": pid, "ts": now, "from_status": None, "to_status": "PROPOSED",
            "actor": "learning-engine", "reason": "generated from findings",
            "evidence": _j({"finding_key": p["finding_key"]})})
    return pid


def get(lstore, pid: str) -> dict:
    row = lstore.one("SELECT * FROM proposals WHERE proposal_id = ?", (pid,))
    if row is None:
        raise ProposalError(f"no proposal {pid}")
    p = dict(row)
    for k in ("supporting_ids", "required_tests", "acceptance_criteria", "limitations"):
        p[k] = json.loads(p[k])
    p["events"] = lstore.all("SELECT * FROM proposal_events WHERE proposal_id = ? ORDER BY id",
                             (pid,))
    p["tests"] = lstore.all("SELECT * FROM proposal_tests WHERE proposal_id = ? ORDER BY id",
                            (pid,))
    return p


def _transition(lstore, pid: str, to: str, actor: str, reason: str, now: datetime,
                evidence: dict | None = None, confirm: str | None = None) -> None:
    p = get(lstore, pid)
    if to not in TRANSITIONS[p["status"]]:
        raise ProposalError(f"{pid}: {p['status']} -> {to} is not allowed")
    if not actor.strip() or not reason.strip():
        raise ProposalError("every transition needs the actor and a reason")
    if to in HUMAN_CONFIRM:
        phrase = f"{HUMAN_CONFIRM[to]} {pid}"
        if confirm != phrase:
            raise ProposalError(f"explicit human confirmation required: pass --confirm "
                                f"\"{phrase}\"")
        if actor in ("learning-engine", "system", "auto"):
            raise ProposalError("approval, deployment and rollback must be made by a person")
    with lstore.store.transaction():
        cur = lstore.store.conn.execute(
            "UPDATE proposals SET status = ? WHERE proposal_id = ? AND status = ?",
            (to, pid, p["status"]))
        if cur.rowcount != 1:
            raise ProposalError(f"{pid} changed concurrently; retry")
        lstore.store._insert("proposal_events", {
            "proposal_id": pid, "ts": now, "from_status": p["status"], "to_status": to,
            "actor": actor, "reason": reason, "evidence": _j(evidence or {})})


def _overlap(a0, a1, b0, b1) -> bool:
    return not (a1 < b0 or b1 < a0)


def start_test(lstore, settings, pid: str, start: date, end: date, dataset: str, actor: str,
               now: datetime) -> None:
    if end < start:
        raise ProposalError("test period end is before its start")
    final = settings.learning.final_evaluation_start
    on_final = final is not None and end >= final
    if on_final and lstore.one("SELECT 1 FROM proposal_tests WHERE proposal_id = ? AND "
                               "final_set = 1", (pid,)):
        raise ProposalError(f"{pid} has already used the final evaluation set; it is single "
                            "use per proposal")
    others = {r["proposal_id"] for r in lstore.all(
        "SELECT proposal_id, period_start, period_end FROM proposal_tests WHERE kind = 'PLAN'")
              if r["proposal_id"] != pid and _overlap(
                  r["period_start"], r["period_end"], start.isoformat(), end.isoformat())}
    limit = settings.learning.max_tests_per_validation_period
    if len(others) >= limit:
        raise ProposalError(f"{len(others)} other proposal(s) were already tested on an "
                            f"overlapping period (limit {limit}): testing more variants on it "
                            "would select on noise. Use fresh data.")
    _transition(lstore, pid, "UNDER_TEST", actor, f"test planned {start}..{end} on {dataset}",
                now, {"period": [start, end], "dataset": dataset, "final_set": on_final,
                      "other_proposals_on_period": sorted(others)})
    with lstore.store.transaction():
        lstore.store._insert("proposal_tests", {
            "proposal_id": pid, "ts": now, "kind": "PLAN", "period_start": start,
            "period_end": end, "dataset": dataset, "final_set": int(on_final),
            "evaluation_run_id": None, "result": None,
            "details": _j({"multiplicity": len(others) + 1})})


def record_test(lstore, settings, pid: str, run_id: str, now: datetime) -> str:
    """Attach a completed Phase 11/14 evaluation run as test evidence. The result
    is derived from that run's recorded conclusion, never entered by hand."""
    from app.aieval.ledger import Ledger

    p = get(lstore, pid)
    if p["status"] != "UNDER_TEST":
        raise ProposalError(f"{pid} is {p['status']}; start a test first")
    plan = [t for t in p["tests"] if t["kind"] == "PLAN"][-1]
    ledger = Ledger(settings.ai_eval.ledger_db)
    try:
        run = ledger.run(run_id)
    finally:
        ledger.close()
    if run is None or run["status"] != "COMPLETED" or not run["results_path"]:
        raise ProposalError(f"{run_id} is not a completed evaluation run in the ledger")
    if not (plan["period_start"] <= run["period_start"] and run["period_end"]
            <= plan["period_end"]):
        raise ProposalError(f"{run_id} covers {run['period_start']}..{run['period_end']}, "
                            f"outside the planned {plan['period_start']}..{plan['period_end']}")
    res = json.loads(Path(run["results_path"]).read_text())
    crit = p["acceptance_criteria"]
    conclusion = res["conclusion"]["conclusion"]
    violations = res["quality"]["rule_compliance"]["candidate_rule_violations"]
    if conclusion in ("PENDING_DATA",) or conclusion == "INCONCLUSIVE":
        result = "INCONCLUSIVE"
    elif conclusion in crit["required_conclusions"] and violations <= crit["max_rule_violations"]:
        result = "PASS"
    else:
        result = "FAIL"
    with lstore.store.transaction():
        lstore.store._insert("proposal_tests", {
            "proposal_id": pid, "ts": now, "kind": "RESULT",
            "period_start": run["period_start"], "period_end": run["period_end"],
            "dataset": run["dataset_id"] or "", "final_set": plan["final_set"],
            "evaluation_run_id": run_id, "result": result,
            "details": _j({"conclusion": conclusion, "reason": res["conclusion"].get("reason"),
                           "rule_violations": violations, "criteria": crit,
                           "results_sha256": run["results_sha256"]})})
    return result


def approve(lstore, pid: str, approver: str, reason: str, confirm: str, now: datetime) -> None:
    p = get(lstore, pid)
    passed = [t for t in p["tests"] if t["kind"] == "RESULT" and t["result"] == "PASS"]
    if not passed:
        raise ProposalError(f"{pid} has no test that passed its predeclared acceptance "
                            "criteria; it cannot be approved")
    _transition(lstore, pid, "APPROVED", approver, reason, now,
                {"passing_tests": [t["evaluation_run_id"] for t in passed]}, confirm)


def deploy(lstore, pid: str, approver: str, reason: str, confirm: str, released_as: str,
           now: datetime) -> None:
    """Record that a person released the approved change as ``released_as`` (a
    new version). Nothing is changed by this call."""
    if not released_as.strip():
        raise ProposalError("deploy needs the new version identifier it was released as")
    _transition(lstore, pid, "DEPLOYED", approver, reason, now,
                {"released_as": released_as,
                 "note": "recorded only; the application never applies changes itself"},
                confirm)


def reject(lstore, pid: str, actor: str, reason: str, now: datetime) -> None:
    _transition(lstore, pid, "REJECTED", actor, reason, now)


def rollback(lstore, pid: str, actor: str, reason: str, confirm: str, now: datetime) -> None:
    _transition(lstore, pid, "ROLLED_BACK", actor, reason, now, confirm=confirm)
