# Strategy robustness validation (Phase 7)

> **Everything here is SIMULATED research on historical candles.** A
> positive result means "worth more forward paper trading", never "will be
> profitable". A negative or inconclusive result is a valid outcome and is
> reported as such.

Phase 7 asks one question: **is there credible evidence of a repeatable edge
after costs, for the exact strategy as frozen?** It reuses the Phase 2
strategy, the Phase 3 cost and risk engines and the Phase 4 backtester. It
duplicates none of them and changes no parameter.

The pre-implementation audit is in [phase7_audit.md](phase7_audit.md).

## Workflow

```bash
# 1. Freeze the baseline (once per strategy version; read-only, never overwritten)
.venv/bin/python -m app.robustness freeze --name orb-baseline-v1
.venv/bin/python -m app.robustness verify        # re-checks hashes and code drift

# 2. Get historical data (read-only Groww candles; ~3 months of 5-minute history)
#    config/universe.csv: symbol,name - a BROAD list of NSE EQ stocks, not chosen by
#    today's price or by past performance (see "Universe" below).
.venv/bin/python -m app.robustness fetch --days 90

# 3. Inspect the chronological plan, then run
.venv/bin/python -m app.robustness plan
.venv/bin/python -m app.robustness run --workers 8          # --grid full|oat|none
```

`run` always writes `data/robustness/<baseline>/<run_id>/`. If data is
missing or too short, the report is **PENDING_DATA** and lists exactly what
is missing. Nothing is estimated or filled in. The dashboard's
**Robustness** view (`streamlit run app/dashboard.py`) shows the verdict,
criteria, tables and charts.

Run time: about 0.1–0.3 s per symbol-session, per configuration. 50 symbols ×
60 sessions with the full grid (22 configurations) takes roughly an hour on 8
workers. Use `--grid oat` (one-at-a-time values only) or `--grid none` for
faster runs.

## Frozen baseline (`app/robustness/baseline.py`)

`config/baselines/<name>.json` holds the following:

| Field | Meaning |
|---|---|
| `settings` | every non-secret setting, with paths stored relative to the project |
| `strategy_fingerprint` | SHA-256 of every decision-relevant setting (market hours, capital, risk, screening, strategy, scoring, execution, cost review, backtest, broker), plus the **content** of the fee-schedule and holiday files. Paths and output folders are excluded, so it is the same on a server. |
| `code_hash` | SHA-256 of the decision-path source files (strategy, screening, scan, costs, risk, backtest engine/execution/dataset, data models/validation, calendar). The project has no git, so this is the code version. |
| `strategy_version`, `execution_model_version` | `orb-long-v1`, `candle-v1` |
| `fee_schedule` | schedule ID, version, verification date and every source with its effective date |
| `historical_adaptations` | `screening.require_spread=false` (no historical quotes) and `cost_review.apply_current_rates_to_past_dates=true` (tariff verified from 2026-10-19). Applied only at run time, recorded in every report, not part of the fingerprint. |
| `protocol_text`, `protocol_sha256` | the predeclared evaluation protocol |
| `frozen_sha256` | self-hash; any edit makes the file fail to load |

Loading rebuilds the settings from the file alone. **Environment variables
and YAML overrides are ignored**, so `NSE_...` variables cannot silently
change the frozen strategy. A changed fee or holiday file breaks the
fingerprint. Changed decision-path code is **code drift**: `run` refuses
it unless `--allow-code-drift` is passed, and then every report says so. A
modified strategy needs a new baseline name and a fresh evaluation.

## Predeclared protocol (`config/robustness/protocol.yaml`)

All rules were fixed on 2026-10-19, before any market data was fetched:
windows, cost scenarios, the parameter grid, regime definitions, comparison
baselines, statistical settings and acceptance criteria. The file explains
each choice. Freezing copies the protocol into the baseline, so later edits
cannot affect a frozen evaluation.

## Data quality and bias controls (`app/robustness/data_audit.py`)

| Check | Handling |
|---|---|
| Invalid OHLC, NaN, duplicate/out-of-order rows, candles outside 09:15–15:30, weekend/holiday rows | Phase 4 per-session validation: the session is **excluded, never repaired**, and listed with reasons |
| Missing candles / zero-volume candles | counted separately; more than 3 missing excludes the session (Groww always lacks 15:20, so 1 is normal) |
| Missing whole sessions | regular trading days with no rows are listed per symbol. Never filled, and never treated as avoided losses |
| Special sessions (e.g. Muhurat) | reported; never traded (only regular sessions enter the plan) |
| Timezone | candle starts in IST; checked against the session grid |
| Corporate actions | an overnight move beyond 20% (outside NSE price bands) is flagged as a suspected unadjusted split/bonus. The symbol is **truncated at the first suspect**, so baselines never mix pre- and post-adjustment data. Smaller adjustments are not detected. Groww does not document its adjustment policy. |
| Universe / survivorship | optional `universe_membership.csv` (`symbol,from,to`) gives a point-in-time universe. Without it, the report states the survivorship and selection bias. |
| Lookahead | decisions use only candles completed by the decision time (`build_snapshot`); daily data ends the previous session; entries fill at the next candle's open; tested by appending wild future data and checking earlier results do not change |
| Bid/ask | none historically; constant assumed spread; spread screen untestable (reported as such) |

### Universe

Survivorship and selection bias are the largest unremovable risks here.
Build `config/universe.csv` from a broad list fixed **before** looking at
results. For example, take every NSE EQ stock in a published index, or
every stock above a liquidity floor, as of the start of the period. Do not
use "stocks between ₹50 and ₹100 today": the screener applies the price band
point-in-time on each session, and pre-filtering by today's price selects on
the outcome.

## Walk-forward plan (`app/robustness/walk_forward.py`)

```
| warm-up | train (2t) | validation (t) | test (t) |
          | t | train ... | validation | test |   (rolled forward by t)
```

- **Warm-up**: the relative-volume lookback (10 sessions), raised to the
  liquidity lookback (20) when daily candles don't cover it. History only:
  never traded, never counted.
- **Test length** `t = max(5, floor(S/8))`, where S is the number of
  tradable sessions. Validation = t, train = 2t, and folds roll by t.
  - A test window needs at least a trading week.
  - S/8 gives about five folds at any data length.
  - Test windows never overlap; their union is the **aggregated
    out-of-sample** period.
- `--train/--validation/--test` override the lengths, and the overrides are
  recorded in the report.
- Every window is simulated separately from the initial capital, with
  earlier sessions as indicator history only. **Only the frozen baseline is
  in the primary evaluation.** Train and validation exist for stability
  checks, not for choosing parameters.

With Groww's ~60 sessions of 5-minute data, the default gives roughly
`10 warm-up + 50 tradable → t = 6, 2–3 folds, about 12–18 out-of-sample
sessions`. That is very little (see "Sample size").

## What is reported

1. **Folds**: every train, validation and test window. Signals, trades,
   gross, costs, net, expectancy, win rate, profit factor and drawdown.
   No fold is omitted.
2. **Aggregated out-of-sample**: test windows chained, with equity carried
   across windows for the drawdown.
3. **Gross vs net**: every cost component (brokerage, STT, stamp duty,
   exchange, IPFT, SEBI, GST, spread, slippage), counted once each.
4. **Signal diagnostic** (labelled as diagnostic, not account performance):
   - Every technically qualified signal, including those the risk/cost review
     rejected, is followed through the same execution rules without touching
     cash. Results are in gross R.
   - The same signals are also sized at the whole shares the session's cash
     could buy, ignoring the risk-per-trade rule.
   - This separates "is there a signal edge?" from "can ₹500 take it?".
5. **Cost scenarios**: base; slippage ×2 and ×4; spread ×2 and ×3; targets
   needing 0.05% trade-through; and conservative (×2, ×2, trade-through).
   - Each scenario is re-simulated, so sizing and rejections use the
     scenario's costs too.
   - Break-even: the extra cost per trade (₹, and % of traded value) that
     zeroes net expectancy, holding the trade set fixed. When net expectancy
     is already ≤ 0, the report gives the cost reduction needed instead.
6. **Parameter sensitivity** over the predeclared grid:
   - One-at-a-time values for: RV threshold, opening-range minutes, stop
     reference, minimum reward-to-risk, EMA filter, liquidity floor, and the
     spread filter (marked untestable).
   - A 4×4 grid of RV threshold × reward-to-risk, shown as a heatmap.
   - Each configuration runs once over the evaluated range, and trades are
     split into windows. Unlike the primary evaluation, capital is carried
     across windows.
   - Every configuration is listed.
   - Instability flags: sign flips, or trade-count jumps over 50%, between
     adjacent values.
   - Train-vs-test Spearman rank correlation per fold.
   - Selection bias: the best t-statistic, compared with the expected
     maximum of N no-edge configurations (≈ 1.9 for N = 22).
   - **Nothing is selected.**
7. **Regimes** (rules in the protocol):
   - Trend: proxy open→close ±0.75%.
   - Volatility and market-wide liquidity, each relative to the previous 20
     sessions' median.
   - Grouping by symbol, ₹10 price band, month and quarter.
   - Every group is shown with trades, net expectancy, costs and drawdown.
     Groups with fewer than 10 trades are marked "not interpretable".
8. **Comparison baselines** (predeclared, same OOS sessions, capital and
   costs):
   - No-trade.
   - Cash retention (no interest modelled).
   - Passive liquid long: buy the most liquid eligible stock at 09:30 and
     sell at 15:10.
   - Benchmark proxy (NIFTYBEES, same rule, if fetched).

## Statistics (`app/robustness/stats.py`)

- **Trade metrics** reuse Phase 4. Profit factor is `None` with no losing
  trades, never infinity. Win rate alone is never used as evidence.
- **Per-session P&L** includes zero-trade sessions. The mean/sd ratio is
  **not annualised**: trades are few, irregular and non-normal, so √250
  scaling would overstate precision.
- **Uncertainty**:
  - A circular **moving-block bootstrap over sessions**, with block length
    ⌈n^(1/3)⌉ and 5,000 resamples with a fixed seed.
  - Trades on the same day or nearby days move together, so they are not
    treated as independent.
  - The CI is for net expectancy per trade.
- **Sample size**: design effect = (bootstrap SE / naive SE)²; effective
  n = n / design effect; minimum detectable effect = (z₀.₉₇₅ + z₀.₈) × SE;
  trades needed for an effect d ≈ design effect × ((z₀.₉₇₅ + z₀.₈) × sd / d)².
- **Reading**:
  - CI above 0: positive evidence.
  - CI below 0: negative evidence.
  - CI inside ±₹1: no economically meaningful edge.
  - Otherwise: **lack of evidence**, which is not the same as evidence of no
    edge.

The 20–30 sessions of Phase 6 forward paper trading are an operational
check. They are far too few to prove profitability on their own.

## Final assessment (`app/robustness/assessment.py`)

| Criterion | Rule |
|---|---|
| C1 executable | out-of-sample trades exist under the frozen capital/risk rules |
| C2 CI above zero | bootstrap CI lower bound of net expectancy > 0 |
| C3 fold consistency | ≥ 60% of folds with trades have positive test expectancy |
| C4 conservative costs | net expectancy > 0 in the conservative scenario |

| Verdict | When |
|---|---|
| `PENDING_DATA` | no usable data or too few sessions for one fold |
| `FAILED_PREDEFINED_CRITERIA` | signals exist but none is executable (C1 fails), or the whole CI is below zero |
| `SUPPORTS_FURTHER_EVALUATION` | C1–C4 all pass. This means more forward paper trading is justified, nothing more. |
| `INCONCLUSIVE` | anything else, with each failing or non-evaluable criterion listed |

**Expected at ₹500:** the Phase 3 cost review rejects typical setups.
Minimum brokerage plus GST and assumed spread or slippage exceed the
₹2.50 risk budget, and the net reward-to-risk falls below 1.5. So the most
likely verdict for the frozen ₹500 baseline is `FAILED_PREDEFINED_CRITERIA`
(C1). The signal diagnostic still shows whether the signals themselves have
any gross edge worth studying under a different, separately frozen version.

## Outputs (`data/robustness/<baseline>/<run_id>/`)

| File | Contents |
|---|---|
| `report.md`, `report.html` | the 13-section Strategy Robustness Report (HTML has the charts inline) |
| `summary.json` | verdict, criteria, plan, OOS metrics, bootstrap, sample size, diagnostics, scenarios, break-even, versions |
| `data_quality.json` | the full data audit |
| `folds.csv` | every window of every fold |
| `oos_trades.csv`, `oos_rejected_signals.csv`, `oos_equity_daily.csv`, `oos_signal_diagnostics.csv` | out-of-sample ledgers |
| `cost_scenarios.csv`, `sensitivity_summary.csv`, `sensitivity_by_fold.csv` | sensitivity results |
| `regimes.csv`, `regime_sessions.csv`, `comparison_baselines.csv` | regime and baseline detail |
| `charts/*.svg` | OOS equity, drawdown, P&L distribution, fold, cost, heatmap and regime charts (an explicit "data unavailable" panel when empty) |
| `frozen_baseline.json` | copy of the baseline used |

The run ID hashes the frozen baseline, the current code hash, the dataset
ID, the plan, the universe file and the options. The files contain no
wall-clock time, so the same inputs give byte-identical output (tested).

## Tests (`tests/test_robustness.py`, synthetic data only)

The tests cover the following:

- chronological separation and fold boundaries;
- overlap rejection and insufficient data;
- warm-up rules;
- future-data leakage;
- candle-completion timing;
- freeze reproducibility, immutability, tamper and code-drift detection, and
  environment-override immunity;
- the Phase 4 `_with` environment fix;
- cost scaling, the spread-doubling and net identities, trade-through fills
  and break-even;
- ₹500 rejection with an untouched shadow diagnostic and whole-share
  affordability;
- open vs closed positions;
- bootstrap determinism, empty or insufficient samples, zero-loss profit
  factor, hand-checked metrics and sample-size formulas;
- selection-bias helpers;
- chained OOS equity;
- assessment rules;
- corporate-action truncation, missing and invalid sessions, point-in-time
  membership and limitation reporting;
- complete and selection-free sensitivity tables, and parallel = serial;
- end-to-end OOS reporting;
- byte-reproducible reports, no automatic parameter changes, and a PENDING
  report without data;
- CLI refusal on code drift;
- no order endpoints, and a fetch that only reads candles;
- the dashboard view.

These tests check implementation logic only. They are **not** evidence about
the strategy.
