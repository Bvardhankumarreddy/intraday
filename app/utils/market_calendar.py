"""NSE trading calendar and session status.

Holidays are loaded from a YAML file that the user fills from NSE's official
holiday list. When that file is missing or empty, ``holidays_loaded`` is
False and callers must surface a warning: weekdays will then be treated as
trading days even if the exchange is closed.

Special sessions (e.g. Muhurat trading) are supported once announced and
configured in ``market.special_sessions``. Unscheduled closures are not
modelled.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from app.config import MarketConfig


class SessionStatus(StrEnum):
    PRE_OPEN = "pre_open"
    OPEN = "open"
    CLOSED = "closed"
    HOLIDAY = "holiday"
    WEEKEND = "weekend"


def load_holidays(path: Path) -> dict[date, str]:
    """Read ``- {date: YYYY-MM-DD, name: ...}`` entries. Missing file -> empty."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        content = yaml.safe_load(fh) or {}
    entries = content.get("holidays", []) if isinstance(content, dict) else content
    holidays: dict[date, str] = {}
    for entry in entries or []:
        day = entry["date"]
        if isinstance(day, str):
            day = date.fromisoformat(day)
        if not isinstance(day, date):
            raise ValueError(f"invalid holiday date in {path}: {entry!r}")
        holidays[day] = str(entry.get("name", ""))
    return holidays


class MarketCalendar:
    def __init__(self, config: MarketConfig, holidays: dict[date, str] | None = None):
        self.config = config
        self.tz = ZoneInfo(config.timezone)
        self.holidays = load_holidays(config.holidays_file) if holidays is None else holidays

    @property
    def holidays_loaded(self) -> bool:
        return bool(self.holidays)

    def covers_year(self, year: int) -> bool:
        """True if the holiday list contains any entry for ``year``. A year
        without entries means the list was never filled for it."""
        return any(d.year == year for d in self.holidays)

    def special_session(self, day: date):
        return next((s for s in self.config.special_sessions if s.date == day), None)

    def is_regular_session(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def is_trading_day(self, day: date) -> bool:
        return self.special_session(day) is not None or self.is_regular_session(day)

    def trading_days(self, start: date, end: date) -> list[date]:
        days, current = [], start
        while current <= end:
            if self.is_trading_day(current):
                days.append(current)
            current += timedelta(days=1)
        return days

    def previous_trading_day(self, day: date) -> date:
        current = day - timedelta(days=1)
        for _ in range(30):
            if self.is_trading_day(current):
                return current
            current -= timedelta(days=1)
        raise ValueError(f"no trading day found in the 30 days before {day}")

    def session_bounds(self, day: date) -> tuple[datetime, datetime]:
        special = self.special_session(day)
        if special is not None:
            return self._at(day, special.open), self._at(day, special.close)
        return (
            self._at(day, self.config.session_open),
            self._at(day, self.config.session_close),
        )

    def session_status(self, now: datetime) -> SessionStatus:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        local = now.astimezone(self.tz)
        day = local.date()
        special = self.special_session(day)
        if special is not None:
            return SessionStatus.OPEN if special.open <= local.time() < special.close \
                else SessionStatus.CLOSED
        if day.weekday() >= 5:
            return SessionStatus.WEEKEND
        if day in self.holidays:
            return SessionStatus.HOLIDAY
        t = local.time()
        if self.config.pre_open_start <= t < self.config.session_open:
            return SessionStatus.PRE_OPEN
        if self.config.session_open <= t < self.config.session_close:
            return SessionStatus.OPEN
        return SessionStatus.CLOSED

    def _at(self, day: date, t: time) -> datetime:
        return datetime.combine(day, t, tzinfo=self.tz)
