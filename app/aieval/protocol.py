"""Predeclared AI-evaluation protocol and AI-system version pins (Phase 11).

The protocol (``config/ai_eval/protocol.yaml``) fixes the comparison rules
before any result exists. Every run stores its SHA-256. ``ai_versions``
identifies the AI system actually used: prompt, model, a hash of the tool
definitions and a hash of the agent's decision code. ``pin_mismatches``
compares those with the protocol's pins. A mismatch labels the run
AI_SYSTEM_DRIFT, and the historical holdout then refuses to run.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import PROJECT_ROOT


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AISystemPins(_Frozen):
    prompt_version: str
    model: str
    tool_schema_sha256: str
    agent_code_sha256: str


class Splits(_Frozen):
    development_fraction: float = Field(gt=0, lt=1)
    validation_fraction: float = Field(gt=0, lt=1)
    holdout_fraction: float = Field(gt=0, lt=1)

    @model_validator(mode="after")
    def _sum(self) -> Splits:
        total = self.development_fraction + self.validation_fraction + self.holdout_fraction
        if abs(total - 1) > 1e-9:
            raise ValueError(f"split fractions must sum to 1 (got {total})")
        return self


class EvidenceHierarchy(_Frozen):
    primary: str
    secondary: str


class Inclusion(_Frozen):
    prospective_session_status: list[str]
    exclude_reconciliation_flagged: bool


class MinimumEvidence(_Frozen):
    min_sessions: int = Field(ge=1)
    min_closed_trades_per_arm: int = Field(ge=1)
    min_discordant_signals: int = Field(ge=0)


class Statistics(_Frozen):
    bootstrap_resamples: int = Field(ge=100)
    confidence: float = Field(gt=0.5, lt=1)
    seed: int
    block_length: int | None = Field(None, ge=1)
    equivalence_margin_inr_per_session: float = Field(gt=0)


class DecisionRule(_Frozen):
    require_known_model_cost_for_improvement: bool = True
    require_selection_beyond_random_veto: bool = True


class AIEvalProtocol(_Frozen):
    protocol_version: str
    effective_date: date
    baseline_file: str
    ai_system: AISystemPins
    splits: Splits
    evidence_hierarchy: EvidenceHierarchy
    primary_metrics: list[str]
    secondary_metrics: list[str]
    inclusion: Inclusion
    handling: dict[str, str]
    minimum_evidence: MinimumEvidence
    statistics: Statistics
    decision_rule: DecisionRule

    def baseline_path(self) -> Path:
        p = Path(self.baseline_file)
        return p if p.is_absolute() else PROJECT_ROOT / p


def load_protocol(path: Path) -> tuple[AIEvalProtocol, str, str]:
    """(protocol, text, sha256 of the text)."""
    text = Path(path).read_text(encoding="utf-8")
    return (AIEvalProtocol.model_validate(yaml.safe_load(text)), text,
            hashlib.sha256(text.encode()).hexdigest())


# Files whose behaviour decides what the AI sees and what its output can do.
AGENT_CODE_FILES = ("app/agent/llm.py", "app/agent/prompts.py", "app/agent/schema.py",
                    "app/agent/tools.py", "app/agent/workflow.py")


def tool_schema_sha() -> str:
    from app.agent.tools import TOOLS

    defs = [{"name": t.name, "description": t.description,
             "input_schema": t.args.model_json_schema()} for t in TOOLS.values()]
    return hashlib.sha256(json.dumps(defs, sort_keys=True).encode()).hexdigest()


def agent_code_sha(root: Path = PROJECT_ROOT) -> str:
    digest = hashlib.sha256()
    for rel in AGENT_CODE_FILES:
        digest.update(rel.encode() + b"\0" + (root / rel).read_bytes() + b"\0")
    return digest.hexdigest()


def ai_versions(settings, llm) -> dict:
    from app.agent.prompts import PROMPT_VERSION, REVIEW_PROMPT_VERSION
    from app.backtesting.engine import STRATEGY_VERSION

    return {"prompt_version": PROMPT_VERSION, "review_prompt_version": REVIEW_PROMPT_VERSION,
            "llm_provider": llm.name, "model": llm.model,
            "max_output_tokens": settings.llm.max_output_tokens,
            "temperature": settings.llm.temperature,
            "tool_schema_sha256": tool_schema_sha(), "agent_code_sha256": agent_code_sha(),
            "strategy_version": STRATEGY_VERSION,
            "agent_limits": settings.agent.model_dump(
                mode="json", include={"max_candidates", "max_tool_calls", "max_model_requests",
                                      "max_runtime_seconds", "intraday_every_n_candles"})}


def pin_mismatches(protocol: AIEvalProtocol, versions: dict) -> list[str]:
    pins = protocol.ai_system.model_dump()
    return [f"{k}: protocol {v!r}, actual {versions.get(k)!r}" for k, v in pins.items()
            if versions.get(k) != v]
