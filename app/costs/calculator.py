"""Round-trip costs and P&L for a long intraday trade.

Price model (no double counting):
- Fees are computed on *reference* turnover: entry_price x qty and
  exit_price x qty.
- Execution costs are separate lines: half the bid-ask spread on each fill
  (buy at ask, sell at bid) plus configured slippage on each fill. The
  reference prices are NOT also adjusted for spread, so spread is counted once.
- Fees are therefore computed on reference prices rather than the
  (slightly different) execution prices. The difference is a fraction of a
  paisa on these trade sizes.

gross_pnl = (exit - entry) x qty
total_costs = buy fees + sell fees + spread + slippage
net_pnl = gross_pnl - total_costs
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.config import ExecutionCostConfig
from app.costs.money import HUNDRED, D, pct_of, round_money
from app.costs.schedule import ChargeKind, ResolvedRates, Side
from app.costs.statutory_charges import OrderCharges, order_charges


@dataclass(frozen=True)
class ExecutionCosts:
    spread_pct_used: Decimal  # full spread, % of price
    spread_source: str  # "quote" or "assumed"
    spread_buy: Decimal
    spread_sell: Decimal
    slippage_pct_per_side: Decimal
    slippage_buy: Decimal
    slippage_sell: Decimal

    @property
    def spread_total(self) -> Decimal:
        return self.spread_buy + self.spread_sell

    @property
    def slippage_total(self) -> Decimal:
        return self.slippage_buy + self.slippage_sell

    @property
    def total(self) -> Decimal:
        return self.spread_total + self.slippage_total

    @property
    def is_estimate(self) -> bool:
        return True  # slippage is always an assumption; spread may be too


def execution_costs(
    entry: Decimal, exit_: Decimal, quantity: int, spread_pct: float | None,
    cfg: ExecutionCostConfig,
) -> ExecutionCosts:
    if spread_pct is None:
        spread, source = D(cfg.assumed_spread_pct), "assumed"
    else:
        if spread_pct < 0:
            raise ValueError("spread cannot be negative")
        spread, source = D(spread_pct), "quote"
    half = spread / 2
    slip = D(cfg.slippage_pct_per_side)
    return ExecutionCosts(
        spread_pct_used=spread,
        spread_source=source,
        spread_buy=round_money(pct_of(entry * quantity, half)),
        spread_sell=round_money(pct_of(exit_ * quantity, half)),
        slippage_pct_per_side=slip,
        slippage_buy=round_money(pct_of(entry * quantity, slip)),
        slippage_sell=round_money(pct_of(exit_ * quantity, slip)),
    )


@dataclass(frozen=True)
class RoundTrip:
    entry: Decimal
    exit: Decimal
    quantity: int
    buy: OrderCharges
    sell: OrderCharges
    execution: ExecutionCosts

    @property
    def buy_turnover(self) -> Decimal:
        return self.buy.turnover

    @property
    def sell_turnover(self) -> Decimal:
        return self.sell.turnover

    @property
    def gross_pnl(self) -> Decimal:
        return (self.exit - self.entry) * self.quantity

    @property
    def broker_charges(self) -> Decimal:
        return self.buy.brokerage + self.sell.brokerage

    @property
    def official_charges(self) -> Decimal:
        """STT, stamp duty, exchange, IPFT, SEBI and other statutory charges."""
        return sum(self.buy.statutory.values(), Decimal(0)) + \
            sum(self.sell.statutory.values(), Decimal(0))

    @property
    def gst(self) -> Decimal:
        return self.buy.gst + self.sell.gst

    @property
    def fees_total(self) -> Decimal:
        return self.buy.total + self.sell.total

    @property
    def total_costs(self) -> Decimal:
        return self.fees_total + self.execution.total

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.total_costs

    @property
    def net_return_pct(self) -> Decimal:
        return self.net_pnl / self.buy_turnover * HUNDRED

    def charge(self, kind: ChargeKind) -> Decimal:
        return self.buy.get(kind) + self.sell.get(kind)


def round_trip(
    rates: ResolvedRates,
    entry: Decimal | float,
    exit_: Decimal | float,
    quantity: int,
    exec_cfg: ExecutionCostConfig,
    spread_pct: float | None = None,
) -> RoundTrip:
    entry, exit_ = D(entry), D(exit_)
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        raise ValueError("quantity must be a positive whole number of shares")
    if entry <= 0 or exit_ <= 0:
        raise ValueError("prices must be positive")
    return RoundTrip(
        entry=entry,
        exit=exit_,
        quantity=quantity,
        buy=order_charges(rates, Side.BUY, entry, quantity),
        sell=order_charges(rates, Side.SELL, exit_, quantity),
        execution=execution_costs(entry, exit_, quantity, spread_pct, exec_cfg),
    )
