# Backtesting (Phase 4)

> **Every result here is SIMULATED.** Fills come from 5-minute candles under
> stated assumptions, not from real order execution. A good backtest is not
> evidence of future profit, and a small sample is not evidence of anything.

## Running

```bash
# Historical data must be supplied from an authorized source (see docs/setup.md
# for the CSV layout). Backtests need two explicit opt-ins:
export NSE_SCREENING__REQUIRE_SPREAD=false                     # no historical quotes
export NSE_COST_REVIEW__APPLY_CURRENT_RATES_TO_PAST_DATES=true # fee rates verified from 2026-10-19

.venv/bin/python -m app.backtesting --data data/historical \
    --start 2025-01-01 --end 2025-06-30 \
    --periods dev:2025-01-01:2025-02-28,val:2025-03-01:2025-04-30,test:2025-05-01:2025-06-30 \
    --sensitivity --walk-forward 40:20
```

Without the opt-ins the run stops with an explanation rather than silently
assuming anything. Earlier sessions in the files are used as lookback
history (liquidity, relative volume) but are only traded from `--start`.

## Dataset policy (`app/backtesting/dataset.py`)

- Each session is validated separately with the Phase 1 validator. A
  session with any error (impossible OHLC, NaN, duplicate or out-of-order
  rows, candles outside 09:15–15:30, a weekend or holiday) is **excluded,
  never repaired**.
- **Missing candles** (no row for a slot) and **zero-volume candles** (a
  row with volume 0) are counted separately. A session missing more than
  `max_missing_candles_per_session` (3) slots is excluded as incomplete.
  Gaps are never filled.
- Daily candles for the liquidity screen are aggregated from accepted
  sessions unless a `<SYMBOL>_1d.csv` exists.
- The dataset ID is a SHA-256 over the file contents and the policy.

## Simulation (`app/backtesting/engine.py`)

The engine processes every session slot by slot in time order. At each
5-minute step:

1. Entries decided at the previous candle close fill at **this candle's open**.
2. Open positions are checked for exits against this candle.
3. Equity is marked at the candle close.
4. At the close, the existing `run_scan` (Phase 2) runs with only completed
   candles visible. Each new signal goes through the Phase 3 `RiskCostEngine`
   with the running daily risk state.

There's one signal per symbol per day (Phase 2 rule) and no duplicate
entries. Positions never stay open overnight.

### Execution model `candle-v1` (`app/backtesting/execution.py`)

| Situation | Rule |
|---|---|
| Entry | next candle's open; skipped if that open ≤ stop or > entry reference + `max_entry_gap_pct` (0.5%) |
| Stop touched | exit at the stop |
| Candle opens below the stop | exit at that open (gap through the stop, worse than planned) |
| Target touched | exit at the target (no price improvement assumed) |
| Stop and target in one candle | **stop assumed first**, trade flagged `ambiguous` |
| 15:10 reached | exit at the open of the first candle at or after 15:10 |
| `max_holding_minutes` reached | exit at that candle's open |
| Data ends with position open | exit at the last close, flagged `incomplete` |

Every fill is checked to lie inside its candle's low–high range. Spread and
slippage are the Phase 3 cost lines (assumed 0.10% spread, 0.05% slippage
per fill), applied once.

### Accounting

```
entry: cash -= qty × fill + buy fees + buy spread + buy slippage
exit:  cash += qty × exit − sell fees − sell spread − sell slippage
equity = cash + qty × last close (open positions, before exit costs)
```

Each session starts a new `DailyRiskState` with the current cash, so the
loss lock, trade count and concurrency limits work as in Phase 3. Planned
loss (at the stop, at sizing time) and realized loss are both recorded. A
gap can make the realized loss larger than planned, and tests demonstrate it.

## Metrics (`app/backtesting/metrics.py`)

Definitions are in the module docstring. In brief:
- **Win:** net P&L > 0. **Loss:** net P&L < 0. Zero counts as neither.
- **Expectancy:** mean net P&L per trade.
- **Profit factor:** wins / |losses|. It's undefined (not infinity) with no
  losing trades.
- **Max drawdown:** taken from the candle-level mark-to-market equity curve.
- **Cost:** shown as % of traded value and as % of gross profit.

Every ratio with no data is `None`. Fewer than `min_trades_for_evidence`
(30) trades is labelled **INSUFFICIENT EVIDENCE**.

## Validation (`app/backtesting/analysis.py`)

- **Periods:** development, validation and test must be in order and must
  not overlap. Each period runs separately from fresh capital. Nothing is
  shuffled.
- **Sensitivity:** varies one parameter at a time: opening-range minutes,
  relative-volume threshold, stop placement, target multiple, slippage,
  spread. It reports every run and picks no winner.
- **Walk-forward:** on each training window, a parameter set is chosen by
  expectancy, but only among sets with at least the minimum trade count;
  otherwise the base settings are kept. The choice is then run once on the
  next, unseen window. Only the unseen-window results count. This is
  automatic selection: Phase 7 does **not** use it for the primary
  evaluation (see [robustness.md](robustness.md)).
- Variants are built with `settings_from_values`, so `NSE_*` environment
  variables cannot override a grid value (fixed in Phase 7: before that, an
  environment variable silently made every grid point identical).

### Optional engine features (Phase 7, off by default)

- `membership=`: point-in-time universe (symbols eligible per session).
- `shadow=True`: risk-rejected signals are followed through the same
  execution rules as a **diagnostic** (`result.shadow`), without touching
  cash, limits, equity or trades.
- `backtest.target_trade_through_pct`: a target counts as filled only if
  the high trades that % beyond it. 0 = `candle-v1`; otherwise the model
  label is `candle-v1+tt<pct>`.

## Outputs (`data/backtests/<run_id>/`)

| File | Contents |
|---|---|
| `config.json` | full settings, dataset/config/strategy/execution/fee versions |
| `dataset_quality.json` | coverage, accepted/excluded sessions with reasons, missing vs zero-volume candles |
| `trades.csv` | trade-by-trade ledger with full cost breakdown, planned vs realized loss, flags |
| `orders.csv`, `fills.csv` | each simulated order and fill separately |
| `rejected_signals.csv` | signals rejected by risk/cost review or not filled, with reasons |
| `equity_daily.csv`, `equity_intraday.csv` | equity curves |
| `metrics.json`, `costs.json` | performance metrics and cost totals |
| `out_of_sample.json`, `sensitivity.csv`, `walk_forward.csv` | when requested |
| `assumptions.md` | assumptions, limitations, evidence label |

The run ID hashes the dataset, config and model versions and the period, so
identical inputs reproduce byte-identical files (tested).

## Limitations

- **Candle-only fills:** no historical quotes, order book or partial fills.
  Intrabar order is unknown, which the "stop first" rule handles
  conservatively.
- **Fee rates:** current Groww rates are applied to past dates (opt-in,
  labelled). Historical tariffs differed.
- **Rejected signals:** only technically qualified setups that failed risk
  review or filling are listed. Breakouts failing Phase 2 rules are not
  signals.
- **Speed:** about 0.1 s per symbol-session on this machine. Phase 7 cut
  this from about 0.3 s by replacing per-row timezone conversions in the
  indicator and snapshot hot paths; outputs were verified byte-identical. A
  large universe over a year, with sensitivity runs, still takes a long
  time.
- **No results on real data yet:** the engine is verified on synthetic
  scenarios with hand-calculated outcomes only.
