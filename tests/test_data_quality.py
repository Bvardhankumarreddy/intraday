from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.data.models import DataMode, Quote, parse_interval
from app.data.validation import check_freshness, validate_candles, validate_quote

IST = ZoneInfo("Asia/Kolkata")
FIVE = parse_interval("5m")


def good_frame(n=6, start="2025-03-12 09:15") -> pd.DataFrame:
    ts = pd.date_range(pd.Timestamp(start, tz=IST), periods=n, freq="5min")
    close = [70.0 + 0.1 * i for i in range(n)]
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": [c - 0.05 for c in close],
            "high": [c + 0.2 for c in close],
            "low": [c - 0.2 for c in close],
            "close": close,
            "volume": [10_000] * n,
        }
    )


def codes(frame, calendar, interval=FIVE):
    return validate_candles(frame, interval, calendar).codes()


def test_clean_data_passes(calendar):
    report = validate_candles(good_frame(), FIVE, calendar)
    assert report.is_valid and not report.issues


def test_missing_columns(calendar):
    assert "missing_columns" in codes(good_frame().drop(columns="volume"), calendar)


def test_empty(calendar):
    assert "empty" in codes(good_frame().iloc[0:0], calendar)


def test_naive_timestamps(calendar):
    f = good_frame()
    f["timestamp"] = f["timestamp"].dt.tz_localize(None)
    assert "naive_timestamps" in codes(f, calendar)


def test_nan(calendar):
    f = good_frame()
    f.loc[2, "close"] = float("nan")
    assert "missing_values" in codes(f, calendar)


def test_non_positive_and_negative_volume(calendar):
    f = good_frame()
    f.loc[1, ["open", "low"]] = [0.0, 0.0]
    f.loc[2, "volume"] = -1
    c = codes(f, calendar)
    assert {"non_positive_price", "negative_volume"} <= c


def test_inconsistent_ohlc(calendar):
    f = good_frame()
    f.loc[3, "high"] = f.loc[3, "close"] - 1
    assert "inconsistent_ohlc" in codes(f, calendar)


def test_duplicates_and_order(calendar):
    f = pd.concat([good_frame(), good_frame().iloc[[2]]], ignore_index=True)
    c = codes(f, calendar)
    assert {"duplicate_timestamps", "unsorted_timestamps"} <= c


def test_outside_session_and_misaligned(calendar):
    f = good_frame(start="2025-03-12 09:00")
    assert "outside_session" in codes(f, calendar)
    f = good_frame(start="2025-03-12 09:17")
    assert "misaligned_timestamps" in codes(f, calendar)


def test_non_trading_day(calendar):
    assert "non_trading_day" in codes(good_frame(start="2025-03-15 09:15"), calendar)
    assert "non_trading_day" in codes(good_frame(start="2025-03-14 09:15"), calendar)


def test_gaps_and_jumps_are_warnings(calendar):
    f = good_frame().drop(index=3).reset_index(drop=True)
    f.loc[4, ["open", "high", "low", "close"]] = [100.0, 101.0, 99.0, 100.0]
    report = validate_candles(f, FIVE, calendar)
    assert report.is_valid
    assert {"intraday_gaps", "large_price_jump"} <= report.codes()


def test_daily_candles_skip_intraday_checks(calendar):
    f = good_frame()
    f["timestamp"] = pd.to_datetime(
        ["2025-03-10", "2025-03-11", "2025-03-12", "2025-03-13", "2025-03-17", "2025-03-18"]
    ).tz_localize(IST)
    assert validate_candles(f, parse_interval("1d"), calendar).is_valid


NOW = datetime(2025, 3, 12, 10, 0, tzinfo=IST)


@pytest.mark.parametrize("mode", [DataMode.LIVE, DataMode.DELAYED])
def test_stale_live_data_rejected(mode):
    assert "stale" in check_freshness(NOW - timedelta(minutes=5), NOW, mode, 120).codes()
    assert check_freshness(NOW - timedelta(seconds=30), NOW, mode, 120).is_valid


def test_historical_data_not_subject_to_staleness():
    assert check_freshness(NOW - timedelta(days=30), NOW, DataMode.HISTORICAL, 120).is_valid


def test_future_timestamp_rejected():
    assert "future_timestamp" in check_freshness(
        NOW + timedelta(minutes=1), NOW, DataMode.LIVE, 120
    ).codes()


def quote(**kw):
    base = dict(
        symbol="X", last_price=70.0, timestamp=NOW, mode=DataMode.LIVE, source="t",
        bid=69.95, ask=70.05, bid_qty=100, ask_qty=100,
    )
    base.update(kw)
    return Quote(**base)


def test_quote_checks():
    assert validate_quote(quote(), NOW, 120).is_valid
    assert quote().spread_pct == pytest.approx(0.1 / 70 * 100)
    assert "crossed_quote" in validate_quote(quote(bid=70.1, ask=70.0), NOW, 120).codes()
    assert "one_sided_quote" in validate_quote(quote(ask=None), NOW, 120).codes()
    assert quote(ask=None).spread_pct is None
    assert "negative_quantity" in validate_quote(quote(bid_qty=-1), NOW, 120).codes()


def test_parse_interval():
    assert parse_interval("5m").minutes == 5
    assert parse_interval("1d").is_daily
    for bad in ("5", "0m", "2d", "1h", "abc"):
        with pytest.raises(ValueError):
            parse_interval(bad)
