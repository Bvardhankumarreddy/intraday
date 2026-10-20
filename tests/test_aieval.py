"""Phase 11: AI vs deterministic-baseline evaluation. ALL DATA IS SYNTHETIC
(tests/robustness_helpers.py): it tests the evaluation machinery, never the
strategy or the AI. No paid API, no live session, no broker.

Fixture: SYNTH over 25 weekdays; 5 warm-up sessions, 20 tradable; an ORB
signal at 09:40 on every other session from day 5, alternating target and
stop exits. Splits 0.3 / 0.2 / 0.5 give 6 / 4 / 10 sessions.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import app.backtesting.engine as engine_mod
from app.agent.llm import LLMResponse, LLMTimeout, MockProvider, ToolUse
from app.aieval import metrics as M
from app.aieval.ledger import DuplicateRun, Ledger
from app.aieval.protocol import (AGENT_CODE_FILES, ai_versions, load_protocol,
                                 pin_mismatches)
from app.aieval.quality import decision_quality
from app.aieval.replay import (AIDecision, ReplayRefused, engine_outcomes, evaluate_replay,
                               run_arm)
from app.aieval.report import regenerate, render_markdown
from app.backtesting.dataset import load_csv_dataset
from app.backtesting.engine import run_backtest
from app.config import PROJECT_ROOT, MarketConfig
from app.utils.market_calendar import MarketCalendar
from tests.robustness_helpers import e2e_days, e2e_settings, frozen_baseline, write_csv_dataset

IST = ZoneInfo("Asia/Kolkata")
APP = PROJECT_ROOT / "app"
REAL_PROTOCOL = PROJECT_ROOT / "config" / "ai_eval" / "protocol.yaml"
NOW = datetime(2026, 10, 20, 18, 0, tzinfo=IST)


def make_protocol(tmp_path: Path, baseline: Path, llm, settings, **minimums) -> Path:
    v = ai_versions(settings, llm)
    text = REAL_PROTOCOL.read_text().replace(
        "baseline_file: config/baselines/orb-baseline-v1.json", f"baseline_file: {baseline}")
    for k in ("prompt_version", "model", "tool_schema_sha256", "agent_code_sha256"):
        text = re.sub(rf"(?m)^  {k}: .*$", f"  {k}: {v[k]}", text, count=1)
    text = text.replace("bootstrap_resamples: 5000", "bootstrap_resamples: 300")
    for k, val in minimums.items():
        text = re.sub(rf"(?m)^  {k}: .*$", f"  {k}: {val}", text, count=1)
    path = tmp_path / "ai_protocol.yaml"
    path.write_text(text)
    return path


@pytest.fixture
def env(tmp_path):
    data = write_csv_dataset(tmp_path / "data")
    baseline = frozen_baseline(tmp_path)
    live = e2e_settings(tmp_path, ai_eval={"output_dir": str(tmp_path / "out"),
                                           "ledger_db": str(tmp_path / "ledger.db")})
    return tmp_path, data, baseline, live


def replay(env, split="holdout", llm=None, decider_factory=None, **minimums):
    tmp, data, baseline, live = env
    llm = llm or MockProvider()
    proto = make_protocol(tmp, baseline, llm, live, **minimums)
    ledger = Ledger(live.ai_eval.ledger_db)
    out = evaluate_replay(live, data, split, ledger, llm=llm, protocol_path=proto, now=NOW,
                          decider_factory=decider_factory)
    return out, ledger, proto


class ScriptedDecider:
    """Deterministic stand-in for the agent: decides from the signal alone."""

    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def decide(self, cand, now):
        self.calls.append((cand.setup.signal_id, now))
        take, why = self.fn(cand, now)
        return AIDecision(take, why, "OK", "consider" if take else "no_trade", "CANDIDATE",
                          f"REC-{cand.setup.signal_id}", "AR-x", True, "QUALIFIED_FOR_REVIEW")


# -- baseline preservation ----------------------------------------------------------------


def test_arm_a_is_exactly_the_unchanged_baseline(env):
    tmp, data, _, live = env
    s = e2e_settings(tmp)
    cal = MarketCalendar(s.market)
    ds = load_csv_dataset(data, s, cal)
    days = e2e_days()
    plain = run_backtest(ds, s, cal, days[5], days[-1])
    observed, log = run_arm(ds, s, cal, days[5], days[-1])
    assert engine_mod.run_scan.__module__ == "app.scan.pipeline"  # restored after the run
    assert [(t.signal_id, t.net_pnl) for t in plain.trades] == \
        [(t.signal_id, t.net_pnl) for t in observed.trades]
    assert plain.final_cash == observed.final_cash and len(log) == len(plain.trades) > 0


def test_frozen_baseline_and_decision_code_untouched():
    from app.robustness.baseline import CODE_FILES, load_frozen

    frozen = load_frozen(PROJECT_ROOT / "config" / "baselines" / "orb-baseline-v1.json")
    assert not frozen.code_drift  # Phase 11 changed no decision-path file
    assert not any("aieval" in c or "agent" in c for c in CODE_FILES)


def test_protocol_pins_match_the_current_ai_system():
    """Changing the prompt, tools or agent code requires a new protocol version."""
    protocol, _, _ = load_protocol(REAL_PROTOCOL)

    class Real:
        name, model = "anthropic", protocol.ai_system.model

    from app.config import Settings

    assert pin_mismatches(protocol, ai_versions(Settings(), Real())) == []
    assert set(AGENT_CODE_FILES) >= {"app/agent/workflow.py", "app/agent/tools.py",
                                     "app/agent/prompts.py"}


# -- end to end, ledger, duplicates ---------------------------------------------------------


def test_replay_end_to_end_with_mock_agent(env):
    out, ledger, _ = replay(env)
    a, b = out["arms"]["A"], out["arms"]["B"]
    assert out["status"] == "OK" and a["closed_trades"] == b["closed_trades"] == 5
    assert out["discordant_signals"] == 0  # the mock considers every eligible signal
    assert out["conclusion"]["conclusion"] == "INCONCLUSIVE"
    assert "insufficient evidence" in out["conclusion"]["reason"]
    decisions = ledger.decisions(out["run_id"])
    assert len(decisions) == 2 * a["signals_evaluated"]
    assert all(d["integrity_ok"] for d in decisions)
    for d in decisions:
        body = d["body"]
        for key in ("signal_id", "symbol", "exchange", "decision_time", "data_sources",
                    "versions", "screening", "proposal", "indicative_risk", "final_decision",
                    "reason", "evaluation_status"):
            assert key in body, key
        assert body["data_sources"][0]["as_of"] == body["decision_time"]
        if d["system"] == "AI_ASSISTED":
            assert body["versions"]["prompt_version"] == "research-v3"
            assert body["ai"]["recommendation_id"].startswith("REC-")
    q = out["quality"]
    assert q["rule_compliance"]["candidate_rule_violations"] == 0
    assert q["operational"]["research_runs"] == 5
    run = ledger.run(out["run_id"])
    assert run["status"] == "COMPLETED" and run["conclusion"] == "INCONCLUSIVE"


def test_ledger_is_immutable_and_duplicates_are_prevented(env):
    out, ledger, _ = replay(env)
    conn = ledger.store.conn
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        conn.execute("UPDATE aieval_decisions SET final_decision = 'TAKEN'")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        conn.execute("DELETE FROM aieval_decisions")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE aieval_outcomes SET body = '{}'")
    with pytest.raises(sqlite3.DatabaseError, match="final"):
        conn.execute("UPDATE aieval_runs SET conclusion = 'EVIDENCE_OF_IMPROVEMENT'")
    o = ledger.outcomes(out["run_id"])[0]
    assert ledger.add_outcome(o["decision_id"], o["kind"], NOW, {}) is False  # no duplicate
    d = ledger.decisions(out["run_id"])[0]
    n = len(ledger.decisions(out["run_id"]))
    ledger.add_decision(out["run_id"], d["system"], d["body"])  # same signal again: ignored
    assert len(ledger.decisions(out["run_id"])) == n
    with pytest.raises(ReplayRefused, match="single use"):  # holdout already used
        replay(env)


def test_duplicate_development_run_is_refused(env):
    replay(env, split="development")
    with pytest.raises(DuplicateRun):
        replay(env, split="development")


def test_holdout_refused_when_ai_system_differs_from_pins(env):
    tmp, data, baseline, live = env
    proto = make_protocol(tmp, baseline, MockProvider(), live)
    proto.write_text(proto.read_text().replace("prompt_version: research-v3",
                                               "prompt_version: research-v1"))
    with pytest.raises(ReplayRefused, match="pins"):
        evaluate_replay(live, data, "holdout", Ledger(live.ai_eval.ledger_db),
                        llm=MockProvider(), protocol_path=proto, now=NOW)
    out = evaluate_replay(live, data, "development", Ledger(live.ai_eval.ledger_db),
                          llm=MockProvider(), protocol_path=proto, now=NOW)
    assert any(lbl.startswith("AI_SYSTEM_DRIFT") for lbl in out["labels"])


def test_missing_data_is_pending_not_fabricated(env, tmp_path):
    _, _, baseline, live = env
    proto = make_protocol(tmp_path, baseline, MockProvider(), live)
    out = evaluate_replay(live, tmp_path / "nothing", "holdout", Ledger(live.ai_eval.ledger_db),
                          llm=MockProvider(), protocol_path=proto, now=NOW)
    assert out["status"] == "PENDING_DATA" and out["conclusion"]["conclusion"] == "PENDING_DATA"
    assert "arms" not in out
    assert "PENDING" in render_markdown(out)


# -- selection, costs, decomposition ---------------------------------------------------------


def test_ai_veto_changes_selection_only_with_equivalent_costs(env):
    stop_days = {d for i, d in enumerate(e2e_days()) if i >= 5 and i % 2 == 1
                 and (i // 2) % 2 == 1}
    veto_losers = ScriptedDecider(lambda c, now: (now.date() not in stop_days, "scripted"))
    out, ledger, _ = replay(env, decider_factory=lambda: veto_losers)
    a, b = out["arms"]["A"], out["arms"]["B"]
    assert b["closed_trades"] < a["closed_trades"]
    assert b["decisions"].get("VETOED_BY_AI", 0) == out["discordant_signals"] > 0
    assert all(now.minute % 5 == 0 for _, now in veto_losers.calls)
    dec = out["incremental"]["decomposition"]
    assert dec["trades_b_only_capacity"] == 0
    assert dec["total_difference_inr"] == pytest.approx(out["incremental"]["net_pnl_difference"])
    # Same fills and the same Phase 3 cost function in both arms. Quantities can differ:
    # vetoing losers leaves arm B more cash, so risk sizing buys more shares later.
    from app.costs.calculator import round_trip
    from app.costs.schedule import load_fee_schedule

    system = {d["decision_id"]: d["system"] for d in ledger.decisions(out["run_id"])}
    trades = {}
    for o in ledger.outcomes(out["run_id"]):
        if o["kind"] == "TRADE_CLOSED":
            trades.setdefault(o["body"]["signal_id"], {})[system[o["decision_id"]]] = o["body"]
    shared = [v for v in trades.values() if len(v) == 2]
    assert shared
    s = e2e_settings(env[0])
    for pair in shared:
        a_t, b_t = pair["BASELINE"], pair["AI_ASSISTED"]
        assert (a_t["entry_price"], a_t["exit_price"]) == (b_t["entry_price"], b_t["exit_price"])
        for t in (a_t, b_t):
            rates = load_fee_schedule(s).rates_on(date.fromisoformat(t["session"]), True)
            rt = round_trip(rates, Decimal(t["entry_price"]), Decimal(t["exit_price"]),
                            t["quantity"], s.execution, None)
            assert str(rt.total_costs) == t["costs"]
    assert dec["shared_trades_with_different_quantity"] == sum(
        p["BASELINE"]["quantity"] != p["AI_ASSISTED"]["quantity"] for p in shared)


def test_ai_cannot_bypass_risk_rejection(env):
    """At Rs 500 the frozen risk rules reject every synthetic signal; an AI that says
    'take everything' still produces no trade."""
    tmp, data, _, live = env
    baseline = frozen_baseline(tmp, name="rs500", capital={"initial_capital": 500},
                               risk={"risk_pct": 0.5, "include_costs_in_risk": True,
                                     "max_daily_loss": 15, "max_aggregate_risk": 5},
                               cost_review={"min_net_reward_risk": 1.5})
    yes = ScriptedDecider(lambda c, now: (True, "always"))
    proto = make_protocol(tmp, baseline, MockProvider(), live)
    out = evaluate_replay(live, data, "holdout", Ledger(live.ai_eval.ledger_db),
                          llm=MockProvider(), protocol_path=proto, now=NOW,
                          decider_factory=lambda: yes)
    assert out["arms"]["A"]["closed_trades"] == out["arms"]["B"]["closed_trades"] == 0
    assert out["arms"]["B"]["decisions"] == {"REJECTED_BY_RISK": 5}
    assert "veto-only AI cannot change any trade" in out["conclusion"]["structural_note"]
    assert out["capital"]["executable_baseline_signals"] == 0


# -- point in time ----------------------------------------------------------------------------


def test_decisions_see_no_future_data(env):
    tmp, data, _, live = env
    s = e2e_settings(tmp)
    cal = MarketCalendar(s.market)
    ds = load_csv_dataset(data, s, cal)
    provider = ds.provider(cal)
    seen = []

    def look(cand, now):
        frame = provider.get_candles(cand.symbol, "5m", now.date(), now.date()).completed(now)
        seen.append(frame["timestamp"].iloc[-1] + pd.Timedelta(minutes=5) <= pd.Timestamp(now))
        return True, "ok"

    d = e2e_days()
    run_arm(ds, s, cal, d[5], d[-1], decider=ScriptedDecider(look))
    assert seen and all(seen)


def test_agent_decision_unchanged_by_later_candles(env, tmp_path):
    """Rewriting every candle after the decision leaves the AI's view identical."""
    from app.agent.workflow import ResearchAgent
    from app.paper.store import PaperStore

    _, data, _, _ = env
    s = e2e_settings(tmp_path, agent={"reports_dir": str(tmp_path / "r")})
    cal = MarketCalendar(s.market)
    day = e2e_days()[5]
    at = datetime.combine(day, datetime.strptime("09:40", "%H:%M").time(), tzinfo=IST)
    other = tmp_path / "other"
    other.mkdir()
    for f in data.iterdir():
        frame = pd.read_csv(f)
        if f.name.endswith("_5m.csv"):
            later = pd.to_datetime(frame["timestamp"]) >= pd.Timestamp(at)
            same_day = pd.to_datetime(frame["timestamp"]).dt.date == day
            frame.loc[later & same_day, ["open", "high", "low", "close"]] = 99.0
        frame.to_csv(other / f.name, index=False)
    views = []
    for i, folder in enumerate((data, other)):
        prov = load_csv_dataset(folder, s, cal).provider(cal)
        agent = ResearchAgent(s, cal, prov, PaperStore(tmp_path / f"a{i}.db"), MockProvider(),
                              point_in_time=True)
        out = agent.run("intraday", at)
        rec = next(r for r in out["recommendations"] if r["symbol"] == "SYNTH")
        views.append({k: rec[k] for k in ("status", "entry_reference", "stop", "target",
                                          "quantity", "ai_assessment", "signal_id")})
    assert views[0] == views[1]


def test_point_in_time_tools_withhold_later_reports(tmp_path):
    from app.agent.tools import ToolContext, t_reports
    from app.paper.store import PaperStore

    s = e2e_settings(tmp_path, robustness={"output_dir": str(tmp_path / "rob")})
    folder = tmp_path / "rob" / "x" / "y"
    folder.mkdir(parents=True)
    (folder / "summary.json").write_text(json.dumps({"status": "OK", "assessment": {
        "verdict": "FUTURE"}}))
    store = PaperStore(tmp_path / "p.db")
    store.conn.execute("INSERT INTO reports (trading_date, generated_at, body) VALUES "
                       "('2025-03-01', 'x', '{\"totals\": {\"net\": 5}}')")

    class P:
        name, mode = "memory", "HISTORICAL"

        def list_instruments(self):
            return []

    cal = MarketCalendar(MarketConfig(), holidays={})
    when = datetime(2025, 2, 10, 9, 40, tzinfo=IST)
    live = t_reports(ToolContext(s, cal, P(), store, when), None)["result"]
    pit = t_reports(ToolContext(s, cal, P(), store, when, point_in_time=True), None)["result"]
    assert live["robustness"]["verdict"] == "FUTURE" and "latest_paper_report" in live
    assert "withheld" in pit["robustness"] and "latest_paper_report" not in pit


# -- failures, malformed output, costs -------------------------------------------------------


def test_api_failure_means_not_taken_and_is_counted(env):
    llm = MockProvider(script=[LLMTimeout("timed out")] * 50)
    out, _, _ = replay(env, llm=llm)
    assert out["arms"]["B"]["closed_trades"] == 0
    assert out["arms"]["B"]["decisions"]["VETOED_BY_AI"] == out["arms"]["A"]["signals_evaluated"]
    q = out["quality"]
    assert q["operational"]["model_errors"] == {"timeout": 5}
    assert q["ai_status_counts"] == {"UNAVAILABLE": 5}
    assert out["selection_diagnostic"]["groups"][0]["ai_view"] == "ai_unavailable_or_invalid"


def test_malformed_and_unsupported_ai_output_is_kept_and_counted(env):
    bad = LLMResponse("s", [], [ToolUse("b", "submit_research", {"overall": "yes"})],
                      "tool_use", 1, 1, 0.1, None, {})
    out, _, _ = replay(env, llm=MockProvider(script=[bad] * 60))
    q = out["quality"]
    assert q["operational"]["invalid_structured_outputs"] == 5
    assert q["operational"]["schema_validation_failures"] >= 5
    assert out["arms"]["B"]["closed_trades"] == 0  # never taken on invalid output


def test_model_cost_accounting(env):
    tmp, data, baseline, live = env
    live = live.model_copy(update={
        "llm": live.llm.model_copy(update={"input_cost_per_mtok": 1.0,
                                           "output_cost_per_mtok": 5.0}),
        "ai_eval": live.ai_eval.model_copy(update={"usd_inr_rate": 85.0})})

    class Priced(MockProvider):
        def _resp(self, text, uses, stop):
            r = super()._resp(text, uses, stop)
            r.input_tokens, r.output_tokens = 1000, 200
            return r

    out = evaluate_replay(live, data, "holdout", Ledger(live.ai_eval.ledger_db), llm=Priced(),
                          protocol_path=make_protocol(tmp, baseline, Priced(), live), now=NOW)
    inc = out["incremental"]
    calls = out["quality"]["operational"]["model_requests"]
    assert inc["model_cost_usd"] == pytest.approx(calls * (1000 * 1 + 200 * 5) / 1e6)
    assert inc["model_cost_inr"] == pytest.approx(inc["model_cost_usd"] * 85, abs=1e-4)
    assert inc["net_incremental_after_model_cost_inr"] == pytest.approx(
        inc["net_pnl_difference"] - inc["model_cost_inr"], abs=0.01)


# -- metrics and statistics ----------------------------------------------------------------


def _t(sid, day, net, gross=None, costs=Decimal("1"), exit_time="10:00", incomplete=False):
    return {"signal_id": sid, "symbol": "X", "session": day, "net": Decimal(str(net)),
            "gross": Decimal(str(gross if gross is not None else net + 1)), "costs": costs,
            "buy_turnover": Decimal("100"), "exit_time": f"{day}T{exit_time}",
            "exit_reason": "TARGET" if net > 0 else "STOP", "incomplete": incomplete,
            "quantity": 1, "ambiguous": False}


def test_arm_metrics_hand_calculated():
    d1, d2 = date(2025, 1, 1), date(2025, 1, 2)
    trades = [_t("a", d1, 10), _t("b", d1, -4, exit_time="11:00"), _t("c", d2, 6),
              _t("d", d2, -2, incomplete=True, exit_time="12:00")]
    m = M.arm_metrics(trades, [d1, d2], Decimal("500"))
    assert m["closed_trades"] == 4 and m["net_pnl"] == 10 and m["expectancy"] == 2.5
    assert m["win_rate_pct"] == 50 and m["avg_win"] == 8 and m["avg_loss"] == -3
    assert m["profit_factor"] == pytest.approx(16 / 6, abs=0.01)
    assert m["max_drawdown_inr"] == 4  # 510 -> 506
    assert m["incomplete_trades"] == 1 and m["total_costs"] == 4


def test_empty_and_undefined_metrics():
    m = M.arm_metrics([], [date(2025, 1, 1)], Decimal("500"))
    assert m["closed_trades"] == 0 and m["expectancy"] is None and m["win_rate_pct"] is None
    assert m["profit_factor"] is None and "no closed trades" in m["profit_factor_note"]
    only_wins = M.arm_metrics([_t("a", date(2025, 1, 1), 3)], [date(2025, 1, 1)], Decimal(500))
    assert only_wins["profit_factor"] is None and "no losing" in only_wins["profit_factor_note"]


def test_unfilled_orders_are_not_trades():
    class R:
        trades = []
        rejected = [type("Rej", (), {"signal_id": "S1", "stage": "execution",
                                     "status": "ENTRY_NOT_FILLED", "reasons": "gap"})()]

    kind, _ = engine_outcomes(R())["S1"]
    assert kind == "NOT_FILLED"
    assert M.arm_metrics([], [date(2025, 1, 1)], Decimal(500))["closed_trades"] == 0


def test_paired_bootstrap_and_decision_rule(tmp_path):
    protocol, _, _ = load_protocol(REAL_PROTOCOL)
    days = [date(2025, 1, d) for d in range(1, 31)]
    a = {d: (Decimal("1"), 1) for d in days}
    same = M.paired_bootstrap(a, a, days, 500, 0.95, 1, None)
    assert same["ci_low"] == same["ci_high"] == 0 and same["status"] == "OK"
    one = M.paired_bootstrap(a, a, days[:1], 500, 0.95, 1, None)
    assert one["status"] == "INSUFFICIENT" and one["ci_low"] is None
    stats = {"closed_trades": 40}
    enough = {"discordant_signals": 12}
    assert M.assess(protocol, 0, stats, stats, 0, one, None)["conclusion"] == "PENDING_DATA"
    low = M.assess(protocol, 30, {"closed_trades": 5}, stats, 12, same, None)
    assert low["conclusion"] == "INCONCLUSIVE" and "insufficient evidence" in low["reason"]
    eq = M.assess(protocol, 30, stats, stats, enough["discordant_signals"], same, 0.0)
    assert eq["conclusion"] == "NO_DEMONSTRATED_IMPROVEMENT" and "margin" in eq["reason"]
    better = {"ci_low": 2.0, "ci_high": 5.0}
    skill = {"beyond_random": True}
    unknown_cost = M.assess(protocol, 30, stats, stats, 12, better, None, None, skill)
    assert unknown_cost["conclusion"] == "INCONCLUSIVE" and "unknown" in unknown_cost["reason"]
    assert M.assess(protocol, 30, stats, stats, 12, better, 3.0, None, skill)["conclusion"] == \
        "EVIDENCE_OF_IMPROVEMENT"
    assert M.assess(protocol, 30, stats, stats, 12, better, 90.0, None, skill)["conclusion"] \
        == "INCONCLUSIVE"  # model cost (3 INR/session) exceeds the 2 INR/session lower bound
    blanket = M.assess(protocol, 30, stats, stats, 12, better, 3.0, None,
                       {"beyond_random": False, "reason": "vetoed every baseline trade"})
    assert blanket["conclusion"] == "INCONCLUSIVE" and "lower exposure" in blanket["reason"]
    worse = M.assess(protocol, 30, stats, stats, 12, {"ci_low": -5.0, "ci_high": -2.0}, 0.0)
    assert worse["conclusion"] == "NO_DEMONSTRATED_IMPROVEMENT"
    wide = M.assess(protocol, 30, stats, stats, 12, {"ci_low": -5.0, "ci_high": 5.0}, 0.0)
    assert wide["conclusion"] == "INCONCLUSIVE"


def test_random_veto_benchmark_separates_selection_from_exposure():
    d = date(2025, 1, 1)
    trades = [_t(f"s{i}", d, net) for i, net in enumerate([-5] * 20 + [5] * 20)]
    every = M.random_veto_benchmark(trades, [], 500, 1, 0.95)
    assert every["status"] == "VETOED_ALL" and not every["beyond_random"]
    none = M.random_veto_benchmark(trades, trades, 500, 1, 0.95)
    assert none["status"] == "NO_VETOES" and not none["beyond_random"]
    losers_only = M.random_veto_benchmark(trades, trades[20:], 2000, 1, 0.95)
    assert losers_only["beyond_random"] and losers_only["ai_selection_effect"] == 100
    assert losers_only["random_expected_effect"] == 0
    rng_like = M.random_veto_benchmark(trades, trades[10:30], 2000, 1, 0.95)  # half and half
    assert not rng_like["beyond_random"]


def test_paired_bootstrap_keeps_sessions_together():
    days = [date(2025, 1, d) for d in range(1, 21)]
    common = {d: (Decimal(str((-1) ** i * 50)), 1) for i, d in enumerate(days)}
    shifted = {d: (v + Decimal("1"), k) for d, (v, k) in common.items()}
    out = M.paired_bootstrap(common, shifted, days, 500, 0.95, 7, None)
    # The shared +/-50 market swing cancels: every resample shows exactly +1.
    assert out["ci_low"] == out["ci_high"] == 1.0


# -- decision quality --------------------------------------------------------------------


def _rec(**over):
    base = {"recommendation_id": "R1", "symbol": "X", "window": "intraday",
            "status": "CANDIDATE", "ai_status": "OK", "screen_status": "PASS",
            "signal_status": "QUALIFIED_PRE_COST", "risk_status": "QUALIFIED_FOR_REVIEW",
            "data_fresh": True, "quantity": 3, "signal_id": "S1",
            "created_at": "2025-01-01T09:40:00+05:30", "unsupported_claims": [],
            "data_sources": [{"as_of": "2025-01-01T09:40:00+05:30"}], "ai_market_context": [],
            "ai_assessment": {"observed_facts": [{"basis": "verified_fact", "text": "x",
                                                  "evidence_ids": ["T1"]}],
                              "supporting_evidence": [], "contradicting_evidence": [],
                              "missing_information": ["news"], "evidence_strength": "none",
                              "recommendation": "consider", "rationale": "ok"}}
    return {**base, **over}


def test_decision_quality_flags_violations_and_inconsistency():
    recs = [
        _rec(),
        _rec(recommendation_id="R2", data_fresh=False, signal_id="S2"),  # system violation
        _rec(recommendation_id="R3", status="REJECTED_BY_RISK", risk_status="REJECTED_COSTS",
             signal_id="S3", unsupported_claims=["number 42 in 'x' appears in no tool output",
                                                 "probability/guarantee language not allowed"]),
        _rec(recommendation_id="R4", status="NO_TRADE", created_at="2025-01-01T09:55:00+05:30",
             data_sources=[{"as_of": "2025-01-01T10:30:00+05:30"}],
             ai_assessment={**_rec()["ai_assessment"], "recommendation": "no_trade",
                            "rationale": "weak volume"}),
    ]
    q = decision_quality(recs, [{"latency_seconds": 1.0, "input_tokens": 5,
                                 "output_tokens": 2, "cost_usd": None,
                                 "error_kind": "timeout"}], [], [])
    rc, ev, co = q["rule_compliance"], q["evidence_quality"], q["consistency"]
    assert rc["candidate_rule_violations"] == 1 and rc["violating_ids"] == ["R2"]
    assert rc["ai_consider_on_risk_rejected"] == 1 and rc["ai_consider_on_stale_data"] == 1
    assert ev["unsupported_by_kind"] == {"number_not_in_any_tool_output": 1,
                                         "probability_language": 1}
    assert ev["source_refs_after_decision_time"] == 1
    assert co["changed_without_new_deterministic_information"] == 1  # R1 -> R4, same inputs
    assert co["invented_probabilities"] == 1 and co["abstentions_with_reason"] == 1
    assert q["operational"]["model_errors"] == {"timeout": 1}
    assert q["operational"]["duplicate_candidates_same_signal"] == 0


# -- reports ---------------------------------------------------------------------------------


def test_report_is_reproducible_from_records(env):
    out, ledger, _ = replay(env)
    run = ledger.run(out["run_id"])
    path = Path(run["results_path"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == run["results_sha256"]
    md = path.parent / "report.md"
    first = md.read_bytes()
    regenerate(path.parent)
    assert md.read_bytes() == first
    text = first.decode()
    for n, title in enumerate(["Executive summary", "Evaluation protocol and version",
                               "Systems compared", "Data coverage and quality",
                               "Results for both systems", "Incremental-value analysis",
                               "Rule-compliance and factual-accuracy results",
                               "Cost and capital-feasibility analysis",
                               "Statistical uncertainty", "Known biases and limitations",
                               "Outstanding defects", "Final evidence-based conclusion"], 1):
        assert f"## {n}. {title}" in text


# -- prospective -------------------------------------------------------------------------


def _paper_db(tmp_path, live):
    from app.agent.store import AgentStore
    from app.evaluation.recorder import EvaluationRecorder
    from app.paper.store import PaperStore

    store = PaperStore(tmp_path / "paper.db")
    EvaluationRecorder(store, live, MarketCalendar(live.market))
    AgentStore(store)
    ins = store._insert
    ins("evaluations", {"evaluation_id": "EVAL-1", "name": "t", "start_date": "2026-10-21",
                        "frozen_config_version": "x", "frozen_settings": "{}",
                        "created_at": "x", "active": 1})
    for d, status in (("2026-10-20", "COMPLETED"), ("2026-10-22", "COMPLETED"),
                      ("2026-10-23", "INCOMPLETE")):
        ins("eval_sessions", {"session_id": d, "evaluation_id": "EVAL-1", "trading_date": d,
                              "kind": "LIVE", "status": status, "primary_eval": 1})
    for sid, d, net, cand_at in (("S1", "2026-10-22", "5", "2026-10-22T09:41:00+05:30"),
                                 ("S2", "2026-10-22", "-3", "2026-10-22T09:50:00+05:30"),
                                 ("S0", "2026-10-20", "9", None)):
        ins("signals", {"signal_id": sid, "trading_date": d, "symbol": "X",
                        "signal_time": f"{d}T09:40:00+05:30", "entry_reference": "70",
                        "stop": "69", "target": "72", "valid_until": f"{d}T09:50:00+05:30",
                        "review_status": "QUALIFIED_FOR_REVIEW", "quantity": 2,
                        "planned_net_loss": "2", "reasons": "[]", "created_at": d})
        ins("orders", {"order_id": f"O{sid}", "signal_id": sid, "position_id": f"P{sid}",
                       "symbol": "X", "exchange": "NSE", "side": "BUY", "kind": "MARKET",
                       "quantity": 2, "state": "FILLED", "created_at": f"{d}T09:45:00+05:30",
                       "reference_price": "70", "execution_model_version": "v",
                       "updated_at": "x", "fill_quantity": 2})
        ins("positions", {"position_id": f"P{sid}", "signal_id": sid, "trading_date": d,
                          "symbol": "X", "state": "CLOSED", "quantity": 2, "entry_price": "70",
                          "entry_time": f"{d}T09:45:00+05:30", "stop": "69", "target": "72",
                          "planned_net_loss": "2", "buy_cash": "140", "buy_costs": "1",
                          "exit_price": "72", "exit_time": f"{d}T10:30:00+05:30",
                          "exit_reason": "TARGET", "gross_pnl": str(Decimal(net) + 1),
                          "total_costs": "1", "net_pnl": net})
        if cand_at:
            body = _rec(recommendation_id=f"REC-{sid}", signal_id=sid, created_at=cand_at)
            ins("agent_recommendations", {
                "recommendation_id": f"REC-{sid}", "run_id": "AR", "market_date": d,
                "window": "intraday", "symbol": "X", "status": "CANDIDATE",
                "created_at": cand_at, "schema_version": "v", "body": json.dumps(body),
                "sha256": "x"})
    return store


def test_prospective_uses_only_timely_recommendations(env):
    from app.aieval.prospective import evaluate_prospective

    tmp, _, baseline, live = env
    proto = make_protocol(tmp, baseline, MockProvider(), live, min_sessions=1,
                          min_closed_trades_per_arm=1, min_discordant_signals=1)
    store = _paper_db(tmp, live)
    out = evaluate_prospective(live, store, Ledger(live.ai_eval.ledger_db), llm=MockProvider(),
                               protocol_path=proto, now=NOW)
    assert out["coverage"]["sessions"] == 1  # 10-20 before effective date; 10-23 INCOMPLETE
    assert out["coverage"]["session_status_counts"] == {"COMPLETED": 1, "INCOMPLETE": 1}
    a, b = out["arms"]["A"], out["arms"]["B"]
    assert a["closed_trades"] == 2 and b["closed_trades"] == 1  # S2's CANDIDATE came late
    assert out["incremental"]["b_not_taken_reasons"]["AI_NOT_TIMELY"] == 1
    assert out["incremental"]["net_pnl_difference"] == 3.0
    assert a["incomplete_sessions"] == 1


def test_prospective_without_sessions_is_pending(env):
    from app.aieval.prospective import evaluate_prospective
    from app.paper.store import PaperStore

    tmp, _, baseline, live = env
    proto = make_protocol(tmp, baseline, MockProvider(), live)
    out = evaluate_prospective(live, PaperStore(tmp / "empty.db"), Ledger(live.ai_eval.ledger_db),
                               llm=MockProvider(), protocol_path=proto, now=NOW)
    assert out["status"] == "PENDING_DATA" and "no active Phase 6 evaluation" in \
        out["conclusion"]["reason"]


# -- safety -----------------------------------------------------------------------------------


def test_evaluation_changes_no_parameters(env):
    watched = [PROJECT_ROOT / "config" / "baselines" / "orb-baseline-v1.json", REAL_PROTOCOL,
               PROJECT_ROOT / "config" / "settings.example.yaml"]
    watched += [p for p in (PROJECT_ROOT / "config").glob("*.yaml")]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in watched if p.exists()}
    tmp, _, baseline, _ = env
    frozen_before = baseline.read_bytes()
    replay(env)
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}
    assert baseline.read_bytes() == frozen_before


def _imports(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_no_real_broker_order_path():
    for p in (APP / "aieval").glob("*.py"):
        imps = _imports(p)
        assert not {i for i in imps if "groww" in i or i in ("subprocess", "socket",
                                                              "requests")}, p.name
        src = p.read_text()
        for word in ("place_order", "modify_order", "cancel_order", "PaperEngine",
                     "execution_mode ="):
            assert word not in src, (p.name, word)


def test_dashboard_ai_evaluation_view(env, monkeypatch):
    from streamlit.testing.v1 import AppTest

    out, ledger, _ = replay(env)
    ledger.close()
    tmp = env[0]
    monkeypatch.setenv("NSE_AI_EVAL__LEDGER_DB", str(tmp / "ledger.db"))
    monkeypatch.setenv("NSE_PAPER_DB", str(tmp / "no-paper.db"))
    at = AppTest.from_file(str(APP / "dashboard.py"), default_timeout=60).run()
    at.sidebar.radio[0].set_value("AI evaluation").run()
    assert not at.exception
    assert any("AI evaluation" in s.value for s in at.subheader)
    assert any(out["conclusion"]["conclusion"] in m.value for m in at.markdown)
