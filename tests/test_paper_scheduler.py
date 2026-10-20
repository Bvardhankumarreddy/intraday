from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import MarketConfig
from app.paper.models import SessionState
from app.paper.scheduler import AlreadyRunning, SessionScheduler, instance_lock, replay_session
from app.utils.market_calendar import MarketCalendar
from tests.paper_helpers import NEUTRAL, TARGET, build

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2025, 3, 12)


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=max(seconds, 0))


def test_instance_lock_prevents_second_scheduler(tmp_path):
    lock = tmp_path / "x.lock"
    with instance_lock(lock):
        with pytest.raises(AlreadyRunning):
            with instance_lock(lock):
                pass
    with instance_lock(lock):  # released afterwards
        pass


def test_scheduler_runs_full_session(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    clock = FakeClock(datetime(2025, 3, 12, 8, 55, tzinfo=IST))
    final = SessionScheduler(engine, clock=clock, sleep=clock.sleep).run()
    assert final is SessionState.CLOSED
    assert store.positions()[0]["net_pnl"] == "15.69"
    messages = [e["message"] for e in store.events(DAY)]
    assert any("scheduler running" in m for m in messages)
    assert any("scheduler finished: CLOSED" in m for m in messages)
    assert store.report(DAY) is not None


def test_scheduler_skips_holidays_and_weekends(calendar, tmp_path):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL])
    for day in (datetime(2025, 3, 14, 9, 0, tzinfo=IST), datetime(2025, 3, 15, 9, 0, tzinfo=IST)):
        clock = FakeClock(day)
        assert SessionScheduler(engine, clock=clock, sleep=clock.sleep).run() is None
    assert store.session(date(2025, 3, 14)) is None
    assert any("not a trading day" in e["message"] for e in store.events())


def test_scheduler_refuses_uncovered_holiday_year(tmp_path):
    cal = MarketCalendar(MarketConfig(), holidays={date(2026, 1, 26): "x"})
    engine, _, store = build(cal, tmp_path, [NEUTRAL])
    clock = FakeClock(datetime(2025, 3, 12, 9, 0, tzinfo=IST))
    assert SessionScheduler(engine, clock=clock, sleep=clock.sleep).run() is None
    assert any("does not cover 2025" in e["message"] for e in store.events())


def test_scheduler_restart_mid_session_resumes(calendar, tmp_path):
    db = tmp_path / "s.db"
    engine, provider, store = build(calendar, tmp_path, [NEUTRAL, TARGET], db=db)
    replay_until = datetime(2025, 3, 12, 9, 41, tzinfo=IST)
    open_dt, _ = calendar.session_bounds(DAY)
    engine.open_session(DAY, open_dt - timedelta(minutes=15))
    for minute in range(20, 45, 5):
        when = datetime(2025, 3, 12, 9, minute, 1, tzinfo=IST)
        if when <= replay_until:
            engine.step(when)
    store.close()
    engine2, _, store2 = build(calendar, tmp_path, [NEUTRAL, TARGET], db=db)
    clock = FakeClock(datetime(2025, 3, 12, 9, 43, tzinfo=IST))
    assert SessionScheduler(engine2, clock=clock, sleep=clock.sleep).run() is SessionState.CLOSED
    assert len(store2.orders(DAY)) == 2 and store2.positions()[0]["net_pnl"] == "15.69"


def test_scheduler_stops_after_bounded_failures(calendar, tmp_path, monkeypatch):
    engine, _, store = build(calendar, tmp_path, [NEUTRAL], paper={"max_consecutive_failures": 3})

    def broken(now):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(engine, "step", broken)
    clock = FakeClock(datetime(2025, 3, 12, 9, 0, tzinfo=IST))
    with pytest.raises(RuntimeError):
        SessionScheduler(engine, clock=clock, sleep=clock.sleep).run()
    errors = [e["message"] for e in store.events(DAY) if e["level"] == "ERROR"]
    assert sum("step failed" in m for m in errors) == 3
    assert any("too many consecutive failures" in m for m in errors)


def test_replay_matches_scheduler(calendar, tmp_path):
    e1, _, s1 = build(calendar, tmp_path / "a", [NEUTRAL, TARGET])
    replay_session(e1, DAY)
    assert s1.session(DAY)["state"] == "CLOSED" and s1.positions()[0]["net_pnl"] == "15.69"


def test_dashboard_renders_paper_labels(calendar, tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    engine, _, store = build(calendar, tmp_path, [NEUTRAL, TARGET])
    replay_session(engine, DAY)
    store.close()
    monkeypatch.setenv("NSE_PAPER_DB", str(tmp_path / "paper.db"))
    import app

    path = str(__import__("pathlib").Path(app.__file__).parent / "dashboard.py")
    at = AppTest.from_file(path, default_timeout=30).run()
    assert not at.exception
    assert any("PAPER TRADING - SIMULATED" in e.value for e in at.error)
    assert any("HISTORICAL" in w.value for w in at.warning)  # non-live data is called out


def test_no_live_order_code_anywhere():
    import pathlib

    import app as pkg
    root = pathlib.Path(pkg.__file__).parent
    for path in root.rglob("*.py"):
        text = path.read_text().lower()
        for word in ("place_order", "modify_order", "cancel_order", "orders.place"):
            assert word not in text, f"{word} in {path}"


def test_dashboard_runs_from_any_working_directory(tmp_path):
    """Regression: Streamlit puts only app/ on sys.path, so `import app` failed
    when run as `streamlit run app/dashboard.py`. Run it in a fresh process
    from another directory, exactly as a user would."""
    import pathlib
    import subprocess
    import sys

    import app

    dashboard = pathlib.Path(app.__file__).parent / "dashboard.py"
    code = ("from streamlit.testing.v1 import AppTest\n"
            f"at = AppTest.from_file({str(dashboard)!r}, default_timeout=60).run()\n"
            "print('EXC', len(at.exception))\n")
    env = {"PATH": "/usr/bin:/bin", "NSE_ENV_FILE": str(tmp_path / "no.env"),
           "NSE_PAPER_DB": str(tmp_path / "none.db"), "HOME": str(tmp_path)}
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=120)
    assert "EXC 0" in out.stdout, out.stdout + out.stderr
