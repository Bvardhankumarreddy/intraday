"""Market-data quality checks.

Errors make a dataset unusable (the provider raises ``DataQualityError``).
Warnings are attached to the dataset so reports can show them. Data is never
repaired or filled in silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

import pandas as pd

from app.data.models import CANDLE_COLUMNS, DataMode, Interval, Quote
from app.utils.market_calendar import MarketCalendar


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Issue:
    code: str
    severity: Severity
    message: str


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)

    def error(self, code: str, message: str) -> None:
        self.issues.append(Issue(code, Severity.ERROR, message))

    def warn(self, code: str, message: str) -> None:
        self.issues.append(Issue(code, Severity.WARNING, message))

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def codes(self) -> set[str]:
        return {i.code for i in self.issues}


def validate_candles(
    frame: pd.DataFrame,
    interval: Interval,
    calendar: MarketCalendar,
    max_jump_pct: float = 20.0,
) -> ValidationReport:
    report = ValidationReport()

    missing = [c for c in CANDLE_COLUMNS if c not in frame.columns]
    if missing:
        report.error("missing_columns", f"missing columns: {missing}")
        return report
    if frame.empty:
        report.error("empty", "no candles returned")
        return report

    ts = frame["timestamp"]
    if not isinstance(ts.dtype, pd.DatetimeTZDtype):
        report.error("naive_timestamps", "timestamps must be timezone-aware")
        return report

    prices = frame[["open", "high", "low", "close"]]
    if frame[CANDLE_COLUMNS[1:]].isna().any().any():
        report.error("missing_values", f"{int(frame.isna().any(axis=1).sum())} rows contain NaN")
        return report
    if (prices <= 0).any().any():
        report.error("non_positive_price", "prices must be positive")
    if (frame["volume"] < 0).any():
        report.error("negative_volume", "volume must be non-negative")

    bad_high = frame["high"] < prices[["open", "close", "low"]].max(axis=1)
    bad_low = frame["low"] > prices[["open", "close"]].min(axis=1)
    if (bad_high | bad_low).any():
        first = ts[bad_high | bad_low].iloc[0]
        report.error(
            "inconsistent_ohlc",
            f"{int((bad_high | bad_low).sum())} candles violate low <= open/close <= high "
            f"(first at {first})",
        )

    if ts.duplicated().any():
        report.error("duplicate_timestamps", f"{int(ts.duplicated().sum())} duplicate timestamps")
    if not ts.is_monotonic_increasing:
        report.error("unsorted_timestamps", "timestamps are not in ascending order")

    local = ts.dt.tz_convert(calendar.tz)
    non_trading = sorted({d for d in local.dt.date if not calendar.is_trading_day(d)})
    if non_trading:
        report.error("non_trading_day", f"candles on non-trading days: {non_trading[:5]}")

    if not interval.is_daily:
        _check_intraday_alignment(local, interval, calendar, report)

    closes = frame["close"].to_numpy()
    if len(closes) > 1:
        jumps = abs(closes[1:] / closes[:-1] - 1) * 100
        if (jumps > max_jump_pct).any():
            report.warn(
                "large_price_jump",
                f"{int((jumps > max_jump_pct).sum())} close-to-close moves exceed {max_jump_pct}%",
            )
    if (frame["volume"] == 0).any():
        report.warn("zero_volume", f"{int((frame['volume'] == 0).sum())} candles have zero volume")

    return report


def _check_intraday_alignment(
    local: pd.Series, interval: Interval, calendar: MarketCalendar, report: ValidationReport
) -> None:
    open_t, close_t = calendar.config.session_open, calendar.config.session_close
    times = local.dt.time
    outside = (times < open_t) | (times >= close_t)
    if outside.any():
        report.error(
            "outside_session",
            f"{int(outside.sum())} candles start outside {open_t}-{close_t}",
        )

    session_open = local.dt.normalize() + pd.Timedelta(
        hours=open_t.hour, minutes=open_t.minute
    )
    offset_min = (local - session_open) / pd.Timedelta(minutes=1)
    misaligned = (offset_min % interval.minutes) != 0
    if misaligned.any():
        report.error(
            "misaligned_timestamps",
            f"{int(misaligned.sum())} candles are not aligned to {interval.label} "
            f"boundaries from {open_t}",
        )

    # Gaps inside a session can be genuine (no trades) but are worth surfacing.
    step = pd.Timedelta(minutes=interval.minutes)
    same_day = local.dt.date == local.shift().dt.date
    gaps = same_day & (local.diff() > step)
    if gaps.any():
        report.warn("intraday_gaps", f"{int(gaps.sum())} gaps between consecutive candles")


def check_freshness(
    timestamp: datetime, now: datetime, mode: DataMode, max_age_seconds: int
) -> ValidationReport:
    """Reject stale data in live/delayed modes. Historical/simulated data is
    never treated as current, so no freshness requirement applies."""
    report = ValidationReport()
    if timestamp.tzinfo is None or now.tzinfo is None:
        report.error("naive_timestamps", "timestamps must be timezone-aware")
        return report
    if timestamp > now + timedelta(seconds=5):
        report.error("future_timestamp", f"data timestamp {timestamp} is after now {now}")
    if mode in (DataMode.LIVE, DataMode.DELAYED):
        age = (now - timestamp).total_seconds()
        if age > max_age_seconds:
            report.error("stale", f"data is {age:.0f}s old (limit {max_age_seconds}s)")
    return report


def validate_quote(quote: Quote, now: datetime, max_age_seconds: int) -> ValidationReport:
    report = check_freshness(quote.timestamp, now, quote.mode, max_age_seconds)
    if quote.last_price <= 0:
        report.error("non_positive_price", "last price must be positive")
    if (quote.bid is None) != (quote.ask is None):
        report.warn("one_sided_quote", "only one side of the order book is available")
    if quote.bid is not None and quote.ask is not None:
        if quote.bid <= 0 or quote.ask <= 0:
            report.error("non_positive_price", "bid and ask must be positive")
        elif quote.bid > quote.ask:
            report.error("crossed_quote", f"bid {quote.bid} is above ask {quote.ask}")
    for name in ("bid_qty", "ask_qty"):
        qty = getattr(quote, name)
        if qty is not None and qty < 0:
            report.error("negative_quantity", f"{name} must be non-negative")
    return report
