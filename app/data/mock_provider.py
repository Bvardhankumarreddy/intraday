"""Deterministic simulated market data for development and tests.

All symbols are fictional (prefixed ``SIM``) so simulated prices can never be
mistaken for real NSE instruments. Output depends only on the seed, symbol
and date, so any date range is reproducible regardless of how it is queried.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
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

TICK = 0.05
MOCK_EPOCH = date(2020, 1, 1)


@dataclass(frozen=True)
class MockProfile:
    instrument: Instrument
    base_price: float
    daily_vol: float  # standard deviation of daily log return
    avg_daily_volume: int  # shares
    spread_bps: float  # quoted bid-ask spread, basis points of mid


DEFAULT_PROFILES = [
    MockProfile(Instrument("SIMALPHA", "Simulated Alpha Ltd"), 72.0, 0.018, 4_000_000, 4),
    MockProfile(Instrument("SIMBETA", "Simulated Beta Ltd"), 88.0, 0.022, 2_500_000, 6),
    MockProfile(Instrument("SIMGAMMA", "Simulated Gamma Ltd"), 55.0, 0.025, 1_500_000, 8),
    # Illiquid: in the price band but far below the traded-value threshold.
    MockProfile(Instrument("SIMDELTA", "Simulated Delta Ltd"), 64.0, 0.030, 20_000, 40),
    # Outside the default 50-100 price band.
    MockProfile(Instrument("SIMEPSILON", "Simulated Epsilon Ltd"), 240.0, 0.015, 3_000_000, 3),
    MockProfile(Instrument("SIMZETA", "Simulated Zeta Ltd"), 18.0, 0.035, 9_000_000, 25),
    # Not tradable (e.g. suspended).
    MockProfile(
        Instrument("SIMETA", "Simulated Eta Ltd", tradable=False), 70.0, 0.02, 1_000_000, 10
    ),
]


def _round_tick(values: np.ndarray) -> np.ndarray:
    return np.round(np.round(values / TICK) * TICK, 2)


class MockMarketDataProvider(MarketDataProvider):
    name = "mock"
    mode = DataMode.SIMULATED

    def __init__(
        self,
        calendar: MarketCalendar,
        seed: int = 42,
        profiles: list[MockProfile] | None = None,
        as_of: datetime | None = None,
    ):
        self.calendar = calendar
        self.seed = seed
        self.profiles = {p.instrument.symbol: p for p in (profiles or DEFAULT_PROFILES)}
        # Fixed "current time" for quotes, so tests stay deterministic.
        self.as_of = as_of
        self._day_open_cache: dict[str, dict[date, float]] = {}

    def list_instruments(self) -> list[Instrument]:
        return [p.instrument for p in self.profiles.values()]

    def get_candles(self, symbol: str, interval: str, start: date, end: date) -> CandleSeries:
        profile = self._profile(symbol)
        iv = parse_interval(interval)
        if start > end:
            raise ValueError("start must not be after end")
        if start < MOCK_EPOCH:
            raise DataUnavailableError(f"mock data starts at {MOCK_EPOCH}")

        frames = [self._minute_bars(profile, d) for d in self.calendar.trading_days(start, end)]
        if not frames:
            raise DataUnavailableError(f"no trading days between {start} and {end}")
        frame = self._aggregate(pd.concat(frames, ignore_index=True), iv)

        report = validate_candles(frame, iv, self.calendar)
        if not report.is_valid:  # would indicate a bug in the generator
            raise DataQualityError(f"mock data failed validation for {symbol}", report.errors)
        return CandleSeries(
            symbol=symbol,
            interval=iv,
            frame=frame,
            mode=self.mode,
            source=f"mock(seed={self.seed})",
            retrieved_at=self.as_of or datetime.now(self.calendar.tz),
            warnings=[i.message for i in report.warnings],
        )

    def get_quote(self, symbol: str) -> Quote:
        """Quote built from the last completed 1-minute bar at ``as_of``."""
        profile = self._profile(symbol)
        if self.as_of is None:
            raise DataUnavailableError("mock quotes require a fixed as_of time")
        now = self.as_of.astimezone(self.calendar.tz)
        day = now.date()
        if not self.calendar.is_trading_day(day) or now < self.calendar.session_bounds(day)[0]:
            day = self.calendar.previous_trading_day(day)
        bars = self._minute_bars(profile, day)
        done = bars[bars["timestamp"] + pd.Timedelta(minutes=1) <= pd.Timestamp(now)]
        if done.empty:
            raise DataUnavailableError(f"no completed bars for {symbol} at {now}")
        last = done.iloc[-1]
        # Simulated half-spread in paise. Real tick sizes come from the exchange
        # and are not modelled here.
        half = max(0.01, round(last["close"] * profile.spread_bps / 20_000, 2))
        return Quote(
            symbol=symbol,
            last_price=float(last["close"]),
            timestamp=(last["timestamp"] + pd.Timedelta(minutes=1)).to_pydatetime(),
            mode=self.mode,
            source=f"mock(seed={self.seed})",
            bid=round(float(last["close"]) - half, 2),
            ask=round(float(last["close"]) + half, 2),
            bid_qty=int(profile.avg_daily_volume // 2000),
            ask_qty=int(profile.avg_daily_volume // 2000),
        )

    # -- generation ---------------------------------------------------------

    def _profile(self, symbol: str) -> MockProfile:
        try:
            return self.profiles[symbol]
        except KeyError:
            raise DataUnavailableError(f"unknown mock symbol {symbol!r}") from None

    def _rng(self, symbol: str, day: date) -> np.random.Generator:
        key = zlib.crc32(f"{symbol}:{day.isoformat()}".encode())
        return np.random.default_rng([self.seed, key])

    def _day_open(self, profile: MockProfile, day: date) -> float:
        """Open price for ``day``: a daily random walk from MOCK_EPOCH."""
        symbol = profile.instrument.symbol
        cache = self._day_open_cache.setdefault(symbol, {})
        if day in cache:
            return cache[day]
        days = self.calendar.trading_days(MOCK_EPOCH, day)
        price = profile.base_price
        for d in days:
            if d in cache:
                price = cache[d]
                continue
            cache[d] = price
            price *= float(np.exp(self._open_to_open_return(profile, d, price)))
        return cache[day]

    def _open_to_open_return(self, profile: MockProfile, day: date, day_open: float) -> float:
        # Mean-revert towards the base price so the price band stays realistic.
        drift = -0.02 * np.log(day_open / profile.base_price)
        return drift + self._rng(profile.instrument.symbol, day).normal(0, profile.daily_vol)

    def _minute_bars(self, profile: MockProfile, day: date) -> pd.DataFrame:
        open_dt, close_dt = self.calendar.session_bounds(day)
        n = int((close_dt - open_dt).total_seconds() // 60)
        day_open = self._day_open(profile, day)
        rng = self._rng(profile.instrument.symbol, day)
        rng.normal()  # first draw is the open-to-open return
        overnight_gap = rng.normal(0, profile.daily_vol * 0.3)
        target = self._open_to_open_return(profile, day, day_open) - overnight_gap

        # Brownian bridge: random intraday path that ends at `target`, so the
        # next session opens `overnight_gap` away from today's close.
        sigma = profile.daily_vol / np.sqrt(n)
        path = np.cumsum(rng.normal(0, sigma, n))
        path += (np.arange(1, n + 1) / n) * (target - path[-1])
        closes = day_open * np.exp(path)
        opens = np.concatenate([[day_open], closes[:-1]])
        wick = np.abs(rng.normal(0, sigma * 0.6, (2, n)))
        highs = np.maximum(opens, closes) * (1 + wick[0])
        lows = np.minimum(opens, closes) * (1 - wick[1])

        # U-shaped intraday volume profile: busier at the open and close.
        x = np.linspace(-1, 1, n)
        shape = 1 + 1.5 * x**2
        expected = profile.avg_daily_volume * shape / shape.sum()
        volume = np.round(expected * rng.lognormal(0, 0.5, n)).astype(np.int64)

        o, c = _round_tick(opens), _round_tick(closes)
        h = np.maximum.reduce([_round_tick(highs), o, c])
        lo = np.minimum.reduce([_round_tick(lows), o, c])
        return pd.DataFrame(
            {
                "timestamp": pd.date_range(open_dt, periods=n, freq="1min"),
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": volume,
            }
        )

    def _aggregate(self, minutes: pd.DataFrame, iv) -> pd.DataFrame:
        if iv.minutes == 1:
            return minutes
        ts = minutes["timestamp"]
        session_start = ts.dt.normalize() + pd.Timedelta(
            hours=self.calendar.config.session_open.hour,
            minutes=self.calendar.config.session_open.minute,
        )
        if iv.is_daily:
            bucket = session_start
        else:
            offset = (ts - session_start) // pd.Timedelta(minutes=iv.minutes)
            bucket = session_start + offset * pd.Timedelta(minutes=iv.minutes)
        grouped = minutes.groupby(bucket, sort=True)
        out = pd.DataFrame(
            {
                "open": grouped["open"].first(),
                "high": grouped["high"].max(),
                "low": grouped["low"].min(),
                "close": grouped["close"].last(),
                "volume": grouped["volume"].sum(),
            }
        )
        out.index.name = "timestamp"
        return out.reset_index()
