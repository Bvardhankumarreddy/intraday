from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.config import RelativeVolumeMode
from app.data.models import parse_interval
from app.strategies.indicators import ema, opening_range, relative_volume, session_vwap
from tests.helpers import DAY, at, bars, flat_session

FIVE = parse_interval("5m")
PREV = date(2025, 3, 11)


def test_vwap_formula():
    f = bars(DAY, [(10, 12, 8, 10, 100), (10, 14, 10, 12, 300)])
    vwap = session_vwap(f)
    # typical prices 10 and 12
    assert vwap.iloc[0] == pytest.approx(10.0)
    assert vwap.iloc[1] == pytest.approx((10 * 100 + 12 * 300) / 400)


def test_vwap_resets_each_session():
    f = pd.concat([bars(PREV, [(50, 50, 50, 50, 1000)]), bars(DAY, [(70, 70, 70, 70, 10)])],
                  ignore_index=True)
    vwap = session_vwap(f)
    assert vwap.iloc[1] == pytest.approx(70.0)  # no carry-over from yesterday


def test_vwap_unavailable_until_volume_trades():
    f = bars(DAY, [(10, 10, 10, 10, 0), (11, 11, 11, 11, 50)])
    vwap = session_vwap(f)
    assert np.isnan(vwap.iloc[0])
    assert vwap.iloc[1] == pytest.approx(11.0)


def test_ema_seed_and_recursion():
    close = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = ema(close, 3)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2] == pytest.approx(2.0)  # SMA seed
    assert out.iloc[3] == pytest.approx(0.5 * 4 + 0.5 * 2.0)
    assert out.iloc[4] == pytest.approx(0.5 * 5 + 0.5 * 3.0)


def test_ema_insufficient_history():
    assert ema(pd.Series([1.0, 2.0]), 20).isna().all()


def test_opening_range_complete():
    f = bars(DAY, [(70, 70.5, 69.8, 70.2, 1), (70.2, 70.3, 69.5, 69.9, 1),
                   (69.9, 70.4, 69.7, 70.3, 1), (70.3, 72, 60, 71, 1)])
    rng = opening_range(f, at("09:15"), 15, FIVE)
    assert (rng.high, rng.low) == (70.5, 69.5)  # 09:30 candle excluded
    assert rng.end == at("09:30")
    assert rng.width == pytest.approx(1.0)
    assert rng.width_pct == pytest.approx(1.0 / 70.0 * 100)


def test_opening_range_incomplete_or_gapped():
    two = bars(DAY, [(70, 71, 69, 70, 1)] * 2)
    assert opening_range(two, at("09:15"), 15, FIVE) is None
    gapped = bars(DAY, [(70, 71, 69, 70, 1)] * 3, start="09:20")
    assert opening_range(gapped, at("09:15"), 15, FIVE) is None


def test_opening_range_must_fit_interval():
    with pytest.raises(ValueError):
        opening_range(bars(DAY, []), at("09:15"), 12, FIVE)


def history(today_rows, prior_volumes):
    frames = [flat_session(d, volume=v) for d, v in prior_volumes]
    frames.append(bars(DAY, today_rows))
    return pd.concat(frames, ignore_index=True)


PRIORS = [(date(2025, 3, 10), 1000), (date(2025, 3, 11), 3000)]


def test_relative_volume_cumulative():
    f = history([(70, 70, 70, 70, 2000)] * 3, PRIORS)
    rv, n = relative_volume(f, DAY, pd.Timestamp(at("09:25")), 10, RelativeVolumeMode.CUMULATIVE)
    # today 6000; baseline mean of 3000 and 9000 = 6000
    assert (rv, n) == (pytest.approx(1.0), 2)


def test_relative_volume_candle_mode():
    f = history([(70, 70, 70, 70, 500), (70, 70, 70, 70, 4000)], PRIORS)
    rv, n = relative_volume(f, DAY, pd.Timestamp(at("09:20")), 10, RelativeVolumeMode.CANDLE)
    assert (rv, n) == (pytest.approx(4000 / 2000), 2)


def test_relative_volume_lookback_limit():
    f = history([(70, 70, 70, 70, 2000)], PRIORS)
    rv, n = relative_volume(f, DAY, pd.Timestamp(at("09:15")), 1, RelativeVolumeMode.CANDLE)
    assert (rv, n) == (pytest.approx(2000 / 3000), 1)  # only 03-11 used


def test_relative_volume_skips_truncated_sessions():
    truncated = flat_session(date(2025, 3, 10), volume=1).iloc[:2]  # ends 09:20
    f = pd.concat([truncated, flat_session(PREV, volume=1000), bars(DAY, [(1, 1, 1, 1, 500)] * 4)],
                  ignore_index=True)
    rv, n = relative_volume(f, DAY, pd.Timestamp(at("09:30")), 10, RelativeVolumeMode.CUMULATIVE)
    assert n == 1 and rv == pytest.approx(2000 / 4000)


def test_relative_volume_unavailable():
    no_history = bars(DAY, [(1, 1, 1, 1, 100)])
    assert relative_volume(no_history, DAY, pd.Timestamp(at("09:15")), 10,
                           RelativeVolumeMode.CUMULATIVE) == (None, 0)
    zero_base = history([(1, 1, 1, 1, 100)], [(PREV, 0)])
    rv, n = relative_volume(zero_base, DAY, pd.Timestamp(at("09:15")), 10,
                            RelativeVolumeMode.CANDLE)
    assert rv is None and n == 1
