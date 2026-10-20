"""Transaction costs, checked against hand calculations.

Worked example (Groww, NSE intraday, rates verified 2026-10-19):
buy 4 @ 71.00 (turnover 284.00), sell 4 @ 72.20 (turnover 288.80)

BUY  brokerage  max(min(0.284, 20), min(5, 7.10)) = 5.00
     stamp      284 x 0.003%      = 0.00852     -> 0.01
     exchange   284 x 0.0030699%  = 0.0087185   -> 0.01
     IPFT       284 x 0.0000001%  = 0.0000003   -> 0.00
     SEBI       284 x 0.0001%     = 0.000284    -> 0.00
     GST        18% x (5.00 + 0.01 + 0.00 + 0.00) = 0.9018 -> 0.90
     total      5.92
SELL brokerage  max(min(0.2888, 20), min(5, 7.22)) = 5.00
     STT        288.80 x 0.025%   = 0.0722      -> 0.07
     exchange   288.80 x 0.0030699% = 0.0088659 -> 0.01
     GST        18% x 5.01 = 0.90
     total      5.98
Fees 11.90. With a 0.05% quoted spread and 0.05% slippage per side:
spread 0.07 + 0.07, slippage 0.14 + 0.14 -> 0.42. Total costs 12.32.
Gross 4.80, net -7.52.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.config import ExecutionCostConfig, Settings
from app.costs.broker_fees import brokerage
from app.costs.calculator import round_trip
from app.costs.schedule import (
    BrokerageRule,
    ChargeKind,
    FeeSchedule,
    FeeScheduleError,
    Rounding,
    Side,
    load_fee_schedule,
)
from app.costs.statutory_charges import order_charges

TRADE_DAY = date(2026, 10, 20)
EXEC = ExecutionCostConfig(assumed_spread_pct=0.10, slippage_pct_per_side=0.05)


@pytest.fixture(scope="module")
def schedule() -> FeeSchedule:
    return load_fee_schedule(Settings())


@pytest.fixture(scope="module")
def rates(schedule):
    return schedule.rates_on(TRADE_DAY)


def rule(**kw) -> BrokerageRule:
    base = dict(rate_pct="0.1", cap_per_order="20", min_per_order="5", min_pct_of_value="2.5",
                effective_from=date(2020, 1, 1),
                source={"name": "t", "url": "t", "retrieved_on": date(2026, 1, 1)})
    base.update(kw)
    return BrokerageRule(**base)


# -- brokerage -------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("284.00", "5.00"),     # 0.1% = 0.284 -> minimum Rs 5 (2.5% = 7.10)
        ("100.00", "2.50"),     # minimum is lower of Rs 5 and 2.5% = 2.50
        ("150.00", "3.75"),     # 2.5% of 150
        ("200.00", "5.00"),     # 2.5% of 200 = 5 = Rs 5
        ("3000.00", "5.00"),    # 0.1% = 3 < 5 -> 5
        ("10000.00", "10.00"),  # 0.1% between minimum and cap
        ("50000.00", "20.00"),  # 0.1% = 50 -> capped at 20
    ],
)
def test_groww_brokerage(value, expected):
    assert brokerage(Decimal(value), rule()) == Decimal(expected)


def test_brokerage_without_minimum_or_cap():
    r = rule(cap_per_order=None, min_per_order=None, min_pct_of_value=None)
    assert brokerage(Decimal("50000"), r) == Decimal("50.00")
    assert brokerage(Decimal("284"), r) == Decimal("0.28")


def test_brokerage_flat_minimum_only():
    r = rule(min_pct_of_value=None)
    assert brokerage(Decimal("100"), r) == Decimal("5.00")


def test_brokerage_rupee_rounding():
    r = rule(rounding="rupee")
    assert brokerage(Decimal("150"), r) == Decimal("4")  # 3.75 -> 4


def test_brokerage_rejects_zero_turnover():
    with pytest.raises(ValueError):
        brokerage(Decimal("0"), rule())


# -- per-order charges -------------------------------------------------------


def test_buy_side_breakdown(rates):
    buy = order_charges(rates, Side.BUY, Decimal("71.00"), 4)
    assert buy.turnover == Decimal("284.00")
    assert buy.brokerage == Decimal("5.00")
    assert buy.get(ChargeKind.STAMP_DUTY) == Decimal("0.01")
    assert buy.get(ChargeKind.STT) == Decimal("0.00")  # STT is sell-side only
    assert buy.get(ChargeKind.EXCHANGE_TRANSACTION) == Decimal("0.01")
    assert buy.get(ChargeKind.IPFT) == Decimal("0.00")
    assert buy.get(ChargeKind.SEBI_TURNOVER_FEE) == Decimal("0.00")
    assert buy.gst_base == Decimal("5.01")
    assert buy.gst == Decimal("0.90")
    assert buy.total == Decimal("5.92")


def test_sell_side_breakdown(rates):
    sell = order_charges(rates, Side.SELL, Decimal("72.20"), 4)
    assert sell.brokerage == Decimal("5.00")
    assert sell.get(ChargeKind.STT) == Decimal("0.07")
    assert sell.get(ChargeKind.STAMP_DUTY) == Decimal("0.00")  # stamp duty is buy-side only
    assert sell.get(ChargeKind.EXCHANGE_TRANSACTION) == Decimal("0.01")
    assert sell.gst == Decimal("0.90")
    assert sell.total == Decimal("5.98")


def test_statutory_rates_on_large_order(rates):
    # 1,000 shares @ 100 = Rs 1,00,000 turnover; makes each rate visible.
    sell = order_charges(rates, Side.SELL, Decimal("100"), 1000)
    buy = order_charges(rates, Side.BUY, Decimal("100"), 1000)
    assert sell.get(ChargeKind.STT) == Decimal("25.00")                 # 0.025%
    assert buy.get(ChargeKind.STAMP_DUTY) == Decimal("3.00")            # 0.003%
    assert buy.get(ChargeKind.EXCHANGE_TRANSACTION) == Decimal("3.07")  # 3.0699 -> 3.07
    assert buy.get(ChargeKind.IPFT) == Decimal("0.00")                  # 0.0001
    assert buy.get(ChargeKind.SEBI_TURNOVER_FEE) == Decimal("0.10")     # 0.0001%
    assert buy.brokerage == Decimal("20.00")                            # capped
    # GST on brokerage + exchange + IPFT + SEBI only (not stamp duty)
    assert buy.gst_base == Decimal("23.17")
    assert buy.gst == Decimal("4.17")                                   # 4.1706
    assert set(buy.gst_components) == {"brokerage", "exchange_transaction", "ipft",
                                       "sebi_turnover_fee"}


def test_gst_never_on_trade_value_stt_or_stamp(rates):
    sell = order_charges(rates, Side.SELL, Decimal("100"), 1000)
    taxable = sell.brokerage + sell.get(ChargeKind.EXCHANGE_TRANSACTION) + \
        sell.get(ChargeKind.IPFT) + sell.get(ChargeKind.SEBI_TURNOVER_FEE)
    assert sell.gst_base == taxable
    assert "stt" not in sell.gst_components


# -- round trip --------------------------------------------------------------


def test_round_trip_worked_example(rates):
    rt = round_trip(rates, 71.0, 72.2, 4, EXEC, spread_pct=0.05)
    assert rt.gross_pnl == Decimal("4.80")
    assert rt.fees_total == Decimal("11.90")
    assert rt.broker_charges == Decimal("10.00")
    assert rt.gst == Decimal("1.80")
    assert rt.official_charges == Decimal("0.10")  # 0.01+0.01 buy, 0.07+0.01 sell
    assert rt.execution.spread_source == "quote"
    assert (rt.execution.spread_buy, rt.execution.spread_sell) == (Decimal("0.07"), Decimal("0.07"))
    assert (rt.execution.slippage_buy, rt.execution.slippage_sell) == (Decimal("0.14"), Decimal("0.14"))
    assert rt.total_costs == Decimal("12.32")
    assert rt.net_pnl == Decimal("-7.52")
    assert rt.net_return_pct == Decimal("-7.52") / Decimal("284") * 100


def test_no_duplicate_charging(rates):
    rt = round_trip(rates, 71.0, 72.2, 4, EXEC, spread_pct=0.05)
    parts = (rt.broker_charges + rt.official_charges + rt.gst
             + rt.execution.spread_total + rt.execution.slippage_total)
    assert parts == rt.total_costs
    assert rt.charge(ChargeKind.STT) == rt.sell.get(ChargeKind.STT)  # not on buy too
    assert rt.charge(ChargeKind.STAMP_DUTY) == rt.buy.get(ChargeKind.STAMP_DUTY)


def test_assumed_spread_is_labelled(rates):
    rt = round_trip(rates, 71.0, 72.2, 4, EXEC, spread_pct=None)
    assert rt.execution.spread_source == "assumed"
    assert rt.execution.spread_pct_used == Decimal("0.1")
    assert rt.execution.spread_buy == Decimal("0.14")  # 284 x 0.05%


def test_costs_exceed_expected_gross_profit(rates):
    rt = round_trip(rates, 71.0, 72.2, 4, EXEC, spread_pct=0.05)
    assert rt.gross_pnl > 0 and rt.net_pnl < 0


@pytest.mark.parametrize(
    ("entry", "exit_", "qty"),
    [(0, 72, 4), (-1, 72, 4), (71, 0, 4), (71, 72, 0), (71, 72, -3), (71, 72, 2.5)],
)
def test_invalid_trade_inputs(rates, entry, exit_, qty):
    with pytest.raises(ValueError):
        round_trip(rates, entry, exit_, qty, EXEC)


def test_deterministic(rates):
    a = round_trip(rates, 71.0, 72.2, 4, EXEC, spread_pct=0.05)
    b = round_trip(rates, 71.0, 72.2, 4, EXEC, spread_pct=0.05)
    assert a == b


# -- schedule validation -----------------------------------------------------


def test_groww_schedule_is_sourced(schedule):
    assert schedule.broker == "groww" and not schedule.illustrative
    for item in [schedule.brokerage, schedule.gst, *schedule.charges]:
        assert item.source.url.startswith("https://")
        assert item.effective_from is not None


def test_unsupported_broker():
    with pytest.raises(FeeScheduleError, match="unsupported broker"):
        load_fee_schedule(Settings(broker={"name": "nosuchbroker"}))


def test_illustrative_schedule_is_labelled():
    s = load_fee_schedule(Settings(broker={"name": "illustrative"}))
    assert s.illustrative


def write_schedule(tmp_path, mutate):
    import yaml

    from app.costs.schedule import schedule_path

    raw = yaml.safe_load(schedule_path("groww").read_text())
    mutate(raw)
    path = tmp_path / "fees.yaml"
    path.write_text(yaml.safe_dump(raw))
    return Settings(broker={"name": "groww", "fee_schedule_file": str(path)})


def test_missing_required_rate_rejected(tmp_path):
    def drop_stt(raw):
        raw["charges"] = [c for c in raw["charges"] if c["kind"] != "stt"]
    with pytest.raises(FeeScheduleError, match="stt"):
        load_fee_schedule(write_schedule(tmp_path, drop_stt))


def test_float_rate_rejected(tmp_path):
    def floaty(raw):
        raw["gst"]["rate_pct"] = 18.0
    with pytest.raises(FeeScheduleError, match="quoted string"):
        load_fee_schedule(write_schedule(tmp_path, floaty))


def test_negative_rate_rejected(tmp_path):
    def negative(raw):
        raw["charges"][0]["rate_pct"] = "-0.1"
    with pytest.raises(FeeScheduleError):
        load_fee_schedule(write_schedule(tmp_path, negative))


def test_missing_rate_field_rejected(tmp_path):
    def no_rate(raw):
        del raw["brokerage"]["rate_pct"]
    with pytest.raises(FeeScheduleError):
        load_fee_schedule(write_schedule(tmp_path, no_rate))


def test_invalid_effective_dates_rejected(tmp_path):
    def bad_dates(raw):
        raw["gst"]["effective_to"] = "2020-01-01"
    with pytest.raises(FeeScheduleError):
        load_fee_schedule(write_schedule(tmp_path, bad_dates))


def test_rates_not_in_effect(schedule):
    with pytest.raises(FeeScheduleError, match="no rate in effect"):
        schedule.rates_on(date(2025, 3, 12))
    resolved = schedule.rates_on(date(2025, 3, 12), allow_retroactive=True)
    assert resolved.warnings and all("retroactively" in w for w in resolved.warnings)


def test_rounding_enum_values():
    assert {r.value for r in Rounding} == {"paisa", "rupee"}
