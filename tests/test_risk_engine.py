"""Phase 3 end to end: Phase 2 candidates through sizing, costs and limits.

The synthetic setup (see tests/helpers.orb_today): entry 71.00, stop 70.40,
target 72.20, signal at 09:40 on 2025-03-12. Fee rates are verified from
2026, so these tests opt in to applying them retroactively (and check the
resulting warning).
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from app.config import Settings
from app.risk.engine import RiskCostEngine
from app.risk.interfaces import TradeEvaluationRequest
from app.risk.risk_limits import DailyRiskState
from app.scan.models import ReviewStatus, ScanDecision, TradeSetup
from app.scan.pipeline import run_scan
from tests.helpers import DAY, at, make_provider, orb_today

RETRO = {"apply_current_rates_to_past_dates": True}
# Large enough account for the setup to be feasible after costs.
ROOMY = dict(
    capital={"initial_capital": 100_000},
    risk={"max_daily_loss": 2000, "max_aggregate_risk": 2000},
    cost_review={**RETRO, "min_net_reward_risk": 1.0},
)


def candidate(calendar, settings, as_of="09:42", **kw):
    p = make_provider(calendar, settings, today_rows=orb_today(), symbol="SYNTH", **kw)
    return run_scan(p, settings, calendar, at(as_of)).ranked[0]


def evaluate(calendar, settings, state=None, as_of="09:42", **kw):
    c = candidate(calendar, settings, as_of=as_of, **kw)
    req = TradeEvaluationRequest.from_candidate(c, settings)
    return RiskCostEngine(settings).evaluate(req, state)


def codes(report):
    return {r.code for r in report.rejection_reasons}


def test_tiny_account_rejected_by_costs(calendar):
    s = Settings(cost_review=RETRO)
    r = evaluate(calendar, s)
    assert r.status is ReviewStatus.REJECTED_COSTS
    assert r.final_quantity == 0 and r.risk_based_quantity == 4
    assert "costs_exceed_risk_budget" in codes(r)
    assert r.at_target is None  # no misleading P&L at quantity zero
    assert any("retroactively" in e for e in r.estimates)


def test_feasible_candidate_full_report(calendar):
    s = Settings(**ROOMY)
    r = evaluate(calendar, s)
    assert r.status is ReviewStatus.QUALIFIED_FOR_REVIEW, codes(r)
    q = r.final_quantity
    assert q > 0 and r.position_value == Decimal("71.0") * q
    t, st = r.at_target, r.at_stop
    assert t.gross_pnl == Decimal("1.2") * q and st.gross_pnl == Decimal("-0.6") * q
    assert t.net_pnl == t.gross_pnl - t.total_costs
    assert t.total_costs == t.fees_total + t.spread_cost + t.slippage_cost
    assert t.buy.stt == 0 and t.sell.stt > 0 and t.buy.stamp_duty > 0 and t.sell.stamp_duty == 0
    assert r.planned_net_loss_at_stop == -st.net_pnl <= r.risk_budget
    assert r.net_reward_risk == (t.net_pnl / -st.net_pnl).quantize(Decimal("0.01"))
    assert Decimal("71") < r.break_even_exit < r.exit_for_desired_profit
    assert r.cost_model.broker == "groww" and not r.cost_model.illustrative
    assert r.cost_model.sources and r.disclaimers


def test_net_reward_risk_threshold(calendar):
    s = Settings(**{**ROOMY, "cost_review": {**RETRO, "min_net_reward_risk": 1.9}})
    r = evaluate(calendar, s)
    assert r.status is ReviewStatus.REJECTED_COSTS and "net_reward_risk_too_low" in codes(r)


def test_risk_limits_override_feasible_trade(calendar):
    s = Settings(**ROOMY)
    state = DailyRiskState.new_session(DAY)
    state.record_realized(-2000, s)
    r = evaluate(calendar, s, state=state)
    assert state.locked
    assert r.status is ReviewStatus.REJECTED_RISK_LIMIT and "daily_loss_lock" in codes(r)


def test_unknown_realized_pnl_is_insufficient_data(calendar):
    s = Settings(**ROOMY)
    r = evaluate(calendar, s, state=DailyRiskState(trading_date=DAY, realized_pnl=None))
    assert r.status is ReviewStatus.INSUFFICIENT_DATA and "realized_pnl_unknown" in codes(r)


def test_missing_fee_rates_without_retroactive_opt_in(calendar):
    s = Settings()  # trade date 2025-03-12 predates verified rates
    r = evaluate(calendar, s)
    assert r.status is ReviewStatus.INSUFFICIENT_DATA and "fee_rates_not_in_effect" in codes(r)


def test_unsupported_broker(calendar):
    s = Settings(broker={"name": "nosuchbroker"}, cost_review=RETRO)
    r = evaluate(calendar, s)
    assert r.status is ReviewStatus.INSUFFICIENT_DATA and "fee_schedule_unavailable" in codes(r)


def test_stale_quote_uses_labelled_assumed_spread(calendar):
    s = Settings(**ROOMY)
    r = evaluate(calendar, s)  # helper quote is from 09:15, evaluated 09:42
    assert r.spread_source == "assumed"
    assert any("stale" in e for e in r.estimates)


def setup(**kw):
    base = dict(
        symbol="SYNTH", exchange="NSE", signal_candle_start=at("09:35"), signal_time=at("09:40"),
        valid_until=at("09:50"), entry_trigger="t", entry_reference=71.0, entry_basis="t",
        stop_reference=70.4, stop_rule="t", target_reference=72.2, target_rule="t",
        risk_per_share=0.6, planned_reward_risk=2.0, exit_by=at("15:10"),
    )
    base.update(kw)
    return base


def request(s, evaluated_at="09:42", **kw):
    from app.data.models import DataMode

    # Bypass validation on purpose: the engine must catch bad setups itself.
    return TradeEvaluationRequest.model_construct(
        setup=TradeSetup.model_construct(**setup(**kw)),
        available_capital=s.capital.initial_capital, risk_pct=s.risk.risk_pct,
        broker=s.broker.name, data_mode=DataMode.SIMULATED, data_source="test",
        evaluated_at=at(evaluated_at), config_version="t", spread_pct=None,
        spread_observed_at=None,
    )


def test_request_model_rejects_invalid_setup():
    with pytest.raises(ValueError):
        TradeEvaluationRequest(setup=setup(stop_reference=71.5), available_capital=500,
                               risk_pct=0.5, broker="groww", data_mode="simulated",
                               evaluated_at=at("09:42"), config_version="t")


def test_invalid_setup_levels():
    s = Settings(**ROOMY)
    r = RiskCostEngine(s).evaluate(request(s, stop_reference=71.5))
    assert r.status is ReviewStatus.INVALID_SETUP and "invalid_levels" in codes(r)


def test_expired_signal():
    s = Settings(**ROOMY)
    r = RiskCostEngine(s).evaluate(request(s, evaluated_at="10:30"))
    assert r.status is ReviewStatus.INVALID_SETUP and "signal_expired" in codes(r)


def test_quantity_ignores_score(calendar):
    s = Settings(**ROOMY)
    c = candidate(calendar, s)
    engine = RiskCostEngine(s)
    a = engine.evaluate(TradeEvaluationRequest.from_candidate(c, s))
    c.score = 100.0
    b = engine.evaluate(TradeEvaluationRequest.from_candidate(c, s))
    assert a.final_quantity == b.final_quantity


def test_deterministic_reports(calendar):
    s = Settings(**ROOMY)
    assert evaluate(calendar, s).model_dump() == evaluate(calendar, s).model_dump()


# -- pipeline integration ------------------------------------------------------


def scan(calendar, s, **kw):
    p = make_provider(calendar, s, today_rows=orb_today(), symbol="SYNTH")
    return run_scan(p, s, calendar, at("09:42"), risk_engine=RiskCostEngine(s), **kw)


def test_scan_no_trade_on_500_rupees(calendar):
    result = scan(calendar, Settings(cost_review=RETRO))
    assert result.risk_evaluated
    assert [c.symbol for c in result.ranked] == ["SYNTH"]  # technically qualified...
    assert result.reviewable == []                          # ...but not financially feasible
    assert result.decision is ScanDecision.NO_TRADE
    assert result.ranked[0].risk_cost_status is ReviewStatus.REJECTED_COSTS


def test_scan_candidate_for_review(calendar):
    result = scan(calendar, Settings(**ROOMY))
    assert result.decision is ScanDecision.CANDIDATES_FOR_REVIEW
    assert [c.symbol for c in result.reviewable] == ["SYNTH"]
    assert result.reviewable[0].risk_report.final_quantity > 0


def test_scan_without_engine_keeps_phase2_behaviour(calendar):
    s = Settings()
    p = make_provider(calendar, s, today_rows=orb_today(), symbol="SYNTH")
    result = run_scan(p, s, calendar, at("09:42"))
    assert not result.risk_evaluated and result.decision is ScanDecision.CANDIDATES_FOR_REVIEW
    assert result.ranked[0].risk_cost_status is ReviewStatus.NOT_EVALUATED


def test_scan_rolls_stale_state_to_new_day(calendar):
    s = Settings(**ROOMY)
    old = DailyRiskState.new_session(DAY - timedelta(days=1))
    old.record_realized(-5000, s)
    result = scan(calendar, s, risk_state=old)  # new trading date: lock does not carry over
    assert result.decision is ScanDecision.CANDIDATES_FOR_REVIEW


@pytest.mark.parametrize("pkg", ["app.risk", "app.costs", "app.scan"])
def test_no_order_placement_code(pkg):
    import importlib
    import pathlib

    root = pathlib.Path(importlib.import_module(pkg).__file__).parent
    text = " ".join(p.read_text().lower() for p in root.rglob("*.py"))
    for word in ("place_order", "modify_order", "cancel_order"):
        assert word not in text
