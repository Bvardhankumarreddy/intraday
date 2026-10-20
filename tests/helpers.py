"""Synthetic market-data builders for tests. All data here is invented."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from app.config import Settings
from app.data.memory_provider import InMemoryProvider
from app.data.models import DataMode, Instrument, Quote
from app.scan.snapshot import build_snapshot, lookback_start
from app.utils.market_calendar import MarketCalendar

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2025, 3, 12)  # a Wednesday; 2025-03-14 is the test holiday


def at(hhmm: str, day: date = DAY) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def bars(day: date, rows: list[tuple[float, float, float, float, int]], start="09:15", step=5):
    """5-minute candles from `start`: rows are (open, high, low, close, volume)."""
    ts = pd.date_range(pd.Timestamp(at(start, day)), periods=len(rows), freq=f"{step}min")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"]).assign(
        timestamp=ts
    )[["timestamp", "open", "high", "low", "close", "volume"]]


def flat_session(day: date, price=70.0, volume=1000, n=75) -> pd.DataFrame:
    return bars(day, [(price, price + 0.1, price - 0.1, price, volume)] * n)


def daily(days: list[date], close=70.0, volume=1_000_000) -> pd.DataFrame:
    ts = [pd.Timestamp(at("09:15", d)) for d in days]
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": volume,
        }
    )


def orb_today(
    breakout_volume=5000,
    breakout_close=71.0,
    breakout_low=70.4,
    breakout_high=None,
    or_high=70.5,
    or_low=69.5,
    base_volume=1000,
    after: list[tuple] | None = None,
) -> list[tuple]:
    """09:15-09:30 opening range, a quiet 09:30 candle, then a breakout candle
    at 09:35 (closes 09:40), then optional later candles."""
    rows = [
        (70.0, or_high, 69.8, 70.2, base_volume),
        (70.2, 70.3, or_low, 69.9, base_volume),
        (69.9, 70.4, 69.7, 70.3, base_volume),
        (70.3, 70.45, 70.1, 70.4, base_volume),  # 09:30, no breakout
        (
            max(70.4, breakout_low),
            breakout_high if breakout_high is not None else breakout_close + 0.05,
            breakout_low,
            breakout_close,
            breakout_volume,
        ),  # 09:35 breakout
    ]
    return rows + (after or [])


def make_provider(
    calendar: MarketCalendar,
    settings: Settings,
    today_rows: list[tuple] | None = None,
    symbol="TEST",
    prior_volume=1000,
    daily_close=70.0,
    daily_volume=1_000_000,
    daily_sessions: int | None = None,
    quote: Quote | None | str = "default",
    instrument: Instrument | None = None,
    mode=DataMode.SIMULATED,
    prior_sessions: int | None = None,
    prior_price=70.0,
) -> InMemoryProvider:
    n_prior = prior_sessions if prior_sessions is not None else (
        settings.strategy.relative_volume_lookback_sessions
    )
    prior_days = (
        calendar.trading_days(lookback_start(calendar, DAY, n_prior), DAY)[:-1] if n_prior else []
    )
    frames = [flat_session(d, price=prior_price, volume=prior_volume) for d in prior_days]
    if today_rows:  # an empty list means "no candles yet today"
        frames.append(bars(DAY, today_rows))
    intraday = pd.concat(frames, ignore_index=True) if frames else None

    n_daily = daily_sessions if daily_sessions is not None else (
        settings.screening.liquidity_lookback_sessions
    )
    daily_days = calendar.trading_days(lookback_start(calendar, DAY, n_daily), DAY)[:-1]
    candles = {}
    if intraday is not None:
        candles[(symbol, settings.strategy.candle_interval)] = intraday
    if daily_days:
        candles[(symbol, "1d")] = daily(daily_days, daily_close, daily_volume)

    quotes = {}
    if quote == "default":
        quotes[symbol] = Quote(
            symbol=symbol, last_price=daily_close, timestamp=at("09:15"), mode=mode,
            source="test", bid=daily_close - 0.02, ask=daily_close + 0.02,
            bid_qty=500, ask_qty=500,
        )
    elif quote is not None:
        quotes[symbol] = quote
    return InMemoryProvider(
        calendar,
        [instrument or Instrument(symbol, f"{symbol} Ltd")],
        candles,
        quotes,
        mode=mode,
    )


def snapshot(provider, settings, calendar, as_of, symbol="TEST"):
    return build_snapshot(provider, provider.get_instrument(symbol), settings, calendar, as_of)
