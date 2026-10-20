# Phase 7 — Pre-implementation audit and plan

Audit date: 2026-10-19. This file was written **before** any Phase 7 code
change, as the Phase 7 spec requires. The findings describe the project as it
stood after Phase 6.

## 1. Components found

| Area | Module | Notes |
|---|---|---|
| Strategy | `app/strategies/opening_range_breakout.py`, `indicators.py` | ORB long-only, version label `orb-long-v1` (in `app/backtesting/engine.py`) |
| Screening / ranking | `app/screening/`, `app/scan/` | `run_scan` is the single decision path for scan, backtest and paper |
| Costs | `app/costs/` + `config/fees/groww_nse_equity_intraday.yaml` | Groww tariff `groww-nse-eq-intraday-2026-10`, verified 2026-10-19, sources in the YAML |
| Risk | `app/risk/` | `RiskCostEngine`, `DailyRiskState`, whole-share sizing, costs included in risk |
| Backtester | `app/backtesting/` | event-driven, execution model `candle-v1`, metrics, periods, OAT sensitivity, walk-forward, reports |
| Paper trading | `app/paper/` | `paper-v1`, SQLite store |
| Live evaluation | `app/evaluation/` | Phase 6 recorder, metrics, reports |

## 2. Test suite

`pytest`: **353 passed, 0 failed, 0 warnings** (2026-10-19, before Phase 7).

## 3. Historical data — what exists

**None.** There is no `data/historical/` folder, no `data/backtests/` output, and
`config/universe.csv` contains only its header. No Phase 4 backtest has ever
been run on real market data. Every Phase 4 result so far came from synthetic
test fixtures.

What can be obtained:

- **Groww 5-minute candles**: the last ~3 months only, at most 15 days per
  request (Groww SDK docs; checked live 2026-10-19). That gives roughly
  60 trading sessions. Observed properties: candle timestamps are candle
  starts, volume is cumulative (the adapter differences it), the 15:20 candle
  is missing every day.
- **Groww daily candles**: up to 1080 days per request; per-day volume.
- **Not available from any configured source**: historical bid/ask quotes,
  point-in-time index or universe constituents, corporate-action records,
  delisted symbols.

Consequences:

- With 10 sessions of relative-volume history and 20 of liquidity history, the
  first ~10–20 of the ~60 sessions are warm-up only (fewer when Groww daily
  candles supply the liquidity history).
- At most about 40–50 tradable sessions remain. That limits both walk-forward
  folds and statistical power.
- The holiday file covers 2026 only, which is enough for Groww's 3-month
  window. Any older data would need earlier NSE holiday lists.

## 4. Data-quality controls already present

- **Session validation**: each session is validated on its own
  (`app/backtesting/dataset.py`) and excluded, never repaired, on any of:
  - impossible OHLC values;
  - NaN values;
  - duplicate rows;
  - out-of-order rows;
  - rows outside 09:15–15:30;
  - weekend or holiday rows;
  - more than 3 missing candles.
- **Missing and zero-volume candles** are counted separately and never filled.
- **Dataset ID**: SHA-256 over the data and the dataset policy.

Missing controls:

- **Corporate actions**: no detection or handling at all. An unadjusted split
  or bonus issue would distort three things:
  - the relative-volume baseline, for up to 10 sessions;
  - the price band;
  - the liquidity average.

  Whether Groww adjusts its intraday candles is not documented.
- **Missing whole sessions**: not reported per symbol. A trading day with no
  rows for a symbol is simply absent.
- **Survivorship**: the universe is whatever list the user supplies today.
  Stocks that were delisted, suspended, or moved out of the ₹50–₹100 band are
  missing. Picking stocks by today's price is a selection bias. The screener
  does recheck the price band point-in-time.
- **Bid/ask**: none historically. The backtest needs `require_spread: false`
  and uses a constant assumed spread of 0.10%.

## 5. Lookahead and leakage review

| Check | Finding |
|---|---|
| Indicator inputs | `build_snapshot` exposes only candles with end ≤ `as_of` (`CandleSeries.completed`). VWAP is per session, RV uses prior sessions, EMA runs forward. **OK** |
| Daily data | fetched up to the previous trading day only. **OK** |
| Price band | uses last completed candle / prior close. **OK** |
| Entry fill | next candle's open, never the signal close. **OK** |
| Stop/target in one candle | stop assumed first. **OK (conservative)** |
| Target fill | filled on a touch (`high >= target`). **Optimistic**: a touch does not guarantee a fill. Sensitivity needed. |
| Ranking among same-slot signals | uses only signal-time data. **OK** |
| Period runs | each period is simulated from fresh capital; earlier sessions are lookback only. **OK** |
| Phase 4 `walk_forward()` | picks the **best training expectancy** among the grid per fold. No leakage into the test window, but it is automatic parameter selection, which Phase 7 forbids for the primary evaluation. |
| Phase 4 `sensitivity()` / `_with()` | rebuilds `Settings(**data)`, which **re-reads `NSE_*` environment variables, and these override the grid value**. If a parameter is set in the environment, every grid point silently runs the same value. **Defect.** |

## 6. Reproducibility and versioning

- **Config version** (`Settings.version()`): hashes every non-secret setting,
  including **absolute file paths** (DB paths, output dirs, holiday file,
  fee file). The same strategy gets a different hash after moving the project
  to a server. **Defect for a frozen baseline.**
- **Strategy code version**: only a hand-written label (`orb-long-v1`). The
  project is not a git repository, so there is no code hash. **Gap.**
- **Backtest reports** are byte-reproducible: the run ID is a hash of inputs,
  with no wall-clock time inside.
- **Phase 6 reports** are rebuilt from persisted SQLite records. Reproducible.
- **Test isolation defect**:
  - `tests/test_evaluation.py::test_config_change_recorded_and_excluded`
    builds a second `Settings` without the temporary output dir, so every test
    run writes a daily report into the real `data/evaluation/`.
  - The 9 `EVAL-*` folders there (all dated 2025-03-12, provider `memory`)
    are test artifacts, not evaluation results.

## 7. Phase 4 / Phase 6 results available

- Phase 4: **no real-data backtest exists.**
- Phase 6: the live paper database has **0 evaluations and 0 sessions**. The
  first live session is scheduled for 2026-10-21.

So there are **no empirical results to review**. Any Phase 7 conclusion must
come from data fetched after this audit, and must say so.

## 8. Other observations relevant to evidence

- **Capital feasibility**: Phase 3 found that ₹500 at Groww fails the default
  risk rule. One share at ₹70 loses about ₹4.96 net at the stop, against a
  ₹2.50 risk budget (0.5% of ₹500), because of the ₹5 minimum brokerage.
  Under the frozen defaults, the primary backtest is therefore expected to
  reject most or all signals with `REJECTED_COSTS` / `REJECTED_CAPITAL`.
  That is a result, not a bug. To separate "does the signal have an edge"
  from "is it tradable at ₹500", a clearly labelled **diagnostic** of every
  technically qualified signal is needed.
- **Performance**: `run_backtest` costs about **300–600 ms per symbol-session**,
  measured on synthetic random walks. Profiling shows about 60% of the time
  goes to repeated timezone-aware `.dt.date` / `.dt.hour` conversions and to
  groupbys on Python `date` objects, in `relative_volume_detail`,
  `session_vwap`, `snapshot.today` and `evaluate`. A sensitivity grid over
  walk-forward folds would take hours. A behaviour-preserving speed-up is
  needed first.
- **Evidence threshold**: `min_trades_for_evidence = 30` is an arbitrary
  count. Phase 7 replaces it, for robustness reporting, with
  uncertainty-based assessment. The label stays in Phase 4/6 reports.

## 9. Implementation plan

Existing modules are reused throughout. No strategy, fee, risk or backtest
logic is duplicated. New code goes in `app/robustness/`.

0. **Fixes found by this audit** (behaviour-preserving):
   - Make the test write to `tmp_path`, and remove the 9 test-artifact
     folders.
   - Fix `_with()` so variants ignore environment overrides.
   - Speed up the indicator, snapshot and strategy hot paths without changing
     results. Equivalence is checked by comparing full trade, rejection and
     equity tables before and after, on a synthetic dataset that produces
     trades.
1. **Frozen baseline** (`baseline.py`):
   - `freeze` writes `config/baselines/<name>.json`, read-only, containing:
     - the full non-secret settings;
     - a **portable strategy fingerprint**, with paths removed and the fee and
       holiday files hashed by content;
     - a strategy-code hash over the decision-path source files;
     - the execution-model version;
     - fee schedule ID, sources and verification date;
     - the predeclared acceptance criteria, sensitivity grid, cost scenarios,
       regime rules and baselines.
   - `load_frozen` builds settings that **ignore environment and YAML
     overrides**, then verifies both hashes.
   - A drifted code hash is reported, not silently accepted.
2. **Historical data fetch** (`fetch.py`):
   - Read-only Groww candles (5-minute and daily) for an explicit symbol
     list, written to `data/historical/` with a manifest. The manifest
     records the request ranges, the volume differencing applied, unknown
     adjustment status and the universe source.
   - Raw rows that fail validation are still saved, so per-session
     validation can exclude them visibly.
3. **Data audit** (`data_audit.py`): extends the dataset quality report with:
   - per-symbol missing trading sessions against the NSE calendar;
   - suspected unadjusted corporate actions: overnight moves beyond the 20%
     price band. Affected baselines are excluded by a declared policy and
     listed;
   - point-in-time universe support through an optional membership file;
   - an explicit list of known biases.
4. **Walk-forward** (`walk_forward.py`):
   - Chronological plan: warm-up, then rolling train → validation → test
     folds.
   - Window lengths derive from the number of tradable sessions with a
     documented rule; manual override is possible.
   - The frozen baseline runs on every window. Aggregated out-of-sample
     results come from the test windows. Nothing is selected.
5. **Cost and execution sensitivity**:
   - Predeclared scenarios: base; slippage ×2 and ×4; spread ×2 and ×3; a
     target trade-through fill rule (new backtest option, default off =
     `candle-v1`); and a conservative combination.
   - Gross vs net results.
   - Break-even extra cost per trade, and as % of turnover, when meaningful.
6. **Parameter sensitivity**:
   - Predeclared one-at-a-time grid around the baseline, plus a 2-D grid of
     RV threshold × reward/risk for a heatmap.
   - Every configuration is reported for train, validation and test.
   - The report includes:
     - an instability flag (sign flips or large swings between adjacent
       values);
     - the number of configurations tested;
     - train-vs-test rank correlation;
     - a best-of-N selection-bias warning.
   - Nothing is selected.
7. **Regimes and stock-level analysis**:
   - Rules are declared before results are computed.
   - Market proxy: a configured benchmark symbol (default NIFTYBEES) if
     present, otherwise an equal-weight universe average, labelled as such.
   - Groupings:
     - session trend;
     - session volatility and market-wide liquidity, each relative to the
       prior 20 sessions only;
     - symbol;
     - price band;
     - month;
     - quarter.
8. **Statistics** (`stats.py`):
   - Reused Phase 4 metrics, plus: losing streaks, return distribution,
     per-fold results, and per-session P&L.
   - Session-block bootstrap CI for expectancy. Sessions are resampled in
     blocks, so trades on the same or nearby days are not treated as
     independent.
   - Effective sample size and minimum detectable effect.
   - No annualisation.
9. **Baselines** (`baselines.py`), all fixed in advance:
   - no-trade;
   - cash retention;
   - passive intraday long in the most liquid eligible stock (09:30 →
     15:10, same capital, costs and screen);
   - benchmark proxy, same rule, if present.
10. **Report** (`report.py`):
    - The 13-section Strategy Robustness Report, in Markdown and HTML.
    - Inline SVG charts generated from the results, with no new dependency.
    - CSV and JSON exports.
    - A dashboard view.
    - Byte-reproducible output.
11. **Final assessment rules**, declared in the frozen baseline:
    - `PENDING_DATA`;
    - `FAILED_PREDEFINED_CRITERIA`;
    - `INCONCLUSIVE`;
    - `SUPPORTS_FURTHER_EVALUATION`.
12. **Tests** covering every item in spec §14, plus docs.
