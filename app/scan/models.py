"""Validated outputs of a scan: screening, indicators, signals and ranking.

``None`` always means "unavailable" and is never interchangeable with zero.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.data.models import DataMode


class Reason(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str


class ScreenStatus(StrEnum):
    PASS = "PASS"
    REJECTED = "REJECTED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class ScreenResult(BaseModel):
    status: ScreenStatus
    last_price: float | None = None
    last_price_time: datetime | None = None
    avg_traded_value: float | None = None
    avg_traded_value_sessions: int = 0
    spread_pct: float | None = None
    spread_observed_at: datetime | None = None
    passed: list[Reason] = []
    rejections: list[Reason] = []


class SignalStatus(StrEnum):
    # Screening passed but no qualifying breakout yet; may still develop.
    WATCH = "WATCH"
    # Every Phase 2 rule passed. Affordability and costs are not yet checked.
    QUALIFIED_PRE_COST = "QUALIFIED_PRE_COST"
    # Signal fired earlier but price later traded at or below the stop.
    INVALIDATED = "INVALIDATED"
    # Signal fired earlier but its entry window has passed.
    EXPIRED = "EXPIRED"
    # No valid setup today, or a mandatory rule failed.
    REJECTED = "REJECTED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    # Screening did not pass, so the strategy was not run.
    NOT_EVALUATED = "NOT_EVALUATED"


class Indicators(BaseModel):
    """Indicator values at the last completed candle (or the signal candle)."""

    as_of_candle: datetime | None = None
    close: float | None = None
    vwap: float | None = None
    ema: float | None = None
    opening_range_high: float | None = None
    opening_range_low: float | None = None
    opening_range_width_pct: float | None = None
    relative_volume: float | None = None
    relative_volume_sessions: int = 0
    relative_volume_today: float | None = None  # numerator (today's window volume)
    relative_volume_baseline: float | None = None  # denominator (prior-session mean)


class TradeSetup(BaseModel):
    """Everything Phase 3 (risk sizing and costs) needs. Prices are reference
    levels, not fills. Quantity and costs are deliberately absent."""

    symbol: str
    exchange: str
    side: str = "BUY"
    signal_candle_start: datetime
    # The signal is known only when its candle closes; no entry before this.
    signal_time: datetime
    valid_until: datetime
    entry_trigger: str
    entry_reference: float = Field(gt=0)
    entry_basis: str
    stop_reference: float = Field(gt=0)
    stop_rule: str
    target_reference: float = Field(gt=0)
    target_rule: str
    risk_per_share: float = Field(gt=0)
    planned_reward_risk: float = Field(gt=0)
    exit_by: datetime

    @model_validator(mode="after")
    def _check_levels(self) -> TradeSetup:
        if not self.stop_reference < self.entry_reference < self.target_reference:
            raise ValueError("require stop < entry < target for a long setup")
        if self.valid_until <= self.signal_time:
            raise ValueError("valid_until must be after signal_time")
        return self

    @property
    def signal_id(self) -> str:
        return f"{self.exchange}:{self.symbol}:ORB:{self.signal_candle_start.isoformat()}"


class ScoreComponent(BaseModel):
    name: str
    weight: float
    normalized: float | None  # 0..1, None when the input is unavailable
    points: float
    note: str


class ReviewStatus(StrEnum):
    """Phase 3 outcome for a QUALIFIED_PRE_COST candidate.

    Precedence when several apply: INVALID_SETUP > INSUFFICIENT_DATA >
    REJECTED_RISK_LIMIT > REJECTED_CAPITAL > REJECTED_COSTS. All reasons are
    reported regardless.
    """

    NOT_EVALUATED = "NOT_EVALUATED"
    QUALIFIED_FOR_REVIEW = "QUALIFIED_FOR_REVIEW"
    REJECTED_RISK_LIMIT = "REJECTED_RISK_LIMIT"
    REJECTED_CAPITAL = "REJECTED_CAPITAL"
    REJECTED_COSTS = "REJECTED_COSTS"
    INVALID_SETUP = "INVALID_SETUP"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class OrderCostReport(BaseModel):
    side: str
    price: Decimal
    turnover: Decimal
    brokerage: Decimal
    stt: Decimal
    stamp_duty: Decimal
    exchange_transaction: Decimal
    ipft: Decimal
    sebi_turnover_fee: Decimal
    other: Decimal
    gst_base: Decimal
    gst: Decimal
    gst_applied_to: list[str]
    total: Decimal


class ExitScenario(BaseModel):
    """Outcome if the position were exited at ``exit_price``. Hypothetical."""

    label: str
    exit_price: Decimal
    gross_pnl: Decimal
    buy: OrderCostReport
    sell: OrderCostReport
    spread_cost: Decimal
    slippage_cost: Decimal
    fees_total: Decimal
    total_costs: Decimal
    net_pnl: Decimal
    net_return_pct: Decimal


class CostModelInfo(BaseModel):
    schedule_id: str
    broker: str
    illustrative: bool
    version: str
    verified_on: date
    trade_date: date
    sources: list[str]
    notes: list[str]
    warnings: list[str]


class RiskReport(BaseModel):
    symbol: str
    exchange: str
    status: ReviewStatus
    evaluated_at: datetime
    signal_time: datetime
    signal_source: str  # data source and mode
    available_capital: Decimal
    entry_reference: Decimal
    stop_reference: Decimal
    target_reference: Decimal
    risk_pct: Decimal
    risk_budget: Decimal
    risk_per_share: Decimal | None
    risk_based_quantity: int
    capital_based_quantity: int
    cost_adjusted_quantity: int
    final_quantity: int
    binding_constraint: str | None
    position_value: Decimal | None
    spread_pct_used: Decimal | None
    spread_source: str | None  # "quote" | "assumed"
    slippage_pct_per_side: Decimal
    at_target: ExitScenario | None = None
    at_stop: ExitScenario | None = None
    break_even_exit: Decimal | None = None
    desired_net_profit: Decimal | None = None
    exit_for_desired_profit: Decimal | None = None
    planned_net_loss_at_stop: Decimal | None = None
    net_reward_risk: Decimal | None = None
    cost_model: CostModelInfo | None = None
    estimates: list[str] = []
    qualification_reasons: list[Reason] = []
    rejection_reasons: list[Reason] = []
    disclaimers: list[str] = []


class Candidate(BaseModel):
    symbol: str
    name: str
    exchange: str
    evaluated_at: datetime
    data_mode: DataMode
    data_source: str
    last_candle_end: datetime | None = None
    data_warnings: list[str] = []
    screen: ScreenResult
    signal_status: SignalStatus
    indicators: Indicators = Indicators()
    setup: TradeSetup | None = None
    score: float | None = None
    score_components: list[ScoreComponent] = []
    qualification_reasons: list[Reason] = []
    rejection_reasons: list[Reason] = []
    # Phase 3 outcome. NOT_EVALUATED when the risk/cost stage did not run.
    risk_cost_status: ReviewStatus = ReviewStatus.NOT_EVALUATED
    risk_report: RiskReport | None = None
    config_version: str


class ScanDecision(StrEnum):
    CANDIDATES_FOR_REVIEW = "CANDIDATES_FOR_REVIEW"
    NO_TRADE = "NO_TRADE"


class ScanResult(BaseModel):
    evaluated_at: datetime
    session_date: str
    data_mode: DataMode
    data_source: str
    config_version: str
    decision: ScanDecision
    ranked: list[Candidate]  # QUALIFIED_PRE_COST only, best first
    candidates: list[Candidate]  # every instrument evaluated
    # Phase 3: ranked candidates that are also QUALIFIED_FOR_REVIEW.
    risk_evaluated: bool = False
    reviewable: list[Candidate] = []
    notes: list[str] = []
