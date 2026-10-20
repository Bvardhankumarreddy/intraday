"""Language-model providers for the research agent (Phase 10).

``LLMProvider.chat`` sends one request in the tool-use conversation format
of the Anthropic Messages API and returns an ``LLMResponse`` (text blocks,
tool-use requests, usage, latency, model, request ID).

Providers:
- ``AnthropicProvider``: HTTPS to ``{base_url}/v1/messages`` with
  ``requests``. The key comes from ``NSE_LLM_API_KEY`` only (``SecretStr``,
  registered for log redaction). Retries are bounded (``llm.max_retries``)
  and apply ONLY to transient failures: timeouts, connection errors, HTTP
  429 (honouring ``retry-after``, capped) and 500/502/503/504/529.
  Authentication (401/403) and bad requests (400/404/413/422) are never
  retried. Error messages never include the key or request headers.
- ``MockProvider``: deterministic and offline (default; used by all unit
  tests). It follows the same tool-use protocol: it asks for a few research
  tools, then submits a schema-valid assessment built ONLY from the facts in
  the conversation. ``script=`` replays fixed responses or errors for tests.

Cost: computed only when ``llm.input_cost_per_mtok`` / ``output_cost_per_mtok``
are configured; otherwise ``None`` (unknown), never guessed.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import LLMProviderName, Settings


class LLMError(RuntimeError):
    kind = "provider_error"


class LLMConfigError(LLMError):
    kind = "config"


class LLMAuthError(LLMError):
    kind = "auth"


class LLMBadRequest(LLMError):
    kind = "bad_request"


class LLMRateLimited(LLMError):
    kind = "rate_limited"


class LLMTimeout(LLMError):
    kind = "timeout"


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    model: str
    text: list[str]
    tool_uses: list[ToolUse]
    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_seconds: float
    request_id: str | None
    raw: dict = field(default_factory=dict)
    attempts: int = 1


class LLMProvider(Protocol):
    name: str
    model: str

    def chat(self, system: str, messages: list[dict], tools: list[dict],
             tool_choice: dict) -> LLMResponse: ...


def cost_usd(settings: Settings, resp: LLMResponse | None) -> float | None:
    c = settings.llm
    if resp is None or c.input_cost_per_mtok is None or c.output_cost_per_mtok is None or \
            resp.input_tokens is None or resp.output_tokens is None:
        return None
    return round(resp.input_tokens / 1e6 * c.input_cost_per_mtok
                 + resp.output_tokens / 1e6 * c.output_cost_per_mtok, 6)


RETRY_STATUS = {429, 500, 502, 503, 504, 529}


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings, session=None,
                 sleep: Callable[[float], None] = time.sleep, rng: random.Random | None = None,
                 clock: Callable[[], float] = time.monotonic):
        if settings.llm_api_key is None or not settings.llm_api_key.get_secret_value():
            raise LLMConfigError("NSE_LLM_API_KEY is not set; the Anthropic provider cannot be "
                                 "used (set it in .env, or use NSE_LLM__PROVIDER=mock)")
        import requests

        self._requests = requests
        self.settings = settings
        self.cfg = settings.llm
        self.model = self.cfg.model
        self._key = settings.llm_api_key.get_secret_value()
        self.session = session or requests.Session()
        self.sleep, self.rng, self.clock = sleep, rng or random.Random(), clock

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
        return min(8.0, 0.5 * 2 ** (attempt - 1)) * (1 + 0.25 * self.rng.uniform(-1, 1))

    def chat(self, system: str, messages: list[dict], tools: list[dict],
             tool_choice: dict) -> LLMResponse:
        body = {"model": self.model, "max_tokens": self.cfg.max_output_tokens,
                "system": system, "messages": messages, "tools": tools,
                "tool_choice": tool_choice}
        if self.cfg.temperature is not None:
            body["temperature"] = self.cfg.temperature
        headers = {"x-api-key": self._key, "anthropic-version": self.cfg.api_version,
                   "content-type": "application/json"}
        if self.cfg.workspace_id:
            headers["anthropic-workspace-id"] = self.cfg.workspace_id
        url = self.cfg.base_url.rstrip("/") + "/v1/messages"
        attempts = self.cfg.max_retries + 1
        last: LLMError | None = None
        for attempt in range(1, attempts + 1):
            start = self.clock()
            try:
                resp = self.session.post(url, headers=headers, data=json.dumps(body),
                                         timeout=self.cfg.timeout_seconds)
            except self._requests.Timeout:
                last = LLMTimeout(f"LLM request timed out after {self.cfg.timeout_seconds}s")
            except self._requests.ConnectionError as exc:
                last = LLMError(f"LLM connection failed: {type(exc).__name__}")
            else:
                latency = self.clock() - start
                if resp.status_code == 200:
                    return self._parse(resp.json(), latency, resp.headers.get("request-id"),
                                       attempt)
                detail = _api_error(resp)
                if resp.status_code in (401, 403):
                    raise LLMAuthError(f"LLM authentication failed (HTTP {resp.status_code}): "
                                       f"{detail}; check NSE_LLM_API_KEY")
                if resp.status_code not in RETRY_STATUS:
                    raise LLMBadRequest(f"LLM request rejected (HTTP {resp.status_code}): "
                                        f"{detail}")
                last = (LLMRateLimited if resp.status_code == 429 else LLMError)(
                    f"LLM HTTP {resp.status_code}: {detail}")
                if attempt < attempts:
                    self.sleep(self._delay(attempt, resp.headers.get("retry-after")))
                continue
            if attempt < attempts:
                self.sleep(self._delay(attempt, None))
        raise last or LLMError("LLM request failed")

    def _parse(self, data: dict, latency: float, request_id: str | None,
               attempts: int) -> LLMResponse:
        text, uses = [], []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                uses.append(ToolUse(block["id"], block["name"], block.get("input") or {}))
        usage = data.get("usage") or {}
        return LLMResponse(model=data.get("model", self.model), text=text, tool_uses=uses,
                           stop_reason=data.get("stop_reason"),
                           input_tokens=usage.get("input_tokens"),
                           output_tokens=usage.get("output_tokens"), latency_seconds=latency,
                           request_id=request_id or data.get("id"), raw=data, attempts=attempts)


def _api_error(resp) -> str:
    try:
        err = resp.json().get("error", {})
        return f"{err.get('type', 'error')}: {str(err.get('message', ''))[:200]}"
    except ValueError:
        return f"non-JSON body ({len(resp.text)} bytes)"


class MockProvider:
    """Deterministic offline provider (default). Builds its answer only from
    the facts it was given; it never adds a number of its own."""

    name = "mock"
    model = "mock-deterministic-v1"

    def __init__(self, script: list | None = None):
        self.script = list(script) if script is not None else None
        self.requests: list[dict] = []

    def chat(self, system: str, messages: list[dict], tools: list[dict],
             tool_choice: dict) -> LLMResponse:
        self.requests.append({"messages": messages, "tool_choice": tool_choice,
                              "tools": [t["name"] for t in tools]})
        if self.script is not None:
            item = self.script.pop(0) if self.script else LLMError("mock script exhausted")
            if isinstance(item, Exception):
                raise item
            return item
        brief = json.loads(messages[0]["content"])
        if any(t["name"] == "submit_review" for t in tools):
            recs = brief.get("records", [])
            return self._resp([], [ToolUse("mock_review", "submit_review", {
                "process_observations": [{
                    "text": f"{r['symbol'] or 'run'}: status {r['status']}",
                    "basis": "verified_fact", "evidence_ids": [r["id"]]} for r in recs[:10]],
                "outcome_observations": [{
                    "text": f"{o['kind']} recorded", "basis": "verified_fact",
                    "evidence_ids": [o["id"]]} for r in recs for o in r["outcomes"]][:10],
                "process_vs_outcome": "deterministic mock review: outcomes are listed "
                                      "separately from the rules that produced each status",
                "data_issues": [], "observations_for_future_evaluation": []})], "tool_use")
        rounds = sum(1 for m in messages if m["role"] == "assistant")
        forced = tool_choice.get("type") == "tool"
        wanted = [c["symbol"] for c in brief.get("shortlist", []) if not c.get("candles_id")]
        if rounds == 0 and wanted and not forced and any(t["name"] == "get_candles"
                                                         for t in tools):
            uses = [ToolUse(f"mock_{i}", "get_candles", {"symbol": s, "interval": "5m",
                                                          "rows": 12})
                    for i, s in enumerate(wanted[:2])]
            return self._resp([], uses, "tool_use")
        return self._resp([], [ToolUse("mock_submit", "submit_research",
                                       _mock_assessment(brief, messages))], "tool_use")

    def _resp(self, text, uses, stop) -> LLMResponse:
        return LLMResponse(model=self.model, text=text, tool_uses=uses, stop_reason=stop,
                           input_tokens=0, output_tokens=0, latency_seconds=0.0,
                           request_id=None, raw={"mock": True,
                                                 "tool_uses": [u.__dict__ for u in uses]})


def _mock_assessment(brief: dict, messages: list[dict]) -> dict:
    """Qualitative assessment that cites the brief's evidence IDs. Number-free."""
    extra = [b["tool_use_id"] for m in messages if m["role"] == "user"
             and isinstance(m["content"], list) for b in m["content"]
             if b.get("type") == "tool_result" and not b.get("is_error")]
    candidates = []
    for c in brief.get("shortlist", []):
        ev = [x for x in (c.get("screen_id"), c.get("indicators_id"), c.get("risk_id"))
              if x]
        eligible = c.get("deterministic_eligible")
        risk = c.get("risk_status")
        if eligible:
            rec, why = "consider", ("all deterministic strategy and risk checks passed; the "
                                    "user must confirm price still holds above the stop")
        elif c.get("signal_status") in ("WATCH",):
            rec, why = "wait_for_confirmation", "the strategy setup has not completed yet"
        else:
            rec, why = "no_trade", (f"deterministic checks did not pass (risk status {risk})"
                                    if risk else "no valid strategy setup")
        candidates.append({
            "symbol": c["symbol"],
            "observed_facts": [{"text": f"screen status {c.get('screen_status')}, signal "
                                        f"status {c.get('signal_status')}",
                                "basis": "verified_fact", "evidence_ids": ev[:1]}],
            "supporting_evidence": ([{"text": "passed every deterministic check",
                                      "basis": "deterministic_calculation",
                                      "evidence_ids": ev}] if eligible else []),
            "contradicting_evidence": ([] if eligible else [{
                "text": "fails at least one deterministic requirement",
                "basis": "deterministic_calculation", "evidence_ids": ev}]),
            "alternative_explanations": ["volume and price moves may be unrelated to any "
                                         "company-specific cause"],
            "evidence_strength": "correlation_only",
            "entry_conditions": ["price holds above the strategy stop at review time",
                                 "data remains fresh"],
            "invalidation_conditions": ["price trades at or below the stop",
                                        "signal validity window passes"],
            "risk_cost_assessment": "see the deterministic risk and cost tool output",
            "missing_information": ["no verified news source is configured"]
            + (["supplementary candles reviewed"] if extra else []),
            "recommendation": rec, "rationale": why,
        })
    overall = "candidates_for_review" if any(c["recommendation"] == "consider"
                                             for c in candidates) else \
        ("watch_only" if candidates and any(c["recommendation"] == "wait_for_confirmation"
                                            for c in candidates) else "no_trade")
    return {"market_context": [{"text": "market context taken from the session and data-health "
                                        "tools", "basis": "verified_fact",
                                "evidence_ids": [x for x in (brief.get("market_status_id"),)
                                                 if x]}],
            "candidates": candidates, "source_disagreements": [], "overall": overall,
            "overall_rationale": "deterministic mock assessment (no language model used)"}


def build_llm(settings: Settings) -> LLMProvider:
    if settings.llm.provider is LLMProviderName.ANTHROPIC:
        return AnthropicProvider(settings)
    return MockProvider()


def response_record(resp: LLMResponse | None) -> dict[str, Any]:
    if resp is None:
        return {}
    return {"model": resp.model, "stop_reason": resp.stop_reason,
            "input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens,
            "latency_seconds": round(resp.latency_seconds, 3), "request_id": resp.request_id,
            "attempts": resp.attempts}
