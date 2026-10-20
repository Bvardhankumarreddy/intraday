"""Decimal helpers. Money is Decimal; rounding happens only where stated."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.costs.schedule import Rounding

PAISA = Decimal("0.01")
RUPEE = Decimal("1")
HUNDRED = Decimal("100")


def D(value: float | int | str | Decimal) -> Decimal:
    """Exact Decimal of a value's shortest decimal representation.

    ``D(70.1)`` is ``Decimal('70.1')``, not the binary float's expansion.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(value)


def round_money(value: Decimal, rule: Rounding = Rounding.PAISA) -> Decimal:
    quantum = RUPEE if rule is Rounding.RUPEE else PAISA
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def pct_of(amount: Decimal, rate_pct: Decimal) -> Decimal:
    return amount * rate_pct / HUNDRED
