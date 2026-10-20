from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import MarketConfig
from app.utils.market_calendar import MarketCalendar, SessionStatus, load_holidays
from tests.conftest import TEST_HOLIDAY

IST = ZoneInfo("Asia/Kolkata")


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (datetime(2025, 3, 12, 8, 59, tzinfo=IST), SessionStatus.CLOSED),
        (datetime(2025, 3, 12, 9, 0, tzinfo=IST), SessionStatus.PRE_OPEN),
        (datetime(2025, 3, 12, 9, 15, tzinfo=IST), SessionStatus.OPEN),
        (datetime(2025, 3, 12, 15, 29, tzinfo=IST), SessionStatus.OPEN),
        (datetime(2025, 3, 12, 15, 30, tzinfo=IST), SessionStatus.CLOSED),
        (datetime(2025, 3, 14, 11, 0, tzinfo=IST), SessionStatus.HOLIDAY),
        (datetime(2025, 3, 15, 11, 0, tzinfo=IST), SessionStatus.WEEKEND),
        # 05:45 UTC is 11:15 IST
        (datetime(2025, 3, 12, 5, 45, tzinfo=ZoneInfo("UTC")), SessionStatus.OPEN),
    ],
)
def test_session_status(calendar, when, expected):
    assert calendar.session_status(when) is expected


def test_naive_time_rejected(calendar):
    with pytest.raises(ValueError):
        calendar.session_status(datetime(2025, 3, 12, 10, 0))


def test_trading_days_skip_weekends_and_holidays(calendar):
    days = calendar.trading_days(date(2025, 3, 10), date(2025, 3, 17))
    assert TEST_HOLIDAY not in days
    assert date(2025, 3, 15) not in days
    assert days == [date(2025, 3, d) for d in (10, 11, 12, 13, 17)]
    assert calendar.previous_trading_day(date(2025, 3, 17)) == date(2025, 3, 13)


def test_holiday_file_loading(tmp_path):
    path = tmp_path / "h.yaml"
    path.write_text("holidays:\n  - date: 2025-03-14\n    name: Test\n")
    assert load_holidays(path) == {date(2025, 3, 14): "Test"}
    assert load_holidays(tmp_path / "missing.yaml") == {}


def test_empty_holiday_list_is_flagged(tmp_path):
    cal = MarketCalendar(MarketConfig(holidays_file=tmp_path / "missing.yaml"))
    assert not cal.holidays_loaded


def test_special_session_and_year_coverage():
    cfg = MarketConfig(special_sessions=[
        {"date": "2026-11-08", "open": "18:00", "close": "19:00", "name": "Muhurat"}])
    cal = MarketCalendar(cfg, holidays={date(2026, 11, 8): "Diwali Laxmi Pujan"})
    sunday = date(2026, 11, 8)
    assert cal.is_trading_day(sunday) and not cal.is_regular_session(sunday)
    assert cal.session_bounds(sunday)[0].hour == 18
    assert cal.session_status(datetime(2026, 11, 8, 18, 30, tzinfo=IST)) is SessionStatus.OPEN
    assert cal.session_status(datetime(2026, 11, 8, 10, 0, tzinfo=IST)) is SessionStatus.CLOSED
    assert cal.covers_year(2026) and not cal.covers_year(2025)


def test_shipped_holiday_file_matches_nse_2026():
    from app.config import PROJECT_ROOT

    holidays = load_holidays(PROJECT_ROOT / "config" / "nse_holidays.yaml")
    assert holidays[date(2026, 1, 15)].startswith("Municipal")  # in NSE's official CM list
    assert holidays[date(2026, 10, 20)] == "Dussehra"
    assert len([d for d in holidays if d.year == 2026]) == 20
