# Final readiness report (Phase 9)

**Review date:** 2026-10-20 (IST).
**Repository:** `nse-intraday-agent/`.
**Machine-readable evidence:** `data/readiness/2026-10-20/`. Regenerate it
with `python -m app.readiness`.

> **No real orders were placed, modified or cancelled during this review or
> at any point in this project.** All execution is simulated (paper). Real
> orders are always placed manually by the user, outside this application.

## 1. Executive summary

**Primary classification: B. INCONCLUSIVE.**

| Dimension | Finding |
|---|---|
| Software completeness | **Verified.** All nine phases are implemented. 460 automated tests pass. The release gate is RELEASABLE, with lint, type and dependency-audit tools not installed and therefore reported SKIPPED, not passed. |
| Accounting and risk controls | **Verified on deterministic fixtures:** ledger invariants, fees once, no duplicates after retries or restarts, a daily-loss lock that persists, reconciliation that never alters records, and end-to-end parity between the paper engine and the backtester. |
| Paper-only boundary | **Verified** by code inspection and tests, against both a fake SDK and the real Groww SDK. Limits are listed in §9. |
| Operational reliability | **Unverified in real conditions.** Recovery and failure handling pass 24 failure-injection scenarios on fixtures. However, **zero live market-hours sessions have ever run**, and current health is **UNHEALTHY**: `config/universe.csv` lists no symbols, so no live session can start. |
| Strategy evidence | **None.** No historical data has been fetched. The Phase 7 verdict is `PENDING_DATA`. There are no backtests on real data and zero live paper sessions. **Profitability is unproven.** |
| ₹500 feasibility | **Not economically feasible under the frozen rules.** The existing cost and risk engine rejects all 30 representative setups (₹50–₹100, stops 0.2–3%). Even one share loses ₹3.17–₹9.04 at the stop, against a ₹2.50 risk budget. The system will therefore report NO TRADE on every signal. |

Why B rather than A or C:
- **Not A.** Important evidence is missing. The system has never run against
  a live market, and the strategy has no evidence at all.
- **Not C.** No critical software, accounting, risk-control or
  execution-safety defect remains. The blockers are a configuration gap
  (empty universe), missing evidence and an economic finding, and the
  software reports each of them correctly.

## 2. Audit results for Phases 1–8

Source: `data/readiness/2026-10-20/audit_matrix.csv`. Results are from
running the tests on 2026-10-20, not from the files existing.

| Phase | Capabilities | Tests run (passed / failed) | Result | Outstanding |
|---|---|---|---|---|
| 1 | Config, providers, validation, calendar, logging | 88 / 0 | PASS | Groww adapter never observed in market hours; holiday list covers 2026 only |
| 2 | Screening, indicators, ORB, ranking | 62 / 0 | PASS | Live universe empty |
| 3 | Groww costs, break-even, sizing, risk limits | 93 / 0 | PASS | Per-order paisa rounding is an assumption |
| 4 | Backtester, `candle-v1`, metrics, reports | 43 / 0 | PASS | Never run on real data; Phase 4 `walk_forward()` auto-selects parameters (not used by Phase 7) |
| 5 | Paper engine, persistence, scheduler, dashboard | 39 / 0 | PASS | No live session yet; local launchd agents are **no longer installed** (removed outside this review) |
| 6 | Live evaluation, data-quality ledger, audit, reconciliation | 29 / 0 | PASS | 0 evaluations recorded; 9 test-artifact folders in `data/evaluation/` |
| 7 | Robustness validation | 45 / 0 | PASS | Empirical verdict `PENDING_DATA` |
| 8 | Hardening, recovery, health, alerts, backups, gate | 53 / 0 | PASS | No notification provider; no git; scanners not installed |
| 9 | End-to-end integration | 8 / 0 | PASS | — |

**Defects found and fixed in this phase** (within the existing architecture):

1. **The config version depended on the install path.**
   `Settings.version()` hashed absolute paths, so relocating the project
   (the planned server move) would have changed the frozen evaluation
   version and made every session non-primary. Paths inside the project are
   now hashed relative to it. Test: `tests/test_config.py::test_version_is_portable_across_install_locations`.
   No evaluation existed yet, so no records were affected.
2. **Health said HEALTHY when no live session could run.** With the Groww
   provider and an empty universe, health reported HEALTHY. It is now a
   config FAIL (UNHEALTHY). Test: `tests/test_ops.py::test_health_fails_when_live_provider_has_no_universe`.

**Duplicate implementations (known, documented, deliberate):**
- The paper engine and the backtester each have their own loop, but share
  the same `execution.py` fill rules and the same cost engine. Parity is
  tested end to end (§3).
- Phase 4's `walk_forward()` and Phase 7's walk-forward co-exist. Only
  Phase 7's is used for evidence.

## 3. End-to-end architecture verification

`tests/test_e2e_integration.py` (8 tests, isolated databases, synthetic
data) runs one session through the whole pipeline:

market data → data-quality ledger → screening → signal → risk/sizing →
paper order → simulated fill → costs → accounting → persistence → reports
and dashboard.

| Check | Evidence | Result |
|---|---|---|
| Each stage uses the previous output | Signal quantity = order = fill = position (28); same `signal_id`; order reference = signal entry | PASS |
| Timestamps, timezones, symbols, session bounds | Every stored timestamp is +05:30. Signal ≤ entry < exit ≤ close. Symbols are consistent across tables | PASS |
| No premature decisions | The 09:35 candle is acted on at 09:40, its close. No order before the signal time | PASS |
| Rejected signals and no-trade decisions recorded | SECOND rejected `REJECTED_RISK_LIMIT` with reasons; audit rows `SIGNAL_REJECTED_RISK` and `NO_SETUP_YET` | PASS |
| No duplicates after a retry | Full session replayed again: same signals, orders, positions and cash | PASS |
| Fees exactly once | Costs have one buy and one sell part; total = buy + sell = `round_trip()` recomputation | PASS |
| Balances reconcile | cash = initial + net; equity = cash + positions on every snapshot; Phase 6 `reconcile` and Phase 8 portfolio reconciliation both clean | PASS |
| Paper/backtest parity | Same trade, prices, costs and net P&L (₹15.69) and the same rejection from `run_backtest` | PASS |
| Dashboard agrees with records | `views.portfolio_summary` and `positions_table` equal the database values | PASS |
| Reports reproducible | Stored report equals a rebuild from the records; evaluation daily report is deterministic | PASS |
| Errors become visible health states | A provider failing all day gives an INCOMPLETE session (coverage 0%), never a successful zero-trade day, plus the `data.blocked` alert and health UNHEALTHY | PASS |
| Missing fee configuration surfaced | Session preflight blocks it; health reports it | PASS |

## 4. Automated test results

Every command was executed on 2026-10-20. The results below are actual
outputs.

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest` | **460 passed**, 0 failed, 0 skipped |
| `scripts/release_gate.sh` (`python -m app.ops gate`) | **RELEASABLE**. See the gate breakdown below. |
| `python -m app.robustness verify` | OK: `orb-baseline-v1` verified, no code drift |
| `python -m app.ops health` | **UNHEALTHY** (exit 2): config FAIL, empty universe; all other checks PASS or SKIP |
| `python -m app.readiness` | Wrote `data/readiness/2026-10-20/` |

Gate breakdown (`data/readiness/2026-10-20/checks.csv`):

| Status | Checks |
|---|---|
| PASS (blocking) | compile; full tests; paper-only safety; recovery and reconciliation; `pip check`; secret scan (configured credential values searched in all files including logs and reports, none found); config; `.env` permissions; `.gitignore`; frozen baseline; DB migration on a copy |
| PASS (non-blocking) | lock file; docs |
| **SKIPPED** (non-blocking) | lint (ruff), type check (mypy), dependency audit (pip-audit). **The tools are not installed, so these checks were not run.** |

Coverage by category:

| Category | Covered by |
|---|---|
| Unit | Phases 1–4 |
| Integration | paper, evaluation, robustness |
| End to end | `test_e2e_integration.py`, `test_paper_only.py` (a full session through the Groww adapter) |
| Backtest reproducibility | Phase 4 byte-identical report test; Phase 7 reproducible-report test |
| Persistence, recovery, reconciliation | `test_failure_injection.py` (24), `test_ops.py` (21) |

## 5. Historical and out-of-sample strategy evidence

| Item | Status |
|---|---|
| Baseline identified | `config/baselines/orb-baseline-v1.json`, strategy `orb-long-v1`, fingerprint `b7852798…`, execution `candle-v1`, protocol `p7-v1` (predeclared) |
| Historical data | **Unavailable.** `data/historical/` does not exist; 0 backtest runs |
| Train / validation / test separation, holdout untouched | Implemented and tested on synthetic data. **No real-data run exists to check** |
| Costs, slippage, gross vs net, drawdown, sensitivity, regimes, uncertainty | Implemented and tested. **Unverified on market data** |
| Phase 7 report | `data/robustness/orb-baseline-v1/06e1812f7631/report.md`: **`PENDING_DATA`** (lists the missing data) |
| **Conclusion** | **Strategy profitability is UNPROVEN.** No evidence exists, positive or negative. No optimisation was run in this review. |

## 6. Live paper-trading evidence

Source: `data/paper/paper.db`, read-only.

| Item | Value |
|---|---|
| Evaluations | **0** |
| Scheduled, completed, incomplete or failed sessions | **0** |
| Signals, orders, fills | 0 |
| Gross, net and unrealized P&L; drawdown | **Unavailable** (no sessions) |
| Data-quality incidents, reconciliation failures, report failures, recoveries | 0 (nothing has run) |

Other databases and folders:
- `data/paper/replay.db` is empty. It would be replay evidence, never live.
- The 9 folders in `data/evaluation/` are **test artifacts**: memory
  provider, dated 2025-03-12, not primary. They are not evidence.

**Live paper-trading evidence: none.** The 20–30 session observation window
has not started. Even when complete, it would be an operational check, not
proof of profitability.

## 7. Transaction costs and ₹500 feasibility

**Tariff:** `groww-nse-eq-intraday-2026-10`, verified 2026-10-19 and
**re-verified 2026-10-20** against groww.in/pricing and Groww's
intraday-charges help page. Both match:

| Charge | Rate |
|---|---|
| Brokerage | lower of ₹20 or 0.1% per order; below ₹5, the lower of ₹5 or 2.5% of value |
| STT | 0.025% on sell |
| Stamp duty | 0.003% on buy |
| NSE exchange + IPFT | 0.00307% per side |
| SEBI | 0.0001% |
| GST | 18% on brokerage, exchange, SEBI and IPFT charges |

**Method:** every figure below is computed by the existing engine
(`RiskCostEngine`, `round_trip`, `break_even_exit_price`) under the frozen
settings:

| Setting | Value |
|---|---|
| Capital | ₹500 |
| Risk per trade | 0.5% (budget ₹2.50), costs included in risk |
| Minimum net reward:risk | 1.5 |
| Target | 2R |
| Assumed spread | 0.10% |
| Slippage | 0.05% per side |

The code is in `app/readiness/feasibility.py`. These are calculations for
hypothetical setups, not market results.

**Costs at the largest affordable whole-share position**
(`feasibility_costs.csv`):

| Entry ₹ | Max whole shares | Round-trip costs ₹ | Costs % of position | Break-even move |
|---|---|---|---|---|
| 50 | 10 | 12.99 | 2.60% | +2.62% |
| 70 | 7 | 12.97 | 2.65% | +2.66% |
| 100 | 5 | 12.99 | 2.60% | +2.61% |

**Engine verdict, 30 setups** (6 prices × 5 stop widths; `feasibility_setups.csv`):
- **0 qualified.**
- 28 `REJECTED_COSTS` (`costs_exceed_risk_budget`).
- 2 `REJECTED_RISK_LIMIT`.

Net loss at the stop for **one share**, which is already above the ₹2.50
budget in every case:

| Entry ₹ | 0.2% stop | 1% stop | 3% stop |
|---|---|---|---|
| 50 | ₹3.17 | ₹3.55 | ₹4.52 |
| 70 | ₹4.44 | ₹4.97 | ₹6.34 |
| 100 | ₹6.32 | ₹7.10 | ₹9.04 |

The cause is the ₹5 / 2.5% minimum brokerage per order (×2 orders, plus
18% GST) together with spread and slippage. A 2R target would need a
2.6–2.9% move just to break even at full size.

**Informational only — the configured capital is unchanged**
(`feasibility_scaling.csv`, ₹70 entry): net reward:risk computed by the
same engine, against the required 1.5.

| Stop | ₹10,000 | ₹50,000 | ₹2,00,000 |
|---|---|---|---|
| 0.5% | 0.54 | 0.65 | 0.94 |
| 1.0% | 0.85 | 1.04 | 1.31 |
| 1.5% | 0.97 | 1.28 | 1.47 |
| 2.0% | 1.04 | 1.43 | 1.56 |
| 3.0% | 1.10 | 1.59 | 1.65 |

- At these larger capitals, the fixed rupee limits sized for ₹500 (₹15
  daily loss, ₹5 aggregate risk) still bind, so every grid row is
  `REJECTED_RISK_LIMIT` as configured.
- Even ignoring those limits, stops of 1.5% or tighter never reach a net
  reward:risk of 1.5, even at ₹2 lakh. Percentage costs consume too much of
  a 2R target.

**Conclusion:** at ₹500 the frozen strategy is **economically impractical
after costs**. The infeasibility is not only about capital: the
cost-to-stop-width ratio also blocks tight-stop ORB setups at any size
under the frozen 2R / 1.5-net rules.

**Assumptions:**
- the spread and slippage percentages are assumptions;
- per-order paisa rounding is an assumption;
- the DP charge and the ₹50 auto square-off charge are excluded, because
  they don't apply to a normally closed intraday trade.

Any change to capital, target or the cost rule would be a **new strategy
version**, needing its own frozen baseline and a fresh evaluation. This
review changed none of them.

## 8. Risk-control and accounting verification

| Control | Evidence (executed tests) | Result |
|---|---|---|
| Whole shares; orders never exceed available cash; reserved vs available | `test_position_sizing.py`, `test_paper_engine.py`, Phase 8 reconciliation (`reserved_exceeds_cash`, exposure checks) | PASS |
| Daily loss lock persists across restarts | `test_failure_injection.py::test_19…` | PASS |
| Stale or invalid data blocks new signals | `test_paper_engine.py::test_stale_data_blocks_new_signals`, `test_failure_injection` 01/03/04, e2e | PASS |
| Missing fee configuration surfaced | e2e `test_missing_fee_configuration_is_surfaced` | PASS |
| Duplicate signals and order events prevented | failure-injection 07/08/09/11; e2e rerun | PASS |
| Session boundaries and holidays | `test_market_calendar.py`, scheduler tests | PASS |
| Open positions at session end | end-of-day exit at 15:10; missing data → FLAGGED → NEEDS_ATTENTION, never an assumed exit (`test_paper_engine.py`) | PASS |
| Failed provider → safe degraded state | circuit breaker, blocked signals, alerts, health UNHEALTHY | PASS |
| Reconciliation errors visible, records never altered | failure-injection 12c and 18 | PASS |
| No-trade is acceptable and recorded | e2e audit decisions; ₹500 `REJECTED_COSTS` tests | PASS |
| Incomplete observations labelled | e2e: provider outage gives an INCOMPLETE session | PASS |

## 9. Security and paper-only execution

**Verified:**
- **Only one module imports the SDK.** Only `app/data/groww_provider.py`
  imports `growwapi` (AST test).
- **Order methods are blocked.** The SDK is reachable only through an
  allowlist proxy, so order, holdings and positions methods raise
  `BlockedSdkCall`. This was **confirmed against the real Groww SDK** on
  2026-10-19/20, using read-only calls only.
- **No order attribute is accessed in `app/`.** No `place_order`,
  `modify_order` or `cancel_order` (AST test).
- **No order calls in a full session.** A complete paper session through
  the real adapter code, against a fake SDK that records order calls, made
  0 order calls, with a timeout on every request.
- **No live execution mode exists.** `execution_mode` accepts only `paper`,
  and the engine has no parameter through which an order client could be
  passed.
- **Secrets stay in `.env`** (mode 600, git-ignored). They are redacted
  from logs, including exact values. The secret scan found no credential
  value in any file, including logs and reports.

**Remaining limits:**
- **Order code still ships.** The growwapi package contains order code (one
  package for data and trading). Isolation relies on the module boundary
  and on the tests above, which a future code change could bypass. That is
  why they are blocking release-gate checks.
- **Supply chain.** Nothing here protects against a compromised SDK
  package, and no dependency vulnerability scan has been run (the tools are
  not installed).
- **The API key can trade.** The Groww credential can place orders from
  other software. Protect `.env`.
- **Dashboard.** It has no authentication. It is safe only on localhost or
  behind an authenticating proxy.

## 10. Outstanding defects and limitations

| # | Item | Severity | Type |
|---|---|---|---|
| 1 | `config/universe.csv` empty: no live session can run (health UNHEALTHY) | HIGH (blocks use) | Configuration (user) |
| 2 | Zero live market-hours sessions: candle publication timing vs the 5-second grace, bid/ask availability and rate limits under load are all unobserved | HIGH (evidence) | Missing evidence |
| 3 | No historical data, so no strategy evidence (Phase 7 `PENDING_DATA`) | HIGH (evidence) | Missing evidence |
| 4 | ₹500 infeasible under the frozen rules (§7) | HIGH (economic) | Finding, not a software defect |
| 5 | Local automation (launchd) not installed; server deployment not done | MEDIUM | Operations |
| 6 | Lint, type and dependency-audit checks never run (tools absent) | MEDIUM | Tooling |
| 7 | No git, so rollback depends on backups and folder copies | MEDIUM | Operations |
| 8 | No external notification provider (alerts go to logs, DB and dashboard) | LOW | Limitation |
| 9 | 9 test-artifact folders in `data/evaluation/` | LOW | Housekeeping (manual delete) |
| 10 | Holiday list covers 2026 only; fee rounding assumed; spread and slippage assumed | LOW–MEDIUM | Documented assumptions |

No unresolved critical software, accounting or execution-safety defect was
found.

## 11. Operational instructions and release checklist

Full runbooks: [runbooks.md](runbooks.md). Security and credential
rotation: [security.md](security.md).

1. **Tests:** `.venv/bin/python -m pytest`.
2. **Release gate:** `scripts/release_gate.sh`. It must say RELEASABLE.
   SKIPPED checks were not run.
3. **Before a session:**
   - `python -m app.ops preflight` must not be UNHEALTHY;
   - then `python -m app.ops backup`.
4. **Paper trading:**
   - `python -m app.evaluation start --name ... --from <date>` (once);
   - then `python -m app.evaluation run` (each trading day before 09:15).
5. **Monitoring:** `python -m app.ops health`, or the dashboard's
   Operations view (`streamlit run app/dashboard.py`).
6. **Reports and evidence:**
   - `python -m app.evaluation report|cumulative`;
   - `python -m app.ops reconcile`;
   - `python -m app.readiness`.
7. **Recovery:** runbooks §6 (crash), §8 (backup and restore), §9 (corrupt
   state).

## 12. Primary readiness classification

### **B. INCONCLUSIVE**

The software is complete and passes every executed check: accounting, risk
controls, recovery and the paper-only boundary. But a justified readiness
decision needs evidence that does not yet exist.

**Missing evidence:**
1. Any live market-hours operation of the system. This is blocked first by
   the empty universe.
2. Any historical or out-of-sample strategy evidence.
3. Any live paper-trading record.

**Material finding:** under the frozen ₹500 configuration, the system's
correct output is NO TRADE for every signal. Manual-use evaluation would
therefore verify operations but cannot produce strategy evidence.

**This classification does not authorise automated execution and makes no
claim of profitability.**

## 13. Recommended next actions

| # | Action | Verify with |
|---|---|---|
| 1 | Fill `config/universe.csv` with a broad NSE EQ list fixed in advance, not picked by today's price | `python -m app.ops preflight` (config PASS) |
| 2 | Before 09:15, run `python -m app.evaluation start --name "ORB initial 20" --from <first session>`, then `python -m app.evaluation run` (manually, or after server deployment) | After 15:30: `python -m app.ops health`, `python -m app.ops reconcile`, the daily report. This yields the first live operational evidence |
| 3 | Run `python -m app.robustness fetch --days 90`, then `run --workers 8` | Phase 7 report with a real verdict |
| 4 | Decide on the economics yourself. Capital, target or cost rules would each be a **new strategy version**: freeze a new baseline and evaluate it fresh. Nothing was changed in this review | — |
| 5 | Optional hardening: `pip install -r requirements-dev.txt`, re-run the gate (lint, types, pip-audit); initialise git; configure server services per the runbooks | Gate output |
| 6 | Delete the test artifacts: `rm -rf data/evaluation/EVAL-*` | — |

## 14. Statement

No real orders were placed, modified or cancelled during this review. The
only broker calls made were read-only market-data requests: quotes and
historical candles, during pre- and post-market checks. All trading in this
project is simulated, and real orders are placed manually by the user.
