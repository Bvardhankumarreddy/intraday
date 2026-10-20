"""Daily and portfolio risk controls for hypothetical positions.

This tool cannot see or control orders placed manually at the broker. The
state here is a research ledger: hypothetical/paper positions and outcomes
that the user or the paper-trading engine records.

Lock rules:
- When realized P&L for the trading date reaches -max_daily_loss, the state
  locks. A locked state rejects every new candidate.
- The state belongs to one exchange trading date. ``roll_to`` returns a fresh
  state only when the exchange date has advanced; during the same date it
  returns the same (possibly locked) state, so a lock can never reset
  mid-session.
- ``realized_pnl = None`` means unknown, which is never treated as zero:
  every candidate is then INSUFFICIENT_DATA.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from app.config import Settings
from app.costs.money import D
from app.scan.models import Reason
from app.utils.market_calendar import MarketCalendar


class OpenPosition(BaseModel):
    signal_id: str
    symbol: str
    entry: Decimal
    quantity: int
    capital_used: Decimal  # turnover plus buy-side costs
    planned_net_loss: Decimal  # at the stop, including costs


class DailyRiskState(BaseModel):
    trading_date: date
    realized_pnl: Decimal | None
    # Account capital at the start of the session. None = use the configured
    # initial capital (live scans); a backtest sets its running balance here.
    account_capital: Decimal | None = None
    trades_taken: int = 0
    open_positions: list[OpenPosition] = []
    locked: bool = False
    lock_reason: str | None = None
    seen_signal_ids: set[str] = set()
    rejected_signal_ids: set[str] = set()

    @classmethod
    def new_session(cls, day: date, account_capital: Decimal | None = None) -> DailyRiskState:
        """Fresh ledger for a trading date: no trades yet, so realized P&L is
        known to be exactly zero."""
        return cls(trading_date=day, realized_pnl=Decimal("0"), account_capital=account_capital)

    def roll_to(self, now: datetime, calendar: MarketCalendar) -> DailyRiskState:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        today = now.astimezone(calendar.tz).date()
        if today < self.trading_date:
            raise ValueError(f"time went backwards: {today} < {self.trading_date}")
        if today == self.trading_date:
            return self  # never reset within the same exchange date
        return DailyRiskState.new_session(today)

    def record_realized(self, pnl: Decimal | float, settings: Settings) -> None:
        if self.realized_pnl is None:
            raise ValueError("realized P&L is unknown; cannot add to it")
        self.realized_pnl += D(pnl)
        if self.realized_pnl <= -D(settings.risk.max_daily_loss):
            self.locked = True
            self.lock_reason = (f"daily realized loss Rs {-self.realized_pnl} reached the "
                                f"limit Rs {settings.risk.max_daily_loss}")

    @property
    def open_risk(self) -> Decimal:
        return sum((p.planned_net_loss for p in self.open_positions), Decimal(0))

    @property
    def capital_in_use(self) -> Decimal:
        return sum((p.capital_used for p in self.open_positions), Decimal(0))


def available_capital(state: DailyRiskState, settings: Settings) -> Decimal:
    """Capital free for a new position: total minus reserve, open positions
    and any realized loss so far today (gains are not added back)."""
    base = state.account_capital if state.account_capital is not None \
        else D(settings.capital.initial_capital)
    capital = base - D(settings.risk.min_cash_reserve)
    capital -= state.capital_in_use
    if state.realized_pnl is not None and state.realized_pnl < 0:
        capital += state.realized_pnl
    return max(capital, Decimal(0))


def limit_violations(
    state: DailyRiskState,
    settings: Settings,
    signal_id: str,
    planned_net_loss: Decimal | None,
) -> tuple[list[Reason], list[Reason]]:
    """Returns (unknowns, violations). Unknowns mean the check could not run."""
    cfg = settings.risk
    unknown: list[Reason] = []
    bad: list[Reason] = []

    if state.realized_pnl is None:
        unknown.append(Reason(code="realized_pnl_unknown",
                              message="today's realized P&L is unknown; daily loss limit "
                                      "cannot be checked"))
    if state.locked:
        bad.append(Reason(code="daily_loss_lock", message=state.lock_reason or "daily lock"))
    if signal_id in state.rejected_signal_ids:
        bad.append(Reason(code="previously_rejected",
                          message="this signal was already rejected or invalidated"))
    elif signal_id in state.seen_signal_ids:
        bad.append(Reason(code="duplicate_candidate",
                          message="this signal was already evaluated/taken today"))
    if state.trades_taken >= cfg.max_trades_per_day:
        bad.append(Reason(code="max_trades_reached",
                          message=f"{state.trades_taken} of {cfg.max_trades_per_day} "
                                  "trades already taken today"))
    if len(state.open_positions) >= cfg.max_concurrent_positions:
        bad.append(Reason(code="max_positions_open",
                          message=f"{len(state.open_positions)} of "
                                  f"{cfg.max_concurrent_positions} positions already open"))

    if planned_net_loss is not None:
        if planned_net_loss > D(cfg.max_daily_loss):
            bad.append(Reason(code="trade_loss_exceeds_daily_limit",
                              message=f"planned net loss Rs {planned_net_loss} exceeds the "
                                      f"daily limit Rs {cfg.max_daily_loss}"))
        if state.realized_pnl is not None:
            worst = -state.realized_pnl + state.open_risk + planned_net_loss
            if worst > D(cfg.max_daily_loss):
                bad.append(Reason(code="would_breach_daily_loss",
                                  message=f"realized loss + open risk + this trade = Rs {worst} "
                                          f"> daily limit Rs {cfg.max_daily_loss}"))
        aggregate = state.open_risk + planned_net_loss
        if aggregate > D(cfg.max_aggregate_risk):
            bad.append(Reason(code="aggregate_risk",
                              message=f"open risk + this trade = Rs {aggregate} > "
                                      f"max aggregate Rs {cfg.max_aggregate_risk}"))
    return unknown, bad
