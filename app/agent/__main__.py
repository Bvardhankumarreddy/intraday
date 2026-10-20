"""AI research agent CLI (Phase 10). Advisory only: never places orders.

    python -m app.agent run --window pre|intraday|post [--at 2026-10-21T09:41] [--data DIR]
    python -m app.agent schedule            # today's pre / intraday / post windows
    python -m app.agent status [--date D]   # runs and recommendations
    python -m app.agent report --run RUN_ID # regenerate a run's report from records
    python -m app.agent compare [--from D --to D]   # AI (B) vs deterministic baseline (A)
    python -m app.agent check-llm           # ONE minimal request to the configured real
                                            # provider (needs NSE_LLM__PROVIDER=anthropic
                                            # and NSE_LLM_API_KEY); reports latency/tokens

Market data comes from the configured provider (``data.provider``), or from
historical CSVs with ``--data`` (replay-style research, labelled HISTORICAL).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from app.config import PROJECT_ROOT, load_settings
from app.utils.logging import configure_logging
from app.utils.market_calendar import MarketCalendar


def _agent(settings, calendar, data_dir: Path | None):
    from app.agent.workflow import ResearchAgent
    from app.data.factory import build_provider
    from app.paper.store import PaperStore

    if data_dir:
        from app.backtesting.dataset import load_csv_dataset

        provider = load_csv_dataset(data_dir, settings, calendar).provider(calendar)
    else:
        provider = build_provider(settings, calendar)
    store = PaperStore(settings.paper.db_path)
    return ResearchAgent(settings, calendar, provider, store)


def check_llm(settings) -> int:
    """Live provider check: one tiny forced tool call. Nothing is stored."""
    from app.agent.llm import AnthropicProvider, LLMError, cost_usd

    try:
        llm = AnthropicProvider(settings)
    except LLMError as exc:
        print(f"NOT CONFIGURED: {exc}")
        return 2
    schema = {"type": "object", "properties": {"status": {"type": "string",
                                                          "enum": ["ok"]}},
              "required": ["status"], "additionalProperties": False}
    try:
        resp = llm.chat("Reply by calling the tool with status ok.",
                        [{"role": "user", "content": "connectivity check"}],
                        [{"name": "report_status", "description": "report status",
                          "input_schema": schema}],
                        {"type": "tool", "name": "report_status"})
    except LLMError as exc:
        print(f"FAILED ({exc.kind}): {exc}")
        return 1
    ok = any(u.name == "report_status" and u.input == {"status": "ok"} for u in resp.tool_uses)
    print(json.dumps({"result": "OK" if ok else "UNEXPECTED_OUTPUT", "model": resp.model,
                      "latency_seconds": round(resp.latency_seconds, 3),
                      "input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens,
                      "cost_usd": cost_usd(settings, resp), "attempts": resp.attempts}, indent=2))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--window", choices=["pre", "intraday", "post"], required=True)
    r.add_argument("--at", type=datetime.fromisoformat)
    r.add_argument("--data", type=Path)
    sub.add_parser("schedule")
    s = sub.add_parser("status")
    s.add_argument("--date", type=date.fromisoformat)
    rp = sub.add_parser("report")
    rp.add_argument("--run", required=True)
    c = sub.add_parser("compare")
    c.add_argument("--from", dest="start", type=date.fromisoformat)
    c.add_argument("--to", dest="end", type=date.fromisoformat)
    c.add_argument("--data", type=Path)
    sub.add_parser("check-llm")
    args = ap.parse_args(argv)

    settings = load_settings()
    configure_logging(settings.log_level, settings)
    calendar = MarketCalendar(settings.market)
    now = datetime.now(calendar.tz)
    print("AI RESEARCH (advisory only). No orders are placed; real orders are placed "
          "manually by the user.")
    if args.cmd == "check-llm":
        return check_llm(settings)
    agent = _agent(settings, calendar, getattr(args, "data", None))
    print(f"LLM provider: {agent.llm.name} ({agent.llm.model})"
          + ("  [deterministic mock - no language model used]" if agent.llm.name == "mock"
             else ""))
    if args.cmd == "run":
        at = args.at.replace(tzinfo=calendar.tz) if args.at and args.at.tzinfo is None \
            else (args.at or now)
        out = agent.run(args.window, at)
        print(json.dumps({k: v for k, v in out.items() if k != "recommendations"}, indent=2,
                         default=str))
        for rec in out.get("recommendations", []):
            print(f"  {rec['symbol'] or '*'}: {rec['status']} - {'; '.join(rec['status_reasons'])[:160]}")
        return 0
    if args.cmd == "schedule":
        import signal

        from app.agent.scheduler import AgentScheduler

        sched = AgentScheduler(agent)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: sched.request_stop())
        for res in sched.run_today():
            print(json.dumps({k: v for k, v in res.items() if k != "recommendations"},
                             default=str))
        return 0
    if args.cmd == "status":
        day = (args.date or now.date()).isoformat()
        for run in agent.agent_store.runs(day):
            print(f"{run['run_id']} {run['window']:8} {run['status']:18} {run['reason'] or ''}")
        for rec in agent.agent_store.recommendations(market_date=day):
            print(f"  {rec['recommendation_id']} {rec['symbol'] or '*':10} {rec['status']}")
        return 0
    if args.cmd == "report":
        from app.agent.report import write_run_report

        print("Report:", write_run_report(agent, args.run))
        return 0
    if args.cmd == "compare":
        from app.agent.compare import compare, default_period
        from app.utils.fs import private_dir, write_preserving

        start, end = default_period(now.date())
        start, end = args.start or start, args.end or end
        res = compare(agent.agent_store, agent.provider, settings, calendar, start, end)
        out_dir = private_dir(PROJECT_ROOT / "data" / "agent" / "evaluation")
        path = out_dir / f"compare_{start}_{end}.json"
        write_preserving(path, json.dumps(res, indent=2, default=str) + "\n")
        print(json.dumps({k: v for k, v in res.items() if k != "details"}, indent=2,
                         default=str))
        print("Written:", path)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
