"""Phase 8 failure-injection tests (spec section 12, scenarios 1-20).

Every failure is injected with fakes, monkeypatches or isolated temporary
databases. No real provider, broker or production database is touched; all
market data is synthetic. Each test checks the resulting STATE as well as the
error: financial records preserved, no duplicates, correct health/alert
status, and safe recovery.

Reference (tests/paper_helpers.py): SYNTH signal at 09:40, entry 71.05,
target 72.20 hit by the 09:45 candle, 28 shares on Rs 2,000 -> net 15.69.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import types
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.data.groww_provider import (CircuitState, ConnectionState, GrowwMarketDataProvider,
                                     GrowwSdkClient)
from app.data.models import DataQualityError, DataUnavailableError, Instrument
from app.ops.alerts import AlertManager
from app.ops.health import health_report
from app.ops.reconciliation import full_reconciliation, write_report
from app.paper.engine import PaperEngine, PaperStateError
from app.paper.models import InvalidTransition, OrderState, PositionState, SessionState, order_id
from app.paper.scheduler import SessionScheduler
from app.paper.store import PaperStore, StoreError
from tests.ops_helpers import FlakyProvider, fake_growwapi_module
from tests.paper_helpers import GAP_DOWN, NEUTRAL, TARGET, build, run, steps
from tests.test_paper_engine import second_symbol

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2025, 3, 12)


def t(hhmm: str, sec: int = 1) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(2025, 3, 12, h, m, sec, tzinfo=IST)


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=max(seconds, 0))


class Mono:
    """Controllable monotonic clock for the circuit breaker."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def exc(name: str, msg: str = "") -> Exception:
    return type(name, (Exception,), {})(msg)


class Client:
    """Fake GrowwDataClient: scripted failures, then documented-format data."""

    def __init__(self, failures=(), quote=None):
        self.failures = list(failures)
        self.calls = 0
        self._quote = quote

    def quote(self, symbol):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self._quote

    def candles(self, symbol, start, end, minutes):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return {"candles": []}


def provider(client, calendar, mono=None, clock=None, **data):
    from app.config import Settings

    s = Settings(data={"min_seconds_between_calls": 0, "retry_jitter_fraction": 0,
                       "circuit_failure_threshold": 3, "circuit_cooldown_seconds": 60, **data})
    return GrowwMarketDataProvider(client, calendar, [Instrument("ABC", "ABC")], s,
                                   sleep=lambda _: None, monotonic=mono or Mono(),
                                   clock=clock or (lambda: t("09:40")))


def good_quote():
    ts = int(t("09:40", 0).timestamp())
    return {"last_price": 71.1, "bid_price": 71.05, "bid_quantity": 300, "offer_price": 71.15,
            "offer_quantity": 250, "last_trade_time": ts}


def snapshot_financials(store: PaperStore) -> dict:
    return {t: [dict(r) for r in store.all(f"SELECT * FROM {t} ORDER BY 1")]
            for t in ("portfolio", "signals", "orders", "positions", "reports")}


def open_alerts(store, settings):
    return {a["alert_key"]: a for a in AlertManager(store, settings).open_alerts()}


# 1 --------------------------------------------------------------------------------------


def test_01_provider_timeout_is_bounded_and_blocks_signals(calendar, tmp_path):
    client = Client(failures=[exc("GrowwAPITimeoutException", "read timed out")] * 10)
    p = provider(client, calendar)
    with pytest.raises(DataUnavailableError, match="after 4 attempt"):
        p.get_quote("ABC")
    assert client.calls == 4 and p.state is ConnectionState.DISCONNECTED  # bounded
    assert p.health()["consecutive_failures"] == 1

    # Engine level: the provider times out for every symbol -> no signal, no
    # fabricated candle, signals blocked, heartbeat + alert say so.
    engine, base, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    flaky = FlakyProvider(base, "Groww candles failed after 4 attempt(s): timeout")
    engine.provider = flaky
    run(engine, DAY, until="09:35")
    flaky.failing = True
    result = engine.step(t("09:40"))
    assert result.signals_blocked and store.signals(DAY) == [] and store.orders(DAY) == []
    beat = store.get_meta("heartbeat")
    assert beat["signals_blocked"] and beat["symbols_ok"] == 0
    assert open_alerts(store, engine.settings)["data.blocked"]["severity"] == "ERROR"


# 2 --------------------------------------------------------------------------------------


def test_02_repeated_rate_limits_open_the_circuit_then_recover(calendar):
    mono = Mono()
    limited = [exc("GrowwAPIRateLimitException", "429 too many requests")] * 12
    client = Client(failures=limited, quote=good_quote())
    p = provider(client, calendar, mono, retry_max_attempts=2)
    for _ in range(3):  # 3 failed calls x 2 attempts
        with pytest.raises(DataUnavailableError):
            p.get_quote("ABC")
    assert p.circuit is CircuitState.OPEN and client.calls == 6
    with pytest.raises(DataUnavailableError, match="circuit OPEN"):
        p.get_quote("ABC")
    assert client.calls == 6 and p.calls_failed_fast == 1  # failed fast: no network call
    mono.t += 61  # cooldown over -> one probe; still limited -> OPEN again
    with pytest.raises(DataUnavailableError):
        p.get_quote("ABC")
    assert client.calls == 7 and p.circuit is CircuitState.OPEN
    client.failures.clear()
    mono.t += 61
    assert p.get_quote("ABC").ask == 71.15
    assert p.circuit is CircuitState.CLOSED and p.state is ConnectionState.CONNECTED
    # opened twice: after 3 failures, and again when the first probe failed
    assert p.health()["circuit_opened"] == 2 and p.health()["outages_recovered"] == 1


# 3 --------------------------------------------------------------------------------------


def test_03_connection_loss_and_recovery(calendar, tmp_path):
    clock_values = iter([t("09:40"), t("09:40", 30)])
    client = Client(failures=[ConnectionError("reset")] * 4, quote=good_quote())
    p = provider(client, calendar, clock=lambda: next(clock_values, t("09:40", 30)))
    with pytest.raises(DataUnavailableError):
        p.get_quote("ABC")
    assert p.health()["outage_started"] is not None
    assert p.get_quote("ABC").last_price == 71.1
    h = p.health()
    assert h["state"] == "CONNECTED" and h["outage_started"] is None
    assert h["last_recovery_seconds"] == 29 and h["retries"] == 3  # 09:40:01 -> 09:40:30

    engine, base, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    flaky = FlakyProvider(base)
    engine.provider = flaky
    open_dt, _ = calendar.session_bounds(DAY)
    engine.open_session(DAY, open_dt - timedelta(minutes=15))
    flaky.failing = True
    for when in steps(engine, DAY, until="09:35"):
        assert engine.step(when).signals_blocked
    assert "data.blocked" in open_alerts(store, engine.settings)
    flaky.failing = False  # connection restored
    for when in steps(engine, DAY):
        if when > t("09:35", 59):
            engine.step(when)
    assert "data.blocked" not in open_alerts(store, engine.settings)  # resolved
    assert len(store.signals(DAY)) == 1 and len(store.orders(DAY)) == 2  # BUY + SELL once
    assert store.positions()[0]["net_pnl"] == "15.69"


# 4 --------------------------------------------------------------------------------------


def test_04_stale_and_out_of_order_data_rejected(calendar, tmp_path):
    from tests.helpers import orb_today
    from tests.backtest_helpers import pad

    epoch = int(t("09:15", 0).timestamp())
    rows = [[epoch + 300 * i, 70, 70.3, 69.9, 70.1, 1000 * (i + 1)] for i in range(4)]
    rows[1], rows[2] = rows[2], rows[1]  # out of order

    class C(Client):
        def candles(self, *a):
            return {"candles": rows}

    with pytest.raises(DataQualityError):  # the whole batch is rejected, nothing partial
        provider(C(), calendar).get_candles("ABC", "5m", DAY, DAY)

    engine, _, store = build(calendar, tmp_path, days={DAY: pad(orb_today()[:3], n=3)},
                             backtest={"max_missing_candles_per_session": 75})
    run(engine, DAY, until="10:30")
    assert engine.step(t("10:30")).signals_blocked and store.signals(DAY) == []
    assert store.get_meta("heartbeat")["signals_blocked"] is True


# 5 --------------------------------------------------------------------------------------


def test_05_database_unavailable_at_startup(tmp_path, monkeypatch):
    with pytest.raises(StoreError, match="is a directory"):
        PaperStore(tmp_path)
    locked = tmp_path / "ro"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(StoreError, match="unavailable|unreadable"):
            PaperStore(locked / "sub" / "paper.db")
    finally:
        locked.chmod(0o700)
    from app.config import Settings

    rep = health_report(Settings(), t("10:00"), db_path=tmp_path)  # a directory, not a DB
    assert rep["status"] == "UNHEALTHY"
    assert next(c for c in rep["checks"] if c["check"] == "database")["status"] == "FAIL"


# 6 --------------------------------------------------------------------------------------


def test_06_database_failure_during_exit_rolls_back_then_completes_once(calendar, tmp_path,
                                                                         monkeypatch):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    run(engine, DAY, until="09:45")
    before = snapshot_financials(store)
    assert store.positions()[0]["state"] == "OPEN"
    real = store.transition_position

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "transition_position", locked)
    with pytest.raises(sqlite3.OperationalError):
        engine.step(t("09:50"))
    assert snapshot_financials(store) == before  # SELL order, cash, position: all rolled back
    monkeypatch.setattr(store, "transition_position", real)
    engine.step(t("09:50"))
    p = store.positions()[0]
    assert p["state"] == "CLOSED" and p["net_pnl"] == "15.69"
    assert list(json.loads(p["costs"])) == ["buy", "sell"]  # fees once each
    assert full_reconciliation(store, engine.settings, engine.risk.schedule, DAY)["status"] == "OK"


def test_06b_scheduler_step_failures_alert_and_abort_cleanly(calendar, tmp_path, monkeypatch):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL], paper={"max_consecutive_failures": 2})
    monkeypatch.setattr(engine, "step", lambda now: (_ for _ in ()).throw(
        sqlite3.OperationalError("disk I/O error")))
    clock = FakeClock(datetime(2025, 3, 12, 9, 0, tzinfo=IST))
    with pytest.raises(sqlite3.OperationalError):
        SessionScheduler(engine, clock=clock, sleep=clock.sleep).run()
    assert store.get_meta("scheduler")["state"] == "ABORTED"
    alerts = open_alerts(store, engine.settings)
    assert alerts["scheduler.aborted"]["severity"] == "CRITICAL"
    rep = health_report(engine.settings, t("10:00"), db_path=store.path)
    assert next(c for c in rep["checks"] if c["check"] == "session")["status"] == "FAIL"


# 7 / 8 ----------------------------------------------------------------------------------


@pytest.mark.parametrize("crash_point", ["after_signal_insert", "after_order_create"])
def test_07_08_crash_inside_signal_and_order_transaction(calendar, tmp_path, monkeypatch,
                                                         crash_point):
    db = tmp_path / "c.db"
    engine, provider_, store = build(calendar, tmp_path, [NEUTRAL, TARGET], db=db)
    run(engine, DAY, until="09:35")
    target = "create_order" if crash_point == "after_signal_insert" else "transition_order"

    def crash(*a, **k):
        raise KeyboardInterrupt("process killed")  # not an Exception: like a hard stop

    monkeypatch.setattr(store, target, crash)
    with pytest.raises(KeyboardInterrupt):
        engine.step(t("09:40"))
    store.close()
    # Restart: nothing half-written survived the crash.
    store2 = PaperStore(db)
    assert store2.signals(DAY) == [] and store2.orders(DAY) == []
    assert Decimal(store2.portfolio()["reserved"]) == 0
    engine2 = PaperEngine(provider_, engine.settings, calendar, store2)
    engine2.open_session(DAY, t("09:41"))
    for when in steps(engine2, DAY):
        if when >= t("09:40"):
            engine2.step(when)
    assert len(store2.signals(DAY)) == 1 and len(store2.orders(DAY)) == 2
    assert store2.positions()[0]["net_pnl"] == "15.69"


def test_07b_crash_after_committed_signal_and_order(calendar, tmp_path):
    db = tmp_path / "c.db"
    engine, provider_, store = build(calendar, tmp_path, [NEUTRAL, TARGET], db=db)
    run(engine, DAY, until="09:40")
    assert store.orders(DAY)[0]["state"] == "PENDING"
    store.close()  # crash after commit: signal + PENDING order + reserved cash persisted
    store2 = PaperStore(db)
    engine2 = PaperEngine(provider_, engine.settings, calendar, store2)
    engine2.open_session(DAY, t("09:42"))
    for when in steps(engine2, DAY):
        if when > t("09:40", 59):
            engine2.step(when)
    assert len(store2.orders(DAY)) == 2 and len(store2.positions()) == 1
    assert store2.positions()[0]["net_pnl"] == "15.69"


# 9 --------------------------------------------------------------------------------------


def test_09_crash_during_simulated_fill(calendar, tmp_path, monkeypatch):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    run(engine, DAY, until="09:40")
    before = snapshot_financials(store)
    real_event = store.event

    def crash_on_fill(now, day, kind, *a, **k):
        if kind == "FILL":
            raise KeyboardInterrupt("killed while recording the fill")
        return real_event(now, day, kind, *a, **k)

    monkeypatch.setattr(store, "event", crash_on_fill)
    with pytest.raises(KeyboardInterrupt):
        engine.step(t("09:45"))
    assert snapshot_financials(store) == before  # cash, order, position untouched
    monkeypatch.setattr(store, "event", real_event)
    engine.step(t("09:45"))
    engine.step(t("09:45", 30))  # duplicate step after recovery
    p = store.positions()[0]
    fills = [e for e in store.events(DAY) if e["kind"] == "FILL" and "BUY" in e["message"]]
    assert len(fills) == 1 and p["buy_costs"] == json.loads(p["costs"])["buy"]["total"]
    engine.verify_ledger(t("09:46"))


# 10 -------------------------------------------------------------------------------------


def test_10_shutdown_preserves_open_position_and_restart_resumes(calendar, tmp_path):
    db = tmp_path / "s.db"
    engine, provider_, store = build(calendar, tmp_path, [NEUTRAL, TARGET], db=db)
    clock = FakeClock(datetime(2025, 3, 12, 9, 0, tzinfo=IST))
    sched = SessionScheduler(engine, clock=clock, sleep=clock.sleep)

    def stop_when_open(now):
        if store.positions(states=(PositionState.OPEN,)) and now >= t("09:50", 0):
            sched.request_stop("SIGTERM received")

    sched.before_step = stop_when_open
    assert sched.run() is None  # interrupted
    p = store.positions()[0]
    assert p["state"] == "OPEN" and p["exit_price"] is None  # NOT closed by shutdown
    assert store.get_meta("scheduler")["state"] == "INTERRUPTED"
    assert any("INTERRUPTED" in e["message"] for e in store.events(DAY))
    assert any(k.startswith("scheduler.interrupted") for k in open_alerts(store, engine.settings))
    store.close()

    store2 = PaperStore(db)
    engine2 = PaperEngine(provider_, engine.settings, calendar, store2)
    clock2 = FakeClock(clock.now)
    assert SessionScheduler(engine2, clock=clock2, sleep=clock2.sleep).run() is \
        SessionState.CLOSED
    p = store2.positions()[0]
    assert (p["state"], p["exit_reason"], p["net_pnl"]) == ("CLOSED", "TARGET", "15.69")


# 11 -------------------------------------------------------------------------------------


def test_11_duplicate_signals_and_fill_events(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    run(engine, DAY, until="09:45")
    for _ in range(3):  # the same steps delivered again (duplicate events)
        engine.step(t("09:40"))
        engine.step(t("09:45"))
    assert len(store.signals(DAY)) == 1 and len(store.orders(DAY)) == 1
    pos = store.positions()[0]
    oid = order_id(pos["signal_id"], "BUY")
    with pytest.raises(InvalidTransition):  # a second fill of the same order is impossible
        with store.transaction():
            store.transition_order(oid, OrderState.FILLED, t("09:46"))
    with pytest.raises(sqlite3.IntegrityError):  # and so is a second position
        with store.transaction():
            store.create_position({k: pos[k] for k in pos.keys() if k != "state"})
    rec = full_reconciliation(store, engine.settings, engine.risk.schedule, DAY, t("09:46"))
    assert rec["counts"]["CRITICAL"] == 0 and rec["counts"]["ERROR"] == 0


# 12 -------------------------------------------------------------------------------------


def test_12_corrupted_database_file_is_refused(tmp_path):
    bad = tmp_path / "paper.db"
    bad.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(StoreError, match="corrupt|unreadable"):
        PaperStore(bad)
    assert bad.read_bytes().startswith(b"this is not")  # never recreated or overwritten


def test_12b_newer_schema_is_refused(tmp_path):
    store = PaperStore(tmp_path / "p.db")
    store.conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    store.close()
    with pytest.raises(StoreError, match="newer than this code"):
        PaperStore(tmp_path / "p.db")


def test_12c_inconsistent_state_refuses_to_resume_without_repair(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    run(engine, DAY)
    with store.transaction():  # an orphan FILLED BUY with no position (corruption)
        store.conn.execute(
            "INSERT INTO orders (order_id, signal_id, position_id, symbol, exchange, side, kind, "
            "quantity, state, created_at, reference_price, execution_model_version, updated_at, "
            "fill_price, fill_time, fill_quantity) VALUES ('PAPER-orphan', 'sig-x', 'POS-x', "
            "'SYNTH', 'NSE', 'BUY', 'MARKET_ENTRY', 5, 'FILLED', '2025-03-12T10:00:00+05:30', "
            "'70', 'paper-v1', '2025-03-12T10:00:00+05:30', '70', "
            "'2025-03-12T10:00:00+05:30', 5)")
    before = snapshot_financials(store)
    with pytest.raises(PaperStateError, match="orphan_buy_fill"):
        engine.open_session(DAY, t("15:45"))
    assert snapshot_financials(store) == before  # nothing repaired or deleted
    assert open_alerts(store, engine.settings)["state.inconsistent"]["severity"] == "CRITICAL"


# 13 -------------------------------------------------------------------------------------


def test_13_missing_or_invalid_configuration(tmp_path, monkeypatch, calendar):
    from app.config import Settings, load_settings
    from app.data.factory import build_provider

    cfg = tmp_path / "settings.yaml"
    cfg.write_text("risk:\n  max_daily_los: 5\n")  # typo
    with pytest.raises(ValidationError, match="max_daily_los"):
        load_settings(cfg)
    cfg.write_text("risk:\n  risk_pct: -1\n")
    with pytest.raises(ValidationError):
        load_settings(cfg)
    cfg.write_text("broker_api_key: abc\n")  # secrets never from YAML
    with pytest.raises(ValueError, match="environment"):
        load_settings(cfg)
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "missing.yaml")
    monkeypatch.setenv("NSE_RISK__MAX_DAILY_LOS", "5")  # typo via environment
    with pytest.raises(ValidationError):
        load_settings()
    monkeypatch.delenv("NSE_RISK__MAX_DAILY_LOS")
    with pytest.raises(DataUnavailableError, match="NSE_BROKER_API_KEY"):
        build_provider(Settings(data={"provider": "groww"}), calendar)

    from app.ops.__main__ import main

    monkeypatch.setenv("NSE_EXECUTION_MODE", "live")
    assert main(["health"]) == 2  # explicit, clean failure


# 14 -------------------------------------------------------------------------------------


def test_14_write_failure_keeps_previous_report_and_records(calendar, tmp_path, monkeypatch):
    from pathlib import Path

    from app.evaluation.recorder import EvaluationRecorder
    from app.evaluation.report import write_daily

    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET],
                             evaluation={"output_dir": str(tmp_path / "eval")})
    recorder = EvaluationRecorder(store, engine.settings, calendar)
    eid = recorder.create_evaluation("t", DAY, t("08:00"))
    run(engine, DAY)
    first = write_daily(store, DAY, engine.settings, eid)
    original = first.read_text()
    financial = snapshot_financials(store)
    real = Path.write_text

    def disk_full(self, *a, **k):
        if self.name.startswith("."):
            raise OSError(28, "No space left on device")
        return real(self, *a, **k)

    with store.transaction():  # make the next report differ
        store.event(t("15:40"), DAY, "OPS", "INFO", "late note")
    monkeypatch.setattr(Path, "write_text", disk_full)
    monkeypatch.setattr("app.evaluation.report.daily_report",
                        lambda *a, **k: {"changed": True})
    with pytest.raises(OSError, match="No space"):
        write_daily(store, DAY, engine.settings, eid)
    assert first.read_text() == original  # atomic write: the old report is intact
    assert snapshot_financials(store) == financial

    monkeypatch.setattr("app.ops.health.free_disk_mb", lambda p: 10.0)
    rep = health_report(engine.settings, t("16:00"), db_path=store.path)
    assert next(c for c in rep["checks"] if c["check"] == "disk")["status"] == "FAIL"


# 15 -------------------------------------------------------------------------------------


def test_15_failed_daily_report_is_visible_and_recoverable(calendar, tmp_path, monkeypatch):
    from app.paper.report import regenerate_report

    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    monkeypatch.setattr("app.paper.report.build_report",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("template broke")))
    with pytest.raises(ValueError):
        run(engine, DAY)
    assert store.session(DAY)["state"] == "CLOSED"  # state preserved
    assert store.positions()[0]["net_pnl"] == "15.69" and store.report(DAY) is None
    assert f"report.failed.{DAY}" in open_alerts(store, engine.settings)
    rep = health_report(engine.settings, t("16:00"), db_path=store.path)
    assert next(c for c in rep["checks"] if c["check"] == "reports")["status"] == "WARN"
    monkeypatch.undo()
    regenerate_report(store, DAY, engine.settings, t("16:05"))
    assert store.report(DAY)["totals"]["net_pnl"] == "15.69"
    assert f"report.failed.{DAY}" not in open_alerts(store, engine.settings)
    rep = health_report(engine.settings, t("16:10"), db_path=store.path)
    assert next(c for c in rep["checks"] if c["check"] == "reports")["status"] == "PASS"


# 16 -------------------------------------------------------------------------------------


def test_16_notification_provider_failure_never_breaks_the_engine(calendar, tmp_path):
    class Broken:
        name = "webhook"

        def send(self, alert):
            raise ConnectionError("POST https://hooks.example/x?token=abcdef123456 failed")

    engine, base, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    engine.notifier = Broken()
    flaky = FlakyProvider(base)
    engine.provider = flaky
    run(engine, DAY, until="09:30")
    flaky.failing = True
    assert engine.step(t("09:35")).signals_blocked  # the step completed despite the notifier
    a = open_alerts(store, engine.settings)["data.blocked"]
    assert a["notify_status"] == "FAILED" and "abcdef123456" not in a["notify_error"]


# 17 -------------------------------------------------------------------------------------


def test_17_invalid_or_expired_credentials(calendar, tmp_path, monkeypatch):
    secret = "TOTALLY-SECRET-VALUE-123"  # gate: allow-secret (fake value)
    module, _ = fake_growwapi_module(None, login_error=exc(
        "GrowwAPIAuthenticationException", f"invalid api key {secret}"))
    monkeypatch.setitem(sys.modules, "growwapi", module)
    with pytest.raises(DataUnavailableError) as info:
        GrowwSdkClient("key", api_secret=secret)
    assert secret not in str(info.value) and info.value.__cause__ is None
    assert "GrowwAPIAuthenticationException" in str(info.value)

    # Expired session during a call -> relogin fails -> clean, counted failure.
    client = Client(failures=[exc("GrowwAPIAuthenticationException", "token expired")] * 5)
    p = provider(client, calendar)
    with pytest.raises(DataUnavailableError):
        p.get_quote("ABC")
    assert client.calls == 1 and p.consecutive_failures == 1  # not retried blindly

    from app.paper.scheduler import provider_or_fail

    store = PaperStore(tmp_path / "p.db")
    monkeypatch.setattr("app.data.factory.build_provider", lambda *a: (_ for _ in ()).throw(
        DataUnavailableError("Groww login failed (GrowwAPIAuthenticationException)")))
    from app.config import Settings

    assert provider_or_fail(Settings(), calendar, store) is None
    assert open_alerts(store, Settings())["provider.unavailable"]["severity"] == "CRITICAL"


# 18 -------------------------------------------------------------------------------------


def test_18_reconciliation_mismatch_detected_never_altered(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    run(engine, DAY)
    with store.transaction():
        store.conn.execute("UPDATE positions SET net_pnl = '99.00'")  # tampered record
    before = snapshot_financials(store)
    rec = full_reconciliation(store, engine.settings, engine.risk.schedule, DAY, t("16:00"))
    codes = {(f["code"], f["severity"]) for f in rec["findings"]}
    assert ("cash_mismatch", "CRITICAL") in codes and ("net_mismatch", "ERROR") in codes
    assert rec["status"] == "FAIL"
    assert snapshot_financials(store) == before  # detection only
    a = write_report(rec, tmp_path / "rec", t("16:00"))
    b = write_report(rec, tmp_path / "rec", t("16:00"))
    assert a != b and a.exists() and b.exists()  # never overwritten
    with pytest.raises(PaperStateError):
        engine.open_session(date(2025, 3, 13), datetime(2025, 3, 13, 9, 0, tzinfo=IST))


# 19 -------------------------------------------------------------------------------------


def test_19_daily_loss_lock_survives_restart(calendar, tmp_path):
    db = tmp_path / "lock.db"
    kw = dict(extra={"SECOND": {DAY: second_symbol()}}, risk={"max_daily_loss": 40}, db=db)
    engine, provider_, store = build(calendar, tmp_path, [NEUTRAL, GAP_DOWN], **kw)
    run(engine, DAY, until="10:00")
    assert store.session(DAY)["locked"] == 1
    store.close()
    store2 = PaperStore(db)
    engine2 = PaperEngine(provider_, engine.settings, calendar, store2)
    engine2.open_session(DAY, t("10:01"))
    assert engine2._risk_state(DAY).locked
    for when in steps(engine2, DAY):
        if when > t("10:00", 59):
            engine2.step(when)
    second = [s for s in store2.signals(DAY) if s["symbol"] == "SECOND"][0]
    assert second["review_status"] == "REJECTED_RISK_LIMIT"
    assert "daily_loss_lock" in second["reasons"]
    assert len(store2.positions()) == 1  # no new paper entry after the lock


# 20 -------------------------------------------------------------------------------------


def test_20_no_order_endpoint_is_called(calendar, tmp_path, monkeypatch):
    """Failure paths included: outages, auth errors and retries never reach an
    order/account method of the (fake) SDK."""
    from tests.backtest_helpers import make_dataset, scenario
    from tests.paper_helpers import paper_settings

    s = paper_settings(tmp_path)
    ds = make_dataset(calendar, s, {DAY: scenario([NEUTRAL, TARGET])})
    module, record = fake_growwapi_module(ds)
    monkeypatch.setitem(sys.modules, "growwapi", module)
    client = GrowwSdkClient("token", api_secret="x" * 8)
    p = GrowwMarketDataProvider(client, calendar, [Instrument("SYNTH", "S")], s,
                                sleep=lambda _: None)
    engine = PaperEngine(p, s, calendar, PaperStore(tmp_path / "x.db"))
    run(engine, DAY, until="10:00")
    real = client._api

    def outage(*a, **k):
        raise exc("GrowwAPITimeoutException", "timeout")

    proxy = types.SimpleNamespace(get_quote=outage, get_historical_candle_data=outage)
    client._api = proxy
    for when in steps(engine, DAY):
        if when > t("10:00", 59) and when < t("10:30"):
            engine.step(when)
    client._api = real
    assert record["order_calls"] == []
