"""Intraday indicators on completed candles.

All functions take candle frames whose ``timestamp`` is the candle start in
exchange time, sorted ascending, with no duplicates (guaranteed by the data
providers' validation). Unavailable values are NaN / None, never zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
import pandas as pd

from app.config import RelativeVolumeMode
from app.data.models import Interval, local_day_keys, local_wall_times


def session_vwap(frame: pd.DataFrame) -> pd.Series:
    """VWAP from each session's open, using typical price (H+L+C)/3.

    Resets at every session. NaN until the session has traded volume.
    """
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3
    session = local_day_keys(frame["timestamp"])
    pv = (typical * frame["volume"]).groupby(session).cumsum()
    vol = frame["volume"].groupby(session).cumsum()
    return (pv / vol.where(vol > 0)).rename("vwap")


def ema(close: pd.Series, period: int) -> pd.Series:
    """Exponential moving average, alpha = 2/(period+1), seeded with the
    simple average of the first ``period`` values. NaN before that.

    Runs continuously across sessions (yesterday's candles warm it up).
    """
    values = close.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) >= period:
        alpha = 2 / (period + 1)
        out[period - 1] = values[:period].mean()
        for i in range(period, len(values)):
            out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return pd.Series(out, index=close.index, name=f"ema{period}")


@dataclass(frozen=True)
class OpeningRange:
    high: float
    low: float
    end: datetime  # first moment after the range

    @property
    def width(self) -> float:
        return self.high - self.low

    @property
    def width_pct(self) -> float:
        """Width as % of the range midpoint."""
        return self.width / ((self.high + self.low) / 2) * 100


def opening_range(
    today: pd.DataFrame, session_open: datetime, minutes: int, interval: Interval
) -> OpeningRange | None:
    """High/low of the first ``minutes`` of the session.

    Returns None until every candle in the window is present and completed.
    """
    if interval.is_daily or minutes % interval.minutes:
        raise ValueError("opening range must be a whole number of intraday candles")
    expected = minutes // interval.minutes
    end = pd.Timestamp(session_open) + pd.Timedelta(minutes=minutes)
    window = today[today["timestamp"] < end]
    if len(window) != expected:
        return None
    # Every candle start must be exactly open + k x interval (UTC nanoseconds).
    starts = pd.Timestamp(session_open).value + np.arange(expected, dtype=np.int64) \
        * int(interval.timedelta.total_seconds() * 1_000_000_000)
    if not (window["timestamp"].array.asi8 == starts).all():
        return None
    return OpeningRange(float(window["high"].max()), float(window["low"].min()), end.to_pydatetime())


@dataclass(frozen=True)
class RelativeVolume:
    ratio: float | None
    sessions: int
    today_volume: float | None  # today's window volume (numerator)
    baseline_volume: float | None  # mean window volume of prior sessions (denominator)


def relative_volume(
    frame: pd.DataFrame,
    session_date: date,
    candle_start: pd.Timestamp,
    lookback_sessions: int,
    mode: RelativeVolumeMode,
) -> tuple[float | None, int]:
    """Ratio and sessions used; see ``relative_volume_detail``."""
    rv = relative_volume_detail(frame, session_date, candle_start, lookback_sessions, mode)
    return rv.ratio, rv.sessions


def relative_volume_detail(
    frame: pd.DataFrame,
    session_date: date,
    candle_start: pd.Timestamp,
    lookback_sessions: int,
    mode: RelativeVolumeMode,
) -> RelativeVolume:
    """Volume up to ``candle_start`` today vs the same time of day in prior
    completed sessions.

    ``CUMULATIVE``: volume from the open through this candle, vs the same
    window in each prior session. ``CANDLE``: this candle's volume vs the
    candle in the same time slot.

    A prior session counts only if it has data at or beyond this time slot (so
    a truncated day does not drag the baseline down); in CANDLE mode it must
    have the exact slot. Returns ``(ratio, sessions_used)``; ratio is None when
    no session qualifies or the baseline volume is zero. The inputs (today's
    volume and the baseline mean) are returned for the audit trail.
    """
    # One grouped pass over all sessions (time of day as minutes since
    # midnight), in numpy: sessions are local calendar days.
    local = local_wall_times(frame["timestamp"])
    days = local.astype("datetime64[D]")
    minute = ((local - days) // np.timedelta64(1, "m")).astype(np.int64)
    slot = candle_start.hour * 60 + candle_start.minute
    volume = frame["volume"].to_numpy(dtype=float)

    uniq, inv = np.unique(days, return_inverse=True)  # sorted session days
    n = len(uniq)
    last_minute = np.full(n, -1, dtype=np.int64)
    np.maximum.at(last_minute, inv, minute)
    reaches_slot = last_minute >= slot  # session has data at/after slot
    if mode is RelativeVolumeMode.CANDLE:
        at_slot = minute == slot
        has_slot = np.bincount(inv, weights=at_slot, minlength=n) > 0
        window = np.bincount(inv, weights=np.where(at_slot, volume, 0.0), minlength=n)
        usable = reaches_slot & has_slot
    else:
        window = np.bincount(inv, weights=np.where(minute <= slot, volume, 0.0), minlength=n)
        usable = reaches_slot

    i = int(np.searchsorted(uniq, np.datetime64(session_date, "D")))
    if i >= n or uniq[i] != np.datetime64(session_date, "D") or not usable[i]:
        return RelativeVolume(None, 0, None, None)
    today_value = float(window[i])

    prior = range(max(0, i - lookback_sessions), i)  # the last N sessions before today
    baseline = [float(window[j]) for j in prior if usable[j]]
    if not baseline:
        return RelativeVolume(None, 0, today_value, None)
    mean = float(np.mean(baseline))
    if mean <= 0:
        return RelativeVolume(None, len(baseline), today_value, mean)
    return RelativeVolume(today_value / mean, len(baseline), today_value, mean)
