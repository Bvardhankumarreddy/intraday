"""Integrated session engine: deterministic baseline + AI-assisted paper arm.

One ``IntegratedEngine.step(now)``, driven by the existing
``SessionScheduler`` (instance lock, holidays, retries, graceful stop,
restart catch-up, Phase 6 recording), does the following:

1. ``StepSnapshot.reset()``: one data view for everything below.
2. **Baseline arm**: the existing ``PaperEngine`` on ``paper.db_path``,
   unchanged. It does health checks, fills, exits, scan, the Phase 3
   risk/cost check, orders and reports. Its errors propagate to the
   scheduler (bounded retries, then abort), exactly as before.
3. **Research agent** (Phase 10), every ``integrated.agent_every_n_candles``
   candles, at the same timestamp. Its immutable recommendations go into the
   AI arm's database. Its risk tool therefore sees the AI arm's own
   portfolio. Failures are isolated: recorded, alerted, never raised.
4. **AI-assisted arm**: a second, identical ``PaperEngine`` on
   ``integrated.ai_db_path`` with one extra condition (``signal_gate``). A
   signal the risk engine approved becomes a paper order only if a CANDIDATE
   recommendation for that signal was persisted at or before this step.
   The gate can only block. It never sizes, prices or creates an order. Its
   failures are isolated from the baseline. After
   ``paper.max_consecutive_failures`` failures the AI arm stops opening new
   positions for the day. Open AI-arm positions are still managed on later
   steps where possible, and the day's AI-arm session is marked INCOMPLETE.

Fallback policy (AI provider down or invalid output): the agent records
UNAVAILABLE or INVALID_OUTPUT, so no CANDIDATE exists and the AI arm opens no
new position. Its existing positions keep their deterministic stop, target
and end-of-day exits. The baseline continues and stays labelled as the
deterministic baseline. Its output is never presented as AI-generated.

Stale or invalid data: the baseline engine blocks new signals itself, the
agent abstains without a model call, and the AI arm blocks too.

Nothing here can reach a broker's order API (``execution_mode`` must be
"paper").
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from app.paper.engine import PaperEngine, StepResult
from app.utils.logging import get_logger

log = get_logger("app.integrated")
PAUSED_STATUS = "PAUSED_BY_MONITORING"


def ai_gate(store, cand, now: datetime) -> tuple[bool, str]:
    """Allow a risk-approved signal only with a CANDIDATE recommendation for it
    persisted in ``store`` at or before ``now``. Reads immutable records only."""
    sid = cand.setup.signal_id
    rows = store.all(
        "SELECT recommendation_id, status, created_at, body FROM agent_recommendations "
        "WHERE market_date = ? AND symbol = ? ORDER BY created_at",
        (now.date().isoformat(), cand.symbol))
    mine = [r for r in rows if json.loads(r["body"]).get("signal_id") == sid
            and datetime.fromisoformat(r["created_at"]) <= now]
    cands = [r for r in mine if r["status"] == "CANDIDATE"]
    if cands:
        return True, f"AI CANDIDATE {cands[-1]['recommendation_id']}"
    if mine:
        last = mine[-1]
        return False, (f"AI-assisted status {last['status']} ({last['recommendation_id']}); "
                       "no CANDIDATE before the order decision")
    return False, ("no AI recommendation for this signal before the order decision (AI "
                   "unavailable, not shortlisted, or not yet reviewed)")


class ExtraArm:
    """A Phase 14 candidate running beside the approved system.

    - SHADOW: ``engine`` is None. The candidate agent researches at the same
      timestamps and writes immutable recommendations to its own database. It
      gates nothing and orders nothing.
    - PAPER_CANARY: the candidate agent plus its own gated paper engine on a
      separate database, for a bounded number of sessions (``max_sessions``
      completed; after that it stops opening sessions).

    Failures are isolated exactly like the AI-assisted arm's.
    """

    def __init__(self, name: str, agent, engine: PaperEngine | None = None,
                 max_sessions: int | None = None):
        self.name, self.agent, self.engine, self.max_sessions = name, agent, engine, max_sessions
        self.failures = 0
        self.disabled: dict[date, str] = {}
        if engine is not None:
            engine.signal_gate = lambda cand, now: ai_gate(engine.store, cand, now)

    @property
    def store(self):
        return self.agent.store

    def exhausted(self, day: date) -> bool:
        if self.engine is None or self.max_sessions is None:
            return False
        done = self.engine.store.all("SELECT COUNT(*) AS n FROM sessions WHERE state = "
                                     "'CLOSED' AND trading_date < ?", (day.isoformat(),))
        return done[0]["n"] >= self.max_sessions


class IntegratedEngine:
    def __init__(self, baseline: PaperEngine, ai_engine: PaperEngine, agent, snapshot,
                 every_n: int = 1, extras: list[ExtraArm] | tuple = ()):
        if baseline.store.path == ai_engine.store.path:
            raise ValueError("the baseline and the AI-assisted arm need separate databases")
        paths = [baseline.store.path, ai_engine.store.path] + [
            x.agent.store.path for x in extras]
        if len(set(paths)) != len(paths):
            raise ValueError("every arm (baseline, AI-assisted, shadow, canary) needs its own "
                             "database")
        if baseline.settings.execution_mode != "paper":
            raise ValueError("integrated trading runs only with execution_mode 'paper'")
        self.baseline, self.ai, self.agent, self.snapshot = baseline, ai_engine, agent, snapshot
        self.every_n = every_n
        self.ai.signal_gate = self.gate
        self.ai_failures = 0
        self.ai_disabled: dict[date, str] = {}
        self.extras = list(extras)
        # Phase 15: monitoring hooks (None = Phase 12 behaviour) and an LLM breaker.
        self.monitor = None
        self.hooks: list = []  # after-step hooks, e.g. WhatsApp events (isolated)
        self._paused: dict[str, list] = {}
        self._llm_failures = 0
        self._breaker_until: datetime | None = None

    def attach_monitor(self, monitor) -> None:
        """Pause gates for every arm plus checks after each step. A paused arm
        opens no new paper entry; its open positions keep their exits."""
        self.monitor = monitor

        def pause_gate(arm, base):
            def g(cand, now):
                why = self._paused.get(arm)
                if why:
                    return False, f"paused by monitoring: {'; '.join(why)}"[:300], \
                        PAUSED_STATUS
                return base(cand, now) if base else (True, "")
            return g

        self.baseline.signal_gate = pause_gate("A", self.baseline.signal_gate)
        self.ai.signal_gate = pause_gate("B", self.ai.signal_gate)
        for x in self.extras:
            if x.engine is not None:
                x.engine.signal_gate = pause_gate(x.name, x.engine.signal_gate)

    # The scheduler talks to the baseline engine (store, settings, calendar,
    # recorder, step_td, provider, alerts) exactly as before.
    def __getattr__(self, name):
        return getattr(self.baseline, name)

    # -- AI arm isolation ------------------------------------------------------------
    def _ai_event(self, day: date | None, now: datetime, level: str, msg: str) -> None:
        try:
            with self.ai.store.transaction():
                self.ai.store.event(now, day, "INTEGRATION", level, msg)
        except Exception:  # noqa: BLE001 - never let bookkeeping stop the baseline
            pass
        (log.error if level == "ERROR" else log.info)(f"AI arm: {msg}")

    def _ai_call(self, day: date, now: datetime, what: str, fn):
        if day in self.ai_disabled:
            return None
        try:
            out = fn()
            self.ai_failures = 0
            return out
        except Exception as exc:  # noqa: BLE001 - isolated from the baseline
            self.ai_failures += 1
            msg = f"{what} failed ({self.ai_failures} in a row): {type(exc).__name__}: {exc}"
            self._ai_event(day, now, "ERROR", msg[:500])
            self.ai._alert("integrated.ai_arm_failed", "WARNING", "integrated",
                           f"AI-assisted arm: {msg}"[:500], now)
            if self.ai_failures >= self.baseline.settings.paper.max_consecutive_failures:
                self.ai_disabled[day] = msg[:300]
                self._ai_event(day, now, "ERROR", "AI-assisted arm stopped for the day; the "
                                                  "baseline continues")
                self.ai._alert("integrated.ai_arm_stopped", "CRITICAL", "integrated",
                               f"AI-assisted arm stopped for {day}: {msg}"[:500], now)
                if self.ai.recorder is not None and self.ai.recorder.active() is not None:
                    try:
                        from app.evaluation.recorder import KIND_FOR_MODE

                        self.ai.recorder.session_incomplete(
                            day, now, f"AI arm stopped: {msg}"[:300],
                            kind=KIND_FOR_MODE[self.ai.provider.mode])
                    except Exception:  # noqa: BLE001
                        pass
            return None

    # -- lifecycle used by SessionScheduler --------------------------------------------
    def preflight(self, day: date) -> list[str]:
        problems = self.baseline.preflight(day)
        ai_problems = self.ai.preflight(day)
        if ai_problems and not problems:
            now = datetime.now(self.calendar.tz)
            self.ai_disabled[day] = "preflight: " + "; ".join(ai_problems)
            self._ai_event(day, now, "ERROR", f"AI arm preflight failed: {ai_problems}")
        return problems

    def open_session(self, day: date, now: datetime):
        state = self.baseline.open_session(day, now)
        self._ai_call(day, now, "open session", lambda: self.ai.open_session(day, now))
        for x in self.extras:
            if x.engine is not None and not x.exhausted(day):
                self._extra_call(x, day, now, "open session",
                                 lambda x=x: x.engine.open_session(day, now))
        return state

    def begin_evaluation_session(self, day: date, now: datetime) -> None:
        self.baseline.begin_evaluation_session(day, now)
        self._ai_call(day, now, "begin evaluation session",
                      lambda: self.ai.begin_evaluation_session(day, now))
        for x in self.extras:
            if x.engine is not None and x.engine.store.session(day) is not None:
                self._extra_call(x, day, now, "begin evaluation session",
                                 lambda x=x: x.engine.begin_evaluation_session(day, now))

    def end_evaluation_session(self, day: date, now: datetime):
        out = self.baseline.end_evaluation_session(day, now)
        self._ai_call(day, now, "end evaluation session",
                      lambda: self.ai.end_evaluation_session(day, now))
        if self.monitor is not None:
            self.monitor.after_step(now, "full")
        for x in self.extras:
            if x.engine is not None and x.engine.store.session(day) is not None:
                self._extra_call(x, day, now, "end evaluation session",
                                 lambda x=x: x.engine.end_evaluation_session(day, now))
        self._post_review(day, now)
        return out

    def step(self, now: datetime) -> StepResult:
        now = now.astimezone(self.calendar.tz)
        day = now.date()
        self.snapshot.reset()
        if self.monitor is not None:
            self._paused = self.monitor.paused(now)
        try:
            return self._step(now, day)
        finally:
            if self.monitor is not None:
                self.monitor.after_step(now)
            for hook in self.hooks:
                try:
                    hook.after_step(now)
                except Exception as exc:  # noqa: BLE001 - a hook never breaks trading
                    log.error(f"after-step hook failed: {type(exc).__name__}")

    def _step(self, now: datetime, day: date) -> StepResult:
        result = self.baseline.step(now)  # baseline first; its errors propagate
        if day in self.ai_disabled:
            return result
        if self._agent_due(now):
            self._run_agent(day, now)
        if self.ai.store.session(day) is None:
            self._ai_call(day, now, "open session", lambda: self.ai.open_session(day, now))
        self._ai_call(day, now, "step", lambda: self.ai.step(now))
        for x in self.extras:
            self._step_extra(x, day, now)
        return result

    # -- shadow / canary candidates (Phase 14) ------------------------------------------
    def _extra_call(self, x: ExtraArm, day: date, now: datetime, what: str, fn):
        if day in x.disabled:
            return None
        try:
            out = fn()
            x.failures = 0
            return out
        except Exception as exc:  # noqa: BLE001 - never affects the baseline or the AI arm
            x.failures += 1
            msg = f"{x.name} {what} failed ({x.failures}): {type(exc).__name__}: {exc}"[:500]
            try:
                with x.store.transaction():
                    x.store.event(now, day, "INTEGRATION", "ERROR", msg)
            except Exception:  # noqa: BLE001
                pass
            if x.failures >= self.baseline.settings.paper.max_consecutive_failures:
                x.disabled[day] = msg
            return None

    def _step_extra(self, x: ExtraArm, day: date, now: datetime) -> None:
        if day in x.disabled or x.exhausted(day):
            return
        if self._agent_due(now):
            self._extra_call(x, day, now, "research run", lambda: x.agent.run("intraday", now))
        if x.engine is not None:
            if x.engine.store.session(day) is None:
                self._extra_call(x, day, now, "open session",
                                 lambda: x.engine.open_session(day, now))
            self._extra_call(x, day, now, "step", lambda: x.engine.step(now))

    # -- agent --------------------------------------------------------------------
    def _agent_due(self, now: datetime) -> bool:
        eod = datetime.combine(now.date(), self.settings.strategy.eod_exit_time,
                               tzinfo=self.calendar.tz)
        slot = int(self.agent._slot("intraday", now))
        return now < eod and slot > 0 and slot % self.every_n == 0

    def _run_agent(self, day: date, now: datetime) -> None:
        cfg = self.baseline.settings.monitoring
        if self._breaker_until is not None:
            if now < self._breaker_until:
                return  # breaker open: no model call, no cost; the AI arm opens nothing
            self._breaker_until = None  # half-open: one trial run below
            self._ai_event(day, now, "INFO", "LLM circuit half-open: one trial research run")
        # Only runs that called the model count: UNAVAILABLE = failure; OK or
        # INVALID_OUTPUT = the provider answered (success). Runs without a model call
        # (unchanged inputs, abstentions, duplicates) leave the count unchanged.
        failed, answered = False, False
        try:
            out = self.agent.run("intraday", now)
            failed = out.get("ai_status") == "UNAVAILABLE"
            answered = out.get("ai_status") in ("OK", "INVALID_OUTPUT")
            if out.get("ai_status") in ("UNAVAILABLE", "INVALID_OUTPUT"):
                self._ai_event(day, now, "WARNING",
                               f"research run {out.get('run_id')}: AI {out['ai_status']}; no "
                               "new AI-arm position without a CANDIDATE recommendation")
        except Exception as exc:  # noqa: BLE001 - recorded FAILED by the agent; isolated
            failed = True
            self._ai_event(day, now, "ERROR", f"research run failed: {type(exc).__name__}: "
                                              f"{exc}"[:500])
        if failed:
            self._llm_failures += 1
        elif answered:
            self._llm_failures = 0
        if self._llm_failures >= cfg.llm_breaker_failures:
            self._breaker_until = now + timedelta(minutes=cfg.llm_breaker_cooldown_minutes)
            self._llm_failures = 0
            self._ai_event(day, now, "ERROR",
                           f"LLM circuit OPEN after {cfg.llm_breaker_failures} failed research "
                           f"runs; no model calls until {self._breaker_until:%H:%M}")
            self.ai._alert("integrated.llm_circuit_open", "ERROR", "llm",
                           "LLM circuit open: repeated model failures; deterministic baseline "
                           "continues, AI arm opens no new positions", now)

    def _post_review(self, day: date, now: datetime) -> None:
        try:
            self.agent.run("post", now)
        except Exception as exc:  # noqa: BLE001
            self._ai_event(day, now, "ERROR", f"post-session review failed: {exc}"[:500])

    # -- the AI gate --------------------------------------------------------------
    def gate(self, cand, now: datetime) -> tuple[bool, str]:
        return ai_gate(self.ai.store, cand, now)


def build_integrated(settings, calendar, provider, a_store, b_store, llm=None) -> IntegratedEngine:
    from app.agent.workflow import ResearchAgent
    from app.evaluation.recorder import EvaluationRecorder
    from app.integrated.snapshot import StepSnapshot

    snap = StepSnapshot(provider)
    baseline = PaperEngine(snap, settings, calendar, a_store,
                           recorder=EvaluationRecorder(a_store, settings, calendar))
    ai_engine = PaperEngine(snap, settings, calendar, b_store,
                            recorder=EvaluationRecorder(b_store, settings, calendar))
    agent = ResearchAgent(settings, calendar, snap, b_store, llm)
    return IntegratedEngine(baseline, ai_engine, agent, snap,
                            settings.integrated.agent_every_n_candles)
