"""Per-order charges: brokerage, statutory/exchange charges and GST.

Each charge applies only on its configured side, to that order's turnover,
and is rounded individually per order. GST is levied on the sum of the
already-rounded GST-applicable components (brokerage plus every charge
marked ``gst_applicable``), never on the trade value, STT or stamp duty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.costs.broker_fees import brokerage
from app.costs.money import pct_of, round_money
from app.costs.schedule import ChargeKind, ResolvedRates, Side


@dataclass(frozen=True)
class OrderCharges:
    side: Side  # BUY or SELL
    turnover: Decimal
    brokerage: Decimal
    statutory: dict[ChargeKind, Decimal]  # every configured kind; 0 where side doesn't apply
    gst_base: Decimal
    gst: Decimal
    gst_components: tuple[str, ...] = field(default=())

    @property
    def total(self) -> Decimal:
        return self.brokerage + sum(self.statutory.values(), Decimal(0)) + self.gst

    def get(self, kind: ChargeKind) -> Decimal:
        return self.statutory.get(kind, Decimal(0))


def order_charges(rates: ResolvedRates, side: Side, price: Decimal, quantity: int) -> OrderCharges:
    if side is Side.BOTH:
        raise ValueError("an order is either BUY or SELL")
    if price <= 0:
        raise ValueError("price must be positive")
    if quantity <= 0:
        raise ValueError("quantity must be a positive whole number")
    turnover = price * quantity

    brk = brokerage(turnover, rates.brokerage)
    statutory: dict[ChargeKind, Decimal] = {}
    gst_base = brk
    gst_components = ["brokerage"]
    for charge in rates.charges:
        applies = charge.side is Side.BOTH or charge.side is side
        amount = round_money(pct_of(turnover, charge.rate_pct), charge.rounding) if applies \
            else Decimal("0.00")
        statutory[charge.kind] = statutory.get(charge.kind, Decimal(0)) + amount
        if applies and charge.gst_applicable:
            gst_base += amount
            gst_components.append(charge.kind.value)
    gst = round_money(pct_of(gst_base, rates.gst.rate_pct))
    return OrderCharges(side, turnover, brk, statutory, gst_base, gst, tuple(gst_components))
