"""Phase 15: monitoring, drift detection, alerts and safe degraded modes.
ALL DATA IS SYNTHETIC (fixtures and directly inserted records). It tests the
monitoring machinery only; no live feed, paid API or broker is used.
"""

from __future__ import annotations

import ast
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from app.agent.llm import LLMTimeout, MockProvider
from app.config import PROJECT_ROOT
from app.monitoring import checks, drift, state
from app.monitoring.alerts import AlertCenter, MonitorStore, OutboxNotifier, clean
from app.monitoring.monitor import run_monitor
from app.paper.store import PaperStore
from tests.backtest_helpers import bt_settings, make_dataset, scenario
from tests.paper_helpers import NEUTRAL, TARGET

IST = ZoneInfo("Asia/Kolkata")
APP = PROJECT_ROOT / "app"
DAY = date(2025, 3, 12)


def t(hhmm: str, day: date = DAY) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, 1, tzinfo=IST)


def settings(tmp: Path, **over):
    base = {
        "paper": {"db_path": str(tmp / "a.db"), "lock_file": str(tmp / "p.lock"),
                  "grace_seconds": 1},
        "integrated": {"ai_db_path": str(tmp / "b.db"), "reports_dir": str(tmp / "ir"),
                       "agent_every_n_candles": 1},
        "monitoring": {"db_path": str(tmp / "mon.db"), "alert_outbox": str(tmp / "out.jsonl"),
                       "ai_min_runs_for_rates": 3, "min_trades_for_drift": 30},
        "experiments": {"registry_db": str(tmp / "reg.db"), "shadow_db_path": str(tmp / "s.db"),
                        "canary_db_path": str(tmp / "c.db")},
        "ai_eval": {"ledger_db": str(tmp / "ledger.db")},
        "learning": {"db_path": str(tmp / "learn.db")},
        "ops": {"backups_dir": str(tmp / "backups")},
        "evaluation": {"output_dir": str(tmp / "eval")},
        "agent": {"reports_dir": str(tmp / "agent")},
        "data": {"universe_file": str(tmp / "universe.csv")},
    }
    for k, v in over.items():
        base[k] = {**base.get(k, {}), **v}
    return bt_settings(**base)


class Capture:
    name, status = "capture", "CAPTURED"

    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)


def finding(key="data.stale", severity="WARNING", effect=None, evidence=None, **kw):
    return checks.F(key, severity, "market-data", "rule text", "threshold",
                    evidence if evidence is not None else {"v": 1}, ["step one"], effect, **kw)


# -- market data -----------------------------------------------------------------------


def _a_store(tmp, s, cal):
    from app.evaluation.recorder import EvaluationRecorder

    store = PaperStore(s.paper.db_path)
    EvaluationRecorder(store, s, cal)
    return store


def _dq(store, day, **vals):
    row = {"trading_date": day.isoformat(), "ts": t("10:00", day).isoformat(), "symbol": "X",
           "expected": 10, "received": 10, "missing": 0, "duplicates": 0, "out_of_order": 0,
           "invalid_ohlc": 0, "invalid_volume": 0, "gaps": 0, "stale": 0, **vals}
    cols = ", ".join(row)
    store.conn.execute(f"INSERT INTO dq_batches ({cols}) VALUES "
                       f"({', '.join('?' for _ in row)})", tuple(row.values()))


def test_stale_missing_malformed_duplicate_out_of_order(tmp_path, calendar):
    s = settings(tmp_path)
    store = _a_store(tmp_path, s, calendar)
    with store.transaction():
        store.set_meta("heartbeat", {"ts": t("10:00").isoformat(), "signals_blocked": True,
                                     "symbols_ok": 0, "symbols_total": 3,
                                     "provider": {"circuit": "open",
                                                  "consecutive_failures": 5}})
    _dq(store, DAY, missing=4, duplicates=1, out_of_order=1, invalid_ohlc=2)
    f, m = checks.check_data(s, calendar, store, t("10:01"))
    keys = {x["key"]: x for x in f}
    assert keys["data.stale"]["severity"] == "CRITICAL"
    assert keys["data.stale"]["effect"]["state"] == "DEGRADED_DATA"
    assert keys["data.invalid_records"]["evidence"] == {"dup": 1, "ooo": 1, "ohlc": 2, "vol": 0}
    assert "data.missing_candles" in keys and "data.provider_failing" in keys
    assert m["missing_candle_pct"] == 40.0
    late, _ = checks.check_data(s, calendar, store, t("10:30"))
    assert any(x["key"] == "data.no_step" for x in late)  # no step for 30 minutes


def test_official_closure_is_not_a_data_gap(tmp_path, calendar):
    s = settings(tmp_path)
    store = _a_store(tmp_path, s, calendar)
    holiday = date(2025, 3, 14)  # the test calendar's holiday
    f, m = checks.check_data(s, calendar, store, t("10:00", holiday))
    assert f == [] and "closed" in m["market"]
    sunday = date(2025, 3, 16)
    _dq(store, sunday)  # data recorded on a closed day = calendar inconsistency
    cf, _ = checks.check_calendar(s, calendar, store, t("10:00"))
    assert cf[0]["key"] == "calendar.inconsistent" and cf[0]["severity"] == "CRITICAL"


# -- AI agent -------------------------------------------------------------------------


def _b_store(s):
    from app.agent.store import AgentStore

    store = PaperStore(s.integrated.ai_db_path)
    AgentStore(store)
    return store


def _calls(store, n, error=None, when=t("10:00"), cost=None, latency=1.0, attempts=1):
    for i in range(n):
        store._insert("agent_model_calls", {
            "run_id": f"R{when.isoformat()}{i}{error}", "call_id": "M1", "provider": "anthropic",
            "model": "m", "prompt_version": "p", "started_at": (when + timedelta(seconds=i))
            .isoformat(), "latency_seconds": latency, "input_tokens": 100, "output_tokens": 10,
            "cost_usd": cost, "attempts": attempts, "error_kind": error,
            "error": "x" if error else None})


def test_llm_timeouts_and_rate_limits_then_recovery(tmp_path):
    s = settings(tmp_path)
    b = _b_store(s)
    _calls(b, 4, error="timeout")
    _calls(b, 2, error="rate_limited", attempts=3)
    _calls(b, 2)
    f, m = checks.check_ai(s, b, t("10:05"), None)
    crit = next(x for x in f if x["key"] == "ai.failure_rate")
    assert crit["severity"] == "CRITICAL" and crit["effect"]["state"] == "DEGRADED_AI"
    assert m["error_kinds"] == {"timeout": 4, "rate_limited": 2} and m["retried_requests"] == 2
    cap = Capture()
    ms = MonitorStore(s.monitoring.db_path)
    ac = AlertCenter(ms, s, [cap])
    ac.sync(f, {"ai"}, t("10:05"))
    later = t("11:00")
    _calls(b, 40, when=later)  # the provider recovered
    f2, _ = checks.check_ai(s, b, later, None)
    out = ac.sync(f2, {"ai"}, later)
    assert "ai.failure_rate" in out["resolved"]
    assert [p["event"] for p in cap.sent] == ["RAISED", "RECOVERED"]


def test_api_cost_threshold(tmp_path):
    s = settings(tmp_path, monitoring={"ai_daily_cost_usd_warning": 0.5})
    b = _b_store(s)
    _calls(b, 6, cost=0.2)
    f, m = checks.check_ai(s, b, t("10:05"), None)
    assert m["cost_usd_today"] == pytest.approx(1.2)
    assert any(x["key"] == "ai.cost" for x in f)
    s2 = settings(tmp_path / "x")
    b2 = _b_store(s2)
    _calls(b2, 6)  # prices not configured: unavailable, never 0, never alerted
    f2, m2 = checks.check_ai(s2, b2, t("10:05"), None)
    assert m2["cost_usd_today"] is None and not any(x["key"] == "ai.cost" for x in f2)


def _integrated(tmp_path, calendar, llm, **over):
    from app.experiments.runtime import build_session

    s = settings(tmp_path, **over)
    prov = make_dataset(calendar, s, {DAY: scenario([NEUTRAL, TARGET])}).provider(calendar)
    eng = build_session(s, calendar, prov, PaperStore(s.paper.db_path),
                        PaperStore(s.integrated.ai_db_path), llm=llm,
                        resolved={"version": None, "label": "test", "components": None,
                                  "shadow": None, "canary": None})
    return s, eng


def _run(eng, until=None):
    from tests.paper_helpers import steps

    eng.open_session(DAY, t("09:00"))
    for w in steps(eng.baseline, DAY, until):
        eng.step(w)


def test_llm_circuit_breaker_bounds_calls_and_isolates_baseline(tmp_path, calendar):
    s, eng = _integrated(tmp_path, calendar, MockProvider(script=[LLMTimeout("t")] * 500),
                         monitoring={"llm_breaker_failures": 2,
                                     "llm_breaker_cooldown_minutes": 60})
    _run(eng, until="11:30")
    calls = eng.ai.store.all("SELECT COUNT(*) AS n FROM agent_model_calls")[0]["n"]
    failed_runs = eng.ai.store.all("SELECT COUNT(*) AS n FROM agent_model_calls WHERE "
                                   "error_kind IS NOT NULL")[0]["n"]
    # 27 candle slots 09:20-11:30. Without the breaker every model-calling run would hit
    # the dead provider; with it: 2 failures, open 60 min, one half-open trial, open again.
    assert failed_runs == calls and calls <= 4, calls
    events = [e["message"] for e in eng.ai.store.all("SELECT message FROM events WHERE kind = "
                                                     "'INTEGRATION'")]
    assert any("circuit OPEN" in e for e in events) and any("half-open" in e for e in events)
    assert len(eng.baseline.store.all("SELECT * FROM orders WHERE side = 'BUY'")) == 1
    assert eng.ai.store.all("SELECT * FROM orders") == []


# -- performance drift ------------------------------------------------------------------


def test_drift_needs_evidence_and_avoids_false_positives(tmp_path):
    cfg = settings(tmp_path).monitoring
    rng = np.random.default_rng(1)
    small = drift.performance_drift([1.0] * 5, [-3.0] * 5, cfg)
    assert small["kind"] == "INSUFFICIENT_EVIDENCE"
    same = drift.performance_drift(list(rng.normal(1, 5, 60)), list(rng.normal(1, 5, 60)), cfg)
    assert same["kind"] == "NO_DRIFT_DETECTED"
    worse = drift.performance_drift(list(rng.normal(2, 2, 80)), list(rng.normal(-3, 2, 80)),
                                    cfg)
    assert worse["kind"] == "PERFORMANCE_DRIFT"
    stable_inputs = drift.data_drift({"rvol": list(rng.normal(2, 0.5, 100))},
                                     {"rvol": list(rng.normal(2, 0.5, 100))}, cfg)
    assert stable_inputs[0]["kind"] == "NO_DRIFT_DETECTED"
    shifted = drift.data_drift({"rvol": list(rng.normal(2, 0.5, 100))},
                               {"rvol": list(rng.normal(3, 0.5, 100))}, cfg)
    assert shifted[0]["kind"] == "DATA_DRIFT"
    assert "POSSIBLE_CONCEPT_DRIFT (hypothesis only)" in drift.classify(worse, stable_inputs)
    assert "cause not established" in drift.classify(worse, shifted)
    assert drift.longest_losing_streak([1, -1, -1, -1, 2, -1]) == 3


def test_ks_matches_known_values():
    d, p = drift.ks_2samp([1, 2, 3, 4, 5], [1, 2, 3, 4, 5])
    assert d == 0 and p == 1.0
    d, p = drift.ks_2samp(list(range(50)), list(range(100, 150)))
    assert d == 1.0 and p < 1e-10


def test_losing_streak_alone_is_not_drift(tmp_path):
    s = settings(tmp_path, monitoring={"losing_streak_info": 3})
    a = PaperStore(s.paper.db_path)
    for i in range(8):
        a._insert("positions", {"position_id": f"P{i}", "signal_id": f"S{i}",
                                "trading_date": f"2025-03-{10 + i:02d}", "symbol": "X",
                                "state": "CLOSED", "quantity": 1, "entry_price": "70",
                                "entry_time": f"2025-03-{10 + i:02d}T09:45", "stop": "69",
                                "target": "72", "planned_net_loss": "1", "buy_cash": "70",
                                "buy_costs": "1", "exit_time": f"2025-03-{10 + i:02d}T10:00",
                                "net_pnl": "-1"})
    f, m = checks.check_performance(s, a, None, t("16:00"))
    assert {x["key"]: x["severity"] for x in f} == {"perf.losing_streak.A": "INFO"}
    assert m["A"]["classification"] == "INSUFFICIENT_EVIDENCE"


# -- alerts --------------------------------------------------------------------------------


def test_alert_dedup_cooldown_escalation_recovery_and_audit(tmp_path):
    s = settings(tmp_path, monitoring={"alert_cooldown_minutes": 30})
    ms = MonitorStore(s.monitoring.db_path)
    cap = Capture()
    ac = AlertCenter(ms, s, [cap])
    a1 = ac.raise_(finding(), t("10:00"))
    a2 = ac.raise_(finding(), t("10:10"))  # duplicate inside the cooldown: no notification
    assert a1["alert_id"] == a2["alert_id"] and a2["occurrences"] == 2
    ac.raise_(finding(), t("10:45"))  # cooldown passed: reminder
    ac.raise_(finding(severity="CRITICAL"), t("10:46"))  # escalation notifies at once
    assert [p["event"] for p in cap.sent] == ["RAISED", "REPEATED", "ESCALATED"]
    for k in ("alert_id", "detected_at", "severity", "component", "rule", "threshold",
              "evidence", "steps", "status"):
        assert k in cap.sent[0], k
    assert ac.resolve("data.stale", t("11:00"))
    assert cap.sent[-1]["event"] == "RECOVERED"
    again = ac.raise_(finding(), t("11:30"))  # a new incident gets a new id
    assert again["alert_id"] != a1["alert_id"]
    kinds = [e["kind"] for e in ac.history(a1["alert_id"])]
    assert kinds[0] == "RAISED" and "RESOLVED" in kinds
    with pytest.raises(sqlite3.DatabaseError):
        ms.store.conn.execute("DELETE FROM monitor_alert_events")


def test_outbox_delivery_is_recorded_honestly(tmp_path):
    s = settings(tmp_path)
    ms = MonitorStore(s.monitoring.db_path)

    class Broken:
        name, status = "broken", "SENT"

        def send(self, payload):
            raise ConnectionError("no route")

    ac = AlertCenter(ms, s, [OutboxNotifier(s.monitoring.alert_outbox), Broken()])
    a = ac.raise_(finding(), t("10:00"))
    assert a["notify"] == ["outbox:WRITTEN", "broken:FAILED"]
    line = json.loads(Path(s.monitoring.alert_outbox).read_text().splitlines()[0])
    assert line["alert_id"] == a["alert_id"] and line["event"] == "RAISED"
    events = {e["kind"] for e in ac.history(a["alert_id"])}
    assert {"NOTIFIED", "NOTIFY_FAILED"} <= events
    assert "DELIVERED" not in json.dumps(ac.history(a["alert_id"]))


def test_alert_inputs_are_sanitised(tmp_path):
    s = settings(tmp_path)
    ac = AlertCenter(MonitorStore(s.monitoring.db_path), s, [Capture()])
    evil = finding(key="data.x\nFAKE LOG LINE", evidence={"m": "api_key=sk-ant-SECRET0123456789"
                                                               "abcdef\r\n[CRITICAL] fake"})
    evil["rule"] = "line1\nline2\x1b[31m"
    a = ac.raise_(evil, t("10:00"))
    assert "\n" not in a["alert_key"] and "\n" not in a["rule"] and "\x1b" not in a["rule"]
    assert "SECRET0123456789" not in a["evidence"]
    assert "\n" not in clean("a\nb") and clean("x" * 900).__len__() == 500


# -- state machine, pause, override, restart -----------------------------------------------


def test_states_pause_recover_and_restart(tmp_path):
    s = settings(tmp_path, monitoring={"recovery_passes": 2})
    ms = MonitorStore(s.monitoring.db_path)
    crit = finding("exec.inconsistent.A", "CRITICAL",
                   {"state": "PAPER_TRADING_PAUSED", "arms": ["A"]})
    st = state.update(ms, s, [crit, finding("ai.failure_rate", "CRITICAL",
                                            {"state": "DEGRADED_AI"})], t("10:00"), 0)
    assert st["state"] == "PAPER_TRADING_PAUSED" and st["paused_arms"] == {
        "A": ["exec.inconsistent.A"]}
    assert state.paused_arms(ms, t("10:01")) == {"A": ["exec.inconsistent.A"]}
    assert state.update(ms, s, [], t("10:05"), 1)["state"] == "RECOVERY"
    assert state.paused_arms(ms, t("10:06")) == {}
    assert state.update(ms, s, [], t("10:10"), 1)["state"] == "RECOVERY"  # not enough passes
    assert state.update(ms, s, [], t("10:15"), 2)["state"] == "NORMAL"
    ms.close()
    again = MonitorStore(s.monitoring.db_path)  # restart: reconstructed from the log
    assert state.current(again)["state"] == "NORMAL"
    log = [e["to_state"] for e in again.all("SELECT to_state FROM monitor_state_events "
                                            "ORDER BY id")]
    assert log == ["PAPER_TRADING_PAUSED", "RECOVERY", "NORMAL"]
    with pytest.raises(sqlite3.DatabaseError):
        again.store.conn.execute("UPDATE monitor_state_events SET to_state = 'NORMAL'")


def test_manual_review_is_sticky_and_overrides_are_limited(tmp_path):
    s = settings(tmp_path)
    ms = MonitorStore(s.monitoring.db_path)
    drift_f = finding("perf.drift.B", "WARNING", {"state": "MANUAL_REVIEW_REQUIRED",
                                                  "arms": ["B"]})
    state.update(ms, s, [drift_f], t("16:00"), 0)
    assert state.update(ms, s, [], t("16:05"), 3)["state"] == "MANUAL_REVIEW_REQUIRED"
    with pytest.raises(ValueError):
        state.clear_review(ms, "owner", "done", "yes", t("16:10"))
    assert state.clear_review(ms, "owner", "reviewed sessions", "CLEAR MANUAL_REVIEW_REQUIRED",
                              t("16:10"))["state"] == "RECOVERY"
    paused = finding("exec.inconsistent.A", "CRITICAL", {"state": "PAPER_TRADING_PAUSED",
                                                         "arms": ["A"]})
    state.update(ms, s, [paused], t("16:20"), 0)
    with pytest.raises(ValueError, match="confirmation"):
        state.override(ms, s, "NORMAL", "owner", "checked", "please", 30, t("16:21"))
    with pytest.raises(ValueError, match="limited"):
        state.override(ms, s, "NORMAL", "owner", "checked", "OVERRIDE PAPER_TRADING_PAUSED",
                       999, t("16:21"))
    st = state.override(ms, s, "NORMAL", "owner", "false positive verified",
                        "OVERRIDE PAPER_TRADING_PAUSED", 30, t("16:21"))
    assert st["override"] and state.paused_arms(ms, t("16:30")) == {}
    assert "os user" in st["actor"]
    state.update(ms, s, [paused], t("17:00"), 0)  # override expired: pause again
    assert state.paused_arms(ms, t("17:01")) == {"A": ["exec.inconsistent.A"]}
    integrity = finding("version.integrity", "CRITICAL", {"state": "PAPER_TRADING_PAUSED",
                                                          "arms": ["B"]})
    state.update(ms, s, [integrity], t("17:05"), 0)
    with pytest.raises(ValueError, match="cannot be overridden"):
        state.override(ms, s, "NORMAL", "owner", "x", "OVERRIDE PAPER_TRADING_PAUSED", 10,
                       t("17:06"))


def test_monitoring_pause_blocks_new_entries_only(tmp_path, calendar):
    s, eng = _integrated(tmp_path, calendar, MockProvider())
    _dq(eng.baseline.store, date(2025, 3, 16))  # a calendar inconsistency -> pause all arms
    _run(eng)
    sig = eng.baseline.store.all("SELECT review_status, reasons FROM signals")
    assert sig[0]["review_status"] == "PAUSED_BY_MONITORING"
    assert "calendar.inconsistent" in sig[0]["reasons"]
    assert eng.baseline.store.all("SELECT * FROM orders") == []
    ms = MonitorStore(s.monitoring.db_path)
    assert state.current(ms)["state"] == "PAPER_TRADING_PAUSED"
    assert any(a["alert_key"] == "calendar.inconsistent" for a in AlertCenter(
        ms, s, []).open())


def test_normal_session_runs_unaffected_with_monitoring(tmp_path, calendar):
    s, eng = _integrated(tmp_path, calendar, MockProvider())
    _run(eng)
    assert [p["net_pnl"] for p in eng.baseline.store.all("SELECT net_pnl FROM positions")] == \
        ["15.69"]
    ms = MonitorStore(s.monitoring.db_path)
    assert state.current(ms)["state"] == "NORMAL"
    snaps = ms.all("SELECT scope FROM monitor_snapshots")
    assert len(snaps) > 50 and eng.monitor.errors == []


# -- versions ---------------------------------------------------------------------------


def test_version_mismatch_and_registry_tampering(tmp_path):
    from app.experiments.registry import Registry
    from app.experiments.validate import init_registry

    s = settings(tmp_path)
    reg = Registry(s.experiments.registry_db)
    init_registry(reg, s, "owner", "INIT paper-ai", t("08:00"))
    f, m, comp = checks.check_versions(s, t("10:00"))
    assert f == [] and comp["prompt"]["version"] == "research-v3"
    b = _b_store(s)
    b._insert("agent_recommendations", {
        "recommendation_id": "R1", "run_id": "x", "market_date": DAY.isoformat(),
        "window": "intraday", "symbol": "X", "status": "WATCH",
        "created_at": t("10:00").isoformat(), "schema_version": "v",
        "body": json.dumps({"prompt_version": "research-v9", "model": "other-model",
                            "ai_status": "OK", "unsupported_claims": [], "status": "WATCH",
                            "market_date": DAY.isoformat(), "signal_id": None}),
        "sha256": "x"})
    _calls(b, 3)
    fa, _ = checks.check_ai(s, b, t("10:05"), comp)
    mis = next(x for x in fa if x["key"] == "version.ai_mismatch")
    assert mis["severity"] == "CRITICAL" and mis["effect"]["arms"] == ["B"]
    reg.store.conn.execute("INSERT INTO experiments (experiment_id, created_at, name, "
                           "hypothesis, reason, context, baseline_version, candidate_version, "
                           "spec, acceptance_version, acceptance_sha256, acceptance_text, "
                           "code_versions, status) VALUES ('EXP-x', 'x', 'n', 'h', 'r', "
                           "'paper-ai', 'v', 'v', '{}', 'a', 's', 't', '{}', 'ACTIVE')")
    f2, _, _ = checks.check_versions(s, t("10:10"))
    assert f2[0]["key"] == "version.integrity" and f2[0]["severity"] == "CRITICAL"


# -- storage, dashboard, boundaries -------------------------------------------------------


def test_storage_and_full_run_snapshot(tmp_path, calendar):
    s = settings(tmp_path)
    _a_store(tmp_path, s, calendar).close()
    snap = run_monitor(s, t("16:00"), "full", notifiers=[])
    assert snap["metrics"]["storage"]["paper"] == "ok"
    assert snap["metrics"]["ai"]["failure_rate"] is None  # unavailable, not zero
    assert snap["metrics"]["storage"]["latest_backup_age_hours"] is None
    assert "performance" in snap["metrics"]
    bad = tmp_path / "corrupt"
    bad.mkdir()
    (bad / "mon.db").write_bytes(b"not a database" * 100)
    s2 = settings(bad, ai_eval={"ledger_db": str(bad / "mon.db")})
    f, m = checks.check_storage(s2, t("16:00"))
    assert any(x["key"] == "storage.db.ledger" and x["severity"] == "CRITICAL" for x in f)


def test_monitoring_never_modifies_trading_records(tmp_path, calendar):
    s, eng = _integrated(tmp_path, calendar, MockProvider())
    _run(eng)
    before = {t_: eng.baseline.store.all(f"SELECT * FROM {t_}") for t_ in
              ("signals", "orders", "positions")}
    recs = eng.ai.store.all("SELECT sha256 FROM agent_recommendations")
    run_monitor(s, t("16:30"), "full", {"A": eng.baseline.store, "B": eng.ai.store},
                notifiers=[])
    after = {t_: eng.baseline.store.all(f"SELECT * FROM {t_}") for t_ in before}
    assert [list(map(dict, before[k])) for k in before] == \
        [list(map(dict, after[k])) for k in after]
    assert recs == eng.ai.store.all("SELECT sha256 FROM agent_recommendations")


def test_no_order_or_parameter_change_path_in_monitoring():
    for p in (APP / "monitoring").glob("*.py"):
        imps = set()
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Import):
                imps |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imps.add(node.module)
        assert not {i for i in imps if "groww" in i or i in (
            "requests", "socket", "subprocess", "app.experiments.promotion")}, p.name
        src = p.read_text()
        for word in ("place_order", "modify_order", "cancel_order", "settings.yaml",
                     "yaml.dump", ".transition(", "update_session("):
            assert word not in src, (p.name, word)


def test_dashboard_monitoring_view_shows_unavailable(tmp_path, calendar, monkeypatch):
    from streamlit.testing.v1 import AppTest

    s = settings(tmp_path)
    run_monitor(s, t("10:00"), "full", notifiers=[])
    monkeypatch.setenv("NSE_MONITORING__DB_PATH", s.monitoring.db_path.as_posix())
    monkeypatch.setenv("NSE_PAPER_DB", str(tmp_path / "none.db"))
    at = AppTest.from_file(str(APP / "dashboard.py"), default_timeout=60).run()
    at.sidebar.radio[0].set_value("Monitoring").run()
    assert not at.exception
    assert any("Monitoring" in x.value for x in at.subheader)
    assert any(m.value == "unavailable" for m in at.metric)


def test_uninitialised_portfolio_is_not_a_failure(tmp_path, calendar):
    """Found on the real project: a never-used paper DB made the check 'fail'."""
    s = settings(tmp_path)
    f, m = checks.check_execution(s, {"A": PaperStore(s.paper.db_path)}, t("10:00"))
    assert f == [] and "not initialised" in m["A"]
