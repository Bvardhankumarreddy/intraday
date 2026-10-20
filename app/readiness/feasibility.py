"""Rs 500 economic-feasibility analysis (Phase 9).

Every number comes from the existing Phase 3 engine (``RiskCostEngine``,
``round_trip``, ``break_even_exit_price``) and the configured broker tariff,
under the frozen baseline's settings. Nothing is re-implemented, and the
configured capital is never changed.

These are CALCULATIONS for hypothetical long setups, not market evidence:
- entry prices across the configured price band (Rs 50-100);
- stop distances across the strategy's allowed risk band
  (``strategy.min_risk_pct`` to ``max_risk_pct``);
- target = entry + ``min_reward_risk`` x risk (the strategy's own rule);
- spread is the assumed spread (no quote), slippage per the configuration.

The question each row answers: would the frozen rules allow ANY whole-share
position, and what do round-trip costs do to the reward/risk?

``scaling_grid`` is INFORMATIONAL only. It shows the same rules at larger
capitals for a Rs 70 entry, to separate the capital effect from the
cost-versus-stop-width effect. The absolute rupee limits (``max_daily_loss``,
``max_aggregate_risk``) are left as configured, so they bind at larger
capitals; the grid reports net reward/risk regardless. It is not a
recommendation, and the configured capital is never changed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from app.config import Settings, settings_from_values
from app.costs.break_even import break_even_exit_price
from app.costs.calculator import round_trip
from app.costs.money import D
from app.data.models import DataMode
from app.risk.engine import RiskCostEngine
from app.risk.interfaces import TradeEvaluationRequest
from app.scan.models import TradeSetup

PRICES = (50, 60, 70, 80, 90, 100)


def _setup(price: float, stop_pct: float, rr: float, when: datetime) -> TradeSetup:
    stop = round(price * (1 - stop_pct / 100), 2)
    risk = price - stop
    target = round(price + rr * risk + 0.004, 2)
    return TradeSetup(
        symbol=f"HYPO{int(price)}", exchange="NSE", signal_candle_start=when - timedelta(minutes=5),
        signal_time=when, valid_until=when + timedelta(minutes=10),
        entry_trigger="hypothetical", entry_reference=price, entry_basis="hypothetical",
        stop_reference=stop, stop_rule=f"{stop_pct}% below entry", target_reference=target,
        target_rule=f"entry + {rr} x risk", risk_per_share=round(risk, 4),
        planned_reward_risk=round((target - price) / risk, 4), exit_by=when + timedelta(hours=5))


def _evaluate(settings: Settings, engine: RiskCostEngine, setup: TradeSetup, capital: float):
    req = TradeEvaluationRequest(
        setup=setup, available_capital=capital, risk_pct=settings.risk.risk_pct,
        broker=settings.broker.name, data_mode=DataMode.SIMULATED,
        data_source="feasibility-calculation", evaluated_at=setup.signal_time,
        config_version=settings.version())
    return engine.evaluate(req)


def _with_capital(settings: Settings, capital: float) -> Settings:
    data = settings.model_dump()
    data["capital"]["initial_capital"] = capital
    return settings_from_values(data)


SCALING_CAPITALS = (500, 2000, 10000, 50000, 200000)
SCALING_STOPS = (0.5, 1.0, 1.5, 2.0, 3.0)


def scaling_grid(settings: Settings, when: datetime, price: float = 70) -> list[dict]:
    out = []
    for stop in SCALING_STOPS:
        setup = _setup(price, stop, settings.strategy.min_reward_risk, when)
        for cap in SCALING_CAPITALS:
            s = _with_capital(settings, cap)
            r = _evaluate(s, RiskCostEngine(s), setup, cap)
            out.append({"entry_price": price, "stop_pct": stop, "capital": cap,
                        "status": r.status.value, "final_quantity": r.final_quantity,
                        "net_reward_risk": float(r.net_reward_risk)
                        if r.net_reward_risk is not None else None,
                        "min_net_reward_risk": settings.cost_review.min_net_reward_risk,
                        "reasons": "; ".join(x.code for x in r.rejection_reasons)})
    return out


def analyse(settings: Settings, when: datetime) -> dict:
    engine = RiskCostEngine(settings)
    if engine.schedule is None:
        raise RuntimeError(f"fee schedule unavailable: {engine.schedule_error}")
    rates = engine.schedule.rates_on(when.date(),
                                     settings.cost_review.apply_current_rates_to_past_dates)
    capital = settings.capital.initial_capital
    st = settings.strategy
    stops = sorted({st.min_risk_pct, 0.5, 1.0, 2.0, st.max_risk_pct})
    cost_rows, setup_rows = [], []
    for price in PRICES:
        p = D(price)
        qty = int(D(capital) // p)
        one = round_trip(rates, p, p, 1, settings.execution, None)
        full = round_trip(rates, p, p, qty, settings.execution, None) if qty else None
        be = break_even_exit_price(rates, p, qty, settings.execution) if qty else None
        cost_rows.append({
            "entry_price": price, "max_whole_shares": qty,
            "position_value": float(p * qty),
            "round_trip_costs_1_share": float(one.total_costs),
            "round_trip_fees_1_share": float(one.fees_total),
            "round_trip_costs_max_shares": float(full.total_costs) if full else None,
            "costs_pct_of_position_max_shares":
                round(float(full.total_costs / (p * qty) * 100), 3) if full else None,
            "break_even_exit_max_shares": float(be) if be is not None else None,
            "break_even_move_pct": round(float((be - p) / p * 100), 3) if be is not None
            else None,
        })
        for stop_pct in stops:
            setup = _setup(price, stop_pct, st.min_reward_risk, when)
            r = _evaluate(settings, engine, setup, capital)
            gross_rr = (setup.target_reference - price) / (price - setup.stop_reference)
            row = {
                "entry_price": price, "stop_pct": stop_pct, "stop": setup.stop_reference,
                "target": setup.target_reference, "gross_reward_risk": round(gross_rr, 3),
                "status": r.status.value, "final_quantity": r.final_quantity,
                "risk_budget": float(r.risk_budget),
                "risk_based_qty": r.risk_based_quantity,
                "capital_based_qty": r.capital_based_quantity,
                "cost_adjusted_qty": r.cost_adjusted_quantity,
                "binding_constraint": r.binding_constraint,
                "planned_net_loss_at_stop": float(r.planned_net_loss_at_stop)
                if r.planned_net_loss_at_stop is not None else None,
                "net_reward_risk": float(r.net_reward_risk) if r.net_reward_risk is not None
                else None,
                "reasons": "; ".join(f"{x.code}" for x in r.rejection_reasons),
            }
            one_share = _setup(price, stop_pct, st.min_reward_risk, when)
            rt1 = round_trip(rates, D(price), D(one_share.stop_reference), 1, settings.execution,
                             None)
            row["one_share_net_loss_at_stop"] = float(-rt1.net_pnl)
            setup_rows.append(row)
    qualified = [r for r in setup_rows if r["status"] == "QUALIFIED_FOR_REVIEW"]
    return {
        "capital": capital,
        "risk_pct": settings.risk.risk_pct,
        "risk_budget": round(capital * settings.risk.risk_pct / 100, 2),
        "include_costs_in_risk": settings.risk.include_costs_in_risk,
        "min_net_reward_risk": settings.cost_review.min_net_reward_risk,
        "assumed_spread_pct": settings.execution.assumed_spread_pct,
        "slippage_pct_per_side": settings.execution.slippage_pct_per_side,
        "fee_schedule": {"id": engine.schedule.schedule_id,
                         "verified_on": str(engine.schedule.verified_on)},
        "cost_table": cost_rows,
        "setups": setup_rows,
        "setups_evaluated": len(setup_rows),
        "setups_qualified": len(qualified),
        "status_counts": {s: sum(1 for r in setup_rows if r["status"] == s)
                          for s in sorted({r["status"] for r in setup_rows})},
        "informational_scaling_grid": scaling_grid(settings, when),
    }
