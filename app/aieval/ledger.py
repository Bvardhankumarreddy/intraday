"""Immutable evaluation ledger (Phase 11).

Tables (their own SQLite file, ``ai_eval.ledger_db``, opened through
``PaperStore`` for WAL, private permissions and transactions):

- ``aieval_runs``: one row per evaluation run. ``run_key`` is UNIQUE (mode,
  split, period, dataset, protocol and AI versions), so the same evaluation
  is never processed twice. Status and the result's hash are set when the
  run finishes. That is the only UPDATE the ledger allows.
- ``aieval_decisions``: every decision of either system for every signal: the
  deterministic baseline (A) and the AI-assisted system (B). Each row holds
  the canonical JSON and its SHA-256. UNIQUE (run_id, system, signal_id)
  prevents duplicate records. Triggers abort any UPDATE or DELETE.
- ``aieval_outcomes``: what happened afterwards (trade, not filled, vetoed,
  per-share price path), APPENDED with a reference to the decision. They are
  also immutable, and an outcome never changes its decision.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from app.paper.store import PaperStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS aieval_runs (
    run_id TEXT PRIMARY KEY,
    run_key TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL,              -- HISTORICAL_REPLAY | PROSPECTIVE
    split TEXT,                      -- development | validation | holdout (replay)
    period_start TEXT,
    period_end TEXT,
    dataset_id TEXT,
    protocol_version TEXT NOT NULL,
    protocol_sha256 TEXT NOT NULL,
    baseline_name TEXT NOT NULL,
    baseline_fingerprint TEXT NOT NULL,
    ai_versions TEXT NOT NULL,
    labels TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,            -- RUNNING | COMPLETED | PENDING_DATA | REFUSED | FAILED
    reason TEXT,
    conclusion TEXT,
    results_path TEXT,
    results_sha256 TEXT
);
CREATE TABLE IF NOT EXISTS aieval_decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    system TEXT NOT NULL,            -- BASELINE | AI_ASSISTED
    signal_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    decision_time TEXT NOT NULL,
    market_date TEXT NOT NULL,
    final_decision TEXT NOT NULL,
    body TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    UNIQUE (run_id, system, signal_id)
);
CREATE TABLE IF NOT EXISTS aieval_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    body TEXT NOT NULL,
    UNIQUE (decision_id, kind)
);
CREATE TRIGGER IF NOT EXISTS aieval_dec_no_update BEFORE UPDATE ON aieval_decisions
BEGIN SELECT RAISE(ABORT, 'evaluation decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS aieval_dec_no_delete BEFORE DELETE ON aieval_decisions
BEGIN SELECT RAISE(ABORT, 'evaluation decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS aieval_out_no_update BEFORE UPDATE ON aieval_outcomes
BEGIN SELECT RAISE(ABORT, 'evaluation outcomes are append-only'); END;
CREATE TRIGGER IF NOT EXISTS aieval_out_no_delete BEFORE DELETE ON aieval_outcomes
BEGIN SELECT RAISE(ABORT, 'evaluation outcomes are append-only'); END;
CREATE TRIGGER IF NOT EXISTS aieval_run_no_delete BEFORE DELETE ON aieval_runs
BEGIN SELECT RAISE(ABORT, 'evaluation runs are kept'); END;
CREATE TRIGGER IF NOT EXISTS aieval_run_final BEFORE UPDATE ON aieval_runs
WHEN OLD.status != 'RUNNING'
BEGIN SELECT RAISE(ABORT, 'a finished evaluation run is final'); END;
"""


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class DuplicateRun(RuntimeError):
    pass


class Ledger:
    def __init__(self, path: Path | str):
        self.store = PaperStore(path)
        self.store.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.store.close()

    # -- runs -----------------------------------------------------------------------
    def start_run(self, row: dict) -> None:
        with self.store.transaction():
            if not self.store._insert("aieval_runs", {**row, "status": "RUNNING"},
                                      or_ignore=True):
                prior = self.store.one("SELECT run_id, status FROM aieval_runs WHERE "
                                       "run_key = ?", (row["run_key"],))
                raise DuplicateRun(f"this evaluation already exists as {prior['run_id']} "
                                   f"({prior['status']}); it is never processed twice")

    def finish_run(self, run_id: str, status: str, now: datetime, reason: str | None = None,
                   conclusion: str | None = None, results_path: str | None = None,
                   results_sha256: str | None = None) -> None:
        with self.store.transaction():
            self.store.conn.execute(
                "UPDATE aieval_runs SET status = ?, finished_at = ?, reason = ?, conclusion = ?, "
                "results_path = ?, results_sha256 = ? WHERE run_id = ?",
                (status, now.isoformat(), reason, conclusion, results_path, results_sha256,
                 run_id))

    def run(self, run_id: str) -> dict | None:
        row = self.store.one("SELECT * FROM aieval_runs WHERE run_id = ?", (run_id,))
        return dict(row) if row else None

    def runs(self) -> list[dict]:
        return [dict(r) for r in self.store.all("SELECT * FROM aieval_runs ORDER BY started_at")]

    def holdout_runs(self, protocol_sha: str, dataset_id: str) -> list[dict]:
        return [dict(r) for r in self.store.all(
            "SELECT * FROM aieval_runs WHERE split = 'holdout' AND protocol_sha256 = ? AND "
            "dataset_id = ? AND status != 'REFUSED'", (protocol_sha, dataset_id))]

    # -- decisions and outcomes ---------------------------------------------------------
    def add_decision(self, run_id: str, system: str, body: dict) -> str:
        text = canonical(body)
        did = "D-" + sha(f"{run_id}|{system}|{body['signal_id']}")[:16]
        with self.store.transaction():
            self.store._insert("aieval_decisions", {
                "decision_id": did, "run_id": run_id, "system": system,
                "signal_id": body["signal_id"], "symbol": body["symbol"],
                "decision_time": body["decision_time"], "market_date": body["market_date"],
                "final_decision": body["final_decision"], "body": text, "sha256": sha(text)},
                or_ignore=True)
        return did

    def add_outcome(self, decision_id: str, kind: str, now: datetime, body: dict) -> bool:
        with self.store.transaction():
            return self.store._insert("aieval_outcomes", {
                "decision_id": decision_id, "kind": kind, "recorded_at": now.isoformat(),
                "body": canonical(body)}, or_ignore=True)

    def decisions(self, run_id: str) -> list[dict]:
        rows = self.store.all("SELECT * FROM aieval_decisions WHERE run_id = ? ORDER BY "
                              "decision_time, symbol, system", (run_id,))
        out = []
        for r in rows:
            d = dict(r)
            d["integrity_ok"] = sha(d["body"]) == d["sha256"]
            d["body"] = json.loads(d["body"])
            out.append(d)
        return out

    def outcomes(self, run_id: str) -> list[dict]:
        rows = self.store.all(
            "SELECT o.* FROM aieval_outcomes o JOIN aieval_decisions d USING (decision_id) "
            "WHERE d.run_id = ? ORDER BY o.id", (run_id,))
        return [{**dict(r), "body": json.loads(r["body"])} for r in rows]
