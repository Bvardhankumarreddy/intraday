"""Phase 9 readiness evidence (read-only; never places orders).

    python -m app.readiness [--skip-gate] [--out DIR]

Runs the full test suite (per-phase results), the release gate (unless
--skip-gate), the Rs 500 feasibility analysis with the frozen baseline, and
collects persisted live/replay/historical evidence. Writes to
``data/readiness/<date>/``:

    readiness_summary.json   everything below, with commands and exit codes
    audit_matrix.csv         per phase: capabilities, tests run, result, issues
    checks.csv               release-gate checks with status (SKIPPED = not run)
    feasibility_costs.csv    round-trip costs and break-even by entry price
    feasibility_setups.csv   engine verdict for each hypothetical setup at Rs 500
    feasibility_scaling.csv  INFORMATIONAL: same rules at larger capitals

The narrative report is docs/final_readiness_report.md. Files are written
with ``write_preserving``, so an earlier version with the same name is
archived, never lost.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from app.config import PROJECT_ROOT, load_settings
from app.readiness import evidence
from app.readiness.feasibility import analyse
from app.utils.fs import private_dir, write_preserving
from app.utils.market_calendar import MarketCalendar


def _csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    pd.DataFrame(rows).to_csv(buf, index=False)
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.readiness")
    ap.add_argument("--skip-gate", action="store_true")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    settings = load_settings()
    tz = MarketCalendar(settings.market).tz
    now = datetime.now(tz)
    out = private_dir(args.out or PROJECT_ROOT / "data" / "readiness" / now.date().isoformat())

    print("running test suite ...", file=sys.stderr)
    tests = evidence.run_pytest()
    matrix = evidence.audit_matrix(tests)
    gate = None
    if not args.skip_gate:
        print("running release gate ...", file=sys.stderr)
        proc = subprocess.run([sys.executable, "-m", "app.ops", "gate", "--json"],
                              cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=3600)
        gate = {"command": "python -m app.ops gate --json", "exit_code": proc.returncode,
                **json.loads(proc.stdout)}
    from app.robustness.baseline import load_frozen

    frozen = load_frozen(PROJECT_ROOT / "config" / "baselines" / "orb-baseline-v1.json")
    feas = analyse(frozen.settings, now.replace(hour=9, minute=45, second=0, microsecond=0))
    from app.ops.health import health_report

    summary = {
        "generated_at": now.isoformat(),
        "statement": "No real orders were placed during this review. All execution is "
                     "simulated (paper).",
        "tests": tests, "audit_matrix": matrix, "release_gate": gate,
        "frozen_baseline": {"file": "config/baselines/orb-baseline-v1.json",
                            "fingerprint": frozen.fingerprint, "code_drift": frozen.code_drift,
                            "protocol": frozen.protocol.protocol_version},
        "live_paper": evidence.paper_evidence(settings.paper.db_path, "LIVE paper database"),
        "replay_paper": evidence.paper_evidence(settings.paper.replay_db_path,
                                                "REPLAY database (not live evidence)"),
        "evaluation_folders": evidence.evaluation_folders(settings),
        "historical": evidence.historical_evidence(settings),
        "universe": evidence.universe_evidence(settings),
        "local_automation_installed": evidence.automation_evidence(),
        "health_now": health_report(settings, now),
        "feasibility": {k: v for k, v in feas.items()
                        if k not in ("cost_table", "setups", "informational_scaling_grid")},
    }
    write_preserving(out / "readiness_summary.json",
                     json.dumps(summary, indent=2, default=str, sort_keys=True) + "\n")
    write_preserving(out / "audit_matrix.csv", _csv(matrix))
    if gate:
        write_preserving(out / "checks.csv", _csv(gate["results"]))
    write_preserving(out / "feasibility_costs.csv", _csv(feas["cost_table"]))
    write_preserving(out / "feasibility_setups.csv", _csv(feas["setups"]))
    write_preserving(out / "feasibility_scaling.csv", _csv(feas["informational_scaling_grid"]))
    private_dir(out.parent)
    for f in out.iterdir():
        if f.is_file():
            f.chmod(0o600)  # owner-only, like every other Phase 8 output
    print(f"tests: {tests['summary']} (exit {tests['exit_code']})")
    if gate:
        print(f"gate: {gate['verdict']}")
    print(f"feasibility at Rs {feas['capital']}: {feas['setups_qualified']} of "
          f"{feas['setups_evaluated']} hypothetical setups qualified ({feas['status_counts']})")
    print(f"live evaluations: {len(summary['live_paper'].get('evaluations', []))}; "
          f"universe symbols: {summary['universe']['symbols']}")
    print(f"written to {out}")
    return 0 if tests["exit_code"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
