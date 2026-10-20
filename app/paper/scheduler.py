"""Session orchestration for paper trading.

- ``replay_session``: drives one session on a simulated clock (mock or
  historical data) through the exact same ``PaperEngine.step`` used live.
- ``SessionScheduler``: the live loop. It holds an exclusive lock file so two
  instances can never run against the same database. It checks the calendar
  (holidays, special sessions, year coverage), runs preflight, opens or
  recovers the session, steps shortly after every candle close, retries
  failed steps a bounded number of times, and stops once the session is
  closed and reported. Every transition is logged and stored as an event.

Phase 8:
- Graceful shutdown: ``request_stop()`` (wired to SIGTERM/SIGINT by the
  CLIs) interrupts the wait between steps. The scheduler stops before the
  next step, never in the middle of one. It records the session as
  INTERRUPTED (event, ``meta.scheduler``, and INCOMPLETE in the evaluation)
  and returns. Open paper positions and pending orders are left exactly as
  persisted: they are NOT closed because the process stops. A restart the
  same day resumes them.
- ``meta.scheduler`` holds RUNNING / INTERRUPTED / ABORTED / FINISHED, the
  consecutive failure count and the last error, for the health check.
- Step failures raise alerts (WARNING, then CRITICAL on abort). Each step
  logs with a correlation ID (``<date>T<HHMM>``).
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from app.paper.engine import PaperEngine, StepResult
from app.paper.models import SessionState
from app.utils.logging import get_logger, log_context
from app.utils.market_calendar import MarketCalendar

log = get_logger(__name__)


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def instance_lock(path: Path) -> Iterator[None]:
    """Exclusive, non-blocking lock. Released automatically if the process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning(f"another paper scheduler holds {path}") from exc
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)


def step_times(calendar: MarketCalendar, day: date, interval: timedelta,
               grace: timedelta) -> list[datetime]:
    """Every candle close of the session plus ``grace``."""
    open_dt, close_dt = calendar.session_bounds(day)
    times, t = [], open_dt + interval
    while t <= close_dt:
        times.append(t + grace)
        t += interval
    return times


def replay_session(engine: PaperEngine, day: date,
                   before_step: Callable[[datetime], None] | None = None) -> list[StepResult]:
    """Run a whole session on a simulated clock. ``before_step`` lets a mock
    provider move its quote clock to the step time."""
    cal = engine.calendar
    grace = timedelta(seconds=engine.settings.paper.grace_seconds)
    open_dt, close_dt = cal.session_bounds(day)
    engine.open_session(day, open_dt - timedelta(minutes=15))
    engine.begin_evaluation_session(day, open_dt - timedelta(minutes=15))
    results = []
    for t in step_times(cal, day, engine.step_td, grace):
        if before_step:
            before_step(t)
        results.append(engine.step(t))
    engine.end_evaluation_session(day, close_dt + grace)
    return results


class SessionScheduler:
    def __init__(self, engine: PaperEngine,
                 clock: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 before_step: Callable[[datetime], None] | None = None):
        self.engine = engine
        self.cal = engine.calendar
        self.clock = clock or (lambda: datetime.now(self.cal.tz))
        self.stop_event = threading.Event()
        self.stop_reason: str | None = None
        # Default wait is interruptible by request_stop().
        self.sleep = sleep if sleep is not time.sleep else self.stop_event.wait
        self.before_step = before_step
        self.cfg = engine.settings.paper

    def request_stop(self, reason: str = "shutdown requested") -> None:
        """Stop before the next step (safe from a signal handler)."""
        self.stop_reason = reason
        self.stop_event.set()

    def _status(self, day: date, state: str, **extra) -> None:
        store = self.engine.store
        try:
            with store.transaction():
                store.set_meta("scheduler", {"state": state, "day": day, "pid": os.getpid(),
                                             "updated_at": self.clock(), **extra})
        except Exception:  # noqa: BLE001 - status is best effort, never fatal
            pass

    def _interrupted(self, day: date, evaluating: bool) -> None:
        reason = self.stop_reason or "shutdown requested"
        now = self.clock()
        open_positions = len(self.engine.store.positions(day, None))
        self._event(day, "WARNING", f"session INTERRUPTED: {reason}. Open paper positions and "
                                    "orders are preserved (not closed); restart to resume.")
        self._status(day, "INTERRUPTED", reason=reason)
        if evaluating:
            try:
                self.engine.recorder.session_incomplete(day, now, f"interrupted: {reason}",
                                                        kind=_kind(self.engine))
            except Exception:  # noqa: BLE001
                pass
        self.engine._alert(f"scheduler.interrupted.{day}", "WARNING", "scheduler",
                           f"paper session interrupted ({reason}); {open_positions} position "
                           "record(s) preserved; restart to resume", now)

    def _event(self, day: date | None, level: str, message: str) -> None:
        store = self.engine.store
        with store.transaction():
            store.event(self.clock(), day, "SESSION", level, message)
        (log.warning if level != "INFO" else log.info)(message)

    def run(self) -> SessionState | None:
        """Run today's session to completion. Returns the final state, or
        None if there is no session to run today."""
        with instance_lock(self.cfg.lock_file):
            now = self.clock()
            day = now.astimezone(self.cal.tz).date()
            if not self.cal.covers_year(day.year):
                self._event(day, "ERROR", f"holiday list does not cover {day.year}; not starting")
                return None
            if not self.cal.is_trading_day(day):
                self._event(day, "INFO", f"{day} is not a trading day "
                                         f"({self.cal.session_status(now).value}); nothing to do")
                return None
            recorder = self.engine.recorder
            evaluating = recorder is not None and recorder.active() is not None
            if evaluating:
                recorder.sync_schedule(day, now)
            problems = self.engine.preflight(day)
            if problems:
                for p in problems:
                    self._event(day, "ERROR", f"preflight: {p}")
                if evaluating and self.cal.is_regular_session(day):
                    recorder.session_failed(day, now, "preflight: " + "; ".join(problems))
                return None
            open_dt, close_dt = self.cal.session_bounds(day)
            if now >= close_dt + timedelta(minutes=30) and self.engine.store.session(day) is None:
                self._event(day, "INFO", "session already over and never started; not trading")
                return None
            state = self.engine.open_session(day, now)
            self.engine.begin_evaluation_session(day, now)
            self._event(day, "INFO", f"scheduler running ({state.value}); session "
                                     f"{open_dt:%H:%M}-{close_dt:%H:%M}")
            grace = timedelta(seconds=self.cfg.grace_seconds)
            failures = 0
            self._status(day, "RUNNING", consecutive_failures=0)
            for t in step_times(self.cal, day, self.engine.step_td, grace):
                if self.stop_event.is_set():
                    self._interrupted(day, evaluating)
                    return None
                session = self.engine.store.session(day)
                if session["state"] != SessionState.ACTIVE.value:
                    break
                now = self.clock()
                if now < t:
                    self.sleep((t - now).total_seconds())
                    if self.stop_event.is_set():
                        self._interrupted(day, evaluating)
                        return None
                elif now > t + self.engine.step_td:
                    continue  # catching up after a restart: only the latest step matters
                if self.before_step:
                    self.before_step(self.clock())
                if self.stop_event.is_set():  # last check right before the step runs
                    self._interrupted(day, evaluating)
                    return None
                try:
                    with log_context(component="paper-scheduler", session_id=str(day),
                                     correlation_id=f"{day}T{t:%H%M}", event_type="step"):
                        self.engine.step(self.clock())
                    if failures:
                        self.engine._resolve_alert("scheduler.step_failed", self.clock())
                    failures = 0
                except Exception as exc:  # noqa: BLE001 - bounded retries, then stop
                    failures += 1
                    self._event(day, "ERROR", f"step failed ({failures}/"
                                              f"{self.cfg.max_consecutive_failures}): {exc}")
                    self._status(day, "RUNNING", consecutive_failures=failures,
                                 last_error=str(exc))
                    self.engine._alert("scheduler.step_failed", "WARNING", "scheduler",
                                       f"paper step failed ({failures} in a row): {exc}",
                                       self.clock())
                    if failures >= self.cfg.max_consecutive_failures:
                        self._event(day, "ERROR", "too many consecutive failures; scheduler "
                                                  "stopping. State is persisted; restart to "
                                                  "resume.")
                        self._status(day, "ABORTED", consecutive_failures=failures,
                                     reason=str(exc))
                        self.engine._alert("scheduler.aborted", "CRITICAL", "scheduler",
                                           f"scheduler stopped after {failures} consecutive "
                                           f"failures: {exc}", self.clock())
                        if evaluating:
                            recorder.session_failed(day, self.clock(),
                                                    f"scheduler aborted after {failures} "
                                                    f"consecutive failures: {exc}",
                                                    kind=_kind(self.engine))
                        raise
            session = self.engine.store.session(day)
            if session["state"] == SessionState.ACTIVE.value:
                now = self.clock()
                if now < close_dt:
                    self.sleep((close_dt - now).total_seconds())
                    if self.stop_event.is_set():
                        self._interrupted(day, evaluating)
                        return None
                self.engine.step(max(self.clock(), close_dt))
            final = SessionState(self.engine.store.session(day)["state"])
            self.engine.end_evaluation_session(day, max(self.clock(), close_dt))
            self._event(day, "INFO", f"scheduler finished: {final.value}")
            self._status(day, "FINISHED", final=final.value)
            return final


def _kind(engine: PaperEngine):
    from app.evaluation.recorder import KIND_FOR_MODE

    return KIND_FOR_MODE[engine.provider.mode]


def run_scheduled(engine, hook, recorder=None) -> int:
    """Run today's session with graceful shutdown: SIGTERM/SIGINT stop the
    scheduler before its next step; open paper positions are preserved."""
    import signal

    from app.paper.scheduler import SessionScheduler

    scheduler = SessionScheduler(engine, before_step=hook)
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(
            sig, lambda signum, _frame: scheduler.request_stop(
                f"signal {signal.Signals(signum).name} received"))
    try:
        final = scheduler.run()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        engine.store.close()
    if scheduler.stop_event.is_set():
        print(f"Stopped: {scheduler.stop_reason}. State is persisted; positions were NOT closed. "
              "Run again to resume.")
        return 3
    print("Session result:", final.value if final else "no session today")
    return 0


def provider_or_fail(settings, calendar, store, recorder=None):
    """Build the configured provider. A credential or connection failure is
    reported cleanly (no secrets), recorded as a failed session and alerted."""
    from datetime import datetime

    from app.data.factory import build_provider
    from app.data.models import DataError
    from app.ops.alerts import safe_alert

    try:
        return build_provider(settings, calendar)
    except DataError as exc:
        now = datetime.now(calendar.tz)
        msg = f"market-data provider unavailable: {exc}"
        print(f"ERROR: {msg}")
        safe_alert(store, settings, "provider.unavailable", "CRITICAL", "market-data", msg, now)
        if recorder is not None and recorder.active() is not None and \
                calendar.is_regular_session(now.date()):
            recorder.session_failed(now.date(), now, msg)
        return None
