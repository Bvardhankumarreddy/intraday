"""Phase 8 operations: logging redaction, filesystem safety, store hardening,
alerts, backup/restore, retention, health and gate checks. Isolated temp
directories and synthetic data only."""

from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import stat
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.ops.alerts import AlertManager
from app.ops.backup import BackupError, create_backup, restore_backup, verify_backup
from app.ops.health import health_report
from app.ops.retention import prune
from app.paper.store import PaperStore
from app.utils.fs import UnsafePathError, safe_child, write_preserving
from app.utils.logging import JsonFormatter, log_context, redact_text, register_secret
from tests.paper_helpers import NEUTRAL, TARGET, build, run

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2025, 3, 12)
NOW = datetime(2025, 3, 12, 16, 0, tzinfo=IST)


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# -- logging -------------------------------------------------------------------------------


def test_redaction_covers_bearer_jwt_and_registered_values():
    register_secret("MY-REAL-TOTP-SEED-XYZ")
    text = ("Authorization: Bearer abcdefgh12345678 then token=zzz999 and "
            "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4 and "  # gate: allow-secret
            "MY-REAL-TOTP-SEED-XYZ")  # fake values for the redaction test
    out = redact_text(text)
    for leaked in ("abcdefgh12345678", "zzz999", "eyJhbGciOiJIUzI1", "MY-REAL-TOTP-SEED-XYZ"):
        assert leaked not in out


def test_log_lines_are_structured_and_redacted_including_exceptions():
    register_secret("SECRET-IN-EXCEPTION-42")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("test.phase8")
    log.addHandler(handler)
    try:
        with log_context(component="scheduler", session_id="2025-03-12",
                         correlation_id="2025-03-12T0940", event_type="step"):
            try:
                raise RuntimeError("login failed with SECRET-IN-EXCEPTION-42")
            except RuntimeError:
                log.exception("step failed", extra={"api_key": "should-hide"})
    finally:
        log.removeHandler(handler)
    line = json.loads(stream.getvalue())
    assert line["component"] == "scheduler" and line["correlation_id"] == "2025-03-12T0940"
    assert line["session_id"] == "2025-03-12" and line["level"] == "ERROR" and line["ts"]
    assert "SECRET-IN-EXCEPTION-42" not in stream.getvalue() and line["api_key"] == "***"


def test_settings_load_registers_secrets_for_redaction(monkeypatch):
    from app.config import load_settings

    monkeypatch.setenv("NSE_BROKER_TOTP_SECRET", "JBSWY3DPEHPK3PXPQRST")
    load_settings()
    assert "JBSWY3DPEHPK3PXPQRST" not in redact_text("seed JBSWY3DPEHPK3PXPQRST used")


# -- filesystem ------------------------------------------------------------------------------


def test_safe_child_blocks_traversal(tmp_path):
    assert safe_child(tmp_path, "a/b.json") == (tmp_path / "a" / "b.json").resolve()
    for bad in ("../x", "/etc/passwd", "a/../../x", ""):
        with pytest.raises(UnsafePathError):
            safe_child(tmp_path, bad)


def test_write_preserving_archives_previous_versions(tmp_path):
    f = tmp_path / "r.json"
    assert write_preserving(f, "v1") is None
    assert write_preserving(f, "v1") is None  # identical: no copy
    archived = write_preserving(f, "v2")
    assert f.read_text() == "v2" and archived.read_text() == "v1"
    assert len(list((tmp_path / ".history").iterdir())) == 1
    assert mode(tmp_path / ".history") == 0o700 and mode(archived) == 0o600


# -- store hardening -----------------------------------------------------------------------------


def test_store_is_private_wal_with_schema_version(tmp_path):
    db = tmp_path / "data" / "paper.db"
    store = PaperStore(db)
    assert mode(db) == 0o600 and mode(db.parent) == 0o700
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert store.schema_version() == 2


def test_reader_does_not_block_engine_writes(calendar, tmp_path):
    db = tmp_path / "c.db"
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET], db=db)
    reader = sqlite3.connect(db)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM events").fetchone()  # open read transaction
    run(engine, DAY)  # every engine write commits while the reader is open
    reader.rollback()
    reader.close()
    assert store.positions()[0]["net_pnl"] == "15.69"


def test_report_history_keeps_every_version(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    run(engine, DAY)
    first = store.report(DAY)
    store.save_report(DAY, NOW, {**first, "note": "regenerated"})
    store.save_report(DAY, NOW, {**first, "note": "regenerated"})  # identical: not duplicated
    versions = store.report_versions(DAY)
    assert len(versions) == 2 and json.loads(versions[0]["body"]) == first
    assert store.report(DAY)["note"] == "regenerated"


def test_pre_phase8_database_is_migrated_additively(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE reports (trading_date TEXT PRIMARY KEY, generated_at "
                       "TEXT NOT NULL, body TEXT NOT NULL);"
                       "INSERT INTO reports VALUES ('2025-03-12', 'x', '{\"a\": 1}');")
    conn.close()
    store = PaperStore(db)
    assert store.schema_version() == 2 and store.report(DAY) == {"a": 1}
    store.save_report(DAY, NOW, {"a": 2})
    assert [json.loads(v["body"]) for v in store.report_versions(DAY)] == [{"a": 1}, {"a": 2}]


# -- alerts ---------------------------------------------------------------------------------------


class Recorder:
    name = "recorder"

    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append((alert["alert_key"], alert["severity"]))


def test_alerts_deduplicate_rate_limit_escalate_and_reopen(tmp_path):
    store = PaperStore(tmp_path / "a.db")
    notifier = Recorder()
    mgr = AlertManager(store, Settings(ops={"alert_repeat_minutes": 30}), notifier)
    for i in range(5):  # a storm of the same alert within the window
        mgr.raise_alert("data.blocked", "WARNING", "md", f"blocked {i}", NOW + timedelta(minutes=i))
    assert notifier.sent == [("data.blocked", "WARNING")]
    assert mgr.open_alerts()[0]["occurrences"] == 5
    mgr.raise_alert("data.blocked", "ERROR", "md", "worse", NOW + timedelta(minutes=6))
    assert notifier.sent[-1] == ("data.blocked", "ERROR")  # escalation notifies at once
    mgr.raise_alert("data.blocked", "ERROR", "md", "still", NOW + timedelta(minutes=40))
    assert len(notifier.sent) == 3  # repeat window elapsed
    assert mgr.resolve("data.blocked", NOW + timedelta(minutes=41))
    assert mgr.open_alerts() == []
    mgr.raise_alert("data.blocked", "WARNING", "md", "again", NOW + timedelta(minutes=42))
    assert mgr.open_alerts()[0]["occurrences"] == 1 and len(notifier.sent) == 4


# -- backup / restore -------------------------------------------------------------------------------


def test_backup_verify_and_isolated_restore(calendar, tmp_path):
    db = tmp_path / "live" / "paper.db"
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET], db=db)
    run(engine, DAY)
    live_before = db.read_bytes()
    b = create_backup(db, tmp_path / "backups", NOW)
    b2 = create_backup(db, tmp_path / "backups", NOW)  # same second: new name, no overwrite
    assert b["path"] != b2["path"] and mode(b["path"]) == 0o600
    rep = verify_backup(b["path"], engine.settings, DAY, active_db=db)
    assert rep["status"] == "PASS" and rep["rows"]["positions"] == 1
    assert rep["portfolio"]["cash"] == "2015.69"
    with pytest.raises(BackupError, match="active database"):
        restore_backup(b["path"], db, db, engine.settings)
    target = tmp_path / "restored" / "paper.db"
    out = restore_backup(b["path"], target, db, engine.settings)
    assert out["verification"]["status"] == "PASS"
    restored = PaperStore(target)
    assert restored.positions()[0]["net_pnl"] == "15.69"
    restored.close()
    with pytest.raises(BackupError, match="already exists"):
        restore_backup(b["path"], target, db, engine.settings)
    assert db.read_bytes() == live_before  # the active database was never touched


def test_tampered_backup_fails_verification(calendar, tmp_path):
    db = tmp_path / "p.db"
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET], db=db)
    run(engine, DAY)
    b = create_backup(db, tmp_path / "bk", NOW)
    conn = sqlite3.connect(b["path"])
    conn.execute("UPDATE portfolio SET cash = '9999'")
    conn.commit()
    conn.close()
    rep = verify_backup(b["path"], engine.settings, DAY)
    status = {c["check"]: c["status"] for c in rep["checks"]}
    assert rep["status"] == "FAIL" and status["manifest_sha256"] == "FAIL"
    assert status["reconciliation"] == "FAIL"


# -- retention ----------------------------------------------------------------------------------------


def test_retention_only_touches_old_logs_and_surplus_backups(tmp_path):
    logs, bk, paper = tmp_path / "logs", tmp_path / "bk", tmp_path / "paper"
    for d in (logs, bk, paper):
        d.mkdir()
    old_log, new_log = logs / "old.log", logs / "new.log"
    old_log.write_text("x")
    new_log.write_text("y")
    os.utime(old_log, (0, 0))
    db = paper / "paper.db"
    db.write_text("trading records")
    (logs / "report.json").write_text("{}")
    os.utime(logs / "report.json", (0, 0))
    for i in range(4):
        f = bk / f"paper-2025010{i}T000000.db"
        f.write_text(str(i))
        f.with_suffix(".json").write_text("{}")
        os.utime(f, (1000 + i, 1000 + i))
    s = Settings(ops={"log_dir": str(logs), "backups_dir": str(bk), "keep_backups": 2,
                      "log_retention_days": 30}, paper={"db_path": str(db)})
    dry = prune(s, datetime.now())
    assert dry["dry_run"] and dry["deleted"] == [] and old_log.exists()
    done = prune(s, datetime.now(), apply=True)
    assert not old_log.exists() and new_log.exists() and (logs / "report.json").exists()
    assert sorted(p.name for p in bk.glob("*.db")) == ["paper-20250102T000000.db",
                                                       "paper-20250103T000000.db"]
    assert db.read_text() == "trading records" and len(done["deleted"]) == 5


# -- health ----------------------------------------------------------------------------------------------


def test_health_detects_stalled_session_in_market_hours(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    run(engine, DAY, until="10:00")
    settings = engine.settings.model_copy(update={"market": engine.settings.market.model_copy(
        update={"holidays_file": tmp_path / "none.yaml"})})
    ok = health_report(settings, datetime(2025, 3, 12, 10, 5, tzinfo=IST), store.path)
    session = next(c for c in ok["checks"] if c["check"] == "session")
    assert session["status"] == "PASS" and ok["status"] != "UNHEALTHY"
    stalled = health_report(settings, datetime(2025, 3, 12, 10, 30, tzinfo=IST), store.path)
    session = next(c for c in stalled["checks"] if c["check"] == "session")
    assert session["status"] == "FAIL" and "stalled" in session["detail"]
    assert stalled["status"] == "UNHEALTHY"


def test_health_is_not_healthy_with_critical_alert_or_bad_permissions(tmp_path):
    db = tmp_path / "h.db"
    store = PaperStore(db)
    s = Settings()
    AlertManager(store, s).raise_alert("state.inconsistent", "CRITICAL", "x", "bad", NOW)
    store.close()
    rep = health_report(s, NOW, db)
    assert rep["status"] == "UNHEALTHY"
    assert next(c for c in rep["checks"] if c["check"] == "alerts")["status"] == "FAIL"
    db.chmod(0o644)
    rep = health_report(s, NOW, db)
    assert next(c for c in rep["checks"] if c["check"] == "permissions")["status"] == "WARN"


# -- gate checks -------------------------------------------------------------------------------------------


def test_secret_scan_finds_values_and_patterns_without_printing_them(tmp_path):
    from app.ops.gate import secret_scan

    (tmp_path / "ok.py").write_text("api_key = load()  # fine\n")
    assert secret_scan(tmp_path, ["REAL-SECRET-VALUE-1"])["status"] == "PASS"
    (tmp_path / "logs.log").write_text("login used REAL-SECRET-VALUE-1\n")
    (tmp_path / "cfg.txt").write_text(
        "NSE_BROKER_TOTP_SECRET=ABCDEFGHIJKLMNOP\n")  # gate: allow-secret (fake, planted)
    (tmp_path / ".env").write_text(
        "NSE_BROKER_TOTP_SECRET=REAL-SECRET-VALUE-1\n")  # gate: allow-secret (fake)
    r = secret_scan(tmp_path, ["REAL-SECRET-VALUE-1"])
    assert r["status"] == "FAIL" and r["blocking"]
    assert "logs.log" in r["detail"] and "cfg.txt" in r["detail"] and ".env:" not in r["detail"]
    assert "REAL-SECRET-VALUE-1" not in r["detail"]


def test_optional_tool_missing_is_skipped_not_passed(monkeypatch):
    from app.ops import gate

    monkeypatch.setattr(gate, "_tool", lambda name: None)
    r = gate.check_optional_tool("lint", "ruff", ["check"])
    assert r["status"] == "SKIPPED" and "NOT run" in r["detail"]


def test_gate_verdict_rules(monkeypatch):
    from app.ops import gate

    def fake(name, status, blocking=True):
        return {"check": name, "status": status, "detail": "", "blocking": blocking}

    for attr in ("check_compile", "secret_scan", "check_db_migration", "check_docs"):
        monkeypatch.setattr(gate, attr, lambda *a, n=attr: fake(n, "PASS"))
    monkeypatch.setattr(gate, "check_config", lambda: [fake("config", "PASS")])
    monkeypatch.setattr(gate, "check_dependencies", lambda: [fake("pip_check", "PASS")])
    monkeypatch.setattr(gate, "check_optional_tool",
                        lambda n, *a: fake(n, "SKIPPED", blocking=False))
    monkeypatch.setattr(gate, "check_pytest", lambda n, a, skip: fake(n, "SKIPPED" if skip
                                                                      else "PASS"))
    assert gate.run_gate(skip_tests=False)["verdict"].startswith("RELEASABLE")
    assert gate.run_gate(skip_tests=True)["verdict"].startswith("NOT RELEASABLE")
    monkeypatch.setattr(gate, "secret_scan", lambda: fake("secret_scan", "FAIL"))
    assert gate.run_gate(skip_tests=False)["verdict"].startswith("BLOCKED")


def test_harden_strips_group_and_other_access(tmp_path, monkeypatch):
    from app.ops.__main__ import harden

    data = tmp_path / "data"
    (data / "paper").mkdir(parents=True)
    db = data / "paper" / "paper.db"
    db.write_text("x")
    db.chmod(0o644)
    frozen = data / "paper" / "frozen.json"
    frozen.write_text("{}")
    frozen.chmod(0o444)
    monkeypatch.setattr("app.ops.__main__.PROJECT_ROOT", tmp_path)
    s = Settings(paper={"db_path": str(db)})
    harden(s)
    assert mode(db) == 0o600 and mode(frozen) == 0o400 and mode(data) == 0o700


def test_dashboard_operations_view(calendar, tmp_path, monkeypatch):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    run(engine, DAY)
    store.close()
    monkeypatch.setenv("NSE_PAPER_DB", str(tmp_path / "paper.db"))
    root = Path(__file__).resolve().parent.parent
    at = AppTest.from_file(str(root / "app" / "dashboard.py"), default_timeout=30).run()
    at.sidebar.radio[0].set_value("Operations").run()
    assert not at.exception
    assert any("Operations health" in s.value for s in at.subheader)


def test_health_fails_when_live_provider_has_no_universe(tmp_path):
    """Phase 9 fix: an empty universe means no live session can run."""
    uni = tmp_path / "universe.csv"
    uni.write_text("symbol,name\n")
    s = Settings(data={"provider": "groww", "universe_file": str(uni)})
    rep = health_report(s, NOW, tmp_path / "none.db")
    cfg = next(c for c in rep["checks"] if c["check"] == "config")
    assert cfg["status"] == "FAIL" and "lists no symbols" in cfg["detail"]
    assert rep["status"] == "UNHEALTHY"
    uni.write_text("symbol,name\nSBIN,State Bank of India\n")
    cfg = next(c for c in health_report(s, NOW, tmp_path / "none.db")["checks"]
               if c["check"] == "config")
    assert "lists no symbols" not in cfg["detail"]
