"""Experiments, validation and controlled promotion CLI (Phase 14). Paper only.

    python -m app.experiments init --by NAME --confirm "INIT paper-ai"   # register current system
    python -m app.experiments create --spec config/experiments/<x>.yaml --by NAME
    python -m app.experiments validate --id EXP-.. --data DIR             # validation split
    python -m app.experiments validate --id EXP-.. --data DIR --final     # final set, once
    python -m app.experiments request-approval --id EXP-.. --by NAME --reason R
    python -m app.experiments approve  --id EXP-.. --by NAME --reason R --confirm "APPROVE EXP-.."
    python -m app.experiments shadow   --id EXP-.. --by NAME --reason R
    python -m app.experiments canary   --id EXP-.. --by NAME --reason R
    python -m app.experiments activate --id EXP-.. --by NAME --reason R --confirm "ACTIVATE EXP-.."
    python -m app.experiments rollback --id EXP-.. --by NAME --reason R --confirm "ROLLBACK EXP-.."
    python -m app.experiments reject   --id EXP-.. --by NAME --reason R
    python -m app.experiments status | show --id EXP-.. | verify | history
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from app.config import load_settings
from app.utils.market_calendar import MarketCalendar


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.experiments")
    ap.add_argument("command", choices=["init", "create", "validate", "request-approval",
                                        "approve", "shadow", "canary", "activate", "rollback",
                                        "reject", "status", "show", "verify", "history"])
    ap.add_argument("--id")
    ap.add_argument("--spec", type=Path)
    ap.add_argument("--data", type=Path)
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--by", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--confirm")
    args = ap.parse_args(argv)

    from app.experiments import promotion as PR
    from app.experiments.registry import Registry
    from app.experiments.runtime import CONTEXT

    settings = load_settings()
    now = datetime.now(MarketCalendar(settings.market).tz)
    reg = Registry(settings.experiments.registry_db)
    print("EXPERIMENTS (paper only; nothing becomes active without validation and a person's "
          "explicit approval).")
    c = args.command
    if c == "init":
        from app.experiments.validate import init_registry

        print("Active version:", init_registry(reg, settings, args.by, args.confirm or "", now))
    elif c == "create":
        from app.experiments.candidate import load_spec
        from app.experiments.validate import create

        print("Experiment:", create(reg, settings, load_spec(args.spec), args.by, now))
    elif c == "validate":
        from app.experiments.validate import validate

        out = validate(reg, args.id, settings, args.data, final=args.final, now=now)
        print(f"{out['split']}: {out['classification']}  report: {out['report']}")
        for crit in out["criteria"]:
            print(f"  {crit['status']:12} {crit['criterion']}")
        for x in out["invalid"]:
            print(f"  INVALID: {x}")
    elif c == "request-approval":
        PR.request_approval(reg, args.id, args.by, args.reason, now)
    elif c == "approve":
        PR.approve(reg, args.id, args.by, args.reason, args.confirm, now)
    elif c == "shadow":
        PR.start_shadow(reg, args.id, args.by, args.reason, now)
    elif c == "canary":
        print(json.dumps(PR.start_canary(reg, settings, args.id, args.by, args.reason, now)))
    elif c == "activate":
        print(json.dumps(PR.activate(reg, settings, args.id, args.by, args.reason, args.confirm,
                                     now)))
    elif c == "rollback":
        print("Restored version:", PR.rollback(reg, args.id, args.by, args.reason, args.confirm,
                                               now))
    elif c == "reject":
        PR.reject(reg, args.id, args.by, args.reason, now)
    elif c == "verify":
        problems = reg.verify()
        print("registry OK" if not problems else "INTEGRITY PROBLEMS:\n  " + "\n  ".join(problems))
        return 1 if problems else 0
    elif c == "history":
        for h in reg.history(CONTEXT):
            print(json.dumps(h, default=str))
    elif c == "show":
        x = reg.experiment(args.id)
        print(json.dumps({k: v for k, v in x.items() if k != "acceptance_text"}, indent=2,
                         default=str))
    else:
        print("Active version:", reg.active_version(CONTEXT))
        for x in reg.all("SELECT experiment_id, status, name, candidate_version FROM "
                         "experiments ORDER BY created_at"):
            print(f"{x['experiment_id']} {x['status']:17} {x['candidate_version']} {x['name']}")
    if c in ("request-approval", "approve", "shadow", "reject", "activate", "canary"):
        print(f"{args.id}: {reg.experiment(args.id)['status']}")
    return 0


def main() -> int:
    from app.experiments.registry import RegistryError

    try:
        return run()
    except (RegistryError, ValueError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
