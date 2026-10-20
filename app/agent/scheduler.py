"""Daily research schedule on the exchange calendar (Phase 10).

Windows for a regular NSE session (IST):
- pre: at ``agent.pre_session_time`` (only before the open);
- intraday: after every ``agent.intraday_every_n_candles``-th candle close
  plus the paper grace period; the workflow calls the model only when the
  inputs changed;
- post: at ``agent.post_session_time``.

Holidays, weekends and special sessions are skipped. One instance at a time
(``agent.lock_file``). ``request_stop()`` ends the day early between runs.
A failing run is recorded (FAILED plus an alert) and the schedule continues.
Past windows are not replayed after a late start, except the post-session
review.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

from app.paper.scheduler import instance_lock, step_times
from app.utils.logging import get_logger

log = get_logger("app.agent.scheduler")


class AgentScheduler:
    def __init__(self, agent, clock=None, sleep=None):
        self.agent = agent
        self.cal = agent.calendar
        self.clock = clock or (lambda: datetime.now(self.cal.tz))
        self.stop_event = threading.Event()
        self.sleep = sleep or self.stop_event.wait

    def request_stop(self) -> None:
        self.stop_event.set()

    def plan(self, day) -> list[tuple[datetime, str]]:
        cfg, s = self.agent.cfg, self.agent.settings
        if not self.cal.is_regular_session(day):
            return []
        open_dt, close_dt = self.cal.session_bounds(day)
        tz = self.cal.tz
        out = [(datetime.combine(day, cfg.pre_session_time, tzinfo=tz), "pre")]
        from app.data.models import parse_interval

        steps = step_times(self.cal, day, parse_interval(s.strategy.candle_interval).timedelta,
                           timedelta(seconds=s.paper.grace_seconds))
        out += [(t, "intraday") for t in steps[cfg.intraday_every_n_candles - 1::
                                                   cfg.intraday_every_n_candles] if t < close_dt]
        out.append((datetime.combine(day, cfg.post_session_time, tzinfo=tz), "post"))
        return [(t, w) for t, w in out if w != "pre" or t < open_dt]

    def run_today(self) -> list[dict]:
        results = []
        with instance_lock(self.agent.cfg.lock_file):
            now = self.clock()
            plan = self.plan(now.date())
            if not plan:
                log.info(f"{now.date()} is not a regular NSE session; no research today")
                return results
            for when, window in plan:
                now = self.clock()
                if now > when + timedelta(minutes=10) and window != "post":
                    continue  # missed window: never replayed with hindsight
                if now < when:
                    self.sleep((when - now).total_seconds())
                if self.stop_event.is_set():
                    break
                try:
                    results.append(self.agent.run(window, self.clock()))
                except Exception as exc:  # noqa: BLE001 - recorded by the agent; keep going
                    log.error(f"research window {window} failed: {exc}")
                    results.append({"status": "FAILED", "window": window, "error": str(exc)})
        return results


