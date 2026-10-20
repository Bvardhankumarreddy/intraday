"""Typed model of the predeclared Phase 7 evaluation protocol
(``config/robustness/protocol.yaml``). See that file for the meaning and
justification of every field."""

from __future__ import annotations

import hashlib
from datetime import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class WalkForwardRule(_Frozen):
    train_sessions: int | None = Field(None, ge=1)
    validation_sessions: int | None = Field(None, ge=1)
    test_sessions: int | None = Field(None, ge=1)
    min_test_sessions: int = Field(5, ge=1)


class CostScenario(_Frozen):
    name: str
    slippage_multiplier: float = Field(1.0, ge=0)
    spread_multiplier: float = Field(1.0, ge=0)
    target_trade_through_pct: float = Field(0.0, ge=0)


class ParameterValues(_Frozen):
    parameter: str  # "<section>.<key>"
    values: list[Any]
    untestable: str | None = None

    @model_validator(mode="after")
    def _check(self) -> ParameterValues:
        if self.parameter.count(".") != 1 or not self.values:
            raise ValueError(f"bad sensitivity entry {self.parameter!r}")
        return self


class Grid2D(_Frozen):
    x: ParameterValues
    y: ParameterValues


class SensitivityPlan(_Frozen):
    one_at_a_time: list[ParameterValues]
    grid_2d: Grid2D | None = None
    unstable_trade_count_change: float = Field(0.5, gt=0)


class RegimeRules(_Frozen):
    benchmark_symbol: str | None = None
    trend_threshold_pct: float = Field(0.75, gt=0)
    lookback_sessions: int = Field(20, ge=1)
    high_volatility_ratio: float = Field(1.5, gt=0)
    low_volatility_ratio: float = Field(0.75, gt=0)
    high_liquidity_ratio: float = Field(1.25, gt=0)
    low_liquidity_ratio: float = Field(0.8, gt=0)
    price_bands: list[float]
    min_trades_to_interpret: int = Field(10, ge=1)


class ComparisonBaselines(_Frozen):
    entry_time: time = time(9, 30)


class StatisticsPlan(_Frozen):
    bootstrap_resamples: int = Field(5000, ge=100)
    confidence: float = Field(0.95, gt=0, lt=1)
    seed: int = 20261019
    block_length: int | None = Field(None, ge=1)
    alpha: float = Field(0.05, gt=0, lt=1)
    power: float = Field(0.80, gt=0, lt=1)
    reference_effects_inr: list[float] = [1.0, 2.0]


class Acceptance(_Frozen):
    min_positive_fold_fraction: float = Field(0.6, ge=0, le=1)
    conservative_scenario: str = "conservative"


class Protocol(_Frozen):
    protocol_version: str
    walk_forward: WalkForwardRule
    cost_scenarios: list[CostScenario]
    sensitivity: SensitivityPlan
    regimes: RegimeRules
    comparison_baselines: ComparisonBaselines
    statistics: StatisticsPlan
    acceptance: Acceptance

    @model_validator(mode="after")
    def _check(self) -> Protocol:
        names = [c.name for c in self.cost_scenarios]
        if "base" not in names or len(set(names)) != len(names):
            raise ValueError("cost_scenarios need unique names including 'base'")
        if self.acceptance.conservative_scenario not in names:
            raise ValueError("acceptance.conservative_scenario is not a cost scenario")
        return self


def load_protocol(path: Path) -> tuple[Protocol, str, str]:
    """(protocol, raw text, sha256 of the raw text)."""
    text = Path(path).read_text(encoding="utf-8")
    return protocol_from_text(text)


def protocol_from_text(text: str) -> tuple[Protocol, str, str]:
    protocol = Protocol.model_validate(yaml.safe_load(text))
    return protocol, text, hashlib.sha256(text.encode()).hexdigest()
