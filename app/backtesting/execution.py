"""Candle-only execution model (version ``candle-v1``).

Historical data here has no quotes or order book, so fills are modelled from
5-minute OHLC with conservative rules. This does NOT reproduce real fills.

Entry (market order placed when the signal candle closes):
- Fills at the OPEN of the next candle for that symbol, never at the signal
  candle's close.
- Skipped if that open is at or below the stop (the setup is already broken)
  or more than ``max_entry_gap_pct`` above the entry reference.

Exit, checked on every candle from the entry candle onward:
1. Time exit: on the first candle starting at or after ``eod_exit_time`` (or
   the max holding time), exit at that candle's OPEN.
2. Gap through stop: a later candle OPENING at/below the stop exits at that
   open (worse than the stop), not at the stop.
3. Stop and target both inside one candle: the intrabar order is unknown, so
   the STOP is assumed first and the trade is flagged ambiguous. The more
   profitable outcome is never chosen.
4. Stop only: exit at the stop. Target only: exit at the target (no price
   improvement assumed, even on a gap up).
   Optional, for sensitivity analysis (``backtest.target_trade_through_pct``,
   default 0 = plain ``candle-v1``): the target counts as reached only when
   the high trades that % beyond it, because a touch does not guarantee a
   fill. The exit price is still the target. The model label then becomes
   ``candle-v1+tt<pct>``.
5. If the session's data ends with the position open: exit at the last
   close, flagged incomplete.

Spread and slippage are applied as the Phase 3 cost lines on these reference
fill prices, so they are counted once. Every fill is checked to lie inside
its candle's [low, high].
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.costs.money import D

EXECUTION_MODEL_VERSION = "candle-v1"


def execution_model_label(target_trade_through_pct: float = 0.0) -> str:
    """Model version string, including any non-default fill option."""
    if not target_trade_through_pct:
        return EXECUTION_MODEL_VERSION
    return f"{EXECUTION_MODEL_VERSION}+tt{target_trade_through_pct:g}"


def target_trigger(target: Decimal, trade_through_pct: float) -> Decimal:
    """Price the high must reach for a target fill (the target itself unless a
    trade-through is required)."""
    if not trade_through_pct:
        return target
    return target * (1 + D(trade_through_pct) / 100)


class ExitReason(StrEnum):
    STOP = "STOP"
    STOP_GAP = "STOP_GAP"
    TARGET = "TARGET"
    AMBIGUOUS_STOP_FIRST = "AMBIGUOUS_STOP_FIRST"
    END_OF_DAY = "END_OF_DAY"
    MAX_HOLDING = "MAX_HOLDING"
    DATA_END = "DATA_END"


class ImpossibleFillError(AssertionError):
    pass


@dataclass(frozen=True)
class Candle:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    @classmethod
    def from_row(cls, row) -> Candle:
        return cls(D(float(row["open"])), D(float(row["high"])), D(float(row["low"])),
                   D(float(row["close"])))

    def check(self, price: Decimal) -> Decimal:
        if price <= 0 or not self.low <= price <= self.high:
            raise ImpossibleFillError(f"fill {price} outside candle [{self.low}, {self.high}]")
        return price


def entry_fill(candle: Candle, entry_reference: Decimal, stop: Decimal,
               max_gap_pct: float) -> tuple[Decimal | None, str]:
    if candle.open <= stop:
        return None, f"next open {candle.open} at/below stop {stop}"
    limit = entry_reference * (1 + D(max_gap_pct) / 100)
    if candle.open > limit:
        return None, (f"next open {candle.open} more than {max_gap_pct}% above entry "
                      f"reference {entry_reference}")
    return candle.check(candle.open), "next candle open"


def exit_fill(candle: Candle, stop: Decimal, target: Decimal, is_entry_candle: bool,
              time_exit: ExitReason | None,
              trigger: Decimal | None = None) -> tuple[Decimal, ExitReason, bool] | None:
    """Returns (price, reason, ambiguous) or None if the position stays open.
    ``trigger`` is the price the high must reach for a target fill (default:
    the target itself); the fill is always at the target."""
    if time_exit is not None:
        return candle.check(candle.open), time_exit, False
    if not is_entry_candle and candle.open <= stop:
        return candle.check(candle.open), ExitReason.STOP_GAP, False
    hit_stop = candle.low <= stop
    hit_target = candle.high >= (target if trigger is None else trigger)
    if hit_stop and hit_target:
        return candle.check(stop), ExitReason.AMBIGUOUS_STOP_FIRST, True
    if hit_stop:
        return candle.check(stop), ExitReason.STOP, False
    if hit_target:
        return candle.check(target), ExitReason.TARGET, False
    return None
