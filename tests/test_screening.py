import pytest

from app.config import Settings
from app.data.models import DataMode, Instrument, Quote
from app.scan.models import ScreenStatus
from app.screening.screener import screen
from tests.helpers import DAY, at, flat_session, make_provider, snapshot


@pytest.fixture
def settings():
    return Settings()


def run(calendar, settings, as_of="11:00", **kw):
    kw.setdefault("today_rows", flat_session(DAY, price=kw.get("daily_close", 70.0)).iloc[:21][
        ["open", "high", "low", "close", "volume"]
    ].itertuples(index=False, name=None))
    kw["today_rows"] = list(kw["today_rows"])
    provider = make_provider(calendar, settings, **kw)
    snap = snapshot(provider, settings, calendar, at(as_of))
    return screen(snap, settings, calendar)


def codes(result):
    return {r.code for r in result.rejections}


def test_clean_stock_passes(calendar, settings):
    result = run(calendar, settings)
    assert result.status is ScreenStatus.PASS
    assert {r.code for r in result.passed} == {"price_in_band", "liquid", "tight_spread"}


@pytest.mark.parametrize(("price", "ok"), [(50.0, True), (100.0, True), (49.95, False), (100.05, False)])
def test_price_band_boundaries(calendar, settings, price, ok):
    result = run(calendar, settings, daily_close=price, daily_volume=10_000_000)
    assert (result.status is ScreenStatus.PASS) is ok
    if not ok:
        assert "price_out_of_band" in codes(result)


def test_liquidity_threshold(calendar, settings):
    # 50 x 100,000 = Rs 50 lakh exactly: at the threshold passes; just below fails.
    at_threshold = run(calendar, settings, daily_close=50.0, daily_volume=100_000)
    assert at_threshold.avg_traded_value == pytest.approx(5_000_000)
    assert at_threshold.status is ScreenStatus.PASS
    below = run(calendar, settings, daily_close=50.0, daily_volume=99_999)
    assert below.status is ScreenStatus.REJECTED and "illiquid" in codes(below)


def test_insufficient_liquidity_history(calendar, settings):
    result = run(calendar, settings, daily_sessions=12)
    assert result.status is ScreenStatus.INSUFFICIENT_DATA
    assert "insufficient_liquidity_history" in codes(result)
    assert result.avg_traded_value_sessions == 12


def spread_quote(bid, ask):
    return Quote(symbol="TEST", last_price=70.0, timestamp=at("10:59"), mode=DataMode.SIMULATED,
                 source="t", bid=bid, ask=ask)


def test_spread_at_and_above_threshold(calendar, settings):
    # 0.07 on a 70.00 mid is exactly 0.10%.
    ok = run(calendar, settings, quote=spread_quote(69.965, 70.035))
    assert ok.status is ScreenStatus.PASS
    wide = run(calendar, settings, quote=spread_quote(69.9, 70.1))
    assert wide.status is ScreenStatus.REJECTED and "wide_spread" in codes(wide)


def test_missing_spread_is_never_zero(calendar, settings):
    result = run(calendar, settings, quote=None)
    assert result.spread_pct is None
    assert result.status is ScreenStatus.INSUFFICIENT_DATA
    assert "spread_unavailable" in codes(result)

    relaxed = Settings(screening={"require_spread": False})
    result = run(calendar, relaxed, quote=None)
    assert result.status is ScreenStatus.PASS and result.spread_pct is None
    assert "spread_not_required" in {r.code for r in result.passed}


def test_one_sided_quote_has_no_spread(calendar, settings):
    result = run(calendar, settings, quote=spread_quote(69.95, None))
    assert result.spread_pct is None
    assert result.status is ScreenStatus.INSUFFICIENT_DATA


def test_suspended_instrument_rejected(calendar, settings):
    result = run(calendar, settings, instrument=Instrument("TEST", "T", tradable=False))
    assert result.status is ScreenStatus.REJECTED and "not_tradable" in codes(result)
    result = run(calendar, settings, instrument=Instrument("TEST", "T", series="BE"))
    assert "series_not_allowed" in codes(result)


def test_stale_intraday_data(calendar, settings):
    # Candles stop at 10:00 but we evaluate at 11:00.
    rows = list(flat_session(DAY).iloc[:9][["open", "high", "low", "close", "volume"]]
                .itertuples(index=False, name=None))
    result = run(calendar, settings, today_rows=rows)
    assert result.status is ScreenStatus.INSUFFICIENT_DATA
    assert "stale_data" in codes(result)


def test_no_staleness_before_first_candle_completes(calendar, settings):
    result = run(calendar, settings, as_of="09:17", today_rows=[])
    assert "stale_data" not in codes(result)
    assert result.last_price == 70.0  # quote at 09:15


def test_future_quote_is_ignored(calendar, settings):
    future = Quote(symbol="TEST", last_price=70.0, timestamp=at("12:00"),
                   mode=DataMode.SIMULATED, source="t", bid=69.99, ask=70.01)
    result = run(calendar, settings, quote=future)
    assert result.spread_pct is None and "spread_unavailable" in codes(result)


def test_previous_close_used_before_open(calendar, settings):
    result = run(calendar, settings, as_of="09:05", today_rows=[], quote=None,
                 daily_close=80.0)
    assert result.last_price == 80.0
    assert any("previous session close" in r.message for r in result.passed)


def test_bad_candles_mean_insufficient_data(calendar, settings):
    rows = list(flat_session(DAY).iloc[:21][["open", "high", "low", "close", "volume"]]
                .itertuples(index=False, name=None))
    rows[5] = (70.0, 69.0, 69.5, 70.0, 1000)  # high below open
    result = run(calendar, settings, today_rows=rows)
    assert result.status is ScreenStatus.INSUFFICIENT_DATA
    assert "intraday_data" in codes(result)


def test_all_reasons_reported(calendar, settings):
    result = run(calendar, settings, daily_close=120.0, daily_volume=100,
                 quote=spread_quote(119.0, 121.0))
    assert {"price_out_of_band", "illiquid", "wide_spread"} <= codes(result)
