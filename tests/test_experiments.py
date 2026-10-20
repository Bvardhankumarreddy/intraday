"""Phase 14: experiments, leakage-resistant validation and controlled promotion.
ALL DATA IS SYNTHETIC and the models are deterministic mocks. The candidate
mock skips the synthetic losing days ON PURPOSE so that the PASS path can be
exercised; nothing here is evidence about any real prompt or model.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.agent.llm import MockProvider
from app.config import MarketConfig, PROJECT_ROOT
from app.experiments import promotion as PR
from app.experiments.candidate import CandidateSpec, current_components, weakened_controls
from app.experiments.registry import IntegrityError, Registry, RegistryError
from app.experiments.runtime import build_session, resolve
from app.experiments.validate import classify, create, init_registry, splits, validate
from app.utils.market_calendar import MarketCalendar
from tests.helpers import bars
from tests.robustness_helpers import e2e_settings, frozen_baseline, session_rows

IST = ZoneInfo("Asia/Kolkata")
APP = PROJECT_ROOT / "app"
EARLY = datetime(2025, 1, 1, 8, 0, tzinfo=IST)
NOW = datetime(2026, 10, 20, 18, 0, tzinfo=IST)
ACCEPT = PROJECT_ROOT / "config" / "experiments" / "acceptance.yaml"


def days() -> list[date]:
    return MarketCalendar(MarketConfig(), holidays={}).trading_days(date(2025, 1, 6),
                                                                   date(2025, 3, 28))


def stop_days() -> set[str]:
    return {d.isoformat() for i, d in enumerate(days()) if i >= 5 and i % 2 == 1
            and (i // 2) % 2 == 1}


def write_data(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    frame = pd.concat([bars(d, session_rows(i)) for i, d in enumerate(days())],
                      ignore_index=True)
    frame["timestamp"] = frame["timestamp"].map(lambda t: t.isoformat())
    frame.to_csv(folder / "SYNTH_5m.csv", index=False)
    pd.DataFrame({"symbol": ["SYNTH"], "name": ["synthetic"]}).to_csv(
        folder / "instruments.csv", index=False)
    return folder


class Selective(MockProvider):
    """Deterministic mock that answers no_trade on the given dates."""

    def __init__(self, skip=frozenset()):
        super().__init__()
        self.skip = set(skip)

    def chat(self, system, messages, tools, tool_choice):
        resp = super().chat(system, messages, tools, tool_choice)
        m = re.search(r"for (\d{4}-\d{2}-\d{2})", json.loads(messages[0]["content"])
                      .get("task", ""))
        if m and m.group(1) in self.skip:
            for u in resp.tool_uses:
                if u.name == "submit_research":
                    for c in u.input["candidates"]:
                        c["recommendation"] = "no_trade"
        return resp


def factory(name, comp):
    return Selective(stop_days() if name == "candidate" else ())


def env(tmp: Path, **acceptance):
    from tests.test_aieval import make_protocol

    data = write_data(tmp / "data")
    base = frozen_baseline(tmp)
    text = ACCEPT.read_text().replace("resamples: 2000", "resamples: 300")
    lowered = {"sessions": 10, "closed_trades_candidate": 2, "closed_trades_approved": 3,
               "concentration_top_sessions_removed": 1, "min_shadow_sessions": 2,
               "canary_sessions": 2, **acceptance}
    for k, v in lowered.items():
        text = re.sub(rf"(?m)^(\s+){k}: .*$", rf"\g<1>{k}: {v}", text, count=1)
    acc = tmp / "acceptance.yaml"
    acc.write_text(text)
    live = e2e_settings(tmp, experiments={
        "registry_db": str(tmp / "registry.db"), "output_dir": str(tmp / "exp"),
        "acceptance_file": str(acc), "shadow_db_path": str(tmp / "shadow.db"),
        "canary_db_path": str(tmp / "canary.db")},
        integrated={"ai_db_path": str(tmp / "b.db"), "reports_dir": str(tmp / "ir")},
        paper={"db_path": str(tmp / "a.db"), "lock_file": str(tmp / "p.lock")},
        evaluation={"output_dir": str(tmp / "eval")},
        agent={"reports_dir": str(tmp / "agent")},
        monitoring={"db_path": str(tmp / "monitoring.db"),
                    "alert_outbox": str(tmp / "alerts.jsonl")},
        ops={"backups_dir": str(tmp / "backups")})
    proto = make_protocol(tmp, base, MockProvider(), live)
    live = live.model_copy(update={"ai_eval": live.ai_eval.model_copy(update={
        "protocol_file": proto, "ledger_db": tmp / "ledger.db", "output_dir": tmp / "ai"})})
    reg = Registry(live.experiments.registry_db)
    init_registry(reg, live, "owner", "INIT paper-ai", EARLY)
    return live, reg, data


def spec(tmp: Path, name="skip-losers", **over) -> CandidateSpec:
    p = tmp / f"{name}.txt"
    p.write_text(f"Research prompt variant {name}.")
    return CandidateSpec(name=name, hypothesis="the candidate skips losing breakouts",
                         reason="synthetic machinery test",
                         prompt={"version": f"research-{name}", "file": str(p)}, **over)


@pytest.fixture(scope="module")
def passed(tmp_path_factory):
    """An experiment that passed validation and the final set (synthetic)."""
    tmp = tmp_path_factory.mktemp("exp")
    live, reg, data = env(tmp)
    eid = create(reg, live, spec(tmp), "owner", EARLY)
    val = validate(reg, eid, live, data, llm_factory=factory, now=EARLY)
    fin = validate(reg, eid, live, data, final=True, llm_factory=factory, now=EARLY)
    return tmp, live, reg, data, eid, val, fin


# -- configuration, reproducibility, splits ---------------------------------------------


def test_experiment_records_everything_needed_to_reproduce(passed):
    tmp, live, reg, data, eid, val, fin = passed
    x = reg.experiment(eid)
    for k in ("hypothesis", "reason", "baseline_version", "candidate_version", "spec",
              "acceptance_version", "acceptance_sha256", "acceptance_text", "code_versions"):
        assert x[k], k
    cand = reg.version(x["candidate_version"])["components"]
    assert cand["prompt"]["version"] == "research-skip-losers" and cand["prompt"]["sha256"]
    assert val["dataset_id"] == fin["dataset_id"] and val["arm_hashes"]["A"]
    assert val["invalid"] == [] and fin["invalid"] == []  # reruns reproduced every arm
    again = reg.add_version("again", cand, "owner", EARLY)
    assert again == x["candidate_version"]  # content-addressed


def test_chronological_splits_with_embargo(passed):
    tmp, live, reg, data, eid, val, fin = passed
    from app.backtesting.dataset import load_csv_dataset
    from app.robustness.baseline import load_frozen
    from app.aieval.protocol import load_protocol
    import yaml

    frozen = load_frozen(load_protocol(live.ai_eval.protocol_file)[0].baseline_path())
    hist = frozen.historical_settings()
    cal = MarketCalendar(hist.market)
    plan = splits(load_csv_dataset(data, hist, cal), hist, cal,
                  yaml.safe_load(live.experiments.acceptance_file.read_text()))
    dev, v, f = plan["development"], plan["validation"], plan["final"]
    assert dev[-1] < v[0] and v[-1] < f[0]  # strictly chronological, never shuffled
    assert len(plan["embargoed"]) == 2 and all(dev[-1] < e < f[0] for e in plan["embargoed"])
    assert sum(len(x) for x in plan["folds"]) == len(v) and len(plan["folds"]) == 3
    assert val["period"] == [v[0], v[-1]] and fin["period"] == [f[0], f[-1]]


def test_pass_requires_every_mandatory_criterion(passed):
    tmp, live, reg, data, eid, val, fin = passed
    assert val["classification"] == fin["classification"] == "PASS"
    assert all(c["status"] == "PASS" for c in val["criteria"] if c["mandatory"])
    m = val["metrics"]
    assert m["CANDIDATE"]["net_pnl"] > m["APPROVED"]["net_pnl"]
    assert val["decomposition"]["trades_b_only_capacity"] == 0
    assert reg.experiment(eid)["status"] == "VALIDATED"
    report = Path(val["report"]).read_text()
    for section in ("Periods and data", "Results by arm", "Uncertainty and robustness",
                    "AI quality and operating cost", "Acceptance criteria",
                    "Classification and promotion eligibility"):
        assert f"## {section}" in report


def test_final_set_runs_only_once(passed):
    tmp, live, reg, data, eid, val, fin = passed
    with pytest.raises(RegistryError):
        validate(reg, eid, live, data, final=True, llm_factory=factory, now=EARLY)


# -- classifications and safeguards -----------------------------------------------------


def test_insufficient_sample_is_inconclusive_and_rejected(tmp_path):
    live, reg, data = env(tmp_path, sessions=200)
    eid = create(reg, live, spec(tmp_path), "owner", EARLY)
    out = validate(reg, eid, live, data, llm_factory=factory, now=EARLY)
    assert out["classification"] == "INCONCLUSIVE"
    assert reg.experiment(eid)["status"] == "REJECTED"
    with pytest.raises(RegistryError):
        PR.request_approval(reg, eid, "owner", "please", NOW)


def test_failing_candidate_is_rejected(tmp_path):
    live, reg, data = env(tmp_path)
    winners = {d.isoformat() for d in days()} - stop_days()
    eid = create(reg, live, spec(tmp_path, "skip-winners"), "owner", EARLY)
    out = validate(reg, eid, live, data, now=EARLY,
                   llm_factory=lambda n, c: Selective(winners if n == "candidate" else ()))
    assert out["classification"] == "FAIL"
    assert any(c["status"] == "FAIL" for c in out["criteria"])
    assert reg.experiment(eid)["status"] == "REJECTED"


def test_weakened_risk_control_is_invalid(tmp_path):
    live, reg, data = env(tmp_path)
    s = spec(tmp_path, "looser", settings={"risk": {"max_trades_per_day": 20}})
    eid = create(reg, live, s, "owner", EARLY)
    out = validate(reg, eid, live, data, llm_factory=factory, now=EARLY)
    assert out["classification"] == "INVALID"
    assert any("risk.max_trades_per_day raised" in x for x in out["invalid"])


def test_weakened_controls_detection():
    from app.config import Settings

    a = Settings()
    assert weakened_controls(a, a) == []
    b = Settings(risk={"max_daily_loss": a.risk.max_daily_loss + 1},
                 execution={"slippage_pct_per_side": 0.0},
                 cost_review={"min_net_reward_risk": 0.5})
    found = " ".join(weakened_controls(a, b))
    for k in ("max_daily_loss", "slippage_pct_per_side", "min_net_reward_risk"):
        assert k in found
    stricter = Settings(risk={"risk_pct": a.risk.risk_pct / 2})
    assert weakened_controls(a, stricter) == []


def test_code_change_or_new_baseline_invalidates(tmp_path, monkeypatch):
    live, reg, data = env(tmp_path)
    eid = create(reg, live, spec(tmp_path), "owner", EARLY)
    import app.experiments.validate as V

    real = V.code_versions()
    monkeypatch.setattr(V, "code_versions", lambda: {**real, "agent_code_sha256": "changed"})
    out = validate(reg, eid, live, data, llm_factory=factory, now=EARLY)
    assert out["classification"] == "INVALID"
    assert any("code changed" in x for x in out["invalid"])


def test_multiple_testing_adjusts_confidence(passed):
    tmp, live, reg, data, eid, val, fin = passed
    second = create(reg, live, spec(tmp, "another"), "owner", EARLY)
    out = validate(reg, second, live, data, llm_factory=factory, now=EARLY)
    mt = out["multiple_testing"]
    assert mt["experiments_on_overlapping_period"] == 2
    assert mt["adjusted_confidence"] == pytest.approx(1 - 0.05 / 2)


def test_classification_precedence():
    ok = {"criterion": "x", "mandatory": True, "status": "PASS", "evidence": {}}
    bad = {**ok, "status": "FAIL"}
    few = {**ok, "criterion": "minimum_evidence", "status": "INSUFFICIENT"}
    assert classify([ok], []) == "PASS"
    assert classify([ok, bad], []) == "FAIL"
    assert classify([few, bad], []) == "INCONCLUSIVE"
    assert classify([ok], ["leak"]) == "INVALID"
    assert classify([{**bad, "mandatory": False}, ok], []) == "PASS"


# -- registry, approval, integrity ---------------------------------------------------------


def test_registry_content_is_immutable_and_tampering_detected(tmp_path):
    live, reg, data = env(tmp_path)
    vid = reg.active_version("paper-ai")
    eid = create(reg, live, spec(tmp_path), "owner", EARLY)
    for sql in ("UPDATE versions SET label = 'x'", "DELETE FROM registry_events",
                "UPDATE experiments SET hypothesis = 'rewritten'"):
        with pytest.raises(sqlite3.DatabaseError):
            reg.store.conn.execute(sql)
    assert reg.verify() == []
    reg.store.conn.execute("UPDATE experiments SET status = 'ACTIVE' WHERE experiment_id = ?",
                           (eid,))
    assert any("does not match its event log" in p for p in reg.verify())
    with pytest.raises(IntegrityError):
        reg.active_version("paper-ai")
    with pytest.raises(IntegrityError):
        resolve(live)
    assert vid


def test_approval_needs_a_person_and_the_exact_phrase(passed):
    tmp, live, reg, data, eid, val, fin = passed
    if reg.experiment(eid)["status"] == "VALIDATED":
        PR.request_approval(reg, eid, "owner", "validated", EARLY)
    with pytest.raises(RegistryError, match="confirmation"):
        PR.approve(reg, eid, "owner", "ok", "APPROVE", EARLY)
    with pytest.raises(RegistryError, match="person"):
        PR.approve(reg, eid, "system", "ok", f"APPROVE {eid}", EARLY)
    with pytest.raises(RegistryError):  # cannot skip ahead
        PR.activate(reg, live, eid, "owner", "go", f"ACTIVATE {eid}", EARLY)


def test_runtime_refuses_strategy_changing_versions(tmp_path):
    live, reg, data = env(tmp_path)
    s = spec(tmp_path, "rvol", settings={"strategy": {"relative_volume_threshold": 2.0}})
    eid = create(reg, live, s, "owner", EARLY)
    for to in ("VALIDATING", "VALIDATED", "AWAITING_APPROVAL"):  # real transitions
        reg.transition(eid, to, "owner", "test path", EARLY)
    reg.transition(eid, "APPROVED", "owner", "ok", EARLY, confirm=f"APPROVE {eid}")
    with pytest.raises(RegistryError, match="new frozen baseline"):
        PR.start_shadow(reg, eid, "owner", "go", EARLY)


# -- shadow, canary, activation, rollback --------------------------------------------------


def _sessions(live, cal, provider, which, llm_factory=None):
    from app.paper.scheduler import replay_session
    from app.paper.store import PaperStore

    a, b = PaperStore(live.paper.db_path), PaperStore(live.integrated.ai_db_path)
    eng = build_session(live, cal, provider, a, b, llm_factory=llm_factory or factory)
    for d in which:
        replay_session(eng, d)
    out = {"engine": eng, "a": a, "b": b}
    return out


def test_full_lifecycle_shadow_canary_activate_rollback(passed):
    tmp, live, reg, data, eid, val, fin = passed
    from app.backtesting.dataset import load_csv_dataset

    cal = MarketCalendar(live.market)
    provider = load_csv_dataset(data, live, cal).provider(cal)
    approved = reg.active_version("paper-ai")
    st = reg.experiment(eid)["status"]
    if st == "VALIDATED":
        PR.request_approval(reg, eid, "owner", "validated", EARLY)
    if reg.experiment(eid)["status"] == "AWAITING_APPROVAL":
        PR.approve(reg, eid, "owner", "reviewed the report", f"APPROVE {eid}", EARLY)
    PR.start_shadow(reg, eid, "owner", "go", EARLY)
    with pytest.raises(RegistryError, match="shadow session"):  # no shadow evidence yet
        PR.start_canary(reg, live, eid, "owner", "too early", EARLY)
    cand_prompt = reg.version(reg.experiment(eid)["candidate_version"])["components"][
        "prompt"]["version"]

    # shadow: research only, never gates or orders anything
    run_days = days()[40:44]
    shadow_run = _sessions(live, cal, provider, run_days[:2])
    eng = shadow_run["engine"]
    assert eng.ai_version["shadow"]["experiment"] == eid and eng.extras[0].engine is None
    from app.paper.store import PaperStore

    sh = PaperStore(live.experiments.shadow_db_path)
    assert {r["prompt_version"] for r in sh.all("SELECT prompt_version FROM agent_runs")} == \
        {cand_prompt}
    assert not sh.all("SELECT 1 FROM sqlite_master WHERE name = 'orders'") or \
        not sh.all("SELECT 1 FROM orders")
    b_prompts = {r["prompt_version"] for r in shadow_run["b"].all(
        "SELECT prompt_version FROM agent_runs")}
    assert cand_prompt not in b_prompts  # the approved arm kept the approved prompt
    for s in (shadow_run["a"], shadow_run["b"], sh):
        s.close()

    ev = PR.start_canary(reg, live, eid, "owner", "shadow looked fine", EARLY)
    assert ev["sessions"] >= 2
    with pytest.raises(RegistryError, match="completed canary"):
        PR.activate(reg, live, eid, "owner", "go", f"ACTIVATE {eid}", EARLY)
    canary_run = _sessions(live, cal, provider, run_days[2:4])
    can = PaperStore(live.experiments.canary_db_path)
    assert {r["trading_date"] for r in can.all("SELECT trading_date FROM sessions")} == \
        {d.isoformat() for d in run_days[2:4]}
    a_orders = canary_run["a"].all("SELECT COUNT(*) AS n FROM orders")[0]["n"]
    assert a_orders >= can.all("SELECT COUNT(*) AS n FROM orders")[0]["n"]
    for s in (canary_run["a"], canary_run["b"], can):
        s.close()

    PR.activate(reg, live, eid, "owner", "canary met its criteria", f"ACTIVATE {eid}", EARLY)
    new = reg.experiment(eid)["candidate_version"]
    assert reg.active_version("paper-ai") == new and resolve(live)["version"] == new
    restarted = Registry(live.experiments.registry_db)  # restart: same verified state
    assert restarted.active_version("paper-ai") == new and restarted.verify() == []

    assert PR.rollback(reg, eid, "owner", "regression seen", f"ROLLBACK {eid}", EARLY) == \
        approved
    assert reg.active_version("paper-ai") == approved and resolve(live)["version"] == approved
    kinds = [h["kind"] for h in reg.history("paper-ai")]
    assert kinds == ["ACTIVATED", "ACTIVATED", "ROLLED_BACK_TO"]
    assert reg.verify() == []


def test_only_one_active_version_per_context(tmp_path):
    live, reg, data = env(tmp_path)
    with pytest.raises(RegistryError, match="already has an active version"):
        reg.bootstrap("paper-ai", current_components(live), "owner", "INIT paper-ai", NOW)
    with pytest.raises(RegistryError, match="confirmation"):
        Registry(tmp_path / "other.db").bootstrap("paper-ai", {}, "owner", "yes", NOW)


def test_no_real_order_path_in_experiments():
    for p in (APP / "experiments").glob("*.py"):
        imps = set()
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Import):
                imps |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imps.add(node.module)
        assert not {i for i in imps if "groww" in i or i in ("requests", "socket",
                                                              "subprocess")}, p.name
        for word in ("place_order", "modify_order", "cancel_order", "execution_mode ="):
            assert word not in p.read_text(), (p.name, word)


def test_dashboard_experiments_view(passed, monkeypatch):
    from streamlit.testing.v1 import AppTest

    tmp, live, *_ = passed
    monkeypatch.setenv("NSE_EXPERIMENTS__REGISTRY_DB", str(live.experiments.registry_db))
    monkeypatch.setenv("NSE_PAPER_DB", str(tmp / "none.db"))
    at = AppTest.from_file(str(APP / "dashboard.py"), default_timeout=60).run()
    at.sidebar.radio[0].set_value("Experiments").run()
    assert not at.exception
    assert any("Experiments and controlled promotion" in s.value for s in at.subheader)
