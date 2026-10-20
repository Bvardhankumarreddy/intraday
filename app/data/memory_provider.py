"""In-memory provider for supplied candles (tests, notebooks, replays)."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from app.data.base_provider import MarketDataProvider
from app.data.models import (
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


class InMemoryProvider(MarketDataProvider):
    name = "memory"

    def __init__(
        self,
        calendar: MarketCalendar,
        instruments: list[Instrument],
        candles: dict[tuple[str, str], pd.DataFrame],
        quotes: dict[str, Quote] | None = None,
        mode: DataMode = DataMode.SIMULATED,
        retrieved_at: datetime | None = None,
        prevalidated: bool = False,
    ):
        self.calendar = calendar
        self.instruments = instruments
        self.candles = candles  # keyed by (symbol, interval label)
        self.quotes = quotes or {}
        self.mode = mode
        self.retrieved_at = retrieved_at or datetime.now(calendar.tz)
        # Data already validated by the caller (e.g. a backtest dataset):
        # skip re-validation and cache repeated identical requests.
        self.prevalidated = prevalidated
        self._cache: dict[tuple, CandleSeries] = {}
        self.last_raw: dict[str, pd.DataFrame] = {}  # raw intraday batch, pre-validation

    def list_instruments(self) -> list[Instrument]:
        return list(self.instruments)

    def get_candles(self, symbol: str, interval: str, start: date, end: date) -> CandleSeries:
        key = (symbol, interval, start, end)
        if self.prevalidated and key in self._cache:
            cached = self._cache[key]
            if not cached.interval.is_daily:
                self.last_raw[symbol] = cached.frame
            return cached
        iv = parse_interval(interval)
        frame = self.candles.get((symbol, iv.label))
        if frame is None:
            raise DataUnavailableError(f"no {iv.label} candles for {symbol}")
        dates = frame["timestamp"].dt.tz_convert(self.calendar.tz).dt.date
        frame = frame[(dates >= start) & (dates <= end)].reset_index(drop=True)
        if frame.empty:
            raise DataUnavailableError(f"no {iv.label} candles for {symbol} in range")
        if not iv.is_daily:
            self.last_raw[symbol] = frame
        warnings: list[str] = []
        if not self.prevalidated:
            report = validate_candles(frame, iv, self.calendar)
            if not report.is_valid:
                details = "; ".join(i.message for i in report.errors)
                raise DataQualityError(f"{symbol} {iv.label} data rejected: {details}",
                                       report.errors)
            warnings = [i.message for i in report.warnings]
        series = CandleSeries(
            symbol=symbol,
            interval=iv,
            frame=frame,
            mode=self.mode,
            source=self.name,
            retrieved_at=self.retrieved_at,
            warnings=warnings,
        )
        if self.prevalidated:
            self._cache[key] = series
        return series

    def get_quote(self, symbol: str) -> Quote:
        if symbol not in self.quotes:
            raise DataUnavailableError(f"no quote for {symbol}")
        return self.quotes[symbol]
