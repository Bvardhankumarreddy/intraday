"""Point-in-time market snapshot for one instrument.

This is the only place a scan fetches data. Everything downstream (screener,
indicators, strategy, ranking) sees only what the snapshot exposes, and the
snapshot exposes only candles that had *completed* at ``as_of`` and quotes
timestamped at or before ``as_of``. That is the look-ahead boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from datetime import date, datetime

import pandas as pd

from app.config import Settings
from app.data.base_provider import MarketDataProvider
from app.data.models import (DataError, DataMode, Instrument, Interval, Quote, day_mask,
                             parse_interval)
from app.data.validation import validate_quote
from app.scan.models import Reason
from app.utils.market_calendar import MarketCalendar


@dataclass
class MarketSnapshot:
    instrument: Instrument
    as_of: datetime
    session_date: date
    interval: Interval
    mode: DataMode
    source: str
    # Completed intraday candles: prior sessions (for baselines) plus today.
    intraday: pd.DataFrame | None
    # Completed daily candles for sessions before `session_date`.
    daily: pd.DataFrame | None
    quote: Quote | None
    data_errors: list[Reason] = field(default_factory=list)
    quote_issue: str | None = None
    warnings: list[str] = field(default_factory=list)

    @cached_property
    def today(self) -> pd.DataFrame:
        """Completed intraday candles of the evaluated session."""
        if self.intraday is None:
            return pd.DataFrame()
        return self.intraday[day_mask(self.intraday["timestamp"], self.session_date)]

    @property
    def last_candle_end(self) -> datetime | None:
        if self.intraday is None or self.intraday.empty:
            return None
        return (self.intraday["timestamp"].iloc[-1] + self.interval.timedelta).to_pydatetime()


def lookback_start(calendar: MarketCalendar, day: date, sessions: int) -> date:
    start = day
    for _ in range(sessions):
        start = calendar.previous_trading_day(start)
    return start


def build_snapshot(
    provider: MarketDataProvider,
    instrument: Instrument,
    settings: Settings,
    calendar: MarketCalendar,
    as_of: datetime,
) -> MarketSnapshot:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    as_of = as_of.astimezone(calendar.tz)
    session_date = as_of.date()
    interval = parse_interval(settings.strategy.candle_interval)
    snap = MarketSnapshot(
        instrument=instrument,
        as_of=as_of,
        session_date=session_date,
        interval=interval,
        mode=provider.mode,
        source=provider.name,
        intraday=None,
        daily=None,
        quote=None,
    )

    intraday_start = lookback_start(
        calendar, session_date, settings.strategy.relative_volume_lookback_sessions
    )
    try:
        series = provider.get_candles(instrument.symbol, interval.label, intraday_start, session_date)
        snap.intraday = _to_exchange_tz(series.completed(as_of), calendar)
        snap.source = series.source
        snap.mode = series.mode
        snap.warnings.extend(series.warnings)
    except DataError as exc:
        snap.data_errors.append(Reason(code="intraday_data", message=str(exc)))

    daily_start = lookback_start(
        calendar, session_date, settings.screening.liquidity_lookback_sessions
    )
    try:
        series = provider.get_candles(
            instrument.symbol, "1d", daily_start, calendar.previous_trading_day(session_date)
        )
        snap.daily = _to_exchange_tz(series.completed(as_of), calendar)
    except DataError as exc:
        snap.data_errors.append(Reason(code="daily_data", message=str(exc)))

    try:
        quote = provider.get_quote(instrument.symbol)
    except DataError as exc:
        snap.quote_issue = str(exc)
    else:
        if quote.timestamp > as_of:
            snap.quote_issue = f"quote timestamp {quote.timestamp} is after evaluation time"
        else:
            report = validate_quote(quote, as_of, settings.data.max_staleness_seconds)
            if report.is_valid:
                snap.quote = quote
            else:
                snap.quote_issue = "; ".join(i.message for i in report.errors)
    return snap


def _to_exchange_tz(frame: pd.DataFrame, calendar: MarketCalendar) -> pd.DataFrame:
    frame = frame.reset_index(drop=True).copy()
    frame["timestamp"] = frame["timestamp"].dt.tz_convert(calendar.tz)
    return frame
