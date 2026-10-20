"""Paper-trading engine: the Phase 2 strategy, Phase 3 risk/costs and Phase 4
execution rules applied to new market data, with persistent state.

``step(now)`` is the unit of work. It is idempotent: it reads all state from
the store, so calling it twice, or after a restart, never duplicates a
signal, order, fill or exit. Each step:

1. checks data health (stale or failing data blocks NEW signals);
2. fills or expires pending entry orders;
3. evaluates exits for open positions on newly completed candles, and flags
   positions whose data is missing (never inventing an exit);
4. stores an equity snapshot;
5. scans for new signals (completed candles only) and sends each new one
   through the Phase 3 risk/cost engine, creating at most one order per
   signal;
6. after the session closes: expires leftover orders, reconciles cash and
   writes the daily report.

Accounting:
- ``cash``: the paper account balance. ``reserved``: part of cash earmarked
  for pending entry orders (estimated need). Available = cash - reserved.
- Entry fill: cash -= qty x price + buy fees + buy spread + buy slippage.
- Exit fill: cash += qty x price - sell fees - sell spread - sell slippage.
- Realized P&L (net) is booked only at exit. Unrealized P&L = qty x (mark -
  entry price), shown separately and never counted as realized.
- Equity = cash + qty x mark price of open positions.
- Ledger invariant, checked on every start: cash = initial capital + sum of
  closed net P&L - sum of buy cash of open positions.

Nothing here talks to a broker's order API. Execution is simulated in this
process (``app/paper/execution.py``); there is no execution adapter that
could be pointed at a broker, and ``settings.execution_mode`` must be
"paper" (anything else is rejected when the engine is created).

Phase 8 operations:
- ``open_session`` refuses to continue on CRITICAL persisted-state findings
  (``app.ops.reconciliation.consistency_checks``), never repairing anything,
  and raises a CRITICAL alert.
- Every step writes a heartbeat (``meta.heartbeat``: time, provider health,
  fresh symbols, whether signals were blocked) for ``python -m app.ops
  health``.
- Alerts (``app.ops.alerts``): blocked data, daily-loss lock, flagged
  positions, unresolved positions at close, report failures, reconciliation
  issues. Alerting never interrupts the engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd

from app.backtesting.engine import STRATEGY_VERSION
from app.backtesting.execution import Candle, ExitReason, entry_fill, exit_fill
from app.config import Settings
from app.costs.money import D
from app.costs.schedule import FeeScheduleError, ResolvedRates, Side
from app.data.base_provider import MarketDataProvider
from app.data.models import DataError, parse_interval
from app.paper.execution import (
    PAPER_EXECUTION_MODEL_VERSION,
    entry_price_ok,
    fill_costs,
    usable_quote,
)
from app.paper.models import OrderState, PositionState, SessionState, order_id, position_id
from app.paper.store import PaperStore, StoreError
from app.risk.engine import RiskCostEngine
from app.risk.interfaces import TradeEvaluationRequest
from app.risk.risk_limits import DailyRiskState, OpenPosition
from app.scan.models import ReviewStatus
from app.scan.pipeline import run_scan
from app.utils.logging import get_logger
from app.utils.market_calendar import MarketCalendar

log = get_logger(__name__)

# Signal status recorded when the optional signal gate (Phase 12, AI-assisted
# paper arm) blocks a signal the risk engine approved.
GATE_REJECTED = "NOT_CONFIRMED_BY_AI"


class PaperStateError(RuntimeError):
    """Persisted state is inconsistent; the engine refuses to continue."""


@dataclass
class StepResult:
    now: datetime
    signals_blocked: bool = False
    new_signals: int = 0
    orders_created: int = 0
    fills: int = 0
    exits: int = 0
    warnings: list[str] = field(default_factory=list)


class PaperEngine:
    def __init__(self, provider: MarketDataProvider, settings: Settings,
                 calendar: MarketCalendar, store: PaperStore,
                 risk_engine: RiskCostEngine | None = None, recorder=None):
        self.provider = provider
        # Optional Phase 6 EvaluationRecorder (data quality, signal audit, sessions).
        self.recorder = recorder
        self.settings = settings
        self.calendar = calendar
        self.store = store
        self.risk = risk_engine or RiskCostEngine(settings)
        self.iv = parse_interval(settings.strategy.candle_interval)
        self.step_td = self.iv.timedelta
        self._health: dict[str, str] = {}  # symbol -> last logged health state
        if settings.execution_mode != "paper":
            raise ValueError(f"unsupported execution mode {settings.execution_mode!r}: this "
                             "application only simulates (paper) orders")
        self.notifier = None  # app.ops.alerts.Notifier; None = log notifier
        self._fresh = (0, 0)
        # Phase 12: optional extra condition for a signal the risk engine approved,
        # ``gate(candidate, now) -> (allowed, reason[, status])``. The AI-assisted
        # paper arm uses it to require a persisted CANDIDATE recommendation;
        # Phase 15 monitoring uses it to pause new entries (status
        # PAUSED_BY_MONITORING). It can only block an order, never create one or
        # change its size; an exception in the gate blocks the order (fail
        # closed). None = no extra condition.
        self.signal_gate = None

    def _alert(self, key: str, severity: str, component: str, message: str,
               now: datetime) -> None:
        from app.ops.alerts import safe_alert

        safe_alert(self.store, self.settings, key, severity, component, message, now,
                   self.notifier)

    def _resolve_alert(self, key: str, now: datetime) -> None:
        from app.ops.alerts import AlertManager

        try:
            AlertManager(self.store, self.settings).resolve(key, now)
        except Exception:  # noqa: BLE001
            pass

    def validate_state(self, now: datetime, day: date | None = None) -> list:
        """Ledger invariant plus persisted-state consistency. CRITICAL
        findings stop the engine (nothing is repaired); others are alerts."""
        from app.ops.reconciliation import consistency_checks

        try:
            self.verify_ledger(now, day)
        except PaperStateError as exc:
            self._alert("state.ledger", "CRITICAL", "paper-state", str(exc), now)
            raise
        findings = consistency_checks(self.store, self.settings, day or now.date(), now)
        critical = [f for f in findings if f.severity == "CRITICAL"]
        if critical:
            msg = "; ".join(f"{f.code} ({f.ref}): {f.message}" for f in critical)
            with self.store.transaction():
                self.store.event(now, day, "OPS", "ERROR", f"inconsistent persisted state: {msg}")
            self._alert("state.inconsistent", "CRITICAL", "paper-state", msg, now)
            raise PaperStateError(f"inconsistent persisted state, refusing to continue: {msg}")
        for f in findings:
            if f.severity == "ERROR":
                self._alert(f"state.{f.code}.{f.ref}", "ERROR", "paper-state",
                            f"{f.code} ({f.ref}): {f.message}", now)
        return findings

    # -- session lifecycle ----------------------------------------------------

    def preflight(self, day: date) -> list[str]:
        """Problems that prevent a session on ``day`` (empty list = OK)."""
        problems = []
        if not self.calendar.is_trading_day(day):
            name = self.calendar.holidays.get(day, "weekend" if day.weekday() >= 5 else "")
            problems.append(f"{day} is not a trading day ({name})")
        if not self.calendar.covers_year(day.year):
            problems.append(f"holiday list has no entries for {day.year}; fill "
                            "config/nse_holidays.yaml from NSE before trading")
        if self.risk.schedule is None:
            problems.append(f"fee schedule unavailable: {self.risk.schedule_error}")
        else:
            try:
                self.risk.schedule.rates_on(
                    day, self.settings.cost_review.apply_current_rates_to_past_dates)
            except FeeScheduleError as exc:
                problems.append(str(exc))
        if self.calendar.special_session(day) is not None:
            problems.append("special session: the strategy's times are defined for regular "
                            "sessions only; not trading it")
        return problems

    def open_session(self, day: date, now: datetime) -> SessionState:
        s = self.settings
        created = self.store.init_portfolio(D(s.capital.initial_capital), now)
        pf = self.store.portfolio()
        with self.store.transaction():
            if created:
                self.store.event(now, day, "SESSION", "INFO",
                                 f"paper portfolio created with Rs {pf['initial_capital']}")
            elif D(pf["initial_capital"]) != D(s.capital.initial_capital):
                self.store.event(now, day, "SESSION", "WARNING",
                                 f"configured capital {s.capital.initial_capital} differs from "
                                 f"the existing paper portfolio ({pf['initial_capital']}); the "
                                 "portfolio is NOT reset")
        self.validate_state(now, day)
        existing = self.store.session(day)
        if existing is not None:
            with self.store.transaction():
                self.store.event(now, day, "RECOVERY", "INFO",
                                 f"resuming existing session (state {existing['state']})")
            return SessionState(existing["state"])
        with self.store.transaction():
            self.store.create_session({
                "trading_date": day, "state": SessionState.ACTIVE.value,
                "data_mode": self.provider.mode.value, "data_source": self.provider.name,
                "config_version": s.version(), "strategy_version": STRATEGY_VERSION,
                "execution_model_version": PAPER_EXECUTION_MODEL_VERSION,
                "fee_schedule_id": self.risk.schedule.schedule_id,
                "start_cash": D(pf["cash"]), "started_at": now,
            })
            self.store.event(now, day, "SESSION", "INFO",
                             f"session opened: data {self.provider.name} "
                             f"[{self.provider.mode.value.upper()}], cash Rs {pf['cash']}")
        return SessionState.ACTIVE

    # -- Phase 6 evaluation hooks (no-ops without a recorder) -------------------

    def begin_evaluation_session(self, day: date, now: datetime) -> None:
        if self.recorder is None or self.recorder.active() is None:
            return
        from app.data.models import DataMode

        if self.provider.mode in (DataMode.LIVE, DataMode.DELAYED):
            self.recorder.sync_schedule(day, now)
        self.recorder.session_started(day, now, self.provider.name, self.provider.mode,
                                      self.risk.schedule, STRATEGY_VERSION,
                                      PAPER_EXECUTION_MODEL_VERSION)

    def end_evaluation_session(self, day: date, now: datetime):
        """Reconcile, record issues (never repaired), set the final session
        status and write the daily evaluation report."""
        if self.recorder is None or self.recorder.active() is None:
            return None
        from app.evaluation.reconcile import reconcile
        from app.evaluation.report import write_daily

        issues = reconcile(self.store, day, self.settings, self.risk.schedule, self.provider)
        self.recorder.record_issues(day, now, issues)
        if issues:
            with self.store.transaction():
                for i in issues:
                    self.store.event(now, day, "RECONCILIATION", "ERROR",
                                     f"{i.code} ({i.ref}): {i.message}")
            self._alert(f"reconciliation.{day}", "ERROR", "reconciliation",
                        f"{len(issues)} reconciliation issue(s) on {day}: "
                        + "; ".join(f"{i.code} ({i.ref})" for i in issues[:5]), now)
        status = self.recorder.session_finished(day, now, self.provider.mode, issues)
        write_daily(self.store, day, self.settings, self.recorder.active()["evaluation_id"])
        return status

    def verify_ledger(self, now: datetime, day: date | None = None) -> None:
        pf = self.store.portfolio()
        closed = self.store.positions(states=(PositionState.CLOSED,))
        open_ = self.store.positions(states=(PositionState.OPEN, PositionState.FLAGGED))
        if any(p["net_pnl"] is None for p in closed):
            raise PaperStateError("a closed position has no net P&L; refusing to continue")
        expected_cash = (D(pf["initial_capital"]) + sum((D(p["net_pnl"]) for p in closed), Decimal(0))
                         - sum((D(p["buy_cash"]) for p in open_), Decimal(0)))
        pending = self.store.orders(state=OrderState.PENDING)
        expected_reserved = sum((D(o["reserved_cash"]) for o in pending), Decimal(0))
        if D(pf["cash"]) != expected_cash or D(pf["reserved"]) != expected_reserved:
            msg = (f"ledger mismatch: cash {pf['cash']} vs expected {expected_cash}, reserved "
                   f"{pf['reserved']} vs expected {expected_reserved}")
            with self.store.transaction():
                self.store.event(now, day, "OPS", "ERROR", msg)
            raise PaperStateError(msg)

    # -- main step ------------------------------------------------------------

    def step(self, now: datetime) -> StepResult:
        now = now.astimezone(self.calendar.tz)
        day = now.date()
        result = StepResult(now)
        session = self.store.session(day)
        if session is None:
            raise PaperStateError(f"no session for {day}; call open_session first")

        candles, blocked = self._health_check(day, now, result)
        result.signals_blocked = blocked
        rates = self._rates(day)

        for order in self.store.orders(state=OrderState.PENDING):
            result.fills += self._process_entry(order, now, rates, candles)
        for pos in self.store.positions(states=(PositionState.OPEN, PositionState.FLAGGED)):
            result.exits += self._process_position(pos, now, candles)
        self._resolve_old_sessions(day, now)
        self._snapshot(day, now, candles)

        _, close_dt = self.calendar.session_bounds(day)
        eod = datetime.combine(day, self.settings.strategy.eod_exit_time, tzinfo=self.calendar.tz)
        if session["state"] == SessionState.ACTIVE.value and not blocked and now < eod:
            self._scan(day, now, rates, candles, result)
            for order in self.store.orders(state=OrderState.PENDING):
                result.fills += self._process_entry(order, now, rates, candles)

        if now >= close_dt and session["state"] == SessionState.ACTIVE.value:
            self.close_session(day, now)
        self._heartbeat(day, now, blocked)
        return result

    def _heartbeat(self, day: date, now: datetime, blocked: bool) -> None:
        health = self.provider.health() if hasattr(self.provider, "health") else \
            {"state": "n/a", "provider": self.provider.name}
        fresh, total = self._fresh
        try:
            with self.store.transaction():
                self.store.set_meta("heartbeat", {
                    "ts": now, "day": day, "provider_name": self.provider.name,
                    "data_mode": self.provider.mode.value, "provider": health,
                    "symbols_ok": fresh, "symbols_total": total, "signals_blocked": blocked,
                    "blocked_reason": "no symbol has fresh, valid market data" if blocked
                    else None})
        except Exception:  # noqa: BLE001 - the heartbeat must never fail a step
            pass
        if blocked:
            self._alert("data.blocked", "ERROR", "market-data",
                        "no symbol has fresh, valid market data; new signals are blocked "
                        f"(provider {health.get('state')}: {health.get('last_error')})", now)
        else:
            self._resolve_alert("data.blocked", now)

    # -- data health ----------------------------------------------------------

    def _health_check(self, day: date, now: datetime, result: StepResult):
        """Completed candles per symbol for ``day``, and whether new signals
        must be blocked (provider failing or every symbol stale)."""
        open_dt, close_dt = self.calendar.session_bounds(day)
        effective = min(now, close_dt)
        expected_end = None
        if effective >= open_dt + self.step_td:
            expected_end = open_dt + ((effective - open_dt) // self.step_td) * self.step_td
        tolerance = timedelta(seconds=self.settings.data.max_staleness_seconds)
        candles: dict[str, pd.DataFrame] = {}
        fresh = 0
        batches = []
        symbols = [i.symbol for i in self.provider.list_instruments()]
        for sym in symbols:
            error = None
            frame = None
            try:
                series = self.provider.get_candles(sym, self.iv.label, day, day)
                frame = series.completed(now)
                state, msg = "OK", ""
                if expected_end is not None:
                    last_end = (frame["timestamp"].iloc[-1] + self.step_td) if not frame.empty \
                        else None
                    if last_end is None or last_end < expected_end - tolerance:
                        state = "STALE"
                        msg = (f"latest candle ends {last_end:%H:%M}" if last_end is not None
                               else "no completed candles") + f", expected {expected_end:%H:%M}"
                candles[sym] = frame
            except DataError as exc:
                state, msg = "ERROR", str(exc)
                error = msg
            if state == "OK":
                fresh += 1
            self._log_health(sym, state, msg, day, now, result)
            if self.recorder is not None:
                batches.append(self._assess(sym, frame, day, now, error))
        blocked = expected_end is not None and fresh == 0
        self._fresh = (fresh, len(symbols))
        if self.recorder is not None:
            health = self.provider.health() if hasattr(self.provider, "health") else None
            reason = "no symbol has fresh, valid market data" if blocked else None
            self.recorder.record_step(day, now, batches, health, blocked, reason,
                                      self.provider.mode)
        return candles, blocked

    def _assess(self, sym: str, frame, day: date, now: datetime, error: str | None):
        """Data-quality counts for one symbol, from the raw batch as received
        (trimmed to candles completed by ``now`` - never future rows)."""
        from app.evaluation.quality import assess_batch

        raw = getattr(self.provider, "last_raw", {}).get(sym)
        if raw is None:
            raw = frame
        if raw is not None and not raw.empty:
            ts = pd.to_datetime(raw["timestamp"])
            raw = raw[(ts + self.step_td) <= pd.Timestamp(now)]
        return assess_batch(sym, raw, day, now, self.calendar, self.iv,
                            self.settings.data.max_staleness_seconds, error)

    def _log_health(self, sym: str, state: str, msg: str, day: date, now: datetime,
                    result: StepResult) -> None:
        if self._health.get(sym) == state:
            return
        previous = self._health.get(sym)
        self._health[sym] = state
        if state == "OK" and previous is None:
            return
        level = "INFO" if state == "OK" else "WARNING"
        text = f"{sym}: data {state}" + (f" ({msg})" if msg else "")
        if state != "OK":
            result.warnings.append(text)
        with self.store.transaction():
            self.store.event(now, day, "HEALTH", level, text, {"symbol": sym, "state": state})
        log.warning(text) if state != "OK" else log.info(text)

    # -- entries --------------------------------------------------------------

    def _process_entry(self, order, now: datetime, rates: ResolvedRates | None,
                       candles: dict[str, pd.DataFrame]) -> int:
        sig = self.store.one("SELECT * FROM signals WHERE signal_id = ?", (order["signal_id"],))
        sym = order["symbol"]
        signal_time = datetime.fromisoformat(sig["signal_time"])
        valid_until = datetime.fromisoformat(sig["valid_until"])
        ref, stop = D(sig["entry_reference"]), D(sig["stop"])
        gap = self.settings.backtest.max_entry_gap_pct

        price, basis, reject, spread_in_price, fill_time = None, "", None, False, now
        quote = None
        try:
            quote = self.provider.get_quote(sym)
        except DataError:
            pass
        ok, _why = usable_quote(quote, signal_time, now, self.settings.data.max_staleness_seconds)
        if ok:
            price, fill_time = D(quote.ask), quote.timestamp
            basis = f"quote ask {quote.ask} @ {quote.timestamp.isoformat()} ({quote.source})"
            spread_in_price = True
            reject = entry_price_ok(price, ref, stop, gap)
        else:
            frame = candles.get(sym)
            if frame is not None and not frame.empty:
                nxt = frame[frame["timestamp"] == pd.Timestamp(signal_time)]
                later = frame[frame["timestamp"] > pd.Timestamp(signal_time)]
                if not nxt.empty:
                    candle = Candle.from_row(nxt.iloc[0])
                    price, reason = entry_fill(candle, ref, stop, gap)
                    fill_time = signal_time
                    basis = f"candle open @ {signal_time.isoformat()}"
                    if price is None:
                        reject = reason
                        price = candle.open
                elif not later.empty:
                    reject = "no candle at the next slot after the signal"
        if price is None and reject is None:
            if now > valid_until:
                with self.store.transaction():
                    self._release(order)
                    self.store.transition_order(order["order_id"], OrderState.EXPIRED, now,
                                                reason=f"not filled by {valid_until:%H:%M}")
                    self.store.event(now, fill_time.date(), "ORDER", "INFO",
                                     f"{sym} entry order expired")
            return 0
        if reject is not None:
            with self.store.transaction():
                self._release(order)
                self.store.transition_order(order["order_id"], OrderState.REJECTED, now,
                                            reason=reject)
                self.store.event(now, signal_time.date(), "ORDER", "INFO",
                                 f"{sym} entry rejected at fill: {reject}")
            return 0
        if rates is None:
            return 0  # cannot cost the fill; leave pending (health event already raised)

        qty = order["quantity"]
        costs = fill_costs(rates, Side.BUY, price, qty, self.settings, spread_in_price)
        need = price * qty + costs.total
        pf = self.store.portfolio()
        available = D(pf["cash"]) - D(pf["reserved"]) + D(order["reserved_cash"])
        with self.store.transaction():
            if need > available:
                self._release(order)
                self.store.transition_order(order["order_id"], OrderState.REJECTED, now,
                                            reason=f"cash {available} < required {need} at fill")
                return 0
            pf = self.store.portfolio()
            self.store.set_cash(D(pf["cash"]) - need,
                                D(pf["reserved"]) - D(order["reserved_cash"]))
            self.store.transition_order(
                order["order_id"], OrderState.FILLED, now, fill_price=price,
                fill_time=fill_time, fill_quantity=qty, fill_basis=basis,
                est_fees=costs.fees.total, est_spread=costs.spread,
                est_slippage=costs.slippage)
            self.store.create_position({
                "position_id": order["position_id"], "signal_id": order["signal_id"],
                "trading_date": signal_time.date(), "symbol": sym, "quantity": qty,
                "entry_price": price, "entry_time": fill_time, "stop": stop,
                "target": D(sig["target"]), "planned_net_loss": D(sig["planned_net_loss"]),
                "buy_cash": need, "buy_costs": costs.total,
                "costs": json.dumps({"buy": costs.breakdown()}, sort_keys=True),
                "mark_price": price, "mark_time": fill_time,
            })
            self.store.event(now, signal_time.date(), "FILL", "INFO",
                             f"{sym} BUY {qty} @ {price} ({basis})")
        return 1

    def _release(self, order) -> None:
        pf = self.store.portfolio()
        self.store.set_cash(D(pf["cash"]), D(pf["reserved"]) - D(order["reserved_cash"]))

    # -- exits ----------------------------------------------------------------

    def _process_position(self, pos, now: datetime, candles: dict[str, pd.DataFrame]) -> int:
        day = date.fromisoformat(pos["trading_date"])
        sym = pos["symbol"]
        frame = candles.get(sym) if day == now.date() else None
        if frame is None:
            try:
                frame = self.provider.get_candles(sym, self.iv.label, day, day).completed(now)
            except DataError as exc:
                return self._flag(pos, now, f"no data: {exc}")
        open_dt, close_dt = self.calendar.session_bounds(day)
        entry_time = datetime.fromisoformat(pos["entry_time"])
        entry_slot = open_dt + ((entry_time - open_dt) // self.step_td) * self.step_td
        last = datetime.fromisoformat(pos["last_candle"]) if pos["last_candle"] else None
        todo = frame[frame["timestamp"] >= pd.Timestamp(entry_slot)]
        if last is not None:
            todo = todo[todo["timestamp"] > pd.Timestamp(last)]

        eod = pd.Timestamp(datetime.combine(day, self.settings.strategy.eod_exit_time,
                                            tzinfo=self.calendar.tz))
        max_hold = self.settings.backtest.max_holding_minutes
        stop, target = D(pos["stop"]), D(pos["target"])
        for _, row in todo.iterrows():
            slot = row["timestamp"]
            time_exit = None
            if slot >= eod:
                time_exit = ExitReason.END_OF_DAY
            elif max_hold is not None and slot >= pd.Timestamp(entry_time) + pd.Timedelta(
                    minutes=max_hold):
                time_exit = ExitReason.MAX_HOLDING
            hit = exit_fill(Candle.from_row(row), stop, target,
                            is_entry_candle=slot == pd.Timestamp(entry_slot), time_exit=time_exit)
            if hit is not None:
                price, reason, ambiguous = hit
                self._close(pos, slot.to_pydatetime(), price, reason, ambiguous, now)
                return 1
            with self.store.transaction():
                self.store.update_position(pos["position_id"], last_candle=slot.to_pydatetime(),
                                           mark_price=D(float(row["close"])),
                                           mark_time=(slot + self.step_td).to_pydatetime())

        # Still open: is the data keeping up?
        effective = min(now, close_dt)
        expected_end = open_dt + ((effective - open_dt) // self.step_td) * self.step_td
        last_end = (frame["timestamp"].iloc[-1] + self.step_td) if not frame.empty else None
        tolerance = timedelta(seconds=self.settings.data.max_staleness_seconds)
        if expected_end > entry_slot and (last_end is None or last_end < expected_end - tolerance):
            return self._flag(pos, now, f"candles missing after "
                                        f"{last_end:%H:%M}" if last_end is not None
                              else "no candles")
        if pos["state"] == PositionState.FLAGGED.value:
            with self.store.transaction():
                self.store.transition_position(pos["position_id"], PositionState.OPEN,
                                               flag_reason=None)
                self.store.event(now, day, "POSITION", "INFO", f"{sym}: data recovered, "
                                                               "position monitored again")
        return 0

    def _flag(self, pos, now: datetime, reason: str) -> int:
        if pos["state"] != PositionState.FLAGGED.value:
            with self.store.transaction():
                self.store.transition_position(pos["position_id"], PositionState.FLAGGED,
                                               flag_reason=reason)
                self.store.event(now, date.fromisoformat(pos["trading_date"]), "POSITION",
                                 "WARNING", f"{pos['symbol']}: exit cannot be evaluated - "
                                            f"{reason}. No exit is assumed.")
            self._alert(f"position.flagged.{pos['position_id']}", "WARNING", "paper-position",
                        f"{pos['symbol']}: exit cannot be evaluated ({reason}); no exit assumed",
                        now)
        return 0

    def _close(self, pos, when: datetime, price: Decimal, reason: ExitReason, ambiguous: bool,
               now: datetime) -> None:
        day = date.fromisoformat(pos["trading_date"])
        rates = self._rates(day)
        if rates is None:
            self._flag(pos, now, "fee rates unavailable; cannot cost the exit")
            return
        qty = pos["quantity"]
        sell = fill_costs(rates, Side.SELL, price, qty, self.settings, spread_in_price=False)
        entry = D(pos["entry_price"])
        gross = (price - entry) * qty
        total_costs = D(pos["buy_costs"]) + sell.total
        net = gross - total_costs
        costs = json.loads(pos["costs"])
        costs["sell"] = sell.breakdown()
        oid = order_id(pos["signal_id"], "SELL")
        with self.store.transaction():
            self.store.create_order({
                "order_id": oid, "signal_id": pos["signal_id"], "position_id": pos["position_id"],
                "symbol": pos["symbol"], "exchange": "NSE", "side": "SELL", "kind": reason.value,
                "quantity": qty, "created_at": when, "reference_price": price,
                "execution_model_version": PAPER_EXECUTION_MODEL_VERSION, "updated_at": now,
            })
            self.store.transition_order(oid, OrderState.PENDING, now)
            self.store.transition_order(
                oid, OrderState.FILLED, now, fill_price=price, fill_time=when, fill_quantity=qty,
                fill_basis=f"candle-v1 {reason.value} @ {when.isoformat()}",
                est_fees=sell.fees.total, est_spread=sell.spread, est_slippage=sell.slippage)
            pf = self.store.portfolio()
            self.store.set_cash(D(pf["cash"]) + price * qty - sell.total, D(pf["reserved"]))
            self.store.transition_position(
                pos["position_id"], PositionState.CLOSED, exit_price=price, exit_time=when,
                exit_reason=reason.value, ambiguous=int(ambiguous), gross_pnl=gross,
                costs=json.dumps(costs, sort_keys=True), total_costs=total_costs, net_pnl=net,
                mark_price=price, mark_time=when, flag_reason=None)
            self.store.event(now, day, "FILL", "INFO",
                             f"{pos['symbol']} SELL {qty} @ {price} ({reason.value}); "
                             f"net {net}" + (" [ambiguous candle]" if ambiguous else ""))
            self._maybe_lock(day, now)

    def _maybe_lock(self, day: date, now: datetime) -> None:
        session = self.store.session(day)
        if session is None or session["locked"]:
            return
        realized = self._realized(day)
        if realized is not None and realized <= -D(self.settings.risk.max_daily_loss):
            reason = (f"daily realized loss Rs {-realized} reached the limit "
                      f"Rs {self.settings.risk.max_daily_loss}")
            self.store.update_session(day, {"locked": 1, "lock_reason": reason})
            self.store.event(now, day, "RISK", "WARNING", f"daily loss lock: {reason}")
            self._alert(f"risk.daily_loss_lock.{day}", "WARNING", "risk",
                        f"daily loss lock: {reason}; no new paper entries today", now)

    # -- signals --------------------------------------------------------------

    def _scan(self, day: date, now: datetime, rates: ResolvedRates | None,
              candles: dict[str, pd.DataFrame], result: StepResult) -> None:
        busy = {p["symbol"] for p in self.store.positions(
            states=(PositionState.OPEN, PositionState.FLAGGED))}
        busy |= {o["symbol"] for o in self.store.orders(state=OrderState.PENDING)}
        try:
            scan = run_scan(self.provider, self.settings, self.calendar, now)
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed silently
            with self.store.transaction():
                self.store.event(now, day, "OPS", "ERROR", f"signal generation failed: {exc}")
            result.warnings.append(f"signal generation failed: {exc}")
            return
        with self.store.transaction():
            self.store.upsert_watchlist([{
                "trading_date": day, "symbol": c.symbol, "ts": now,
                "screen_status": c.screen.status.value, "signal_status": c.signal_status.value,
                "last_price": c.screen.last_price, "last_price_time": c.screen.last_price_time,
                "spread_pct": c.screen.spread_pct, "score": c.score,
                "reason": c.rejection_reasons[0].message if c.rejection_reasons else "",
            } for c in scan.candidates])
        outcomes: dict[str, tuple[str, object, str]] = {}
        try:
            self._process_signals(day, now, scan, busy, result, outcomes)
        finally:
            if self.recorder is not None:
                self.recorder.record_audit(day, now, self._audit_rows(scan, outcomes))

    def _process_signals(self, day, now, scan, busy, result, outcomes) -> None:
        for cand in scan.ranked:
            setup = cand.setup
            if self.store.signal_exists(setup.signal_id):
                outcomes[cand.symbol] = ("ALREADY_PROCESSED", None,
                                         f"signal {setup.signal_id} handled earlier")
                continue  # idempotent: processed before (maybe before a restart)
            result.new_signals += 1
            state = self._risk_state(day)
            report = self.risk.evaluate(TradeEvaluationRequest.from_candidate(cand, self.settings),
                                        state)
            status = report.status
            reasons = [f"{r.code}: {r.message}" for r in report.rejection_reasons]
            if status is ReviewStatus.QUALIFIED_FOR_REVIEW and cand.symbol in busy:
                status = ReviewStatus.REJECTED_RISK_LIMIT
                reasons.append("symbol_busy: a position or order for this symbol is open")
            gate_reason, gate_status = None, GATE_REJECTED
            if status is ReviewStatus.QUALIFIED_FOR_REVIEW and self.signal_gate is not None:
                try:
                    allowed, why, *extra = self.signal_gate(cand, now)
                except Exception as exc:  # noqa: BLE001 - fail closed, recorded
                    allowed, why, extra = False, f"gate error: {type(exc).__name__}: {exc}"[:300], []
                if not allowed:
                    gate_reason = why
                    gate_status = extra[0] if extra else GATE_REJECTED
                    reasons.append(f"{'gate' if extra else 'ai_gate'}: {why}")
            status_value = gate_status if gate_reason else status.value
            with self.store.transaction():
                self.store.record_signal({
                    "signal_id": setup.signal_id, "trading_date": day, "symbol": cand.symbol,
                    "signal_time": setup.signal_time, "entry_reference": D(setup.entry_reference),
                    "stop": D(setup.stop_reference), "target": D(setup.target_reference),
                    "valid_until": setup.valid_until, "review_status": status_value,
                    "quantity": report.final_quantity,
                    "planned_net_loss": report.planned_net_loss_at_stop,
                    "reasons": json.dumps(reasons), "created_at": now,
                })
                if gate_reason:
                    self.store.event(now, day, "AI_GATE" if gate_status == GATE_REJECTED
                                     else "MONITORING", "INFO",
                                     f"{cand.symbol} approved by the risk engine but not "
                                     f"ordered ({gate_status}): {gate_reason}")
                    outcomes[cand.symbol] = (gate_status, report, gate_reason)
                    continue
                if status is not ReviewStatus.QUALIFIED_FOR_REVIEW:
                    self.store.event(now, day, "RISK", "INFO",
                                     f"{cand.symbol} signal rejected: {status.value}",
                                     {"reasons": reasons})
                    outcomes[cand.symbol] = ("SIGNAL_REJECTED_RISK", report,
                                             "; ".join(reasons) or status.value)
                    continue
                reserve = report.position_value + report.at_target.buy.total \
                    + report.at_target.spread_cost / 2 + report.at_target.slippage_cost / 2
                pf = self.store.portfolio()
                if D(pf["cash"]) - D(pf["reserved"]) < reserve:
                    self.store.event(now, day, "RISK", "INFO",
                                     f"{cand.symbol} not ordered: available cash below estimate")
                    outcomes[cand.symbol] = ("NOT_ORDERED_CASH", report,
                                             "available cash below the estimated need")
                    continue
                oid = order_id(setup.signal_id, "BUY")
                created = self.store.create_order({
                    "order_id": oid, "signal_id": setup.signal_id,
                    "position_id": position_id(setup.signal_id), "symbol": cand.symbol,
                    "exchange": cand.exchange, "side": "BUY", "kind": "MARKET_ENTRY",
                    "quantity": report.final_quantity, "created_at": now,
                    "reference_price": D(setup.entry_reference), "reserved_cash": reserve,
                    "execution_model_version": PAPER_EXECUTION_MODEL_VERSION,
                    "est_fees": report.at_target.buy.total, "updated_at": now,
                })
                if not created:
                    outcomes[cand.symbol] = ("DUPLICATE_ORDER_PREVENTED", report,
                                             "an order for this signal already exists")
                    continue
                self.store.transition_order(oid, OrderState.PENDING, now)
                self.store.set_cash(D(pf["cash"]), D(pf["reserved"]) + reserve)
                self.store.event(now, day, "ORDER", "INFO",
                                 f"{cand.symbol} paper BUY {report.final_quantity} created "
                                 f"(ref {setup.entry_reference})")
                busy.add(cand.symbol)
                result.orders_created += 1
                outcomes[cand.symbol] = ("ORDER_CREATED", report,
                                         f"paper BUY {report.final_quantity} created")

    _NO_SIGNAL_DECISION = {
        "WATCH": "NO_SETUP_YET",
        "REJECTED": "NO_VALID_SETUP",
        "INSUFFICIENT_DATA": "INSUFFICIENT_DATA",
        "EXPIRED": "SETUP_NO_LONGER_VALID",
        "INVALIDATED": "SETUP_NO_LONGER_VALID",
        "QUALIFIED_PRE_COST": "NOT_PROCESSED",
    }

    def _audit_rows(self, scan, outcomes) -> list[dict]:
        """One audit row per screen-eligible symbol for this step."""
        rows = []
        for c in scan.candidates:
            if c.screen.status.value != "PASS":
                continue
            ind, setup = c.indicators, c.setup
            decision, report, reason = outcomes.get(c.symbol, (
                self._NO_SIGNAL_DECISION.get(c.signal_status.value, c.signal_status.value),
                None, c.rejection_reasons[0].message if c.rejection_reasons else ""))
            rows.append({
                "symbol": c.symbol, "data_source": c.data_source, "data_mode": c.data_mode.value,
                "screen_status": c.screen.status.value, "signal_status": c.signal_status.value,
                "signal_id": setup.signal_id if setup else None,
                "close": ind.close, "opening_range_high": ind.opening_range_high,
                "opening_range_low": ind.opening_range_low, "vwap": ind.vwap, "ema": ind.ema,
                "relative_volume": ind.relative_volume,
                "relative_volume_today": ind.relative_volume_today,
                "relative_volume_baseline": ind.relative_volume_baseline,
                "relative_volume_sessions": ind.relative_volume_sessions,
                "entry_reference": setup.entry_reference if setup else None,
                "stop": setup.stop_reference if setup else None,
                "target": setup.target_reference if setup else None,
                "quantity": report.final_quantity if report else None,
                "est_round_trip_costs": report.at_target.total_costs
                if report is not None and report.at_target is not None else None,
                "net_reward_risk": report.net_reward_risk if report else None,
                "risk_status": report.status.value if report else None,
                "risk_reasons": json.dumps([f"{r.code}: {r.message}"
                                            for r in report.rejection_reasons]) if report else None,
                "decision": decision, "reason": reason,
            })
        return rows

    def _realized(self, day: date) -> Decimal | None:
        closed = self.store.positions(day, (PositionState.CLOSED,))
        if any(p["net_pnl"] is None for p in closed):
            return None  # unknown is never zero
        return sum((D(p["net_pnl"]) for p in closed), Decimal(0))

    def _risk_state(self, day: date) -> DailyRiskState:
        session = self.store.session(day)
        state = DailyRiskState(trading_date=day, realized_pnl=self._realized(day),
                               account_capital=D(session["start_cash"]))
        for p in self.store.positions(states=(PositionState.OPEN, PositionState.FLAGGED)):
            state.open_positions.append(OpenPosition(
                signal_id=p["signal_id"], symbol=p["symbol"], entry=D(p["entry_price"]),
                quantity=p["quantity"], capital_used=D(p["buy_cash"]),
                planned_net_loss=D(p["planned_net_loss"])))
        for o in self.store.orders(state=OrderState.PENDING):
            sig = self.store.one("SELECT planned_net_loss FROM signals WHERE signal_id = ?",
                                 (o["signal_id"],))
            state.open_positions.append(OpenPosition(
                signal_id=o["signal_id"], symbol=o["symbol"], entry=D(o["reference_price"]),
                quantity=o["quantity"], capital_used=D(o["reserved_cash"]),
                planned_net_loss=D(sig["planned_net_loss"])))
        entries = self.store.all(
            "SELECT COUNT(*) AS n FROM orders o JOIN signals s USING (signal_id) "
            "WHERE s.trading_date = ? AND o.side = 'BUY' AND o.state IN ('PENDING', 'FILLED')",
            (day.isoformat(),))
        state.trades_taken = entries[0]["n"]
        state.seen_signal_ids = {r["signal_id"] for r in self.store.signals(day)}
        if session["locked"]:
            state.locked, state.lock_reason = True, session["lock_reason"]
        return state

    # -- snapshots, close ------------------------------------------------------

    def _snapshot(self, day: date, now: datetime, candles) -> None:
        pf = self.store.portfolio()
        value = Decimal(0)
        for p in self.store.positions(states=(PositionState.OPEN, PositionState.FLAGGED)):
            value += D(p["mark_price"]) * p["quantity"]
        with self.store.transaction():
            self.store.snapshot({"ts": now, "trading_date": day, "cash": D(pf["cash"]),
                                 "reserved": D(pf["reserved"]), "position_value": value,
                                 "equity": D(pf["cash"]) + value})

    def close_session(self, day: date, now: datetime) -> SessionState:
        from app.paper.report import build_report  # local import avoids a cycle

        with self.store.transaction():
            for order in self.store.orders(day, OrderState.PENDING):
                self._release(order)
                self.store.transition_order(order["order_id"], OrderState.EXPIRED, now,
                                            reason="session ended before fill")
        unresolved = self.store.positions(day, (PositionState.OPEN, PositionState.FLAGGED))
        self.verify_ledger(now, day)
        state = SessionState.NEEDS_ATTENTION if unresolved else SessionState.CLOSED
        with self.store.transaction():
            self.store.update_session(day, {"state": state.value, "ended_at": now})
            for p in unresolved:
                self.store.event(now, day, "OPS", "ERROR",
                                 f"{p['symbol']} paper position unresolved at session end "
                                 f"({p['state']}: {p['flag_reason']}); needs review")
            self.store.event(now, day, "SESSION", "INFO", f"session closed: {state.value}")
        if unresolved:
            self._alert(f"session.needs_attention.{day}", "WARNING", "paper-session",
                        f"{len(unresolved)} paper position(s) unresolved at session end", now)
        try:
            body = build_report(self.store, day, self.settings)
            with self.store.transaction():
                self.store.save_report(day, now, body)
                self.store.event(now, day, "REPORT", "INFO", "daily report generated")
        except Exception as exc:  # noqa: BLE001
            with self.store.transaction():
                self.store.event(now, day, "REPORT", "ERROR", f"report generation failed: {exc}")
            self._alert(f"report.failed.{day}", "ERROR", "reports",
                        f"daily report for {day} failed: {exc}; session state and financial "
                        "records are preserved; regenerate with `python -m app.paper report`",
                        now)
            raise
        self._resolve_alert(f"report.failed.{day}", now)
        return state

    def _resolve_old_sessions(self, today: date, now: datetime) -> None:
        """Earlier sessions left NEEDS_ATTENTION become CLOSED once all their
        positions were resolved from real data; their report is regenerated."""
        from app.paper.report import build_report

        rows = self.store.all("SELECT trading_date FROM sessions WHERE state = ? AND "
                              "trading_date < ?", (SessionState.NEEDS_ATTENTION.value,
                                                   today.isoformat()))
        for row in rows:
            day = date.fromisoformat(row["trading_date"])
            if self.store.positions(day, (PositionState.OPEN, PositionState.FLAGGED)):
                continue
            with self.store.transaction():
                self.store.update_session(day, {"state": SessionState.CLOSED.value})
                self.store.save_report(day, now, build_report(self.store, day, self.settings))
                self.store.event(now, day, "RECOVERY", "INFO",
                                 "unresolved positions resolved from market data; session "
                                 "closed and report regenerated")

    def _rates(self, day: date) -> ResolvedRates | None:
        try:
            return self.risk.schedule.rates_on(
                day, self.settings.cost_review.apply_current_rates_to_past_dates)
        except (FeeScheduleError, AttributeError):
            return None


__all__ = ["PaperEngine", "PaperStateError", "StepResult", "StoreError"]
