# Phase 10: audit and plan (AI research agent)

Written 2026-10-20, before any Phase 10 code.

## Audit

- **Tests:** 460 pass (baseline).
- **LLM integration:** none. Only an unused `llm_api_key` secret slot exists
  (`NSE_LLM_API_KEY`, `SecretStr`, already redacted from logs and excluded
  from config hashes).
- **Credentials:** `.env` contains **no** `NSE_LLM_*` entry, so a real-model
  integration test cannot run until a key is added.
- **Dependencies:** `requests` 2.34 is installed (via growwapi). No LLM SDK
  is installed. The Anthropic Messages API is called over HTTPS with
  `requests`, so no new package is needed.
- **Reusable components:**

  | Purpose | Module |
  |---|---|
  | Session state, holidays | `MarketCalendar` |
  | Screening, signals, ranking, scores | `run_scan` (no-risk view) |
  | Completed-candle data boundary | `build_snapshot` |
  | Opening range, VWAP, relative volume | strategy `evaluate` |
  | Sizing, costs, net reward:risk, review status | `RiskCostEngine` |
  | Cost estimates | `round_trip` |
  | Portfolio, risk state, daily reports, events | `PaperStore` |
  | Evaluation status | Phase 6 evaluation tables |
  | Strategy verdict | Phase 7 report `summary.json` |
  | Readiness status | Phase 9 `readiness_summary.json` |
  | Data freshness | heartbeat in `meta` |
  | Alerts | `app.ops.alerts` |
  | Overwrite-safe writes | `app.utils.fs.write_preserving` |
  | One process at a time | `instance_lock` |

- **Recommendation format today:** `ScanResult` / `Candidate` (Phase 2) and
  `RiskReport` (Phase 3). There are no AI recommendations.
- **Safest integration point:** a new advisory package `app/agent/` that
  only reads through the existing modules. It writes only its own tables
  and reports, and never touches the paper engine, risk limits or strategy
  configuration.

## Architecture

```
app/agent/
  schema.py     versioned recommendation + LLM output schemas (pydantic, extra=forbid)
  llm.py        provider abstraction: AnthropicProvider (HTTPS), MockProvider
                (deterministic); bounded retries, timeouts, usage/latency/cost
  tools.py      read-only tool registry: strict input models, symbol/range/budget
                limits, provenance, audit records; no shell/file/network/broker access
  prompts.py    versioned system prompt (role, constraints, data-is-not-instructions)
  store.py      append-only tables: runs (unique per window), tool calls, model
                calls, recommendations (immutable via SQLite triggers), outcomes
  workflow.py   bounded research cycle: data gate -> scan -> shortlist -> tools ->
                bounded LLM tool loop -> deterministic gating -> persist
  review.py     post-session outcomes (appended, never edits), expiry
  compare.py    A (deterministic baseline) vs B (AI-assisted) evaluation
  scheduler.py  pre / intraday / post windows on the exchange calendar
  report.py     JSON + Markdown daily report; dashboard view in views.py
  __main__.py   CLI: run, schedule, review, compare, report, check-provider
```

## Key rules

- **Numbers.** Every number in a recommendation (prices, quantity, costs,
  net reward:risk) is written by deterministic code from tool outputs. The
  model writes only qualitative fields, and each claim must cite tool-call
  evidence IDs. Numbers in model text that are absent from tool outputs are
  flagged as unsupported claims.
- **Status.** The status is decided by deterministic gates. The model can
  only make a status more conservative: it can never approve a risk-rejected
  or data-stale candidate.
- **Defaults.** The default provider is `mock`. A paid API is used only when
  `NSE_LLM__PROVIDER=anthropic` and `NSE_LLM_API_KEY` are set. The default
  model is the small `claude-haiku-5-5`. Cost is reported only if prices are
  configured; it is never guessed.
- **Live integration test.** It is opt-in (`NSE_LLM_LIVE_TEST=1`), and its
  run or skip status is reported.
