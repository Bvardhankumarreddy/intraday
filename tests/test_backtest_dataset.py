from datetime import date

import pandas as pd
import pytest

from app.backtesting.dataset import DatasetError, build_dataset, load_csv_dataset
from app.data.models import Instrument
from tests.backtest_helpers import bt_settings, flat_day
from tests.helpers import bars

D1, D2, D3 = date(2025, 3, 10), date(2025, 3, 11), date(2025, 3, 12)
SAT = date(2025, 3, 15)


def frame(*parts):
    return pd.concat(parts, ignore_index=True)


def build(calendar, raw, **over):
    s = bt_settings(**over)
    return build_dataset({"X": (Instrument("X", "X Ltd"), raw)}, s, calendar, "test")


def quality(ds_or_report, day):
    rows = ds_or_report.symbols["X"].sessions
    return next(r for r in rows if r.session == day)


def test_clean_sessions_accepted(calendar):
    ds = build(calendar, frame(bars(D1, flat_day()), bars(D2, flat_day())))
    assert ds.sessions() == [D1, D2]
    q = quality(ds, D1)
    assert (q.candles, q.expected, q.missing_candles, q.accepted) == (75, 75, 0, True)
    assert ds.symbols["X"].daily_source.startswith("aggregated")
    daily = ds.symbols["X"].daily
    assert list(daily["volume"]) == [75_000, 75_000]


def test_duplicate_candle_excludes_only_that_session(calendar):
    d2 = bars(D2, flat_day())
    raw = frame(bars(D1, flat_day()), d2, d2.iloc[[10]])
    ds = build(calendar, raw)
    assert ds.sessions() == [D1]
    assert any("duplicate" in r for r in quality(ds, D2).reasons)


def test_out_of_order_rows_excluded(calendar):
    d2 = bars(D2, flat_day())
    swapped = pd.concat([d2.iloc[:5], d2.iloc[[6, 5]], d2.iloc[7:]], ignore_index=True)
    ds = build(calendar, frame(bars(D1, flat_day()), swapped))
    assert ds.sessions() == [D1]
    assert any("order" in r for r in quality(ds, D2).reasons)


def test_missing_vs_zero_volume(calendar):
    d2 = bars(D2, flat_day()).drop(index=[20, 21]).reset_index(drop=True)
    d2.loc[30, "volume"] = 0
    ds = build(calendar, frame(bars(D1, flat_day()), d2))
    q = quality(ds, D2)
    assert q.accepted and (q.missing_candles, q.zero_volume_candles) == (2, 1)


def test_incomplete_session_excluded(calendar):
    short = bars(D2, flat_day()[:40])  # 35 candles missing
    ds = build(calendar, frame(bars(D1, flat_day()), short))
    q = quality(ds, D2)
    assert not q.accepted and "incomplete" in q.reasons[0]


def test_non_trading_day_and_bad_ohlc_excluded(calendar):
    bad = bars(D2, flat_day())
    bad.loc[3, "high"] = 1.0
    ds = build(calendar, frame(bars(D1, flat_day()), bad, bars(SAT, flat_day())))
    assert ds.sessions() == [D1]
    assert not quality(ds, SAT).accepted


def test_nothing_usable_rejected(calendar):
    bad = bars(D1, flat_day())
    bad["close"] = -1.0
    with pytest.raises(DatasetError):
        build(calendar, bad)


def test_dataset_id_is_deterministic(calendar):
    raw = frame(bars(D1, flat_day()), bars(D2, flat_day()))
    a, b = build(calendar, raw), build(calendar, raw.copy())
    assert a.dataset_id == b.dataset_id
    changed = raw.copy()
    changed.loc[0, "volume"] = 999
    assert build(calendar, changed).dataset_id != a.dataset_id


def test_quality_report_contents(calendar):
    ds = build(calendar, frame(bars(D1, flat_day()), bars(D2, flat_day()[:40])))
    rep = ds.quality_report()
    assert rep["symbols"]["X"]["sessions_accepted"] == 1
    assert rep["excluded_sessions"][0]["session"] == D2.isoformat()
    assert rep["coverage"]["first_session"] == D1


def test_csv_loader(tmp_path, calendar):
    (tmp_path / "instruments.csv").write_text("symbol,name\nX,X Ltd\n")
    raw = frame(bars(D1, flat_day()), bars(D2, flat_day()))
    raw.assign(timestamp=raw["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%S%z")) \
        .to_csv(tmp_path / "X_5m.csv", index=False)
    ds = load_csv_dataset(tmp_path, bt_settings(), calendar)
    assert ds.sessions() == [D1, D2] and ds.source.startswith("csv:")
    with pytest.raises(DatasetError):
        empty = tmp_path / "empty"
        empty.mkdir()
        (empty / "instruments.csv").write_text("symbol,name\nX,X Ltd\n")
        load_csv_dataset(empty, bt_settings(), calendar)
