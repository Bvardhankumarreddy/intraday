"""AI evaluation CLI (Phase 11). Evaluation only: never places orders.

    python -m app.aieval protocol                       # show protocol and version pins
    python -m app.aieval replay --data DIR --split development|validation|holdout
    python -m app.aieval prospective [--from D --to D]  # live paper records (primary)
    python -m app.aieval status                         # evaluation runs in the ledger
    python -m app.aieval report --run AE-...            # regenerate a report from records

Replay with the real model costs API calls (one research run per signal
time, capped by ai_eval.max_agent_runs). The holdout split runs once per
protocol version and dataset.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from app.config import load_settings


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.aieval")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("protocol")
    r = sub.add_parser("replay")
    r.add_argument("--data", type=Path, required=True)
    r.add_argument("--split", choices=["development", "validation", "holdout"], required=True)
    p = sub.add_parser("prospective")
    p.add_argument("--from", dest="start", type=date.fromisoformat)
    p.add_argument("--to", dest="end", type=date.fromisoformat)
    sub.add_parser("status")
    rp = sub.add_parser("report")
    rp.add_argument("--run", required=True)
    args = ap.parse_args(argv)

    from app.agent.llm import build_llm
    from app.aieval.ledger import Ledger
    from app.aieval.protocol import ai_versions, load_protocol, pin_mismatches

    settings = load_settings()
    print("AI EVALUATION (research only). No orders are placed; real orders are placed "
          "manually by the user.")
    if args.cmd == "protocol":
        protocol, _, psha = load_protocol(settings.ai_eval.protocol_file)
        versions = ai_versions(settings, build_llm(settings))
        print(json.dumps({"protocol_version": protocol.protocol_version,
                          "protocol_sha256": psha, "pins": protocol.ai_system.model_dump(),
                          "actual": versions,
                          "drift": pin_mismatches(protocol, versions)}, indent=2, default=str))
        return 0
    ledger = Ledger(settings.ai_eval.ledger_db)
    if args.cmd == "status":
        for row in ledger.runs():
            print(f"{row['run_id']} {row['mode']:17} {row['split'] or '-':11} "
                  f"{row['period_start']}..{row['period_end']} {row['status']:12} "
                  f"{row['conclusion'] or row['reason'] or ''}")
        return 0
    if args.cmd == "report":
        from app.aieval.report import regenerate

        row = ledger.run(args.run)
        if row is None or not row["results_path"]:
            print(f"no completed run {args.run}")
            return 1
        print("Report:", regenerate(Path(row["results_path"]).parent))
        return 0
    if args.cmd == "replay":
        from app.aieval.replay import evaluate_replay

        out = evaluate_replay(settings, args.data, args.split, ledger)
    else:
        from app.aieval.prospective import evaluate_prospective
        from app.paper.store import PaperStore

        if not Path(settings.paper.db_path).exists():
            print(f"PENDING_DATA: no paper database at {settings.paper.db_path}")
            return 0
        out = evaluate_prospective(settings, PaperStore(settings.paper.db_path), ledger,
                                   args.start, args.end)
    c = out["conclusion"]
    print(f"{out['status']}: {c['conclusion']} - {c.get('reason', '')}")
    if out.get("run_id"):
        print("Report:", Path(settings.ai_eval.output_dir) / out["run_id"] / "report.md")
    return 0


def main() -> int:
    from app.aieval.ledger import DuplicateRun
    from app.aieval.replay import ReplayRefused

    try:
        return run()
    except (ReplayRefused, DuplicateRun) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - one clear line, no traceback
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
