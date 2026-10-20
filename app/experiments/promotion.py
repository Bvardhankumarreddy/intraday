"""Controlled promotion and rollback (Phase 14).

Each step checks its evidence before the registry records the transition:

| Step | From → To | Who | Requires |
|---|---|---|---|
| request | VALIDATED → AWAITING_APPROVAL | person | validation + final set PASS (already enforced by VALIDATED) |
| approve | AWAITING_APPROVAL → APPROVED | person + "APPROVE <id>" | — |
| shadow | APPROVED → SHADOW | person | the candidate does not change deterministic strategy settings |
| canary | SHADOW → PAPER_CANARY | person | ≥ ``min_shadow_sessions`` sessions of shadow research, without model failure in most runs |
| activate | PAPER_CANARY → ACTIVE | person + "ACTIVATE <id>" | ``canary_sessions`` COMPLETED canary sessions; canary net ≥ approved AI arm − shortfall; 0 rule violations |
| rollback | ACTIVE → ROLLED_BACK | person + "ROLLBACK <id>" | restores the version that was active before |
| reject | any pre-active → REJECTED | person | — |

A database flag alone never activates anything: ``runtime.resolve`` reads the
verified hash chain. Rollback never deletes history.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.experiments.candidate import changes_deterministic_strategy
from app.experiments.registry import Registry, RegistryError


def request_approval(reg: Registry, eid: str, actor: str, reason: str, now: datetime) -> None:
    reg.transition(eid, "AWAITING_APPROVAL", actor, reason, now)


def approve(reg: Registry, eid: str, actor: str, reason: str, confirm: str,
            now: datetime) -> None:
    reg.transition(eid, "APPROVED", actor, reason, now, confirm=confirm)


def start_shadow(reg: Registry, eid: str, actor: str, reason: str, now: datetime) -> None:
    x = reg.experiment(eid)
    comp = reg.version(x["candidate_version"])["components"]
    if changes_deterministic_strategy(comp):
        raise RegistryError("this candidate changes deterministic strategy settings: it can be "
                            "validated and approved, but deploying it needs a new frozen "
                            "baseline (Phase 7), not a runtime switch")
    if reg.all("SELECT 1 FROM experiments WHERE context = ? AND status = 'SHADOW'",
               (x["context"],)):
        raise RegistryError("another experiment is already in SHADOW")
    reg.transition(eid, "SHADOW", actor, reason, now)


def _shadow_evidence(settings, version_comp: dict, since: str) -> dict:
    from app.paper.store import PaperStore

    path = Path(settings.experiments.shadow_db_path)
    if not path.exists():
        return {"sessions": 0, "runs": 0, "failed_runs": 0}
    st = PaperStore(path)
    try:
        runs = [dict(r) for r in st.all(
            "SELECT market_date, status, model FROM agent_runs WHERE window = 'intraday' AND "
            "prompt_version = ? AND started_at >= ?", (version_comp["prompt"]["version"], since))]
        errors = st.all("SELECT COUNT(*) AS n FROM agent_model_calls m JOIN agent_runs r "
                        "USING (run_id) WHERE m.error_kind IS NOT NULL AND r.prompt_version = ? "
                        "AND r.started_at >= ?", (version_comp["prompt"]["version"], since))
    except Exception:  # noqa: BLE001
        runs, errors = [], [{"n": 0}]
    finally:
        st.close()
    return {"sessions": len({r["market_date"] for r in runs}), "runs": len(runs),
            "failed_runs": sum(1 for r in runs if r["status"] == "FAILED"),
            "model_errors": errors[0]["n"]}


def start_canary(reg: Registry, settings, eid: str, actor: str, reason: str,
                 now: datetime) -> dict:
    import yaml

    x = reg.experiment(eid)
    if x["status"] != "SHADOW":
        raise RegistryError(f"{eid} is {x['status']}; the paper canary follows SHADOW")
    acc = yaml.safe_load(x["acceptance_text"])
    since = next(e["ts"] for e in reversed(x["events"]) if e.get("to_status") == "SHADOW")
    ev = _shadow_evidence(settings, reg.version(x["candidate_version"])["components"], since)
    need = acc["prospective"]["min_shadow_sessions"]
    if ev["sessions"] < need:
        raise RegistryError(f"{ev['sessions']} shadow session(s) recorded; {need} required")
    if ev["runs"] and ev["failed_runs"] > ev["runs"] / 2:
        raise RegistryError(f"shadow runs mostly failed ({ev['failed_runs']}/{ev['runs']})")
    if reg.all("SELECT 1 FROM experiments WHERE context = ? AND status = 'PAPER_CANARY'",
               (x["context"],)):
        raise RegistryError("another experiment is already in PAPER_CANARY")
    reg.transition(eid, "PAPER_CANARY", actor, reason, now, {"shadow": ev})
    return ev


def canary_evidence(settings, since: str) -> dict:
    """Canary arm vs the approved AI arm over the canary's COMPLETED sessions."""
    from app.costs.money import D
    from app.paper.store import PaperStore

    out = {"sessions": 0, "canary_net": None, "approved_net": None, "rule_violations": None}
    cpath, bpath = Path(settings.experiments.canary_db_path), Path(settings.integrated.ai_db_path)
    if not cpath.exists() or not bpath.exists():
        return out
    c, b = PaperStore(cpath), PaperStore(bpath)
    try:
        days = [r["trading_date"] for r in c.all(
            "SELECT trading_date FROM sessions WHERE state = 'CLOSED' AND started_at >= ?",
            (since,))]
        if not days:
            return out
        marks = ", ".join("?" for _ in days)

        def net(store):
            rows = store.all(f"SELECT net_pnl FROM positions WHERE state = 'CLOSED' AND "
                             f"trading_date IN ({marks})", tuple(days))
            return float(sum((D(r["net_pnl"]) for r in rows), D(0)))

        viol = c.all(f"SELECT COUNT(*) AS n FROM agent_recommendations WHERE status = "
                     f"'CANDIDATE' AND market_date IN ({marks}) AND json_extract(body, "
                     f"'$.risk_status') != 'QUALIFIED_FOR_REVIEW'", tuple(days))
        out.update(sessions=len(days), canary_net=round(net(c), 2),
                   approved_net=round(net(b), 2), rule_violations=viol[0]["n"], days=days)
    finally:
        c.close()
        b.close()
    return out


def activate(reg: Registry, settings, eid: str, actor: str, reason: str, confirm: str,
             now: datetime) -> dict:
    import yaml

    x = reg.experiment(eid)
    if x["status"] != "PAPER_CANARY":
        raise RegistryError(f"{eid} is {x['status']}; only a PAPER_CANARY experiment can be "
                            "activated")
    acc = yaml.safe_load(x["acceptance_text"])["prospective"]
    since = next(e["ts"] for e in reversed(x["events"]) if e.get("to_status") == "PAPER_CANARY")
    ev = canary_evidence(settings, since)
    if ev["sessions"] < acc["canary_sessions"]:
        raise RegistryError(f"{ev['sessions']} completed canary session(s); "
                            f"{acc['canary_sessions']} required")
    if ev["rule_violations"]:
        raise RegistryError(f"canary recorded {ev['rule_violations']} rule violation(s)")
    if ev["canary_net"] < ev["approved_net"] - acc["canary_max_net_shortfall_inr"]:
        raise RegistryError(f"canary net Rs {ev['canary_net']} trails the approved arm (Rs "
                            f"{ev['approved_net']}) by more than Rs "
                            f"{acc['canary_max_net_shortfall_inr']}")
    reg.transition(eid, "ACTIVE", actor, reason, now, {"canary": ev}, confirm)
    return ev


def rollback(reg: Registry, eid: str, actor: str, reason: str, confirm: str,
             now: datetime) -> str:
    x = reg.experiment(eid)
    if x["status"] != "ACTIVE":
        raise RegistryError(f"{eid} is {x['status']}; only the active experiment can be rolled "
                            "back")
    act = [e for e in x["events"] if e["kind"] == "TRANSITION" and e["to_status"] == "ACTIVE"]
    activation = reg.all("SELECT evidence FROM registry_events WHERE kind = 'ACTIVATED' AND subject = ? "
                         "ORDER BY seq DESC LIMIT 1", (x["candidate_version"],))
    previous = json.loads(activation[0]["evidence"]).get("previous") if activation else None
    if not act or previous is None:
        raise RegistryError("no earlier approved version to restore")
    reg.transition(eid, "ROLLED_BACK", actor, reason, now, {"restore_version": previous},
                   confirm)
    return previous


def reject(reg: Registry, eid: str, actor: str, reason: str, now: datetime) -> None:
    reg.transition(eid, "REJECTED", actor, reason, now)
