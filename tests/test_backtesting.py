"""Backtest engine and execution model on synthetic scenarios.

Shared setup (tests/backtest_helpers.py): signal at 09:40 for SYNTH with
entry reference 71.00, stop 70.40, target 72.20. With Rs 2,000 and 10% risk
(costs excluded from risk), quantity = 28:
  risk qty floor(200 / 0.60) = 333; capital qty floor(2000 / 71) = 28;
  cash check at 28 shares: 1988 + buy fees 6.03 + spread 0.99 + slippage 0.99
  = 1996.01 <= 2000.
"""

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.backtesting.engine import BacktestConfigError, run_backtest
from app.config import ExecutionCostConfig
from app.costs.calculator import round_trip
from app.costs.schedule import ChargeKind, load_fee_schedule
from tests.backtest_helpers import bt_settings, make_dataset, pad, scenario
from tests.helpers import DAY, orb_today

IST = ZoneInfo("Asia/Kolkata")
DAY2 = date(2025, 3, 13)

NEUTRAL = (71.05, 71.3, 70.9, 71.2, 1000)  # 09:40 entry candle: no stop/target touch
TARGET = (71.2, 72.3, 71.1, 72.1, 1000)
STOP = (71.2, 71.3, 70.3, 70.5, 1000)
GAP_DOWN = (70.0, 70.2, 69.8, 70.1, 1000)
BOTH = (71.2, 72.3, 70.3, 71.0, 1000)


def t(hhmm: str, day: date = DAY) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def run(calendar, after, settings=None, start=DAY, end=DAY, **kw):
    s = settings or bt_settings()
    ds = make_dataset(calendar, s, {DAY: scenario(after)}, **kw)
    return run_backtest(ds, s, calendar, start, end)


def only_trade(result):
    assert len(result.trades) == 1, (result.trades, result.rejected)
    return result.trades[0]


def test_target_exit_hand_calculated(calendar):
    tr = only_trade(run(calendar, [NEUTRAL, TARGET]))
    assert tr.signal_time == t("09:40")
    assert (tr.entry_time, tr.entry_price) == (t("09:40"), Decimal("71.05"))  # next open
    assert (tr.exit_time, tr.exit_price, tr.exit_reason) == (t("09:45"), Decimal("72.2"),
                                                             "TARGET")
    assert tr.quantity == 28
    assert tr.gross_pnl == Decimal("32.20")  # (72.20 - 71.05) x 28
    # buy fees 6.03 + sell fees 6.48 + spread 2.00 + slippage 2.00
    assert tr.total_costs == Decimal("16.51")
    assert tr.net_pnl == Decimal("15.69")
    assert tr.holding_minutes == 5


def test_costs_match_phase3_calculator(calendar):
    s = bt_settings()
    tr = only_trade(run(calendar, [NEUTRAL, TARGET], s))
    rates = load_fee_schedule(s).rates_on(DAY, allow_retroactive=True)
    rt = round_trip(rates, tr.entry_price, tr.exit_price, tr.quantity, s.execution, None)
    assert (tr.total_costs, tr.net_pnl, tr.gst, tr.stt) == (
        rt.total_costs, rt.net_pnl, rt.gst, rt.charge(ChargeKind.STT))
    assert tr.fee_schedule_id.startswith("groww") and "retroactively" in tr.fee_warnings


def test_stop_exit(calendar):
    tr = only_trade(run(calendar, [NEUTRAL, STOP]))
    assert (tr.exit_price, tr.exit_reason) == (Decimal("70.4"), "STOP")
    assert tr.gross_pnl == Decimal("-18.20")  # (70.40 - 71.05) x 28


def test_gap_through_stop_fills_at_open(calendar):
    tr = only_trade(run(calendar, [NEUTRAL, GAP_DOWN]))
    assert (tr.exit_price, tr.exit_reason) == (Decimal("70.0"), "STOP_GAP")
    assert tr.exit_price < tr.stop  # worse than the planned stop


def test_ambiguous_candle_assumes_stop_first(calendar):
    tr = only_trade(run(calendar, [NEUTRAL, BOTH]))
    assert (tr.exit_price, tr.exit_reason, tr.ambiguous) == (Decimal("70.4"),
                                                             "AMBIGUOUS_STOP_FIRST", True)


def test_end_of_day_exit(calendar):
    tr = only_trade(run(calendar, [NEUTRAL]))
    assert (tr.exit_time, tr.exit_reason) == (t("15:10"), "END_OF_DAY")
    assert tr.exit_price == Decimal("71.2")  # open of the 15:10 candle


def test_max_holding_exit(calendar):
    s = bt_settings(backtest={"max_holding_minutes": 30})
    tr = only_trade(run(calendar, [NEUTRAL], s))
    assert (tr.exit_time, tr.exit_reason) == (t("10:10"), "MAX_HOLDING")


@pytest.mark.parametrize(("next_open", "why"), [(70.3, "below stop"), (71.5, "above entry")])
def test_entry_not_filled(calendar, next_open, why):
    candle = (next_open, next_open + 0.1, next_open - 0.1, next_open, 1000)
    r = run(calendar, [candle])
    assert r.trades == []
    assert [x.status for x in r.rejected] == ["ENTRY_NOT_FILLED"]
    assert why.split()[-1] in r.rejected[0].reasons
    assert any(o.status == "CANCELLED" for o in r.orders)


def test_signal_only_from_completed_candle_and_opening_range(calendar):
    # With a 30-minute range, the 09:35 breakout candle is inside the range.
    s = bt_settings(strategy={"opening_range_minutes": 30})
    assert run(calendar, [NEUTRAL, TARGET], s).trades == []


def test_insufficient_capital(calendar):
    r = run(calendar, [NEUTRAL, TARGET], bt_settings(capital={"initial_capital": 60}))
    assert r.trades == []
    assert [x.status for x in r.rejected] == ["REJECTED_CAPITAL"]


def test_tiny_account_default_rules_rejected_by_costs(calendar):
    s = bt_settings(capital={"initial_capital": 500},
                    risk={"risk_pct": 0.5, "include_costs_in_risk": True})
    r = run(calendar, [NEUTRAL, TARGET], s)
    assert r.trades == [] and r.rejected[0].status == "REJECTED_COSTS"


def second_symbol_rows():
    """Flat 70 until a 10:30 breakout (signal 10:35): range 70.10/69.90;
    cumulative volume 15 x 1000 + 10000 = 25000 vs 16000 baseline (1.56);
    stop = candle low 70.00 (0.85% risk)."""
    rows = [(70.0, 70.1, 69.9, 70.0, 1000)] * 15
    rows.append((70.0, 70.65, 70.0, 70.6, 10_000))
    return pad(rows)


def test_planned_loss_above_daily_limit_is_rejected_up_front(calendar):
    # Planned net loss at the stop is 33.25 (0.60 x 28 + costs) > 10.
    r = run(calendar, [NEUTRAL, STOP], bt_settings(risk={"max_daily_loss": 10}))
    assert r.trades == []
    assert "trade_loss_exceeds_daily_limit" in r.rejected[0].reasons


def test_daily_loss_lock_blocks_later_signal(calendar):
    # Limit 40 admits the trade (planned loss 33.25), but the gap through the
    # stop realizes -45.83, which locks the day before SECOND's 10:35 signal.
    s = bt_settings(risk={"max_daily_loss": 40})
    ds = make_dataset(calendar, s, {DAY: scenario([NEUTRAL, GAP_DOWN])},
                      extra={"SECOND": {DAY: second_symbol_rows()}})
    r = run_backtest(ds, s, calendar, DAY, DAY)
    assert [(x.symbol, x.net_pnl) for x in r.trades] == [("SYNTH", Decimal("-45.83"))]
    assert -r.trades[0].net_pnl > r.trades[0].planned_net_loss  # realized worse than planned
    blocked = [x for x in r.rejected if x.symbol == "SECOND"]
    assert blocked and blocked[0].status == "REJECTED_RISK_LIMIT"
    assert "daily_loss_lock" in blocked[0].reasons

    control = bt_settings()  # without the tight limit, SECOND trades too
    r2 = run_backtest(make_dataset(calendar, control, {DAY: scenario([NEUTRAL, GAP_DOWN])},
                                   extra={"SECOND": {DAY: second_symbol_rows()}}),
                      control, calendar, DAY, DAY)
    assert {x.symbol for x in r2.trades} == {"SYNTH", "SECOND"}


def test_cash_and_equity_accounting(calendar):
    r = run(calendar, [NEUTRAL, TARGET])
    tr = only_trade(r)
    assert r.final_cash == r.initial_capital + tr.net_pnl
    # While the position is open (09:40 candle close = 09:45) equity marks it
    point = next(p for p in r.equity if p.time == t("09:45"))
    entry_cash = Decimal("2000") - (Decimal("1989.40") + Decimal("6.03") + Decimal("0.99")
                                    + Decimal("0.99"))
    assert point.cash == entry_cash
    assert point.position_value == Decimal("71.2") * 28
    assert point.equity == point.cash + point.position_value
    assert r.daily_equity[-1].equity == r.final_cash


def test_chronological_records(calendar):
    r = run(calendar, [NEUTRAL, TARGET])
    times = [p.time for p in r.equity]
    assert times == sorted(times) and len(set(times)) == len(times)
    tr = r.trades[0]
    assert tr.signal_time <= tr.entry_time < tr.exit_time
    assert [f.side for f in r.fills] == ["BUY", "SELL"]


def test_no_look_ahead(calendar):
    s = bt_settings()
    base = run(calendar, [NEUTRAL, TARGET], s)
    # Change everything after the exit: the trade must not change.
    crash = [(72.1, 72.1, 60.0, 61.0, 90_000)] * 5
    later = run(calendar, [NEUTRAL, TARGET] + crash, s)
    assert later.trades[0] == base.trades[0]
    # A different FUTURE session cannot change today's trade either.
    ds = make_dataset(calendar, s, {DAY: scenario([NEUTRAL, TARGET]),
                                    DAY2: scenario([NEUTRAL, STOP])})
    two_days = run_backtest(ds, s, calendar, DAY, DAY2)
    assert two_days.trades[0].net_pnl == base.trades[0].net_pnl
    assert two_days.trades[0].exit_price == base.trades[0].exit_price


def test_no_duplicate_entries(calendar):
    # Price dips back under the range high and breaks out again: one trade only.
    again = [(71.05, 71.1, 70.45, 70.45, 1000), (70.45, 71.4, 70.45, 71.3, 20_000)]
    r = run(calendar, [*again])
    assert len(r.trades) == 1 and r.trades[0].signal_time == t("09:40")
    assert len([f for f in r.fills if f.side == "BUY"]) == 1


def test_incomplete_session_closes_flagged(calendar):
    s = bt_settings(backtest={"max_missing_candles_per_session": 30})
    rows = pad(orb_today(after=[NEUTRAL]), n=57)  # data ends at 14:00
    ds = make_dataset(calendar, s, {DAY: rows})
    tr = only_trade(run_backtest(ds, s, calendar, DAY, DAY))
    assert (tr.exit_reason, tr.incomplete) == ("DATA_END", True)


def test_deterministic_runs(calendar):
    a = run(calendar, [NEUTRAL, TARGET])
    b = run(calendar, [NEUTRAL, TARGET])
    assert a.trades == b.trades and a.equity == b.equity and a.rejected == b.rejected


def test_preflight_requires_explicit_assumptions(calendar):
    with pytest.raises(BacktestConfigError, match="require_spread"):
        run(calendar, [NEUTRAL], bt_settings(screening={"require_spread": True}))
    with pytest.raises(BacktestConfigError, match="apply_current_rates"):
        run(calendar, [NEUTRAL],
            bt_settings(cost_review={"apply_current_rates_to_past_dates": False}))


def test_no_margin_or_live_order_api():
    import pathlib

    import app.backtesting as pkg
    text = " ".join(p.read_text().lower()
                    for p in pathlib.Path(pkg.__file__).parent.rglob("*.py"))
    for forbidden in ("place_order", "modify_order", "cancel_order", "import requests",
                      "import httpx", "urllib.request"):
        assert forbidden not in text


def test_execution_config_is_shared_with_phase3():
    assert isinstance(bt_settings().execution, ExecutionCostConfig)
