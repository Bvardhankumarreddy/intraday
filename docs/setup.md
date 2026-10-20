# Setup and configuration

## Requirements

Python 3.12 or 3.13. (Docker setup comes in a later phase.)

## Configuration

1. `cp config/settings.example.yaml config/settings.yaml` and edit.
2. Optionally `cp .env.example .env`. Env vars override the YAML:
   `NSE_<SECTION>__<KEY>`, e.g. `NSE_CAPITAL__INITIAL_CAPITAL=1000`.
3. Secrets (API keys) go only in `.env` / the environment. The loader refuses
   them in YAML and never prints them.

Run commands from the project root; relative paths in the YAML resolve
against the current directory.

## NSE holidays

`config/nse_holidays.yaml` contains NSE's official 2026 cash-market list.
Add each new year from NSE's holiday list before trading it: the paper
scheduler refuses to run in a year with no entries. Announced special
sessions (e.g. Muhurat) go in `market.special_sessions`.

## Data providers

`data.provider` selects the source. Every dataset is labelled with its mode
(`simulated`, `historical`, `delayed`, `live`).

### mock (default)

Deterministic simulated candles for fictional `SIM*` symbols. Same seed,
symbol and date always give the same data. The set includes cases for later
screening tests: an illiquid stock (SIMDELTA), stocks outside the price band
(SIMEPSILON, SIMZETA) and a non-tradable stock (SIMETA).

### historical

CSV files in `data.historical_dir`, obtained from an authorized source:

```
instruments.csv          symbol,name[,series,isin,tradable]
<SYMBOL>_5m.csv          timestamp,open,high,low,close,volume
<SYMBOL>_1d.csv          same columns, one row per session
```

- `timestamp` is the candle **start** time, ISO 8601. Include the offset
  (`2025-03-12T09:15:00+05:30`); without one, IST is assumed and a warning
  is attached.
- Intraday candles must align to the interval from 09:15.
- Files that fail validation (missing values, impossible OHLC, duplicates,
  unsorted rows, non-trading days, candles outside the session) are rejected.
  Nothing is repaired automatically.
- This provider has no live quotes, so it cannot evaluate a live trade.

## Candle completeness

A candle starting at `t` is complete only at `t + interval`.
`CandleSeries.completed(now)` returns only completed candles. Later phases use
this to prevent look-ahead bias.
