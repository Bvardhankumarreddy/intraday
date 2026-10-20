"""Collect actual evidence from the repository and persisted state (Phase 9).

Everything is read-only. Counts come from the databases and files as they
exist; missing evidence is reported as missing, never filled in. Test results
come from running pytest now, not from the existence of test files.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

from app.config import PROJECT_ROOT, Settings

# Phase -> (capabilities, test files whose results count as its evidence,
#           known outstanding issues). Results are filled in from the run.
PHASES = [
    ("1", "Market-data foundation: config, providers (mock/CSV/memory/Groww), validation, "
          "calendar, logging",
     ["test_config.py", "test_providers.py", "test_data_quality.py", "test_market_calendar.py",
      "test_logging.py", "test_groww_provider.py"],
     "Groww adapter never observed during market hours; NSE holiday list covers 2026 only"),
    ("2", "Screening, indicators, ORB signals, signal states, ranking",
     ["test_screening.py", "test_indicators.py", "test_strategy.py", "test_ranking.py"],
     "Live universe (config/universe.csv) is empty"),
    ("3", "Groww cost model, break-even, sizing, daily risk limits, review status",
     ["test_transaction_costs.py", "test_break_even.py", "test_position_sizing.py",
      "test_risk_limits.py", "test_risk_engine.py"],
     "Fee rounding per order is an assumption (Groww publishes no rounding rule)"),
    ("4", "Backtesting engine, execution model candle-v1, metrics, reports",
     ["test_backtesting.py", "test_backtest_metrics.py", "test_backtest_dataset.py"],
     "Never run on real historical data; Phase 4 walk_forward() auto-selects (unused by "
     "Phase 7)"),
    ("5", "Paper engine, order lifecycle, SQLite persistence, scheduler, dashboard",
     ["test_paper_engine.py", "test_paper_scheduler.py"],
     "No live session has run; local launchd automation currently not installed"),
    ("6", "Live evaluation: sessions, data-quality ledger, signal audit, reconciliation, "
          "reports",
     ["test_evaluation.py"],
     "Zero evaluations/sessions recorded; 9 test-artifact folders in data/evaluation"),
    ("7", "Robustness validation: frozen baseline, walk-forward, sensitivity, statistics",
     ["test_robustness.py"],
     "Empirical verdict PENDING_DATA: no historical data fetched"),
    ("8", "Hardening: paper-only boundary, resilience, recovery, reconciliation, health, "
          "alerts, backups, release gate",
     ["test_paper_only.py", "test_failure_injection.py", "test_ops.py"],
     "Lint/type/dependency-audit tools not installed (gate SKIPPED); no notification "
     "provider; no git"),
    ("9", "End-to-end integration audit",
     ["test_e2e_integration.py"], ""),
    ("10", "AI research agent: LLM provider, read-only tools, bounded workflow, immutable "
           "recommendations, A/B comparison",
     ["test_agent.py"],
     "Live LLM verified 2026-10-20 (check-llm + opt-in live test, claude-haiku-5-5); "
     "cost tracking off (prices not configured); no verified news source"),
    ("11", "AI evaluation: predeclared protocol, immutable decision ledger, historical replay "
           "(A vs B on the unchanged engine), prospective comparison, decision quality, "
           "paired bootstrap, report and dashboard",
     ["test_aieval.py"],
     "Empirical result PENDING_DATA: no historical data and no live paper session; model "
     "prices and USD/INR not configured (model cost in INR not computed)"),
    ("12", "Integrated paper trading: baseline and AI-assisted paper arms side by side on one "
           "data snapshot per step, AI gate (veto only), isolation and fallback, daily "
           "integrated report, cumulative comparison, dashboard",
     ["test_integrated.py"],
     "0 of 20-30 required live observation sessions recorded: config/universe.csv empty, no "
     "integrated evaluation started; Rs 500 rejects the synthetic setup on costs"),
    ("13", "Learning pipeline: immutable decision/outcome dataset, horizon outcomes, rule "
           "review (process vs outcome), aggregates and recurring errors, proposals with human "
           "approval and selection-bias guards, report, dashboard",
     ["test_learning.py"],
     "No real recommendations to learn from yet (0 live sessions); interpretation errors need "
     "human annotation"),
    ("14", "Experiments and promotion: content-addressed versions, hash-chained registry, "
           "three-arm leakage-resistant validation, acceptance rules, PASS/FAIL/INCONCLUSIVE/"
           "INVALID, shadow, paper canary, human-confirmed activation and rollback",
     ["test_experiments.py"],
     "Registry not initialised on this machine (a person must run `init`); no real-data "
     "experiment; strategy-changing candidates need a new frozen baseline"),
    ("15", "Monitoring: data, calendar, AI, execution, version and storage checks; drift "
           "detection (data/performance/possible concept); deduplicated alerts with audit "
           "history; safe degraded states with pause gates; LLM circuit breaker; dashboard",
     ["test_monitoring.py"],
     "No external alert channel (local outbox + log only); never observed on a live session; "
     "no web authentication (CLI controls only)"),
]


def run_pytest() -> dict:
    cmd = [sys.executable, "-m", "pytest", "-q", "-rA", "-o", "addopts=", "-p",
           "no:cacheprovider"]
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=3600)
    per_file: dict[str, Counter] = {}
    for line in proc.stdout.splitlines():
        m = re.match(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) tests/([\w./]+?)::", line)
        if m:
            per_file.setdefault(m.group(2), Counter())[m.group(1)] += 1
    summary = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    failures = [l for l in proc.stdout.splitlines() if l.startswith(("FAILED", "ERROR"))]
    return {"command": " ".join(["python"] + cmd[1:]), "exit_code": proc.returncode,
            "summary": summary, "per_file": {k: dict(v) for k, v in per_file.items()},
            "failures": failures[:50]}


def audit_matrix(pytest_result: dict) -> list[dict]:
    rows = []
    for phase, caps, files, issues in PHASES:
        counts = Counter()
        for f in files:
            counts.update(pytest_result["per_file"].get(f, {}))
        missing = [f for f in files if f not in pytest_result["per_file"]]
        ran = sum(counts.values())
        result = "FAIL" if counts["FAILED"] or counts["ERROR"] else \
            ("NOT RUN" if not ran else "PASS")
        rows.append({"phase": phase, "capabilities": caps, "tests": "; ".join(files),
                     "tests_passed": counts["PASSED"],
                     "tests_failed": counts["FAILED"] + counts["ERROR"],
                     "test_files_not_run": "; ".join(missing), "result": result,
                     "outstanding": issues})
    return rows


def _ro(path: Path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def paper_evidence(path: Path, label: str) -> dict:
    path = Path(path)
    if not path.exists():
        return {"database": str(path), "label": label, "status": "UNAVAILABLE",
                "reason": "database does not exist"}
    conn = _ro(path)
    conn.row_factory = sqlite3.Row
    t = _tables(conn)

    def count(sql, default=0):
        try:
            return conn.execute(sql).fetchone()[0]
        except sqlite3.Error:
            return default

    out = {"database": str(path.relative_to(PROJECT_ROOT)) if path.is_relative_to(PROJECT_ROOT)
           else str(path), "label": label, "status": "OK"}
    if not t:
        return {**out, "status": "EMPTY", "reason": "no tables (never used)"}
    out["evaluations"] = [dict(r) for r in conn.execute(
        "SELECT evaluation_id, name, start_date, frozen_config_version, active FROM "
        "evaluations")] if "evaluations" in t else []
    if "eval_sessions" in t:
        out["eval_sessions_by_kind_status"] = {
            f"{r['kind']}/{r['status']}": r["n"] for r in conn.execute(
                "SELECT kind, status, COUNT(*) AS n FROM eval_sessions GROUP BY kind, status")}
        out["primary_live_sessions"] = count(
            "SELECT COUNT(*) FROM eval_sessions WHERE kind = 'LIVE' AND primary_eval = 1")
    out["paper_sessions_by_state"] = {r["state"]: r["n"] for r in conn.execute(
        "SELECT state, COUNT(*) AS n FROM sessions GROUP BY state")} if "sessions" in t else {}
    out["sessions_with_trades"] = count(
        "SELECT COUNT(DISTINCT trading_date) FROM positions WHERE state = 'CLOSED'")
    out["signals_total"] = count("SELECT COUNT(*) FROM signals")
    out["signals_rejected"] = count(
        "SELECT COUNT(*) FROM signals WHERE review_status != 'QUALIFIED_FOR_REVIEW'")
    out["orders_by_side_state"] = {f"{r['side']}/{r['state']}": r["n"] for r in conn.execute(
        "SELECT side, state, COUNT(*) AS n FROM orders GROUP BY side, state")} \
        if "orders" in t else {}
    closed = conn.execute("SELECT gross_pnl, total_costs, net_pnl FROM positions WHERE "
                          "state = 'CLOSED'").fetchall() if "positions" in t else []
    out["closed_positions"] = len(closed)
    out["open_or_flagged_positions"] = count(
        "SELECT COUNT(*) FROM positions WHERE state != 'CLOSED'")
    if closed:
        from decimal import Decimal
        out["gross_pnl"] = str(sum(Decimal(r["gross_pnl"]) for r in closed))
        out["total_costs"] = str(sum(Decimal(r["total_costs"]) for r in closed))
        out["net_pnl"] = str(sum(Decimal(r["net_pnl"]) for r in closed))
    out["data_quality_incidents"] = count(
        "SELECT COUNT(*) FROM dq_batches WHERE stale = 1 OR error IS NOT NULL OR missing > 0")
    out["reconciliation_issues"] = count("SELECT COUNT(*) FROM reconciliation_issues")
    out["report_generation_failures"] = count(
        "SELECT COUNT(*) FROM events WHERE kind = 'REPORT' AND level = 'ERROR'")
    out["recovery_events"] = count("SELECT COUNT(*) FROM events WHERE kind = 'RECOVERY'")
    out["open_alerts"] = count("SELECT COUNT(*) FROM alerts WHERE status = 'OPEN'")
    conn.close()
    return out


def evaluation_folders(settings: Settings) -> dict:
    root = Path(settings.evaluation.output_dir)
    folders = sorted(p for p in root.glob("EVAL-*") if p.is_dir()) if root.exists() else []
    artifacts = []
    for f in folders:
        kinds = set()
        for j in f.glob("daily/*.json"):
            try:
                body = json.loads(j.read_text())
                for s in body.get("1_session_summary", []):
                    kinds.add((s.get("provider"), s.get("primary_eval")))
            except (OSError, ValueError):
                pass
        artifacts.append({"folder": f.name, "sessions": sorted(str(k) for k in kinds)})
    test_like = [a for a in artifacts if all("memory" in s for s in a["sessions"])
                 and a["sessions"]]
    return {"folders": len(folders), "test_artifacts_memory_provider": len(test_like),
            "detail": artifacts}


def historical_evidence(settings: Settings) -> dict:
    hist = Path(settings.data.historical_dir)
    bt = Path(settings.backtest.output_dir)
    rob = Path(settings.robustness.output_dir)
    runs = []
    for summary in sorted(rob.glob("*/*/summary.json")) if rob.exists() else []:
        body = json.loads(summary.read_text())
        runs.append({"report": str(summary.parent.relative_to(PROJECT_ROOT)),
                     "status": body.get("status"),
                     "verdict": body.get("assessment", {}).get("verdict"),
                     "missing": body.get("missing")})
    return {"historical_data_dir_exists": hist.exists(),
            "historical_5m_files": len(list(hist.glob("*_5m.csv"))) if hist.exists() else 0,
            "backtest_runs": len([p for p in bt.iterdir() if p.is_dir()]) if bt.exists() else 0,
            "robustness_runs": runs}


def universe_evidence(settings: Settings) -> dict:
    path = Path(settings.data.universe_file)
    rows = [l for l in path.read_text().splitlines()[1:] if l.strip() and not
            l.startswith("#")] if path.exists() else []
    return {"file": str(path.relative_to(PROJECT_ROOT)), "symbols": len(rows)}


def automation_evidence() -> dict:
    agents = Path.home() / "Library" / "LaunchAgents"
    names = ("com.nseintraday.evaluation", "com.nseintraday.dashboard")
    return {n: (agents / f"{n}.plist").exists() for n in names}
