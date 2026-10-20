"""Per-batch market-data quality assessment.

Each step, every symbol's raw intraday batch (as received, before
validation) is measured against what the session should contain by now:

- expected: session slots whose candle had completed at ``now``.
- received: distinct, in-session, valid candles among those slots.
- missing = expected - received.
- duplicates: rows sharing a timestamp. out_of_order: rows earlier than the
  previous row. invalid_ohlc: NaN, non-positive or inconsistent OHLC.
  invalid_volume: NaN or negative volume. gaps: jumps larger than one
  interval between consecutive in-session candles.
- stale: the latest valid candle ends more than ``max_staleness_seconds``
  before the latest slot that should have completed.

Nothing here alters data; it only counts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

import pandas as pd

from app.data.models import Interval
from app.utils.market_calendar import MarketCalendar


@dataclass(frozen=True)
class BatchQuality:
    symbol: str
    expected: int
    received: int
    missing: int
    duplicates: int
    out_of_order: int
    invalid_ohlc: int
    invalid_volume: int
    gaps: int
    last_candle_end: datetime | None
    stale: bool
    error: str | None = None

    @property
    def ok(self) -> bool:
        return (self.error is None and not self.stale and self.duplicates == 0
                and self.out_of_order == 0 and self.invalid_ohlc == 0
                and self.invalid_volume == 0)

    @property
    def incident(self) -> bool:
        return not self.ok or self.missing > 0

    def as_row(self) -> dict:
        return asdict(self)


def expected_slots(calendar: MarketCalendar, day: date, now: datetime,
                   interval: Interval) -> list[pd.Timestamp]:
    open_dt, close_dt = calendar.session_bounds(day)
    step = interval.timedelta
    slots, t = [], open_dt
    while t + step <= min(now, close_dt):
        slots.append(pd.Timestamp(t))
        t += step
    return slots


def assess_batch(symbol: str, raw: pd.DataFrame | None, day: date, now: datetime,
                 calendar: MarketCalendar, interval: Interval, max_staleness_seconds: int,
                 error: str | None = None) -> BatchQuality:
    slots = expected_slots(calendar, day, now, interval)
    expected = len(slots)
    if raw is None or raw.empty:
        return BatchQuality(symbol, expected, 0, expected, 0, 0, 0, 0, 0, None,
                            stale=expected > 0, error=error)

    ts = pd.to_datetime(raw["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(calendar.tz)
    ts = ts.dt.tz_convert(calendar.tz).reset_index(drop=True)
    o, h, lo, c = (pd.to_numeric(raw[k], errors="coerce").reset_index(drop=True)
                   for k in ("open", "high", "low", "close"))
    vol = pd.to_numeric(raw["volume"], errors="coerce").reset_index(drop=True)

    duplicates = int(ts.duplicated().sum())
    out_of_order = int((ts.diff() < pd.Timedelta(0)).sum())
    bad_ohlc = (o.isna() | h.isna() | lo.isna() | c.isna() | (o <= 0) | (h <= 0) | (lo <= 0)
                | (c <= 0) | (h < pd.concat([o, c, lo], axis=1).max(axis=1))
                | (lo > pd.concat([o, c], axis=1).min(axis=1)))
    bad_vol = vol.isna() | (vol < 0)
    valid = ~bad_ohlc & ~bad_vol & ~ts.duplicated()

    slot_set = set(slots)
    received_slots = {t for t, ok in zip(ts, valid) if ok and t in slot_set}
    in_session = sorted(t for t, ok in zip(ts, valid) if ok)
    step = interval.timedelta
    gaps = sum(1 for a, b in zip(in_session, in_session[1:])
               if a.date() == b.date() and b - a > step)
    last_end = (max(received_slots) + step).to_pydatetime() if received_slots else None
    stale = False
    if slots:
        latest_expected_end = slots[-1] + step
        tolerance = timedelta(seconds=max_staleness_seconds)
        stale = last_end is None or last_end < latest_expected_end - tolerance
    return BatchQuality(
        symbol=symbol, expected=expected, received=len(received_slots),
        missing=max(expected - len(received_slots), 0), duplicates=duplicates,
        out_of_order=out_of_order, invalid_ohlc=int(bad_ohlc.sum()),
        invalid_volume=int(bad_vol.sum()), gaps=gaps, last_candle_end=last_end,
        stale=stale, error=error,
    )
