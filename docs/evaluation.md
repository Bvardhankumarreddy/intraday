# Live paper-trading evaluation (Phase 6)

> **This application never places, modifies or cancels real orders.** It
> reads market data, simulates paper orders and records what happened.
> Every real trade is yours to place manually. Paper results are
> simulations and are not evidence of profitability, however good they look.

## One-time setup

1. **Credentials** in `.env` (never in source, YAML, logs or reports):
   `NSE_BROKER_API_KEY` (TOTP token) and `NSE_BROKER_TOTP_SECRET`. See
   `.env.example`.
2. **Live data provider:** create `config/settings.yaml` from the example
   and set `data.provider: groww`, or export `NSE_DATA__PROVIDER=groww`.
3. **Stocks to watch:** list them in `config/universe.csv` (`symbol,name`).
4. **Holidays:** `config/nse_holidays.yaml` must cover the year. 2026 is
   included, from NSE's official list.
5. **Start the evaluation.** This freezes the current configuration:
   ```bash
   .venv/bin/python -m app.evaluation start --name "ORB initial 20" --from 2026-10-21
   ```
   Changing settings afterwards doesn't rewrite anything. Later sessions
   simply stop being *primary* and the change is logged in `config_changes`.

## Each trading day

```bash
.venv/bin/python -m app.evaluation run      # start before 09:15 IST; blocks until ~15:31
```

- **Stopping:** Ctrl+C. **Resuming:** run the same command again. State is
  in SQLite, so nothing is duplicated (signal and order IDs are
  deterministic).
- **On holidays and weekends** it exits immediately with a logged reason.
- **One instance only:** a lock file prevents a second copy.
- **If the app isn't running during a session,** that day is recorded as
  INCOMPLETE ("application did not run") at the next `run` or `sync`. It's
  never counted as a quiet no-trade day.

## Checking the data is live

Every session row, report and dashboard page shows the **data mode**:

| Mode | Session kind | Counts in primary results? |
|---|---|---|
| live (Groww) | LIVE | yes, if the config equals the frozen config |
| delayed | DELAYED | no |
| historical replay | REPLAY | no (replays also use a separate database) |
| mock | SIMULATED | no |

`python -m app.evaluation run` prints the provider and mode at start, and
warns when they aren't live.

## Reports

```bash
.venv/bin/python -m app.evaluation status          # sessions, funnel, net P&L, evidence label
.venv/bin/python -m app.evaluation report --date 2026-10-21
.venv/bin/python -m app.evaluation cumulative      # JSON + CSVs
.venv/bin/python -m app.evaluation reconcile --date 2026-10-21
.venv/bin/streamlit run app/dashboard.py           # "Evaluation" view in the sidebar
```

Files go to `data/evaluation/<evaluation_id>/`:
- `daily/<date>.json`: the 8-section daily report.
- `cumulative.json`: primary results and all-kinds results, plus config
  changes.
- `sessions_primary.csv`, `sessions_all.csv`, `trades.csv`,
  `signal_audit.csv`, `data_quality.csv`.

Reports are rebuilt from the database only, with no wall-clock time
inside, so regenerating them gives identical files (tested).

## Session status

| Status | Meaning |
|---|---|
| SCHEDULED | trading day not started yet |
| RUNNING | in progress |
| COMPLETED | ran to the end; ≥ 90% of 5-minute steps monitored, ≥ 95% of expected candles received, no unresolved positions, no reconciliation issues |
| INCOMPLETE | app didn't run or was interrupted, coverage below thresholds, unresolved positions, or reconciliation issues (the reason is recorded) |
| FAILED | preflight failed (e.g. missing fee schedule) or the scheduler aborted |

## Data-quality monitoring

**Every step and symbol:** the raw batch as received (trimmed to candles
completed by that moment) is measured. The checks are expected vs received
candles, missing, duplicate, out-of-order, invalid OHLC, invalid volume,
gaps, staleness and provider errors.

**Every step:** provider state (CONNECTED / DEGRADED / DISCONNECTED) and
retry count are recorded. If no symbol has fresh, valid data, **new
signals are blocked** and the reason is stored. Nothing is ever filled in.

Groww's 5-minute feed omits the 15:20 candle. It's counted as missing.
That doesn't affect trading (exits happen by 15:10), and one missing
candle per symbol stays within the 95% data-coverage threshold.

## Signal audit

There's one row per eligible symbol per step, with:
- opening-range high and low, VWAP, EMA, relative volume and its inputs
  (today's window volume and the prior-session baseline);
- entry, stop and target, planned quantity, estimated round-trip costs and
  net reward/risk;
- the risk-check status and reasons, and the decision.

Decisions:

| Decision | Meaning |
|---|---|
| NO_SETUP_YET | no setup yet, still within the signal window |
| NO_VALID_SETUP | no valid breakout by the cutoff |
| INSUFFICIENT_DATA | the setup couldn't be evaluated |
| SIGNAL_REJECTED_RISK | rejected by the Phase 3 risk and cost checks |
| NOT_ORDERED_CASH | not enough available cash for the order |
| ORDER_CREATED | a paper order was created |
| ALREADY_PROCESSED | this signal was handled at an earlier step |
| SETUP_NO_LONGER_VALID | the setup expired or was invalidated |

Each stage is recorded separately: signal → rejected or order → fill →
closed position. A generated signal is not a trade.

## Reconciliation (never auto-repaired)

These are checked at every session end, or on demand with
`python -m app.evaluation reconcile`:
- the cash and reserved-cash ledger;
- gross, total costs and net per trade;
- fees recomputed from the dated fee schedule;
- spread counted once (zero for quote fills, half the assumed spread for
  candle fills);
- order quantities and prices against positions;
- no fill before its signal and no exit before its entry;
- fills inside their candle's range;
- equity = cash + positions in every snapshot;
- report totals against trades.

Issues are stored, shown in the daily report and logged as errors. The
session becomes INCOMPLETE and the affected trades are excluded from
metrics, but they're still listed.

## Metric definitions

Metrics come from **eligible closed trades** in **primary sessions** (LIVE,
frozen config). Open, flagged, unfilled, rejected or expired items are
never trades.

| Metric | Definition |
|---|---|
| win / loss | net P&L > 0 / < 0; exactly 0 counts as neither |
| win rate | wins ÷ eligible trades (none if there are no trades) |
| expectancy | Σ net ÷ eligible trades (= average net per trade) |
| average win / loss | mean net P&L of wins / of losses |
| profit factor | Σ win net ÷ \|Σ loss net\|; **undefined** with no losses (never ∞) |
| max drawdown | peak-to-trough of equity (cash + marked positions) from every stored step, in time order |
| holding time | mean of exit time − entry time |
| sessions with trades | sessions with at least one closed trade |
| data-quality counts | sums of missing-candle batches, duplicates, out-of-order, invalid, stale and provider errors |
| evidence | "INSUFFICIENT SAMPLE" below `backtest.min_trades_for_evidence` (30) eligible trades |

No confidence intervals or profitability claims are made from small samples.

## Observation protocol

- **Window:** about 20–30 actual NSE sessions
  (`observation_target_sessions: 20`). This is an operational check, not a
  statistical proof. The evaluation keeps running beyond it until you run
  `python -m app.evaluation finish`.
- **Keep the configuration fixed.** If you must change it, the change is
  recorded and later sessions are excluded from the primary results.
- **Don't tune parameters** on individual wins or losses.
- **Every scheduled session is reported,** including incomplete and
  no-trade days.
- **There's no automatic switch to real trading,** ever.

## Execution-simulation limitations

- **Fill prices:** without fresh bid/ask, entries fill at the next candle's
  open plus assumed spread (0.10%) and slippage (0.05% per fill). Real fills
  differ.
- **Exits:** stops and targets are evaluated on completed 5-minute candles,
  so a stop is detected up to 5 minutes late.
- **Both touched in one candle:** the stop is assumed first.
- **Not modelled:** partial fills, queue position and market impact.
- **₹500 at Groww:** with the default risk settings, most setups are
  rejected on costs (see `docs/cost_model.md`). A no-trade session is a
  valid outcome.
