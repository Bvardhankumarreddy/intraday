"""Risk-based position sizing for long cash-equity trades.

    risk_budget            = available_capital x risk_pct / 100
    risk_per_share         = entry - stop
    risk_based_quantity    = floor(risk_budget / risk_per_share)
    capital_based_quantity = floor(available_capital / entry)
                             (and floor(max_position_value / entry) if set)

The final quantity is the largest whole number not above either limit that
also satisfies:

- cash: entry turnover + buy-side fees + buy-side execution costs fit in the
  available capital (fees are paid from the same account);
- if ``include_costs_in_risk``: the planned *net* loss at the stop, including
  every round-trip cost, fits in the risk budget.

Both conditions get stricter as quantity grows, so the largest valid
quantity is found by bisection. Quantity never increases because of a
strategy score, and no margin or leverage is assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

from app.config import Settings
from app.costs.calculator import round_trip
from app.costs.money import HUNDRED, D, round_money
from app.costs.schedule import ResolvedRates


class InvalidSetupError(ValueError):
    pass


def _floor_int(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_FLOOR)) if x > 0 else 0


@dataclass
class SizingResult:
    available_capital: Decimal
    risk_budget: Decimal
    risk_per_share: Decimal
    risk_based_quantity: int
    capital_based_quantity: int
    cost_adjusted_quantity: int
    final_quantity: int
    binding_constraint: str | None
    notes: list[str] = field(default_factory=list)
    # Net loss at the stop for one share, to explain zero-quantity outcomes.
    one_share_net_loss_at_stop: Decimal | None = None


def size_position(
    entry: float | Decimal,
    stop: float | Decimal,
    available_capital: Decimal,
    settings: Settings,
    rates: ResolvedRates,
    spread_pct: float | None,
) -> SizingResult:
    entry, stop = D(entry), D(stop)
    if entry <= 0:
        raise InvalidSetupError("entry price must be positive")
    if stop <= 0:
        raise InvalidSetupError("stop price must be positive")
    if stop >= entry:
        raise InvalidSetupError(f"stop {stop} must be below entry {entry} for a long trade")
    if available_capital < 0:
        raise InvalidSetupError("available capital cannot be negative")

    risk_cfg, exec_cfg = settings.risk, settings.execution
    risk_budget = round_money(available_capital * D(risk_cfg.risk_pct) / HUNDRED)
    risk_per_share = entry - stop
    risk_qty = _floor_int(risk_budget / risk_per_share)
    capital_qty = _floor_int(available_capital / entry)
    if risk_cfg.max_position_value is not None:
        capital_qty = min(capital_qty, _floor_int(D(risk_cfg.max_position_value) / entry))

    def cash_ok(q: int) -> bool:
        rt = round_trip(rates, entry, stop, q, exec_cfg, spread_pct)
        need = rt.buy_turnover + rt.buy.total + rt.execution.spread_buy + rt.execution.slippage_buy
        return need <= available_capital

    def net_risk_ok(q: int) -> bool:
        if not risk_cfg.include_costs_in_risk:
            return True
        net_loss = -round_trip(rates, entry, stop, q, exec_cfg, spread_pct).net_pnl
        return net_loss <= risk_budget

    upper = min(risk_qty, capital_qty)
    one_share_loss = -round_trip(rates, entry, stop, 1, exec_cfg, spread_pct).net_pnl

    def ok(q: int) -> bool:
        return cash_ok(q) and net_risk_ok(q)

    final = 0
    if upper >= 1 and ok(1):
        lo, hi = 1, upper  # ok(lo) is True
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if ok(mid):
                lo = mid
            else:
                hi = mid - 1
        final = lo

    if final == upper and upper > 0:
        binding = "risk_budget" if risk_qty <= capital_qty else "capital"
    elif upper == 0:
        binding = "risk_budget" if risk_qty == 0 else "capital"
    elif not cash_ok(final + 1):
        binding = "cash_including_buy_costs"
    else:
        binding = "net_loss_including_costs"

    notes = []
    if final == 0:
        notes.append(
            f"no whole-share quantity fits: risk budget Rs {risk_budget}, risk/share "
            f"Rs {risk_per_share}, capital allows {capital_qty}, one share would lose "
            f"Rs {one_share_loss} net at the stop"
        )
    return SizingResult(
        available_capital=available_capital,
        risk_budget=risk_budget,
        risk_per_share=risk_per_share,
        risk_based_quantity=risk_qty,
        capital_based_quantity=capital_qty,
        cost_adjusted_quantity=final,
        final_quantity=final,
        binding_constraint=binding,
        notes=notes,
        one_share_net_loss_at_stop=one_share_loss,
    )
