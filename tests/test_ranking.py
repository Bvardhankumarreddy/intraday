from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.data.memory_provider import InMemoryProvider
from app.data.mock_provider import MockMarketDataProvider
from app.data.models import DataMode
from app.risk.interfaces import TradeEvaluationRequest
from app.scan.models import (
    Candidate,
    Indicators,
    ScanDecision,
    ScreenResult,
    ScreenStatus,
    SignalStatus,
)
from app.scan.pipeline import run_scan
from app.screening.ranking import rank, score_candidate
from app.strategies.opening_range_breakout import SignalEvaluation
from tests.helpers import at, make_provider, orb_today

IST = ZoneInfo("Asia/Kolkata")


def inputs(atv=5e6 * 20, rv=3.0, close_location=1.0, extension=0.0, vwap_dist=1.0, spread=0.0):
    screen = ScreenResult(status=ScreenStatus.PASS, avg_traded_value=atv, spread_pct=spread)
    ev = SignalEvaluation(
        status=SignalStatus.QUALIFIED_PRE_COST,
        indicators=Indicators(relative_volume=rv),
        close_location=close_location,
        extension_pct=extension,
        vwap_distance_pct=vwap_dist,
    )
    return screen, ev


def points(components):
    return {c.name: c.points for c in components}


def test_maximum_score_is_100():
    score, parts = score_candidate(*inputs(), Settings())
    assert score == 100
    assert points(parts) == {"liquidity": 25, "relative_volume": 25, "breakout_quality": 20,
                             "trend": 15, "spread": 15}


def test_component_normalisation():
    screen, ev = inputs(atv=5e6, rv=2.25, close_location=0.5, extension=0.5,
                        vwap_dist=0.5, spread=0.05)
    _, parts = score_candidate(screen, ev, Settings())
    assert points(parts) == {
        "liquidity": 0,  # at the screening minimum
        "relative_volume": 12.5,  # halfway from 1.5 to 3.0
        "breakout_quality": 10,  # (0.5 + (1 - 0.5/1.0)) / 2 = 0.5
        "trend": 7.5,  # 0.5% of the 1% full-score distance
        "spread": 7.5,  # half the 0.10% maximum
    }


def test_missing_inputs_score_zero_and_are_flagged():
    screen, ev = inputs(spread=None)
    ev.indicators = Indicators(relative_volume=None)
    _, parts = score_candidate(screen, ev, Settings())
    by_name = {c.name: c for c in parts}
    assert by_name["spread"].points == 0 and by_name["spread"].normalized is None
    assert "unavailable" in by_name["spread"].note
    assert by_name["relative_volume"].points == 0


def test_weights_must_sum_to_100():
    with pytest.raises(ValidationError):
        Settings(scoring={"weight_liquidity": 30})
    assert Settings(scoring={"weight_liquidity": 30, "weight_spread": 10}).scoring


def candidate(symbol, score, atv, status=SignalStatus.QUALIFIED_PRE_COST):
    return Candidate(
        symbol=symbol, name=symbol, exchange="NSE", evaluated_at=datetime(2025, 3, 12, tzinfo=IST),
        data_mode=DataMode.SIMULATED, data_source="t",
        screen=ScreenResult(status=ScreenStatus.PASS, avg_traded_value=atv),
        signal_status=status, score=score, config_version="x",
    )


def test_rank_is_deterministic_and_excludes_unqualified():
    pool = [
        candidate("CCC", 80, 1e7),
        candidate("BBB", 80, 2e7),  # same score, more liquid -> first
        candidate("AAA", 80, 1e7),  # same score and liquidity as CCC -> symbol order
        candidate("ZZZ", 99, 9e9, status=SignalStatus.REJECTED),
        candidate("DDD", 90, 1e7, status=SignalStatus.EXPIRED),
        candidate("EEE", 95, 1e7),
    ]
    order = [c.symbol for c in rank(pool)]
    assert order == ["EEE", "BBB", "AAA", "CCC"]
    assert order == [c.symbol for c in rank(list(reversed(pool)))]


# -- pipeline --------------------------------------------------------------


def combine(calendar, *providers):
    instruments, candles, quotes = [], {}, {}
    for p in providers:
        instruments += p.instruments
        candles.update(p.candles)
        quotes.update(p.quotes)
    return InMemoryProvider(calendar, instruments, candles, quotes)


def test_scan_ranks_qualified_and_reports_rejections(calendar):
    s = Settings()
    good = make_provider(calendar, s, today_rows=orb_today(), symbol="GOOD")
    weak = make_provider(calendar, s, today_rows=orb_today(breakout_volume=1000), symbol="WEAK")
    # Same breakout as GOOD, but fails the liquidity screen: mandatory rules beat score.
    illiq = make_provider(calendar, s, today_rows=orb_today(), symbol="ILLIQ", daily_volume=100)
    result = run_scan(combine(calendar, good, weak, illiq), s, calendar, at("09:42"))

    assert result.decision is ScanDecision.CANDIDATES_FOR_REVIEW
    assert [c.symbol for c in result.ranked] == ["GOOD"]
    by = {c.symbol: c for c in result.candidates}
    assert by["GOOD"].score is not None and 0 <= by["GOOD"].score <= 100
    assert by["GOOD"].risk_cost_status == "NOT_EVALUATED"
    assert by["WEAK"].signal_status is SignalStatus.WATCH and by["WEAK"].score is None
    assert by["ILLIQ"].signal_status is SignalStatus.NOT_EVALUATED and by["ILLIQ"].score is None
    assert "illiquid" in {r.code for r in by["ILLIQ"].rejection_reasons}
    assert all(c.config_version == s.version() for c in result.candidates)
    assert any("NOT evaluated" in n for n in result.notes)


def test_scan_returns_no_trade(calendar):
    s = Settings()
    weak = make_provider(calendar, s, today_rows=orb_today(breakout_volume=1000), symbol="WEAK")
    result = run_scan(weak, s, calendar, at("09:42"))
    assert result.decision is ScanDecision.NO_TRADE and result.ranked == []


def test_duplicate_listing_does_not_duplicate_signal(calendar):
    s = Settings()
    p = make_provider(calendar, s, today_rows=orb_today(), symbol="GOOD")
    p.instruments = p.instruments * 2
    result = run_scan(p, s, calendar, at("09:42"))
    assert len(result.ranked) == 1
    assert "duplicate_signal" in {r.code for r in result.candidates[1].rejection_reasons}


def test_scan_on_mock_is_deterministic_and_screens_cases(calendar):
    s = Settings()
    as_of = at("11:00")
    run = lambda: run_scan(MockMarketDataProvider(calendar, seed=42, as_of=as_of),  # noqa: E731
                           s, calendar, as_of)
    first, second = run(), run()
    assert first.model_dump() == second.model_dump()
    by = {c.symbol: c for c in first.candidates}
    codes = lambda sym: {r.code for r in by[sym].screen.rejections}  # noqa: E731
    assert "illiquid" in codes("SIMDELTA")
    assert "price_out_of_band" in codes("SIMEPSILON") | codes("SIMZETA")
    assert "not_tradable" in codes("SIMETA")
    assert first.data_mode is DataMode.SIMULATED


def test_trade_evaluation_request_handoff(calendar):
    s = Settings()
    good = make_provider(calendar, s, today_rows=orb_today(), symbol="GOOD")
    cand = run_scan(good, s, calendar, at("09:42")).ranked[0]
    req = TradeEvaluationRequest.from_candidate(cand, s)
    assert req.available_capital == 500 and req.risk_pct == 0.5 and req.broker == "groww"
    assert req.setup.entry_reference == 71.0 and req.setup.signal_time == at("09:40")

    no_setup = cand.model_copy(update={"setup": None})
    with pytest.raises(ValueError):
        TradeEvaluationRequest.from_candidate(no_setup, s)


def test_config_version_changes_with_settings():
    assert Settings().version() == Settings().version()
    assert Settings().version() != Settings(risk={"risk_pct": 0.25}).version()
