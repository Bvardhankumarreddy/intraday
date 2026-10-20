# Phase 12: Integration and evaluation report

Date: 2026-10-20. Every figure below was observed in this repository. No
metric, test result or observation session is invented.

## 1. Architecture and reused components

| Need | Reused component (unchanged unless noted) |
|---|---|
| Session loop, lock, holidays, retries, restart, graceful stop | `app/paper/scheduler.py` `SessionScheduler`, `replay_session` |
| Screening, ORB strategy, ranking | `app/scan`, `app/screening`, `app/strategies` (frozen) |
| Sizing, costs, risk limits | `app/risk`, `app/costs` (frozen; Groww schedule `groww-nse-eq-intraday-2026-10`, verified 2026-10-19) |
| Paper execution, accounting, persistence | `app/paper/engine.py`, `app/paper/store.py`, one instance per arm. **Only change:** an optional `signal_gate` hook, off by default, which can only block an order and fails closed. |
| Data quality, sessions, signal audit, reconciliation | `app/evaluation` (Phase 6), one recorder per arm |
| AI research | `app/agent` (Phase 10), unchanged in this phase |
| Comparison protocol, ledger, statistics | `app/aieval` (Phase 11). Added `evaluate_integrated`. |
| Alerts, health | `app/ops` |

New: `app/integrated/` (`snapshot.py`, `engine.py`, `report.py`, `views.py`,
`__main__.py`). The frozen baseline's decision-path code is untouched; the
gate verifies "no code drift".

## 2. Implementation summary

- **One controlled workflow per step.** It runs a data snapshot, then the
  baseline arm, then the agent, then the gated AI-assisted arm; see
  `docs/integrated_trading.md`.
- **Separate portfolios.** Both arms run on the same data, costs, fills and
  risk engine, and are stored in separate databases.
- **The AI gate.** It allows a risk-approved signal only with a CANDIDATE
  recommendation persisted at or before the decision. It reads immutable
  records only.
- **Failure isolation.** AI provider and AI-arm failures never stop the
  baseline. The fallback policy is documented and tested.
- **Daily integrated report.** It covers reliability, market context,
  candidates with [FACT], [CALC] and [AI] labels, the financial assessment,
  decisions, per-arm paper results, incidents and reconciliation. It is
  reproducible from records.
- **Cumulative comparison.** It counts only sessions COMPLETED in both arms
  and uses the Phase 11 protocol: paired bootstrap, random-veto benchmark,
  decision quality, model cost and the predeclared decision rule.
- **Protocol freeze.** `python -m app.integrated start` records the
  protocol, baseline, configuration, AI versions and data mode, and refuses
  on AI-system drift.
- **Dashboard view "Integrated".**

### Defect found and fixed during integration

The first version of `StepSnapshot` always exposed `health()`. For a provider
without health reporting it returned `None`, and the paper engine then
crashed. It now delegates the method only when the wrapped provider has it
(`test_stale_data_blocks_both_arms_and_the_agent`). A test of mine also wrote
synthetic evaluation files into `data/evaluation`. The repository's guard test
caught it, and the four folders it created today were removed; the 9
pre-existing folders are untouched.

## 3. Tests (actual)

| Command | Result |
|---|---|
| `python -m pytest tests/test_integrated.py -q` | 27 passed |
| `bash scripts/release_gate.sh` (full suite) | **548 passed, 1 skipped** (the opt-in live-LLM test). RELEASABLE. Frozen baseline verified, no code drift. Lint, type-check and dependency audit SKIPPED (tools not installed). |

`tests/test_integrated.py` covers:

- end to end with deterministic fixtures;
- an AI veto affecting the AI arm only;
- snapshot consistency;
- LLM timeout and malformed output;
- model-request and tool-call limits;
- unauthorized tool calls;
- AI-arm failure isolation;
- stale data;
- session boundaries and holidays;
- duplicate orders and recommendations;
- restart with open positions in both arms;
- immutable recommendations;
- the gate ignoring later records (no future leakage);
- ₹500 risk rejection;
- ₹500 whole shares and cost infeasibility;
- cost reconciliation, and costs recorded once;
- unfilled orders;
- portfolio reconciliation;
- daily-report reproducibility;
- outdated or missing fee schedule;
- baseline-vs-AI cumulative metrics;
- empty-sample PENDING;
- separate databases;
- no order endpoint;
- the dashboard view.

**Live model integration check** (provider `anthropic`, model
`claude-haiku-5-5`, `max_output_tokens` 6000, synthetic fixture,
temporary databases):

- The session completed in both arms. There were 8 model requests, 0 errors
  and 0 unsupported claims, with 64,385 input and 7,226 output tokens and a
  mean latency of 4.1 s.
- The model answered `wait_for_confirmation` on the risk-approved setup. The
  gate blocked arm B (`NOT_CONFIRMED_BY_AI`). Arm A took the trade (synthetic
  net ₹15.69).
- This is a machinery check, not evidence.

## 4. Prospective observation sessions completed

**0 of the 20–30 required.** No integrated evaluation has been started, and
no live session has run. The reason is that `config/universe.csv` lists 0
symbols, and `python -m app.ops health` is UNHEALTHY for that reason.
`python -m app.integrated cumulative` returns PENDING_DATA ("no active Phase 6
evaluation in both arms").

## 5. Baseline and AI-assisted results

Not available: there is no actual observation. Results from synthetic
fixtures are not reported as evidence.

## 6. Transaction costs and ₹500 feasibility

In the integrated workflow, at ₹500 the risk engine sized **6 whole shares** of
the synthetic setup (entry about ₹71). It then rejected the trade
(REJECTED_COSTS): gross at target ₹7.20, total costs ₹12.80, net ₹−5.60. No
paper order was created in either arm, and cash stayed at ₹500. This matches
the Phase 9 finding that ₹500 rejects typical setups. A veto-only AI cannot
change that.

Costs are recorded once per round trip and reconcile with the Phase 3
calculator (tested). The fee schedule is `groww-nse-eq-intraday-2026-10`,
verified 2026-10-19. Its age is flagged after
`evaluation.max_fee_schedule_age_days`.

## 7. AI factual accuracy and rule compliance

These come from synthetic runs only (Phases 11 and 12):

- 0 rule violations;
- unsupported claims are flagged and kept;
- no probability claims;
- the real model was conservative (it said wait on every qualified synthetic
  setup).

Actual-session accuracy is pending observations.

## 8. Data-quality, execution and recovery incidents

None in actual operation, because nothing has run. In tests:

- stale data blocks both arms;
- a gap below the stop leaves the entry order REJECTED (unfilled, not a trade);
- restart recovery records RECOVERY events and reproduces the uninterrupted
  results;
- AI-arm crashes stop only the AI arm and mark its session INCOMPLETE.

## 9. Known limitations and unresolved risks

- The empty universe blocks every live session (critical for evaluation, not
  for safety).
- No verified news source.
- No bid/ask in historical data.
- The two portfolios diverge after a veto (attributed in the report).
- Only the top `agent.max_candidates` setups are reviewed.
- Model cost in INR is not computed until model prices and
  `ai_eval.usd_inr_rate` are configured.
- The live Groww feed has never been observed during market hours with this
  integration.
- Lint, type and dependency-audit tools are not installed.
- Each integrated session costs about 64k input tokens of model use in the
  synthetic check; live universes will cost more.

## 10. Readiness classification

**Inconclusive.**

- The integration and safety checks pass: 548 tests, an AI that can only
  veto, risk controls independent of the AI, failure isolation, records that
  reconcile, and reproducible reports.
- No actual observation session exists: 0 of 20–30. Nothing can be said about
  usefulness or value.
- The strategy remains unproven, and ₹500 rejects typical setups on costs.
- No critical integration, accounting, data-quality, risk-control or
  execution-safety defect is open, so the system is not "Not ready". Without
  observations it is not "Ready for continued manual-use evaluation" either.

The intended next state is a continued manual-use evaluation once sessions are
recorded. This classification does not authorise real-money trading.

## 11. Operating instructions

See `docs/integrated_trading.md` (start, run, report, cumulative, replay,
dashboard; prerequisites; fallback policy).

## 12. Real orders

**No real order was placed, modified or cancelled.** The integration code has
no broker import, and no order, modify or cancel call (tested). Both arms are
simulated paper portfolios, and `execution_mode` can only be `paper`.
