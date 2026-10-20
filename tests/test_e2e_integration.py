"""Phase 9 end-to-end integration (spec section 4). Synthetic, deterministic
data and isolated databases only; this verifies wiring and accounting, not
strategy performance.

Scenario on 2025-03-12, Rs 2,000, max 1 trade/day:
- SYNTH: ORB signal at 09:40 -> paper BUY 28 @ 71.05 -> target 72.20 (09:45
  candle) -> net Rs 15.69 (hand-checked in Phase 4/5 tests).
- SECOND: breakout signal at 10:35 -> rejected by the risk engine
  (max trades per day reached) -> recorded, no order.
Then 2025-03-13: the provider fails all day -> the session must be INCOMPLETE,
never a "successful zero-trade" session, and health/alerts must show it.

Pipeline checked: market data -> data-quality ledger -> screening -> signal
-> risk/sizing -> paper order -> simulated fill -> costs -> accounting ->
persistence -> reports/dashboard, against the Phase 4 backtester for parity.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.backtesting.engine import run_backtest
from app.costs.calculator import round_trip
from app.evaluation.reconcile import reconcile
from app.evaluation.recorder import EvaluationRecorder
from app.evaluation.report import daily_report
from app.ops.alerts import AlertManager
from app.ops.health import health_report
from app.ops.reconciliation import full_reconciliation
from app.paper import views
from app.paper.report import build_report
from app.paper.scheduler import replay_session
from app.paper.store import PaperStore
from tests.backtest_helpers import make_dataset, scenario
from tests.ops_helpers import FlakyProvider
from tests.paper_helpers import NEUTRAL, TARGET, build
from tests.test_paper_engine import second_symbol

IST = ZoneInfo("Asia/Kolkata")
DAY, DAY2 = date(2025, 3, 12), date(2025, 3, 13)


@pytest.fixture(scope="module")
def session(tmp_path_factory, request):
    from app.config import MarketConfig
    from app.utils.market_calendar import MarketCalendar

    tmp = tmp_path_factory.mktemp("e2e")
    calendar = MarketCalendar(MarketConfig(), holidays={date(2025, 3, 14): "Test holiday"})
    engine, provider, store = build(
        calendar, tmp, [NEUTRAL, TARGET], extra={"SECOND": {DAY: second_symbol()}},
        risk={"max_trades_per_day": 1}, evaluation={"output_dir": str(tmp / "eval")})
    recorder = EvaluationRecorder(store, engine.settings, calendar)
    eid = recorder.create_evaluation("e2e", DAY, datetime(2025, 3, 12, 8, 0, tzinfo=IST))
    engine.recorder = recorder
    replay_session(engine, DAY)
    return {"calendar": calendar, "engine": engine, "provider": provider, "store": store,
            "eid": eid, "tmp": tmp}


def rows(store, sql, params=()):
    return [dict(r) for r in store.all(sql, params)]


def test_each_stage_uses_the_previous_stage_output(session):
    store = session["store"]
    sig = {s["symbol"]: s for s in rows(store, "SELECT * FROM signals")}
    assert set(sig) == {"SYNTH", "SECOND"}
    assert sig["SYNTH"]["review_status"] == "QUALIFIED_FOR_REVIEW"
    assert sig["SECOND"]["review_status"] == "REJECTED_RISK_LIMIT"
    assert "max_trades" in sig["SECOND"]["reasons"]
    orders = rows(store, "SELECT * FROM orders ORDER BY created_at")
    assert [(o["symbol"], o["side"], o["state"]) for o in orders] == [
        ("SYNTH", "BUY", "FILLED"), ("SYNTH", "SELL", "FILLED")]  # nothing for SECOND
    buy = orders[0]
    pos = rows(store, "SELECT * FROM positions")[0]
    # sizing -> order -> fill -> position carry the same numbers
    assert sig["SYNTH"]["quantity"] == buy["quantity"] == buy["fill_quantity"] == \
        pos["quantity"] == 28
    assert buy["signal_id"] == pos["signal_id"] == sig["SYNTH"]["signal_id"]
    assert Decimal(buy["reference_price"]) == Decimal(sig["SYNTH"]["entry_reference"])
    assert (pos["entry_price"], pos["exit_price"], pos["net_pnl"]) == ("71.05", "72.2", "15.69")
    # data-quality ledger and signal audit recorded every step for every symbol
    assert store.one("SELECT COUNT(DISTINCT ts) AS n FROM dq_batches")["n"] == 75
    assert {r["symbol"] for r in rows(store, "SELECT DISTINCT symbol FROM dq_batches")} == \
        {"SYNTH", "SECOND"}
    decisions = {(a["symbol"], a["decision"]) for a in rows(store, "SELECT * FROM signal_audit")}
    assert ("SYNTH", "ORDER_CREATED") in decisions
    assert ("SECOND", "SIGNAL_REJECTED_RISK") in decisions
    assert ("SECOND", "NO_SETUP_YET") in decisions  # no-trade decisions are recorded too


def test_timestamps_timezones_symbols_and_session_boundaries(session):
    store, cal = session["store"], session["calendar"]
    open_dt, close_dt = cal.session_bounds(DAY)
    stamps = []
    for table, cols in {"signals": ("signal_time", "valid_until", "created_at"),
                        "orders": ("created_at", "fill_time", "updated_at"),
                        "positions": ("entry_time", "exit_time", "mark_time"),
                        "snapshots": ("ts",), "events": ("ts",),
                        "signal_audit": ("ts",), "dq_batches": ("ts",)}.items():
        for r in rows(store, f"SELECT * FROM {table}"):
            stamps += [datetime.fromisoformat(r[c]) for c in cols if r.get(c)]
    assert stamps and all(s.utcoffset() == timedelta(hours=5, minutes=30) for s in stamps)
    sig = rows(store, "SELECT * FROM signals WHERE symbol = 'SYNTH'")[0]
    pos = rows(store, "SELECT * FROM positions")[0]
    signal_time = datetime.fromisoformat(sig["signal_time"])
    # The 09:35 candle can only be acted on once it has closed at 09:40.
    assert signal_time == datetime(2025, 3, 12, 9, 40, tzinfo=IST)
    assert datetime.fromisoformat(sig["created_at"]) >= signal_time
    assert signal_time <= datetime.fromisoformat(pos["entry_time"]) < \
        datetime.fromisoformat(pos["exit_time"]) <= close_dt
    early = rows(store, "SELECT * FROM signal_audit WHERE symbol = 'SYNTH' AND ts < ?",
                 (signal_time.isoformat(),))
    assert early and all(a["decision"] != "ORDER_CREATED" for a in early)
    for r in rows(store, "SELECT * FROM snapshots"):
        assert open_dt < datetime.fromisoformat(r["ts"]) <= close_dt + timedelta(minutes=1)
    syms = {r["symbol"] for t in ("signals", "orders", "positions")
            for r in rows(store, f"SELECT symbol FROM {t}")}
    assert syms <= {i.symbol for i in session["provider"].list_instruments()}


def test_fees_once_balances_reconcile_and_parity_with_backtest(session):
    store, engine, cal = session["store"], session["engine"], session["calendar"]
    pos = rows(store, "SELECT * FROM positions")[0]
    costs = json.loads(pos["costs"])
    assert set(costs) == {"buy", "sell"}
    assert Decimal(pos["total_costs"]) == Decimal(costs["buy"]["total"]) + \
        Decimal(costs["sell"]["total"])
    rates = engine.risk.schedule.rates_on(DAY, True)
    rt = round_trip(rates, Decimal("71.05"), Decimal("72.2"), 28, engine.settings.execution, None)
    assert (rt.total_costs, rt.net_pnl) == (Decimal(pos["total_costs"]), Decimal(pos["net_pnl"]))
    pf = store.portfolio()
    assert Decimal(pf["cash"]) == Decimal(pf["initial_capital"]) + Decimal(pos["net_pnl"])
    assert Decimal(pf["reserved"]) == 0
    for s in rows(store, "SELECT * FROM snapshots"):
        assert Decimal(s["equity"]) == Decimal(s["cash"]) + Decimal(s["position_value"])
    assert reconcile(store, DAY, engine.settings, engine.risk.schedule, session["provider"]) == []
    rec = full_reconciliation(store, engine.settings, engine.risk.schedule, DAY)
    assert rec["status"] == "OK", rec["findings"]
    # Same data, same settings through the Phase 4 backtester: same trade.
    ds = make_dataset(cal, engine.settings, {DAY: scenario([NEUTRAL, TARGET])},
                      extra={"SECOND": {DAY: second_symbol()}})
    bt = run_backtest(ds, engine.settings, cal, DAY, DAY)
    assert [(t.symbol, t.entry_price, t.exit_price, t.net_pnl, t.total_costs)
            for t in bt.trades] == [("SYNTH", Decimal("71.05"), Decimal("72.2"),
                                     Decimal(pos["net_pnl"]), Decimal(pos["total_costs"]))]
    assert [(r.symbol, r.status) for r in bt.rejected] == [("SECOND", "REJECTED_RISK_LIMIT")]


def test_dashboard_values_and_reports_agree_with_records(session):
    store, engine, eid = session["store"], session["engine"], session["eid"]
    summary = views.portfolio_summary(store, DAY)
    pf = store.portfolio()
    assert summary["cash"] == Decimal(pf["cash"]) and summary["equity"] == Decimal(pf["cash"])
    assert summary["realized_pnl_today"] == Decimal("15.69")
    assert summary["daily_pnl"] == Decimal("15.69") and summary["open_positions"] == 0
    table = views.positions_table(store, DAY)
    assert list(table["net_pnl"].astype(str)) == ["15.69"]
    stored = store.report(DAY)
    assert stored["totals"]["net_pnl"] == "15.69"
    assert build_report(store, DAY, engine.settings) == build_report(store, DAY, engine.settings)
    assert json.dumps(build_report(store, DAY, engine.settings), sort_keys=True, default=str) \
        == json.dumps(stored, sort_keys=True, default=str)  # reproducible from records
    a = daily_report(store, DAY, engine.settings, eid)
    b = daily_report(store, DAY, engine.settings, eid)
    assert a == b


def test_rerun_does_not_duplicate_anything(session):
    store, engine = session["store"], session["engine"]
    before = {t: store.one(f"SELECT COUNT(*) AS n FROM {t}")["n"]
              for t in ("signals", "orders", "positions")}
    cash = store.portfolio()["cash"]
    replay_session(engine, DAY)  # a full retry of the same session
    after = {t: store.one(f"SELECT COUNT(*) AS n FROM {t}")["n"]
             for t in ("signals", "orders", "positions")}
    assert after == before and store.portfolio()["cash"] == cash


def test_provider_failure_is_visible_and_never_a_successful_zero_trade_day(session):
    store, engine, eid = session["store"], session["engine"], session["eid"]
    flaky = FlakyProvider(engine.provider)
    flaky.failing = True
    engine.provider = flaky
    try:
        replay_session(engine, DAY2)
    finally:
        engine.provider = flaky.base
    ev = rows(store, "SELECT * FROM eval_sessions WHERE trading_date = ?", (DAY2.isoformat(),))[0]
    assert ev["status"] == "INCOMPLETE" and "data coverage 0%" in ev["status_reason"]
    assert store.signals(DAY2) == [] and store.positions(DAY2) == []
    report = daily_report(store, DAY2, engine.settings, eid)
    assert "no market-data batches were recorded" in json.dumps(report) or \
        report["2_market_data_health"]["steps_signals_blocked"] > 0
    alerts = {a["alert_key"] for a in AlertManager(store, engine.settings).open_alerts()}
    assert "data.blocked" in alerts
    rep = health_report(engine.settings, datetime(2025, 3, 13, 16, 0, tzinfo=IST), store.path)
    status = {c["check"]: c["status"] for c in rep["checks"]}
    assert status["market_data"] == "FAIL" and status["alerts"] in ("WARN", "FAIL")
    assert rep["status"] == "UNHEALTHY"


def test_isolated_store_is_not_the_project_database(session):
    from app.config import PROJECT_ROOT

    assert not str(session["store"].path).startswith(str(PROJECT_ROOT / "data"))
    assert isinstance(session["store"], PaperStore)


def test_missing_fee_configuration_is_surfaced(tmp_path):
    """A broker without a fee schedule blocks the session at preflight and
    shows in health; it is never treated as zero cost."""
    from app.config import MarketConfig, Settings
    from app.paper.engine import PaperEngine
    from app.utils.market_calendar import MarketCalendar

    cal = MarketCalendar(MarketConfig(), holidays={date(2025, 3, 14): "Test holiday"})
    s = Settings(broker={"name": "nosuchbroker"})
    engine = PaperEngine(None, s, cal, PaperStore(tmp_path / "f.db"))
    problems = engine.preflight(DAY)
    assert any("fee schedule unavailable" in p for p in problems)
    rep = health_report(s, datetime(2025, 3, 12, 8, 50, tzinfo=IST), tmp_path / "f.db")
    cfg = next(c for c in rep["checks"] if c["check"] == "config")
    assert cfg["status"] in ("WARN", "FAIL") and "fee schedule unavailable" in cfg["detail"]
