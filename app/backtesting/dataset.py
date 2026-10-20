"""Historical dataset preparation for backtests.

Policy:
- Each session is validated on its own, using the Phase 1 validator. A
  session with any error (bad OHLC, duplicates, out-of-order rows, candles
  outside the session, a non-trading day, NaN) is EXCLUDED, never repaired.
- Missing candles (a slot with no row) and zero-volume candles (a row with
  volume 0) are counted separately. A session missing more than
  ``max_missing_candles_per_session`` slots is EXCLUDED as incomplete.
  Missing candles are never filled, so they can't create signals.
- Excluded sessions are removed entirely. Baselines (relative volume,
  liquidity) then see fewer sessions and may report INSUFFICIENT_DATA.
- Daily candles for the liquidity screen are aggregated from accepted
  intraday sessions (open/high/low/close/sum volume) unless a ``1d`` file
  exists.
- The dataset ID is a SHA-256 over the source bytes and the policy, so the
  same files and policy always give the same ID.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from app.config import Settings
from app.data.memory_provider import InMemoryProvider
from app.data.models import CANDLE_COLUMNS, DataMode, Instrument, parse_interval
from app.data.historical_provider import HistoricalCSVProvider
from app.data.validation import validate_candles
from app.utils.market_calendar import MarketCalendar


class DatasetError(ValueError):
    pass


@dataclass(frozen=True)
class SessionQuality:
    symbol: str
    session: date
    candles: int
    expected: int
    missing_candles: int
    zero_volume_candles: int
    accepted: bool
    reasons: tuple[str, ...]


@dataclass
class SymbolData:
    instrument: Instrument
    intraday: pd.DataFrame  # accepted sessions only
    daily: pd.DataFrame
    daily_source: str
    sessions: list[SessionQuality]
    source_sha256: str

    @property
    def accepted_sessions(self) -> list[date]:
        return [s.session for s in self.sessions if s.accepted]


@dataclass
class HistoricalDataset:
    symbols: dict[str, SymbolData]
    interval: str
    source: str
    loaded_at: datetime
    dataset_id: str
    rejected_symbols: dict[str, str] = field(default_factory=dict)

    def sessions(self) -> list[date]:
        days = {d for s in self.symbols.values() for d in s.accepted_sessions}
        return sorted(days)

    def provider(self, calendar: MarketCalendar) -> InMemoryProvider:
        candles = {}
        for sym, data in self.symbols.items():
            candles[(sym, self.interval)] = data.intraday
            candles[(sym, "1d")] = data.daily
        return InMemoryProvider(
            calendar,
            [d.instrument for d in self.symbols.values()],
            candles,
            quotes={},
            mode=DataMode.HISTORICAL,
            retrieved_at=self.loaded_at,
            prevalidated=True,
        )

    def quality_report(self) -> dict:
        rows = [s for d in self.symbols.values() for s in d.sessions]
        return {
            "dataset_id": self.dataset_id,
            "source": self.source,
            "loaded_at": self.loaded_at.isoformat(),
            "interval": self.interval,
            "coverage": {
                "first_session": min((r.session for r in rows), default=None),
                "last_session": max((r.session for r in rows), default=None),
            },
            "symbols": {
                sym: {
                    "sessions_total": len(d.sessions),
                    "sessions_accepted": sum(s.accepted for s in d.sessions),
                    "sessions_excluded": sum(not s.accepted for s in d.sessions),
                    "missing_candles": sum(s.missing_candles for s in d.sessions),
                    "zero_volume_candles": sum(s.zero_volume_candles for s in d.sessions),
                    "daily_source": d.daily_source,
                    "source_sha256": d.source_sha256,
                }
                for sym, d in self.symbols.items()
            },
            "excluded_sessions": [
                {"symbol": r.symbol, "session": r.session.isoformat(), "reasons": list(r.reasons)}
                for r in rows if not r.accepted
            ],
            "rejected_symbols": self.rejected_symbols,
        }


def _sha(frame: pd.DataFrame) -> str:
    buf = io.StringIO()
    frame.to_csv(buf, index=False)
    return hashlib.sha256(buf.getvalue().encode()).hexdigest()


def build_dataset(
    frames: dict[str, tuple[Instrument, pd.DataFrame]],
    settings: Settings,
    calendar: MarketCalendar,
    source: str,
    daily_frames: dict[str, pd.DataFrame] | None = None,
    loaded_at: datetime | None = None,
) -> HistoricalDataset:
    """Validate raw candle frames (timestamp, OHLCV) per symbol and session."""
    iv = parse_interval(settings.strategy.candle_interval)
    open_t, close_t = settings.market.session_open, settings.market.session_close
    expected = int(((close_t.hour * 60 + close_t.minute) - (open_t.hour * 60 + open_t.minute))
                   // iv.minutes)
    max_missing = settings.backtest.max_missing_candles_per_session
    symbols: dict[str, SymbolData] = {}
    rejected: dict[str, str] = {}
    hashes = []

    for sym in sorted(frames):
        instrument, raw = frames[sym]
        missing_cols = [c for c in CANDLE_COLUMNS if c not in raw.columns]
        if missing_cols:
            rejected[sym] = f"missing columns {missing_cols}"
            continue
        sha = _sha(raw[CANDLE_COLUMNS])
        hashes.append(f"{sym}:{sha}")
        frame = raw[CANDLE_COLUMNS].copy()
        ts = pd.to_datetime(frame["timestamp"])
        if ts.dt.tz is None:
            ts = ts.dt.tz_localize(calendar.tz)
        frame["timestamp"] = ts.dt.tz_convert(calendar.tz)
        for col in CANDLE_COLUMNS[1:]:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")

        quality: list[SessionQuality] = []
        accepted_parts = []
        # Keep file order inside each session so out-of-order rows are caught.
        for day, part in frame.groupby(frame["timestamp"].dt.date, sort=True):
            part = part.reset_index(drop=True)
            report = validate_candles(part, iv, calendar, settings.data.max_candle_jump_pct)
            reasons = [i.message for i in report.errors]
            slots = part["timestamp"].drop_duplicates()
            missing = max(expected - len(slots), 0)
            zero_vol = int((part["volume"] == 0).sum()) if report.is_valid else 0
            if report.is_valid and missing > max_missing:
                reasons.append(f"incomplete session: {missing} of {expected} candles missing "
                               f"(max {max_missing})")
            ok = not reasons
            quality.append(SessionQuality(sym, day, len(part), expected, missing, zero_vol,
                                          ok, tuple(reasons)))
            if ok:
                accepted_parts.append(part)

        if not accepted_parts:
            rejected[sym] = f"no valid sessions ({len(quality)} excluded)"
            continue
        intraday = pd.concat(accepted_parts, ignore_index=True)
        if daily_frames and sym in daily_frames:
            daily, daily_source = daily_frames[sym], "1d file"
        else:
            daily, daily_source = _aggregate_daily(intraday, calendar), \
                "aggregated from accepted intraday sessions"
        symbols[sym] = SymbolData(instrument, intraday, daily, daily_source, quality, sha)

    usable = {k: v for k, v in symbols.items() if k not in rejected}
    if not usable:
        raise DatasetError(f"no usable symbols in dataset: {rejected}")
    policy = f"{iv.label}|missing<={max_missing}|jump={settings.data.max_candle_jump_pct}"
    dataset_id = hashlib.sha256("|".join(hashes + [policy]).encode()).hexdigest()[:16]
    return HistoricalDataset(
        symbols=usable,
        interval=iv.label,
        source=source,
        loaded_at=loaded_at or datetime.now(calendar.tz),
        dataset_id=dataset_id,
        rejected_symbols=rejected,
    )


def _aggregate_daily(intraday: pd.DataFrame, calendar: MarketCalendar) -> pd.DataFrame:
    g = intraday.groupby(intraday["timestamp"].dt.date, sort=True)
    out = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
    })
    out["timestamp"] = [pd.Timestamp(calendar.session_bounds(d)[0]) for d in out.index]
    return out.reset_index(drop=True)[CANDLE_COLUMNS]


def load_csv_dataset(directory: Path, settings: Settings, calendar: MarketCalendar) -> HistoricalDataset:
    """Load ``<SYMBOL>_<interval>.csv`` files listed in ``instruments.csv``."""
    directory = Path(directory)
    reader = HistoricalCSVProvider(directory, calendar)
    instruments = reader.list_instruments()
    iv = parse_interval(settings.strategy.candle_interval).label
    frames, daily = {}, {}
    for inst in instruments:
        path = directory / f"{inst.symbol}_{iv}.csv"
        if not path.exists():
            continue
        frames[inst.symbol] = (inst, pd.read_csv(path))
        daily_path = directory / f"{inst.symbol}_1d.csv"
        if daily_path.exists():
            d = pd.read_csv(daily_path)
            ts = pd.to_datetime(d["timestamp"], format="ISO8601")
            d["timestamp"] = (ts.dt.tz_localize(calendar.tz) if ts.dt.tz is None
                              else ts.dt.tz_convert(calendar.tz))
            daily[inst.symbol] = d[CANDLE_COLUMNS]
    if not frames:
        raise DatasetError(f"no {iv} candle files found in {directory}")
    return build_dataset(frames, settings, calendar, source=f"csv:{directory}",
                         daily_frames=daily)
