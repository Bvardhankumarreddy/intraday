"""Paper engine: lifecycle, fills, accounting, risk, persistence, reports."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.paper.engine import PaperEngine, PaperStateError
from app.paper.models import (
    InvalidTransition,
    OrderState,
    PositionState,
    SessionState,
    check_order_transition,
    order_id,
)
from app.paper.report import build_report
from app.paper.store import PaperStore
from tests.backtest_helpers import pad
from tests.paper_helpers import (
    BOTH,
    GAP_DOWN,
    NEUTRAL,
    STOP,
    TARGET,
    build,
    quote,
    run,
    steps,
)

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2025, 3, 12)


def t(hhmm: str, sec: int = 0) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(2025, 3, 12, h, m, sec, tzinfo=IST)


def only_position(store):
    rows = store.positions()
    assert len(rows) == 1, [dict(r) for r in rows]
    return rows[0]


# -- lifecycle ------------------------------------------------------------------


def test_order_state_machine():
    check_order_transition(OrderState.CREATED, OrderState.PENDING)
    check_order_transition(OrderState.PENDING, OrderState.FILLED)
    for bad in [(OrderState.FILLED, OrderState.PENDING), (OrderState.CREATED, OrderState.FILLED),
                (OrderState.EXPIRED, OrderState.FILLED)]:
        with pytest.raises(InvalidTransition):
            check_order_transition(*bad)
    assert "PARTIALLY_FILLED" not in OrderState.__members__  # not supported (documented)


def test_paper_ids_are_not_broker_ids():
    oid = order_id("NSE:SYNTH:ORB:x", "BUY")
    assert oid.startswith("PAPER-") and oid == order_id("NSE:SYNTH:ORB:x", "BUY")
    assert oid != order_id("NSE:SYNTH:ORB:x", "SELL")


# -- fills (hand-calculated, identical to the Phase 4 backtest) ------------------


def test_candle_fill_and_target_exit(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    run(engine, DAY)
    p = only_position(store)
    assert (p["entry_price"], p["entry_time"]) == ("71.05", t("09:40").isoformat())
    assert (p["exit_price"], p["exit_reason"], p["state"]) == ("72.2", "TARGET", "CLOSED")
    assert (p["quantity"], p["gross_pnl"], p["total_costs"], p["net_pnl"]) == \
        (28, "32.20", "16.51", "15.69")
    pf = store.portfolio()
    assert (Decimal(pf["cash"]), Decimal(pf["reserved"])) == (Decimal("2015.69"), 0)
    buy = store.order(order_id(p["signal_id"], "BUY"))
    assert buy["fill_basis"].startswith("candle open")
    assert buy["execution_model_version"].startswith("paper-v1")


def test_quote_fill_at_ask_has_no_spread_cost(calendar, tmp_path):
    q = quote("SYNTH", t("09:40"), ask=71.08)
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET], quotes={"SYNTH": q})
    run(engine, DAY, until="09:40")
    order = store.orders(DAY)[0]
    assert order["state"] == "FILLED" and order["fill_price"] == "71.08"
    assert order["fill_basis"].startswith("quote ask 71.08")
    assert Decimal(order["est_spread"]) == 0                       # spread is in the price
    assert Decimal(order["est_slippage"]) == Decimal("1.00")       # 1990.24 x 0.05% = 0.995 -> 1.00


def test_stale_quote_is_not_used(calendar, tmp_path):
    q = quote("SYNTH", t("09:30"), ask=71.08)  # older than the signal
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET], quotes={"SYNTH": q})
    run(engine, DAY)
    assert only_position(store)["entry_price"] == "71.05"  # fell back to candle open


def test_stop_exit(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, STOP])
    run(engine, DAY)
    p = only_position(store)
    assert (p["exit_price"], p["exit_reason"], p["gross_pnl"]) == ("70.4", "STOP", "-18.20")


def test_gap_through_stop(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, GAP_DOWN])
    run(engine, DAY)
    p = only_position(store)
    assert (p["exit_price"], p["exit_reason"]) == ("70.0", "STOP_GAP")
    assert Decimal(p["net_pnl"]) == Decimal("-45.83")  # same as the Phase 4 backtest


def test_ambiguous_candle_stop_first(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, BOTH])
    run(engine, DAY)
    p = only_position(store)
    assert (p["exit_price"], p["exit_reason"], p["ambiguous"]) == ("70.4",
                                                                   "AMBIGUOUS_STOP_FIRST", 1)


def test_end_of_day_exit_and_reconciliation(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL])
    run(engine, DAY)
    p = only_position(store)
    assert (p["exit_reason"], p["exit_time"]) == ("END_OF_DAY", t("15:10").isoformat())
    assert store.session(DAY)["state"] == SessionState.CLOSED.value
    engine.verify_ledger(t("15:40"))  # ledger reconciles


def test_entry_rejected_when_next_open_below_stop(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [(70.3, 70.4, 70.2, 70.3, 1000)])
    run(engine, DAY)
    order = store.orders(DAY)[0]
    assert order["state"] == "REJECTED" and "below stop" in order["reason"]
    assert store.positions() == [] and Decimal(store.portfolio()["reserved"]) == 0


def test_signals_only_after_opening_range_and_completed_candle(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    run(engine, DAY, until="09:35")
    assert store.signals(DAY) == []          # 09:35 candle still forming at 09:35:01
    engine.step(t("09:40", 1))
    sig = store.signals(DAY)[0]
    assert sig["signal_time"] == t("09:40").isoformat()


# -- idempotency, duplicates, restart ---------------------------------------------


def test_repeated_steps_are_idempotent(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    run(engine, DAY, until="09:40")
    before = (len(store.signals(DAY)), len(store.orders(DAY)), store.portfolio()["cash"])
    for _ in range(3):
        engine.step(t("09:40", 1))
    assert (len(store.signals(DAY)), len(store.orders(DAY)), store.portfolio()["cash"]) == before


def test_duplicate_order_prevented_by_store(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    run(engine, DAY, until="09:40")
    order = store.orders(DAY)[0]
    with store.transaction():
        assert store.create_order({k: order[k] for k in order.keys() if k != "state"}) is False
    assert len(store.orders(DAY)) == 1


def test_restart_recovery_matches_uninterrupted_run(calendar, tmp_path):
    ref_engine, _, ref_store = build(calendar, tmp_path / "ref", [NEUTRAL, TARGET])
    run(ref_engine, DAY)

    db = tmp_path / "restart.db"
    engine, provider, store = build(calendar, tmp_path, [NEUTRAL, TARGET], db=db)
    run(engine, DAY, until="09:45")  # position is open
    assert store.positions(states=(PositionState.OPEN,))
    store.close()

    store2 = PaperStore(db)  # "restart": new process state, same database
    engine2 = PaperEngine(provider, engine.settings, calendar, store2)
    assert engine2.open_session(DAY, t("09:46")) is SessionState.ACTIVE
    for when in steps(engine2, DAY):
        if when > t("09:45", 59):
            engine2.step(when)
    assert any(e["kind"] == "RECOVERY" for e in store2.events(DAY))
    assert len(store2.signals(DAY)) == 1 and len(store2.orders(DAY)) == 2
    p, ref = only_position(store2), only_position(ref_store)
    assert (p["net_pnl"], p["exit_price"]) == (ref["net_pnl"], ref["exit_price"])
    assert store2.portfolio()["cash"] == ref_store.portfolio()["cash"]


def test_tampered_ledger_refuses_to_resume(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    run(engine, DAY)
    with store.transaction():
        store.set_cash(Decimal("9999"), Decimal(0))
    with pytest.raises(PaperStateError, match="ledger mismatch"):
        engine.open_session(DAY, t("15:45"))


def test_portfolio_is_never_reset(calendar, tmp_path):
    engine, provider, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    run(engine, DAY)
    from tests.paper_helpers import paper_settings

    other = PaperEngine(provider, paper_settings(tmp_path, capital={"initial_capital": 500}),
                        calendar, store)
    other.open_session(DAY, t("15:50"))
    assert store.portfolio()["cash"] == "2015.69"
    assert any("NOT reset" in e["message"] for e in store.events(DAY))


def test_database_failure_rolls_back(calendar, tmp_path, monkeypatch):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    run(engine, DAY, until="09:40")
    pending = store.orders(DAY)[0]
    assert pending["state"] == "PENDING"
    cash_before = store.portfolio()["cash"]

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "create_position", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        engine.step(t("09:45", 1))
    assert store.order(pending["order_id"])["state"] == "PENDING"  # nothing half-applied
    assert store.portfolio()["cash"] == cash_before and store.positions() == []
    monkeypatch.undo()
    engine.step(t("09:45", 1))  # retry succeeds
    assert only_position(store)["entry_price"] == "71.05"


# -- capital, risk, accounting -------------------------------------------------------


def test_insufficient_capital_no_order(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET],
                             capital={"initial_capital": 60})
    run(engine, DAY)
    assert store.signals(DAY)[0]["review_status"] == "REJECTED_CAPITAL"
    assert store.orders(DAY) == []


def test_default_500_rupee_rules_reject_by_costs(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET],
                             capital={"initial_capital": 500},
                             risk={"risk_pct": 0.5, "include_costs_in_risk": True})
    run(engine, DAY)
    assert store.signals(DAY)[0]["review_status"] == "REJECTED_COSTS"
    assert Decimal(store.portfolio()["cash"]) == 500


def second_symbol():
    rows = [(70.0, 70.1, 69.9, 70.0, 1000)] * 15 + [(70.0, 70.65, 70.0, 70.6, 10_000)]
    return pad(rows)


def test_daily_loss_lock(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, GAP_DOWN],
                             extra={"SECOND": {DAY: second_symbol()}},
                             risk={"max_daily_loss": 40})
    run(engine, DAY)
    assert store.session(DAY)["locked"] == 1
    second = [s for s in store.signals(DAY) if s["symbol"] == "SECOND"][0]
    assert second["review_status"] == "REJECTED_RISK_LIMIT" and "daily_loss_lock" in \
        second["reasons"]


def test_cash_equity_and_unrealized(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, (71.2, 71.6, 71.1, 71.5, 1000)])
    run(engine, DAY, until="09:45")
    from app.paper.views import portfolio_summary

    s = portfolio_summary(store, DAY)
    # entry cash: 28 x 71.05 + 6.03 fees + 0.99 spread + 0.99 slippage = 1997.41
    assert s["cash"] == Decimal("2000") - Decimal("1997.41")
    assert s["position_value"] == Decimal("71.2") * 28        # marked at the 09:40 close
    assert s["unrealized_pnl"] == (Decimal("71.2") - Decimal("71.05")) * 28
    assert s["equity"] == s["cash"] + s["position_value"]
    assert s["realized_pnl_today"] == 0                       # nothing closed yet


def test_fee_accounting_breakdown(calendar, tmp_path):
    import json

    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    run(engine, DAY)
    p = only_position(store)
    costs = json.loads(p["costs"])
    assert costs["buy"]["brokerage"] == "5.00" and costs["sell"]["stt"] == "0.51"
    assert Decimal(costs["buy"]["total"]) + Decimal(costs["sell"]["total"]) == \
        Decimal(p["total_costs"])


# -- data problems -------------------------------------------------------------------


def test_missing_data_flags_position_without_inventing_exit(calendar, tmp_path):
    rows = pad([*__import__("tests.helpers", fromlist=["orb_today"]).orb_today(after=[NEUTRAL])],
               n=30)  # data stops at 11:45
    engine, _, store = build(calendar, tmp_path, days={DAY: rows},
                             backtest={"max_missing_candles_per_session": 60})
    run(engine, DAY)
    p = only_position(store)
    assert p["state"] == "FLAGGED" and p["exit_price"] is None
    assert store.session(DAY)["state"] == SessionState.NEEDS_ATTENTION.value
    assert any("No exit is assumed" in e["message"] for e in store.events(DAY))
    report = store.report(DAY)
    assert report["unresolved_positions"] and report["operational_errors"]


def test_flagged_position_resolved_from_late_data(calendar, tmp_path):
    from tests.helpers import orb_today

    db = tmp_path / "late.db"
    short = pad(orb_today(after=[NEUTRAL]), n=30)
    engine, _, store = build(calendar, tmp_path, days={DAY: short}, db=db,
                             backtest={"max_missing_candles_per_session": 60})
    run(engine, DAY)
    assert store.session(DAY)["state"] == "NEEDS_ATTENTION"
    store.close()
    # Next session: the full day's data is now available from the provider.
    nxt = date(2025, 3, 13)
    engine2, _, store2 = build(calendar, tmp_path, db=db,
                               days={DAY: pad(orb_today(after=[NEUTRAL])),
                                     nxt: pad([(71.2, 71.3, 71.1, 71.2, 1000)] * 3)},
                               backtest={"max_missing_candles_per_session": 60})
    run(engine2, nxt, until="09:30")
    p = [r for r in store2.positions(DAY)][0]
    assert (p["state"], p["exit_reason"]) == ("CLOSED", "END_OF_DAY")
    assert store2.session(DAY)["state"] == "CLOSED"
    assert store2.report(DAY)["unresolved_positions"] == []


def test_stale_data_blocks_new_signals(calendar, tmp_path):
    from tests.helpers import orb_today

    engine, _, store = build(calendar, tmp_path, days={DAY: pad(orb_today()[:3], n=3)},
                             backtest={"max_missing_candles_per_session": 75})
    run(engine, DAY, until="10:30")
    result = engine.step(t("10:30", 1))
    assert result.signals_blocked
    assert any(e["kind"] == "HEALTH" and "STALE" in e["message"] for e in store.events(DAY))
    assert store.signals(DAY) == []


# -- report ---------------------------------------------------------------------------


def test_daily_report_contents_and_reproducibility(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    run(engine, DAY)
    report = store.report(DAY)
    assert report["label"].startswith("PAPER TRADING - SIMULATED")
    assert report["signals_generated"] == 1 and report["signals_rejected"] == 0
    assert len(report["fills"]) == 2 and report["unfilled_or_expired"] == []
    assert report["totals"]["net_pnl"] == "15.69" and report["totals"]["total_costs"] == "16.51"
    assert report["totals"]["gross_pnl"] == "32.20"
    assert report["statistics"]["evidence"] == "INSUFFICIENT EVIDENCE"
    assert report["statistics"]["profit_factor"] is None  # no losses: undefined
    assert build_report(store, DAY, engine.settings) == report


def test_report_export(calendar, tmp_path):
    from app.paper.report import export_report

    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    run(engine, DAY)
    path = export_report(store, DAY, tmp_path / "out")
    assert path.exists() and (tmp_path / "out" / "paper_2025-03-12.md").exists()
    assert "SIMULATED" in (tmp_path / "out" / "paper_2025-03-12.md").read_text()


def test_pending_order_expires_at_session_end(calendar, tmp_path):
    # Signal fires but the next candle is missing until after valid_until.
    from tests.helpers import orb_today

    rows = orb_today()  # data ends right after the signal candle
    engine, _, store = build(calendar, tmp_path, days={DAY: pad(rows, n=5)},
                             backtest={"max_missing_candles_per_session": 75})
    run(engine, DAY)
    order = store.orders(DAY)[0]
    assert order["state"] == "EXPIRED"
    assert Decimal(store.portfolio()["reserved"]) == 0
    assert store.report(DAY)["unfilled_or_expired"][0]["state"] == "EXPIRED"


def test_holiday_and_special_session_preflight(calendar, tmp_path):
    engine, _, _ = build(calendar, tmp_path, [NEUTRAL])
    assert "not a trading day" in engine.preflight(date(2025, 3, 14))[0]
    assert "not a trading day" in engine.preflight(date(2025, 3, 15))[0]
    assert engine.preflight(DAY) == []
