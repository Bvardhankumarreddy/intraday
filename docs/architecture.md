# Architecture

```
MarketDataProvider (mock | historical CSV | in-memory | live in Phase 6)
        │  validated candles + quotes, labelled with DataMode
        ▼
build_snapshot(as_of)            app/scan/snapshot.py
        │  ONLY candles completed at as_of, quotes timestamped ≤ as_of
        ▼
screen()                         app/screening/  → PASS / REJECTED / INSUFFICIENT_DATA
        │  (PASS only)
        ▼
evaluate()                       app/strategies/ → SignalStatus + TradeSetup
        │  (QUALIFIED_PRE_COST only)
        ▼
score_candidate() → rank()       app/screening/ranking.py
        ▼
ScanResult                       app/scan/models.py → CANDIDATES_FOR_REVIEW / NO_TRADE
        ▼
TradeEvaluationRequest           app/risk/interfaces.py
        ▼
RiskCostEngine.evaluate()        app/risk/engine.py → RiskReport + ReviewStatus
        │  fee schedule (app/costs/), sizing, break-even, daily limits
        ▼
ScanResult.reviewable            QUALIFIED_FOR_REVIEW only → else NO_TRADE
```

## Principles

- **One look-ahead boundary.** Only `build_snapshot` reads providers, and it
  exposes only data available at `as_of`. Screening, indicators, strategy and
  ranking are pure functions of the snapshot. A test checks that future
  candles do not change a result.
- **Unavailable is not zero.** Missing values are `None`/NaN and produce
  explicit reasons, never default numbers.
- **Mandatory rules before score.** Only candidates that pass every rule are
  scored.
- **Separation of concerns.** The strategy emits reference prices only.
  Quantity, brokerage, taxes, slippage and net P&L belong to Phase 3
  (`TradeEvaluator`). Nothing in the codebase places orders.
- **Traceability.** Every output carries the data mode, data source,
  evaluation time and `config_version` (a hash of all non-secret settings).

## Modules

| Path | Purpose |
|---|---|
| `app/config.py` | Settings (YAML + env), validation, `version()` |
| `app/data/` | Provider interface, mock/historical/in-memory providers, validation |
| `app/utils/market_calendar.py` | Sessions, holidays, session status |
| `app/scan/` | Snapshot, output models, pipeline, CLI (`python -m app.scan`) |
| `app/screening/` | Eligibility, liquidity, screener, scoring and ranking |
| `app/strategies/` | Indicators and the opening-range breakout |
| `app/risk/interfaces.py` | Request model and `TradeEvaluator` protocol |
| `app/risk/engine.py` | Risk/cost evaluation and final review status |
| `app/risk/position_sizing.py`, `risk_limits.py` | Sizing, daily state and limits |
| `app/costs/` | Fee schedules, brokerage, statutory charges, GST, execution costs, break-even |
| `config/fees/` | Sourced, dated fee schedules (Groww, illustrative) |
| `app/evaluation/` | Evaluation register, data-quality assessment, signal audit, reconciliation, metrics, reports (`python -m app.evaluation`) |
| `app/agent/` | Phase 10 AI research agent (advisory): LLM providers, read-only tools, bounded workflow, append-only recommendations, post-session review, A/B comparison, dashboard view (`python -m app.agent`) |
| `app/readiness/` | Phase 9 readiness evidence and Rs 500 feasibility (`python -m app.readiness`) |
| `app/ops/` | Phase 8: health, alerts (dedup, pluggable notifier), portfolio reconciliation with severities, backup/verify/restore, retention, release gate, dashboard Operations view (`python -m app.ops`) |
| `app/utils/fs.py` | owner-only permissions, path-traversal guard, overwrite-preserving writes |
| `app/robustness/` | Phase 7: frozen baseline, predeclared protocol, Groww history fetch, data audit, walk-forward, cost/parameter sensitivity, regimes, comparison baselines, statistics, assessment, report (`python -m app.robustness`) |
| `app/paper/` | Paper engine, order lifecycle, SQLite store, scheduler, reports, views (`python -m app.paper`) |
| `app/data/groww_provider.py` | Read-only Groww live-data adapter (unverified live) |
| `app/dashboard.py` | Streamlit paper-trading dashboard (read-only) |
| `app/backtesting/` | Dataset preparation, event-driven engine, execution model, metrics, validation, reports (`python -m app.backtesting`) |
