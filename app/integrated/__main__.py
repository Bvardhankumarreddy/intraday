"""Integrated AI-assisted paper trading CLI (Phase 12). SIMULATED orders only.

    python -m app.integrated start --name "integrated-v1" [--from 2026-10-21]
        Freeze the protocol: start Phase 6 evaluations in BOTH paper databases
        (baseline and AI-assisted) and record the strategy, AI and protocol
        versions and the configured data mode.
    python -m app.integrated run            # today's live session, both arms
    python -m app.integrated replay --date D [--data DIR]   # separate replay databases
    python -m app.integrated report --date D [--replay]     # daily integrated report
    python -m app.integrated cumulative     # baseline vs AI-assisted (Phase 11 rules)
    python -m app.integrated status

No real order is ever placed. The AI-assisted arm is a second paper portfolio.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from app.config import load_settings
from app.utils.logging import configure_logging
from app.utils.market_calendar import MarketCalendar

LABEL = ("PAPER TRADING - SIMULATED (baseline and AI-assisted arms). No real orders; real "
         "orders are placed manually by the user.")


def _stores(settings, replay: bool):
    from app.paper.store import PaperStore

    a = PaperStore(settings.paper.replay_db_path if replay else settings.paper.db_path)
    b = PaperStore(settings.integrated.replay_ai_db_path if replay
                   else settings.integrated.ai_db_path)
    return a, b


def protocol_record(settings, llm, provider_mode: str | None) -> dict:
    from app.aieval.protocol import ai_versions, load_protocol, pin_mismatches
    from app.robustness.baseline import load_frozen, strategy_fingerprint

    protocol, _, psha = load_protocol(settings.ai_eval.protocol_file)
    frozen = load_frozen(protocol.baseline_path())
    versions = ai_versions(settings, llm)
    fp, _ = strategy_fingerprint(settings)
    return {"protocol_version": protocol.protocol_version, "protocol_sha256": psha,
            "baseline": frozen.name, "baseline_fingerprint": frozen.fingerprint,
            "live_strategy_fingerprint": fp,
            "live_config_matches_frozen_baseline": fp == frozen.fingerprint,
            "baseline_code_drift": frozen.code_drift, "config_version": settings.version(),
            "ai_versions": versions, "ai_system_drift": pin_mismatches(protocol, versions),
            "configured_data_provider": settings.data.provider.value,
            "data_mode": provider_mode, "mode": "observation (paper only)"}


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.integrated")
    ap.add_argument("command", choices=["start", "run", "replay", "report", "cumulative",
                                        "status"])
    ap.add_argument("--name")
    ap.add_argument("--from", dest="start", type=date.fromisoformat)
    ap.add_argument("--to", dest="end", type=date.fromisoformat)
    ap.add_argument("--date", type=date.fromisoformat)
    ap.add_argument("--data", type=Path)
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--allow-drift", action="store_true")
    args = ap.parse_args(argv)

    from app.agent.llm import build_llm
    from app.evaluation.recorder import EvaluationRecorder

    settings = load_settings()
    configure_logging(settings.log_level, settings)
    calendar = MarketCalendar(settings.market)
    now = datetime.now(calendar.tz)
    print(LABEL)
    replay = args.command == "replay" or args.replay
    a_store, b_store = _stores(settings, replay)
    rec_a = EvaluationRecorder(a_store, settings, calendar)
    rec_b = EvaluationRecorder(b_store, settings, calendar)

    if args.command == "start":
        if not args.name:
            ap.error("start needs --name")
        record = protocol_record(settings, build_llm(settings), None)
        if record["ai_system_drift"] and not args.allow_drift:
            print("REFUSED: the AI system differs from the protocol pins: "
                  + "; ".join(record["ai_system_drift"]) + ". Update the protocol (new "
                  "version) or pass --allow-drift to record the evaluation as drifted.")
            return 2
        start = args.start or now.date()
        ea = rec_a.create_evaluation(args.name, start, now)
        eb = rec_b.create_evaluation(args.name + " [AI-assisted]", start, now)
        record.update(start_date=start, evaluations={"A": ea, "B": eb},
                      started_at=now.isoformat())
        for store in (a_store, b_store):
            with store.transaction():
                store.set_meta("integrated_protocol", record)
                store.event(now, None, "EVALUATION", "INFO",
                            f"integrated evaluation started: {json.dumps(record, default=str)}"
                            [:2000])
        print(json.dumps(record, indent=2, default=str))
        return 0

    if args.command in ("run", "replay"):
        from app.data.mock_provider import MockMarketDataProvider
        from app.integrated.report import write_daily

        if args.command == "replay":
            if args.date is None:
                ap.error("replay needs --date")
            for rec, nm in ((rec_a, "replay"), (rec_b, "replay [AI-assisted]")):
                if rec.active() is None:
                    rec.create_evaluation(nm, args.date, now)
            if args.data:
                from app.backtesting.dataset import load_csv_dataset

                provider = load_csv_dataset(args.data, settings, calendar).provider(calendar)
            else:
                provider = MockMarketDataProvider(calendar, seed=settings.data.mock_seed)
        else:
            if rec_a.active() is None or rec_b.active() is None:
                print("No active integrated evaluation. Start one: python -m app.integrated "
                      "start --name ...")
                return 2
            from app.paper.scheduler import provider_or_fail

            provider = provider_or_fail(settings, calendar, a_store, rec_a)
            if provider is None:
                return 1
        hook = (lambda t: setattr(provider, "as_of", t)) \
            if isinstance(provider, MockMarketDataProvider) else None
        print(f"Data: {provider.name} [{provider.mode.value.upper()}]"
              + ("" if provider.mode.value == "live" else
                 " - NOT live: never counted as primary evidence"))
        from app.experiments.registry import RegistryError
        from app.experiments.runtime import build_session

        try:
            engine = build_session(settings, calendar, provider, a_store, b_store)
        except RegistryError as exc:
            print(f"REFUSED: {exc}. The AI system cannot be trusted, so the session does not "
                  "start; run `python -m app.experiments verify`.")
            return 2
        print(f"AI system: {engine.ai_version['label']}"
              + "".join(f"; {k} {engine.ai_version[k]['experiment']}"
                        for k in ("shadow", "canary") if engine.ai_version.get(k)))
        print(f"AI: {engine.agent.llm.name} ({engine.agent.llm.model})"
              + (" - deterministic mock, no language model" if engine.agent.llm.name == "mock"
                 else ""))
        if args.command == "replay":
            from app.paper.scheduler import replay_session

            problems = engine.preflight(args.date)
            if problems:
                print("Cannot start:", *problems, sep="\n  ")
                return 1
            replay_session(engine, args.date, before_step=hook)
            day, code = args.date, 0
        else:
            from app.paper.scheduler import run_scheduled

            day = now.date()
            b_path = b_store.path
            code = run_scheduled(engine, hook, rec_a)  # closes the baseline store
            engine.ai.store.close()
            a_store, b_store = _stores(settings, False)
            if not Path(b_path).exists():
                return code
        settings_for_report = settings if not replay else settings.model_copy(update={
            "integrated": settings.integrated.model_copy(update={
                "reports_dir": Path(settings.integrated.reports_dir) / "replay"})})
        if a_store.session(day) is not None:
            print("Integrated daily report:", write_daily(settings_for_report, calendar,
                                                          a_store, b_store, day))
        return code

    if args.command == "report":
        from app.integrated.report import write_daily

        day = args.date or now.date()
        s = settings if not replay else settings.model_copy(update={
            "integrated": settings.integrated.model_copy(update={
                "reports_dir": Path(settings.integrated.reports_dir) / "replay"})})
        print("Integrated daily report:", write_daily(s, calendar, a_store, b_store, day))
        return 0

    if args.command == "cumulative":
        from app.aieval.ledger import Ledger
        from app.aieval.prospective import evaluate_integrated

        out = evaluate_integrated(settings, a_store, b_store, Ledger(settings.ai_eval.ledger_db),
                                  args.start, args.end)
        c = out["conclusion"]
        print(f"{out['status']}: {c['conclusion']} - {c.get('reason', '')}")
        if out.get("run_id"):
            print("Report:", Path(settings.ai_eval.output_dir) / out["run_id"] / "report.md")
        for s in out.get("sessions", out.get("coverage", {}).get("session_status", [])):
            print(f"  {s['session']}: A {s['A']}, B {s['B']}"
                  + ("" if s["eligible"] else " (not counted)"))
        return 0

    # status
    for name, store, rec in (("A baseline", a_store, rec_a), ("B AI-assisted", b_store, rec_b)):
        ev = rec.active()
        hb = store.get_meta("heartbeat")
        print(f"{name}: evaluation {ev['evaluation_id'] if ev else 'none'}; heartbeat "
              f"{(hb or {}).get('time', 'none')}")
        for s in store.all("SELECT trading_date, state, data_mode FROM sessions ORDER BY "
                           "trading_date DESC LIMIT 5"):
            print(f"   {s['trading_date']} {s['state']} [{s['data_mode']}]")
    proto = b_store.get_meta("integrated_protocol")
    print("Protocol:", json.dumps({k: proto.get(k) for k in (
        "protocol_version", "start_date", "data_mode", "ai_system_drift")} if proto else None,
        default=str))
    return 0


def main() -> int:
    try:
        return run()
    except Exception as exc:  # noqa: BLE001 - one clear line, no traceback
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
