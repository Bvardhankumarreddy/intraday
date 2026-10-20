"""Historical OHLCV from local CSV files.

Expected layout (see docs/setup.md)::

    <historical_dir>/instruments.csv       symbol,name[,series,isin,tradable]
    <historical_dir>/<SYMBOL>_<interval>.csv   timestamp,open,high,low,close,volume

Timestamps are candle *start* times. Timestamps without an offset are taken
as exchange-local time (IST) and a warning is attached. The files must come
from an authorized source; this provider only reads them.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd

from app.data.base_provider import MarketDataProvider
from app.data.models import (
    CANDLE_COLUMNS,
    CandleSeries,
    DataMode,
    DataQualityError,
    DataUnavailableError,
    Instrument,
    Quote,
    parse_interval,
)
from app.data.validation import validate_candles
from app.utils.market_calendar import MarketCalendar

_TRUE = {"1", "true", "yes", "y"}


class HistoricalCSVProvider(MarketDataProvider):
    name = "historical_csv"
    mode = DataMode.HISTORICAL

    def __init__(self, directory: Path, calendar: MarketCalendar, max_jump_pct: float = 20.0):
        self.directory = Path(directory)
        self.calendar = calendar
        self.max_jump_pct = max_jump_pct

    def list_instruments(self) -> list[Instrument]:
        path = self.directory / "instruments.csv"
        if not path.exists():
            raise DataUnavailableError(f"instrument list not found: {path}")
        table = pd.read_csv(path, dtype=str).fillna("")
        if "symbol" not in table.columns or "name" not in table.columns:
            raise DataQualityError(f"{path} must have 'symbol' and 'name' columns")
        if table["symbol"].duplicated().any():
            raise DataQualityError(f"{path} contains duplicate symbols")
        return [
            Instrument(
                symbol=row["symbol"].strip(),
                name=row["name"].strip(),
                series=row.get("series", "").strip() or "EQ",
                isin=row.get("isin", "").strip() or None,
                tradable=(row.get("tradable", "").strip().lower() in _TRUE)
                if row.get("tradable", "").strip()
                else True,
            )
            for _, row in table.iterrows()
        ]

    def get_candles(self, symbol: str, interval: str, start: date, end: date) -> CandleSeries:
        iv = parse_interval(interval)
        if start > end:
            raise ValueError("start must not be after end")
        path = self.directory / f"{symbol}_{iv.label}.csv"
        if not path.exists():
            raise DataUnavailableError(f"no {iv.label} data file for {symbol}: {path}")

        raw = pd.read_csv(path)
        missing = [c for c in CANDLE_COLUMNS if c not in raw.columns]
        if missing:
            raise DataQualityError(f"{path} is missing columns {missing}")
        frame = raw[CANDLE_COLUMNS].copy()
        warnings: list[str] = []

        try:
            stamps = pd.to_datetime(frame["timestamp"], format="ISO8601")
        except (ValueError, TypeError) as exc:
            raise DataQualityError(f"{path}: unparseable timestamps ({exc})") from exc
        if stamps.dt.tz is None:
            stamps = stamps.dt.tz_localize(self.calendar.tz)
            warnings.append("timestamps had no timezone; assumed exchange-local time (IST)")
        frame["timestamp"] = stamps.dt.tz_convert(self.calendar.tz)
        for col in ("open", "high", "low", "close", "volume"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")

        local_dates = frame["timestamp"].dt.date
        frame = frame[(local_dates >= start) & (local_dates <= end)].reset_index(drop=True)
        if frame.empty:
            raise DataUnavailableError(f"{path} has no candles between {start} and {end}")

        report = validate_candles(frame, iv, self.calendar, self.max_jump_pct)
        if not report.is_valid:
            details = "; ".join(i.message for i in report.errors)
            raise DataQualityError(f"{symbol} {iv.label} data rejected: {details}", report.errors)
        warnings.extend(i.message for i in report.warnings)

        return CandleSeries(
            symbol=symbol,
            interval=iv,
            frame=frame,
            mode=self.mode,
            source=f"csv:{path.name}",
            retrieved_at=datetime.now(self.calendar.tz),
            warnings=warnings,
        )

    def get_quote(self, symbol: str) -> Quote:
        raise DataUnavailableError(
            "historical provider has no live quotes; a live trade cannot be evaluated"
        )
