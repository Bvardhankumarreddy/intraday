from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.data.historical_provider import HistoricalCSVProvider
from app.data.mock_provider import MockMarketDataProvider
from app.data.models import DataMode, DataQualityError, DataUnavailableError
from tests.conftest import TEST_HOLIDAY

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2025, 3, 12)


# -- mock provider ----------------------------------------------------------


@pytest.fixture
def mock(calendar):
    return MockMarketDataProvider(calendar, seed=7, as_of=datetime(2025, 3, 12, 10, 3, tzinfo=IST))


def test_mock_is_deterministic(calendar, mock):
    a = mock.get_candles("SIMALPHA", "5m", DAY, DAY).frame
    b = MockMarketDataProvider(calendar, seed=7).get_candles("SIMALPHA", "5m", DAY, DAY).frame
    pd.testing.assert_frame_equal(a, b)
    # Same day fetched as part of a longer range is identical.
    c = mock.get_candles("SIMALPHA", "5m", date(2025, 3, 10), DAY).frame
    pd.testing.assert_frame_equal(
        c[c["timestamp"].dt.date == DAY].reset_index(drop=True), a
    )


def test_mock_seed_changes_data(calendar, mock):
    other = MockMarketDataProvider(calendar, seed=8)
    assert not mock.get_candles("SIMALPHA", "5m", DAY, DAY).frame.equals(
        other.get_candles("SIMALPHA", "5m", DAY, DAY).frame
    )


def test_mock_labelled_simulated(mock):
    series = mock.get_candles("SIMALPHA", "5m", DAY, DAY)
    assert series.mode is DataMode.SIMULATED
    assert mock.get_quote("SIMALPHA").mode is DataMode.SIMULATED


def test_mock_session_shape(mock):
    frame = mock.get_candles("SIMBETA", "5m", DAY, DAY).frame
    assert len(frame) == 75  # 375 minutes / 5
    assert frame["timestamp"].iloc[0] == pd.Timestamp("2025-03-12 09:15", tz=IST)
    assert frame["timestamp"].iloc[-1] == pd.Timestamp("2025-03-12 15:25", tz=IST)


def test_mock_aggregation_is_consistent(mock):
    one = mock.get_candles("SIMGAMMA", "1m", DAY, DAY).frame
    fifteen = mock.get_candles("SIMGAMMA", "15m", DAY, DAY).frame
    first = one.iloc[:15]
    row = fifteen.iloc[0]
    assert row["open"] == first["open"].iloc[0]
    assert row["high"] == first["high"].max()
    assert row["low"] == first["low"].min()
    assert row["close"] == first["close"].iloc[-1]
    assert row["volume"] == first["volume"].sum()
    daily = mock.get_candles("SIMGAMMA", "1d", DAY, DAY).frame
    assert daily["volume"].iloc[0] == one["volume"].sum()


def test_mock_skips_non_trading_days(mock):
    frame = mock.get_candles("SIMALPHA", "1d", date(2025, 3, 10), date(2025, 3, 17)).frame
    days = set(frame["timestamp"].dt.date)
    assert TEST_HOLIDAY not in days and date(2025, 3, 15) not in days
    assert len(frame) == 5


def test_mock_overnight_gap_is_moderate(mock):
    daily = mock.get_candles("SIMALPHA", "1d", date(2025, 1, 1), date(2025, 6, 30)).frame
    gaps = (daily["open"].iloc[1:].to_numpy() / daily["close"].iloc[:-1].to_numpy() - 1)
    assert abs(gaps).max() < 0.05


def test_mock_quote_uses_only_completed_bars(mock):
    q = mock.get_quote("SIMALPHA")
    # as_of 10:03 -> last completed 1m bar is 10:02-10:03
    assert q.timestamp == datetime(2025, 3, 12, 10, 3, tzinfo=IST)
    bars = mock.get_candles("SIMALPHA", "1m", DAY, DAY).frame
    assert q.last_price == bars.loc[bars["timestamp"].dt.strftime("%H:%M") == "10:02", "close"].item()
    assert q.bid < q.last_price < q.ask


def test_completed_excludes_in_progress_candle(mock):
    series = mock.get_candles("SIMALPHA", "5m", DAY, DAY)
    done = series.completed(datetime(2025, 3, 12, 10, 3, tzinfo=IST))
    # 09:55 candle ends at 10:00 (complete); 10:00 candle is still forming.
    assert done["timestamp"].iloc[-1] == pd.Timestamp("2025-03-12 09:55", tz=IST)


def test_mock_unknown_symbol(mock):
    with pytest.raises(DataUnavailableError):
        mock.get_candles("RELIANCE", "5m", DAY, DAY)


def test_mock_instruments_include_screening_cases(mock):
    symbols = {i.symbol: i for i in mock.list_instruments()}
    assert all(s.startswith("SIM") for s in symbols)
    assert not symbols["SIMETA"].tradable


# -- historical CSV provider -------------------------------------------------


def write_csv(directory, symbol="ABC", interval="5m", rows=None, tz=True):
    ts = pd.date_range("2025-03-12 09:15", periods=4, freq="5min", tz=IST if tz else None)
    frame = rows if rows is not None else pd.DataFrame(
        {
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S%z" if tz else "%Y-%m-%d %H:%M:%S"),
            "open": [70, 70.5, 71, 70.8],
            "high": [70.6, 71.2, 71.3, 71.0],
            "low": [69.8, 70.3, 70.7, 70.5],
            "close": [70.5, 71, 70.8, 70.9],
            "volume": [1000, 1500, 1200, 900],
        }
    )
    frame.to_csv(directory / f"{symbol}_{interval}.csv", index=False)
    (directory / "instruments.csv").write_text(
        "symbol,name,isin,tradable\nABC,ABC Ltd,INE000000000,true\nXYZ,XYZ Ltd,,false\n"
    )


def test_historical_reads_valid_csv(tmp_path, calendar):
    write_csv(tmp_path)
    p = HistoricalCSVProvider(tmp_path, calendar)
    series = p.get_candles("ABC", "5m", DAY, DAY)
    assert series.mode is DataMode.HISTORICAL
    assert len(series.frame) == 4
    assert str(series.frame["timestamp"].dt.tz) == "Asia/Kolkata"
    assert not series.warnings


def test_historical_naive_timestamps_warn(tmp_path, calendar):
    write_csv(tmp_path, tz=False)
    series = HistoricalCSVProvider(tmp_path, calendar).get_candles("ABC", "5m", DAY, DAY)
    assert any("assumed" in w for w in series.warnings)
    assert series.frame["timestamp"].iloc[0] == pd.Timestamp("2025-03-12 09:15", tz=IST)


def test_historical_rejects_bad_data(tmp_path, calendar):
    write_csv(tmp_path)
    path = tmp_path / "ABC_5m.csv"
    frame = pd.read_csv(path)
    frame.loc[2, "high"] = 60  # high below close
    frame.to_csv(path, index=False)
    with pytest.raises(DataQualityError, match="violate"):
        HistoricalCSVProvider(tmp_path, calendar).get_candles("ABC", "5m", DAY, DAY)


def test_historical_rejects_duplicates(tmp_path, calendar):
    write_csv(tmp_path)
    path = tmp_path / "ABC_5m.csv"
    frame = pd.read_csv(path)
    pd.concat([frame, frame.iloc[[1]]]).to_csv(path, index=False)
    with pytest.raises(DataQualityError):
        HistoricalCSVProvider(tmp_path, calendar).get_candles("ABC", "5m", DAY, DAY)


def test_historical_non_numeric_rejected(tmp_path, calendar):
    write_csv(tmp_path)
    path = tmp_path / "ABC_5m.csv"
    frame = pd.read_csv(path)
    frame["close"] = frame["close"].astype(object)
    frame.loc[1, "close"] = "n/a"
    frame.to_csv(path, index=False)
    with pytest.raises(DataQualityError):
        HistoricalCSVProvider(tmp_path, calendar).get_candles("ABC", "5m", DAY, DAY)


def test_historical_missing_file_and_range(tmp_path, calendar):
    write_csv(tmp_path)
    p = HistoricalCSVProvider(tmp_path, calendar)
    with pytest.raises(DataUnavailableError):
        p.get_candles("NOPE", "5m", DAY, DAY)
    with pytest.raises(DataUnavailableError):
        p.get_candles("ABC", "5m", date(2025, 4, 1), date(2025, 4, 2))


def test_historical_has_no_live_quotes(tmp_path, calendar):
    write_csv(tmp_path)
    with pytest.raises(DataUnavailableError, match="cannot be evaluated"):
        HistoricalCSVProvider(tmp_path, calendar).get_quote("ABC")


def test_historical_instruments(tmp_path, calendar):
    write_csv(tmp_path)
    inst = {i.symbol: i for i in HistoricalCSVProvider(tmp_path, calendar).list_instruments()}
    assert inst["ABC"].isin == "INE000000000" and inst["ABC"].tradable
    assert inst["XYZ"].isin is None and not inst["XYZ"].tradable
