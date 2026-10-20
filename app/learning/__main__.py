"""Learning pipeline CLI (Phase 13). Analysis and proposals only: nothing here
changes prompts, models, settings or the strategy, and nothing places orders.

    python -m app.learning ingest [--source DB] [--label L]   # default: AI paper arm DB
    python -m app.learning outcomes [--data DIR] [--at ISO]   # append knowable outcomes
    python -m app.learning review                             # rule-based post-trade review
    python -m app.learning annotate --record LR-.. --category C --reason R --by NAME
    python -m app.learning correct --record LR-.. --field F --value V --reason R --by NAME
    python -m app.learning analyse                            # findings, proposals, report
    python -m app.learning proposals [--id P-..]
    python -m app.learning test-plan --id P-.. --from D --to D --dataset NAME --by NAME
    python -m app.learning test-result --id P-.. --run AE-..
    python -m app.learning approve  --id P-.. --by NAME --reason R --confirm "APPROVE P-.."
    python -m app.learning deploy   --id P-.. --by NAME --reason R --released-as VERSION \\
                                    --confirm "DEPLOY P-.."
    python -m app.learning reject   --id P-.. --by NAME --reason R
    python -m app.learning rollback --id P-.. --by NAME --reason R --confirm "ROLLBACK P-.."
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from app.config import load_settings
from app.utils.market_calendar import MarketCalendar


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.learning")
    ap.add_argument("command", choices=["ingest", "outcomes", "review", "annotate", "correct",
                                        "analyse", "proposals", "test-plan", "test-result",
                                        "approve", "deploy", "reject", "rollback"])
    ap.add_argument("--source", type=Path)
    ap.add_argument("--label", default="ai-paper-arm")
    ap.add_argument("--data", type=Path)
    ap.add_argument("--at", type=datetime.fromisoformat)
    ap.add_argument("--record")
    ap.add_argument("--category")
    ap.add_argument("--field")
    ap.add_argument("--value")
    ap.add_argument("--reason", default="")
    ap.add_argument("--by", default="")
    ap.add_argument("--id")
    ap.add_argument("--from", dest="start", type=date.fromisoformat)
    ap.add_argument("--to", dest="end", type=date.fromisoformat)
    ap.add_argument("--dataset")
    ap.add_argument("--run")
    ap.add_argument("--confirm")
    ap.add_argument("--released-as", dest="released_as", default="")
    args = ap.parse_args(argv)

    from app.learning import proposals as P
    from app.learning.dataset import LearningStore

    settings = load_settings()
    cal = MarketCalendar(settings.market)
    now = datetime.now(cal.tz)
    ls = LearningStore(settings.learning.db_path)
    print("LEARNING PIPELINE (analysis and proposals only; never changes the active system).")
    cmd = args.command
    if cmd == "ingest":
        from app.learning.dataset import ingest

        src = args.source or settings.integrated.ai_db_path
        if not Path(src).exists():
            print(f"PENDING: no agent database at {src}")
            return 0
        print(json.dumps(ingest(ls, src, args.label, settings, now), indent=2))
    elif cmd == "outcomes":
        from app.learning.dataset import compute_outcomes
        from app.paper.store import PaperStore

        if args.data:
            from app.backtesting.dataset import load_csv_dataset

            provider = load_csv_dataset(args.data, settings, cal).provider(cal)
        else:
            from app.data.factory import build_provider

            provider = build_provider(settings, cal)
        stores = {r["source_id"]: PaperStore(r["path"]) for r in
                  ls.all("SELECT * FROM learn_sources") if Path(r["path"]).exists()}
        at = args.at.replace(tzinfo=cal.tz) if args.at and args.at.tzinfo is None else args.at
        print(json.dumps(compute_outcomes(ls, provider, settings, cal, at or now, stores)))
    elif cmd == "review":
        from app.learning.review import review_all

        print(json.dumps(review_all(ls, settings, now)))
    elif cmd == "annotate":
        from app.learning.review import annotate

        annotate(ls, args.record, args.category, args.reason, args.by, now)
        print("annotation recorded (append-only)")
    elif cmd == "correct":
        ls.correct(args.record, args.field, args.value, args.reason, args.by, now)
        print("correction recorded as an append-only event; the original is unchanged")
    elif cmd == "analyse":
        from app.learning.analysis import analyse, proposals_from_findings
        from app.learning.report import write

        created = proposals_from_findings(ls, analyse(ls, settings), now)
        print(f"proposals created: {created or 'none'}")
        print("Report:", write(ls, settings))
    elif cmd == "proposals":
        if args.id:
            print(json.dumps(P.get(ls, args.id), indent=2, default=str))
        else:
            for r in ls.all("SELECT proposal_id, status, evidence_level, component, issue "
                            "FROM proposals ORDER BY created_at"):
                print(f"{r['proposal_id']} {r['status']:11} {r['evidence_level']:19} "
                      f"{r['component']}: {r['issue'][:90]}")
    elif cmd == "test-plan":
        P.start_test(ls, settings, args.id, args.start, args.end, args.dataset or "", args.by,
                     now)
        print(f"{args.id} UNDER_TEST")
    elif cmd == "test-result":
        print(f"{args.id}: {P.record_test(ls, settings, args.id, args.run, now)}")
    elif cmd == "approve":
        P.approve(ls, args.id, args.by, args.reason, args.confirm, now)
        print(f"{args.id} APPROVED by {args.by}. Nothing has been changed: build the change "
              "as a new version and record it with `deploy`.")
    elif cmd == "deploy":
        P.deploy(ls, args.id, args.by, args.reason, args.confirm, args.released_as, now)
        print(f"{args.id} DEPLOYED as {args.released_as} (recorded only)")
    elif cmd == "reject":
        P.reject(ls, args.id, args.by, args.reason, now)
        print(f"{args.id} REJECTED (kept for the record)")
    elif cmd == "rollback":
        P.rollback(ls, args.id, args.by, args.reason, args.confirm, now)
        print(f"{args.id} ROLLED_BACK (recorded)")
    return 0


def main() -> int:
    from app.learning.proposals import ProposalError

    try:
        return run()
    except (ProposalError, ValueError, KeyError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
