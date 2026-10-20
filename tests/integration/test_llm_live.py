"""LIVE language-model integration test (opt-in; costs a few cents at most).

Skipped unless LLM_LIVE_TEST=1. It reads NSE_LLM_API_KEY (and optional
NSE_LLM__MODEL) from the project's .env, makes ONE minimal tool-use request
through AnthropicProvider, and checks the structured reply and usage
accounting. Market data, paper records and brokers are not touched.

    LLM_LIVE_TEST=1 .venv/bin/python -m pytest tests/integration -q -o addopts=""

The default unit suite never runs this (no paid API, no network).
"""

import os

import pytest

from app.config import PROJECT_ROOT, Settings

LIVE = os.environ.get("LLM_LIVE_TEST") == "1"


@pytest.mark.skipif(not LIVE, reason="set LLM_LIVE_TEST=1 to run the live LLM test")
def test_live_anthropic_structured_tool_call():
    from app.agent.llm import AnthropicProvider

    settings = Settings(_env_file=str(PROJECT_ROOT / ".env"))  # explicit opt-in to real .env
    if settings.llm_api_key is None:
        pytest.skip("NSE_LLM_API_KEY not set in .env")
    llm = AnthropicProvider(settings)
    schema = {"type": "object", "properties": {"status": {"type": "string", "enum": ["ok"]}},
              "required": ["status"], "additionalProperties": False}
    resp = llm.chat("Call the tool with status ok.", [{"role": "user", "content": "check"}],
                    [{"name": "report_status", "description": "report", "input_schema": schema}],
                    {"type": "tool", "name": "report_status"})
    assert any(u.name == "report_status" and u.input == {"status": "ok"}
               for u in resp.tool_uses)
    assert resp.input_tokens and resp.output_tokens and resp.latency_seconds > 0
