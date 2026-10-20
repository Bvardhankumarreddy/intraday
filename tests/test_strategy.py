"""Opening-range breakout. Base scenario (see helpers.orb_today):

range high 70.50 / low 69.50 from 09:15-09:30; the 09:35 candle closes 71.00
(low 70.40) on heavy volume, so the signal is known at 09:40.
Cumulative relative volume = (4*1000 + 5000) / (5*1000) = 1.8.
Stop = 70.40, risk 0.60, target = 71.00 + 2 * 0.60 = 72.20.
"""

import pytest

from app.config import Settings
from app.scan.models import SignalStatus
from app.strategies.opening_range_breakout import evaluate
from tests.helpers import at, make_provider, orb_today, snapshot

NEUTRAL = (71.0, 71.2, 70.8, 71.1, 1000)  # stays between stop and target


@pytest.fixture
def settings():
    return Settings()


def run(calendar, settings, as_of, rows=None, **kw):
    provider = make_provider(calendar, settings, today_rows=rows if rows is not None else orb_today(),
                             **kw)
    return evaluate(snapshot(provider, settings, calendar, at(as_of)), settings, calendar)


def codes(ev):
    return {r.code for r in ev.rejections}


def test_qualified_breakout(calendar, settings):
    ev = run(calendar, settings, "09:42")
    assert ev.status is SignalStatus.QUALIFIED_PRE_COST
    s = ev.setup
    assert s.signal_candle_start == at("09:35") and s.signal_time == at("09:40")
    assert s.valid_until == at("09:50") and s.exit_by == at("15:10")
    assert s.entry_reference == 71.0 and s.stop_reference == 70.4
    assert s.risk_per_share == pytest.approx(0.6)
    assert s.target_reference == pytest.approx(72.2)
    assert s.planned_reward_risk >= 2.0
    assert ev.indicators.relative_volume == pytest.approx(1.8)
    assert ev.indicators.opening_range_high == 70.5 and ev.indicators.opening_range_low == 69.5
    assert {r.code for r in ev.qualification} >= {"breakout", "relative_volume", "above_vwap",
                                                   "valid_stop"}


def test_unfinished_signal_candle_is_not_used(calendar, settings):
    # At 09:39 the 09:35 candle is still forming: its close is not yet known.
    ev = run(calendar, settings, "09:39")
    assert ev.status is SignalStatus.WATCH
    assert ev.setup is None and "no_breakout" in codes(ev)


def test_opening_range_still_forming(calendar, settings):
    ev = run(calendar, settings, "09:25")
    assert ev.status is SignalStatus.WATCH and "opening_range_forming" in codes(ev)


def test_look_ahead_future_candles_do_not_change_result(calendar, settings):
    crash = [(71.0, 71.0, 60.0, 60.5, 90_000)] * 3
    base = run(calendar, settings, "09:42")
    with_future = run(calendar, settings, "09:42", rows=orb_today(after=crash))
    assert with_future.status is base.status
    assert with_future.setup == base.setup
    assert with_future.indicators == base.indicators


def test_low_relative_volume_keeps_watching(calendar, settings):
    ev = run(calendar, settings, "09:42", rows=orb_today(breakout_volume=1000))
    assert ev.status is SignalStatus.WATCH and "low_relative_volume" in codes(ev)


def test_close_below_vwap_rejected(calendar, settings):
    rows = orb_today(breakout_close=71.0, breakout_high=73.0, breakout_low=70.6,
                     breakout_volume=50_000)
    ev = run(calendar, settings, "09:42", rows=rows)
    assert ev.setup is None and "below_vwap" in codes(ev)


def test_overextended_entry_rejected(calendar, settings):
    ev = run(calendar, settings, "09:42", rows=orb_today(breakout_close=71.5))
    assert "overextended" in codes(ev)


@pytest.mark.parametrize(
    ("kwargs", "strategy", "code"),
    [
        ({"breakout_low": 71.0}, {}, "invalid_stop"),
        ({"breakout_low": 70.95}, {}, "stop_too_tight"),
        ({}, {"stop_reference": "opening_range_low", "max_risk_pct": 1.0}, "stop_too_wide"),
    ],
)
def test_stop_validation(calendar, kwargs, strategy, code):
    settings = Settings(strategy=strategy)
    ev = run(calendar, settings, "09:42", rows=orb_today(**kwargs))
    assert ev.setup is None and code in codes(ev)


def test_alternative_stop_references(calendar):
    ev = run(calendar, Settings(strategy={"stop_reference": "opening_range_low"}), "09:42")
    assert ev.setup.stop_reference == 69.5 and ev.setup.target_reference == pytest.approx(74.0)
    ev = run(calendar, Settings(strategy={"stop_reference": "opening_range_mid"}), "09:42")
    assert ev.setup.stop_reference == 70.0


def test_ema_filter(calendar):
    on = Settings(strategy={"use_ema_filter": True})
    assert run(calendar, on, "09:42").status is SignalStatus.QUALIFIED_PRE_COST
    # Prior sessions traded at 72, so EMA20 sits above today's 71.00 close.
    ev = run(calendar, on, "09:42", prior_price=72.0)
    assert "below_ema" in codes(ev)


def test_only_first_signal_counts(calendar, settings):
    # Pull back below the range high, then break out again: still one signal.
    later = [(71.0, 71.1, 70.45, 70.45, 1000), (70.45, 71.4, 70.45, 71.3, 20_000)]
    ev = run(calendar, settings, "09:52", rows=orb_today(after=later))
    assert ev.setup.signal_candle_start == at("09:35")
    again = run(calendar, settings, "09:52", rows=orb_today(after=later))
    assert again.setup.signal_id == ev.setup.signal_id


def test_stop_traded_invalidates(calendar, settings):
    ev = run(calendar, settings, "09:47", rows=orb_today(after=[(71.0, 71.1, 70.3, 70.5, 1000)]))
    assert ev.status is SignalStatus.INVALIDATED and "stop_traded" in codes(ev)


def test_target_traded_invalidates(calendar, settings):
    ev = run(calendar, settings, "09:47", rows=orb_today(after=[(71.0, 72.3, 70.9, 72.1, 1000)]))
    assert ev.status is SignalStatus.INVALIDATED and "target_traded" in codes(ev)


def test_signal_expires(calendar, settings):
    ev = run(calendar, settings, "10:00", rows=orb_today(after=[NEUTRAL] * 4))
    assert ev.status is SignalStatus.EXPIRED and "expired" in codes(ev)


def test_breakout_after_cutoff_rejected(calendar):
    settings = Settings(strategy={"last_signal_time": "09:35"})
    ev = run(calendar, settings, "09:42")
    assert ev.status is SignalStatus.REJECTED
    assert {"after_cutoff", "past_signal_cutoff"} <= codes(ev)


def test_no_breakout_all_day_is_rejected(calendar, settings):
    flat = orb_today()[:4] + [(70.4, 70.45, 70.2, 70.3, 1000)] * 60
    ev = run(calendar, settings, "14:45", rows=flat)
    assert ev.status is SignalStatus.REJECTED
    assert {"no_breakout", "past_signal_cutoff"} <= codes(ev)


def test_missing_opening_range_candle(calendar, settings):
    provider = make_provider(calendar, settings, today_rows=orb_today())
    key = ("TEST", "5m")
    frame = provider.candles[key]
    provider.candles[key] = frame[frame["timestamp"] != at("09:20")].reset_index(drop=True)
    ev = evaluate(snapshot(provider, settings, calendar, at("09:42")), settings, calendar)
    assert ev.status is SignalStatus.INSUFFICIENT_DATA and "opening_range_incomplete" in codes(ev)


def test_insufficient_relative_volume_history(calendar, settings):
    ev = run(calendar, settings, "09:42", prior_sessions=2)
    assert ev.status is SignalStatus.INSUFFICIENT_DATA
    assert "relative_volume_unavailable" in codes(ev)


def test_no_intraday_data(calendar, settings):
    provider = make_provider(calendar, settings, today_rows=None, prior_sessions=0)
    ev = evaluate(snapshot(provider, settings, calendar, at("09:42")), settings, calendar)
    assert ev.status is SignalStatus.INSUFFICIENT_DATA and "intraday_data" in codes(ev)
