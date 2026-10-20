"""Contract between the strategy engine and the Phase 3 risk/cost engine.

The strategy produces reference prices only. Quantity, affordability,
brokerage, taxes, slippage and net P&L belong exclusively to the risk/cost
engine (``app.risk.engine.RiskCostEngine``), which implements
``TradeEvaluator``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, Field

from app.config import Settings
from app.data.models import DataMode
from app.scan.models import Candidate, RiskReport, TradeSetup


class TradeEvaluationRequest(BaseModel):
    setup: TradeSetup  # entry/stop/target references and timestamps
    available_capital: float = Field(gt=0)  # configured capital (before open positions)
    risk_pct: float = Field(gt=0)
    broker: str  # selects the fee schedule
    data_mode: DataMode
    data_source: str = "unknown"
    evaluated_at: datetime
    config_version: str
    # Bid-ask spread from a validated quote, and when it was observed.
    spread_pct: float | None = Field(None, ge=0)
    spread_observed_at: datetime | None = None

    @classmethod
    def from_candidate(cls, candidate: Candidate, settings: Settings) -> TradeEvaluationRequest:
        if candidate.setup is None:
            raise ValueError(f"{candidate.symbol} has no trade setup to evaluate")
        return cls(
            setup=candidate.setup,
            available_capital=settings.capital.initial_capital,
            risk_pct=settings.risk.risk_pct,
            broker=settings.broker.name,
            data_mode=candidate.data_mode,
            data_source=candidate.data_source,
            evaluated_at=candidate.evaluated_at,
            config_version=candidate.config_version,
            spread_pct=candidate.screen.spread_pct,
            spread_observed_at=candidate.screen.spread_observed_at,
        )


class TradeEvaluator(Protocol):
    def evaluate(self, request: TradeEvaluationRequest) -> RiskReport: ...
