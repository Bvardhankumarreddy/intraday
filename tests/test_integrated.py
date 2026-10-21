"""Phase 12: integrated baseline + AI-assisted paper trading. ALL DATA IS
SYNTHETIC (tests/paper_helpers.py); deterministic mock LLM; isolated
databases. No paid API, no live session, no broker.

Fixture: SYNTH breaks out at 09:40 (entry ref 71.00, stop 70.40, target 72.20).
With Rs 2,000 and 10% risk the risk engine approves 28 shares; the 09:40
candle opens 71.05 and the target exit gives net Rs 15.69.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.agent.llm import LLMResponse, LLMTimeout, MockProvider, ToolUse
from app.config import PROJECT_ROOT
from app.data.models import DataMode
from app.integrated.engine import IntegratedEngine, build_integrated
from app.integrated.report import build_daily, write_daily
from app.paper.engine import GATE_REJECTED
from app.paper.scheduler import SessionScheduler, replay_session
from app.paper.store import PaperStore
from tests.backtest_helpers import make_dataset, scenario
from tests.paper_helpers import GAP_DOWN, NEUTRAL, TARGET, paper_settings, steps

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2025, 3, 12)
APP = PROJECT_ROOT / "app"
REAL_PROTOCOL = PROJECT_ROOT / "config" / "ai_eval" / "protocol.yaml"


def t(hhmm: str, day: date = DAY) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, 1, tzinfo=IST)


def make(calendar, tmp_path, after=(NEUTRAL, TARGET), llm=None, mode=None, days=None,
         a_db="a.db", b_db="b.db", **over):
    over = {**over, "agent": {"reports_dir": str(tmp_path / "agent"),
                              **over.get("agent", {})},
            "integrated": {"reports_dir": str(tmp_path / "reports"),
                           **over.get("integrated", {})},
            "evaluation": {"output_dir": str(tmp_path / "eval"),
                           **over.get("evaluation", {})}}
    s = paper_settings(tmp_path, **over)
    prov = make_dataset(calendar, s, days or {DAY: scenario(list(after))}).provider(calendar)
    if mode is not None:
        prov.mode = mode
    eng = build_integrated(s, calendar, prov, PaperStore(tmp_path / a_db),
                           PaperStore(tmp_path / b_db), llm or MockProvider())
    return eng


def run_day(eng: IntegratedEngine, day: date = DAY, until: str | None = None):
    open_dt, _ = eng.calendar.session_bounds(day)
    eng.open_session(day, open_dt - timedelta(minutes=15))
    for when in steps(eng.baseline, day, until):
        eng.step(when)


def arm(store) -> dict:
    return {"signals": [dict(r) for r in store.all("SELECT * FROM signals")],
            "orders": [dict(r) for r in store.all("SELECT * FROM orders WHERE side = 'BUY'")],
            "positions": [dict(r) for r in store.all("SELECT * FROM positions")]}


def submit(rec="consider", symbol="SYNTH") -> LLMResponse:
    body = {"market_context": [], "source_disagreements": [],
            "candidates": [{"symbol": symbol, "observed_facts": [
                {"text": "screen passed", "basis": "verified_fact", "evidence_ids": ["T3"]}],
                "supporting_evidence": [], "contradicting_evidence": [],
                "alternative_explanations": [], "evidence_strength": "correlation_only",
                "entry_conditions": ["above stop"], "invalidation_conditions": ["stop"],
                "risk_cost_assessment": "see risk tool", "missing_information": [],
                "recommendation": rec, "rationale": "scripted"}],
            "overall": "candidates_for_review", "overall_rationale": "ok"}
    return LLMResponse("scripted", [], [ToolUse("s1", "submit_research", body)], "tool_use",
                       10, 20, 0.5, "req", {})


# -- end to end ---------------------------------------------------------------------


def test_end_to_end_both_arms_same_data_costs_and_fills(calendar, tmp_path):
    eng = make(calendar, tmp_path)
    run_day(eng)
    a, b = arm(eng.baseline.store), arm(eng.ai.store)
    assert [p["net_pnl"] for p in a["positions"]] == ["15.69"]
    assert [(p["entry_price"], p["exit_price"], p["quantity"], p["total_costs"], p["net_pnl"])
            for p in a["positions"]] == \
        [(p["entry_price"], p["exit_price"], p["quantity"], p["total_costs"], p["net_pnl"])
         for p in b["positions"]]
    cand = eng.ai.store.one("SELECT * FROM agent_recommendations WHERE status = 'CANDIDATE'")
    order = b["orders"][0]
    assert cand["created_at"] <= order["created_at"]  # persisted before the paper order
    assert eng.baseline.store.session(DAY)["state"] == eng.ai.store.session(DAY)["state"] \
        == "CLOSED"
    assert not eng.baseline.store.all("SELECT 1 FROM sqlite_master WHERE name = "
                                      "'agent_recommendations'")  # baseline has no AI records


def test_ai_wait_vetoes_only_the_ai_arm(calendar, tmp_path):
    llm = MockProvider(script=[submit("wait_for_confirmation")] * 40)
    eng = make(calendar, tmp_path, llm=llm)
    run_day(eng, until="10:00")
    a, b = arm(eng.baseline.store), arm(eng.ai.store)
    assert len(a["orders"]) == 1 and b["orders"] == []
    assert b["signals"][0]["review_status"] == GATE_REJECTED
    assert "AI-assisted status WATCH" in b["signals"][0]["reasons"]


def test_snapshot_gives_identical_data_and_fewer_requests(calendar, tmp_path):
    eng = make(calendar, tmp_path)
    calls = []
    inner = eng.snapshot.inner
    original = inner.get_candles
    inner.get_candles = lambda *a, **k: (calls.append(a), original(*a, **k))[1]
    eng.open_session(DAY, t("09:00"))
    eng.step(t("09:40"))
    assert calls and len(calls) == len(set(calls))  # each request reached the feed once
    assert eng.snapshot.requests == len(eng.snapshot._cache)


# -- AI failures and fallback -----------------------------------------------------


def test_llm_timeout_blocks_new_ai_positions_baseline_unaffected(calendar, tmp_path):
    eng = make(calendar, tmp_path, llm=MockProvider(script=[LLMTimeout("timeout")] * 80))
    run_day(eng)
    a, b = arm(eng.baseline.store), arm(eng.ai.store)
    assert len(a["positions"]) == 1 and b["positions"] == []
    assert b["signals"][0]["review_status"] == GATE_REJECTED
    events = [e["message"] for e in eng.ai.store.all("SELECT message FROM events WHERE kind = "
                                                     "'INTEGRATION'")]
    assert any("AI UNAVAILABLE" in m for m in events)
    rel = build_daily(eng.settings, calendar, eng.baseline.store, eng.ai.store, DAY)
    assert rel["reliability"]["model_errors"] > 0
    assert rel["reliability"]["ai_provider_available"] is False


def test_malformed_model_output_blocks_ai_arm(calendar, tmp_path):
    bad = LLMResponse("s", [], [ToolUse("b", "submit_research", {"overall": "yes"})],
                      "tool_use", 1, 1, 0.1, None, {})
    eng = make(calendar, tmp_path, llm=MockProvider(script=[bad] * 200))
    run_day(eng, until="10:00")
    assert arm(eng.ai.store)["orders"] == [] and len(arm(eng.baseline.store)["orders"]) == 1


def test_model_request_and_tool_limits_hold_in_integration(calendar, tmp_path):
    loop = LLMResponse("s", [], [ToolUse(f"c{i}", "get_candles", {"symbol": "SYNTH"})
                                 for i in range(3)], "tool_use", 1, 1, 0.1, None, {})
    eng = make(calendar, tmp_path, llm=MockProvider(script=[loop] * 400),
               agent={"max_model_requests": 2, "max_tool_calls": 12})
    run_day(eng, until="10:00")
    per_run = [r["n"] for r in eng.ai.store.all(
        "SELECT COUNT(*) AS n FROM agent_model_calls GROUP BY run_id")]
    tools = [r["n"] for r in eng.ai.store.all(
        "SELECT COUNT(*) AS n FROM agent_tool_calls GROUP BY run_id")]
    assert per_run and max(per_run) <= 2 and max(tools) <= 12
    assert arm(eng.ai.store)["orders"] == []


def test_unauthorized_tool_call_is_refused_and_powerless(calendar, tmp_path):
    attempt = LLMResponse("s", [], [ToolUse("x", "place_order", {"symbol": "SYNTH"})],
                          "tool_use", 1, 1, 0.1, None, {})
    eng = make(calendar, tmp_path, llm=MockProvider(script=[attempt, submit()] * 40))
    run_day(eng, until="10:00")
    refused = eng.ai.store.all("SELECT * FROM agent_tool_calls WHERE tool = 'place_order'")
    assert refused and all(not r["ok"] and "unknown tool" in r["error"] for r in refused)
    assert len(arm(eng.ai.store)["orders"]) <= 1  # only the gated engine path can order


def test_ai_arm_failures_are_isolated_from_the_baseline(calendar, tmp_path, monkeypatch):
    eng = make(calendar, tmp_path)

    def boom(now):
        raise RuntimeError("simulated AI-arm crash")

    monkeypatch.setattr(eng.ai, "step", boom)
    run_day(eng)
    assert len(arm(eng.baseline.store)["positions"]) == 1
    assert DAY in eng.ai_disabled
    errors = eng.ai.store.all("SELECT message FROM events WHERE kind = 'INTEGRATION' AND "
                              "level = 'ERROR'")
    assert any("stopped for the day" in e["message"] for e in errors)


# -- data quality and session boundaries --------------------------------------------


def test_stale_data_blocks_both_arms_and_the_agent(calendar, tmp_path):
    from app.data.models import DataUnavailableError

    eng = make(calendar, tmp_path)
    eng.open_session(DAY, t("09:00"))

    def down(*a, **k):
        raise DataUnavailableError("feed down")

    eng.snapshot.inner.get_candles = down
    eng.step(t("09:40"))  # the breakout candle exists, but the feed is down
    assert eng.baseline.store.get_meta("heartbeat")["signals_blocked"] is True
    assert arm(eng.baseline.store)["orders"] == [] and arm(eng.ai.store)["orders"] == []
    runs = eng.ai.store.all("SELECT status, reason FROM agent_runs ORDER BY started_at DESC")
    assert runs[0]["status"] == "ABSTAINED" and "stale" in runs[0]["reason"]


def test_agent_not_run_outside_the_trading_window(calendar, tmp_path):
    eng = make(calendar, tmp_path)
    assert not eng._agent_due(t("09:15"))  # no completed candle
    assert eng._agent_due(t("09:20"))
    assert not eng._agent_due(t("15:20"))  # at/after the end-of-day exit time
    assert make(calendar, tmp_path / "n3", integrated={"agent_every_n_candles": 3}) \
        ._agent_due(t("09:30")) is True


def test_holiday_is_not_traded(calendar, tmp_path):
    eng = make(calendar, tmp_path)
    holiday = date(2025, 3, 15)  # Saturday
    sched = SessionScheduler(eng, clock=lambda: t("10:00", holiday), sleep=lambda s: None)
    assert sched.run() is None
    assert eng.baseline.store.session(holiday) is None and eng.ai.store.session(holiday) is None


# -- duplicates, restarts, immutability ------------------------------------------------


def test_repeated_steps_never_duplicate_orders_or_recommendations(calendar, tmp_path):
    eng = make(calendar, tmp_path)
    eng.open_session(DAY, t("09:00"))
    for _ in range(3):
        eng.step(t("09:40"))
    for store in (eng.baseline.store, eng.ai.store):
        assert len(arm(store)["orders"]) == 1
    recs = eng.ai.store.all("SELECT recommendation_id FROM agent_recommendations")
    assert len(recs) == len({r["recommendation_id"] for r in recs})


def test_restart_with_open_positions_recovers_both_arms(calendar, tmp_path):
    eng = make(calendar, tmp_path)
    run_day(eng, until="09:45")
    assert [p["state"] for p in arm(eng.ai.store)["positions"]] == ["OPEN"]
    eng.baseline.store.close()
    eng.ai.store.close()
    again = make(calendar, tmp_path)  # same databases, new process
    open_dt, _ = again.calendar.session_bounds(DAY)
    again.open_session(DAY, t("09:47"))
    for when in [w for w in steps(again.baseline, DAY) if w > t("09:46")]:
        again.step(when)
    fresh = make(calendar, tmp_path / "ref")
    run_day(fresh)
    for store, ref in ((again.baseline.store, fresh.baseline.store),
                       (again.ai.store, fresh.ai.store)):
        got = [(p["state"], p["net_pnl"]) for p in arm(store)["positions"]]
        assert got == [(p["state"], p["net_pnl"]) for p in arm(ref)["positions"]]
        assert len(arm(store)["orders"]) == len(arm(ref)["orders"])
        assert store.all("SELECT 1 FROM events WHERE kind = 'RECOVERY'")


def test_recommendations_immutable_in_ai_arm(calendar, tmp_path):
    eng = make(calendar, tmp_path)
    run_day(eng, until="09:45")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        eng.ai.store.conn.execute("UPDATE agent_recommendations SET status = 'CANDIDATE'")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        eng.ai.store.conn.execute("DELETE FROM agent_recommendations")


def test_gate_uses_only_recommendations_persisted_before_the_decision(calendar, tmp_path):
    eng = make(calendar, tmp_path, llm=MockProvider(script=[submit("wait_for_confirmation")]
                                                    * 40))
    run_day(eng, until="09:45")

    class C:
        symbol = "SYNTH"

        class setup:
            signal_id = arm(eng.baseline.store)["signals"][0]["signal_id"]

    # forge a CANDIDATE "from the future": the gate must not see it at 09:40
    body = {"signal_id": C.setup.signal_id}
    eng.ai.store.conn.execute(
        "INSERT INTO agent_recommendations (recommendation_id, run_id, market_date, window, "
        "symbol, status, created_at, schema_version, body, sha256) VALUES ('REC-future', 'x', "
        "?, 'intraday', 'SYNTH', 'CANDIDATE', ?, 'v', ?, 'x')",
        (DAY.isoformat(), t("11:00").isoformat(), json.dumps(body)))
    assert eng.gate(C, t("09:40"))[0] is False
    assert eng.gate(C, t("11:00"))[0] is True


# -- risk, capital, costs, fills, accounting --------------------------------------------


def test_rs500_risk_rejection_applies_to_both_arms(calendar, tmp_path):
    rs500 = {"capital": {"initial_capital": 500},
             "risk": {"risk_pct": 0.5, "include_costs_in_risk": True, "max_daily_loss": 15,
                      "max_aggregate_risk": 5, "max_trades_per_day": 2},
             "cost_review": {"min_net_reward_risk": 1.5}}
    eng = make(calendar, tmp_path, llm=MockProvider(script=[submit("consider")] * 80), **rs500)
    run_day(eng, until="10:00")
    for store in (eng.baseline.store, eng.ai.store):
        a = arm(store)
        assert a["orders"] == [] and a["signals"][0]["review_status"] != "QUALIFIED_FOR_REVIEW"
        assert a["signals"][0]["review_status"] != GATE_REJECTED  # risk decided, not the AI


def test_rs500_whole_shares_and_cost_infeasibility(calendar, tmp_path):
    """At Rs 500 the risk engine sizes whole shares within capital, then rejects: costs
    (Rs 12.80) exceed the gross profit at target (Rs 7.20) - the Phase 9 finding."""
    eng = make(calendar, tmp_path, capital={"initial_capital": 500})
    run_day(eng)
    for store in (eng.baseline.store, eng.ai.store):
        sig = arm(store)["signals"][0]
        assert isinstance(sig["quantity"], int) and sig["quantity"] == 6
        assert 6 * Decimal(sig["entry_reference"]) <= Decimal(500)
        assert sig["review_status"] == "REJECTED_COSTS" and "net Rs -5.60" in sig["reasons"]
        assert arm(store)["orders"] == [] and Decimal(store.portfolio()["cash"]) == 500


def test_costs_reconcile_and_are_recorded_once(calendar, tmp_path):
    from app.costs.calculator import round_trip
    from app.costs.schedule import load_fee_schedule
    from app.evaluation.reconcile import reconcile
    from app.risk.engine import RiskCostEngine

    eng = make(calendar, tmp_path)
    run_day(eng)
    s = eng.settings
    for store in (eng.baseline.store, eng.ai.store):
        p = arm(store)["positions"][0]
        rates = load_fee_schedule(s).rates_on(DAY, True)
        rt = round_trip(rates, Decimal(p["entry_price"]), Decimal(p["exit_price"]),
                        p["quantity"], s.execution, None)
        assert Decimal(p["total_costs"]) == rt.total_costs
        assert Decimal(p["net_pnl"]) == Decimal(p["gross_pnl"]) - Decimal(p["total_costs"])
        assert reconcile(store, DAY, s, RiskCostEngine(s).schedule) == []


def test_unfilled_orders_are_not_trades(calendar, tmp_path):
    eng = make(calendar, tmp_path, after=(GAP_DOWN, NEUTRAL))
    run_day(eng)
    for store in (eng.baseline.store, eng.ai.store):
        a = arm(store)
        assert a["positions"] == [] and a["orders"][0]["state"] in ("EXPIRED", "CANCELLED",
                                                                    "REJECTED")
        assert Decimal(store.portfolio()["cash"]) == Decimal(store.portfolio()["initial_capital"])


def test_financial_reconciliation_of_both_portfolios(calendar, tmp_path):
    eng = make(calendar, tmp_path)
    run_day(eng)
    for store in (eng.baseline.store, eng.ai.store):
        pf = store.portfolio()
        closed = sum(Decimal(p["net_pnl"]) for p in arm(store)["positions"])
        assert Decimal(pf["cash"]) == Decimal(pf["initial_capital"]) + closed
        assert Decimal(pf["reserved"]) == 0
        snap = store.one("SELECT * FROM snapshots ORDER BY ts DESC LIMIT 1")
        assert Decimal(snap["equity"]) == Decimal(snap["cash"]) + Decimal(snap["position_value"])


# -- reports and metrics ---------------------------------------------------------------


def test_daily_report_is_reproducible_and_reconciles(calendar, tmp_path):
    eng = make(calendar, tmp_path)
    run_day(eng)
    path = write_daily(eng.settings, calendar, eng.baseline.store, eng.ai.store, DAY)
    first = path.read_bytes()
    write_daily(eng.settings, calendar, eng.baseline.store, eng.ai.store, DAY)
    assert path.read_bytes() == first
    body = json.loads((path.parent / "integrated_daily.json").read_text())
    assert body["arms"]["A"]["net_pnl"] == body["arms"]["B"]["net_pnl"] == "15.69"
    assert body["arms"]["A"]["available_cash"] == "2015.69"
    cand = [c for c in body["candidates"] if c["status"] == "CANDIDATE"]
    assert cand and cand[0]["financial_calc"]["quantity"] == 28
    text = path.read_text()
    for marker in ("[FACT]", "[CALC]", "[AI]", "News: UNAVAILABLE", "Session reliability"):
        assert marker in text


def test_fee_schedule_outdated_or_missing_is_flagged(calendar, tmp_path):
    eng = make(calendar, tmp_path, evaluation={"max_fee_schedule_age_days": 1})
    late = date(2027, 1, 4)
    body = build_daily(eng.settings, calendar, eng.baseline.store, eng.ai.store, late)
    assert "verified" in body["fee_schedule"]["warning"]
    missing = make(calendar, tmp_path / "m", broker={"fee_schedule_file": str(tmp_path / "x")})
    assert missing.preflight(DAY)  # refuses to trade without a fee schedule


def test_baseline_vs_ai_cumulative_metrics(calendar, tmp_path):
    from app.aieval.ledger import Ledger
    from app.aieval.prospective import evaluate_integrated
    from app.evaluation.recorder import EvaluationRecorder
    from tests.robustness_helpers import frozen_baseline
    from tests.test_aieval import make_protocol

    days = {DAY: scenario([NEUTRAL, TARGET]), date(2025, 3, 13): scenario([NEUTRAL, TARGET])}
    llm = MockProvider(script=([submit("consider")] * 3 + [submit("no_trade")] * 400))
    eng = make(calendar, tmp_path, llm=llm, mode=DataMode.LIVE, days=days)
    for store, nm in ((eng.baseline.store, "x"), (eng.ai.store, "x [AI]")):
        EvaluationRecorder(store, eng.settings, calendar).create_evaluation(nm, DAY, t("08:00"))
    for d in sorted(days):
        replay_session(eng, d)
    proto = make_protocol(tmp_path, frozen_baseline(tmp_path), MockProvider(), eng.settings)
    proto.write_text(re.sub(r"effective_date: .*", "effective_date: 2025-01-01",
                            proto.read_text()))
    live = eng.settings.model_copy(update={"ai_eval": eng.settings.ai_eval.model_copy(update={
        "ledger_db": tmp_path / "ledger.db", "output_dir": tmp_path / "ai_eval"})})
    out = evaluate_integrated(live, eng.baseline.store, eng.ai.store,
                              Ledger(tmp_path / "ledger.db"), llm=MockProvider(),
                              protocol_path=proto, now=t("18:00", date(2025, 3, 13)))
    assert out["status"] == "OK" and out["coverage"]["sessions"] == 2
    a, b = out["arms"]["A"], out["arms"]["B"]
    assert a["closed_trades"] == 2 and b["closed_trades"] == 1
    assert out["incremental"]["net_pnl_difference"] == pytest.approx(
        b["net_pnl"] - a["net_pnl"])
    assert out["discordant_signals"] == 1
    assert out["conclusion"]["conclusion"] == "INCONCLUSIVE"  # tiny sample
    assert b["decisions"]["VETOED_BY_AI"] == 1


def test_cumulative_without_sessions_is_pending(calendar, tmp_path):
    from app.aieval.ledger import Ledger
    from app.aieval.prospective import evaluate_integrated

    eng = make(calendar, tmp_path)
    out = evaluate_integrated(eng.settings, eng.baseline.store, eng.ai.store,
                              Ledger(tmp_path / "l.db"), llm=MockProvider(),
                              now=t("18:00"))
    assert out["status"] == "PENDING_DATA"


# -- safety -----------------------------------------------------------------------------


def test_separate_databases_required(calendar, tmp_path):
    eng = make(calendar, tmp_path)
    with pytest.raises(ValueError, match="separate"):
        IntegratedEngine(eng.baseline, eng.baseline, eng.agent, eng.snapshot)


def test_no_real_order_endpoint_in_integration_code():
    for p in (APP / "integrated").glob("*.py"):
        imps = set()
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Import):
                imps |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imps.add(node.module)
        assert not {i for i in imps if "groww" in i or i in ("requests", "socket",
                                                              "subprocess")}, p.name
        src = p.read_text()
        for word in ("place_order", "modify_order", "cancel_order"):
            assert word not in src, (p.name, word)


def test_dashboard_integrated_view(calendar, tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    eng = make(calendar, tmp_path)
    run_day(eng)
    eng.baseline.store.close()
    eng.ai.store.close()
    monkeypatch.setenv("NSE_PAPER__DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("NSE_INTEGRATED__AI_DB_PATH", str(tmp_path / "b.db"))
    monkeypatch.setenv("NSE_AI_EVAL__LEDGER_DB", str(tmp_path / "none.db"))
    at = AppTest.from_file(str(APP / "dashboard.py"), default_timeout=60).run()
    at.sidebar.radio[0].set_value("Integrated").run()
    assert not at.exception
    assert any("Integrated paper trading" in s.value for s in at.subheader)


def test_live_quotes_stamped_after_step_time_are_used(tmp_path, calendar):
    """Observed live 2026-10-21: quotes carry the exchange last-trade time, a few
    seconds after the scheduled step, and were discarded as 'after evaluation
    time', so every stock screened INSUFFICIENT_DATA. Live steps now fetch quotes
    first and evaluate at their receipt time; replays keep the step time."""
    from tests.paper_helpers import quote

    when = t("09:30")
    for mode, expect_spread in ((DataMode.LIVE, True), (DataMode.SIMULATED, False)):
        eng = make(calendar, tmp_path / mode.value, mode=mode,
                   screening={"require_spread": True})
        eng.snapshot.inner.quotes = {"SYNTH": quote("SYNTH", when + timedelta(seconds=3), 70.6)}
        eng.clock = lambda: when + timedelta(seconds=8)
        eng.open_session(DAY, when - timedelta(minutes=30))
        eng.step(when)
        row = eng.baseline.store.all("SELECT * FROM watchlist WHERE symbol = 'SYNTH'")[0]
        assert (row["spread_pct"] is not None) is expect_spread, (mode, dict(row))
        if not expect_spread:
            assert "after evaluation time" in row["reason"]
