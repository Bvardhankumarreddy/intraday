"""Version and experiment registry with a hash-chained audit log (Phase 14).

Tables (``experiments.registry_db``):
- ``versions``: immutable, content-addressed AI-system versions (prompt text
  and version, model configuration, tool allow-list, agent limits,
  settings overrides). ``version_id`` is derived from the SHA-256 of the
  components.
- ``experiments``: experiment definitions. The content is frozen (trigger).
  ``status`` is a cached copy of the event log, never the authority.
- ``registry_events``: an append-only, hash-chained log
  (``hash = sha256(prev_hash + event)``) of every registry action: version
  registration, experiment creation, validation results and transitions.

Authorization comes from the chain, not from a flag. ``verify()`` recomputes
every hash, checks that each status equals its last event, and checks that
every human transition carries the person's name and the exact confirmation
phrase. The ACTIVE version of a context is the one named by the latest
verified ACTIVATED or ROLLED_BACK event. A direct edit of a status or a
version is detected, and ``active_version`` then refuses to answer
(``IntegrityError``). The integrated runner refuses to start the AI arm in
that case.

Promotion state machine:

    DRAFT → VALIDATING → VALIDATED → AWAITING_APPROVAL → APPROVED* → SHADOW → PAPER_CANARY → ACTIVE*
      └──────────┴────────────┴──────────────┴──────────────┴───────────┴──────────┴──→ REJECTED
    ACTIVE → ROLLED_BACK* (the previous approved version becomes active again)
    ACTIVE → SUPERSEDED (when another version is activated)

    * = a named person + reason + "--confirm '<ACTION> <experiment_id>'".
    VALIDATED requires a PASS classification; FAIL, INCONCLUSIVE and INVALID
    go to REJECTED automatically, with the evidence kept.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from app.paper.store import PaperStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    version_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    label TEXT NOT NULL,
    components TEXT NOT NULL,
    sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    name TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    reason TEXT NOT NULL,
    context TEXT NOT NULL,
    baseline_version TEXT NOT NULL,
    candidate_version TEXT NOT NULL,
    spec TEXT NOT NULL,
    acceptance_version TEXT NOT NULL,
    acceptance_sha256 TEXT NOT NULL,
    acceptance_text TEXT NOT NULL,
    code_versions TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS registry_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS versions_frozen BEFORE UPDATE ON versions
BEGIN SELECT RAISE(ABORT, 'versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS versions_kept BEFORE DELETE ON versions
BEGIN SELECT RAISE(ABORT, 'versions are kept'); END;
CREATE TRIGGER IF NOT EXISTS experiments_content BEFORE UPDATE ON experiments
WHEN NEW.experiment_id IS NOT OLD.experiment_id OR NEW.spec IS NOT OLD.spec
  OR NEW.hypothesis IS NOT OLD.hypothesis OR NEW.baseline_version IS NOT OLD.baseline_version
  OR NEW.candidate_version IS NOT OLD.candidate_version
  OR NEW.acceptance_sha256 IS NOT OLD.acceptance_sha256
  OR NEW.acceptance_text IS NOT OLD.acceptance_text OR NEW.code_versions IS NOT OLD.code_versions
  OR NEW.created_at IS NOT OLD.created_at OR NEW.context IS NOT OLD.context
BEGIN SELECT RAISE(ABORT, 'experiment content is immutable'); END;
CREATE TRIGGER IF NOT EXISTS experiments_kept BEFORE DELETE ON experiments
BEGIN SELECT RAISE(ABORT, 'experiments are kept, including rejected ones'); END;
CREATE TRIGGER IF NOT EXISTS events_frozen BEFORE UPDATE ON registry_events
BEGIN SELECT RAISE(ABORT, 'the registry log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_kept BEFORE DELETE ON registry_events
BEGIN SELECT RAISE(ABORT, 'the registry log is append-only'); END;
"""

GENESIS = "0" * 64
TRANSITIONS = {
    "DRAFT": {"VALIDATING", "REJECTED"},
    "VALIDATING": {"VALIDATED", "REJECTED"},
    "VALIDATED": {"AWAITING_APPROVAL", "REJECTED"},
    "AWAITING_APPROVAL": {"APPROVED", "REJECTED"},
    "APPROVED": {"SHADOW", "REJECTED"},
    "SHADOW": {"PAPER_CANARY", "REJECTED"},
    "PAPER_CANARY": {"ACTIVE", "REJECTED"},
    "ACTIVE": {"ROLLED_BACK", "SUPERSEDED"},
    "REJECTED": set(), "ROLLED_BACK": set(), "SUPERSEDED": set(),
}
HUMAN = {"APPROVED": "APPROVE", "ACTIVE": "ACTIVATE", "ROLLED_BACK": "ROLLBACK"}
MACHINE_ACTORS = {"system", "auto", "validator", "monitor", "learning-engine"}


class RegistryError(RuntimeError):
    pass


class IntegrityError(RegistryError):
    pass


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Registry:
    def __init__(self, path: Path | str):
        self.store = PaperStore(path)
        self.store.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.store.close()

    def all(self, sql, params=()) -> list[dict]:
        return [dict(r) for r in self.store.all(sql, params)]

    def one(self, sql, params=()):
        r = self.store.one(sql, params)
        return dict(r) if r else None

    # -- chain -------------------------------------------------------------------------
    def _append(self, kind: str, subject: str, actor: str, reason: str, evidence: dict,
                now: datetime, from_status=None, to_status=None) -> str:
        last = self.one("SELECT hash FROM registry_events ORDER BY seq DESC LIMIT 1")
        prev = last["hash"] if last else GENESIS
        body = {"ts": now.isoformat(), "kind": kind, "subject": subject,
                "from_status": from_status, "to_status": to_status, "actor": actor,
                "reason": reason, "evidence": evidence}
        h = sha(prev + canonical(body))
        self.store._insert("registry_events", {**body, "evidence": canonical(evidence),
                                      "prev_hash": prev, "hash": h})
        return h

    def verify(self) -> list[str]:
        """Return integrity problems (empty = the registry can be trusted)."""
        problems, prev = [], GENESIS
        last_status: dict[str, str] = {}
        for e in self.all("SELECT * FROM registry_events ORDER BY seq"):
            body = {k: e[k] for k in ("ts", "kind", "subject", "from_status", "to_status",
                                      "actor", "reason")}
            body["evidence"] = json.loads(e["evidence"])
            if e["prev_hash"] != prev or sha(prev + canonical(body)) != e["hash"]:
                problems.append(f"event {e['seq']} breaks the hash chain")
            prev = e["hash"]
            if e["kind"] == "TRANSITION":
                if e["to_status"] in HUMAN:
                    phrase = f"{HUMAN[e['to_status']]} {e['subject']}"
                    if body["evidence"].get("confirm") != phrase or \
                            e["actor"].lower() in MACHINE_ACTORS:
                        problems.append(f"event {e['seq']}: {e['to_status']} without a "
                                        "person's explicit confirmation")
                last_status[e["subject"]] = e["to_status"]
            elif e["kind"] == "EXPERIMENT_CREATED":
                last_status[e["subject"]] = "DRAFT"
            elif e["kind"] in ("ACTIVATED", "ROLLED_BACK_TO"):
                ev = body["evidence"]
                ok = (ev.get("confirm") == f"INIT {ev.get('context')}" if ev.get("previous") is
                      None and e["kind"] == "ACTIVATED" and not ev.get("experiment") else
                      ev.get("confirm") == f"{'ACTIVATE' if e['kind'] == 'ACTIVATED' else 'ROLLBACK'}"
                                           f" {ev.get('experiment')}")
                if not ok or e["actor"].lower() in MACHINE_ACTORS:
                    problems.append(f"event {e['seq']}: {e['kind']} of {e['subject']} without a "
                                    "person's explicit confirmation")
        for x in self.all("SELECT experiment_id, status FROM experiments"):
            if last_status.get(x["experiment_id"]) != x["status"]:
                problems.append(f"{x['experiment_id']}: status {x['status']} does not match its "
                                f"event log ({last_status.get(x['experiment_id'])})")
        for v in self.all("SELECT * FROM versions"):
            if sha(v["components"]) != v["sha256"] or not v["version_id"].endswith(
                    v["sha256"][:12]):
                problems.append(f"version {v['version_id']} content does not match its hash")
        return problems

    def require_valid(self) -> None:
        problems = self.verify()
        if problems:
            raise IntegrityError("registry integrity check failed: " + "; ".join(problems[:5]))

    # -- versions ------------------------------------------------------------------------
    def add_version(self, label: str, components: dict, actor: str, now: datetime) -> str:
        text = canonical(components)
        digest = sha(text)
        vid = f"V-{digest[:12]}"
        with self.store.transaction():
            if self.one("SELECT 1 FROM versions WHERE version_id = ?", (vid,)) is None:
                self.store._insert("versions", {"version_id": vid, "created_at": now,
                                                "label": label, "components": text,
                                                "sha256": digest})
                self._append("VERSION_REGISTERED", vid, actor, label, {"sha256": digest}, now)
        return vid

    def version(self, vid: str) -> dict:
        v = self.one("SELECT * FROM versions WHERE version_id = ?", (vid,))
        if v is None:
            raise RegistryError(f"unknown version {vid}")
        v["components"] = json.loads(v["components"])
        return v

    def bootstrap(self, context: str, components: dict, actor: str, confirm: str,
                  now: datetime) -> str:
        """Register the CURRENT system as the first approved, active version of a
        context. A person must confirm; it can be done only once per context."""
        if confirm != f"INIT {context}":
            raise RegistryError(f'explicit confirmation required: --confirm "INIT {context}"')
        if actor.lower() in MACHINE_ACTORS or not actor.strip():
            raise RegistryError("the initial version must be registered by a person")
        if self._active_events(context):
            raise RegistryError(f"context {context} already has an active version")
        with self.store.transaction():
            vid = self.add_version("initial approved system", components, actor, now)
            self._append("ACTIVATED", vid, actor, "initial approved version",
                         {"context": context, "confirm": confirm, "previous": None}, now)
        return vid

    def _active_events(self, context: str) -> list[dict]:
        return [e for e in self.all("SELECT * FROM registry_events WHERE kind IN ('ACTIVATED', "
                                    "'ROLLED_BACK_TO') ORDER BY seq")
                if json.loads(e["evidence"]).get("context") == context]

    def active_version(self, context: str) -> str | None:
        self.require_valid()
        events = self._active_events(context)
        return events[-1]["subject"] if events else None

    def history(self, context: str) -> list[dict]:
        return [{"seq": e["seq"], "ts": e["ts"], "kind": e["kind"], "version": e["subject"],
                 "actor": e["actor"], "reason": e["reason"],
                 **{k: v for k, v in json.loads(e["evidence"]).items() if k != "confirm"}}
                for e in self._active_events(context)]

    # -- experiments -----------------------------------------------------------------------
    def create_experiment(self, row: dict, actor: str, now: datetime) -> str:
        eid = f"EXP-{now:%Y%m%d}-{sha(canonical(row))[:8]}"
        with self.store.transaction():
            self.store._insert("experiments", {**row, "experiment_id": eid, "created_at": now,
                                               "status": "DRAFT"})
            self._append("EXPERIMENT_CREATED", eid, actor, row["name"],
                         {"candidate": row["candidate_version"],
                          "baseline": row["baseline_version"]}, now)
        return eid

    def experiment(self, eid: str) -> dict:
        x = self.one("SELECT * FROM experiments WHERE experiment_id = ?", (eid,))
        if x is None:
            raise RegistryError(f"unknown experiment {eid}")
        for k in ("spec", "code_versions"):
            x[k] = json.loads(x[k])
        x["events"] = [{**e, "evidence": json.loads(e["evidence"])} for e in self.all(
            "SELECT * FROM registry_events WHERE subject = ? ORDER BY seq", (eid,))]
        return x

    def record(self, eid: str, kind: str, actor: str, reason: str, evidence: dict,
               now: datetime) -> None:
        with self.store.transaction():
            self._append(kind, eid, actor, reason, evidence, now)

    def transition(self, eid: str, to: str, actor: str, reason: str, now: datetime,
                   evidence: dict | None = None, confirm: str | None = None) -> None:
        self.require_valid()
        x = self.experiment(eid)
        if to not in TRANSITIONS[x["status"]]:
            raise RegistryError(f"{eid}: {x['status']} -> {to} is not allowed")
        if not actor.strip() or not reason.strip():
            raise RegistryError("every transition needs the actor and a reason")
        ev = dict(evidence or {})
        if to in HUMAN:
            phrase = f"{HUMAN[to]} {eid}"
            if confirm != phrase:
                raise RegistryError(f'explicit human confirmation required: --confirm "{phrase}"')
            if actor.lower() in MACHINE_ACTORS:
                raise RegistryError(f"{to} must be decided by a person, not '{actor}'")
            ev["confirm"] = confirm
        with self.store.transaction():
            cur = self.store.conn.execute(
                "UPDATE experiments SET status = ? WHERE experiment_id = ? AND status = ?",
                (to, eid, x["status"]))
            if cur.rowcount != 1:
                raise RegistryError(f"{eid} changed concurrently")
            self._append("TRANSITION", eid, actor, reason, ev, now, x["status"], to)
            if to == "ACTIVE":
                prev = self.active_version_unchecked(x["context"])
                if prev:
                    owner = self.one("SELECT experiment_id FROM experiments WHERE "
                                     "candidate_version = ? AND status = 'ACTIVE' AND "
                                     "experiment_id != ?", (prev, eid))
                    if owner:
                        self.store.conn.execute(
                            "UPDATE experiments SET status = 'SUPERSEDED' WHERE "
                            "experiment_id = ?", (owner["experiment_id"],))
                        self._append("TRANSITION", owner["experiment_id"], actor,
                                     f"superseded by {eid}", {}, now, "ACTIVE", "SUPERSEDED")
                self._append("ACTIVATED", x["candidate_version"], actor, reason,
                             {"context": x["context"], "confirm": confirm, "previous": prev,
                              "experiment": eid}, now)
            if to == "ROLLED_BACK":
                restore = ev.get("restore_version")
                self._append("ROLLED_BACK_TO", restore, actor, reason,
                             {"context": x["context"], "confirm": confirm,
                              "from_version": x["candidate_version"], "experiment": eid}, now)

    def active_version_unchecked(self, context: str) -> str | None:
        events = self._active_events(context)
        return events[-1]["subject"] if events else None
