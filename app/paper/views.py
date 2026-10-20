"""Read-only views of the paper store, shaped for the dashboard and CLI.

Pure functions over ``PaperStore`` so they can be tested without Streamlit.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pandas as pd

from app.costs.money import D
from app.paper.models import PositionState
from app.paper.store import PaperStore


def sessions(store: PaperStore) -> pd.DataFrame:
    return pd.DataFrame([dict(r) for r in store.all(
        "SELECT trading_date, state, data_mode, data_source, start_cash, locked, lock_reason "
        "FROM sessions ORDER BY trading_date DESC")])


def portfolio_summary(store: PaperStore, day: date) -> dict:
    """Cash, reserved, equity and P&L split into realized and unrealized."""
    pf = store.portfolio()
    session = store.session(day)
    open_ = store.positions(states=(PositionState.OPEN, PositionState.FLAGGED))
    closed_today = store.positions(day, (PositionState.CLOSED,))
    value = sum((D(p["mark_price"]) * p["quantity"] for p in open_), Decimal(0))
    unrealized = sum(((D(p["mark_price"]) - D(p["entry_price"])) * p["quantity"]
                      for p in open_), Decimal(0))
    realized = None if any(p["net_pnl"] is None for p in closed_today) else \
        sum((D(p["net_pnl"]) for p in closed_today), Decimal(0))
    cash, reserved = D(pf["cash"]), D(pf["reserved"])
    equity = cash + value
    start = D(session["start_cash"]) if session else None
    snaps = store.snapshots(day)
    return {
        "initial_capital": D(pf["initial_capital"]),
        "cash": cash,
        "reserved": reserved,
        "available_cash": cash - reserved,
        "open_positions": len(open_),
        "position_value": value,
        "equity": equity,
        "realized_pnl_today": realized,  # None = unknown, never shown as 0
        "unrealized_pnl": unrealized,  # gross, before exit costs
        "daily_pnl": equity - start if start is not None else None,
        "locked": bool(session["locked"]) if session else False,
        "lock_reason": session["lock_reason"] if session else None,
        "last_snapshot": snaps[-1]["ts"] if snaps else None,
    }


def positions_table(store: PaperStore, day: date | None = None) -> pd.DataFrame:
    rows = []
    for p in store.positions(day):
        costs = json.loads(p["costs"]) if p["costs"] else {}
        fee = lambda k: sum((D(side.get(k, "0")) for side in costs.values()),  # noqa: E731
                            Decimal(0))
        rows.append({
            "symbol": p["symbol"], "state": p["state"], "qty": p["quantity"],
            "entry": p["entry_price"], "entry_time": p["entry_time"], "stop": p["stop"],
            "target": p["target"], "exit": p["exit_price"], "exit_time": p["exit_time"],
            "exit_reason": p["exit_reason"],
            "value": str(D(p["mark_price"]) * p["quantity"]) if p["mark_price"] else None,
            "gross_pnl": p["gross_pnl"], "brokerage": str(fee("brokerage")),
            "gst": str(fee("gst")), "stt": str(fee("stt")),
            "other_charges": str(fee("stamp_duty") + fee("exchange_transaction") + fee("ipft")
                                 + fee("sebi_turnover_fee")),
            "spread+slippage": str(fee("spread") + fee("slippage")),
            "total_costs": p["total_costs"], "net_pnl": p["net_pnl"],
            "ambiguous": bool(p["ambiguous"]), "flag": p["flag_reason"],
        })
    return pd.DataFrame(rows)


def orders_table(store: PaperStore, day: date) -> pd.DataFrame:
    return pd.DataFrame([{k: o[k] for k in (
        "order_id", "symbol", "side", "kind", "quantity", "state", "created_at",
        "reference_price", "fill_price", "fill_time", "fill_basis", "reason", "est_fees",
        "est_spread", "est_slippage")} for o in store.orders(day)])


def signals_table(store: PaperStore, day: date) -> pd.DataFrame:
    return pd.DataFrame([{
        "symbol": s["symbol"], "signal_time": s["signal_time"],
        "review_status": s["review_status"], "qty": s["quantity"],
        "entry_ref": s["entry_reference"], "stop": s["stop"], "target": s["target"],
        "reasons": "; ".join(json.loads(s["reasons"])),
    } for s in store.signals(day)])


def watchlist_table(store: PaperStore, day: date) -> pd.DataFrame:
    return pd.DataFrame([dict(r) for r in store.watchlist(day)])


def events_table(store: PaperStore, day: date, kinds: tuple[str, ...] | None = None) -> pd.DataFrame:
    rows = [dict(e) for e in store.events(day) if kinds is None or e["kind"] in kinds]
    return pd.DataFrame(rows)


def data_health(store: PaperStore, day: date) -> dict:
    """Latest health state per symbol from HEALTH events, plus last market-data times."""
    states: dict[str, str] = {}
    for e in store.events(day):
        if e["kind"] == "HEALTH" and e["data"]:
            d = json.loads(e["data"])
            states[d["symbol"]] = d["state"]
    marks = [p["mark_time"] for p in store.positions(day) if p["mark_time"]]
    quotes = [w["last_price_time"] for w in store.watchlist(day) if w["last_price_time"]]
    return {
        "symbol_states": states,
        "unhealthy": sorted(s for s, v in states.items() if v != "OK"),
        "last_candle_or_price_time": max(marks + quotes) if (marks or quotes) else None,
    }
