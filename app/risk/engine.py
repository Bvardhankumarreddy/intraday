"""Phase 3 risk and cost engine: is a technically qualified setup feasible?

For each request: validate the setup, resolve the fee rates for the trade
date, size the position, cost the exits at target and stop, find break-even,
compute net reward/risk, apply daily/portfolio limits, and return one
``RiskReport`` with a final ``ReviewStatus`` and every reason.

Hard limits are never overridden by score, cost results or any AI layer.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.config import Settings
from app.costs.break_even import BreakEvenError, break_even_exit_price, required_exit_price
from app.costs.calculator import RoundTrip, round_trip
from app.costs.money import D, round_money
from app.costs.schedule import (
    ChargeKind,
    FeeSchedule,
    FeeScheduleError,
    ResolvedRates,
    load_fee_schedule,
)
from app.costs.statutory_charges import OrderCharges
from app.risk.interfaces import TradeEvaluationRequest
from app.risk.position_sizing import InvalidSetupError, SizingResult, size_position
from app.risk.risk_limits import DailyRiskState, available_capital, limit_violations
from app.scan.models import (
    CostModelInfo,
    ExitScenario,
    OrderCostReport,
    Reason,
    ReviewStatus,
    RiskReport,
)

DISCLAIMERS = [
    "Planned loss is not a guaranteed maximum: gaps, slippage, illiquidity or a delayed "
    "manual exit can produce a larger loss than the stop scenario shows.",
    "The target is a reference level, not an expected outcome. Price may never reach it.",
    "This tool does not place or manage orders. Limits apply only to hypothetical "
    "positions recorded here, not to orders placed at the broker.",
]

PRECEDENCE = [
    ReviewStatus.INVALID_SETUP,
    ReviewStatus.INSUFFICIENT_DATA,
    ReviewStatus.REJECTED_RISK_LIMIT,
    ReviewStatus.REJECTED_CAPITAL,
    ReviewStatus.REJECTED_COSTS,
]


class RiskCostEngine:
    def __init__(self, settings: Settings, schedule: FeeSchedule | None = None):
        self.settings = settings
        self.schedule = schedule
        self.schedule_error: str | None = None
        if schedule is None:
            try:
                self.schedule = load_fee_schedule(settings)
            except FeeScheduleError as exc:
                self.schedule_error = str(exc)

    def evaluate(
        self, request: TradeEvaluationRequest, state: DailyRiskState | None = None
    ) -> RiskReport:
        s = self.settings
        setup = request.setup
        trade_date = setup.signal_time.date()
        state = state or DailyRiskState.new_session(trade_date)
        flagged: dict[ReviewStatus, list[Reason]] = {k: [] for k in PRECEDENCE}
        passed: list[Reason] = []
        estimates: list[str] = []

        entry, stop, target = (D(setup.entry_reference), D(setup.stop_reference),
                               D(setup.target_reference))
        report = RiskReport(
            symbol=setup.symbol,
            exchange=setup.exchange,
            status=ReviewStatus.NOT_EVALUATED,
            evaluated_at=request.evaluated_at,
            signal_time=setup.signal_time,
            signal_source=f"{request.data_source} [{request.data_mode.value.upper()}]",
            available_capital=Decimal(0),
            entry_reference=entry,
            stop_reference=stop,
            target_reference=target,
            risk_pct=D(s.risk.risk_pct),
            risk_budget=Decimal(0),
            risk_per_share=entry - stop if entry > stop else None,
            risk_based_quantity=0,
            capital_based_quantity=0,
            cost_adjusted_quantity=0,
            final_quantity=0,
            binding_constraint=None,
            position_value=None,
            spread_pct_used=None,
            spread_source=None,
            slippage_pct_per_side=D(s.execution.slippage_pct_per_side),
            disclaimers=DISCLAIMERS,
        )

        # 1. Setup validity.
        if not (Decimal(0) < stop < entry < target):
            flagged[ReviewStatus.INVALID_SETUP].append(Reason(
                code="invalid_levels", message=f"require 0 < stop {stop} < entry {entry} "
                                               f"< target {target}"))
        if request.evaluated_at > setup.valid_until:
            flagged[ReviewStatus.INVALID_SETUP].append(Reason(
                code="signal_expired", message=f"signal valid until {setup.valid_until:%H:%M}, "
                                               f"evaluated {request.evaluated_at:%H:%M}"))

        # 2. Fee rates for the trade date.
        rates: ResolvedRates | None = None
        if self.schedule is None:
            flagged[ReviewStatus.INSUFFICIENT_DATA].append(Reason(
                code="fee_schedule_unavailable", message=self.schedule_error or "no schedule"))
        else:
            try:
                rates = self.schedule.rates_on(
                    trade_date, s.cost_review.apply_current_rates_to_past_dates)
            except FeeScheduleError as exc:
                flagged[ReviewStatus.INSUFFICIENT_DATA].append(Reason(
                    code="fee_rates_not_in_effect", message=str(exc)))
            else:
                report.cost_model = self._cost_model_info(rates)
                if self.schedule.illustrative:
                    estimates.append("ILLUSTRATIVE fee model: no broker selected; "
                                     "charges are examples, not actual tariffs")
                estimates += rates.warnings

        # 3. Spread input (quote if fresh, otherwise the configured assumption).
        spread = request.spread_pct
        if spread is not None and request.spread_observed_at is not None:
            age = request.evaluated_at - request.spread_observed_at
            if age > timedelta(seconds=s.data.max_staleness_seconds):
                estimates.append(f"quote spread is {age.total_seconds():.0f}s old (stale); "
                                 f"assumed spread used instead")
                spread = None
        if spread is None:
            estimates.append(f"spread assumed at {s.execution.assumed_spread_pct}% "
                             "(no fresh quote)")
        estimates.append(f"slippage assumed at {s.execution.slippage_pct_per_side}% per fill")

        # 4. Sizing, costs and break-even (only with valid levels and rates).
        if rates is not None and not flagged[ReviewStatus.INVALID_SETUP]:
            capital = available_capital(state, s)
            report.available_capital = capital
            try:
                sizing = size_position(entry, stop, capital, s, rates, spread)
            except InvalidSetupError as exc:
                flagged[ReviewStatus.INVALID_SETUP].append(
                    Reason(code="invalid_setup", message=str(exc)))
            else:
                self._apply_sizing(report, sizing, flagged, passed)
                if sizing.final_quantity > 0:
                    self._apply_costs(report, rates, sizing.final_quantity, spread,
                                      flagged, passed)

        # 5. Daily / portfolio limits (run even if sizing failed, for full reasons).
        unknown, violations = limit_violations(
            state, s, setup.signal_id, report.planned_net_loss_at_stop)
        flagged[ReviewStatus.INSUFFICIENT_DATA] += unknown
        flagged[ReviewStatus.REJECTED_RISK_LIMIT] += violations
        if not unknown and not violations:
            passed.append(Reason(code="within_risk_limits",
                                 message="daily loss, trade count, open positions and "
                                         "aggregate risk limits respected"))

        # 6. Final status.
        report.status = next((k for k in PRECEDENCE if flagged[k]),
                             ReviewStatus.QUALIFIED_FOR_REVIEW)
        report.rejection_reasons = [r for k in PRECEDENCE for r in flagged[k]]
        report.qualification_reasons = passed
        report.estimates = estimates
        return report

    # -- helpers -------------------------------------------------------------

    def _apply_sizing(self, report: RiskReport, sizing: SizingResult,
                      flagged: dict, passed: list[Reason]) -> None:
        report.risk_budget = sizing.risk_budget
        report.risk_per_share = sizing.risk_per_share
        report.risk_based_quantity = sizing.risk_based_quantity
        report.capital_based_quantity = sizing.capital_based_quantity
        report.cost_adjusted_quantity = sizing.cost_adjusted_quantity
        report.final_quantity = sizing.final_quantity
        report.binding_constraint = sizing.binding_constraint
        if sizing.final_quantity > 0:
            report.position_value = report.entry_reference * sizing.final_quantity
            passed.append(Reason(code="sized",
                                 message=f"{sizing.final_quantity} shares "
                                         f"(limited by {sizing.binding_constraint})"))
            return
        note = "; ".join(sizing.notes)
        binding = sizing.binding_constraint
        if binding == "capital" or binding == "cash_including_buy_costs":
            flagged[ReviewStatus.REJECTED_CAPITAL].append(
                Reason(code="insufficient_capital", message=note))
        elif binding == "risk_budget":
            flagged[ReviewStatus.REJECTED_RISK_LIMIT].append(
                Reason(code="risk_per_share_exceeds_budget", message=note))
        else:  # price risk fits, but costs push the net loss over the budget
            flagged[ReviewStatus.REJECTED_COSTS].append(
                Reason(code="costs_exceed_risk_budget", message=note))

    def _apply_costs(self, report: RiskReport, rates: ResolvedRates, qty: int,
                     spread: float | None, flagged: dict, passed: list[Reason]) -> None:
        s = self.settings
        entry = report.entry_reference
        at_target = round_trip(rates, entry, report.target_reference, qty, s.execution, spread)
        at_stop = round_trip(rates, entry, report.stop_reference, qty, s.execution, spread)
        report.at_target = _scenario("target", at_target)
        report.at_stop = _scenario("stop", at_stop)
        report.spread_pct_used = at_target.execution.spread_pct_used
        report.spread_source = at_target.execution.spread_source
        report.planned_net_loss_at_stop = -at_stop.net_pnl

        try:
            report.break_even_exit = break_even_exit_price(rates, entry, qty, s.execution, spread)
            report.desired_net_profit = D(s.cost_review.desired_net_profit)
            report.exit_for_desired_profit = required_exit_price(
                rates, entry, qty, s.execution, spread, report.desired_net_profit)
        except BreakEvenError as exc:
            flagged[ReviewStatus.REJECTED_COSTS].append(
                Reason(code="break_even_unreachable", message=str(exc)))

        if at_target.net_pnl <= 0:
            flagged[ReviewStatus.REJECTED_COSTS].append(Reason(
                code="costs_exceed_target_profit",
                message=f"gross Rs {at_target.gross_pnl} at target minus costs "
                        f"Rs {at_target.total_costs} = net Rs {at_target.net_pnl}"))
            return
        if at_stop.net_pnl >= 0:  # cannot happen with stop < entry, but never divide by it
            flagged[ReviewStatus.INVALID_SETUP].append(
                Reason(code="no_loss_at_stop", message="stop scenario shows no loss"))
            return
        rr = round_money(at_target.net_pnl / -at_stop.net_pnl, )
        report.net_reward_risk = rr
        if rr < D(s.cost_review.min_net_reward_risk):
            flagged[ReviewStatus.REJECTED_COSTS].append(Reason(
                code="net_reward_risk_too_low",
                message=f"net reward/risk {rr} < {s.cost_review.min_net_reward_risk} "
                        f"(net +{at_target.net_pnl} / -{-at_stop.net_pnl})"))
        else:
            passed.append(Reason(code="net_reward_risk",
                                 message=f"net reward/risk {rr} after costs"))

    def _cost_model_info(self, rates: ResolvedRates) -> CostModelInfo:
        sch = rates.schedule
        items = [rates.brokerage, rates.gst, *rates.charges]
        return CostModelInfo(
            schedule_id=sch.schedule_id,
            broker=sch.broker,
            illustrative=sch.illustrative,
            version=sch.version,
            verified_on=sch.verified_on,
            trade_date=rates.day,
            sources=sorted({f"{i.source.name} ({i.source.url}), effective "
                            f"{i.effective_from}" for i in items}),
            notes=list(sch.notes),
            warnings=list(rates.warnings),
        )


def _order(o: OrderCharges, price: Decimal) -> OrderCostReport:
    return OrderCostReport(
        side=o.side.value,
        price=price,
        turnover=o.turnover,
        brokerage=o.brokerage,
        stt=o.get(ChargeKind.STT),
        stamp_duty=o.get(ChargeKind.STAMP_DUTY),
        exchange_transaction=o.get(ChargeKind.EXCHANGE_TRANSACTION),
        ipft=o.get(ChargeKind.IPFT),
        sebi_turnover_fee=o.get(ChargeKind.SEBI_TURNOVER_FEE),
        other=o.get(ChargeKind.OTHER),
        gst_base=o.gst_base,
        gst=o.gst,
        gst_applied_to=list(o.gst_components),
        total=o.total,
    )


def _scenario(label: str, rt: RoundTrip) -> ExitScenario:
    return ExitScenario(
        label=label,
        exit_price=rt.exit,
        gross_pnl=rt.gross_pnl,
        buy=_order(rt.buy, rt.entry),
        sell=_order(rt.sell, rt.exit),
        spread_cost=rt.execution.spread_total,
        slippage_cost=rt.execution.slippage_total,
        fees_total=rt.fees_total,
        total_costs=rt.total_costs,
        net_pnl=rt.net_pnl,
        net_return_pct=round_money(rt.net_return_pct),
    )
