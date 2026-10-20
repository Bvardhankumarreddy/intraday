"""System states and safe degraded modes (Phase 15).

States: NORMAL, DEGRADED_DATA, DEGRADED_AI, PAPER_TRADING_PAUSED, RECOVERY,
MANUAL_REVIEW_REQUIRED.

- The desired state comes from the current findings' ``effect``. Precedence:
  PAPER_TRADING_PAUSED > MANUAL_REVIEW_REQUIRED > DEGRADED_DATA >
  DEGRADED_AI > NORMAL. The arms a finding names are paused (no new paper
  entries). Open positions keep being managed by their deterministic exits.
- Leaving a degraded state goes through RECOVERY. NORMAL is restored only
  after ``monitoring.recovery_passes`` consecutive evaluations without any
  blocking finding: dependencies and data health are re-checked, never
  assumed.
- MANUAL_REVIEW_REQUIRED (performance drift) is sticky until a person clears
  it.
- An override is a person's time-limited, audited decision to resume:
  ``--confirm "OVERRIDE <state>"``, at most ``override_max_minutes``. It never
  overrides a CRITICAL integrity finding (registry or calendar).
- Every transition is appended to ``monitor_state_events`` with reason, actor,
  findings and time. The state is reconstructed from that log after a
  restart.
- Nothing here changes a strategy parameter or promotes anything.
"""

from __future__ import annotations

import getpass
import json
from datetime import datetime, timedelta

from app.monitoring.alerts import MonitorStore, clean

ORDER = ["PAPER_TRADING_PAUSED", "MANUAL_REVIEW_REQUIRED", "DEGRADED_DATA", "DEGRADED_AI",
         "NORMAL"]
UNOVERRIDABLE = ("version.integrity", "calendar.inconsistent", "storage.db")


def current(ms: MonitorStore) -> dict:
    row = ms.one("SELECT * FROM monitor_state_events ORDER BY id DESC LIMIT 1")
    if row is None:
        return {"state": "NORMAL", "since": None, "paused_arms": {}, "reason": "no history"}
    f = json.loads(row["findings"])
    return {"state": row["to_state"], "since": row["ts"], "reason": row["reason"],
            "actor": row["actor"], "paused_arms": f.get("paused_arms", {}),
            "expires_at": row["expires_at"], "override": f.get("override", False)}


def desired(findings: list[dict]) -> tuple[str, dict, list[str]]:
    state, paused, blocking = "NORMAL", {}, []
    for f in findings:
        eff = f.get("effect") or {}
        s = eff.get("state")
        if not s:
            continue
        blocking.append(f["key"])
        if ORDER.index(s) < ORDER.index(state):
            state = s
        if s in ("PAPER_TRADING_PAUSED", "MANUAL_REVIEW_REQUIRED"):
            for arm in eff.get("arms", []):
                paused.setdefault(arm, []).append(f["key"])
    return state, paused, blocking


def _append(ms, now, frm, to, reason, actor, findings, expires=None):
    with ms.store.transaction():
        ms.store._insert("monitor_state_events", {
            "ts": now, "from_state": frm, "to_state": to, "reason": clean(reason, 400),
            "actor": clean(actor, 60), "findings": json.dumps(findings, default=str),
            "expires_at": expires})


def update(ms: MonitorStore, settings, findings: list[dict], now: datetime,
           clean_passes: int) -> dict:
    cur = current(ms)
    want, paused, blocking = desired(findings)
    if cur.get("override") and cur.get("expires_at") and \
            now < datetime.fromisoformat(cur["expires_at"]) and \
            not any(k.startswith(UNOVERRIDABLE) for k in blocking):
        return {**cur, "blocking": blocking}  # a person's override is still in force
    if cur["state"] == "MANUAL_REVIEW_REQUIRED" and want != "PAPER_TRADING_PAUSED":
        want, paused = "MANUAL_REVIEW_REQUIRED", cur["paused_arms"]  # sticky until cleared
    if want == "NORMAL" and cur["state"] not in ("NORMAL",):
        target = "NORMAL" if (cur["state"] == "RECOVERY" and clean_passes
                              >= settings.monitoring.recovery_passes) else "RECOVERY"
        if target != cur["state"]:
            _append(ms, now, cur["state"], target,
                    "dependencies and data checks pass" + (
                        f" ({clean_passes} consecutive clean evaluations)" if target == "NORMAL"
                        else "; verifying before NORMAL"), "monitor",
                    {"blocking": [], "paused_arms": {}})
        return {**current(ms), "blocking": []}
    if want != cur["state"] or paused != cur["paused_arms"]:
        _append(ms, now, cur["state"], want, "; ".join(blocking) or "no blocking finding",
                "monitor", {"blocking": blocking, "paused_arms": paused})
    return {**current(ms), "blocking": blocking}


def override(ms: MonitorStore, settings, state_to: str, actor: str, reason: str, confirm: str,
             minutes: int, now: datetime) -> dict:
    cur = current(ms)
    if confirm != f"OVERRIDE {cur['state']}":
        raise ValueError(f'explicit confirmation required: --confirm "OVERRIDE {cur["state"]}"')
    if not actor.strip() or not reason.strip():
        raise ValueError("an override needs the person's name and a reason")
    if state_to not in ("NORMAL", "DEGRADED_DATA", "DEGRADED_AI"):
        raise ValueError("an override can only resume (NORMAL) or set a degraded mode")
    if minutes > settings.monitoring.override_max_minutes:
        raise ValueError(f"override limited to {settings.monitoring.override_max_minutes} min")
    last = ms.one("SELECT findings FROM monitor_state_events ORDER BY id DESC LIMIT 1")
    blocking = json.loads(last["findings"]).get("blocking", []) if last else []
    if any(k.startswith(UNOVERRIDABLE) for k in blocking):
        raise ValueError("integrity failures (registry, calendar, database) cannot be "
                         "overridden; fix the cause")
    expires = now + timedelta(minutes=minutes)
    _append(ms, now, cur["state"], state_to, reason, f"{actor} (os user {getpass.getuser()})",
            {"override": True, "paused_arms": {}, "blocking": blocking}, expires.isoformat())
    return current(ms)


def clear_review(ms: MonitorStore, actor: str, reason: str, confirm: str,
                 now: datetime) -> dict:
    cur = current(ms)
    if cur["state"] != "MANUAL_REVIEW_REQUIRED":
        raise ValueError(f"state is {cur['state']}, not MANUAL_REVIEW_REQUIRED")
    if confirm != "CLEAR MANUAL_REVIEW_REQUIRED" or not actor.strip() or not reason.strip():
        raise ValueError('a person, a reason and --confirm "CLEAR MANUAL_REVIEW_REQUIRED" '
                         "are required")
    _append(ms, now, cur["state"], "RECOVERY", f"review completed: {reason}",
            f"{actor} (os user {getpass.getuser()})", {"paused_arms": {}, "blocking": []})
    return current(ms)


def paused_arms(ms: MonitorStore, now: datetime) -> dict:
    cur = current(ms)
    if cur.get("override") and cur.get("expires_at") and \
            now < datetime.fromisoformat(cur["expires_at"]):
        return {}
    return cur.get("paused_arms", {}) if cur["state"] in ("PAPER_TRADING_PAUSED",
                                                          "MANUAL_REVIEW_REQUIRED") else {}
