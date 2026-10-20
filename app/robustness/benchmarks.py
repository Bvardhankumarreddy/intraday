"""Comparison baselines, predeclared in the protocol before any result:

1. **no_trade**: P&L 0 and no costs. The strategy has to beat doing nothing
   after costs, or it is not worth the risk and effort.
2. **cash_retention**: the same numbers as no_trade, read as "capital stays
   in the Groww account". No interest is modelled; any real yield on idle
   cash would raise this baseline.
3. **passive_liquid_long**: each session, at ``entry_time`` (09:30, when the
   opening range is complete), buy as many whole shares as cash allows of
   the eligible stock with the highest average traded value. Eligible means
   it passed the same screen (price band, liquidity) at that moment, using
   only data available then. Buy at the open of the candle starting at
   ``entry_time``; sell at the open of the first candle at or after the
   strategy's ``eod_exit_time``. Same fees, spread and slippage; no stop.
   This separates the ORB rules from simply being long a liquid in-band
   stock intraday, and shows the cost drag of one round trip a day.
4. **benchmark_proxy**: the same rule applied to the benchmark symbol (e.g.
   NIFTYBEES), when it is in the dataset. This is the broad market's
   intraday move after costs at the same capital. It is not screened by the
   price band.

All baselines use the same out-of-sample sessions, the same initial capital
and fresh capital per test window, like the strategy.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pandas as pd

from app.backtesting.dataset import HistoricalDataset, SymbolData
from app.backtesting.execution import Candle
from app.config import Settings
from app.costs.calculator import round_trip
from app.costs.money import D
from app.costs.schedule import load_fee_schedule
from app.data.models import day_mask
from app.scan.models import ScreenStatus
from app.scan.pipeline import run_scan
from app.utils.market_calendar import MarketCalendar


def _bars(data: SymbolData, day: date) -> pd.DataFrame:
    return data.intraday[day_mask(data.intraday["timestamp"], day)].set_index("timestamp")


def _affordable_qty(rates, price: Decimal, cash: Decimal, settings: Settings) -> int:
    qty = int(cash // price)
    while qty > 0:
        rt = round_trip(rates, price, price, qty, settings.execution, None)
        need = rt.buy_turnover + rt.buy.total + rt.execution.spread_buy + \
            rt.execution.slippage_buy
        if need <= cash:
            return qty
        qty -= 1
    return 0


def _session_trade(data: SymbolData, day: date, settings: Settings, calendar: MarketCalendar,
                   rates, cash: Decimal, entry_time) -> dict:
    bars = _bars(data, day)
    entry_at = pd.Timestamp(datetime.combine(day, entry_time, tzinfo=calendar.tz))
    exit_at = pd.Timestamp(datetime.combine(day, settings.strategy.eod_exit_time,
                                            tzinfo=calendar.tz))
    if entry_at not in bars.index:
        return {"status": "NO_ENTRY_CANDLE"}
    entry = Candle.from_row(bars.loc[entry_at])
    qty = _affordable_qty(rates, entry.open, cash, settings)
    if qty == 0:
        return {"status": "UNAFFORDABLE", "price": float(entry.open)}
    later = bars[bars.index >= exit_at]
    if later.empty:
        exit_price, incomplete = D(float(bars["close"].iloc[-1])), True
    else:
        exit_price, incomplete = Candle.from_row(later.iloc[0]).open, False
    rt = round_trip(rates, entry.open, exit_price, qty, settings.execution, None)
    return {"status": "TRADED", "symbol": data.instrument.symbol, "quantity": qty,
            "entry": float(entry.open), "exit": float(exit_price), "incomplete": incomplete,
            "gross_pnl": rt.gross_pnl, "costs": rt.total_costs, "net_pnl": rt.net_pnl,
            "gross_return_pct": float((exit_price / entry.open - 1) * 100)}


def _pick_passive(dataset: HistoricalDataset, settings: Settings, calendar: MarketCalendar,
                  day: date, entry_time) -> SymbolData | None:
    as_of = datetime.combine(day, entry_time, tzinfo=calendar.tz)
    scan = run_scan(dataset.provider(calendar), settings, calendar, as_of)
    eligible = [c for c in scan.candidates if c.screen.status is ScreenStatus.PASS
                and c.screen.avg_traded_value is not None]
    if not eligible:
        return None
    best = max(eligible, key=lambda c: (c.screen.avg_traded_value, c.symbol))
    return dataset.symbols[best.symbol]


def run_rule(windows: list[list[date]], settings: Settings, calendar: MarketCalendar,
             pick) -> dict:
    """Apply a one-trade-per-session rule over windows (fresh capital each)."""
    schedule = load_fee_schedule(settings)
    initial = D(settings.capital.initial_capital)
    rows, offset = [], Decimal(0)
    for wi, sessions in enumerate(windows):
        cash = initial
        for day in sessions:
            rates = schedule.rates_on(day, settings.cost_review.apply_current_rates_to_past_dates)
            data = pick(day)
            if data is None:
                rows.append({"window": wi, "session": day, "status": "NO_ELIGIBLE_STOCK",
                             "net_pnl": 0.0})
                continue
            tr = _session_trade(data, day, settings, calendar, rates, cash, pick.entry_time)
            if tr["status"] == "TRADED":
                cash += tr["net_pnl"]
            rows.append({"window": wi, "session": day,
                         **{k: (float(v) if isinstance(v, Decimal) else v) for k, v in tr.items()},
                         "net_pnl": float(tr.get("net_pnl", 0))})
        offset += cash - initial
    traded = [r for r in rows if r["status"] == "TRADED"]
    n = len(traded)
    return {"sessions": len(rows), "trades": n,
            "gross_pnl": round(sum(r["gross_pnl"] for r in traded), 2),
            "total_costs": round(sum(r["costs"] for r in traded), 2),
            "net_pnl": round(float(offset), 2),
            "net_expectancy": round(float(offset) / n, 4) if n else None,
            "mean_gross_return_pct": round(sum(r["gross_return_pct"] for r in traded) / n, 4)
            if n else None,
            "skipped": {s: sum(1 for r in rows if r["status"] == s)
                        for s in {r["status"] for r in rows} if s != "TRADED"},
            "rows": rows}


class _Picker:
    def __init__(self, fn, entry_time):
        self.fn, self.entry_time = fn, entry_time

    def __call__(self, day):
        return self.fn(day)


def comparison_baselines(dataset: HistoricalDataset, benchmark: SymbolData | None,
                         settings: Settings, calendar: MarketCalendar,
                         windows: list[list[date]], entry_time) -> dict:
    sessions = sum(len(w) for w in windows)
    zero = {"sessions": sessions, "trades": 0, "gross_pnl": 0.0, "total_costs": 0.0,
            "net_pnl": 0.0, "net_expectancy": None}
    out = {
        "no_trade": {**zero, "status": "OK", "relevance": "the strategy must beat doing "
                     "nothing after costs"},
        "cash_retention": {**zero, "status": "OK", "relevance": "capital left in the account; "
                           "no interest modelled, so this understates the alternative"},
    }
    passive = run_rule(windows, settings, calendar, _Picker(
        lambda d: _pick_passive(dataset, settings, calendar, d, entry_time), entry_time))
    out["passive_liquid_long"] = {**passive, "status": "OK", "relevance":
                                  "same universe, screen, capital and costs, without the ORB "
                                  "entry and exit rules"}
    if benchmark is None:
        out["benchmark_proxy"] = {"status": "UNAVAILABLE",
                                  "reason": "benchmark symbol not in the dataset"}
    else:
        bench = run_rule(windows, settings, calendar,
                         _Picker(lambda d: benchmark if day_mask(
                             benchmark.intraday["timestamp"], d).any() else None, entry_time))
        out["benchmark_proxy"] = {**bench, "status": "OK", "symbol":
                                  benchmark.instrument.symbol, "relevance":
                                  "broad-market intraday move at the same capital and costs"}
    return out
