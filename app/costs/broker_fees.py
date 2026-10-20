"""Brokerage per executed order."""

from __future__ import annotations

from decimal import Decimal

from app.costs.money import pct_of, round_money
from app.costs.schedule import BrokerageRule


def brokerage(order_value: Decimal, rule: BrokerageRule) -> Decimal:
    """Brokerage for one executed order (buy and sell are separate orders).

    1. base = rate x value, capped at ``cap_per_order``
    2. minimum = lower of ``min_per_order`` and ``min_pct_of_value`` x value
       (whichever of the two are configured)
    3. brokerage = max(base, minimum), rounded per the rule

    For Groww intraday: 0.1% capped at Rs 20; if below Rs 5, the lower of Rs 5
    or 2.5% of value.
    """
    if order_value <= 0:
        raise ValueError("order value must be positive")
    base = pct_of(order_value, rule.rate_pct)
    if rule.cap_per_order is not None:
        base = min(base, rule.cap_per_order)

    floors = []
    if rule.min_per_order is not None:
        floors.append(rule.min_per_order)
    if rule.min_pct_of_value is not None:
        floors.append(pct_of(order_value, rule.min_pct_of_value))
    minimum = min(floors) if floors else Decimal(0)
    return round_money(max(base, minimum), rule.rounding)
