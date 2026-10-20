from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.backtesting.analysis import (
    Period,
    run_periods,
    sensitivity,
    validate_periods,
    walk_forward,
)
from app.backtesting.engine import BacktestResult, EquityPoint, TradeRecord, run_backtest
from app.backtesting.metrics import compute_metrics, max_drawdown
from app.backtesting.report import write_report
from tests.backtest_helpers import bt_settings, make_dataset, scenario
from tests.helpers import DAY

IST = ZoneInfo("Asia/Kolkata")
T0 = datetime(2025, 3, 12, 9, 40, tzinfo=IST)
DAY2 = date(2025, 3, 13)
NEUTRAL = (71.05, 71.3, 70.9, 71.2, 1000)
TARGET = (71.2, 72.3, 71.1, 72.1, 1000)
STOP = (71.2, 71.3, 70.3, 70.5, 1000)


def trade(net: str, gross: str, costs: str = "10", minutes: int = 30) -> TradeRecord:
    d = Decimal
    return TradeRecord(
        trade_id="T", signal_id="S", symbol="X", session=DAY, signal_time=T0, entry_time=T0,
        entry_reference=d(70), entry_price=d(70), exit_time=T0 + timedelta(minutes=minutes),
        exit_price=d(71), exit_reason="TARGET", quantity=10, stop=d(69), target=d(72),
        planned_net_loss=d(15), buy_turnover=d(700), sell_turnover=d(710), gross_pnl=d(gross),
        brokerage=d(0), stt=d(0), stamp_duty=d(0), exchange_transaction=d(0), ipft=d(0),
        sebi_turnover_fee=d(0), gst=d(0), spread_cost=d(0), slippage_cost=d(0),
        total_costs=d(costs), net_pnl=d(net), holding_minutes=minutes, ambiguous=False,
        incomplete=False, fee_schedule_id="x", fee_warnings="",
    )


def result(trades, final="1000", equity=None) -> BacktestResult:
    r = BacktestResult(start=DAY, end=DAY, initial_capital=Decimal(1000),
                       final_cash=Decimal(final), dataset_id="d", config_version="c",
                       strategy_version="s", execution_model_version="e",
                       fee_schedule_id="f", sessions=[DAY])
    r.trades = trades
    r.equity = equity or []
    return r


def test_zero_trades_are_safe():
    m = compute_metrics(result([]), 30)
    assert m["trades"] == 0 and m["win_rate_pct"] is None and m["expectancy_per_trade"] is None
    assert m["profit_factor"] is None and m["profit_factor_note"] == "undefined: no trades"
    assert m["avg_holding_minutes"] is None and m["cost_pct_of_traded_value"] is None
    assert m["max_drawdown"] == 0 and m["evidence"] == "INSUFFICIENT EVIDENCE"


def test_no_losses_profit_factor_undefined_not_infinite():
    m = compute_metrics(result([trade("20", "30"), trade("10", "20")], "1030"), 30)
    assert m["profit_factor"] is None and "no losing" in m["profit_factor_note"]
    assert m["win_rate_pct"] == Decimal("100.00") and m["avg_loss"] is None


def test_hand_calculated_metrics():
    trades = [trade("30", "40"), trade("10", "20", minutes=60), trade("-20", "-10"),
              trade("0", "10")]
    m = compute_metrics(result(trades, "1020"), 3)
    assert (m["wins"], m["losses"], m["breakeven_trades"]) == (2, 1, 1)
    assert m["win_rate_pct"] == Decimal("50.00")             # 2 / 4
    assert m["avg_win"] == Decimal("20.00")                  # (30 + 10) / 2
    assert m["avg_loss"] == Decimal("-20.00")
    assert m["expectancy_per_trade"] == Decimal("5.00")      # 20 / 4
    assert m["profit_factor"] == Decimal("2.00")             # 40 / 20
    assert m["total_net_pnl"] == Decimal("20") and m["net_return_pct"] == Decimal("2.00")
    assert m["total_costs"] == Decimal("40")
    assert m["cost_pct_of_traded_value"] == Decimal("0.71")  # 40 / (4 x 1410) x 100
    assert m["cost_pct_of_gross_profit"] == Decimal("57.14")  # 40 / 70 x 100
    assert m["avg_holding_minutes"] == Decimal("37.50")
    assert m["evidence"] == "sample meets minimum"


def test_max_drawdown():
    pts = [EquityPoint(T0, Decimal(e), Decimal(0), Decimal(e)) for e in
           ["1000", "1200", "900", "1300", "1100"]]
    assert max_drawdown(Decimal(1000), pts) == (Decimal("300.00"), Decimal("25.00"))
    assert max_drawdown(Decimal(1000), []) == (Decimal("0.00"), Decimal("0.00"))


def test_drawdown_uses_mark_to_market(calendar):
    # During the stop trade, equity marks the open position at candle closes.
    s = bt_settings()
    ds = make_dataset(calendar, s, {DAY: scenario([NEUTRAL, STOP])})
    r = run_backtest(ds, s, calendar, DAY, DAY)
    m = compute_metrics(r, 1)
    assert m["max_drawdown"] >= -r.trades[0].net_pnl  # at least the realized loss


# -- validation framework ------------------------------------------------------


def two_day_dataset(calendar, s):
    return make_dataset(calendar, s, {DAY: scenario([NEUTRAL, TARGET]),
                                      DAY2: scenario([NEUTRAL, STOP])})


def test_periods_must_be_chronological_and_disjoint():
    with pytest.raises(ValueError):
        validate_periods([Period("dev", DAY, DAY2), Period("test", DAY2, DAY2)])
    with pytest.raises(ValueError):
        validate_periods([Period("dev", DAY2, DAY2), Period("test", DAY, DAY)])
    with pytest.raises(ValueError):
        validate_periods([Period("x", DAY2, DAY)])


def test_out_of_sample_periods_are_separate(calendar):
    s = bt_settings(backtest={"min_trades_for_evidence": 1})
    out = run_periods(two_day_dataset(calendar, s), s, calendar,
                      [Period("dev", DAY, DAY), Period("test", DAY2, DAY2)])
    dev, test = out["dev"][0], out["test"][0]
    assert [t.session for t in dev.trades] == [DAY]
    assert [t.session for t in test.trades] == [DAY2]
    assert out["dev"][1]["total_net_pnl"] > 0 > out["test"][1]["total_net_pnl"]
    assert test.initial_capital == dev.initial_capital  # each period starts fresh


def test_sensitivity_one_at_a_time(calendar):
    s = bt_settings()
    grid = [("execution", "slippage_pct_per_side", [0.05, 0.10])]
    rows = sensitivity(two_day_dataset(calendar, s), s, calendar, DAY, DAY, grid)
    assert [r["value"] for r in rows] == [0.05, 0.10]
    assert [r["is_base"] for r in rows] == [True, False]
    assert rows[1]["total_costs"] > rows[0]["total_costs"]


def test_walk_forward_tests_on_unseen_window(calendar):
    s = bt_settings(backtest={"min_trades_for_evidence": 1})
    grid = [("execution", "slippage_pct_per_side", [0.05, 0.10])]
    folds = walk_forward(two_day_dataset(calendar, s), s, calendar, 1, 1, grid)
    last = folds[-1]
    assert last["train_end"] < last["test_start"]
    assert last["test_start"] == DAY2


# -- reports -------------------------------------------------------------------


def test_reports_are_reproducible(tmp_path, calendar):
    s = bt_settings()
    ds = two_day_dataset(calendar, s)
    paths = []
    for _ in range(2):
        r = run_backtest(ds, s, calendar, DAY, DAY2)
        paths.append(write_report(r, compute_metrics(r, 30), ds, s, tmp_path))
    assert paths[0] == paths[1]
    files = sorted(p.name for p in paths[0].iterdir())
    assert {"config.json", "dataset_quality.json", "trades.csv", "orders.csv", "fills.csv",
            "rejected_signals.csv", "equity_daily.csv", "metrics.json", "costs.json",
            "assumptions.md"} <= set(files)
    assert "SIMULATED" in (paths[0] / "assumptions.md").read_text()
    snapshot = {f: (paths[0] / f).read_bytes() for f in files}
    r = run_backtest(ds, s, calendar, DAY, DAY2)
    write_report(r, compute_metrics(r, 30), ds, s, tmp_path)
    assert {f: (paths[0] / f).read_bytes() for f in files} == snapshot
