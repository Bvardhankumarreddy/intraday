"""Which AI system runs in an integrated session (Phase 14).

Without a registry, the built-in system runs and is labelled "unregistered".
With one:
- the hash chain is verified first; any tampering raises ``IntegrityError``
  and the session refuses to start;
- the ACTIVE version of context ``paper-ai`` supplies the AI-assisted arm's
  prompt variant, model configuration, tool allow-list and agent limits;
- the version must match the running agent code (``agent_code_sha256``).
  A mismatch raises ``VersionMismatch``: changed code needs a new version
  through the promotion workflow;
- a version that changes deterministic strategy settings is never applied at
  runtime. That needs a new frozen baseline (Phase 7);
- at most one experiment in SHADOW and one in PAPER_CANARY are attached as
  extra, isolated arms.

The deterministic baseline arm never takes anything from the registry.
"""

from __future__ import annotations

from pathlib import Path

from app.experiments.candidate import (agent_kwargs, apply_components,
                                       changes_deterministic_strategy)
from app.experiments.registry import Registry, RegistryError

CONTEXT = "paper-ai"


class VersionMismatch(RegistryError):
    pass


def resolve(settings) -> dict:
    path = Path(settings.experiments.registry_db)
    if not path.exists():
        return {"version": None, "label": "unregistered (built-in system)", "components": None,
                "shadow": None, "canary": None}
    from app.aieval.protocol import agent_code_sha

    reg = Registry(path)
    try:
        reg.require_valid()
        vid = reg.active_version(CONTEXT)
        if vid is None:
            return {"version": None, "label": "registry has no active version",
                    "components": None, "shadow": None, "canary": None}
        comp = reg.version(vid)["components"]
        if comp.get("agent_code_sha256") != agent_code_sha():
            raise VersionMismatch(f"active version {vid} was approved for agent code "
                                  f"{str(comp.get('agent_code_sha256'))[:12]}, but the running "
                                  f"code is {agent_code_sha()[:12]}; register and promote a new "
                                  "version")
        if changes_deterministic_strategy(comp):
            raise VersionMismatch(f"active version {vid} changes deterministic strategy "
                                  "settings; that requires a new frozen baseline")

        def staged(status):
            rows = reg.all("SELECT experiment_id, candidate_version FROM experiments WHERE "
                           "context = ? AND status = ?", (CONTEXT, status))
            if len(rows) > 1:
                raise RegistryError(f"more than one experiment in {status}")
            if not rows:
                return None
            c = reg.version(rows[0]["candidate_version"])["components"]
            return {"experiment": rows[0]["experiment_id"],
                    "version": rows[0]["candidate_version"], "components": c}

        return {"version": vid, "label": f"active version {vid}", "components": comp,
                "shadow": staged("SHADOW"), "canary": staged("PAPER_CANARY")}
    finally:
        reg.close()


def build_session(settings, calendar, provider, a_store, b_store, llm=None, llm_factory=None,
                  resolved: dict | None = None):
    """The integrated engine with the registry's active AI version and any
    shadow or canary candidate attached."""
    from app.agent.llm import build_llm
    from app.agent.workflow import ResearchAgent
    from app.evaluation.recorder import EvaluationRecorder
    from app.integrated.engine import ExtraArm, IntegratedEngine
    from app.integrated.snapshot import StepSnapshot
    from app.paper.engine import PaperEngine
    from app.paper.store import PaperStore

    r = resolved if resolved is not None else resolve(settings)
    snap = StepSnapshot(provider)

    def agent_for(comp, store, name):
        s = apply_components(settings, comp) if comp else settings
        model = (llm_factory(name, comp) if llm_factory else
                 (llm if (llm is not None and name == "ai") else build_llm(s)))
        return ResearchAgent(s, calendar, snap, store, model,
                             **(agent_kwargs(comp) if comp else {}))

    baseline = PaperEngine(snap, settings, calendar, a_store,
                           recorder=EvaluationRecorder(a_store, settings, calendar))
    ai_engine = PaperEngine(snap, settings, calendar, b_store,
                            recorder=EvaluationRecorder(b_store, settings, calendar))
    extras = []
    if r.get("shadow"):
        store = PaperStore(settings.experiments.shadow_db_path)
        extras.append(ExtraArm("shadow", agent_for(r["shadow"]["components"], store, "shadow")))
    if r.get("canary"):
        from app.experiments.validate import load_acceptance

        acc, _, _ = load_acceptance(settings.experiments.acceptance_file)
        store = PaperStore(settings.experiments.canary_db_path)
        canary = PaperEngine(snap, settings, calendar, store,
                             recorder=EvaluationRecorder(store, settings, calendar))
        extras.append(ExtraArm("canary", agent_for(r["canary"]["components"], store, "canary"),
                               canary, acc["prospective"]["canary_sessions"]))
    engine = IntegratedEngine(baseline, ai_engine, agent_for(r["components"], b_store, "ai"),
                              snap, settings.integrated.agent_every_n_candles, extras)
    engine.ai_version = r
    from app.monitoring.monitor import SessionMonitor

    engine.attach_monitor(SessionMonitor(settings, {
        "A": a_store, "B": b_store,
        **{x.name: x.engine.store for x in extras if x.engine is not None}}))
    if settings.whatsapp.enabled:
        from app.monitoring.alerts import MonitorStore
        from app.notify.events import SessionEvents
        from app.notify.whatsapp import Notifications

        engine.hooks.append(SessionEvents(settings, {"A": a_store, "B": b_store},
                                          Notifications(settings, MonitorStore(
                                              settings.monitoring.db_path))))
    return engine
