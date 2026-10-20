from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.risk.risk_limits import (
    DailyRiskState,
    OpenPosition,
    available_capital,
    limit_violations,
)

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2025, 3, 12)
S = Settings()  # max_daily_loss 15, max_trades 2, max_concurrent 1, max_aggregate 5


def codes(pair):
    unknown, bad = pair
    return {r.code for r in unknown} | {r.code for r in bad}


def position(loss="2", used="300"):
    return OpenPosition(signal_id="x", symbol="X", entry=Decimal("70"), quantity=4,
                        capital_used=Decimal(used), planned_net_loss=Decimal(loss))


def test_clean_state_passes():
    assert codes(limit_violations(DailyRiskState.new_session(DAY), S, "sig", Decimal("2"))) == set()


def test_daily_loss_lock():
    state = DailyRiskState.new_session(DAY)
    state.record_realized(-10, S)
    assert not state.locked
    state.record_realized(-5, S)  # total -15 reaches the limit
    assert state.locked and "15" in state.lock_reason
    assert "daily_loss_lock" in codes(limit_violations(state, S, "sig", Decimal("1")))


def test_lock_does_not_reset_mid_session(calendar):
    state = DailyRiskState.new_session(DAY)
    state.record_realized(-20, S)
    same_day = state.roll_to(datetime(2025, 3, 12, 14, 0, tzinfo=IST), calendar)
    assert same_day is state and same_day.locked
    after_close = state.roll_to(datetime(2025, 3, 12, 23, 59, tzinfo=IST), calendar)
    assert after_close.locked  # still the same exchange date


def test_lock_resets_on_next_trading_date(calendar):
    state = DailyRiskState.new_session(DAY)
    state.record_realized(-20, S)
    nxt = state.roll_to(datetime(2025, 3, 13, 9, 0, tzinfo=IST), calendar)
    assert not nxt.locked and nxt.realized_pnl == 0 and nxt.trading_date == date(2025, 3, 13)


def test_roll_backwards_rejected(calendar):
    with pytest.raises(ValueError):
        DailyRiskState.new_session(DAY).roll_to(datetime(2025, 3, 11, 10, tzinfo=IST), calendar)


def test_unknown_realized_pnl_is_not_zero():
    state = DailyRiskState(trading_date=DAY, realized_pnl=None)
    unknown, _ = limit_violations(state, S, "sig", Decimal("1"))
    assert [r.code for r in unknown] == ["realized_pnl_unknown"]
    with pytest.raises(ValueError):
        state.record_realized(-1, S)


def test_trade_count_and_concurrency():
    state = DailyRiskState.new_session(DAY)
    state.trades_taken = 2
    state.open_positions = [position()]
    c = codes(limit_violations(state, S, "sig", Decimal("1")))
    assert {"max_trades_reached", "max_positions_open"} <= c


def test_aggregate_and_daily_breach():
    s = Settings(risk={"max_concurrent_positions": 3})
    state = DailyRiskState.new_session(DAY)
    state.open_positions = [position(loss="4")]
    assert "aggregate_risk" in codes(limit_violations(state, s, "sig", Decimal("2")))  # 6 > 5
    state = DailyRiskState.new_session(DAY)
    state.realized_pnl = Decimal("-14")
    assert "would_breach_daily_loss" in codes(limit_violations(state, s, "sig", Decimal("2")))


def test_single_trade_above_daily_limit():
    s = Settings(risk={"max_aggregate_risk": 100})
    c = codes(limit_violations(DailyRiskState.new_session(DAY), s, "sig", Decimal("16")))
    assert "trade_loss_exceeds_daily_limit" in c


def test_duplicate_and_previously_rejected():
    state = DailyRiskState.new_session(DAY)
    state.seen_signal_ids = {"a"}
    state.rejected_signal_ids = {"b"}
    assert "duplicate_candidate" in codes(limit_violations(state, S, "a", Decimal("1")))
    assert "previously_rejected" in codes(limit_violations(state, S, "b", Decimal("1")))


def test_available_capital_after_positions_reserve_and_losses():
    s = Settings(risk={"min_cash_reserve": 50})
    state = DailyRiskState.new_session(DAY)
    state.open_positions = [position(used="300")]
    state.realized_pnl = Decimal("-10")
    assert available_capital(state, s) == Decimal("140")  # 500 - 50 - 300 - 10
    state.realized_pnl = Decimal("25")  # gains are not added back
    assert available_capital(state, s) == Decimal("150")
