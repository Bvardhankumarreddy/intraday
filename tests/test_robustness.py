"""Phase 7 robustness validation. Every dataset here is SYNTHETIC (invented)
and tests implementation logic only. Nothing here is evidence about the
strategy's real performance."""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from app.backtesting.analysis import _with
from app.backtesting.engine import BacktestResult, EquityPoint, TradeRecord, run_backtest
from app.backtesting.execution import Candle, ExitReason, exit_fill, target_trigger
from app.config import settings_from_values
from app.costs.calculator import round_trip
from app.costs.schedule import load_fee_schedule
from app.data.models import day_mask, local_day_keys
from app.robustness import assessment, stats
from app.robustness.baseline import BaselineError, freeze, load_frozen, strategy_fingerprint
from app.robustness.data_audit import (apply_corporate_action_policy, corporate_action_suspects,
                                       known_limitations, missing_sessions)
from app.robustness.protocol import CostScenario, load_protocol
from app.robustness.report import write_report
from app.robustness.runner import RunOptions, evaluate
from app.robustness.sensitivity import (Variant, break_even, build_variants, run_variants,
                                        scenario_settings, sensitivity_tables)
from app.robustness.walk_forward import Plan, PlanError, Window, Fold, chain_results, make_plan, \
    validate_plan, warmup_length
from app.utils.market_calendar import MarketCalendar
from tests.backtest_helpers import bt_settings, flat_day, make_dataset, scenario
from tests.helpers import DAY
from tests.robustness_helpers import (NEUTRAL, PROTOCOL, STOP, TARGET, e2e_days, e2e_settings,
                                      frozen_baseline, small_protocol, write_csv_dataset)

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent.parent


def trade(net: str, session: date = DAY, gross: str | None = None, minute: int = 0,
          entry="71", exit_="72", stop="70.4") -> TradeRecord:
    t = datetime(session.year, session.month, session.day, 10, minute, tzinfo=IST)
    g = Decimal(gross if gross is not None else net)
    return TradeRecord(
        trade_id=f"T{minute}", signal_id=f"S{session}{minute}", symbol="SYNTH", session=session,
        signal_time=t, entry_time=t, entry_reference=Decimal("71"), entry_price=Decimal(entry),
        exit_time=t + timedelta(minutes=30), exit_price=Decimal(exit_), exit_reason="TARGET",
        quantity=10, stop=Decimal(stop), target=Decimal("72.2"), planned_net_loss=Decimal(5),
        buy_turnover=Decimal(710), sell_turnover=Decimal(720), gross_pnl=g, brokerage=Decimal(0),
        stt=Decimal(0), stamp_duty=Decimal(0), exchange_transaction=Decimal(0), ipft=Decimal(0),
        sebi_turnover_fee=Decimal(0), gst=Decimal(0), spread_cost=Decimal(0),
        slippage_cost=Decimal(0), total_costs=g - Decimal(net), net_pnl=Decimal(net),
        holding_minutes=30, ambiguous=False, incomplete=False, fee_schedule_id="x",
        fee_warnings="")


def result_with(trades: list[TradeRecord], sessions: list[date], initial=Decimal(500)):
    r = BacktestResult(start=sessions[0], end=sessions[-1], initial_capital=initial,
                       final_cash=initial + sum((t.net_pnl for t in trades), Decimal(0)),
                       dataset_id="d", config_version="c", strategy_version="s",
                       execution_model_version="e", fee_schedule_id="f", sessions=sessions)
    r.trades = trades
    return r


# -- fast date helpers (performance refactor) -----------------------------------------


@pytest.mark.parametrize("tz", ["Asia/Kolkata", "Europe/London", "America/New_York"])
def test_day_helpers_match_dt_date(tz):
    # Includes DST transitions for the non-IST zones.
    ts = pd.Series(pd.date_range("2025-03-08", "2025-11-03", freq="97min", tz=ZoneInfo(tz)))
    expected = ts.dt.date
    keys = local_day_keys(ts)
    assert [pd.Timestamp(k).date() for k in keys] == list(expected)
    for d in [date(2025, 3, 9), date(2025, 3, 30), date(2025, 11, 2)]:
        assert (day_mask(ts, d) == (expected == d).to_numpy()).all()


# -- chronological plan, warm-up and fold boundaries ----------------------------------


def test_fold_boundaries_follow_the_predeclared_rule(tmp_path, calendar):
    s = e2e_settings(tmp_path)
    ds = make_dataset(calendar, s, {d: flat_day() for d in calendar.trading_days(
        date(2025, 1, 6), date(2025, 3, 28))}, prior=0)
    protocol, _, _ = load_protocol(PROTOCOL)
    plan = make_plan(ds, s, calendar, protocol.walk_forward)
    sessions = [d for d in ds.sessions() if calendar.is_regular_session(d)]
    assert len(plan.warmup) == 5  # max(rv 5, liquidity 5), no daily file
    s_tradable = len(sessions) - 5
    t = max(5, s_tradable // 8)
    assert (plan.test_sessions, plan.validation_sessions, plan.train_sessions) == (t, t, 2 * t)
    assert len(plan.folds) == (s_tradable - 4 * t) // t + 1
    f0, f1 = plan.folds[0], plan.folds[1]
    assert f0.train.sessions == tuple(sessions[5:5 + 2 * t])
    assert f0.validation.start == sessions[5 + 2 * t]
    assert f0.test.sessions == tuple(sessions[5 + 3 * t:5 + 4 * t])
    assert f1.train.start == sessions[5 + t]  # rolled forward by the test length
    assert f1.test.start == sessions[5 + 4 * t]
    # chronological, no overlap within a fold; test windows disjoint and ordered
    for f in plan.folds:
        assert f.train.end < f.validation.start <= f.validation.end < f.test.start
    tests = [d for f in plan.folds for d in f.test.sessions]
    assert tests == sorted(set(tests))
    assert not set(plan.warmup) & {d for f in plan.folds for w in f.windows() for d in w.sessions}


def test_overlapping_plan_is_rejected():
    d = [date(2025, 1, i) for i in range(1, 10)]
    bad = Fold(0, Window(0, "train", tuple(d[0:3])), Window(0, "validation", tuple(d[2:4])),
               Window(0, "test", tuple(d[4:5])))
    with pytest.raises(PlanError):
        validate_plan(Plan("OK", "", (), "", tuple(d), 3, 2, 1, "", (bad,)))


def test_insufficient_sessions_give_insufficient_plan(tmp_path, calendar):
    s = e2e_settings(tmp_path)
    ds = make_dataset(calendar, s, {d: flat_day() for d in calendar.trading_days(
        date(2025, 1, 6), date(2025, 1, 24))}, prior=0)
    plan = make_plan(ds, s, calendar, load_protocol(PROTOCOL)[0].walk_forward)
    assert plan.status == "INSUFFICIENT_DATA" and not plan.folds
    assert "tradable sessions" in plan.reason


def test_warmup_uses_liquidity_lookback_without_daily_file(calendar):
    s = bt_settings()  # rv lookback 10, liquidity 20
    ds = make_dataset(calendar, s)
    n, why = warmup_length(ds, s, calendar)
    assert n == 20 and "no daily file" in why


# -- no future data -------------------------------------------------------------------


def test_later_sessions_do_not_change_earlier_window(calendar):
    """Future data must not leak into earlier decisions: adding a wild later
    session leaves an earlier window's trades and equity unchanged."""
    s = bt_settings()
    base = {DAY: scenario([NEUTRAL, TARGET])}
    later = {**base, date(2025, 3, 13): scenario([(80, 95, 60, 90, 900000)] * 3)}
    r1 = run_backtest(make_dataset(calendar, s, base), s, calendar, DAY, DAY)
    r2 = run_backtest(make_dataset(calendar, s, later), s, calendar, DAY, DAY)
    assert r1.table("trades").equals(r2.table("trades"))
    assert r1.table("equity").equals(r2.table("equity"))


def test_signal_waits_for_candle_completion(calendar):
    """The 09:35 breakout candle closes at 09:40: the entry fills at the 09:40
    open, never at the signal candle's close."""
    s = bt_settings()
    r = run_backtest(make_dataset(calendar, s, {DAY: scenario([NEUTRAL, TARGET])}), s,
                     calendar, DAY, DAY)
    tr = r.trades[0]
    assert tr.signal_time == datetime(2025, 3, 12, 9, 40, tzinfo=IST)
    assert tr.entry_time == tr.signal_time and tr.entry_price == Decimal("71.05")


# -- frozen baseline ----------------------------------------------------------------------


def test_freeze_is_reproducible_and_immutable(tmp_path):
    path = frozen_baseline(tmp_path)
    assert not os.stat(path).st_mode & stat.S_IWUSR  # read-only
    fb = load_frozen(path)
    assert fb.settings.version() == e2e_settings(tmp_path).version()
    assert fb.fingerprint == strategy_fingerprint(e2e_settings(tmp_path))[0]
    assert not fb.code_drift
    with pytest.raises(BaselineError, match="never overwritten"):
        frozen_baseline(tmp_path)


def test_frozen_settings_ignore_environment_overrides(tmp_path, monkeypatch):
    fb = load_frozen(frozen_baseline(tmp_path))
    monkeypatch.setenv("NSE_STRATEGY__RELATIVE_VOLUME_THRESHOLD", "9.9")
    monkeypatch.setenv("NSE_CAPITAL__INITIAL_CAPITAL", "99999")
    again = load_frozen(fb.path)
    assert again.settings.strategy.relative_volume_threshold == 1.2
    assert again.settings.capital.initial_capital == 2000
    assert again.historical_settings().screening.require_spread is False
    assert again.historical_settings().cost_review.apply_current_rates_to_past_dates is True


def test_tampered_baseline_is_rejected(tmp_path):
    path = frozen_baseline(tmp_path)
    body = json.loads(path.read_text())
    body["settings"]["strategy"]["min_reward_risk"] = 1.0
    path.chmod(0o644)
    path.write_text(json.dumps(body))
    with pytest.raises(BaselineError, match="modified after freezing"):
        load_frozen(path)


def test_changed_holiday_file_breaks_the_fingerprint(tmp_path):
    path = frozen_baseline(tmp_path)
    (tmp_path / "holidays.yaml").write_text("- {date: 2025-02-26, name: test}\n")
    with pytest.raises(BaselineError, match="fingerprint"):
        load_frozen(path)


def test_fingerprint_ignores_paths_but_not_parameters(tmp_path):
    a = e2e_settings(tmp_path)
    moved = settings_from_values({**a.model_dump(), "paper": {
        **a.model_dump()["paper"], "db_path": str(tmp_path / "elsewhere.db")}})
    assert strategy_fingerprint(a)[0] == strategy_fingerprint(moved)[0]
    changed = _with(a, "strategy", "min_reward_risk", 3.0)
    assert strategy_fingerprint(a)[0] != strategy_fingerprint(changed)[0]


def test_code_drift_is_detected(tmp_path, monkeypatch):
    path = frozen_baseline(tmp_path)
    import app.robustness.baseline as bl
    monkeypatch.setattr(bl, "code_hash", lambda root=None: ("0" * 64, []))
    assert load_frozen(path).code_drift


def test_sensitivity_variants_ignore_environment(monkeypatch):
    # Phase 4 defect fixed: Settings(**data) let NSE_* env vars override the grid value.
    monkeypatch.setenv("NSE_STRATEGY__MIN_REWARD_RISK", "9")
    base = bt_settings()
    assert _with(base, "strategy", "min_reward_risk", 1.5).strategy.min_reward_risk == 1.5


# -- costs and execution ----------------------------------------------------------------


def test_cost_scenarios_scale_assumptions_without_touching_base():
    base = bt_settings()
    sc = scenario_settings(base, CostScenario(name="x", slippage_multiplier=2,
                                              spread_multiplier=3,
                                              target_trade_through_pct=0.05))
    assert sc.execution.slippage_pct_per_side == pytest.approx(0.10)
    assert sc.execution.assumed_spread_pct == pytest.approx(0.30)
    assert sc.backtest.target_trade_through_pct == 0.05
    assert base.execution.slippage_pct_per_side == 0.05  # base unchanged


def test_spread_scenario_doubles_spread_cost_and_net_identity(calendar):
    s = bt_settings()
    ds = make_dataset(calendar, s, {DAY: scenario([NEUTRAL, TARGET])})
    base = run_backtest(ds, s, calendar, DAY, DAY).trades[0]
    wide = run_backtest(ds, scenario_settings(s, CostScenario(name="w", spread_multiplier=2)),
                        calendar, DAY, DAY).trades[0]
    assert wide.quantity == base.quantity
    assert wide.spread_cost == pytest.approx(2 * base.spread_cost, abs=Decimal("0.02"))
    rates = load_fee_schedule(s).rates_on(DAY, True)
    rt = round_trip(rates, base.entry_price, base.exit_price, base.quantity, s.execution, None)
    assert base.total_costs == rt.total_costs
    for t in (base, wide):
        assert t.gross_pnl - t.total_costs == t.net_pnl


def test_target_trade_through_rule():
    c = Candle(Decimal("71"), Decimal("72.21"), Decimal("70.9"), Decimal("72"))
    target = Decimal("72.2")
    assert exit_fill(c, Decimal("70.4"), target, False, None)[1] is ExitReason.TARGET
    trig = target_trigger(target, 0.05)  # needs 72.2361
    assert exit_fill(c, Decimal("70.4"), target, False, None, trigger=trig) is None
    c2 = Candle(Decimal("71"), Decimal("72.30"), Decimal("70.9"), Decimal("72"))
    price, reason, _ = exit_fill(c2, Decimal("70.4"), target, False, None, trigger=trig)
    assert (price, reason) == (target, ExitReason.TARGET)  # still filled at the target


def test_break_even_definitions():
    pos = [trade("4", gross="10"), trade("2", gross="8", minute=5)]
    be = break_even(pos)
    assert be["status"] == "OK" and be["extra_cost_per_trade_inr"] == 3.0
    assert be["extra_cost_pct_of_traded_value"] == pytest.approx(3 / 1430 * 100, abs=1e-5)
    neg = break_even([trade("-3", gross="2")])
    assert neg["status"] == "NOT_MEANINGFUL" and "fall by Rs 3.00" in neg["note"]
    assert break_even([])["status"] == "NOT_MEANINGFUL"


# -- capital, whole shares, open vs closed, diagnostics --------------------------------------


def test_rs500_rejection_is_reported_and_shadow_never_touches_cash(calendar):
    """Rs 500 default rules: the setup fails the cost-inclusive risk budget.
    The shadow diagnostic follows it, but cash, equity and trades are untouched."""
    s = bt_settings(capital={"initial_capital": 500},
                    risk={"risk_pct": 0.5, "include_costs_in_risk": True, "max_daily_loss": 15,
                          "max_aggregate_risk": 5, "max_trades_per_day": 2},
                    cost_review={"min_net_reward_risk": 1.5})
    ds = make_dataset(calendar, s, {DAY: scenario([NEUTRAL, TARGET])})
    plain = run_backtest(ds, s, calendar, DAY, DAY)
    r = run_backtest(ds, s, calendar, DAY, DAY, shadow=True)
    assert r.trades == [] and r.final_cash == Decimal(500)
    assert r.rejected and r.rejected[0].stage == "risk"
    assert r.table("equity").equals(plain.table("equity"))
    sh = r.shadow[0]
    assert sh.filled and sh.entry_price == Decimal("71.05") and sh.exit_reason == "TARGET"
    assert sh.affordable_qty == 7  # floor(500 / 71.05): whole shares only
    assert sh.r_multiple == round((Decimal("72.2") - Decimal("71.05"))
                                  / (Decimal("71.05") - Decimal("70.4")), 4)
    diag = stats.signal_diagnostics(r, load_protocol(PROTOCOL)[0].statistics)
    assert diag["executed"] == 0 and diag["risk_rejected"] == 1


def test_only_closed_trades_count_and_incomplete_are_flagged(calendar):
    # Allow a truncated session (normally excluded as incomplete) so the data
    # ends right after the entry with the position still open.
    s = bt_settings(backtest={"max_missing_candles_per_session": 75})
    rows = scenario([NEUTRAL])[:6]
    r = run_backtest(make_dataset(calendar, s, {DAY: rows}, prior=20), s, calendar, DAY, DAY)
    assert len(r.trades) == 1 and r.trades[0].incomplete
    assert r.trades[0].exit_reason == "DATA_END"
    m = stats.summarize(r)
    assert m["incomplete_executions"] == 1 and m["trades"] == 1


# -- statistics ---------------------------------------------------------------------------


def test_bootstrap_is_deterministic_and_brackets_the_estimate():
    rng = np.random.default_rng(0)
    days = [(date(2025, 1, 1) + timedelta(days=i), float(rng.normal(1, 5)), 1) for i in range(60)]
    a = stats.bootstrap(days, 500, 0.95, 7)
    b = stats.bootstrap(days, 500, 0.95, 7)
    assert a == b and a["status"] == "OK"
    assert a["ci_low"] < a["estimate"] < a["ci_high"]
    assert a["block_length"] == 4  # ceil(60 ** (1/3))


def test_insufficient_and_empty_samples():
    assert stats.bootstrap([], 500, 0.95, 1)["status"] == "INSUFFICIENT"
    one = stats.bootstrap([(DAY, 5.0, 1), (date(2025, 3, 13), 0.0, 0)], 500, 0.95, 1)
    assert one["status"] == "INSUFFICIENT" and one["ci_low"] is None
    empty = stats.summarize(result_with([], [DAY]))
    assert empty["trades"] == 0 and empty["expectancy_per_trade"] is None
    assert empty["win_rate_pct"] is None and empty["profit_factor"] is None
    assert stats.sample_size([], one, 0.05, 0.8, [1.0])["status"] == "INSUFFICIENT"


def test_zero_loss_profit_factor_is_undefined_not_infinite():
    r = result_with([trade("5"), trade("3", minute=5)], [DAY])
    m = stats.summarize(r)
    assert m["profit_factor"] is None and m["profit_factor_note"] == "undefined: no losing trades"


def test_statistical_metrics_hand_checked():
    d2 = date(2025, 3, 13)
    trades = [trade("10", DAY), trade("-4", DAY, minute=5), trade("-2", d2, minute=10),
              trade("-1", d2, minute=15), trade("6", d2, minute=20)]
    r = result_with(trades, [DAY, d2, date(2025, 3, 17)])
    m = stats.summarize(r)
    assert m["trades"] == 5 and m["win_rate_pct"] == Decimal("40.00")
    assert m["expectancy_per_trade"] == Decimal("1.80")
    assert m["profit_factor"] == Decimal("2.29")  # 16 / 7
    assert m["max_consecutive_losses"] == 3
    assert m["eligible_sessions"] == 3 and m["sessions_with_trades"] == 2
    assert stats.session_pnl(r) == [(DAY, 6.0, 2), (d2, 3.0, 3), (date(2025, 3, 17), 0.0, 0)]
    assert m["session_mean_net_pnl"] == 3.0


def test_sample_size_formulas():
    trades = [trade(str(x), minute=i) for i, x in enumerate([5, -3, 4, -2, 6, -1, 3, -4])]
    nets = np.array([5, -3, 4, -2, 6, -1, 3, -4], dtype=float)
    sd = nets.std(ddof=1)
    naive = sd / np.sqrt(8)
    boot = {"se": naive, "ci_low": -1.0, "ci_high": 3.0}  # design effect exactly 1
    out = stats.sample_size(trades, boot, 0.05, 0.8, [1.0])
    z = 1.959964 + 0.841621
    assert out["design_effect"] == 1.0 and out["effective_n"] == 8.0
    assert out["min_detectable_effect"] == pytest.approx(z * naive, abs=1e-3)
    assert out["trades_needed_for_effect"]["1"] == int(np.ceil((z * sd / 1.0) ** 2))
    assert "lack of evidence" in out["reading"]


def test_selection_bias_helpers():
    assert stats.expected_max_null(1) is None
    assert 1.5 < stats.expected_max_null(20) < stats.expected_max_null(200) < 3.5
    assert stats.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert stats.spearman([1, 2], [2, 1]) is None


def test_chained_out_of_sample_record():
    a = result_with([trade("5")], [DAY])
    a.equity = [EquityPoint(datetime(2025, 3, 12, 10, tzinfo=IST), Decimal(505), Decimal(0),
                            Decimal(505))]
    d2 = date(2025, 3, 20)
    b = result_with([trade("-8", d2)], [d2])
    b.equity = [EquityPoint(datetime(2025, 3, 20, 10, tzinfo=IST), Decimal(492), Decimal(0),
                            Decimal(492))]
    c = chain_results([a, b], Decimal(500))
    assert c.final_cash == Decimal(497) and len(c.trades) == 2
    assert [p.equity for p in c.equity] == [Decimal(505), Decimal(497)]  # 492 + earlier +5
    assert stats.summarize(c)["max_drawdown"] == Decimal("8.00")


# -- assessment ----------------------------------------------------------------------------


def _ok_boot(lo, hi):
    return {"status": "OK", "ci_low": lo, "ci_high": hi, "confidence": 0.95}


def test_assessment_rules():
    p = load_protocol(PROTOCOL)[0]
    diag = {"signals_total": 10, "risk_rejected_by_status": {"REJECTED_COSTS": 10}}
    folds = [{"trades": 3, "net_expectancy": 1.0}, {"trades": 2, "net_expectancy": 2.0}]
    cons = {"conservative": {"net_expectancy": 0.5}}
    v = assessment.assess(p, {"trades": 0}, {"status": "INSUFFICIENT", "reason": "x"}, [], {},
                          diag)
    assert v["verdict"] == "FAILED_PREDEFINED_CRITERIA"
    v = assessment.assess(p, {"trades": 5}, _ok_boot(0.2, 3.0), folds, cons, diag)
    assert v["verdict"] == "SUPPORTS_FURTHER_EVALUATION"
    v = assessment.assess(p, {"trades": 5}, _ok_boot(-0.5, 3.0), folds, cons, diag)
    assert v["verdict"] == "INCONCLUSIVE"
    v = assessment.assess(p, {"trades": 5}, _ok_boot(-3.0, -0.1), folds, cons, diag)
    assert v["verdict"] == "FAILED_PREDEFINED_CRITERIA"
    none = {"signals_total": 0, "risk_rejected_by_status": {}}
    v = assessment.assess(p, {"trades": 0}, {"status": "INSUFFICIENT", "reason": "x"}, [], {},
                          none)
    assert v["verdict"] == "INCONCLUSIVE"
    assert {c["status"] for c in v["criteria"]} == {"NOT_EVALUABLE"}


# -- data quality, corporate actions, universe -----------------------------------------------


def test_corporate_action_suspect_is_flagged_and_truncated(calendar):
    s = bt_settings()
    d1, d2, d3 = date(2025, 3, 11), DAY, date(2025, 3, 13)
    half = [(35.0, 35.05, 34.95, 35.0, 2000)] * 75  # unadjusted 2:1 split look-alike
    ds = make_dataset(calendar, s, {d1: flat_day(), d2: half, d3: half})
    suspects = corporate_action_suspects(ds)
    assert [(x["symbol"], x["session"]) for x in suspects] == [("SYNTH", d2)]
    assert suspects[0]["price_ratio"] == 0.5
    fixed, actions = apply_corporate_action_policy(ds, suspects)
    assert actions[0]["sessions_dropped"] == 2
    assert d2 not in fixed.sessions() and d1 in fixed.sessions()
    assert fixed.dataset_id != ds.dataset_id


def test_missing_and_invalid_sessions_are_reported_not_filled(calendar):
    s = bt_settings()
    bad = flat_day()
    bad[10] = (70.0, 69.0, 71.0, 70.0, 1000)  # high below low: invalid
    ds = make_dataset(calendar, s, {DAY: bad, date(2025, 3, 17): flat_day()}, prior=3)
    assert DAY not in ds.sessions()  # excluded, never repaired
    excluded = ds.quality_report()["excluded_sessions"]
    assert any(e["session"] == "2025-03-12" for e in excluded)
    gaps = missing_sessions(ds, calendar)
    assert date(2025, 3, 13) in gaps["SYNTH"]  # weekday with no rows at all
    assert date(2025, 3, 14) not in gaps["SYNTH"]  # the test holiday


def test_point_in_time_membership_restricts_trading(calendar):
    s = bt_settings()
    ds = make_dataset(calendar, s, {DAY: scenario([NEUTRAL, TARGET])})
    r = run_backtest(ds, s, calendar, DAY, DAY, membership=lambda d: set())
    assert r.trades == [] and any("Point-in-time" in a for a in r.assumptions)
    assert len(run_backtest(ds, s, calendar, DAY, DAY,
                            membership=lambda d: {"SYNTH"}).trades) == 1


def test_limitations_name_survivorship_without_membership():
    out = known_limitations({"point_in_time_universe": None}, has_daily_file=False)
    issues = {x["issue"] for x in out}
    assert "Survivorship and selection bias in the universe" in issues
    assert "No historical bid/ask quotes" in issues
    with_pit = known_limitations({"point_in_time_universe": {"rows": 3}}, True)
    assert "Survivorship and selection bias in the universe" not in {x["issue"] for x in with_pit}


# -- parameter sensitivity ---------------------------------------------------------------------


def test_variants_are_predeclared_deduplicated_and_include_baseline():
    p = load_protocol(PROTOCOL)[0]
    vs = build_variants(p, bt_settings())
    assert vs[0].label == "baseline" and vs[0].changes == ()
    assert len({v.key for v in vs}) == len(vs)
    untestable = [v for v in vs if v.untestable]
    assert {v.changes[0][0] for v in untestable} == {"screening.max_spread_pct"}
    # every OAT value and every grid cell is represented
    grid = p.sensitivity.grid_2d
    assert len(vs) == 1 + sum(len(o.values) - 1 for o in p.sensitivity.one_at_a_time) \
        + (len(grid.x.values) - 1) * (len(grid.y.values) - 1)


def test_sensitivity_tables_report_all_and_select_nothing(tmp_path, calendar):
    p = load_protocol(PROTOCOL)[0]
    base = bt_settings()
    days = calendar.trading_days(date(2025, 2, 3), date(2025, 3, 7))
    plan = Plan("OK", "", (), "", tuple(days), 2, 1, 1, "",
                (Fold(0, Window(0, "train", tuple(days[0:2])),
                      Window(0, "validation", tuple(days[2:3])),
                      Window(0, "test", tuple(days[3:4]))),))
    variants = build_variants(p, base)
    lo = next(v for v in variants if v.changes == (("strategy.relative_volume_threshold", 1.2),))
    hi = next(v for v in variants if v.changes == (("strategy.relative_volume_threshold", 2.0),))
    runs = {}
    for v in variants:
        if v.untestable:
            continue
        net = "-2" if v.key == hi.key else "3"
        trades = [trade(net, days[3])] if v.key != lo.key else \
            [trade(net, days[3], minute=i) for i in range(5)]
        runs[v.key] = ("OK", result_with(trades, list(days[:4])), "")
    out = sensitivity_tables(plan, variants, runs, p, base)
    assert out["configurations_listed"] == len(variants)
    assert out["configurations_untestable"] == 2
    assert all("selected" not in k for row in out["summary"] for k in row)
    flags = {(f["from"], f["to"]): f["reasons"] for f in out["instability"]}
    assert "trade count changes 5 -> 1" in flags[(1.2, 1.5)]
    assert "changes sign" in flags[(1.5, 2.0)]
    cell = next(c for c in out["heatmap"]["cells"] if (c["x"], c["y"]) == (1.5, 2.0))
    assert cell["oos_net_expectancy"] == 3.0  # the frozen baseline's own cell
    assert out["selection_bias"]["expected_max_t_under_no_edge"] is not None


def test_run_variants_parallel_equals_serial(calendar, monkeypatch):
    # Streamlit's AppTest (dashboard tests) leaves the dashboard script as
    # __main__, which spawned workers would re-run. A CLI process has a plain,
    # guarded __main__; mimic that.
    import sys
    import types

    monkeypatch.setitem(sys.modules, "__main__", types.ModuleType("__main__"))
    s = bt_settings()
    ds = make_dataset(calendar, s, {DAY: scenario([NEUTRAL, TARGET])})
    plan = Plan("OK", "", (), "", (DAY,), 1, 1, 1, "",
                (Fold(0, Window(0, "train", (DAY,)), Window(0, "validation", (DAY,)),
                      Window(0, "test", (DAY,))),))
    vs = [Variant("baseline", (), ("base",)),
          Variant("rr3", (("strategy.min_reward_risk", 3.0),), ("oat",))]
    serial = run_variants(ds, s, calendar, plan, vs, None, 1)
    parallel = run_variants(ds, s, calendar, plan, vs, None, 2)
    for v in vs:
        assert serial[v.key][1].table("trades").equals(parallel[v.key][1].table("trades"))


# -- end to end: report, reproducibility, no automatic changes, no orders ---------------------


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    data = write_csv_dataset(tmp / "data")
    path = frozen_baseline(tmp)
    before = path.read_bytes()
    fb = load_frozen(path)
    opts = RunOptions(data_dir=data, grid="none", workers=1, train=4, validation=2, test=2)
    results = evaluate(fb, opts, progress=lambda m: None)
    return tmp, path, before, fb, opts, results


def test_e2e_out_of_sample_reporting(e2e):
    tmp, path, _, fb, _, results = e2e
    assert results["status"] == "OK"
    plan = results["plan"]
    assert plan["warmup_sessions"] == 5 and plan["tradable_sessions"] == 20
    assert len(plan["folds"]) == 7 and plan["oos_sessions"] == 14
    tests = [r for r in results["folds"] if r["partition"] == "test"]
    assert len(tests) == 7 and len(results["folds"]) == 21  # every window reported
    oos = results["oos_result"]
    assert sum(r["trades"] for r in tests) == len(oos.trades) > 0
    assert sorted(oos.sessions) == oos.sessions and len(oos.sessions) == 14
    assert {t.exit_reason for t in oos.trades} == {"TARGET", "STOP"}
    assert results["oos"]["total_net_pnl"] == sum(t.net_pnl for t in oos.trades)
    assert set(results["cost_scenarios"]) == {s.name for s in fb.protocol.cost_scenarios}
    cons = results["cost_scenarios"]["conservative"]
    assert cons["total_costs"] >= results["cost_scenarios"]["base"]["total_costs"]
    assert results["assessment"]["verdict"] in assessment.VERDICTS
    assert results["comparison"]["no_trade"]["net_pnl"] == 0.0
    assert results["comparison"]["benchmark_proxy"]["status"] == "UNAVAILABLE"


def test_e2e_report_is_reproducible(e2e, tmp_path):
    _, path, _, fb, opts, results = e2e
    a = write_report(results, path, tmp_path / "a")
    again = evaluate(fb, opts, progress=lambda m: None)
    b = write_report(again, path, tmp_path / "b")
    files = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    assert files == sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    for f in files:
        assert (a / f).read_bytes() == (b / f).read_bytes(), f
    md = (a / "report.md").read_text()
    for n in range(1, 14):
        assert f"## {n}. " in md
    assert "SIMULATED RESEARCH" in md and "frozen_baseline.json" in {f.name for f in files}
    assert (a / "charts" / "equity.svg").exists()


def test_e2e_makes_no_automatic_parameter_changes(e2e):
    tmp, path, before, fb, _, results = e2e
    assert path.read_bytes() == before  # frozen file untouched
    assert load_frozen(path).fingerprint == fb.fingerprint
    assert "selected" not in json.dumps(results["assessment"])
    assert not (ROOT / "config" / "baselines").exists() or not any(
        p.name.startswith("test-baseline") for p in (ROOT / "config" / "baselines").iterdir())


def test_pending_report_when_data_missing(tmp_path):
    path = frozen_baseline(tmp_path)
    results = evaluate(load_frozen(path), RunOptions(data_dir=tmp_path / "nope", grid="none"))
    assert results["status"] == "PENDING_DATA"
    assert results["assessment"]["verdict"] == "PENDING_DATA"
    assert any("does not exist" in m for m in results["missing"])
    out = write_report(results, path, tmp_path / "out")
    md = (out / "report.md").read_text()
    assert "PENDING" in md and "## 13. " in md
    assert not (out / "oos_trades.csv").exists()  # no fabricated tables


def test_cli_run_refuses_code_drift(tmp_path, monkeypatch, capsys):
    from app.robustness.__main__ import main
    import app.robustness.baseline as bl

    path = frozen_baseline(tmp_path)
    monkeypatch.setattr(bl, "code_hash", lambda root=None: ("f" * 64, []))
    code = main(["run", "--baseline", str(path), "--data", str(tmp_path), "--grid", "none",
                 "--out", str(tmp_path / "o")])
    assert code == 3 and "new strategy version" in capsys.readouterr().err


def test_no_broker_order_endpoints_in_phase7():
    src = "\n".join(p.read_text() for p in (ROOT / "app" / "robustness").glob("*.py"))
    assert not re.search(r"place_order|modify_order|cancel_order|order_id\s*=", src)


def test_fetch_uses_only_candle_reads(tmp_path, calendar):
    from app.data.models import CandleSeries, DataMode, Instrument, parse_interval
    from app.robustness.fetch import fetch_history

    class FakeGroww:
        name = "fake-groww"

        def __init__(self):
            self.calls = []

        def get_candles(self, symbol, interval, start, end):
            self.calls.append((symbol, interval))
            iv = parse_interval(interval)
            frame = make_dataset(calendar, bt_settings(), prior=2).symbols["SYNTH"].intraday \
                if not iv.is_daily else make_dataset(calendar, bt_settings(), prior=2) \
                .symbols["SYNTH"].daily
            return CandleSeries(symbol, iv, frame, DataMode.HISTORICAL, self.name,
                                datetime(2025, 3, 13, tzinfo=IST))

        def __getattr__(self, name):  # any other call (e.g. an order method) fails loudly
            raise AssertionError(f"unexpected call {name}")

    fake = FakeGroww()
    manifest = fetch_history(fake, [Instrument("SYNTH", "Synth")], date(2025, 3, 10), DAY,
                             tmp_path, calendar, 5, None, datetime(2025, 3, 13, tzinfo=IST),
                             progress=lambda m: None)
    assert {c[1] for c in fake.calls} == {"5m", "1d"}
    assert (tmp_path / "SYNTH_5m.csv").exists() and (tmp_path / "instruments.csv").exists()
    assert manifest["symbols"]["SYNTH"]["5m_status"] == "OK"
    assert "adjust" in manifest["adjustment_note"]


def test_dashboard_robustness_view_shows_verdict(e2e, tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    _, path, _, _, _, results = e2e
    write_report(results, path, tmp_path / "rob")
    monkeypatch.setenv("NSE_ROBUSTNESS__OUTPUT_DIR", str(tmp_path / "rob"))
    monkeypatch.setenv("NSE_PAPER_DB", str(tmp_path / "none.db"))
    at = AppTest.from_file(str(ROOT / "app" / "dashboard.py"), default_timeout=30).run()
    at.sidebar.radio[0].set_value("Robustness").run()
    assert not at.exception
    assert any(results["assessment"]["verdict"] in m.value for m in at.markdown)
    assert any("SIMULATED research" in w.value for w in at.warning)
