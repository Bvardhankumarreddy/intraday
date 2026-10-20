"""Phase 13: learning pipeline. ALL DATA IS SYNTHETIC (integrated paper
sessions on tests/paper_helpers fixtures, deterministic mock LLM). It tests
the machinery only; nothing here is evidence about the strategy or the AI.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import MarketConfig, PROJECT_ROOT
from app.learning import proposals as P
from app.learning.analysis import analyse, proposals_from_findings, wilson
from app.learning.dataset import (LeakageError, LearningStore, compute_outcomes,
                                  decision_features, ingest)
from app.learning.report import build, markdown, write
from app.learning.review import RULES_VERSION, annotate, classify, review_all
from app.paper.scheduler import replay_session
from app.paper.store import PaperStore
from app.utils.market_calendar import MarketCalendar
from tests.backtest_helpers import scenario
from tests.paper_helpers import NEUTRAL, STOP, TARGET

IST = ZoneInfo("Asia/Kolkata")
APP = PROJECT_ROOT / "app"
NOW = datetime(2025, 3, 15, 10, 0, tzinfo=IST)


@pytest.fixture(scope="module")
def sessions(tmp_path_factory):
    """Ten integrated paper sessions (both arms) with the mock agent."""
    from tests.test_integrated import make

    tmp = tmp_path_factory.mktemp("learn")
    cal = MarketCalendar(MarketConfig(), holidays={})
    days = cal.trading_days(date(2025, 3, 3), date(2025, 3, 14))
    sc = {d: scenario([NEUTRAL, TARGET] if i % 2 == 0 else [NEUTRAL, STOP])
          for i, d in enumerate(days)}
    eng = make(cal, tmp, days=sc)
    for d in days:
        replay_session(eng, d)
    eng.baseline.store.close()
    eng.ai.store.close()
    return tmp, cal, eng.settings, eng.snapshot.inner


def pipeline(sessions, tmp_path, **learning):
    tmp, cal, s, provider = sessions
    s = s.model_copy(update={"learning": s.learning.model_copy(update={
        "db_path": tmp_path / "learn.db", "reports_dir": tmp_path / "reports", **learning}),
        "ai_eval": s.ai_eval.model_copy(update={"ledger_db": tmp_path / "ledger.db"})})
    ls = LearningStore(s.learning.db_path)
    info = ingest(ls, tmp / "b.db", "synthetic", s, NOW)
    stores = {r["source_id"]: PaperStore(r["path"]) for r in ls.all("SELECT * FROM learn_sources")}
    out = compute_outcomes(ls, provider, s, cal, NOW, stores)
    rev = review_all(ls, s, NOW)
    return ls, s, cal, provider, info, out, rev


# -- dataset ---------------------------------------------------------------------------


def test_ingest_outcomes_review_end_to_end(sessions, tmp_path):
    ls, s, *_, info, out, rev = pipeline(sessions, tmp_path)
    assert info["added"] > 0 and info["refused_leakage"] == []
    assert out["added"] > 0 and out["pending"] == 0
    assert rev["reviewed"] == info["added"]
    rec = ls.records()[0]
    b = rec["body"]
    assert rec["integrity_ok"]
    for key in ("decision_ts_ist", "decision_ts_utc", "symbol", "exchange", "market_session",
                "strategy_version", "data_snapshot_ts", "data_fresh", "features", "model",
                "prompt_version", "tool_call_ids", "ai_output", "recommendation"):
        assert key in b, key
    assert b["recommendation"] in ("CONSIDER", "WAIT", "NO_TRADE", "UNAVAILABLE")
    assert datetime.fromisoformat(b["decision_ts_utc"]) == datetime.fromisoformat(
        b["decision_ts_ist"])
    kinds = {k for o in ls.outcomes().values() for k in o}
    assert {"H15m", "H30m", "H60m", "EOD", "TRADE_PATH", "PAPER_EXECUTION"} <= kinds


def test_records_immutable_and_corrections_append_only(sessions, tmp_path):
    ls, *_ = pipeline(sessions, tmp_path)
    conn = ls.store.conn
    for sql in ("UPDATE learn_records SET symbol = 'X'", "DELETE FROM learn_records",
                "UPDATE learn_outcomes SET body = '{}'", "DELETE FROM learn_reviews"):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    r = ls.records()[0]
    ls.correct(r["record_id"], "symbol", "OTHER", "typo in source", "analyst", NOW)
    assert ls.records()[0]["body"]["symbol"] == r["body"]["symbol"]  # original unchanged
    c = ls.corrections(r["record_id"])[0]
    assert json.loads(c["new_value"]) == "OTHER" and c["actor"] == "analyst"
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE learn_corrections SET reason = 'x'")
    with pytest.raises(ValueError):
        ls.correct(r["record_id"], "symbol", "Y", "", "", NOW)


def test_duplicates_and_restart_are_idempotent(sessions, tmp_path):
    ls, s, cal, provider, info, *_ = pipeline(sessions, tmp_path)
    n_out = len(ls.all("SELECT * FROM learn_outcomes"))
    ls.close()
    again = LearningStore(s.learning.db_path)  # a restart
    tmp = sessions[0]
    assert ingest(again, tmp / "b.db", "synthetic", s, NOW)["added"] == 0
    assert compute_outcomes(again, provider, s, cal, NOW)["added"] == 0
    assert review_all(again, s, NOW)["reviewed"] == 0
    assert len(again.all("SELECT * FROM learn_outcomes")) == n_out


def test_delayed_outcomes_are_pending_not_guessed(sessions, tmp_path):
    tmp, cal, s, provider = sessions
    s2 = s.model_copy(update={"learning": s.learning.model_copy(update={
        "db_path": tmp_path / "l.db"})})
    ls = LearningStore(s2.learning.db_path)
    ingest(ls, tmp / "b.db", "synthetic", s2, NOW)
    first = ls.records()[0]
    early = datetime.fromisoformat(first["body"]["decision_ts_ist"]) + timedelta(minutes=20)
    out = compute_outcomes(ls, provider, s2, cal, early)
    got = ls.outcomes().get(first["record_id"], {})
    assert "H15m" in got and "H30m" not in got and "EOD" not in got and out["pending"] > 0
    assert got["H15m"]["_available_at"] <= early.isoformat()
    assert review_all(ls, s2, early)["reviewed"] < len(ls.records())  # waits for EOD


def test_missing_market_data_is_recorded_not_invented(sessions, tmp_path):
    from app.data.models import DataUnavailableError

    tmp, cal, s, _ = sessions

    class Down:
        def get_candles(self, *a, **k):
            raise DataUnavailableError("feed down")

        def list_instruments(self):
            return []

    s2 = s.model_copy(update={"learning": s.learning.model_copy(update={
        "db_path": tmp_path / "l.db"})})
    ls = LearningStore(s2.learning.db_path)
    ingest(ls, tmp / "b.db", "synthetic", s2, NOW)
    compute_outcomes(ls, Down(), s2, cal, NOW)
    bodies = [o for rec in ls.outcomes().values() for k, o in rec.items() if k.startswith("H")]
    assert bodies and all(o["status"] == "NO_DATA" for o in bodies)


def test_future_information_cannot_enter_decision_features():
    rec = {"recommendation_id": "R", "created_at": "2025-03-12T09:40:01+05:30",
           "data_fresh": True, "data_sources": []}
    ok = {"as_of_candle": "2025-03-12T09:35:00+05:30", "close": 71.0}
    feats = decision_features(rec, ok, 5)
    assert feats["close"] == 71.0
    for leak in ({"as_of_candle": "2025-03-12T09:40:00+05:30"},):
        with pytest.raises(LeakageError):
            decision_features(rec, leak, 5)
    with pytest.raises(LeakageError):
        decision_features({**rec, "data_sources": [{"tool_call_id": "T1",
                                                     "as_of": "2025-03-12T10:00:00+05:30"}]},
                          ok, 5)
    outcome_words = {"return", "mfe", "mae", "exit", "pnl", "outcome"}
    assert not {k for k in feats if any(w in k for w in outcome_words)}


# -- review -----------------------------------------------------------------------------


def _body(**over):
    base = {"features": {"has_setup": True, "risk_status": "QUALIFIED_FOR_REVIEW",
                         "entry_reference": 71.0, "stop": 70.4, "quantity": 28,
                         "estimated_costs_at_target": "6.0"},
            "data_fresh": True, "unsupported_claims": [], "recommendation": "CONSIDER",
            "rule_compliant": True, "ai_status": "OK", "system_status": "CANDIDATE"}
    return {**base, **over}


def test_process_is_judged_separately_from_outcome(sessions):
    s = sessions[2]
    loss = {"TRADE_PATH": {"status": "SIMULATED", "net_pnl": "-10"},
            "H15m": {"status": "OK", "return_pct": 0.5, "mae_pct": -0.1}}
    sound_loss = classify(_body(), loss, s)
    assert sound_loss["process_grade"] == "SOUND" and sound_loss["outcome_grade"] == "LOSS"
    assert sound_loss["outcome_category"] == "NORMAL_UNCERTAINTY"
    win = {"TRADE_PATH": {"status": "SIMULATED", "net_pnl": "12"},
           "H15m": {"status": "OK", "return_pct": 0.5, "mae_pct": -0.1}}
    lucky = classify(_body(unsupported_claims=["number 9 ..."], data_fresh=False), win, s)
    assert lucky["process_grade"] == "FLAWED" and lucky["outcome_grade"] == "WIN"
    assert {"UNSUPPORTED_CLAIM", "DATA_QUALITY", "STRATEGY_RULE_VIOLATION"} <= \
        set(lucky["process_categories"])
    weak = classify(_body(), {"TRADE_PATH": {"status": "SIMULATED", "net_pnl": "-5"},
                              "H15m": {"status": "OK", "return_pct": -1.0, "mae_pct": -1.0}}, s)
    assert weak["outcome_category"] == "WEAK_BREAKOUT"
    waited = classify(_body(recommendation="WAIT", system_status="WATCH"), win, s)
    assert waited["outcome_grade"] == "MISSED_GAIN"
    assert classify(_body(), {}, s)["outcome_category"] == "INSUFFICIENT_EVIDENCE"
    exec_dev = classify(_body(), {"H15m": {"status": "OK", "return_pct": 1, "mae_pct": 0},
                                  "PAPER_EXECUTION": {"status": "ORDERED",
                                                      "order": {"state": "FILLED"},
                                                      "fill_deviation_pct": 0.9,
                                                      "signal_quantity": 28}}, s)
    assert exec_dev["outcome_category"] == "EXECUTION_DEVIATION"
    sizing = classify(_body(), {"PAPER_EXECUTION": {"status": "ORDERED", "signal_quantity": 20,
                                                    "order": {"state": "FILLED"}}}, s)
    assert "RISK_SIZING_ERROR" in sizing["process_categories"]


def test_human_annotation_is_stored_beside_rules_review(sessions, tmp_path):
    ls, *_ = pipeline(sessions, tmp_path)
    rid = ls.records()[0]["record_id"]
    annotate(ls, rid, "INCORRECT_INTERPRETATION", "misread VWAP as support", "analyst", NOW)
    rows = ls.all("SELECT rules_version FROM learn_reviews WHERE record_id = ?", (rid,))
    assert {r["rules_version"].split(":")[0] for r in rows} == {RULES_VERSION, "human"}
    with pytest.raises(ValueError):
        annotate(ls, rid, "MADE_UP", "x", "y", NOW)


# -- analysis -------------------------------------------------------------------------


def test_analysis_counts_each_signal_once_and_flags_small_samples(sessions, tmp_path):
    ls, s, *_ = pipeline(sessions, tmp_path)
    a = analyse(ls, s)
    assert a["records"] > a["signals"]  # signals are reviewed in several runs
    perf = a["performance_hypothetical"]
    assert perf["baseline_all_eligible"]["n"] <= a["signals"]
    assert perf["baseline_all_eligible"]["interpretable"] is False
    assert "insufficient sample" in perf["baseline_all_eligible"]["note"]
    assert all(not g["interpretable"] for g in a["groups"])
    assert "hypothetical" in perf["note"]


def test_wilson_interval():
    assert wilson(0, 0, 0.95) == (None, None)
    lo, hi = wilson(5, 10, 0.95)
    assert lo == pytest.approx(0.2366, abs=1e-3) and hi == pytest.approx(0.7634, abs=1e-3)


def test_recurring_findings_create_deduplicated_proposals(sessions, tmp_path):
    ls, s, *_ = pipeline(sessions, tmp_path, min_recurring_count=1, recurring_rate=0.01)
    a = analyse(ls, s)
    assert a["findings"] and all(f["evidence_level"] == "HYPOTHESIS" for f in a["findings"])
    first = proposals_from_findings(ls, a, NOW)
    assert first and proposals_from_findings(ls, analyse(ls, s), NOW) == []  # no duplicates
    p = P.get(ls, first[0])
    assert p["status"] == "PROPOSED" and p["supporting_ids"]
    for k in ("hypothesis", "change", "component", "baseline_version", "candidate_version",
              "required_tests", "acceptance_criteria", "limitations", "expected_benefit",
              "possible_downside"):
        assert p[k], k


# -- proposal workflow and safeguards ---------------------------------------------------


def _proposal(ls):
    return P.create(ls, {"finding_key": "test:x", "issue": "i", "hypothesis": "h",
                         "change": "c", "component": "prompt", "supporting_ids": ["LR-1"],
                         "sample_size": 3, "evidence_level": "HYPOTHESIS",
                         "baseline_version": "prompt research-v3",
                         "candidate_version": "prompt research-v4",
                         "expected_benefit": "b", "possible_downside": "d",
                         "required_tests": ["t"], "limitations": ["l"],
                         "acceptance_criteria": {"required_conclusions":
                                                 ["EVIDENCE_OF_IMPROVEMENT"],
                                                 "max_rule_violations": 0}}, NOW)


def _ledger_run(s, tmp_path, conclusion, start="2025-03-03", end="2025-03-14"):
    from app.aieval.ledger import Ledger

    led = Ledger(s.ai_eval.ledger_db)
    rid = f"AE-{conclusion[:6]}{len(led.runs())}"
    res = tmp_path / f"{rid}.json"
    res.write_text(json.dumps({"conclusion": {"conclusion": conclusion, "reason": "r"},
                               "quality": {"rule_compliance": {
                                   "candidate_rule_violations": 0}}}))
    led.start_run({"run_id": rid, "run_key": rid, "mode": "PROSPECTIVE_INTEGRATED",
                   "split": None, "period_start": start, "period_end": end, "dataset_id": "d",
                   "protocol_version": "v", "protocol_sha256": "x", "baseline_name": "b",
                   "baseline_fingerprint": "f", "ai_versions": "{}", "labels": "[]",
                   "started_at": NOW.isoformat()})
    led.finish_run(rid, "COMPLETED", NOW, None, conclusion, str(res), "sha")
    led.close()
    return rid


def test_unapproved_or_failed_proposal_cannot_become_active(sessions, tmp_path):
    ls, s, *_ = pipeline(sessions, tmp_path)
    pid = _proposal(ls)
    with pytest.raises(P.ProposalError):  # must be tested first
        P.approve(ls, pid, "owner", "ok", f"APPROVE {pid}", NOW)
    P.start_test(ls, s, pid, date(2025, 3, 1), date(2025, 3, 31), "replay", "owner", NOW)
    assert P.record_test(ls, s, pid, _ledger_run(s, tmp_path, "INCONCLUSIVE"), NOW) == \
        "INCONCLUSIVE"
    assert P.record_test(ls, s, pid, _ledger_run(s, tmp_path, "NO_DEMONSTRATED_IMPROVEMENT"),
                         NOW) == "FAIL"
    with pytest.raises(P.ProposalError, match="no test that passed"):
        P.approve(ls, pid, "owner", "ok", f"APPROVE {pid}", NOW)
    with pytest.raises(P.ProposalError):  # cannot deploy what is not approved
        P.deploy(ls, pid, "owner", "go", f"DEPLOY {pid}", "research-v4", NOW)
    P.reject(ls, pid, "owner", "failed its test", NOW)
    assert P.get(ls, pid)["status"] == "REJECTED"
    with pytest.raises(P.ProposalError):
        P.start_test(ls, s, pid, date(2025, 4, 1), date(2025, 4, 30), "x", "owner", NOW)


def test_approval_requires_person_confirmation_and_passing_evidence(sessions, tmp_path):
    ls, s, *_ = pipeline(sessions, tmp_path)
    pid = _proposal(ls)
    P.start_test(ls, s, pid, date(2025, 3, 1), date(2025, 3, 31), "replay", "owner", NOW)
    with pytest.raises(P.ProposalError, match="not a completed"):
        P.record_test(ls, s, pid, "AE-doesnotexist", NOW)
    with pytest.raises(P.ProposalError, match="outside"):
        P.record_test(ls, s, pid, _ledger_run(s, tmp_path, "EVIDENCE_OF_IMPROVEMENT",
                                              "2025-05-01", "2025-05-30"), NOW)
    assert P.record_test(ls, s, pid, _ledger_run(s, tmp_path, "EVIDENCE_OF_IMPROVEMENT"),
                         NOW) == "PASS"
    with pytest.raises(P.ProposalError, match="confirmation"):
        P.approve(ls, pid, "owner", "ok", "yes", NOW)
    with pytest.raises(P.ProposalError, match="person"):
        P.approve(ls, pid, "learning-engine", "ok", f"APPROVE {pid}", NOW)
    P.approve(ls, pid, "owner", "tested on fresh data", f"APPROVE {pid}", NOW)
    P.deploy(ls, pid, "owner", "released", f"DEPLOY {pid}", "research-v4", NOW)
    P.rollback(ls, pid, "owner", "regressed", f"ROLLBACK {pid}", NOW)
    p = P.get(ls, pid)
    assert [e["to_status"] for e in p["events"]] == ["PROPOSED", "UNDER_TEST", "APPROVED",
                                                     "DEPLOYED", "ROLLED_BACK"]
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        ls.store.conn.execute("UPDATE proposals SET hypothesis = 'rewritten'")
    with pytest.raises(sqlite3.DatabaseError):
        ls.store.conn.execute("DELETE FROM proposal_events")


def test_active_configuration_never_changes(sessions, tmp_path):
    watched = sorted(p for p in (PROJECT_ROOT / "config").rglob("*.*") if p.is_file()) + \
        [APP / "agent" / "prompts.py"]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in watched}
    ls, s, *_ = pipeline(sessions, tmp_path, min_recurring_count=1, recurring_rate=0.01)
    for pid in proposals_from_findings(ls, analyse(ls, s), NOW):
        P.reject(ls, pid, "owner", "test", NOW)
    pid = _proposal(ls)
    P.start_test(ls, s, pid, date(2025, 3, 1), date(2025, 3, 31), "replay", "owner", NOW)
    P.record_test(ls, s, pid, _ledger_run(s, tmp_path, "EVIDENCE_OF_IMPROVEMENT"), NOW)
    P.approve(ls, pid, "owner", "ok", f"APPROVE {pid}", NOW)
    P.deploy(ls, pid, "owner", "ok", f"DEPLOY {pid}", "research-v4", NOW)
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in watched}
    from app.agent.prompts import PROMPT_VERSION

    assert PROMPT_VERSION == "research-v3"  # deploy records; it never applies


def test_selection_bias_guards(sessions, tmp_path):
    ls, s, *_ = pipeline(sessions, tmp_path)
    s2 = s.model_copy(update={"learning": s.learning.model_copy(update={
        "max_tests_per_validation_period": 2, "final_evaluation_start": date(2025, 6, 1)})})
    ids = [P.create(ls, {
        "finding_key": f"k{i}", "issue": "i", "hypothesis": "h", "change": "c",
        "component": "prompt", "supporting_ids": [], "sample_size": 1,
        "evidence_level": "HYPOTHESIS", "baseline_version": "b", "candidate_version": "c",
        "expected_benefit": "b", "possible_downside": "d", "required_tests": [],
        "limitations": [], "acceptance_criteria": {"required_conclusions": ["X"],
                                                   "max_rule_violations": 0}}, NOW)
        for i in range(3)]
    for pid in ids[:2]:
        P.start_test(ls, s2, pid, date(2025, 3, 1), date(2025, 3, 31), "val", "owner", NOW)
    with pytest.raises(P.ProposalError, match="fresh data"):
        P.start_test(ls, s2, ids[2], date(2025, 3, 15), date(2025, 4, 15), "val", "owner",
                     NOW)
    P.start_test(ls, s2, ids[2], date(2025, 4, 1), date(2025, 4, 30), "fresh", "owner", NOW)
    tests = ls.all("SELECT * FROM proposal_tests WHERE kind = 'PLAN'")
    assert json.loads(tests[1]["details"])["multiplicity"] == 2


def test_final_evaluation_set_is_single_use(sessions, tmp_path):
    ls, s, *_ = pipeline(sessions, tmp_path)
    s2 = s.model_copy(update={"learning": s.learning.model_copy(update={
        "final_evaluation_start": date(2025, 3, 10)})})
    pid = _proposal(ls)
    P.start_test(ls, s2, pid, date(2025, 3, 1), date(2025, 3, 31), "final", "owner", NOW)
    with ls.store.transaction():  # simulate a second planned test of the same proposal
        ls.store.conn.execute("UPDATE proposals SET status = 'PROPOSED' WHERE proposal_id = ?",
                              (pid,))
    with pytest.raises(P.ProposalError, match="single use"):
        P.start_test(ls, s2, pid, date(2025, 3, 11), date(2025, 3, 20), "final", "owner", NOW)


# -- reports and boundaries -----------------------------------------------------------


def test_report_is_reproducible(sessions, tmp_path):
    ls, s, *_ = pipeline(sessions, tmp_path, min_recurring_count=1, recurring_rate=0.01)
    proposals_from_findings(ls, analyse(ls, s), NOW)
    path = write(ls, s)
    first = path.read_bytes()
    write(ls, s)
    assert path.read_bytes() == first
    text = first.decode()
    for section in ("Coverage", "AI-assisted vs deterministic baseline", "By group",
                    "AI factual accuracy", "Error categories", "Change between periods",
                    "Findings", "Improvement proposals", "Missing data, uncertainty"):
        assert f"## {section}" in text, section
    assert markdown(json.loads(json.dumps(build(ls, s), default=str, sort_keys=True))) == text


def test_learning_has_no_llm_broker_or_config_write_path():
    for p in (APP / "learning").glob("*.py"):
        imps = set()
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Import):
                imps |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imps.add(node.module)
        assert not {i for i in imps if "groww" in i or i in (
            "requests", "socket", "subprocess", "app.agent.llm", "app.paper.engine")}, p.name
        src = p.read_text()
        for word in ("place_order", "modify_order", "cancel_order", "settings.yaml",
                     "set_meta(", "yaml.dump", "os.environ["):
            assert word not in src, (p.name, word)


def test_dashboard_learning_view(sessions, tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    ls, s, *_ = pipeline(sessions, tmp_path)
    ls.close()
    monkeypatch.setenv("NSE_LEARNING__DB_PATH", str(tmp_path / "learn.db"))
    monkeypatch.setenv("NSE_PAPER_DB", str(tmp_path / "none.db"))
    at = AppTest.from_file(str(APP / "dashboard.py"), default_timeout=60).run()
    at.sidebar.radio[0].set_value("Learning").run()
    assert not at.exception
    assert any("Learning review" in x.value for x in at.subheader)
