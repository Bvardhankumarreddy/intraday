"""Application configuration.

Settings come from three places, in increasing order of precedence:

1. Defaults defined in this module.
2. A YAML file (``config/settings.yaml``, or the path in ``NSE_CONFIG_FILE``).
3. Environment variables / ``.env``, prefixed ``NSE_`` with ``__`` for nesting,
   e.g. ``NSE_RISK__RISK_PCT=0.25``.

Secrets (API keys, tokens) are only ever read from the environment and are
stored as ``SecretStr`` so they are never printed or logged in plain text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, time
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config" / "settings.yaml"


class DataProviderName(StrEnum):
    MOCK = "mock"
    HISTORICAL = "historical"
    GROWW = "groww"  # live market data; needs a Groww Trading API subscription


class _Section(BaseModel):
    """Configuration section: unknown keys are an error, so a typo (e.g.
    ``max_daily_los``) fails at startup instead of silently using a default."""

    model_config = ConfigDict(extra="forbid")


class SpecialSession(_Section):
    """An exchange-announced session outside the normal calendar, e.g. Muhurat."""

    date: date
    open: time
    close: time
    name: str

    @model_validator(mode="after")
    def _check(self) -> SpecialSession:
        if self.open >= self.close:
            raise ValueError("special session open must be before close")
        return self


class MarketConfig(_Section):
    exchange: str = "NSE"
    timezone: str = "Asia/Kolkata"
    pre_open_start: time = time(9, 0)
    session_open: time = time(9, 15)
    session_close: time = time(15, 30)
    # Path to a YAML list of exchange holidays. Must be filled from NSE's
    # official holiday circular; the app warns when it is missing or empty.
    holidays_file: Path = PROJECT_ROOT / "config" / "nse_holidays.yaml"
    # Announced special sessions (date, open, close). They count as trading
    # days, but the strategy only runs in regular sessions.
    special_sessions: list[SpecialSession] = []

    @model_validator(mode="after")
    def _check_session(self) -> MarketConfig:
        if not self.pre_open_start <= self.session_open < self.session_close:
            raise ValueError("require pre_open_start <= session_open < session_close")
        return self


class CapitalConfig(_Section):
    initial_capital: float = Field(500.0, gt=0, description="Trading capital in INR")


class RiskConfig(_Section):
    risk_pct: float = Field(0.5, gt=0, le=10, description="Risk per trade, % of capital")
    max_daily_loss: float = Field(15.0, gt=0, description="Max daily loss in INR")
    max_trades_per_day: int = Field(2, ge=1)
    max_concurrent_positions: int = Field(1, ge=1)
    # Sum of planned net losses at the stop across open hypothetical positions.
    max_aggregate_risk: float = Field(5.0, gt=0, description="INR")
    min_cash_reserve: float = Field(0.0, ge=0, description="INR kept unused")
    max_position_value: float | None = Field(None, gt=0, description="INR per position")
    # When True (conservative), the planned net loss at the stop *including all
    # round-trip costs* must fit within the risk budget, not just price risk.
    include_costs_in_risk: bool = True
    # Leverage/margin is not supported. The flag exists so the assumption is
    # explicit; setting it True is rejected.
    allow_margin: bool = False

    @model_validator(mode="after")
    def _no_margin(self) -> RiskConfig:
        if self.allow_margin:
            raise ValueError("margin/leverage is not supported by this research tool")
        return self


class ExecutionCostConfig(_Section):
    """Execution-cost assumptions, separate from fees."""

    # Used only when no valid live/simulated quote spread is available; the
    # result is then labelled as an estimate.
    assumed_spread_pct: float = Field(0.10, ge=0, description="% of price, full spread")
    # Extra adverse movement per fill beyond paying the half-spread.
    slippage_pct_per_side: float = Field(0.05, ge=0)


class CostReviewConfig(_Section):
    # Reject when net reward/risk after all costs is below this.
    min_net_reward_risk: float = Field(1.5, gt=0)
    # The report also shows the exit price needed for this net profit (INR).
    desired_net_profit: float = Field(5.0, gt=0)
    # Fee rates are only verified from their effective dates. For research on
    # earlier dates, set True to apply the current rates anyway; every result
    # is then labelled as using retroactive rates.
    apply_current_rates_to_past_dates: bool = False


class ScreeningConfig(_Section):
    min_price: float = Field(50.0, gt=0)
    max_price: float = Field(100.0, gt=0)
    # 50 lakh INR = 5,000,000. Average over `liquidity_lookback_sessions`.
    min_avg_traded_value: float = Field(5_000_000.0, ge=0)
    liquidity_lookback_sessions: int = Field(20, ge=1)
    max_spread_pct: float = Field(0.10, gt=0, description="Max bid-ask spread, % of mid")
    # When True, a missing spread makes the stock INSUFFICIENT_DATA. Set False
    # only for historical research where no quotes exist; the spread is then
    # reported as unavailable and scores zero. It is never assumed to be zero.
    require_spread: bool = True
    allowed_series: list[str] = ["EQ"]

    @model_validator(mode="after")
    def _check_price_band(self) -> ScreeningConfig:
        if self.min_price >= self.max_price:
            raise ValueError("min_price must be below max_price")
        return self


class StopReference(StrEnum):
    SIGNAL_CANDLE_LOW = "signal_candle_low"
    OPENING_RANGE_LOW = "opening_range_low"
    OPENING_RANGE_MID = "opening_range_mid"


class RelativeVolumeMode(StrEnum):
    # Volume from the session open to the candle's end vs the same window in
    # prior sessions.
    CUMULATIVE = "cumulative"
    # The single candle's volume vs the same time slot in prior sessions.
    CANDLE = "candle"


class StrategyConfig(_Section):
    """Opening-range breakout parameters. These are hypotheses to test."""

    candle_interval: str = "5m"
    opening_range_minutes: int = Field(15, ge=1)
    relative_volume_threshold: float = Field(1.5, gt=0)
    relative_volume_mode: RelativeVolumeMode = RelativeVolumeMode.CUMULATIVE
    relative_volume_lookback_sessions: int = Field(10, ge=1)
    relative_volume_min_sessions: int = Field(5, ge=1)
    use_ema_filter: bool = False
    ema_period: int = Field(20, ge=2)
    # Reject entries that close too far above the opening-range high.
    max_extension_pct: float = Field(1.0, gt=0)
    stop_reference: StopReference = StopReference.SIGNAL_CANDLE_LOW
    # Risk per share as % of entry must fall within these bounds.
    min_risk_pct: float = Field(0.2, gt=0)
    max_risk_pct: float = Field(3.0, gt=0)
    min_reward_risk: float = Field(2.0, gt=0)
    # A signal is only actionable for this long after its candle closes.
    signal_valid_minutes: int = Field(10, ge=1)
    # No new signals from candles that close after this time.
    last_signal_time: time = time(14, 30)
    # Latest time a hypothetical position may stay open. Must be earlier than
    # the broker's own intraday auto square-off time; verify against Groww.
    eod_exit_time: time = time(15, 10)

    @model_validator(mode="after")
    def _check(self) -> StrategyConfig:
        if self.min_risk_pct >= self.max_risk_pct:
            raise ValueError("min_risk_pct must be below max_risk_pct")
        if self.relative_volume_min_sessions > self.relative_volume_lookback_sessions:
            raise ValueError("relative_volume_min_sessions exceeds lookback")
        if self.last_signal_time >= self.eod_exit_time:
            raise ValueError("last_signal_time must be before eod_exit_time")
        return self


class ScoringConfig(_Section):
    """Weights and normalisation for the 0-100 candidate score.

    The score orders candidates that already passed every mandatory rule. It
    is not a probability of profit. See docs/strategy.md.
    """

    weight_liquidity: float = Field(25, ge=0)
    weight_relative_volume: float = Field(25, ge=0)
    weight_breakout_quality: float = Field(20, ge=0)
    weight_trend: float = Field(15, ge=0)
    weight_spread: float = Field(15, ge=0)
    # Average traded value at this multiple of the screening minimum scores 1.
    liquidity_full_score_multiple: float = Field(20.0, gt=1)
    # Relative volume at this level scores 1.
    relative_volume_full_score: float = Field(3.0, gt=0)
    # Close this far above VWAP / EMA (in %) scores 1.
    trend_full_score_pct: float = Field(1.0, gt=0)

    @model_validator(mode="after")
    def _check_weights(self) -> ScoringConfig:
        total = (
            self.weight_liquidity
            + self.weight_relative_volume
            + self.weight_breakout_quality
            + self.weight_trend
            + self.weight_spread
        )
        if abs(total - 100) > 1e-9:
            raise ValueError(f"scoring weights must sum to 100, got {total}")
        return self


class BrokerConfig(_Section):
    # Selects config/fees/<name>_nse_equity_intraday.yaml unless
    # fee_schedule_file is given. "illustrative" uses a labelled example model.
    name: str = "groww"
    fee_schedule_file: Path | None = None


class DataConfig(_Section):
    provider: DataProviderName = DataProviderName.MOCK
    historical_dir: Path = PROJECT_ROOT / "data" / "historical"
    mock_seed: int = 42
    # Quotes/candles older than this are rejected as stale in live/delayed modes.
    max_staleness_seconds: int = Field(120, ge=1)
    # Price moves between consecutive candles above this are flagged.
    max_candle_jump_pct: float = Field(20.0, gt=0)
    # Instruments to watch with a live provider: CSV with symbol,name columns.
    universe_file: Path = PROJECT_ROOT / "config" / "universe.csv"
    # Live provider call policy.
    retry_max_attempts: int = Field(4, ge=1)
    retry_base_delay_seconds: float = Field(0.5, ge=0)
    retry_max_delay_seconds: float = Field(8.0, ge=0)
    min_seconds_between_calls: float = Field(0.15, ge=0)  # Groww live data: 10/s
    # Every provider HTTP request times out after this (the Groww SDK's own
    # default is no timeout at all).
    request_timeout_seconds: float = Field(10.0, gt=0, le=120)
    # Backoff delays get +/- this fraction of random jitter.
    retry_jitter_fraction: float = Field(0.25, ge=0, le=1)
    # Circuit breaker: after this many consecutive failed calls, calls fail
    # fast for the cooldown, then one probe call is allowed (half-open).
    circuit_failure_threshold: int = Field(5, ge=1)
    circuit_cooldown_seconds: float = Field(60.0, ge=1)


class BacktestConfig(_Section):
    """Candle-only execution assumptions and dataset policy for backtests."""

    # Skip the entry if the next candle opens more than this % above the
    # signal's entry reference (the setup is no longer the one evaluated).
    max_entry_gap_pct: float = Field(0.5, ge=0)
    # Optional time stop; None = hold until stop, target or eod_exit_time.
    max_holding_minutes: int | None = Field(None, ge=1)
    # Sensitivity option: the target counts as filled only when the high trades
    # this % beyond it (a touch does not guarantee a fill). 0 = candle-v1.
    target_trade_through_pct: float = Field(0.0, ge=0)
    # Sessions with more missing candles than this are excluded entirely.
    max_missing_candles_per_session: int = Field(3, ge=0)
    # Fewer closed trades than this is labelled insufficient evidence.
    min_trades_for_evidence: int = Field(30, ge=1)
    output_dir: Path = PROJECT_ROOT / "data" / "backtests"


class PaperConfig(_Section):
    db_path: Path = PROJECT_ROOT / "data" / "paper" / "paper.db"  # live paper portfolio
    # Replays never write to the live paper portfolio.
    replay_db_path: Path = PROJECT_ROOT / "data" / "paper" / "replay.db"
    lock_file: Path = PROJECT_ROOT / "data" / "paper" / "paper.lock"
    # Seconds after a candle closes before the engine steps (lets data arrive).
    grace_seconds: int = Field(5, ge=0)
    # Consecutive failed steps before the scheduler stops with an error.
    max_consecutive_failures: int = Field(5, ge=1)


class EvaluationConfig(_Section):
    """Phase 6 live paper-trading evaluation."""

    # A session counts as COMPLETED only if at least this share of its
    # scheduled monitoring steps ran and of its expected candles arrived.
    min_step_coverage_pct: float = Field(90.0, ge=0, le=100)
    min_data_coverage_pct: float = Field(95.0, ge=0, le=100)
    # Flag the broker tariff when its verification is older than this.
    max_fee_schedule_age_days: int = Field(90, ge=1)
    # Initial observation window (not a statistical requirement).
    observation_target_sessions: int = Field(20, ge=1)
    output_dir: Path = PROJECT_ROOT / "data" / "evaluation"


class RobustnessConfig(_Section):
    """Phase 7 strategy robustness validation (paths and run options only;
    the evaluation protocol itself is predeclared in ``protocol_file`` and
    copied into each frozen baseline)."""

    protocol_file: Path = PROJECT_ROOT / "config" / "robustness" / "protocol.yaml"
    baselines_dir: Path = PROJECT_ROOT / "config" / "baselines"
    output_dir: Path = PROJECT_ROOT / "data" / "robustness"
    # Parallel processes for sensitivity runs (results do not depend on it).
    workers: int = Field(1, ge=1)


class OpsConfig(_Section):
    """Phase 8 operations: health, alerts, backups, retention."""

    backups_dir: Path = PROJECT_ROOT / "data" / "backups"
    keep_backups: int = Field(14, ge=1)  # older backups may be pruned (never trading records)
    log_dir: Path = PROJECT_ROOT / "data" / "logs"
    log_retention_days: int = Field(30, ge=1)
    reconciliation_dir: Path = PROJECT_ROOT / "data" / "ops" / "reconciliation"
    # Health is DEGRADED below this much free disk space at the data folder.
    min_free_disk_mb: int = Field(500, ge=1)
    # During a session, health is UNHEALTHY if no step ran for this long.
    max_step_gap_minutes: int = Field(15, ge=1)
    # The same alert is re-notified at most once per this many minutes.
    alert_repeat_minutes: int = Field(30, ge=1)


class LLMProviderName(StrEnum):
    MOCK = "mock"  # deterministic, offline; used by tests and by default
    ANTHROPIC = "anthropic"  # Anthropic Messages API (needs NSE_LLM_API_KEY)


class LLMConfig(_Section):
    """Phase 10 language-model provider. The key is NSE_LLM_API_KEY (env only)."""

    provider: LLMProviderName = LLMProviderName.MOCK
    # Small, inexpensive model by default; any Messages-API model id works.
    model: str = "claude-haiku-5-5"
    base_url: str = "https://api.anthropic.com"
    api_version: str = "2023-06-01"
    # Needed only for API keys that are not scoped to a workspace: sent as the
    # anthropic-workspace-id header. An identifier, not a secret.
    workspace_id: str | None = Field(None, min_length=1, max_length=100)
    timeout_seconds: float = Field(30.0, gt=0, le=300)
    max_retries: int = Field(2, ge=0, le=5)  # transient errors only (timeouts, 429, 5xx)
    # The full research schema for several symbols needs several thousand tokens; a
    # response cut off at this limit is rejected, never accepted (Phase 11 audit).
    max_output_tokens: int = Field(6000, ge=256, le=16000)
    # None = not sent. Newer models reject the parameter (HTTP 400); set it only
    # for a model that accepts it.
    temperature: float | None = Field(None, ge=0, le=1)
    # USD per million tokens. None = cost not computed (never guessed); set
    # these from the provider's current price list to track spend.
    input_cost_per_mtok: float | None = Field(None, ge=0)
    output_cost_per_mtok: float | None = Field(None, ge=0)


class AgentConfig(_Section):
    """Phase 10 AI research agent: bounded workflow limits and windows."""

    max_candidates: int = Field(3, ge=1, le=10)
    max_tool_calls: int = Field(40, ge=1, le=200)
    max_model_requests: int = Field(6, ge=1, le=20)
    max_runtime_seconds: float = Field(180.0, ge=5)
    max_candle_rows: int = Field(80, ge=5, le=400)
    max_history_days: int = Field(10, ge=1, le=30)
    pre_session_time: time = time(8, 45)
    post_session_time: time = time(15, 45)
    # Intraday research runs after every Nth candle close (and only calls the
    # model when the inputs changed since the previous run).
    intraday_every_n_candles: int = Field(3, ge=1)
    reports_dir: Path = PROJECT_ROOT / "data" / "agent" / "reports"
    lock_file: Path = PROJECT_ROOT / "data" / "agent" / "agent.lock"


class AIEvalConfig(_Section):
    """Phase 11 evaluation of the AI agent against the deterministic baseline.
    The comparison rules are predeclared in ``protocol_file``."""

    protocol_file: Path = PROJECT_ROOT / "config" / "ai_eval" / "protocol.yaml"
    output_dir: Path = PROJECT_ROOT / "data" / "ai_eval"
    ledger_db: Path = PROJECT_ROOT / "data" / "ai_eval" / "ledger.db"
    # Rupees per US dollar, to state model costs in INR. None = not converted
    # (never guessed); model prices themselves are llm.*_cost_per_mtok.
    usd_inr_rate: float | None = Field(None, gt=0)
    # Spending guard for historical replay: the run refuses before any model
    # call if more research runs than this would be needed.
    max_agent_runs: int = Field(300, ge=1)


class IntegratedConfig(_Section):
    """Phase 12: deterministic baseline and AI-assisted paper arm run together.
    The baseline uses ``paper.db_path``; the AI-assisted arm its own database."""

    ai_db_path: Path = PROJECT_ROOT / "data" / "paper" / "ai_paper.db"
    replay_ai_db_path: Path = PROJECT_ROOT / "data" / "paper" / "ai_replay.db"
    reports_dir: Path = PROJECT_ROOT / "data" / "integrated" / "reports"
    # The agent runs after every Nth candle close; 1 = it reviews each signal
    # before the AI arm can enter (it only calls the model when inputs changed).
    agent_every_n_candles: int = Field(1, ge=1)


class LearningConfig(_Section):
    """Phase 13: learning from recommendations and outcomes. Proposes changes
    only; never changes the active configuration."""

    db_path: Path = PROJECT_ROOT / "data" / "learning" / "learning.db"
    reports_dir: Path = PROJECT_ROOT / "data" / "learning" / "reports"
    # Outcome horizons after the decision (minutes), plus end of day.
    horizons_minutes: list[int] = Field(default_factory=lambda: [15, 30, 60])
    # Groups with fewer observations are shown but never interpreted.
    min_sample: int = Field(20, ge=2)
    # A recurring error needs at least this many cases AND a Wilson lower bound
    # of its rate at or above ``recurring_rate``.
    min_recurring_count: int = Field(3, ge=1)
    recurring_rate: float = Field(0.2, gt=0, lt=1)
    confidence: float = Field(0.95, gt=0.5, lt=1)
    # Selection-bias guard: distinct proposals that may be tested on the same
    # validation period before it is treated as used up (fresh data required).
    max_tests_per_validation_period: int = Field(5, ge=1)
    # Start of the untouched final evaluation set (None = not designated). A
    # proposal may be tested on it at most once.
    final_evaluation_start: date | None = None
    # Paper fill vs entry reference beyond the assumed slippage plus this (%).
    execution_tolerance_pct: float = Field(0.1, ge=0)
    # Estimated vs actual round-trip costs beyond this (INR).
    cost_tolerance_inr: float = Field(0.5, ge=0)


class ExperimentsConfig(_Section):
    """Phase 14: candidate experiments, version registry and controlled promotion."""

    registry_db: Path = PROJECT_ROOT / "data" / "experiments" / "registry.db"
    output_dir: Path = PROJECT_ROOT / "data" / "experiments"
    acceptance_file: Path = PROJECT_ROOT / "config" / "experiments" / "acceptance.yaml"
    shadow_db_path: Path = PROJECT_ROOT / "data" / "paper" / "shadow.db"
    canary_db_path: Path = PROJECT_ROOT / "data" / "paper" / "canary.db"
    # Spending guard for validation runs that call a real model.
    max_agent_runs: int = Field(600, ge=1)


class MonitoringConfig(_Section):
    """Phase 15: monitoring, drift detection, alerts and safe degraded modes.
    Thresholds are configurable; the rationale is in docs/monitoring.md."""

    db_path: Path = PROJECT_ROOT / "data" / "monitoring" / "monitoring.db"
    # Local alert interface: every notification is appended here (JSON lines).
    alert_outbox: Path = PROJECT_ROOT / "data" / "monitoring" / "alerts.jsonl"
    alert_cooldown_minutes: int = Field(30, ge=1)
    snapshot_retention_days: int = Field(30, ge=1)
    # -- market data
    max_invalid_records: int = Field(0, ge=0)        # duplicates / out-of-order / bad OHLC
    max_missing_candle_pct: float = Field(5.0, ge=0)
    max_quote_age_seconds: int = Field(120, ge=1)
    max_spread_multiple: float = Field(3.0, gt=1)    # x screening.max_spread_pct
    # -- AI agent (over the last ``ai_window_runs`` research runs that called the model)
    ai_window_runs: int = Field(30, ge=5)
    ai_failure_rate_warning: float = Field(0.2, gt=0, le=1)
    ai_failure_rate_critical: float = Field(0.5, gt=0, le=1)
    ai_invalid_rate_warning: float = Field(0.2, gt=0, le=1)
    ai_unsupported_rate_warning: float = Field(0.3, gt=0, le=1)
    ai_latency_p95_warning_seconds: float = Field(30.0, gt=0)
    ai_daily_cost_usd_warning: float | None = Field(None, gt=0)
    ai_min_runs_for_rates: int = Field(10, ge=1)
    # NO_TRADE share or recommendation volume vs the trailing reference
    behaviour_change_abs: float = Field(0.4, gt=0, le=1)
    volume_change_factor: float = Field(3.0, gt=1)
    # Circuit breaker for model calls in integrated sessions.
    llm_breaker_failures: int = Field(3, ge=1)
    llm_breaker_cooldown_minutes: int = Field(15, ge=1)
    # -- performance drift
    min_trades_for_drift: int = Field(30, ge=5)
    recent_window_sessions: int = Field(20, ge=5)
    drift_confidence: float = Field(0.95, gt=0.5, lt=1)
    data_drift_min_samples: int = Field(30, ge=10)
    data_drift_p_value: float = Field(0.01, gt=0, lt=0.5)
    losing_streak_info: int = Field(6, ge=2)
    # -- recovery
    recovery_passes: int = Field(3, ge=1)            # clean evaluations before NORMAL
    max_backup_age_hours: int = Field(48, ge=1)
    override_max_minutes: int = Field(120, ge=5)


class WhatsAppConfig(_Section):
    """Meta WhatsApp Cloud API notifications (research alerts, never orders).
    Excluded from ``Settings.version()``: delivery cannot affect a decision.
    The access token is the secret NSE_WHATSAPP_TOKEN."""

    enabled: bool = False
    api_version: str = Field("v23.0", pattern=r"^v\d+\.\d+$")
    base_url: str = "https://graph.facebook.com"
    phone_number_id: str | None = Field(None, pattern=r"^\d{5,20}$")   # Meta sender id
    # Recipient(s) in international format without "+", e.g. 9198xxxxxxxx. Only your
    # own number: sending buy/sell calls to others is investment advice (SEBI).
    recipients: list[str] = Field(default_factory=list)
    # Business-initiated messages need an APPROVED template: one body variable {{1}}.
    template_name: str = "nse_alert"
    template_language: str = "en"
    timeout_seconds: float = Field(10.0, gt=0, le=60)
    max_retries: int = Field(2, ge=0, le=5)
    # What to send.
    send_signals: bool = True            # AI-assisted arm CANDIDATE recommendations
    send_fills: bool = True              # paper entries filled (both arms)
    send_exits: bool = True              # paper exits with net P&L (both arms)
    send_alerts: bool = True             # monitoring alerts and recoveries
    min_alert_severity: Literal["INFO", "WARNING", "CRITICAL"] = "WARNING"
    send_daily_summary: bool = True      # 16:00 summary (post-session job)
    max_messages_per_day: int = Field(60, ge=1)   # cost and spam guard

    @field_validator("recipients")
    @classmethod
    def _numbers(cls, v: list[str]) -> list[str]:
        for n in v:
            if not re.fullmatch(r"\d{10,15}", n):
                raise ValueError(f"recipient {n!r} must be digits in international format "
                                 "without '+', e.g. 919876543210")
        if len(v) > 3:
            raise ValueError("at most 3 recipients (your own numbers only)")
        return v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NSE_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    market: MarketConfig = MarketConfig()
    capital: CapitalConfig = CapitalConfig()
    risk: RiskConfig = RiskConfig()
    screening: ScreeningConfig = ScreeningConfig()
    strategy: StrategyConfig = StrategyConfig()
    scoring: ScoringConfig = ScoringConfig()
    execution: ExecutionCostConfig = ExecutionCostConfig()
    cost_review: CostReviewConfig = CostReviewConfig()
    backtest: BacktestConfig = BacktestConfig()
    paper: PaperConfig = PaperConfig()
    evaluation: EvaluationConfig = EvaluationConfig()
    robustness: RobustnessConfig = RobustnessConfig()
    ops: OpsConfig = OpsConfig()
    llm: LLMConfig = LLMConfig()
    agent: AgentConfig = AgentConfig()
    ai_eval: AIEvalConfig = AIEvalConfig()
    integrated: IntegratedConfig = IntegratedConfig()
    learning: LearningConfig = LearningConfig()
    experiments: ExperimentsConfig = ExperimentsConfig()
    monitoring: MonitoringConfig = MonitoringConfig()
    whatsapp: WhatsAppConfig = WhatsAppConfig()
    # The ONLY supported execution mode. Simulated (paper) orders only; real
    # orders are placed manually by the user outside this application. Any
    # other value (e.g. "live") is rejected at startup. There is no live
    # execution adapter in this codebase.
    execution_mode: Literal["paper"] = "paper"
    broker: BrokerConfig = BrokerConfig()
    data: DataConfig = DataConfig()
    log_level: str = "INFO"

    # Secrets: environment only. Not used until a live provider is added.
    broker_api_key: SecretStr | None = None  # Groww: the API key, or the TOTP token
    broker_api_secret: SecretStr | None = None  # Groww approval-key secret (daily approval)
    broker_totp_secret: SecretStr | None = None  # Groww TOTP secret (no daily approval)
    llm_api_key: SecretStr | None = None
    whatsapp_token: SecretStr | None = None  # Meta WhatsApp Cloud API access token

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Environment beats the YAML file, which is passed in as init kwargs.
        return env_settings, dotenv_settings, init_settings

    @model_validator(mode="after")
    def _check_cross_fields(self) -> Settings:
        if not self.market.session_open < self.strategy.eod_exit_time <= self.market.session_close:
            raise ValueError("strategy.eod_exit_time must fall inside the trading session")
        return self

    def version(self) -> str:
        """Short hash of every non-secret setting, recorded on each output so
        results can be traced to the configuration that produced them."""
        data = self.model_dump(
            mode="json", exclude={"broker_api_key", "broker_api_secret", "broker_totp_secret",
                                  "llm_api_key", "whatsapp_token",
                                  # Notification delivery cannot affect a decision; excluded
                                  # so adding or changing it never re-versions an evaluation.
                                  "whatsapp"}
        )
        # Paths inside the project are hashed relative to it, so moving the
        # whole project (e.g. to a server) keeps the version. Phase 9 fix: the
        # absolute path used to change the version, which would have made every
        # relocated evaluation session non-primary.
        digest = hashlib.sha256(json.dumps(_portable(data), sort_keys=True).encode()).hexdigest()
        return digest[:12]


def _portable(obj: Any) -> Any:
    root = str(PROJECT_ROOT)
    if isinstance(obj, dict):
        return {k: _portable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_portable(v) for v in obj]
    if isinstance(obj, str) and (obj == root or obj.startswith(root + os.sep)):
        return "<PROJECT_ROOT>" + obj[len(root):]
    return obj


class _ExplicitSettings(Settings):
    """Settings built from the given values only: no environment variables,
    no .env, no YAML file."""

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        return (init_settings,)


def settings_from_values(values: dict[str, Any]) -> Settings:
    """Build Settings from explicit values, ignoring NSE_* environment
    overrides. Use this for variants and frozen configurations, where an
    environment variable must never silently replace the intended value."""
    return _ExplicitSettings(**values)


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        content = yaml.safe_load(fh) or {}
    if not isinstance(content, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    for secret in ("broker_api_key", "broker_api_secret", "broker_totp_secret",
                   "llm_api_key", "whatsapp_token"):
        if secret in content:
            raise ValueError(f"{secret} must be set via environment, not {path}")
    return content


def load_settings(config_file: str | Path | None = None) -> Settings:
    """Load settings from YAML (if present) with environment overrides."""
    path = Path(config_file or os.environ.get("NSE_CONFIG_FILE") or DEFAULT_CONFIG_FILE)
    if config_file is not None and not path.exists():
        raise FileNotFoundError(path)
    file_values = _read_yaml(path) if path.exists() else {}
    # Always the project's own .env, whatever the working directory
    # (NSE_ENV_FILE overrides it; tests point it elsewhere).
    env_file = os.environ.get("NSE_ENV_FILE") or str(PROJECT_ROOT / ".env")
    settings = Settings(_env_file=env_file, **file_values)
    # Exact secret values are redacted from every log line from now on.
    from app.utils.logging import register_settings_secrets

    register_settings_secrets(settings)
    return settings
