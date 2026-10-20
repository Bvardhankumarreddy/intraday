"""SQLite persistence for paper trading (stdlib ``sqlite3``).

Conventions:
- Money is stored as TEXT decimal strings; timestamps as ISO 8601 with offset.
- Every state change that must be atomic runs inside ``store.transaction()``:
  either all of it is committed or none of it.
- Idempotency comes from primary keys and UNIQUE constraints: a signal is
  recorded once, and a signal can have at most one BUY and one SELL order.
- The portfolio row is created once. It is never reset by the application.

Phase 8 hardening:
- Opening validates the database: a missing directory, unreadable or
  corrupt file (``PRAGMA quick_check``), or a schema written by a newer
  version raises ``StoreError`` with a clear message. Nothing is recreated
  or repaired automatically.
- WAL journal, ``synchronous=FULL`` and a busy timeout, so the dashboard
  reading at the same moment does not make an engine write fail.
- Database file and directory are owner-only (0600 / 0700).
- Schema changes are additive (``CREATE ... IF NOT EXISTS``) and recorded
  in ``meta.schema_version``.
- Reports are never lost: every generated version is kept in the
  append-only ``report_history``; ``reports`` holds the latest.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.utils.fs import private_dir, private_file
from app.paper.models import (
    OrderState,
    PositionState,
    check_order_transition,
    check_position_transition,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initial_capital TEXT NOT NULL,
    cash TEXT NOT NULL,
    reserved TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    trading_date TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    data_mode TEXT NOT NULL,
    data_source TEXT NOT NULL,
    config_version TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    execution_model_version TEXT NOT NULL,
    fee_schedule_id TEXT NOT NULL,
    start_cash TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    locked INTEGER NOT NULL DEFAULT 0,
    lock_reason TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    trading_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    signal_time TEXT NOT NULL,
    entry_reference TEXT NOT NULL,
    stop TEXT NOT NULL,
    target TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    review_status TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    planned_net_loss TEXT,
    reasons TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    position_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    side TEXT NOT NULL,
    kind TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reference_price TEXT NOT NULL,
    reserved_cash TEXT NOT NULL DEFAULT '0',
    fill_price TEXT,
    fill_time TEXT,
    fill_quantity INTEGER,
    fill_basis TEXT,
    execution_model_version TEXT NOT NULL,
    reason TEXT,
    est_fees TEXT,
    est_spread TEXT,
    est_slippage TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (signal_id, side)
);
CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL UNIQUE,
    trading_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    state TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    entry_price TEXT NOT NULL,
    entry_time TEXT NOT NULL,
    stop TEXT NOT NULL,
    target TEXT NOT NULL,
    planned_net_loss TEXT NOT NULL,
    buy_cash TEXT NOT NULL,
    buy_costs TEXT NOT NULL,
    last_candle TEXT,
    mark_price TEXT,
    mark_time TEXT,
    exit_price TEXT,
    exit_time TEXT,
    exit_reason TEXT,
    ambiguous INTEGER NOT NULL DEFAULT 0,
    gross_pnl TEXT,
    costs TEXT,
    total_costs TEXT,
    net_pnl TEXT,
    flag_reason TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    trading_date TEXT,
    kind TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    ts TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    cash TEXT NOT NULL,
    reserved TEXT NOT NULL,
    position_value TEXT NOT NULL,
    equity TEXT NOT NULL,
    PRIMARY KEY (trading_date, ts)
);
CREATE TABLE IF NOT EXISTS watchlist (
    trading_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    screen_status TEXT NOT NULL,
    signal_status TEXT NOT NULL,
    last_price TEXT,
    last_price_time TEXT,
    spread_pct TEXT,
    score TEXT,
    reason TEXT,
    PRIMARY KEY (trading_date, symbol)
);
CREATE TABLE IF NOT EXISTS reports (
    trading_date TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS report_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_date TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    alert_key TEXT PRIMARY KEY,
    severity TEXT NOT NULL,
    component TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    occurrences INTEGER NOT NULL,
    last_notified_at TEXT,
    notify_status TEXT,
    notify_error TEXT,
    resolved_at TEXT
);
"""

SCHEMA_VERSION = 2  # 1 = Phases 5-7 (no meta table); 2 = Phase 8 additions


class StoreError(RuntimeError):
    pass


def _s(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


class PaperStore:
    BUSY_TIMEOUT_MS = 5000

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._depth = 0
        memory = self.path == ":memory:"
        if not memory:
            p = Path(self.path)
            if p.is_dir():
                raise StoreError(f"database path {p} is a directory")
            try:
                private_dir(p.parent)
            except OSError as exc:
                raise StoreError(f"database directory {p.parent} unavailable: {exc}") from exc
        try:
            self.conn = sqlite3.connect(self.path, isolation_level=None,
                                        timeout=self.BUSY_TIMEOUT_MS / 1000)
        except sqlite3.Error as exc:
            raise StoreError(f"database {self.path} unavailable: {exc}") from exc
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute(f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}")
            if not memory:
                self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = FULL")
            self.conn.execute("PRAGMA foreign_keys = ON")
            check = self.conn.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise StoreError(f"database {self.path} failed integrity check: {check}")
            self._check_schema_version()
            self.conn.executescript(SCHEMA)
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES "
                              "('schema_version', ?)", (str(SCHEMA_VERSION),))
        except sqlite3.DatabaseError as exc:
            self.conn.close()
            raise StoreError(f"database {self.path} is unreadable or corrupt: {exc}") from exc
        except StoreError:
            self.conn.close()
            raise
        if not memory:
            for suffix in ("", "-wal", "-shm"):
                private_file(Path(self.path + suffix))

    def _check_schema_version(self) -> None:
        has_meta = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
        if not has_meta:
            return
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is not None and int(row[0]) > SCHEMA_VERSION:
            raise StoreError(f"database schema version {row[0]} is newer than this code "
                             f"({SCHEMA_VERSION}); refusing to open (roll forward the code)")

    def schema_version(self) -> int:
        row = self.one("SELECT value FROM meta WHERE key = 'schema_version'")
        return int(row["value"]) if row else 1

    def set_meta(self, key: str, value: dict) -> None:
        """Operational status (heartbeat, scheduler state). Not financial data."""
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                          (key, json.dumps(value, default=str, sort_keys=True)))

    def get_meta(self, key: str) -> dict | None:
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except ValueError:
            return {"value": row["value"]}

    def ping(self) -> bool:
        return self.conn.execute("SELECT 1").fetchone()[0] == 1

    def close(self) -> None:
        try:
            if self._depth == 0 and self.path != ":memory:":
                self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error:
            pass
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic unit of work. Nested calls join the outer transaction."""
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield self.conn
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("ROLLBACK")
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("COMMIT")

    # -- generic helpers ----------------------------------------------------

    def _insert(self, table: str, row: dict, or_ignore: bool = False) -> bool:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
        cur = self.conn.execute(f"{verb} INTO {table} ({cols}) VALUES ({marks})",
                                [_s(v) for v in row.values()])
        return cur.rowcount == 1

    def _update(self, table: str, key: str, key_value: str, values: dict) -> None:
        sets = ", ".join(f"{k} = ?" for k in values)
        cur = self.conn.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?",
                                [_s(v) for v in values.values()] + [key_value])
        if cur.rowcount != 1:
            raise StoreError(f"{table} {key_value} not found")

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    # -- portfolio ----------------------------------------------------------

    def init_portfolio(self, initial: Decimal, now: datetime) -> bool:
        """Create the portfolio once. Returns False if it already exists."""
        with self.transaction():
            return self._insert("portfolio", {"id": 1, "initial_capital": initial, "cash": initial,
                                              "reserved": Decimal(0), "created_at": now},
                                or_ignore=True)

    def portfolio(self) -> sqlite3.Row:
        row = self.one("SELECT * FROM portfolio WHERE id = 1")
        if row is None:
            raise StoreError("paper portfolio not initialised")
        return row

    def set_cash(self, cash: Decimal, reserved: Decimal) -> None:
        if cash < 0 or reserved < 0:
            raise StoreError(f"negative cash ({cash}) or reserved ({reserved})")
        self._update("portfolio", "id", 1, {"cash": cash, "reserved": reserved})

    # -- sessions -----------------------------------------------------------

    def session(self, day: date) -> sqlite3.Row | None:
        return self.one("SELECT * FROM sessions WHERE trading_date = ?", (day.isoformat(),))

    def create_session(self, row: dict) -> bool:
        return self._insert("sessions", row, or_ignore=True)

    def update_session(self, day: date, values: dict) -> None:
        self._update("sessions", "trading_date", day.isoformat(), values)

    # -- signals ------------------------------------------------------------

    def signal_exists(self, signal_id: str) -> bool:
        return self.one("SELECT 1 FROM signals WHERE signal_id = ?", (signal_id,)) is not None

    def record_signal(self, row: dict) -> bool:
        """Insert once. Returns False if this signal was already processed."""
        return self._insert("signals", row, or_ignore=True)

    def signals(self, day: date) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM signals WHERE trading_date = ? ORDER BY signal_time, symbol",
                        (day.isoformat(),))

    # -- orders ---------------------------------------------------------------

    def create_order(self, row: dict) -> bool:
        """Insert once per (signal, side). Returns False for a duplicate."""
        row = {**row, "state": OrderState.CREATED.value}
        return self._insert("orders", row, or_ignore=True)

    def order(self, order_id: str) -> sqlite3.Row:
        row = self.one("SELECT * FROM orders WHERE order_id = ?", (order_id,))
        if row is None:
            raise StoreError(f"order {order_id} not found")
        return row

    def transition_order(self, order_id: str, new: OrderState, now: datetime, **values) -> None:
        old = OrderState(self.order(order_id)["state"])
        check_order_transition(old, new)
        self._update("orders", "order_id", order_id,
                     {"state": new.value, "updated_at": now, **values})

    def orders(self, day: date | None = None, state: OrderState | None = None) -> list[sqlite3.Row]:
        sql, params = "SELECT o.* FROM orders o JOIN signals s USING (signal_id) WHERE 1=1", []
        if day is not None:
            sql += " AND s.trading_date = ?"
            params.append(day.isoformat())
        if state is not None:
            sql += " AND o.state = ?"
            params.append(state.value)
        return self.all(sql + " ORDER BY o.created_at, o.order_id", tuple(params))

    # -- positions ------------------------------------------------------------

    def create_position(self, row: dict) -> None:
        self._insert("positions", {**row, "state": PositionState.OPEN.value})

    def position(self, position_id: str) -> sqlite3.Row:
        row = self.one("SELECT * FROM positions WHERE position_id = ?", (position_id,))
        if row is None:
            raise StoreError(f"position {position_id} not found")
        return row

    def transition_position(self, position_id: str, new: PositionState, **values) -> None:
        old = PositionState(self.position(position_id)["state"])
        if old is not new:
            check_position_transition(old, new)
        self._update("positions", "position_id", position_id, {"state": new.value, **values})

    def update_position(self, position_id: str, **values) -> None:
        self._update("positions", "position_id", position_id, values)

    def positions(self, day: date | None = None,
                  states: tuple[PositionState, ...] | None = None) -> list[sqlite3.Row]:
        sql, params = "SELECT * FROM positions WHERE 1=1", []
        if day is not None:
            sql += " AND trading_date = ?"
            params.append(day.isoformat())
        if states:
            sql += f" AND state IN ({', '.join('?' for _ in states)})"
            params += [s.value for s in states]
        return self.all(sql + " ORDER BY entry_time, symbol", tuple(params))

    # -- events, snapshots, reports -------------------------------------------

    def event(self, now: datetime, day: date | None, kind: str, level: str, message: str,
              data: dict | None = None) -> None:
        self._insert("events", {"ts": now, "trading_date": day, "kind": kind, "level": level,
                                "message": message,
                                "data": json.dumps(data, default=str, sort_keys=True)
                                if data else None})

    def events(self, day: date | None = None) -> list[sqlite3.Row]:
        if day is None:
            return self.all("SELECT * FROM events ORDER BY id")
        return self.all("SELECT * FROM events WHERE trading_date = ? ORDER BY id",
                        (day.isoformat(),))

    def snapshot(self, row: dict) -> None:
        self._insert("snapshots", row, or_ignore=True)

    def snapshots(self, day: date) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM snapshots WHERE trading_date = ? ORDER BY ts",
                        (day.isoformat(),))

    def upsert_watchlist(self, rows: list[dict]) -> None:
        for row in rows:
            cols = ", ".join(row)
            self.conn.execute(
                f"INSERT OR REPLACE INTO watchlist ({cols}) VALUES "
                f"({', '.join('?' for _ in row)})", [_s(v) for v in row.values()])

    def watchlist(self, day: date) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM watchlist WHERE trading_date = ? ORDER BY symbol",
                        (day.isoformat(),))

    def save_report(self, day: date, now: datetime, body: dict) -> None:
        """Latest report in ``reports``; every distinct version is also kept
        in the append-only ``report_history`` (nothing is ever lost)."""
        text = json.dumps(body, default=str, sort_keys=True)
        digest = hashlib.sha256(text.encode()).hexdigest()
        last = self.one("SELECT sha256 FROM report_history WHERE trading_date = ? "
                        "ORDER BY id DESC LIMIT 1", (day.isoformat(),))
        with self.transaction():
            old = self.one("SELECT generated_at, body FROM reports WHERE trading_date = ?",
                           (day.isoformat(),))
            if old is not None and last is None:  # report written before Phase 8
                self._insert("report_history", {
                    "trading_date": day, "generated_at": old["generated_at"],
                    "sha256": hashlib.sha256(old["body"].encode()).hexdigest(),
                    "body": old["body"]})
                last = {"sha256": hashlib.sha256(old["body"].encode()).hexdigest()}
            if last is None or last["sha256"] != digest:
                self._insert("report_history", {"trading_date": day, "generated_at": now,
                                                "sha256": digest, "body": text})
            self.conn.execute(
                "INSERT OR REPLACE INTO reports (trading_date, generated_at, body) "
                "VALUES (?, ?, ?)", (day.isoformat(), now.isoformat(), text))

    def report_versions(self, day: date) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM report_history WHERE trading_date = ? ORDER BY id",
                        (day.isoformat(),))

    def report(self, day: date) -> dict | None:
        row = self.one("SELECT body FROM reports WHERE trading_date = ?", (day.isoformat(),))
        return None if row is None else json.loads(row["body"])

    def report_dates(self) -> list[str]:
        return [r["trading_date"] for r in self.all(
            "SELECT trading_date FROM reports ORDER BY trading_date")]
