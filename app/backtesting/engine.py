"""Event-driven backtest of the Phase 2 strategy with Phase 3 risk and costs.

For each trading session, candles are processed slot by slot (5-minute
steps) in timestamp order. At each slot:

1. Pending entries (decided at the previous candle close) fill at this
   candle's open, under the execution model.
2. Open positions are checked against this candle for exits.
3. Equity is marked at the candle close.
4. At the candle close, the existing ``run_scan`` (snapshot -> screener ->
   ORB strategy -> ranking) runs with ``as_of`` = that close, so only
   completed candles are visible. Each newly fired signal goes through the
   Phase 3 ``RiskCostEngine`` with the running ``DailyRiskState``.

Accounting (cash model, no margin):
- Entry: cash -= qty x fill + buy fees + buy spread + buy slippage
- Exit:  cash += qty x exit - sell fees - sell spread - sell slippage
  The two together change cash by exactly the trade's net P&L.
- Equity = cash + qty x last close for open positions (mark-to-market,
  before exit costs). Positions never stay open overnight.
- Realized P&L feeds the daily risk state, so the daily loss lock works as
  in Phase 3. Each session starts a new state with the current cash.

Optional, off by default:
- ``membership``: point-in-time universe (symbols eligible on each session).
- ``shadow``: DIAGNOSTIC outcomes for technically qualified signals that the
  risk/cost review rejected. Each is followed through the same execution
  rules without touching cash, limits or the trade list, so the signal's
  edge can be studied separately from whether the account could take it.

Nothing here places orders. "Orders" and "fills" are simulation records.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd

from app.backtesting.dataset import HistoricalDataset
from app.backtesting.execution import (
    Candle,
    ExitReason,
    entry_fill,
    execution_model_label,
    exit_fill,
    target_trigger,
)
from app.config import Settings
from app.costs.calculator import round_trip
from app.costs.money import D
from app.costs.schedule import ChargeKind, FeeScheduleError
from app.data.models import parse_interval
from app.risk.engine import RiskCostEngine
from app.risk.interfaces import TradeEvaluationRequest
from app.risk.risk_limits import DailyRiskState, OpenPosition
from app.scan.models import ReviewStatus, RiskReport, TradeSetup
from app.scan.pipeline import run_scan
from app.utils.market_calendar import MarketCalendar

STRATEGY_VERSION = "orb-long-v1"

# Screening failures that depend only on the instrument and on sessions before
# today, so they cannot change during the session. Such symbols are not
# re-scanned for the rest of the day (a speed-up with identical results).
STATIC_REJECTIONS = {"wrong_exchange", "series_not_allowed", "not_tradable", "illiquid",
                     "insufficient_liquidity_history", "daily_data"}


class BacktestConfigError(ValueError):
    pass


@dataclass
class OrderRecord:
    order_id: str
    signal_id: str
    symbol: str
    side: str
    kind: str
    created_at: datetime
    reference_price: Decimal
    quantity: int
    status: str  # FILLED | CANCELLED
    note: str = ""


@dataclass
class FillRecord:
    order_id: str
    symbol: str
    side: str
    time: datetime
    price: Decimal
    quantity: int
    basis: str


@dataclass
class TradeRecord:
    trade_id: str
    signal_id: str
    symbol: str
    session: date
    signal_time: datetime
    entry_time: datetime
    entry_reference: Decimal
    entry_price: Decimal
    exit_time: datetime
    exit_price: Decimal
    exit_reason: str
    quantity: int
    stop: Decimal
    target: Decimal
    planned_net_loss: Decimal
    buy_turnover: Decimal
    sell_turnover: Decimal
    gross_pnl: Decimal
    brokerage: Decimal
    stt: Decimal
    stamp_duty: Decimal
    exchange_transaction: Decimal
    ipft: Decimal
    sebi_turnover_fee: Decimal
    gst: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    total_costs: Decimal
    net_pnl: Decimal
    holding_minutes: int
    ambiguous: bool
    incomplete: bool
    fee_schedule_id: str
    fee_warnings: str


@dataclass
class RejectedSignal:
    time: datetime
    symbol: str
    signal_id: str
    stage: str  # risk | execution
    status: str
    reasons: str


@dataclass
class ShadowOutcome:
    """DIAGNOSTIC: what a risk-rejected signal would have done under the same
    execution rules. Never part of P&L, equity or trade statistics."""

    signal_id: str
    symbol: str
    session: date
    signal_time: datetime
    review_status: str
    reasons: str
    entry_reference: Decimal
    stop: Decimal
    target: Decimal
    filled: bool
    not_filled_reason: str = ""
    entry_time: datetime | None = None
    entry_price: Decimal | None = None
    exit_time: datetime | None = None
    exit_price: Decimal | None = None
    exit_reason: str = ""
    ambiguous: bool = False
    incomplete: bool = False
    gross_per_share: Decimal | None = None
    r_multiple: Decimal | None = None  # gross per share / (entry fill - stop)
    # Whole shares the session's starting cash could buy, ignoring the
    # risk-per-trade rule, and the net result after all costs at that size.
    affordable_qty: int = 0
    affordable_costs: Decimal | None = None
    affordable_net_pnl: Decimal | None = None
    holding_minutes: int | None = None


@dataclass
class EquityPoint:
    time: datetime
    cash: Decimal
    position_value: Decimal
    equity: Decimal


@dataclass
class BacktestResult:
    start: date
    end: date
    initial_capital: Decimal
    final_cash: Decimal
    dataset_id: str
    config_version: str
    strategy_version: str
    execution_model_version: str
    fee_schedule_id: str
    sessions: list[date]
    trades: list[TradeRecord] = field(default_factory=list)
    orders: list[OrderRecord] = field(default_factory=list)
    fills: list[FillRecord] = field(default_factory=list)
    rejected: list[RejectedSignal] = field(default_factory=list)
    equity: list[EquityPoint] = field(default_factory=list)
    daily_equity: list[EquityPoint] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    shadow: list[ShadowOutcome] = field(default_factory=list)  # diagnostic only

    def table(self, name: str) -> pd.DataFrame:
        rows = [asdict(r) for r in getattr(self, name)]
        return pd.DataFrame(rows)


@dataclass
class _Position:
    setup: TradeSetup
    report: RiskReport
    quantity: int
    entry_time: datetime | None = None
    entry_price: Decimal | None = None
    entry_order: str = ""
    buy_cash: Decimal = Decimal(0)


def preflight(dataset: HistoricalDataset, settings: Settings, engine: RiskCostEngine,
              sessions: list[date]) -> None:
    if settings.screening.require_spread:
        raise BacktestConfigError(
            "historical candles have no quotes, so the spread screen can't pass. Set "
            "screening.require_spread: false for backtests; costs then use "
            "execution.assumed_spread_pct (labelled as assumed).")
    if engine.schedule is None:
        raise BacktestConfigError(f"fee schedule unavailable: {engine.schedule_error}")
    bad = []
    for day in sessions:
        try:
            engine.schedule.rates_on(day, settings.cost_review.apply_current_rates_to_past_dates)
        except FeeScheduleError:
            bad.append(day)
    if bad:
        raise BacktestConfigError(
            f"no fee rates in effect for {len(bad)} session(s) ({bad[0]}..{bad[-1]}). Set "
            "cost_review.apply_current_rates_to_past_dates: true to apply current rates "
            "(results will be labelled retroactive).")


def run_backtest(
    dataset: HistoricalDataset,
    settings: Settings,
    calendar: MarketCalendar,
    start: date,
    end: date,
    membership: Callable[[date], set[str]] | None = None,
    shadow: bool = False,
) -> BacktestResult:
    if start > end:
        raise BacktestConfigError("start must not be after end")
    engine = RiskCostEngine(settings)
    sessions = [d for d in dataset.sessions() if start <= d <= end and calendar.is_trading_day(d)]
    preflight(dataset, settings, engine, sessions)
    provider = dataset.provider(calendar)
    cash = D(settings.capital.initial_capital)
    step = pd.Timedelta(parse_interval(settings.strategy.candle_interval).timedelta)

    result = BacktestResult(
        start=start, end=end, initial_capital=cash, final_cash=cash,
        dataset_id=dataset.dataset_id, config_version=settings.version(),
        strategy_version=STRATEGY_VERSION,
        execution_model_version=execution_model_label(settings.backtest.target_trade_through_pct),
        fee_schedule_id=engine.schedule.schedule_id, sessions=sessions,
    )
    result.assumptions = _assumptions(settings, engine)
    if membership is not None:
        result.assumptions.append("Point-in-time universe: each session trades only the "
                                  "symbols listed as members on that date.")
    trade_through = settings.backtest.target_trade_through_pct
    seq = {"order": 0, "trade": 0}

    def next_id(kind: str) -> str:
        seq[kind] += 1
        return f"{kind[0].upper()}{seq[kind]:05d}"

    for day in sessions:
        state = DailyRiskState.new_session(day, account_capital=cash)
        rates = engine.schedule.rates_on(day, settings.cost_review.apply_current_rates_to_past_dates)
        bars = {sym: d.intraday[d.intraday["timestamp"].dt.date == day].set_index("timestamp")
                for sym, d in dataset.symbols.items()}
        slots = sorted({ts for b in bars.values() for ts in b.index})
        eod = pd.Timestamp(datetime.combine(day, settings.strategy.eod_exit_time, tzinfo=calendar.tz))
        cutoff = pd.Timestamp(datetime.combine(day, settings.strategy.last_signal_time,
                                               tzinfo=calendar.tz))
        pending: dict[str, _Position] = {}
        open_pos: dict[str, _Position] = {}
        # Symbols still able to produce a signal today (see STATIC_REJECTIONS).
        active = set(bars) if membership is None else set(bars) & set(membership(day))
        last_close: dict[str, Decimal] = {}
        session_cash = cash
        shadow_pending: dict[str, ShadowOutcome] = {}  # signal_id -> outcome
        shadow_open: dict[str, ShadowOutcome] = {}

        def time_exit_for(slot: pd.Timestamp, entry_time: datetime) -> ExitReason | None:
            if slot >= eod:
                return ExitReason.END_OF_DAY
            if (settings.backtest.max_holding_minutes is not None and
                    slot >= pd.Timestamp(entry_time)
                    + pd.Timedelta(minutes=settings.backtest.max_holding_minutes)):
                return ExitReason.MAX_HOLDING
            return None

        def close_shadow(sh: ShadowOutcome, when: datetime, price: Decimal, reason: ExitReason,
                         ambiguous: bool, incomplete: bool) -> None:
            sh.exit_time, sh.exit_price, sh.exit_reason = when, price, reason.value
            sh.ambiguous, sh.incomplete = ambiguous, incomplete
            sh.gross_per_share = price - sh.entry_price
            sh.r_multiple = round(sh.gross_per_share / (sh.entry_price - sh.stop), 4)
            sh.holding_minutes = int((when - sh.entry_time).total_seconds() // 60)
            sh.affordable_qty = int(session_cash // sh.entry_price)
            if sh.affordable_qty > 0:
                rt = round_trip(rates, sh.entry_price, price, sh.affordable_qty,
                                settings.execution, None)
                sh.affordable_costs, sh.affordable_net_pnl = rt.total_costs, rt.net_pnl
            result.shadow.append(sh)

        def close_position(sym: str, pos: _Position, when: datetime, price: Decimal,
                           reason: ExitReason, ambiguous: bool, incomplete: bool) -> None:
            nonlocal cash
            rt = round_trip(rates, pos.entry_price, price, pos.quantity, settings.execution, None)
            cash += rt.sell_turnover - rt.sell.total - rt.execution.spread_sell \
                - rt.execution.slippage_sell
            oid = next_id("order")
            result.orders.append(OrderRecord(oid, pos.setup.signal_id, sym, "SELL", reason.value,
                                             when, price, pos.quantity, "FILLED"))
            result.fills.append(FillRecord(oid, sym, "SELL", when, price, pos.quantity,
                                           f"exit: {reason.value}"))
            result.trades.append(TradeRecord(
                trade_id=next_id("trade"), signal_id=pos.setup.signal_id, symbol=sym,
                session=day, signal_time=pos.setup.signal_time, entry_time=pos.entry_time,
                entry_reference=D(pos.setup.entry_reference), entry_price=pos.entry_price,
                exit_time=when, exit_price=price, exit_reason=reason.value,
                quantity=pos.quantity, stop=D(pos.setup.stop_reference),
                target=D(pos.setup.target_reference),
                planned_net_loss=pos.report.planned_net_loss_at_stop or Decimal(0),
                buy_turnover=rt.buy_turnover, sell_turnover=rt.sell_turnover,
                gross_pnl=rt.gross_pnl, brokerage=rt.broker_charges,
                stt=rt.charge(ChargeKind.STT), stamp_duty=rt.charge(ChargeKind.STAMP_DUTY),
                exchange_transaction=rt.charge(ChargeKind.EXCHANGE_TRANSACTION),
                ipft=rt.charge(ChargeKind.IPFT),
                sebi_turnover_fee=rt.charge(ChargeKind.SEBI_TURNOVER_FEE), gst=rt.gst,
                spread_cost=rt.execution.spread_total, slippage_cost=rt.execution.slippage_total,
                total_costs=rt.total_costs, net_pnl=rt.net_pnl,
                holding_minutes=int((when - pos.entry_time).total_seconds() // 60),
                ambiguous=ambiguous, incomplete=incomplete,
                fee_schedule_id=rates.schedule.schedule_id, fee_warnings="; ".join(rates.warnings),
            ))
            state.open_positions = [p for p in state.open_positions
                                    if p.signal_id != pos.setup.signal_id]
            state.record_realized(rt.net_pnl, settings)

        for slot in slots:
            # 0. DIAGNOSTIC shadow entries/exits (no effect on anything below).
            for sid, sh in list(shadow_pending.items()):
                del shadow_pending[sid]
                if slot not in bars[sh.symbol].index:
                    sh.not_filled_reason = "no candle at the next slot"
                    result.shadow.append(sh)
                    continue
                price, why = entry_fill(Candle.from_row(bars[sh.symbol].loc[slot]),
                                        sh.entry_reference, sh.stop,
                                        settings.backtest.max_entry_gap_pct)
                if price is None:
                    sh.not_filled_reason = why
                    result.shadow.append(sh)
                    continue
                sh.filled, sh.entry_time, sh.entry_price = True, slot.to_pydatetime(), price
                shadow_open[sid] = sh
            for sid, sh in list(shadow_open.items()):
                if slot not in bars[sh.symbol].index:
                    continue
                hit = exit_fill(Candle.from_row(bars[sh.symbol].loc[slot]), sh.stop, sh.target,
                                is_entry_candle=slot.to_pydatetime() == sh.entry_time,
                                time_exit=time_exit_for(slot, sh.entry_time),
                                trigger=target_trigger(sh.target, trade_through))
                if hit is not None:
                    close_shadow(sh, slot.to_pydatetime(), hit[0], hit[1], hit[2], False)
                    del shadow_open[sid]

            # 1. Fill pending entries at this candle's open.
            for sym, pos in list(pending.items()):
                del pending[sym]
                row = bars[sym].loc[slot] if slot in bars[sym].index else None
                reason = "no candle at the next slot"
                price = None
                if row is not None:
                    price, reason = entry_fill(Candle.from_row(row), D(pos.setup.entry_reference),
                                               D(pos.setup.stop_reference),
                                               settings.backtest.max_entry_gap_pct)
                if price is not None:
                    rt = round_trip(rates, price, price, pos.quantity, settings.execution, None)
                    need = rt.buy_turnover + rt.buy.total + rt.execution.spread_buy \
                        + rt.execution.slippage_buy
                    if need > cash:
                        price, reason = None, f"cash {cash} < required {need} at fill"
                if price is None:
                    result.orders.append(OrderRecord(pos.entry_order, pos.setup.signal_id, sym,
                                                     "BUY", "MARKET_ENTRY", slot.to_pydatetime(),
                                                     D(pos.setup.entry_reference), pos.quantity,
                                                     "CANCELLED", reason))
                    result.rejected.append(RejectedSignal(slot.to_pydatetime(), sym,
                                                          pos.setup.signal_id, "execution",
                                                          "ENTRY_NOT_FILLED", reason))
                    state.open_positions = [p for p in state.open_positions
                                            if p.signal_id != pos.setup.signal_id]
                    state.trades_taken -= 1
                    continue
                cash -= need
                pos.entry_time, pos.entry_price, pos.buy_cash = slot.to_pydatetime(), price, need
                result.orders.append(OrderRecord(pos.entry_order, pos.setup.signal_id, sym, "BUY",
                                                 "MARKET_ENTRY", pos.setup.signal_time,
                                                 D(pos.setup.entry_reference), pos.quantity,
                                                 "FILLED"))
                result.fills.append(FillRecord(pos.entry_order, sym, "BUY", pos.entry_time,
                                               price, pos.quantity, reason))
                for p in state.open_positions:
                    if p.signal_id == pos.setup.signal_id:
                        p.capital_used = need
                open_pos[sym] = pos

            # 2. Exits on this candle.
            for sym, pos in list(open_pos.items()):
                if slot not in bars[sym].index:
                    continue
                candle = Candle.from_row(bars[sym].loc[slot])
                target = D(pos.setup.target_reference)
                hit = exit_fill(candle, D(pos.setup.stop_reference), target,
                                is_entry_candle=slot.to_pydatetime() == pos.entry_time,
                                time_exit=time_exit_for(slot, pos.entry_time),
                                trigger=target_trigger(target, trade_through))
                if hit is not None:
                    price, why, ambiguous = hit
                    close_position(sym, pos, slot.to_pydatetime(), price, why, ambiguous, False)
                    del open_pos[sym]

            # 3. Mark to market at the candle close.
            close_time = (slot + step).to_pydatetime()
            for sym in bars:
                if slot in bars[sym].index:
                    last_close[sym] = D(float(bars[sym].loc[slot, "close"]))
            pos_value = sum((last_close[s] * p.quantity for s, p in open_pos.items()), Decimal(0))
            result.equity.append(EquityPoint(close_time, cash, pos_value, cash + pos_value))

            # 4. Scan for new signals at this close. A locked day still scans, so
            #    blocked signals are recorded with their rejection reason.
            if slot + step > cutoff:
                continue
            if not active:
                continue
            scan = run_scan(provider, settings, calendar, close_time, symbols=active)
            for cand in scan.candidates:
                codes = {r.code for r in cand.screen.rejections}
                # Static screen failure, or the symbol's one signal for the day
                # (Phase 2 rule) has already fired: nothing new can come from it.
                if codes & STATIC_REJECTIONS or cand.setup is not None:
                    active.discard(cand.symbol)
            for cand in scan.ranked:
                setup = cand.setup
                if setup.signal_time != close_time or setup.signal_id in state.seen_signal_ids:
                    continue
                if cand.symbol in open_pos or cand.symbol in pending:
                    continue
                report = engine.evaluate(TradeEvaluationRequest.from_candidate(cand, settings),
                                         state)
                state.seen_signal_ids.add(setup.signal_id)
                if report.status is not ReviewStatus.QUALIFIED_FOR_REVIEW:
                    state.rejected_signal_ids.add(setup.signal_id)
                    reasons = "; ".join(f"{r.code}: {r.message}" for r in report.rejection_reasons)
                    result.rejected.append(RejectedSignal(
                        close_time, cand.symbol, setup.signal_id, "risk", report.status.value,
                        reasons))
                    if shadow:
                        shadow_pending[setup.signal_id] = ShadowOutcome(
                            signal_id=setup.signal_id, symbol=cand.symbol, session=day,
                            signal_time=setup.signal_time, review_status=report.status.value,
                            reasons=reasons, entry_reference=D(setup.entry_reference),
                            stop=D(setup.stop_reference), target=D(setup.target_reference),
                            filled=False)
                    continue
                pos = _Position(setup, report, report.final_quantity, entry_order=next_id("order"))
                pending[cand.symbol] = pos
                state.trades_taken += 1
                state.open_positions.append(OpenPosition(
                    signal_id=setup.signal_id, symbol=cand.symbol, entry=D(setup.entry_reference),
                    quantity=pos.quantity,
                    capital_used=report.position_value + report.at_target.buy.total,
                    planned_net_loss=report.planned_net_loss_at_stop))

        # Session over: anything still open closes at its last known price.
        for sym, pos in open_pos.items():
            last_slot = bars[sym].index[-1]
            price = D(float(bars[sym].loc[last_slot, "close"]))
            close_position(sym, pos, (last_slot + step).to_pydatetime(), price,
                           ExitReason.DATA_END, False, True)
        for sym, pos in pending.items():
            result.rejected.append(RejectedSignal(pos.setup.signal_time, sym, pos.setup.signal_id,
                                                  "execution", "ENTRY_NOT_FILLED",
                                                  "session data ended before the next candle"))
        for sh in shadow_open.values():
            last_slot = bars[sh.symbol].index[-1]
            close_shadow(sh, (last_slot + step).to_pydatetime(),
                         D(float(bars[sh.symbol].loc[last_slot, "close"])), ExitReason.DATA_END,
                         False, True)
        for sh in shadow_pending.values():
            sh.not_filled_reason = "session data ended before the next candle"
            result.shadow.append(sh)
        result.daily_equity.append(EquityPoint(
            datetime.combine(day, settings.market.session_close, tzinfo=calendar.tz),
            cash, Decimal(0), cash))

    result.final_cash = cash
    return result


def _assumptions(settings: Settings, engine: RiskCostEngine) -> list[str]:
    s = settings
    out = [
        "SIMULATED results from historical candles - not actual trading results.",
        f"Execution model {execution_model_label(s.backtest.target_trade_through_pct)}: "
        "entries at the next candle open; stop/target "
        "intrabar; stop assumed first when both are inside one candle; gaps through the stop "
        "fill at the open; time exit at the open of the first candle at/after "
        f"{s.strategy.eod_exit_time:%H:%M}.",
        f"Spread assumed {s.execution.assumed_spread_pct}% (no historical quotes); slippage "
        f"{s.execution.slippage_pct_per_side}% per fill.",
        f"Max entry gap {s.backtest.max_entry_gap_pct}% above the entry reference.",
        f"Fee schedule {engine.schedule.schedule_id} "
        f"({'ILLUSTRATIVE' if engine.schedule.illustrative else 'broker tariff'}).",
        "Each candidate is assessed with Phase 3 sizing and limits against the running "
        "daily state; positions never carry overnight.",
        "Rejected signals = technically qualified setups that failed risk/cost review or "
        "could not be filled. Breakouts failing Phase 2 rules are not signals.",
    ]
    if s.cost_review.apply_current_rates_to_past_dates:
        out.append("Current fee rates applied retroactively to earlier sessions.")
    return out
