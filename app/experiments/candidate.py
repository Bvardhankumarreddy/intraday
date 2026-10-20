"""Candidate specifications and AI-system version components (Phase 14).

A candidate spec (YAML) can change:
- the prompt: a text file plus a NEW version label;
- the LLM model and its configuration (``llm`` overrides);
- which research tools the model may request (``tools``);
- agent limits (``agent`` overrides);
- deterministic settings for research (``settings``: strategy, screening,
  scoring, risk, execution, cost_review).

Mandatory risk controls can never be weakened. ``weakened_controls`` lists any
override that would loosen them, and such an experiment is INVALID. The
loosening rules are: a higher risk per trade, daily loss, aggregate risk,
trades per day or concurrent positions; a lower minimum net reward:risk;
dropping costs from the risk calculation; a lower assumed spread or slippage;
skipping the spread screen in live trading; or applying unverified fee rates.

A version's ``components`` fully define the AI system. With the same data,
the same components give the same deterministic arm, and the model calls are
recorded.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.config import PROJECT_ROOT, settings_from_values

SETTING_SECTIONS = {"strategy", "screening", "scoring", "risk", "execution", "cost_review"}
DETERMINISTIC = {"strategy", "screening", "scoring", "risk", "execution", "cost_review"}


class PromptSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=3, max_length=60)
    file: str


class CandidateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=3, max_length=80)
    hypothesis: str = Field(min_length=10)
    reason: str = Field(min_length=5)
    context: str = "paper-ai"
    prompt: PromptSpec | None = None
    llm: dict = Field(default_factory=dict)
    tools: list[str] | None = None
    agent: dict = Field(default_factory=dict)
    settings: dict = Field(default_factory=dict)


def load_spec(path: Path) -> CandidateSpec:
    return CandidateSpec.model_validate(yaml.safe_load(Path(path).read_text()))


def current_components(settings) -> dict:
    """Components of the AI system currently built into the code and settings."""
    from app.agent.prompts import PROMPT_VERSION, SYSTEM
    from app.agent.tools import TOOLS
    from app.aieval.protocol import agent_code_sha

    return {"prompt": {"version": PROMPT_VERSION, "text": SYSTEM,
                       "sha256": hashlib.sha256(SYSTEM.encode()).hexdigest()},
            "llm": {"provider": settings.llm.provider.value, "model": settings.llm.model,
                    "max_output_tokens": settings.llm.max_output_tokens,
                    "temperature": settings.llm.temperature},
            "tools": sorted(TOOLS),
            "agent": settings.agent.model_dump(mode="json", include={
                "max_candidates", "max_tool_calls", "max_model_requests",
                "max_runtime_seconds", "intraday_every_n_candles"}),
            "settings_overrides": {}, "agent_code_sha256": agent_code_sha()}


def candidate_components(spec: CandidateSpec, base: dict) -> dict:
    from app.agent.tools import TOOLS

    comp = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    if spec.prompt is not None:
        p = Path(spec.prompt.file)
        text = (p if p.is_absolute() else PROJECT_ROOT / p).read_text()
        if spec.prompt.version == base["prompt"]["version"] and \
                hashlib.sha256(text.encode()).hexdigest() != base["prompt"]["sha256"]:
            raise ValueError("a changed prompt needs a new version label")
        comp["prompt"] = {"version": spec.prompt.version, "text": text,
                          "sha256": hashlib.sha256(text.encode()).hexdigest()}
    if spec.llm:
        comp["llm"] = {**base["llm"], **spec.llm}
    if spec.tools is not None:
        unknown = set(spec.tools) - set(TOOLS)
        if unknown:
            raise ValueError(f"unknown tools: {sorted(unknown)}")
        comp["tools"] = sorted(spec.tools)
    if spec.agent:
        comp["agent"] = {**base["agent"], **spec.agent}
    bad = set(spec.settings) - SETTING_SECTIONS
    if bad:
        raise ValueError(f"settings overrides allowed only for {sorted(SETTING_SECTIONS)}; got "
                         f"{sorted(bad)}")
    comp["settings_overrides"] = spec.settings
    return comp


def changes_deterministic_strategy(components: dict) -> bool:
    return bool(set(components.get("settings_overrides", {})) & DETERMINISTIC)


def apply_components(settings, components: dict):
    """Settings with the version's model, agent and settings overrides applied."""
    data = settings.model_dump()
    data["llm"] = {**data["llm"], **{k: v for k, v in components["llm"].items()
                                      if k in data["llm"]}}
    data["agent"] = {**data["agent"], **components["agent"]}
    for section, values in components.get("settings_overrides", {}).items():
        data[section] = {**data[section], **values}
    new = settings_from_values(data)
    return new.model_copy(update={"llm_api_key": settings.llm_api_key,
                                  "broker_api_key": settings.broker_api_key,
                                  "broker_api_secret": settings.broker_api_secret,
                                  "broker_totp_secret": settings.broker_totp_secret})


def agent_kwargs(components: dict) -> dict:
    from app.agent.prompts import PROMPT_VERSION

    p = components["prompt"]
    custom = p["version"] != PROMPT_VERSION
    return {"system_prompt": p["text"] if custom else None,
            "prompt_version": p["version"] if custom else None,
            "allowed_tools": components["tools"]}


def weakened_controls(active, candidate) -> list[str]:
    """Overrides that would loosen a mandatory control (empty = none)."""
    a, c = active, candidate
    out = []

    def higher(sec, key):
        av, cv = getattr(getattr(a, sec), key), getattr(getattr(c, sec), key)
        if cv > av:
            out.append(f"{sec}.{key} raised from {av} to {cv}")

    def lower(sec, key):
        av, cv = getattr(getattr(a, sec), key), getattr(getattr(c, sec), key)
        if cv < av:
            out.append(f"{sec}.{key} lowered from {av} to {cv}")

    for k in ("risk_pct", "max_daily_loss", "max_aggregate_risk", "max_trades_per_day",
              "max_concurrent_positions"):
        higher("risk", k)
    lower("cost_review", "min_net_reward_risk")
    lower("execution", "assumed_spread_pct")
    lower("execution", "slippage_pct_per_side")
    if a.risk.include_costs_in_risk and not c.risk.include_costs_in_risk:
        out.append("risk.include_costs_in_risk switched off")
    if not a.cost_review.apply_current_rates_to_past_dates and \
            c.cost_review.apply_current_rates_to_past_dates:
        out.append("cost_review.apply_current_rates_to_past_dates switched on (unverified "
                   "fees)")
    if a.screening.require_spread and not c.screening.require_spread:
        out.append("screening.require_spread switched off")
    return out
