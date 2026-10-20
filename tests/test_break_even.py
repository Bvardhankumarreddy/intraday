from datetime import date
from decimal import Decimal

import pytest

from app.config import ExecutionCostConfig, Settings
from app.costs.break_even import BreakEvenError, break_even_exit_price, required_exit_price
from app.costs.calculator import round_trip
from app.costs.schedule import load_fee_schedule

EXEC = ExecutionCostConfig(assumed_spread_pct=0.10, slippage_pct_per_side=0.05)
PAISA = Decimal("0.01")


@pytest.fixture(scope="module")
def rates():
    return load_fee_schedule(Settings()).rates_on(date(2026, 10, 20))


def brute_force(rates, entry, qty, spread, profit=Decimal(0)):
    """Independent reference: scan upward one paisa at a time."""
    p = Decimal(str(entry))
    while round_trip(rates, entry, p, qty, EXEC, spread).net_pnl < profit:
        p += PAISA
    return p


@pytest.mark.parametrize(
    ("entry", "qty", "spread"),
    [(71.0, 4, 0.05), (71.0, 4, None), (55.55, 1, 0.08), (99.95, 5, 0.02), (60.0, 100, 0.05)],
)
def test_break_even_matches_brute_force(rates, entry, qty, spread):
    be = break_even_exit_price(rates, entry, qty, EXEC, spread)
    assert be == brute_force(rates, entry, qty, spread)
    # Tolerance: net >= 0 at the price, < 0 one paisa lower.
    assert round_trip(rates, entry, be, qty, EXEC, spread).net_pnl >= 0
    assert round_trip(rates, entry, be - PAISA, qty, EXEC, spread).net_pnl < 0


def test_break_even_worked_example(rates):
    # 4 @ 71.00: fixed brokerage+GST about 11.80 dominates, so the exit must
    # rise about 3 rupees per share (~4.2%) just to break even.
    be = break_even_exit_price(rates, 71.0, 4, EXEC, 0.05)
    assert Decimal("73.9") < be < Decimal("74.2")


def test_desired_profit_exit(rates):
    price = required_exit_price(rates, 71.0, 4, EXEC, 0.05, desired_net_profit=5)
    assert price == brute_force(rates, 71.0, 4, 0.05, Decimal(5))
    assert round_trip(rates, 71.0, price, 4, EXEC, 0.05).net_pnl >= 5
    assert price > break_even_exit_price(rates, 71.0, 4, EXEC, 0.05)


def test_larger_size_lowers_break_even_distance(rates):
    small = break_even_exit_price(rates, 60.0, 4, EXEC, 0.05) - Decimal(60)
    large = break_even_exit_price(rates, 60.0, 100, EXEC, 0.05) - Decimal(60)
    assert large < small


def test_infeasible_break_even(rates):
    with pytest.raises(BreakEvenError):
        required_exit_price(rates, 1.0, 1, EXEC, 0.05, desired_net_profit=1000)


@pytest.mark.parametrize(("entry", "qty"), [(0, 4), (-5, 4), (71, 0)])
def test_invalid_inputs(rates, entry, qty):
    with pytest.raises(BreakEvenError):
        break_even_exit_price(rates, entry, qty, EXEC)
