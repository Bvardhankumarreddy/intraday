"""Versioned schemas for the AI research agent (Phase 10).

Two layers:

1. ``ResearchOutput``: what the language model may return. It holds
   qualitative analysis only. Every claim states its basis
   (``verified_fact`` / ``deterministic_calculation`` / ``interpretation``)
   and cites tool-call evidence IDs. There are no numeric probability or
   confidence fields: the model is never asked for a probability of success.

2. ``Recommendation``: what is persisted. Its numeric fields (prices,
   quantity, costs, net reward:risk) are filled ONLY by deterministic code
   from tool outputs (strategy setup, Phase 3 risk/cost engine). Its
   ``status`` is decided by deterministic gates; the model can only make it
   more conservative.

A recommendation is not an order. A CANDIDATE is not an executed trade.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "agent-rec-v1"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- model output (validated) -------------------------------------------------------


class Claim(_Strict):
    text: str = Field(min_length=1, max_length=600)
    basis: Literal["verified_fact", "deterministic_calculation", "interpretation"]
    # Tool-call IDs (e.g. "T3") whose outputs support the claim. Required for
    # verified facts and calculations; interpretations may cite none.
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)


class CandidateAssessment(_Strict):
    symbol: str = Field(min_length=1, max_length=32)
    observed_facts: list[Claim] = Field(default_factory=list, max_length=12)
    supporting_evidence: list[Claim] = Field(default_factory=list, max_length=12)
    contradicting_evidence: list[Claim] = Field(default_factory=list, max_length=12)
    alternative_explanations: list[str] = Field(default_factory=list, max_length=6)
    # How strong the explanatory evidence is: correlation is not causation.
    evidence_strength: Literal["none", "correlation_only", "plausible_explanation",
                               "verified_causal"]
    entry_conditions: list[str] = Field(default_factory=list, max_length=8)
    invalidation_conditions: list[str] = Field(default_factory=list, max_length=8)
    risk_cost_assessment: str = Field(max_length=800)
    missing_information: list[str] = Field(default_factory=list, max_length=10)
    recommendation: Literal["consider", "wait_for_confirmation", "no_trade"]
    rationale: str = Field(max_length=1200)


class ResearchOutput(_Strict):
    market_context: list[Claim] = Field(default_factory=list, max_length=10)
    candidates: list[CandidateAssessment] = Field(default_factory=list, max_length=10)
    source_disagreements: list[str] = Field(default_factory=list, max_length=10)
    overall: Literal["candidates_for_review", "watch_only", "no_trade"] = Field(
        description="Run-level verdict: candidates_for_review, watch_only or no_trade. Not a "
                    "per-candidate value (wait_for_confirmation belongs in candidates).")
    overall_rationale: str = Field(max_length=1200)


class PostSessionReview(_Strict):
    process_observations: list[Claim] = Field(default_factory=list, max_length=12)
    outcome_observations: list[Claim] = Field(default_factory=list, max_length=12)
    process_vs_outcome: str = Field(max_length=1200)
    data_issues: list[str] = Field(default_factory=list, max_length=10)
    # Observations only. Nothing here changes the strategy (no tuning).
    observations_for_future_evaluation: list[str] = Field(default_factory=list,
                                                          max_length=10)


# -- persisted recommendation ---------------------------------------------------------


class RecStatus(StrEnum):
    RESEARCH_ONLY = "RESEARCH_ONLY"  # analysis only; no valid strategy setup (e.g. pre-session)
    WATCH = "WATCH"  # setup forming / waiting for confirmation / AI review unavailable
    CANDIDATE = "CANDIDATE"  # every deterministic check passed AND the AI did not object
    REJECTED_BY_RISK = "REJECTED_BY_RISK"  # Phase 3 risk/cost engine rejected it
    NO_TRADE = "NO_TRADE"  # abstain (stale data, AI abstained, nothing qualifies)
    EXPIRED = "EXPIRED"  # used only in appended outcome records, never by rewriting


class SourceRef(_Strict):
    tool_call_id: str
    tool: str
    source: str
    data_mode: str | None = None
    as_of: str | None = None


class Recommendation(_Strict):
    schema_version: str = SCHEMA_VERSION
    recommendation_id: str
    run_id: str
    created_at: datetime
    market_date: date
    window: Literal["pre", "intraday", "post"]
    symbol: str | None  # None = the run-level NO_TRADE record
    exchange: str = "NSE"
    status: RecStatus
    status_reasons: list[str]
    valid_until: datetime | None = None
    # versions
    model: str | None
    prompt_version: str
    strategy_version: str
    config_version: str
    fee_schedule_id: str | None = None
    # deterministic, from tools
    screen_status: str | None = None
    signal_status: str | None = None
    signal_id: str | None = None
    score: float | None = None
    entry_condition: str | None = None
    entry_reference: float | None = None
    stop: float | None = None
    target: float | None = None
    proposal_basis: str | None = None  # "existing ORB strategy rules"
    quantity: int | None = None
    estimated_costs_at_target: str | None = None
    estimated_spread_slippage: str | None = None
    planned_net_loss_at_stop: str | None = None
    net_reward_risk: str | None = None
    risk_status: str | None = None
    risk_reasons: list[str] = Field(default_factory=list)
    data_sources: list[SourceRef] = Field(default_factory=list)
    data_fresh: bool
    # AI layer (clearly labelled)
    ai_status: Literal["OK", "UNAVAILABLE", "NOT_REQUESTED", "INVALID_OUTPUT"]
    ai_assessment: CandidateAssessment | None = None
    ai_market_context: list[Claim] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    untrusted_content_flags: list[str] = Field(default_factory=list)
    model_call_ids: list[str] = Field(default_factory=list)
    tool_call_ids: list[str] = Field(default_factory=list)
    disclaimer: str = ("Research only. Not an order and not advice to trade. Real orders are "
                       "placed manually by the user. No probability of success is implied.")
