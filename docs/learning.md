# Learning and continuous-improvement pipeline (Phase 13)

The pipeline learns from recommendations, paper results and reviews so that
weaknesses can be found and changes proposed. It **never changes the active
system**. It does not update model weights, prompts, thresholds, settings or
the strategy. It has no broker access and no order path. The language model is
not used here: every metric and proposal is computed deterministically from
records. More data or more AI reasoning is not assumed to improve trading.

## 1. Audit (what existed) and data flow

Existing pieces reused:

- the agent's immutable recommendations and append-only outcomes (Phase 10);
- the integrated AI paper arm's database (Phase 12), which holds the
  recommendations, signals, orders and positions of the same signals;
- `compare.simulate` (the Phase 4 candle rules, used for trade paths);
- the Phase 11 evaluation ledger, used for test evidence.

Before this phase there was no learning dataset, review engine or proposal
workflow.

```
agent_recommendations (immutable) ──ingest──> learn_records (decision-time only, hashed, immutable)
                                                   │
market data (after the decision) ──outcomes──> learn_outcomes (H15m/H30m/H60m/EOD, TRADE_PATH,
paper DB (signals/orders/positions)                 PAPER_EXECUTION; append-only, only once knowable)
                                                   │
                                     review ──> learn_reviews (process vs outcome; rules + human)
                                                   │
                                     analyse ──> aggregates, recurring errors, period change,
                                                 findings ──> proposals (PROPOSED)
                                                   │
                    human: test-plan → test-result (from a ledger run) → approve* → deploy* (recorded only)
```

## 2. Files

| File | Purpose |
|---|---|
| `app/learning/dataset.py` | Schema; ingestion with leakage refusal; horizon, trade-path and paper outcomes; corrections |
| `app/learning/review.py` | `review-rules-v1` post-trade classification; human annotations |
| `app/learning/analysis.py` | Aggregates, Wilson and bootstrap intervals, recurring errors, findings, proposal templates |
| `app/learning/proposals.py` | Proposal state machine, human confirmation, selection-bias guards, immutable history |
| `app/learning/report.py` | Reproducible review report (Markdown and JSON) |
| `app/learning/__main__.py` | CLI |
| `app/learning/views.py` | Dashboard view "Learning" (read-only) |
| `app/config.py`, `config/settings.example.yaml` | `learning:` section |
| `tests/test_learning.py` | 19 tests |

## 3. Schema and migration

A new, separate SQLite database (`learning.db_path`, default
`data/learning/learning.db`) is created on first use, with WAL and owner-only
permissions. No existing database is changed, so no migration is needed.

| Table | Contents | Mutability |
|---|---|---|
| `learn_sources` | source agent databases | insert only |
| `learn_records` | one per recommendation: IST and UTC timestamps, symbol, exchange, session, strategy, configuration, model and prompt versions, data snapshot time, freshness, decision-time features (screen, signal, score, entry/stop/target, quantity, costs, net reward:risk, risk result, indicators, relative-volume bucket), tool and model call IDs, structured AI output, CONSIDER / WAIT / NO_TRADE / UNAVAILABLE, rule compliance | immutable (hashed; triggers) |
| `learn_outcomes` | per record and kind: return, MFE/MAE, benchmark and excess return, hypothetical trade path (gross, net, costs, exit), paper execution (fill deviation, actual costs and P&L, unfilled or rejected) | append-only |
| `learn_corrections` | field, old, new, reason, who, when | append-only |
| `learn_reviews` | process grade and categories, outcome grade and category, evidence, rules version or human | append-only |
| `proposals`, `proposal_events`, `proposal_tests` | proposals (content frozen; status changes only through events), transitions, test plans and results | content and history immutable |

## 4. Learning workflow and approval process

**Review rules** (`review-rules-v1`). The process is judged only from
decision-time evidence; the outcome is graded separately. A loss is not a bad
decision by itself, and a win does not prove a sound process.

- Process categories: DATA_QUALITY, UNSUPPORTED_CLAIM,
  STRATEGY_RULE_VIOLATION, RISK_SIZING_ERROR.
- Outcome-informed categories: WEAK_BREAKOUT, EXECUTION_DEVIATION,
  COST_ESTIMATE_ERROR, NORMAL_UNCERTAINTY, INSUFFICIENT_EVIDENCE.
- INCORRECT_INTERPRETATION is added by a person (`annotate`).

**Analysis.**

- Each signal is counted once for performance.
- Every decision record counts for AI quality.
- Groups below `min_sample` (20) are never interpreted.
- Rates use Wilson intervals. Period changes use a session bootstrap.
- A finding is a **HYPOTHESIS** unless its sample and interval support it
  (**ESTABLISHED_PATTERN**). Even then it only justifies a test.

**Proposal workflow.**

```
PROPOSED → UNDER_TEST → APPROVED* → DEPLOYED* → ROLLED_BACK*
   └──────────┴────────────┴──→ REJECTED          (* = a person + reason + "--confirm '<ACTION> <id>'")
```

- `test-result` derives PASS, FAIL or INCONCLUSIVE from a **completed
  evaluation run in the Phase 11 ledger** against the proposal's
  predeclared acceptance criteria (default: EVIDENCE_OF_IMPROVEMENT and 0
  rule violations). Results cannot be typed in by hand.
- `approve` refuses without a passing test.
- `deploy` only records that a person released the change as a new version
  (for example `research-v4` with a new protocol version). It changes
  nothing.
- **Selection-bias guards.** At most `max_tests_per_validation_period` (5)
  proposals may use overlapping validation periods; after that, fresh data
  is required. The final evaluation set (`final_evaluation_start`) is
  single-use per proposal. Every test and every rejected proposal is kept.

## 5. Commands

```bash
python -m app.learning ingest [--source data/paper/ai_paper.db]
python -m app.learning outcomes [--data DIR]          # only outcomes whose window has passed
python -m app.learning review
python -m app.learning annotate --record LR-.. --category INCORRECT_INTERPRETATION --reason ".." --by NAME
python -m app.learning correct  --record LR-.. --field F --value V --reason ".." --by NAME
python -m app.learning analyse                        # findings, proposals, report
python -m app.learning proposals [--id P-..]
python -m app.learning test-plan   --id P-.. --from D --to D --dataset NAME --by NAME
python -m app.learning test-result --id P-.. --run AE-..
python -m app.learning approve  --id P-.. --by NAME --reason ".." --confirm "APPROVE P-.."
python -m app.learning deploy   --id P-.. --by NAME --reason ".." --released-as VERSION --confirm "DEPLOY P-.."
python -m app.learning reject   --id P-.. --by NAME --reason ".."
python -m app.learning rollback --id P-.. --by NAME --reason ".." --confirm "ROLLBACK P-.."
```

Daily, after the integrated session: run `ingest`, `outcomes`, `review`, then
`analyse` periodically. The dashboard view is "Learning".

## 6. Example (SYNTHETIC data, thresholds deliberately lowered)

This example comes from ten synthetic integrated sessions with the mock agent.
`min_recurring_count` was 1 and `recurring_rate` 0.01, so that a proposal would
appear. **It is not evidence.**

```
| WEAK_BREAKOUT | 1 | 3 | [0.0615, 0.7923] | True |
- HYPOTHESIS [recurring:WEAK_BREAKOUT] WEAK_BREAKOUT in 1 of 3 reviewed records ...
### P-20250315-001 - PROPOSED (HYPOTHESIS)
- Change: breakout confirmation filter (e.g. relative-volume threshold) - a NEW strategy
  version and frozen baseline (component strategy)
- Baseline strategy orb-long-v1 -> candidate strategy-candidate (to be built as a new version)
- Required tests: build the candidate as a new, separately versioned component; Phase 11
  replay on development and validation splits; prospective paper sessions on a fresh period
  (Phase 12); full regression suite and release gate
- Acceptance: required_conclusions ['EVIDENCE_OF_IMPROVEMENT'], max_rule_violations 0,
  fresh_period_required
- Limitations: correlation, not causation; sample size 3; hypothetical trade paths
```

With the default thresholds, the same synthetic data produce **no**
proposal: 1 weak breakout in 3 signals is below the recurrence rule.

On the real project (2026-10-20), `ingest` found 0 recommendations, because no
live session has run. `analyse` produced no finding and no proposal, and an
empty review report (`data/learning/reports/review_none_none.md`).

## 7. Tests (actual results)

- `tests/test_learning.py`: **19 passed**.
- Full suite and gate (`bash scripts/release_gate.sh`, 2026-10-20): **567 passed, 1
  skipped** (the opt-in live-LLM test). RELEASABLE. Frozen baseline verified, no code
  drift. Lint, type-check and dependency audit SKIPPED (tools not installed).

Covered:

- immutable records and append-only corrections;
- duplicates, and restart idempotence;
- delayed outcomes stay pending;
- missing data recorded as NO_DATA;
- future-leakage refusal;
- process vs outcome separation;
- human annotation;
- one count per signal, and small-sample flags;
- Wilson intervals;
- deduplicated proposals;
- failed or unapproved proposals blocked;
- person and confirmation required;
- active configuration unchanged by the whole workflow (file hashes);
- selection-bias limit and single-use final set;
- report reproducibility;
- no LLM, broker or config-write path;
- the dashboard view.

## 8. Known limitations and risks

- No real data yet: every learning output on the real project is empty.
- Trade paths are hypothetical (candle rules). Paper fills are separate
  but simulated too.
- Interpretation errors are not detected mechanically; human annotation
  is needed.
- The regime is a trend label from the equal-weight universe benchmark only.
  Volatility and liquidity regimes need the Phase 7 history.
- Proposal templates cover the defined categories only. A new kind of
  problem needs a new template, which is a code change.
- Phase 14 adds candidate experiments. Until then a proposal's test
  evidence is a Phase 11 or Phase 12 evaluation run of whatever system was
  evaluated, recorded against the proposal's period.

## 9. Phase 13 completion checklist

| Requirement | Status |
|---|---|
| Inspect existing implementation, reuse, document the plan | VERIFIED (§1) |
| Immutable decision dataset with decision-time features, IST and UTC, versions, AI output | VERIFIED (tests) |
| Outcomes over predefined horizons, trade path, paper execution, benchmark, MFE/MAE | VERIFIED (synthetic) |
| Corrections append-only; originals never overwritten | VERIFIED |
| No future information in decision-time data | VERIFIED (leakage refusal tests) |
| Structured post-trade analysis, process separate from outcome | VERIFIED |
| Aggregation by strategy, symbol, regime, setup, recommendation, model version, period | VERIFIED (regime: trend only) |
| Recurring errors, meaningful changes, sample-size gating, hypothesis vs finding | VERIFIED |
| Proposals with hypothesis, change, versions, tests, criteria, limitations | VERIFIED |
| Statuses PROPOSED, UNDER_TEST, REJECTED, APPROVED, DEPLOYED, ROLLED_BACK, with explicit human approval | VERIFIED |
| Safeguards: no automatic changes, no leakage, sample sizes, validation-reuse guard, final set, rejected proposals kept | VERIFIED |
| Reports with sample size and period beside metrics | VERIFIED |
| Restart recovery, report reproducibility | VERIFIED |
| Regression suite and security checks | VERIFIED (release gate) |
| Learning from real recommendations | BLOCKED: no live sessions (empty `config/universe.csv`) |
| Manual-only real orders preserved; no broker credentials | VERIFIED |
