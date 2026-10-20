"""Append-only persistence for the research agent (tables in the paper DB).

- agent_runs: one row per research run. ``run_key`` (date, window, slot,
  attempt) is UNIQUE, so the same evaluation window can't run twice.
- agent_tool_calls / agent_model_calls: full audit of every tool execution
  and model request (latency, tokens, cost, raw response, errors).
- agent_recommendations: the ORIGINAL recommendations. SQLite triggers
  abort any UPDATE or DELETE, so nothing (including future code) can rewrite
  a recommendation after its outcome is known. Each row stores the canonical
  JSON and its SHA-256.
- agent_outcomes: later facts (expiry, price path, paper result) APPENDED
  with a reference to the recommendation. Also immutable.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

AGENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    run_key TEXT NOT NULL UNIQUE,
    market_date TEXT NOT NULL,
    window TEXT NOT NULL,
    slot TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    llm_provider TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT NOT NULL,
    input_fingerprint TEXT,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS agent_tool_calls (
    run_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    args TEXT NOT NULL,
    ok INTEGER NOT NULL,
    output TEXT,
    error TEXT,
    untrusted_flags TEXT,
    started_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    PRIMARY KEY (run_id, call_id)
);
CREATE TABLE IF NOT EXISTS agent_model_calls (
    run_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    latency_seconds REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    attempts INTEGER,
    stop_reason TEXT,
    request_id TEXT,
    error_kind TEXT,
    error TEXT,
    raw_response TEXT,
    PRIMARY KEY (run_id, call_id)
);
CREATE TABLE IF NOT EXISTS agent_recommendations (
    recommendation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    market_date TEXT NOT NULL,
    window TEXT NOT NULL,
    symbol TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    valid_until TEXT,
    schema_version TEXT NOT NULL,
    body TEXT NOT NULL,
    sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    body TEXT NOT NULL,
    UNIQUE (recommendation_id, kind)
);
CREATE TRIGGER IF NOT EXISTS agent_rec_no_update BEFORE UPDATE ON agent_recommendations
BEGIN SELECT RAISE(ABORT, 'agent recommendations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agent_rec_no_delete BEFORE DELETE ON agent_recommendations
BEGIN SELECT RAISE(ABORT, 'agent recommendations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS agent_out_no_update BEFORE UPDATE ON agent_outcomes
BEGIN SELECT RAISE(ABORT, 'agent outcomes are append-only'); END;
CREATE TRIGGER IF NOT EXISTS agent_out_no_delete BEFORE DELETE ON agent_outcomes
BEGIN SELECT RAISE(ABORT, 'agent outcomes are append-only'); END;
"""


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


class AgentStore:
    def __init__(self, store):
        self.store = store
        store.conn.executescript(AGENT_SCHEMA)

    # -- runs -------------------------------------------------------------------
    def latest_run(self, market_date: str, window: str, slot: str):
        return self.store.one("SELECT * FROM agent_runs WHERE market_date = ? AND window = ? "
                              "AND slot = ? ORDER BY started_at DESC, run_key DESC LIMIT 1",
                              (market_date, window, slot))

    def previous_fingerprint(self, market_date: str, window: str):
        row = self.store.one("SELECT input_fingerprint FROM agent_runs WHERE market_date = ? "
                             "AND window = ? AND status = 'COMPLETED' ORDER BY started_at DESC "
                             "LIMIT 1", (market_date, window))
        return row["input_fingerprint"] if row else None

    def start_run(self, row: dict) -> bool:
        with self.store.transaction():
            return self.store._insert("agent_runs", row, or_ignore=True)

    def finish_run(self, run_id: str, status: str, now: datetime, reason: str | None,
                   summary: dict, fingerprint: str | None = None, model: str | None = None):
        with self.store.transaction():
            self.store.conn.execute(
                "UPDATE agent_runs SET status = ?, finished_at = ?, reason = ?, summary = ?, "
                "input_fingerprint = COALESCE(?, input_fingerprint), model = COALESCE(?, model) "
                "WHERE run_id = ?", (status, now.isoformat(), reason, canonical(summary),
                                     fingerprint, model, run_id))

    # -- audit ----------------------------------------------------------------------
    def record_tool(self, run_id: str, call) -> None:
        with self.store.transaction():
            self.store._insert("agent_tool_calls", {
                "run_id": run_id, "call_id": call.id, "tool": call.tool,
                "requested_by": call.requested_by, "args": canonical(call.args),
                "ok": int(call.ok), "output": canonical(call.output) if call.output else None,
                "error": call.error, "untrusted_flags": canonical(call.untrusted_flags),
                "started_at": call.started_at, "duration_ms": call.duration_ms},
                or_ignore=True)

    def record_model_call(self, row: dict) -> None:
        with self.store.transaction():
            self.store._insert("agent_model_calls", row, or_ignore=True)

    # -- recommendations ---------------------------------------------------------------
    def save_recommendation(self, rec) -> bool:
        body = rec.model_dump(mode="json")
        text = canonical(body)
        with self.store.transaction():
            return self.store._insert("agent_recommendations", {
                "recommendation_id": rec.recommendation_id, "run_id": rec.run_id,
                "market_date": rec.market_date, "window": rec.window, "symbol": rec.symbol,
                "status": rec.status.value, "created_at": rec.created_at,
                "valid_until": rec.valid_until, "schema_version": rec.schema_version,
                "body": text, "sha256": hashlib.sha256(text.encode()).hexdigest()},
                or_ignore=True)

    def recommendations(self, market_date: str | None = None, run_id: str | None = None):
        sql, params = "SELECT * FROM agent_recommendations WHERE 1=1", []
        if market_date:
            sql += " AND market_date = ?"
            params.append(market_date)
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        return [dict(r) for r in self.store.all(sql + " ORDER BY created_at, symbol",
                                                tuple(params))]

    def verify(self, row: dict) -> bool:
        return hashlib.sha256(row["body"].encode()).hexdigest() == row["sha256"]

    def recent(self, since: str, until: str, symbol: str | None) -> list[dict]:
        sql = ("SELECT recommendation_id, market_date, window, symbol, status, created_at "
               "FROM agent_recommendations WHERE market_date >= ? AND created_at <= ?")
        params: list = [since, until]
        if symbol:
            sql += " AND symbol = ?"
            params.append(symbol)
        rows = [dict(r) for r in self.store.all(sql + " ORDER BY created_at DESC", tuple(params))]
        for r in rows:
            r["outcomes"] = [o["kind"] for o in self.outcomes(r["recommendation_id"])
                             if o["recorded_at"] <= until]
        return rows

    # -- outcomes ------------------------------------------------------------------------
    def append_outcome(self, rec_id: str, kind: str, now: datetime, body: dict) -> bool:
        with self.store.transaction():
            return self.store._insert("agent_outcomes", {
                "recommendation_id": rec_id, "kind": kind, "recorded_at": now,
                "body": canonical(body)}, or_ignore=True)

    def outcomes(self, rec_id: str) -> list[dict]:
        return [dict(r) for r in self.store.all(
            "SELECT * FROM agent_outcomes WHERE recommendation_id = ? ORDER BY id", (rec_id,))]

    def runs(self, market_date: str | None = None) -> list[dict]:
        if market_date:
            return [dict(r) for r in self.store.all(
                "SELECT * FROM agent_runs WHERE market_date = ? ORDER BY started_at",
                (market_date,))]
        return [dict(r) for r in self.store.all("SELECT * FROM agent_runs ORDER BY started_at")]
