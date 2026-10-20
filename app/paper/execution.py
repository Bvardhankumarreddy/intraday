"""Paper execution model ``paper-v1``.

Entries (a market BUY created when a signal is accepted at a candle close):
1. Quote fill: if a valid quote with an ask exists, timestamped at or after
   the signal time and no older than ``data.max_staleness_seconds``, the order
   fills at the ASK. The spread is already paid in that price, so the spread
   cost line for this fill is zero (no double counting); slippage still applies.
2. Candle fill: otherwise the order waits for the next completed candle
   starting at the signal time and fills at its OPEN (Phase 4 ``candle-v1``),
   with half the assumed spread charged as a cost line.
Both reject the fill if the price is at/below the stop or more than
``backtest.max_entry_gap_pct`` above the entry reference. An order still
unfilled after the signal's ``valid_until`` EXPIRES.

Exits use only completed 5-minute candles and the Phase 4 ``candle-v1``
rules (stop, gap through the stop at the open, target, stop-first on
ambiguous candles, time exit at the open of the first candle at/after
``eod_exit_time`` or the max holding time). Quotes never trigger exits. The
candle containing the entry is included, so a stop touched within it counts
(conservative).

Every fill records its price source and timestamp in ``fill_basis``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.backtesting.execution import EXECUTION_MODEL_VERSION as CANDLE_MODEL
from app.config import Settings
from app.costs.money import D, pct_of, round_money
from app.costs.schedule import ChargeKind, ResolvedRates, Side
from app.costs.statutory_charges import OrderCharges, order_charges
from app.data.models import Quote
from app.data.validation import validate_quote

PAPER_EXECUTION_MODEL_VERSION = f"paper-v1(entry: quote-ask | {CANDLE_MODEL}; exit: {CANDLE_MODEL})"


@dataclass(frozen=True)
class FillCosts:
    fees: OrderCharges
    spread: Decimal
    slippage: Decimal

    @property
    def total(self) -> Decimal:
        return self.fees.total + self.spread + self.slippage

    def breakdown(self) -> dict[str, str]:
        f = self.fees
        return {
            "brokerage": str(f.brokerage),
            "stt": str(f.get(ChargeKind.STT)),
            "stamp_duty": str(f.get(ChargeKind.STAMP_DUTY)),
            "exchange_transaction": str(f.get(ChargeKind.EXCHANGE_TRANSACTION)),
            "ipft": str(f.get(ChargeKind.IPFT)),
            "sebi_turnover_fee": str(f.get(ChargeKind.SEBI_TURNOVER_FEE)),
            "gst": str(f.gst),
            "spread": str(self.spread),
            "slippage": str(self.slippage),
            "total": str(self.total),
        }


def fill_costs(rates: ResolvedRates, side: Side, price: Decimal, quantity: int,
               settings: Settings, spread_in_price: bool) -> FillCosts:
    """Fees (Phase 3) plus execution costs for one fill."""
    turnover = price * quantity
    half_spread = Decimal(0) if spread_in_price else D(settings.execution.assumed_spread_pct) / 2
    return FillCosts(
        fees=order_charges(rates, side, price, quantity),
        spread=round_money(pct_of(turnover, half_spread)),
        slippage=round_money(pct_of(turnover, D(settings.execution.slippage_pct_per_side))),
    )


def entry_price_ok(price: Decimal, entry_reference: Decimal, stop: Decimal,
                   max_gap_pct: float) -> str | None:
    """Reason the price is unacceptable for entry, or None."""
    if price <= stop:
        return f"price {price} at/below stop {stop}"
    limit = entry_reference * (1 + D(max_gap_pct) / 100)
    if price > limit:
        return f"price {price} more than {max_gap_pct}% above entry reference {entry_reference}"
    return None


def usable_quote(quote: Quote | None, signal_time: datetime, now: datetime,
                 max_age_seconds: int) -> tuple[bool, str]:
    if quote is None:
        return False, "no quote"
    if quote.ask is None or quote.ask <= 0:
        return False, "quote has no ask"
    if quote.timestamp < signal_time:
        return False, "quote older than the signal"
    if quote.timestamp > now + timedelta(seconds=5):
        return False, "quote timestamp is in the future"
    age = (now - quote.timestamp).total_seconds()
    if age > max_age_seconds:  # enforced for every data mode, not only live
        return False, f"quote is {age:.0f}s old (limit {max_age_seconds}s)"
    report = validate_quote(quote, now, max_age_seconds)
    if not report.is_valid:
        return False, "; ".join(i.message for i in report.errors)
    return True, ""
