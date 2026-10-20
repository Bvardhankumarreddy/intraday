# Paper trading (Phase 5)

> **Everything here is SIMULATED.** The paper engine never places, modifies
> or cancels real orders, and it doesn't see or manage positions in your
> broker account. Paper results are not evidence of real profitability.

## Commands

```bash
# Replay one session on a simulated clock (mock data, or historical CSVs with --data)
.venv/bin/python -m app.paper replay --date 2026-10-19
.venv/bin/python -m app.paper replay --date 2025-03-12 --data data/historical   # needs opt-ins below

# Live session with the configured provider (blocks until the session ends)
.venv/bin/python -m app.paper run

.venv/bin/python -m app.paper status [--date D]
.venv/bin/python -m app.paper report --date D [--export]   # JSON + Markdown in data/paper/reports/

.venv/bin/streamlit run app/dashboard.py                     # read-only paper dashboard
```

A replay or run on a date before 2026-10-19 needs
`cost_review.apply_current_rates_to_past_dates: true`, because fee rates
are verified only from that date. Historical CSVs need
`screening.require_spread: false`, because they contain no quotes.

## Live data: current status

| Item | Status |
|---|---|
| Adapter `app/data/groww_provider.py` | Implemented from Groww's published SDK docs, tested with fixtures in the documented formats |
| Tested against a live Groww account | **Partly.** On 2026-10-19, after market hours: login, quotes, 5-minute and daily candles all work. Not yet observed during market hours |
| Prerequisites | Paid Groww Trading API subscription (the free trial excludes live quotes and candles); `growwapi` and `pyotp` (in requirements.txt); in `.env`: `NSE_BROKER_API_KEY` (TOTP token) + `NSE_BROKER_TOTP_SECRET` (**recommended, no daily approval**), or `NSE_BROKER_API_KEY` + `NSE_BROKER_API_SECRET` (approval key, approve daily); symbols in `config/universe.csv`; `data.provider: groww` |
| Groww facts relied on | quotes one symbol per call (bid/ask, depth, `last_trade_time` in ms); 5-min candles ≤ 15 days per request, last 3 months only; daily candles full history; live data 10 req/s, 300/min |
| Observed on the live API | candle timestamps are candle **start** times; intraday candle volume is **cumulative** for the session (converted to per-candle); `last_trade_time` is in **seconds** (docs say ms); the **15:20 candle is missing** every day checked (reported as a gap, never filled); `get_historical_candle_data` (deprecated) still works |

Missing prerequisites stop the run with an error. It never falls back to
mock or historical data. The SDK wrapper exposes only `quote` and `candles`,
and a test checks that no order code exists anywhere in `app/`.

## Session workflow (`app/paper/scheduler.py`)

1. **Instance lock:** an exclusive lock file prevents two schedulers on the
   same database.
2. **Calendar:** the NSE holiday list must cover the year, and the day must
   be a trading day. Special sessions such as Muhurat are recognised once
   configured, but the strategy doesn't trade them: its times are defined
   for regular sessions.
3. **Preflight:** checks fee rates for the date and that the day is a
   trading day.
4. **Open or recover the session:** the portfolio is created once with
   ₹500 and never reset. The cash ledger is verified on every start, and
   any mismatch stops the engine.
5. **Every candle close + `grace_seconds`:** `PaperEngine.step(now)`.
6. **After 15:30:** leftover orders expire, the ledger is reconciled and
   the daily report is written.

Failed steps are logged and retried at the next candle, up to
`max_consecutive_failures` in a row, and then the scheduler stops. State is
persisted, so a restart resumes where it stopped.

## Order lifecycle (`app/paper/models.py`)

```
CREATED -> PENDING -> FILLED | EXPIRED | REJECTED | CANCELLED
CREATED -> REJECTED | CANCELLED
position: OPEN -> CLOSED;  OPEN <-> FLAGGED (data missing);  FLAGGED -> CLOSED
```

- **IDs:** `PAPER-<hash(signal, side)>`, so one signal can create at most
  one BUY and one SELL. The database enforces this with a UNIQUE
  constraint.
- **Partial fills:** not implemented. There's no reliable depth history,
  and orders are a few shares.

## Execution model `paper-v1` (`app/paper/execution.py`)

| Event | Price source and timestamp |
|---|---|
| Entry with a fresh quote | the **ask**, at the quote's exchange timestamp (quote at/after the signal, ≤ `max_staleness_seconds` old). The spread is in the price, so its cost line is 0; slippage still applies |
| Entry without a usable quote | **open** of the next completed candle (Phase 4 `candle-v1`), half the assumed spread charged |
| Entry rejected | price ≤ stop, or > entry reference + `max_entry_gap_pct`; order **expires** if unfilled by `valid_until` |
| Stop / target / gap / ambiguous | completed 5-minute candles only, Phase 4 rules (stop first when both are touched) |
| Time exit | open of the first candle at/after 15:10 (or the max holding time) |
| Data missing for an open position | position **FLAGGED** with a warning. No exit is assumed. It resolves later from real candles if they arrive |

Every fill records its price source and timestamp in `fill_basis`.

## Accounting

- **Cash:** the account balance. **Reserved:** cash earmarked for pending
  entries. **Available** = cash − reserved.
- **Entry:** cash −= qty × price + buy fees + buy spread + buy slippage.
- **Exit:** cash += qty × price − sell fees − sell spread − sell slippage.
- **Realized P&L:** net of all costs, booked only at exit.
- **Unrealized P&L:** qty × (mark − entry), gross, shown separately and
  never counted as realized.
- **Equity** = cash + marked value of open positions. **Daily P&L** =
  equity − session start cash.
- **Ledger check:** cash = initial capital + Σ closed net P&L − Σ buy cash
  of open positions.
- **Missing values:** an unknown realized P&L is reported as unknown
  (never zero) and makes the risk check `INSUFFICIENT_DATA`.

## Persistence (`app/paper/store.py`, SQLite)

The database has these tables: portfolio, sessions (with data mode and all
version IDs), signals, orders, positions (with the full cost breakdown),
events (health, risk, recovery, ops), equity snapshots, watchlist and
reports. Critical changes run in transactions. A failure mid-change rolls
everything back, and a test injects one.

## Data health

On every step, each symbol's last completed candle is compared with the
time it should have reached. Changes of state (OK / STALE / ERROR) are
stored as events. If no symbol is fresh, **new signals are blocked**.
Existing positions are still monitored, and are flagged if their data
stops.

## Limitations

- **Live data is unverified** until it's run against a subscribed Groww
  account.
- **Exits are evaluated on completed 5-minute candles,** so a stop is
  detected up to 5 minutes late (the fill price is still the stop or the
  gap open).
- **No partial fills, queue position or market impact.**
- **Mock data rarely produces signals.** Its volume is random.
- **Special sessions are not traded.**
- **Fee rates before 2026-10-19 need the retroactive opt-in.**
