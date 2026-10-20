# NSE Intraday Research Agent

A research tool for NSE cash-equity intraday setups. It screens stocks,
evaluates predefined strategies and reports costs and risk. **It never places,
modifies or cancels orders** — every trading decision is made manually.

Nothing here predicts profit. Passing tests show that the code follows its
rules, not that the strategy is profitable. Backtest and simulated results do
not guarantee future performance.

**Real orders are placed manually by the user, outside this application.**
The application only simulates (paper) orders. There is no live execution
mode.

## Current readiness (Phase 9 final review, 2026-10-20)

**Classification: B. INCONCLUSIVE.** Full report:
[docs/final_readiness_report.md](docs/final_readiness_report.md). Evidence:
`python -m app.readiness`, written to `data/readiness/<date>/`.

| Area | State |
|---|---|
| Software | All phases implemented; 488 tests pass (Phase 10 included; 1 opt-in live LLM test skipped); release gate RELEASABLE (lint, type and dependency-audit tools not installed, so those checks are SKIPPED) |
| Accounting, risk controls, recovery, paper-only boundary | Verified with executed tests (fixtures; one read-only check against the real Groww SDK) |
| Live operation | **Unverified.** No market-hours session has ever run. Health is UNHEALTHY until `config/universe.csv` lists symbols |
| Strategy evidence | **None.** Profitability is unproven: no historical data fetched, no live paper sessions |
| ₹500 capital | **Economically impractical under the frozen rules.** The cost/risk engine rejects every representative setup, so the system will report NO TRADE |

This is not a production-ready or profitable system. It is a research and
paper-trading tool that is ready to collect its first evidence once the
universe is configured.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Config, market-data interfaces (mock + historical CSV), data validation, calendar, logging | Done, tested |
| 2 | Screening, indicators, opening-range breakout, signal states, ranking, NO TRADE | Done, tested |
| 3 | Groww cost model, break-even, position sizing, daily risk limits, final review status | Done, tested |
| 4 | Historical backtesting, execution model, metrics, out-of-sample / walk-forward / sensitivity, reports | Done, tested on synthetic data |
| 5 | Paper trading: order lifecycle, SQLite persistence & recovery, scheduler, Groww data adapter, dashboard, daily reports | Done, tested |
| 6 | Live paper-trading evaluation: session register, data-quality ledger, signal audit, reconciliation, metrics, reports | **Done, tested with fixtures; live observation starts 2026-10-21** |
| 7 | Robustness validation: frozen baseline, data audit, walk-forward, cost/parameter sensitivity, regimes, bootstrap uncertainty, baselines, report | **Infrastructure done and tested on synthetic data; empirical results PENDING historical data** |
| 8 | Production hardening: paper-only boundary, timeouts/circuit breaker, state validation, reconciliation, health, alerts, backup/restore, graceful shutdown, release gate, runbooks | **Done, failure-injection tested; not yet proven in a live session** |
| 9 | Final integration audit, ₹500 feasibility analysis, readiness report, project closure | **Done; classification B (Inconclusive)** |
| 10 | AI research agent: LLM provider (Anthropic / deterministic mock), 11 read-only tools, bounded workflow, immutable recommendations, dashboard, AI-vs-baseline comparison | **Done and tested with the mock; live LLM PENDING (no `NSE_LLM_API_KEY`)** |

## Quick start

```bash
cd nse-intraday-agent
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.lock      # exact tested versions

.venv/bin/python -m pytest                                   # all tests
.venv/bin/python -m app.main --date 2025-03-12               # data check
.venv/bin/python -m app.scan --at 2025-03-12T10:30 --all     # scan incl. risk & costs
.venv/bin/python -m app.scan --at 2025-03-12T10:30 --json    # full structured output
.venv/bin/python -m app.scan --at 2025-03-12T10:30 --no-risk # technical view only

# Build the symbol pool from Groww data (fixed rules: NSE EQ, intraday allowed, no ETFs,
# close Rs 40-120, avg traded value >= Rs 50 lakh, top 50 by liquidity). Review it; never
# change it during an evaluation. --dry-run shows the selection without writing.
.venv/bin/python -m app.data.universe_builder [--dry-run] [--force]

# Server deployment (k3s, namespace nse-intraday): see docs/deployment.md

# Live paper-trading evaluation - see docs/evaluation.md (fill config/universe.csv first)
.venv/bin/python -m app.evaluation start --name "ORB initial 20" --from 2026-10-21
.venv/bin/python -m app.evaluation run          # each trading day, before 09:15 IST
.venv/bin/python -m app.evaluation cumulative

# Paper trading (SIMULATED) - see docs/paper_trading.md
.venv/bin/python -m app.paper replay --date 2026-10-19
.venv/bin/streamlit run app/dashboard.py

# AI research agent (advisory only) - see docs/ai_agent.md
.venv/bin/python -m app.agent run --window intraday       # one research run (mock LLM by default)
.venv/bin/python -m app.agent schedule                     # pre / intraday / post windows today
.venv/bin/python -m app.agent check-llm                    # verify a real model (needs NSE_LLM_API_KEY)
.venv/bin/python -m app.agent compare                      # AI-assisted vs deterministic baseline

# Final readiness evidence (tests, gate, Rs 500 feasibility, persisted evidence)
.venv/bin/python -m app.readiness                          # -> data/readiness/<date>/

# Operations - see docs/runbooks.md (crash recovery: section 6; backup/restore: section 8)
.venv/bin/python -m app.ops preflight                      # before each session
.venv/bin/python -m app.ops health                         # any time (exit 0/1/2)
.venv/bin/python -m app.ops backup                         # before each session
scripts/release_gate.sh                                    # before any release

# Robustness validation (SIMULATED) - see docs/robustness.md
.venv/bin/python -m app.robustness verify                   # frozen baseline
.venv/bin/python -m app.robustness fetch --days 90          # Groww candles (needs universe.csv)
.venv/bin/python -m app.robustness run --workers 8          # report in data/robustness/

# Backtest (SIMULATED) - needs authorized historical CSVs; see docs/backtesting.md
NSE_SCREENING__REQUIRE_SPREAD=false NSE_COST_REVIEW__APPLY_CURRENT_RATES_TO_PAST_DATES=true \
  .venv/bin/python -m app.backtesting --data data/historical --start 2025-01-01 --end 2025-06-30
```

`--at` is the evaluation time (IST). The scan uses only candles that had
completed by then.

## Docs

- [docs/setup.md](docs/setup.md): configuration, holidays, data files
- [docs/architecture.md](docs/architecture.md): data flow and modules
- [docs/strategy.md](docs/strategy.md): screening, indicators, signal rules, states, scoring, example output
- [docs/cost_model.md](docs/cost_model.md): Groww fee schedule and sources, P&L, break-even, sizing, risk limits, review statuses
- [docs/backtesting.md](docs/backtesting.md): dataset policy, execution model, accounting, metrics, validation, outputs
- [docs/paper_trading.md](docs/paper_trading.md): paper engine, order lifecycle, live-data status, accounting, persistence
- [docs/evaluation.md](docs/evaluation.md): starting/stopping the evaluation, data modes, reports, metric formulas, reconciliation, observation protocol
- [docs/runbooks.md](docs/runbooks.md): operations runbooks and release gate; [docs/security.md](docs/security.md): security findings, credential rotation; [docs/phase8_audit.md](docs/phase8_audit.md): risk register
- [docs/ai_agent.md](docs/ai_agent.md): AI research agent architecture, providers, tools, workflow, schema, cost, evaluation ([plan](docs/phase10_plan.md))
- [docs/final_readiness_report.md](docs/final_readiness_report.md): Phase 9 final audit and readiness classification
- [docs/robustness.md](docs/robustness.md): Phase 7 frozen baseline, protocol, data audit, walk-forward, sensitivity, statistics, assessment ([audit](docs/phase7_audit.md))

## Known limitations

- **₹500 is not enough for this strategy at Groww under default rules.**
  The ₹5 / 2.5% minimum brokerage makes every typical setup infeasible. Most
  scans therefore end in NO TRADE. See [docs/cost_model.md](docs/cost_model.md).
- **Fee rates are verified only from 2026-10-19 onward.** For past dates,
  `apply_current_rates_to_past_dates` must be enabled; results are then
  labelled as using retroactive rates.
- **Fee rounding is assumed** (per order, to the paisa). Groww doesn't
  publish rounding rules. Compare against a real contract note.
- **Spread and slippage are assumptions** unless a fresh quote exists.
- **No backtest on real data yet.** No historical data has been fetched and
  `config/universe.csv` is empty, so the Phase 7 verdict is PENDING_DATA. The
  engine is verified only on synthetic scenarios. Backtest results are simulated from candles and are not trading
  results.
- **Traded value is approximate** (close × volume). Providers don't supply
  exchange turnover yet.
- **Tick size is not modelled.** The spread threshold interacts with tick
  size: if a stock's tick were ₹0.05, a one-tick spread at ₹50 would already
  be 0.10%. Current tick sizes must come from the exchange/provider
  (Phase 6), not from assumptions.
- **Historical CSVs have no quotes**, so the spread can't be checked. Set
  `screening.require_spread: false` for historical research. The spread is
  then reported as unavailable, never as zero.
- **Mock data is for testing only.** Its volume is unrelated to price, so it
  rarely produces signals.
- **Holidays:** NSE's official 2026 list is included. Other years must be
  added before trading them; the paper scheduler refuses uncovered years.
- **Live data:** Groww login (TOTP), quotes and candles verified on 2026-10-19
  and 2026-10-20 outside market hours. Nothing has been observed during
  market hours yet.
- **No Docker, no git, no server deployment yet.** The local launchd agents
  are not installed (see docs/runbooks.md, "Server deployment notes").
- **No external alert notifications.** Alerts go to logs, the database and
  the dashboard.
