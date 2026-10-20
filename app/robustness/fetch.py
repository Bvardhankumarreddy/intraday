"""Fetch historical candles from Groww into the CSV layout used by
``load_csv_dataset`` (read-only market data; no order endpoints exist in the
client this uses).

Writes to ``<out>/``:
- ``instruments.csv``: symbol,name (the strategy universe plus the benchmark)
- ``<SYMBOL>_5m.csv`` / ``<SYMBOL>_1d.csv``: timestamp (ISO, IST), OHLCV
- ``manifest.json``: provider, SDK version, fetch time, requested ranges,
  per-symbol row counts and failures, transformations, the universe file's
  hash and what is unknown about the data.

Rules:
- Nothing is filled, adjusted or invented. A symbol Groww returns nothing
  for is recorded as failed.
- 5-minute rows that fail Groww-side validation (e.g. one bad session) are
  still saved as received (after volume differencing), so per-session
  validation in the dataset loader excludes exactly the bad sessions, with
  reasons.
- Groww keeps ~3 months of 5-minute history. Earlier requests return no data
  and are reported as such.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from app.config import PROJECT_ROOT
from app.data.models import CANDLE_COLUMNS, DataError, DataQualityError, Instrument
from app.utils.market_calendar import MarketCalendar

ADJUSTMENT_NOTE = ("Groww's documentation does not say whether historical candles are adjusted "
                   "for splits, bonuses or dividends. Treat the adjustment status as unknown; "
                   "large overnight moves are flagged by the data audit.")


def _write(frame: pd.DataFrame, path: Path) -> int:
    out = frame[CANDLE_COLUMNS].copy()
    out["timestamp"] = out["timestamp"].map(lambda t: t.isoformat())
    out.to_csv(path, index=False)
    return len(out)


def daily_start(calendar: MarketCalendar, start: date, sessions: int) -> date:
    """Calendar date early enough to hold ``sessions`` trading days before ``start``."""
    day, found = start, 0
    while found < sessions:
        day -= timedelta(days=1)
        if calendar.is_trading_day(day) or not calendar.holidays_loaded:
            found += 1
    return day - timedelta(days=7)  # margin for unlisted holidays


def fetch_history(provider, instruments: list[Instrument], start: date, end: date,
                  out_dir: Path, calendar: MarketCalendar, liquidity_sessions: int,
                  universe_file: Path | None, fetched_at: datetime, progress=print) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    d_start = daily_start(calendar, start, liquidity_sessions)
    rows, failures = {}, {}
    for inst in instruments:
        sym = inst.symbol
        entry = {}
        try:
            series = provider.get_candles(sym, "5m", start, end)
            frame, entry["5m_status"] = series.frame, "OK"
            entry["5m_warnings"] = series.warnings[:20]
        except DataQualityError as exc:
            frame = getattr(provider, "last_raw", {}).get(sym)
            entry["5m_status"] = "SAVED_AS_RECEIVED_FAILED_VALIDATION" if frame is not None \
                else "FAILED"
            entry["5m_error"] = str(exc)[:500]
        except DataError as exc:
            frame, entry["5m_status"], entry["5m_error"] = None, "FAILED", str(exc)[:500]
        if frame is not None and not frame.empty:
            entry["5m_rows"] = _write(frame, out / f"{sym}_5m.csv")
            entry["5m_first"] = frame["timestamp"].iloc[0].isoformat()
            entry["5m_last"] = frame["timestamp"].iloc[-1].isoformat()
        try:
            daily = provider.get_candles(sym, "1d", d_start, end)
            entry["1d_rows"] = _write(daily.frame, out / f"{sym}_1d.csv")
            entry["1d_status"] = "OK"
        except DataError as exc:
            entry["1d_status"], entry["1d_error"] = "FAILED", str(exc)[:500]
        rows[sym] = entry
        if entry["5m_status"] == "FAILED":
            failures[sym] = entry.get("5m_error", "")
        progress(f"{sym}: 5m {entry['5m_status']} ({entry.get('5m_rows', 0)} rows), "
                 f"1d {entry['1d_status']} ({entry.get('1d_rows', 0)} rows)")
    fetched = [i for i in instruments if (out / f"{i.symbol}_5m.csv").exists()]
    pd.DataFrame({"symbol": [i.symbol for i in fetched],
                  "name": [i.name for i in fetched]}).to_csv(out / "instruments.csv", index=False)
    try:  # package metadata only: the SDK itself is imported solely by the provider
        from importlib.metadata import PackageNotFoundError, version

        sdk = version("growwapi")
    except PackageNotFoundError:
        sdk = "not installed"
    universe = None
    if universe_file and Path(universe_file).exists():
        p = Path(universe_file)
        universe = {"file": str(p.relative_to(PROJECT_ROOT)) if p.is_relative_to(PROJECT_ROOT)
                    else str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                    "note": "fetch-time list, not point-in-time constituents"}
    manifest = {
        "provider": getattr(provider, "name", type(provider).__name__),
        "sdk_version": sdk, "fetched_at": fetched_at.isoformat(),
        "requested": {"5m": [start, end], "1d": [d_start, end]},
        "symbols": rows, "failed_symbols": failures, "universe": universe,
        "transformations": [
            "5m volume converted from Groww's cumulative session volume to per-candle volume "
            "(first candle keeps its value)",
            "timestamps are candle starts in IST (Asia/Kolkata)",
            "daily candles stamped at the session open",
        ],
        "adjustment_note": ADJUSTMENT_NOTE,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True,
                                                  default=str) + "\n")
    return manifest
