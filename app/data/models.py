"""Market-data value types shared by every provider.

Conventions:
- Candle timestamps are timezone-aware and mark the *start* of the candle.
- A candle is complete only once ``start + interval <= now``. Strategies must
  never read OHLCV from an incomplete candle.
- Every dataset carries its ``DataMode`` and source so live, delayed,
  historical and simulated data can never be confused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

import numpy as np
import pandas as pd

CANDLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


# Fast equivalents of ``ts.dt.date`` for hot paths. ``.dt.date`` on a
# timezone-aware series converts every row to local time and builds Python
# date objects, which dominated backtest run time.

def day_mask(ts: pd.Series, day) -> np.ndarray:
    """Boolean array, same as ``ts.dt.date == day`` in the series' own
    timezone (compares UTC nanoseconds against the day's local bounds)."""
    arr = ts.array
    tz = getattr(arr, "tz", None)
    start, end = pd.Timestamp(day), pd.Timestamp(day + timedelta(days=1))
    if tz is not None:
        start, end = start.tz_localize(tz), end.tz_localize(tz)
    values = arr.asi8
    return (values >= start.value) & (values < end.value)


def local_wall_times(ts: pd.Series) -> np.ndarray:
    """Naive local wall-clock times (``datetime64[ns]``) in the series' own
    timezone. A ZoneInfo zone is converted by its IANA name, which pandas
    handles several times faster than the ZoneInfo object (same rules)."""
    arr = ts.array
    tz = getattr(arr, "tz", None)
    if tz is not None:
        key = getattr(tz, "key", None)
        arr = (arr.tz_convert(key) if key else arr).tz_localize(None)
    return np.asarray(arr, dtype="datetime64[ns]")


def local_day_keys(ts: pd.Series) -> np.ndarray:
    """Local calendar day of each row as ``datetime64[D]``.
    Equal keys <=> equal ``ts.dt.date``."""
    return local_wall_times(ts).astype("datetime64[D]")


class DataMode(StrEnum):
    LIVE = "live"
    DELAYED = "delayed"
    HISTORICAL = "historical"
    SIMULATED = "simulated"


class DataError(Exception):
    """Base class for market-data failures."""


class DataQualityError(DataError):
    """Data failed validation and must not be used."""

    def __init__(self, message: str, issues: list | None = None):
        super().__init__(message)
        self.issues = issues or []


class DataUnavailableError(DataError):
    """The provider cannot supply the requested data."""


@dataclass(frozen=True)
class Interval:
    """A candle interval: minutes (``5m``) or one trading day (``1d``)."""

    label: str
    minutes: int | None  # None for daily candles

    @property
    def is_daily(self) -> bool:
        return self.minutes is None

    @property
    def timedelta(self) -> timedelta:
        if self.minutes is None:
            raise ValueError("daily interval has no fixed timedelta")
        return timedelta(minutes=self.minutes)


_INTERVAL_RE = re.compile(r"^(\d+)(m|d)$")


def parse_interval(label: str) -> Interval:
    match = _INTERVAL_RE.match(label.strip().lower())
    if not match:
        raise ValueError(f"invalid interval {label!r}; use e.g. '1m', '5m', '15m', '1d'")
    count, unit = int(match.group(1)), match.group(2)
    if unit == "d":
        if count != 1:
            raise ValueError("only '1d' daily candles are supported")
        return Interval("1d", None)
    if count < 1 or count > 375:
        raise ValueError("minute interval must be between 1 and 375")
    return Interval(f"{count}m", count)


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    exchange: str = "NSE"
    series: str = "EQ"
    isin: str | None = None
    tradable: bool = True


@dataclass(frozen=True)
class Quote:
    symbol: str
    last_price: float
    timestamp: datetime
    mode: DataMode
    source: str
    bid: float | None = None
    ask: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    # `timestamp` is the exchange/provider time of the data (e.g. last trade
    # time); `received_at` is when this application received it.
    received_at: datetime | None = None

    @property
    def spread_pct(self) -> float | None:
        """Bid-ask spread as a percentage of the mid price, if both sides exist."""
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0:
            return None
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 100


@dataclass
class CandleSeries:
    symbol: str
    interval: Interval
    frame: pd.DataFrame
    mode: DataMode
    source: str
    retrieved_at: datetime
    warnings: list[str] = field(default_factory=list)

    def completed(self, now: datetime) -> pd.DataFrame:
        """Rows whose candle had fully closed at ``now``."""
        if self.frame.empty:
            return self.frame
        if self.interval.is_daily:
            # A daily candle is complete once its session date is in the past.
            # (normalize(ts) < local midnight) <=> (ts < local midnight)
            ts = self.frame["timestamp"]
            today = pd.Timestamp(now).tz_convert(ts.dt.tz).normalize()
            return self.frame[ts < today]
        end = self.frame["timestamp"] + self.interval.timedelta
        return self.frame[end <= pd.Timestamp(now)]

    @property
    def last_timestamp(self) -> pd.Timestamp | None:
        return None if self.frame.empty else self.frame["timestamp"].iloc[-1]
