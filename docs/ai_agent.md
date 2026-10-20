# AI research agent (Phase 10)

> **Advisory only.**
> - The agent cannot place, modify or cancel orders. It cannot change risk
>   limits, fees, strategy settings or past records.
> - A recommendation is not an order, and a CANDIDATE is not a trade. You
>   decide, and you place any real order manually.
> - No probability of success is produced or implied, and no profit is
>   promised. NO_TRADE is a valid, frequent outcome. Under the frozen ₹500
>   rules it is the expected one (see `docs/final_readiness_report.md`).

**Status on 2026-10-20:**
- The interfaces, tools, workflow, storage, reports, dashboard and
  comparison are implemented and tested with the deterministic mock.
- **Live LLM integration: PENDING.** `NSE_LLM_API_KEY` is not set, so
  `python -m app.agent check-llm` reports NOT CONFIGURED, and the opt-in
  live test was skipped.
- The Anthropic request format follows the Messages API and is verified
  against a fake HTTP session only.

## Architecture

```
calendar + market-data provider + paper DB  (existing, read-only)
        |
  tools.py   11 approved read-only tools (strict input models, limits, provenance, audit)
        |
  workflow.py  bounded cycle: session gate -> data gate -> scan -> shortlist ->
               evidence tools -> change check -> LLM tool loop -> post-validation ->
               deterministic status gates -> immutable records + report
        |                      \
  llm.py (AnthropicProvider | MockProvider)   risk/cost numbers: Phase 3 RiskCostEngine
        |
  store.py (append-only tables)  report.py (JSON/MD)  views.py (dashboard "AI research")
  review.py (post-session outcomes)  compare.py (A vs B)  scheduler.py (pre/intraday/post)
```

**What the model may do:** request approved tools within a budget, then
submit a structured qualitative assessment (`ResearchOutput`).

**What it can never do:**
- produce the numbers in a recommendation (price, quantity, costs, reward:risk);
- change a recommendation's status upward;
- approve a risk rejection;
- act on stale data.

## Providers and configuration

| Setting (`NSE_` env or YAML `llm:`) | Default | Meaning |
|---|---|---|
| `llm.provider` (`NSE_LLM__PROVIDER`) | `mock` | `mock` (offline, deterministic) or `anthropic` |
| `llm.model` (`NSE_LLM__MODEL`) | `claude-haiku-5-5` | small, inexpensive model; any Messages-API model ID |
| `llm.workspace_id` (`NSE_LLM__WORKSPACE_ID`) | none | only for API keys **not** scoped to a workspace: sent as the `anthropic-workspace-id` header (the API rejects such keys without it) |
| `llm.timeout_seconds` | 30 | per request |
| `llm.max_retries` | 2 | transient errors only (timeout, connection, 429, 5xx/529); `retry-after` honoured |
| `llm.max_output_tokens`, `llm.temperature` | 6000, null | temperature is sent only when set; newer models reject it |
| `llm.input_cost_per_mtok`, `llm.output_cost_per_mtok` | none | USD per million tokens. **Set these from the provider's current price list to track spend.** Cost is never guessed |
| **`NSE_LLM_API_KEY`** | — | secret, `.env` only (never YAML), redacted from logs |

To enable the real model:

```bash
# in .env (chmod 600):
NSE_LLM__PROVIDER=anthropic
NSE_LLM_API_KEY=<your Anthropic API key>
# verify with ONE tiny request (prints model, latency, tokens):
.venv/bin/python -m app.agent check-llm
```

**Expected cost.** A research run sends about 5–20k input tokens (the tool
evidence) and up to about 2k output tokens. A run happens at most once per
window: pre-session, every 3rd candle close during the session (skipped when
nothing changed), and post-session. That is roughly 25 model requests a day
at most, on a small model. Configure the prices to see actual spend, which is
recorded per request in `agent_model_calls`.

**Limitations:**
- LLM output can be wrong or inconsistent. That is why every claim must cite
  evidence and numbers are checked.
- No news source is integrated.
- Mock mode uses no language model at all. It is clearly labelled.

## Tools and permissions

| Tool | Reads |
|---|---|
| `get_market_status` | exchange calendar and session state |
| `get_data_health` | freshness of completed candles per universe symbol; provider and paper heartbeat |
| `get_screening_results` | existing `run_scan`: screen and signal status, score, reasons, strategy setup |
| `get_candles` | completed 5m/1d candles only, up to the evaluation time (≤ `agent.max_candle_rows`) |
| `get_indicators` | opening range, VWAP, EMA, relative volume, rule results |
| `check_risk_and_costs` | Phase 3 engine on the strategy setup: whole-share quantity, costs, net R:R, approve or reject |
| `estimate_costs` | hypothetical round trip on the configured tariff (bounded inputs) |
| `get_portfolio_risk` | simulated paper portfolio and configured limits |
| `get_reports_summary` | latest paper report totals, evaluation and robustness status |
| `get_news` | **UNAVAILABLE.** No approved, source-attributed news provider; nothing is fabricated |
| `get_past_recommendations` | earlier recommendations (append-only store) |

**Restrictions:**
- Inputs: pydantic models with `extra="forbid"`; symbols restricted to the
  configured universe; row and day caps.
- Budgets: per-run tool-call, model-request and runtime limits.
- Unknown tools are rejected.
- **There are no** shell, file-path, network (other than the market-data
  provider) or broker tools. Tests parse the agent code to make sure it
  never imports `subprocess`, `socket`, `shutil`, `growwapi` or the Groww
  adapter, and that only `llm.py` uses `requests`.
- Tool text is wrapped as **UNTRUSTED DATA**. Instruction-like content
  ("ignore previous instructions", "place an order", "disable risk") is
  flagged on the recommendation and has no effect.

## Workflow and schedule

```bash
.venv/bin/python -m app.agent run --window pre|intraday|post [--at 2026-10-21T09:41] [--data DIR]
.venv/bin/python -m app.agent schedule      # today's windows on the NSE calendar (IST)
.venv/bin/python -m app.agent status [--date D]
.venv/bin/python -m app.agent report --run AR-...
```

**Windows:**
- **pre:** 08:45, before the open. Results are `RESEARCH_ONLY`; nothing
  about the session is assumed known.
- **intraday:** every 3rd candle close plus the grace period, using
  completed candles only.
  - Data is rechecked each time; no fresh symbol means NO_TRADE without a
    model call.
  - When inputs are unchanged since the last run, the run is
    `SKIPPED_NO_CHANGE` (no model call).
- **post:** 15:45, review only.
  - Appends `EXPIRED`, `PRICE_PATH` (hypothetical, same execution rules)
    and `PAPER_OUTCOME` records.
  - Then a structured AI review that separates process quality from
    profitability. It records observations only and never tunes the
    strategy.

**Rules:**
- Holidays, weekends and special sessions are skipped.
- One run per (date, window, slot). A crashed run is marked ABANDONED and
  retried as a new attempt.
- Missed windows are never replayed with hindsight, except the post-session
  review.
- One scheduler instance at a time (`agent.lock_file`).

**Limits:** `agent.max_candidates` (3), `max_tool_calls` (40),
`max_model_requests` (6), `max_runtime_seconds` (180).

## Recommendation schema (`agent-rec-v1`, `app/agent/schema.py`)

| Group | Fields |
|---|---|
| Identity | `recommendation_id`, `run_id`, `created_at`, `market_date`, `window`, `symbol`, `exchange` |
| Versions | `model`, `prompt_version`, `strategy_version`, `config_version`, `fee_schedule_id` |
| Deterministic (from tools) | `screen_status`, `signal_status`, `signal_id`, `score`, `entry_condition`, `entry_reference`, `stop`, `target`, `proposal_basis`, `quantity`, `estimated_costs_at_target`, `estimated_spread_slippage`, `planned_net_loss_at_stop`, `net_reward_risk`, `risk_status`, `risk_reasons`, `data_sources[]` (tool call, source, mode, as-of), `data_fresh` |
| AI (labelled) | `ai_status`, `ai_assessment` (observed facts, supporting/contradicting evidence with `basis` and `evidence_ids`, alternative explanations, evidence strength, entry and invalidation conditions, risk/cost assessment, missing information, recommendation, rationale), `ai_market_context` |
| Checks | `unsupported_claims`, `untrusted_content_flags`, `model_call_ids`, `tool_call_ids` |
| Status | `status`, `status_reasons`, `valid_until` |

**Statuses and who decides them (deterministic gates):**

| Status | When |
|---|---|
| `CANDIDATE` | screen PASS, valid ORB setup, risk engine `QUALIFIED_FOR_REVIEW`, fresh data, **and** the AI said `consider` |
| `WATCH` | the AI said wait; or the setup is forming; or the AI was unavailable or invalid while the deterministic checks passed |
| `REJECTED_BY_RISK` | the risk/cost engine rejected it, whatever the AI said |
| `NO_TRADE` | stale data, the AI abstained, the signal is no longer valid, or nothing qualifies (a run-level record) |
| `RESEARCH_ONLY` | pre-session, or no actionable setup |
| `EXPIRED` | **appended outcome only**; the original is never rewritten |

**Immutability:**
- SQLite triggers abort any UPDATE or DELETE on `agent_recommendations` and
  `agent_outcomes`.
- Each record stores its canonical JSON with a SHA-256, which the report
  re-verifies.

## Evidence rules

- Claims are typed `verified_fact`, `deterministic_calculation` or
  `interpretation`. The first two must cite successful tool calls (`T#`).
- **Post-validation flags as `unsupported_claims`:**
  - citations of missing or failed calls;
  - any number in AI text that appears in no tool output (small counts
    excepted);
  - probability or guarantee language.
- Flags are shown in reports and on the dashboard, and counted in the
  comparison.
- The schema has no probability or confidence fields. Any later
  probabilities must come from a separately validated, calibrated model,
  never from the LLM.

## Reports and dashboard

- **Report files:** each run writes
  `data/agent/reports/<date>/<run_id>.json` and `.md`, built only from the
  persisted records, so they can be regenerated with
  `python -m app.agent report --run ...`.
- **Report contents:**
  - a NO TRADE banner when nothing qualifies;
  - deterministic numbers and AI commentary, clearly labelled;
  - outcomes;
  - the tool-call audit;
  - model calls with latency, tokens and cost.
- **Dashboard:** `streamlit run app/dashboard.py`, then the **AI research**
  view. It shows the same records, with every tool input and output
  inspectable.

## Does the AI add value? (`python -m app.agent compare --from D --to D`)

The comparison uses the same runs, data and evidence.

| Arm | Definition |
|---|---|
| A (deterministic baseline) | every intraday record that passed all deterministic checks |
| B (AI-assisted) | the `CANDIDATE` records |

B is a subset of A by construction, and violations are counted (expected
0). Both arms are simulated with the Phase 4 candle execution rules: entry
at the first candle after the recommendation, exit at stop, target or end of
day, the risk engine's quantity, and Phase 3 costs.

**Reported:**
- signals and simulated trades per arm, net P&L, and B − A;
- abstention rate;
- rejection reasons, status and AI-status counts;
- unsupported claims;
- model requests and errors, mean latency, cost;
- per-session counts.

Small samples are labelled insufficient. The baseline stays unchanged, and
nothing is tuned from the result.

**Current evidence: none.** There are no live research runs yet. Under the
frozen ₹500 rules, A is expected to stay empty: the risk engine rejects every
setup. With no eligible signals, AI selection cannot add measurable value.
That will be reported, not hidden.

## Tests

```bash
.venv/bin/python -m pytest tests/test_agent.py -q -o addopts=""              # deterministic, offline
LLM_LIVE_TEST=1 .venv/bin/python -m pytest tests/integration -q -o addopts="" # live, opt-in (needs key)
```

`tests/test_agent.py` (28 tests) covers:
- schema validation, request format, usage and cost accounting;
- timeouts and rate limits (bounded, `retry-after`), and no retry on auth
  or bad request;
- missing configuration;
- determinism, and numbers coming from the engines;
- the ₹500 rejection even when the AI says consider; AI downgrade-only;
- stale or missing data abstaining without a model call;
- unsupported or contradictory claims, and failed-tool citations;
- malformed output; provider failure with an alert;
- duplicate runs and recommendations, and abandoning a crashed run;
- immutability and appended outcomes;
- report reproducibility;
- tool and model-request limits; tool input and output validation with no
  look-ahead;
- untrusted content;
- no shell, filesystem or broker capability (AST check);
- agent runs never change settings or paper records;
- zero order-endpoint calls through the Groww adapter;
- the schedule respecting the calendar with no hindsight replay;
- the A/B comparison and the dashboard view.
