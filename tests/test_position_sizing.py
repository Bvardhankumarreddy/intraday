from datetime import date
from decimal import Decimal

import pytest

from app.config import Settings
from app.costs.calculator import round_trip
from app.costs.schedule import load_fee_schedule
from app.risk.position_sizing import InvalidSetupError, size_position


@pytest.fixture(scope="module")
def rates():
    return load_fee_schedule(Settings()).rates_on(date(2026, 10, 20))


def settings(**risk):
    risk.setdefault("include_costs_in_risk", False)
    return Settings(risk=risk)


def test_risk_based_sizing_formula(rates):
    # budget 500 x 0.5% = 2.50; risk/share 0.60 -> floor(4.17) = 4; capital floor(500/71) = 7
    r = size_position(71.0, 70.4, Decimal(500), settings(), rates, 0.05)
    assert r.risk_budget == Decimal("2.50")
    assert r.risk_per_share == Decimal("0.6")
    assert (r.risk_based_quantity, r.capital_based_quantity, r.final_quantity) == (4, 7, 4)
    assert r.binding_constraint == "risk_budget"


def test_capital_constraint_binds(rates):
    # budget 100 x 10% = 10 -> 16 shares by risk, but only floor(100/71) = 1 by capital
    r = size_position(71.0, 70.4, Decimal(100), settings(risk_pct=10), rates, 0.05)
    assert (r.risk_based_quantity, r.capital_based_quantity, r.final_quantity) == (16, 1, 1)
    assert r.binding_constraint == "capital"


def test_buy_costs_must_fit_in_cash(rates):
    # 1 share @ 71 costs 71 + ~2.2 buy-side charges > 72 available
    r = size_position(71.0, 70.4, Decimal(72), settings(risk_pct=10), rates, 0.05)
    assert r.capital_based_quantity == 1 and r.final_quantity == 0
    assert r.binding_constraint == "cash_including_buy_costs"


def test_price_above_capital_gives_zero(rates):
    r = size_position(600.0, 590.0, Decimal(500), settings(risk_pct=10), rates, 0.05)
    assert r.capital_based_quantity == 0 and r.final_quantity == 0
    assert r.binding_constraint == "capital" and r.notes


def test_risk_per_share_above_budget_gives_zero(rates):
    r = size_position(71.0, 68.0, Decimal(500), settings(), rates, 0.05)  # 3.00 > 2.50
    assert r.risk_based_quantity == 0 and r.final_quantity == 0
    assert r.binding_constraint == "risk_budget"


def test_max_position_value(rates):
    r = size_position(71.0, 70.4, Decimal(500), settings(risk_pct=10, max_position_value=150),
                      rates, 0.05)
    assert r.capital_based_quantity == 2 and r.final_quantity == 2


def test_costs_in_risk_reject_tiny_account(rates):
    s = Settings()  # include_costs_in_risk defaults to True
    r = size_position(71.0, 70.4, Decimal(500), s, rates, 0.05)
    assert r.risk_based_quantity == 4 and r.final_quantity == 0
    assert r.binding_constraint == "net_loss_including_costs"
    # one share: brokerage 1.78 each side (2.5% of 71) dominates the loss
    assert r.one_share_net_loss_at_stop > Decimal("2.50")


def test_costs_in_risk_largest_valid_quantity(rates):
    s = Settings(capital={"initial_capital": 100_000})
    r = size_position(71.0, 70.4, Decimal(100_000), s, rates, 0.05)
    budget = Decimal(500)
    q = r.final_quantity
    assert 0 < q < r.risk_based_quantity  # costs reduce the size
    loss = lambda n: -round_trip(rates, 71.0, 70.4, n, s.execution, 0.05).net_pnl  # noqa: E731
    assert loss(q) <= budget < loss(q + 1)
    assert r.binding_constraint == "net_loss_including_costs"


@pytest.mark.parametrize(("entry", "stop"), [(71, 71), (71, 72), (0, -1), (71, 0), (-5, -6)])
def test_invalid_stop_or_entry(rates, entry, stop):
    with pytest.raises(InvalidSetupError):
        size_position(entry, stop, Decimal(500), settings(), rates, 0.05)


def test_margin_is_rejected():
    with pytest.raises(ValueError, match="margin"):
        Settings(risk={"allow_margin": True})
