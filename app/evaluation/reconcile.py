"""Reconciliation and execution-audit checks over persisted paper records.

Every check re-derives a number from its inputs and compares it with what
was stored. Mismatches are returned (and recorded by the caller); nothing
is repaired.

Checks:
- ledger: cash = initial + sum(closed net) - sum(open buy cash);
  reserved = sum(reserved cash of PENDING orders)
- per closed position: gross = (exit - entry) x qty; total costs = buy
  total + sell total; net = gross - total costs; matching FILLED BUY and
  SELL orders with the position's quantity
- fees: buy/sell brokerage, statutory charges and GST recomputed with the
  Phase 3 fee schedule for the trade date equal the stored breakdown
- spread consistency: quote fills carry zero spread cost; candle fills carry
  half the assumed spread (counted once)
- timing (no look-ahead): signal time <= entry fill time <= exit time
- fill prices inside the candle that produced them, when candles are
  available from the provider
- equity snapshots: equity = cash + position value
- report totals equal the sum of the position records
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import pandas as pd

from app.config import Settings
from app.costs.money import D, pct_of, round_money
from app.costs.schedule import ChargeKind, FeeSchedule, FeeScheduleError, Side
from app.costs.statutory_charges import order_charges
from app.data.base_provider import MarketDataProvider
from app.data.models import DataError, parse_interval
from app.paper.models import OrderState, PositionState
from app.paper.store import PaperStore


@dataclass(frozen=True)
class Issue:
    code: str
    ref: str
    message: str


def reconcile(store: PaperStore, day: date, settings: Settings,
              schedule: FeeSchedule | None,
              provider: MarketDataProvider | None = None) -> list[Issue]:
    issues: list[Issue] = []
    issues += _ledger(store)
    positions = store.positions(day)
    candles = _candles(provider, positions, day, settings)
    for p in positions:
        issues += _orders(store, p)
        if p["state"] != PositionState.CLOSED.value:
            continue
        issues += _position_math(p)
        issues += _fees(p, schedule, day, settings)
        issues += _timing(store, p)
        issues += _fill_ranges(store, p, candles.get(p["symbol"]), settings)
    issues += _snapshots(store, day)
    issues += _report_totals(store, day)
    return issues


def _ledger(store: PaperStore) -> list[Issue]:
    pf = store.portfolio()
    closed = store.positions(states=(PositionState.CLOSED,))
    open_ = store.positions(states=(PositionState.OPEN, PositionState.FLAGGED))
    out = []
    if any(p["net_pnl"] is None for p in closed):
        out.append(Issue("missing_net_pnl", "portfolio", "a closed position has no net P&L"))
        return out
    cash = D(pf["initial_capital"]) + sum((D(p["net_pnl"]) for p in closed), Decimal(0)) \
        - sum((D(p["buy_cash"]) for p in open_), Decimal(0))
    if cash != D(pf["cash"]):
        out.append(Issue("cash_mismatch", "portfolio",
                         f"stored cash {pf['cash']} != derived {cash}"))
    reserved = sum((D(o["reserved_cash"]) for o in store.orders(state=OrderState.PENDING)),
                   Decimal(0))
    if reserved != D(pf["reserved"]):
        out.append(Issue("reserved_mismatch", "portfolio",
                         f"stored reserved {pf['reserved']} != derived {reserved}"))
    return out


def _orders(store: PaperStore, p) -> list[Issue]:
    rows = store.all("SELECT * FROM orders WHERE signal_id = ?", (p["signal_id"],))
    by_side = {r["side"]: r for r in rows}
    out = []
    buy = by_side.get("BUY")
    if buy is None or buy["state"] != OrderState.FILLED.value:
        out.append(Issue("buy_order_missing", p["position_id"],
                         "position has no FILLED BUY order"))
    elif buy["fill_quantity"] != p["quantity"] or D(buy["fill_price"]) != D(p["entry_price"]):
        out.append(Issue("buy_fill_mismatch", p["position_id"],
                         "BUY fill quantity/price differ from the position"))
    if p["state"] == PositionState.CLOSED.value:
        sell = by_side.get("SELL")
        if sell is None or sell["state"] != OrderState.FILLED.value:
            out.append(Issue("sell_order_missing", p["position_id"],
                             "closed position has no FILLED SELL order"))
        elif sell["fill_quantity"] != p["quantity"] or D(sell["fill_price"]) != D(p["exit_price"]):
            out.append(Issue("sell_fill_mismatch", p["position_id"],
                             "SELL fill quantity/price differ from the position"))
    return out


def _position_math(p) -> list[Issue]:
    costs = json.loads(p["costs"])
    gross = (D(p["exit_price"]) - D(p["entry_price"])) * p["quantity"]
    total = D(costs["buy"]["total"]) + D(costs["sell"]["total"])
    out = []
    if gross != D(p["gross_pnl"]):
        out.append(Issue("gross_mismatch", p["position_id"],
                         f"gross {p['gross_pnl']} != (exit - entry) x qty = {gross}"))
    if total != D(p["total_costs"]):
        out.append(Issue("costs_mismatch", p["position_id"],
                         f"total costs {p['total_costs']} != buy + sell = {total}"))
    if D(p["gross_pnl"]) - D(p["total_costs"]) != D(p["net_pnl"]):
        out.append(Issue("net_mismatch", p["position_id"], "net != gross - total costs"))
    return out


def _fees(p, schedule: FeeSchedule | None, day: date, settings: Settings) -> list[Issue]:
    if schedule is None:
        return [Issue("fee_schedule_missing", p["position_id"], "cannot verify fees")]
    try:
        rates = schedule.rates_on(day, settings.cost_review.apply_current_rates_to_past_dates)
    except FeeScheduleError as exc:
        return [Issue("fee_rates_missing", p["position_id"], str(exc))]
    costs = json.loads(p["costs"])
    out = []
    for side, price_key in ((Side.BUY, "entry_price"), (Side.SELL, "exit_price")):
        fees = order_charges(rates, side, D(p[price_key]), p["quantity"])
        stored = costs[side.value]
        expected = {"brokerage": fees.brokerage, "gst": fees.gst,
                    "stt": fees.get(ChargeKind.STT), "stamp_duty": fees.get(ChargeKind.STAMP_DUTY),
                    "exchange_transaction": fees.get(ChargeKind.EXCHANGE_TRANSACTION),
                    "sebi_turnover_fee": fees.get(ChargeKind.SEBI_TURNOVER_FEE),
                    "ipft": fees.get(ChargeKind.IPFT)}
        for k, v in expected.items():
            if D(stored.get(k, "0")) != v:
                out.append(Issue(f"fee_mismatch_{side.value}_{k}", p["position_id"],
                                 f"{side.value} {k}: stored {stored.get(k)} != recomputed {v}"))
    return out


def _timing(store: PaperStore, p) -> list[Issue]:
    sig = store.one("SELECT signal_time FROM signals WHERE signal_id = ?", (p["signal_id"],))
    out = []
    if sig is not None and datetime.fromisoformat(p["entry_time"]) < \
            datetime.fromisoformat(sig["signal_time"]):
        out.append(Issue("entry_before_signal", p["position_id"],
                         "entry filled before the signal existed (look-ahead)"))
    if datetime.fromisoformat(p["exit_time"]) < datetime.fromisoformat(p["entry_time"]):
        out.append(Issue("exit_before_entry", p["position_id"], "exit before entry"))
    return out


def _candles(provider, positions, day: date, settings: Settings) -> dict[str, pd.DataFrame]:
    out = {}
    if provider is None:
        return out
    iv = parse_interval(settings.strategy.candle_interval).label
    for sym in {p["symbol"] for p in positions}:
        try:
            out[sym] = provider.get_candles(sym, iv, day, day).frame.set_index("timestamp")
        except DataError:
            pass
    return out


def _fill_ranges(store: PaperStore, p, frame: pd.DataFrame | None,
                 settings: Settings) -> list[Issue]:
    out = []
    step = parse_interval(settings.strategy.candle_interval).timedelta
    for o in store.all("SELECT * FROM orders WHERE signal_id = ? AND state = 'FILLED'",
                       (p["signal_id"],)):
        price, when = D(o["fill_price"]), datetime.fromisoformat(o["fill_time"])
        assumed_half = D(settings.execution.assumed_spread_pct) / 2
        turnover = price * o["fill_quantity"]
        if o["fill_basis"] and o["fill_basis"].startswith("quote"):
            if D(o["est_spread"]) != 0:
                out.append(Issue("spread_double_counted", o["order_id"],
                                 "quote fill (spread in price) also charged a spread cost"))
            continue
        if D(o["est_spread"] or 0) != round_money(pct_of(turnover, assumed_half)):
            out.append(Issue("spread_inconsistent", o["order_id"],
                             f"candle fill spread {o['est_spread']} != half the assumed spread"))
        if frame is None:
            continue
        slot = pd.Timestamp(when)
        if slot not in frame.index:
            slot = pd.Timestamp(when) - (pd.Timestamp(when) - frame.index[0]) % step
        if slot in frame.index:
            row = frame.loc[slot]
            if not D(float(row["low"])) <= price <= D(float(row["high"])):
                out.append(Issue("fill_outside_candle", o["order_id"],
                                 f"fill {price} outside candle [{row['low']}, {row['high']}]"))
    return out


def _snapshots(store: PaperStore, day: date) -> list[Issue]:
    return [Issue("equity_mismatch", s["ts"], f"equity {s['equity']} != cash + positions")
            for s in store.snapshots(day)
            if D(s["equity"]) != D(s["cash"]) + D(s["position_value"])]


def _report_totals(store: PaperStore, day: date) -> list[Issue]:
    report = store.report(day)
    if report is None:
        return []
    closed = store.positions(day, (PositionState.CLOSED,))
    net = sum((D(p["net_pnl"]) for p in closed), Decimal(0))
    costs = sum((D(p["total_costs"]) for p in closed), Decimal(0))
    out = []
    if D(report["totals"]["net_pnl"]) != net:
        out.append(Issue("report_net_mismatch", day.isoformat(),
                         f"report net {report['totals']['net_pnl']} != positions {net}"))
    if D(report["totals"]["total_costs"]) != costs:
        out.append(Issue("report_costs_mismatch", day.isoformat(),
                         f"report costs {report['totals']['total_costs']} != positions {costs}"))
    return out
