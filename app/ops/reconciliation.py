"""Portfolio-wide financial reconciliation with severities (read-only).

Reuses every Phase 6 check in ``app.evaluation.reconcile.reconcile`` for
each session date (ledger, per-position P&L maths, fee recomputation with the
Phase 3 schedule, order/position agreement, timing, spread consistency,
snapshots, report totals). It adds consistency rules that need no second
accounting implementation:

- uniqueness constraints that prevent duplicate orders/fills/positions exist
- fills: a FILLED order has a price, time and full quantity (no partial or
  incomplete fills); every FILLED BUY has a position; a FILLED SELL belongs to
  a CLOSED position
- positions: CLOSED ones carry exit, P&L and costs; OPEN/FLAGGED ones carry
  none; fees recorded once (``buy_costs`` equals the stored buy breakdown
  total; no sell costs before the exit)
- no PENDING order or OPEN/FLAGGED position from an earlier session (paper
  positions never carry overnight)
- sessions: a CLOSED session has no unresolved positions
- cash: cash >= 0, 0 <= reserved <= cash; available = cash - reserved
- exposure and limits: a position's buy cash never exceeds the session's
  starting cash; quantity never exceeds the approved signal quantity; BUY
  entries per day <= ``max_trades_per_day``; open positions <=
  ``max_concurrent_positions``
- unrealized P&L: open positions in an active session are marked with a
  price no older than the staleness limit plus one candle; otherwise the
  unrealized figure is flagged as using a stale price

Nothing here writes to financial tables. ``write_report`` saves a new,
uniquely named JSON file each time and never overwrites one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.config import Settings
from app.costs.money import D
from app.data.models import parse_interval
from app.evaluation.reconcile import reconcile
from app.paper.models import OrderState, PositionState
from app.paper.store import PaperStore
from app.utils.fs import private_dir

CRITICAL = {"cash_mismatch", "reserved_mismatch", "missing_net_pnl", "negative_cash",
            "reserved_exceeds_cash", "uniqueness_constraint_missing", "orphan_buy_fill",
            "exposure_exceeds_capital"}
WARNING = {"fee_schedule_missing", "fee_rates_missing", "stale_mark_price",
           "session_not_closed"}
RANK = {"CRITICAL": 0, "ERROR": 1, "WARNING": 2}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    ref: str
    message: str


def severity_of(code: str) -> str:
    if code in CRITICAL:
        return "CRITICAL"
    if code in WARNING:
        return "WARNING"
    return "ERROR"


def _f(code: str, ref: str, message: str) -> Finding:
    return Finding(code, severity_of(code), ref, message)


def _has_unique(store: PaperStore, table: str, cols: list[str]) -> bool:
    for idx in store.all(f"PRAGMA index_list({table})"):
        if idx["unique"] and [c["name"] for c in store.all(
                f"PRAGMA index_info({idx['name']})")] == cols:
            return True
    return False


def consistency_checks(store: PaperStore, settings: Settings, today: date,
                       now: datetime | None = None) -> list[Finding]:
    out: list[Finding] = []
    # -- uniqueness constraints (idempotency) ------------------------------
    for table, cols, what in (("orders", ["signal_id", "side"], "duplicate orders/fills"),
                              ("positions", ["signal_id"], "duplicate positions"),
                              ("signals", ["signal_id"], "duplicate signals")):
        if not _has_unique(store, table, cols):
            out.append(_f("uniqueness_constraint_missing", table,
                          f"no UNIQUE({', '.join(cols)}): {what} possible"))

    pf = store.portfolio()
    cash, reserved = D(pf["cash"]), D(pf["reserved"])
    if cash < 0:
        out.append(_f("negative_cash", "portfolio", f"cash {cash} < 0"))
    if reserved < 0 or reserved > cash:
        out.append(_f("reserved_exceeds_cash", "portfolio",
                      f"reserved {reserved} outside [0, cash {cash}]"))

    # -- fills ---------------------------------------------------------------
    positions = {p["position_id"]: p for p in store.positions()}
    for o in store.all("SELECT * FROM orders WHERE state = ?", (OrderState.FILLED.value,)):
        if o["fill_price"] is None or o["fill_time"] is None or o["fill_quantity"] is None:
            out.append(_f("incomplete_fill", o["order_id"], "FILLED order lacks price/time/qty"))
        elif o["fill_quantity"] != o["quantity"]:
            out.append(_f("partial_fill", o["order_id"],
                          f"filled {o['fill_quantity']} of {o['quantity']} (partial fills are "
                          "not supported by the paper model)"))
        pos = positions.get(o["position_id"])
        if o["side"] == "BUY" and pos is None:
            out.append(_f("orphan_buy_fill", o["order_id"], "FILLED BUY without a position"))
        if o["side"] == "SELL" and (pos is None or pos["state"] != PositionState.CLOSED.value):
            out.append(_f("orphan_sell_fill", o["order_id"],
                          "FILLED SELL whose position is not CLOSED"))

    # -- positions -------------------------------------------------------------
    sessions = {s["trading_date"]: s for s in store.all("SELECT * FROM sessions")}
    signals = {s["signal_id"]: s for s in store.all("SELECT * FROM signals")}
    for p in positions.values():
        pid, state = p["position_id"], p["state"]
        costs = json.loads(p["costs"]) if p["costs"] else {}
        if state == PositionState.CLOSED.value:
            missing = [k for k in ("exit_price", "exit_time", "exit_reason", "gross_pnl",
                                   "total_costs", "net_pnl") if p[k] is None]
            if missing:
                out.append(_f("closed_position_incomplete", pid, f"missing {missing}"))
            if "sell" not in costs:
                out.append(_f("sell_costs_missing", pid, "closed position has no sell costs"))
        else:
            if p["exit_price"] is not None or p["net_pnl"] is not None:
                out.append(_f("open_position_has_exit", pid,
                              f"{state} position carries exit/P&L fields"))
            if "sell" in costs:
                out.append(_f("fees_recorded_twice", pid, "sell costs before any exit"))
            if p["trading_date"] < today.isoformat():
                out.append(_f("position_from_past_session", pid,
                              f"{state} since {p['trading_date']}: positions never carry "
                              "overnight; needs review"))
        if "buy" not in costs or D(costs["buy"]["total"]) != D(p["buy_costs"]):
            out.append(_f("fees_recorded_twice" if "buy" in costs else "buy_costs_missing", pid,
                          f"buy_costs {p['buy_costs']} != stored buy breakdown total"))
        session = sessions.get(p["trading_date"])
        if session is not None and D(p["buy_cash"]) > D(session["start_cash"]):
            out.append(_f("exposure_exceeds_capital", pid,
                          f"buy cash {p['buy_cash']} > session starting cash "
                          f"{session['start_cash']}"))
        sig = signals.get(p["signal_id"])
        if sig is not None and p["quantity"] > sig["quantity"]:
            out.append(_f("quantity_exceeds_approval", pid,
                          f"quantity {p['quantity']} > approved {sig['quantity']}"))

    # -- orders and sessions ---------------------------------------------------
    for o in store.orders(state=OrderState.PENDING):
        sig = signals.get(o["signal_id"])
        if sig is not None and sig["trading_date"] < today.isoformat():
            out.append(_f("stale_pending_order", o["order_id"],
                          f"PENDING since {sig['trading_date']}; should have expired"))
    for day, s in sessions.items():
        unresolved = [p for p in positions.values() if p["trading_date"] == day
                      and p["state"] != PositionState.CLOSED.value]
        if s["state"] == "CLOSED" and unresolved:
            out.append(_f("closed_session_has_open_positions", day,
                          f"{len(unresolved)} unresolved position(s)"))
        if s["state"] == "ACTIVE" and day < today.isoformat():
            out.append(_f("session_not_closed", day, "past session still ACTIVE"))
        entries = store.one(
            "SELECT COUNT(*) AS n FROM orders o JOIN signals g USING (signal_id) WHERE "
            "g.trading_date = ? AND o.side = 'BUY' AND o.state IN ('PENDING', 'FILLED')",
            (day,))["n"]
        if entries > settings.risk.max_trades_per_day:
            out.append(_f("trade_limit_exceeded", day,
                          f"{entries} entries > max_trades_per_day "
                          f"{settings.risk.max_trades_per_day}"))
    open_now = [p for p in positions.values() if p["state"] != PositionState.CLOSED.value]
    if len(open_now) > settings.risk.max_concurrent_positions:
        out.append(_f("concurrency_limit_exceeded", "portfolio",
                      f"{len(open_now)} open > {settings.risk.max_concurrent_positions}"))

    # -- unrealized P&L freshness ---------------------------------------------------
    if now is not None:
        step = parse_interval(settings.strategy.candle_interval).timedelta
        limit = timedelta(seconds=settings.data.max_staleness_seconds) + step
        today_session = sessions.get(today.isoformat())
        if today_session is not None and today_session["state"] == "ACTIVE":
            for p in open_now:
                mark = datetime.fromisoformat(p["mark_time"]) if p["mark_time"] else None
                if mark is None or now - mark > limit:
                    out.append(_f("stale_mark_price", p["position_id"],
                                  f"unrealized P&L uses a price from {p['mark_time']} "
                                  f"(older than {limit})"))
    return out


def full_reconciliation(store: PaperStore, settings: Settings, schedule, today: date,
                        now: datetime | None = None, provider=None) -> dict:
    """All checks over every session date. Read-only."""
    days = sorted({r["trading_date"] for r in store.all("SELECT trading_date FROM sessions")}
                  | {r["trading_date"] for r in store.all(
                      "SELECT DISTINCT trading_date FROM positions")})
    findings: dict[tuple[str, str], Finding] = {}
    try:
        store.portfolio()
        has_portfolio = True
    except Exception:  # noqa: BLE001 - an empty database has nothing to reconcile
        has_portfolio = False
    if has_portfolio:
        for d in days:
            for i in reconcile(store, date.fromisoformat(d), settings, schedule, provider):
                findings.setdefault((i.code, i.ref), _f(i.code, i.ref, i.message))
        for f in consistency_checks(store, settings, today, now):
            findings.setdefault((f.code, f.ref), f)
    items = sorted(findings.values(), key=lambda f: (RANK[f.severity], f.code, f.ref))
    counts = {s: sum(1 for f in items if f.severity == s) for s in RANK}
    status = "FAIL" if counts["CRITICAL"] or counts["ERROR"] else \
        ("WARN" if counts["WARNING"] else "OK")
    summary = {}
    if has_portfolio:
        pf = store.portfolio()
        open_ = store.positions(states=(PositionState.OPEN, PositionState.FLAGGED))
        unrealized = sum(((D(p["mark_price"]) - D(p["entry_price"])) * p["quantity"]
                          for p in open_ if p["mark_price"]), Decimal(0))
        realized = sum((D(p["net_pnl"]) for p in store.positions(states=(PositionState.CLOSED,))
                        if p["net_pnl"] is not None), Decimal(0))
        value = sum((D(p["mark_price"]) * p["quantity"] for p in open_ if p["mark_price"]),
                    Decimal(0))
        summary = {"initial_capital": pf["initial_capital"], "cash": pf["cash"],
                   "reserved": pf["reserved"],
                   "available": str(D(pf["cash"]) - D(pf["reserved"])),
                   "open_positions": len(open_), "position_value_at_mark": str(value),
                   "equity_at_mark": str(D(pf["cash"]) + value),
                   "realized_net_pnl": str(realized), "unrealized_pnl_at_mark": str(unrealized),
                   "valuation": "equity = cash + sum(quantity x last mark price) of open "
                                "positions; unrealized excludes exit costs"}
    return {"generated_at": now.isoformat() if now else None, "database": store.path,
            "status": status, "counts": counts, "sessions_checked": days,
            "portfolio": summary,
            "findings": [{"code": f.code, "severity": f.severity, "ref": f.ref,
                          "message": f.message} for f in items],
            "note": "Read-only: no record was changed. Fix causes, never the numbers."}


def write_report(result: dict, out_dir: Path, now: datetime) -> Path:
    root = private_dir(Path(out_dir))
    stem = f"reconciliation-{now.strftime('%Y%m%dT%H%M%S')}"
    path, n = root / f"{stem}.json", 1
    while path.exists():  # never overwrite an earlier report
        n += 1
        path = root / f"{stem}-{n}.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")
    path.chmod(0o600)
    return path
