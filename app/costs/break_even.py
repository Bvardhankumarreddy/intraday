"""Break-even and required exit prices.

Costs are piecewise in the exit price (brokerage minimum/cap kinks, per-order
rounding), so no single closed form is used. Instead:

- The search runs over exit prices in whole paise, from the entry price up to
  ``max_multiple`` x entry.
- f(p) = net_pnl(exit = p) - desired_profit. f is non-decreasing whenever
  every charge on the sell side rises by less than ``quantity`` rupees per
  rupee of price (true for all rates here: total sell-side rates are < 1%).
  Per-order paisa rounding can only flatten f locally, never reverse it by
  more than a paisa, so after bisection we step down while f(p - 0.01) >= 0
  to guarantee the *smallest* qualifying price.
- Result: the smallest price p (in paise) with f(p) >= 0. Tolerance: by
  construction f(p) >= 0 and f(p - 0.01) < 0, so the price is exact to one
  paisa and the net P&L at it is within one paisa x quantity (plus rounding)
  of the desired profit.
- If no price up to the bound qualifies, ``BreakEvenError`` is raised.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from app.config import ExecutionCostConfig
from app.costs.calculator import round_trip
from app.costs.money import PAISA, D
from app.costs.schedule import ResolvedRates


class BreakEvenError(ValueError):
    pass


def required_exit_price(
    rates: ResolvedRates,
    entry: Decimal | float,
    quantity: int,
    exec_cfg: ExecutionCostConfig,
    spread_pct: float | None = None,
    desired_net_profit: Decimal | float = 0,
    max_multiple: Decimal = Decimal(3),
) -> Decimal:
    """Smallest exit price (to the paisa) giving net P&L >= desired profit."""
    entry, target = D(entry), D(desired_net_profit)
    if entry <= 0:
        raise BreakEvenError("entry price must be positive")
    if quantity <= 0:
        raise BreakEvenError("quantity must be positive")

    def f(paise: int) -> Decimal:
        exit_price = Decimal(paise) * PAISA
        return round_trip(rates, entry, exit_price, quantity, exec_cfg, spread_pct).net_pnl - target

    lo = int((entry / PAISA).to_integral_value(rounding=ROUND_CEILING))
    hi = int((entry * max_multiple / PAISA).to_integral_value(rounding=ROUND_CEILING))
    if f(hi) < 0:
        raise BreakEvenError(
            f"no exit price up to {Decimal(hi) * PAISA} reaches net P&L {target}"
        )
    if f(lo) >= 0:
        return Decimal(lo) * PAISA

    # Invariant: f(lo) < 0 <= f(hi)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if f(mid) >= 0:
            hi = mid
        else:
            lo = mid
    for _ in range(1000):  # guard against local non-monotonicity from rounding
        if hi - 1 > 0 and f(hi - 1) >= 0:
            hi -= 1
        else:
            break
    return Decimal(hi) * PAISA


def break_even_exit_price(
    rates: ResolvedRates,
    entry: Decimal | float,
    quantity: int,
    exec_cfg: ExecutionCostConfig,
    spread_pct: float | None = None,
) -> Decimal:
    return required_exit_price(rates, entry, quantity, exec_cfg, spread_pct, 0)
