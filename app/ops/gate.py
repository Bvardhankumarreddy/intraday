"""Release and regression gate (``python -m app.ops gate``).

Each check has a result:
- PASS: the check ran and passed.
- FAIL: the check ran and failed. Blocking checks stop the release.
- WARN: a non-blocking concern.
- SKIPPED: the check did NOT run (tool missing, or skipped by option). It is
  never reported as passed.

The release is blocked if any blocking check FAILs. Blocking checks are
also listed as SKIPPED when skipped on purpose (``--skip-tests``); the
summary then says "NOT RELEASABLE: blocking checks skipped". Justified
exceptions are documented in docs/runbooks.md.
"""

from __future__ import annotations

import compileall
import json
import re
import sqlite3
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

from app.config import PROJECT_ROOT

SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", "node_modules"}
BINARY = {".db", ".sqlite", ".png", ".jpg", ".pdf", ".pyc", ".whl", ".zip", ".gz"}
PATTERNS = {
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "env_credential": re.compile(
        r"NSE_(?:BROKER_API_KEY|BROKER_API_SECRET|BROKER_TOTP_SECRET|LLM_API_KEY)\s*=\s*"
        r"(?![<#\s]|$)\S+"),
    "assigned_secret": re.compile(
        r"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*[\"'][A-Za-z0-9/+_=-]{20,}[\"']"),
}
REQUIRED_DOCS = ["README.md", "docs/runbooks.md", "docs/security.md", "docs/phase8_audit.md",
                 "docs/final_readiness_report.md", "docs/ai_agent.md",
                 "docs/ai_evaluation.md", "docs/phase11_audit.md",
                 "docs/integrated_trading.md", "docs/phase12_report.md",
                 "docs/learning.md", "docs/experiments.md", "docs/monitoring.md",
                 "docs/architecture.md", ".env.example"]


def _result(name: str, status: str, detail: str, blocking: bool) -> dict:
    return {"check": name, "status": status, "detail": detail, "blocking": blocking}


def _tool(name: str) -> str | None:
    local = Path(sys.executable).parent / name
    return str(local) if local.exists() else shutil.which(name)


def _run(cmd: list[str], timeout: int = 1800) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True,
                          timeout=timeout)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _tail(text: str, n: int = 3) -> str:
    return " | ".join(text.splitlines()[-n:])


def check_compile() -> dict:
    ok = all(compileall.compile_dir(PROJECT_ROOT / d, quiet=1, force=False)
             for d in ("app", "tests"))
    return _result("compile", "PASS" if ok else "FAIL", "all modules byte-compile" if ok
                   else "syntax errors (see output)", True)


def check_pytest(name: str, args: list[str], skip: bool) -> dict:
    if skip:
        return _result(name, "SKIPPED", "skipped by option (not run)", True)
    code, out = _run([sys.executable, "-m", "pytest", "-q", "-o", "addopts=",
                      "-p", "no:cacheprovider", *args])
    return _result(name, "PASS" if code == 0 else "FAIL", _tail(out, 1), True)


def check_optional_tool(name: str, tool: str, args: list[str]) -> dict:
    exe = _tool(tool)
    if exe is None:
        return _result(name, "SKIPPED", f"{tool} not installed (pip install -r "
                                        "requirements-dev.txt); check NOT run", False)
    code, out = _run([exe, *args])
    return _result(name, "PASS" if code == 0 else "FAIL", _tail(out), False)


def check_dependencies() -> list[dict]:
    out = []
    code, text = _run([sys.executable, "-m", "pip", "check"])
    out.append(_result("pip_check", "PASS" if code == 0 else "FAIL", _tail(text, 1), True))
    lock = PROJECT_ROOT / "requirements.lock"
    if not lock.exists():
        out.append(_result("lock_file", "WARN", "requirements.lock missing", False))
    else:
        code, frozen = _run([sys.executable, "-m", "pip", "freeze", "--exclude-editable"])
        want = {l.strip().lower() for l in lock.read_text().splitlines()
                if l.strip() and not l.startswith("#")}
        have = {l.strip().lower() for l in frozen.splitlines() if l.strip()}
        diff = sorted(want ^ have)
        out.append(_result("lock_file", "PASS" if not diff else "WARN",
                           "installed packages match requirements.lock" if not diff
                           else f"{len(diff)} difference(s): {diff[:6]}", False))
    out.append(check_optional_tool("dependency_audit", "pip-audit",
                                   ["-r", "requirements.lock", "--progress-spinner", "off"]))
    return out


def _scan_files(root: Path):
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or not path.is_file():
            continue
        if path.name == ".env" or path.suffix.lower() in BINARY or \
                path.stat().st_size > 5_000_000:
            continue
        yield path


def secret_scan(root: Path = PROJECT_ROOT, secrets: list[str] | None = None) -> dict:
    """Scan source, docs, config, logs and reports. .env is excluded (it is the
    only allowed home for secrets). Values are never printed."""
    if secrets is None:
        from app.config import load_settings

        s = load_settings()
        secrets = [v.get_secret_value() for v in (s.broker_api_key, s.broker_api_secret,
                                                   s.broker_totp_secret, s.llm_api_key) if v]
    secrets = [x for x in secrets if len(x) >= 6]
    findings = []
    for path in _scan_files(root):
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        rel = str(path.relative_to(root))
        for value in secrets:
            if value in text:
                findings.append(f"{rel}: contains a configured secret value")
        for name, rx in PATTERNS.items():
            for line in text.splitlines():
                if rx.search(line) and "gate: allow-secret" not in line:
                    findings.append(f"{rel}: {name} pattern")
                    break
    return _result("secret_scan", "PASS" if not findings else "FAIL",
                   f"no secrets found in scanned files ({len(secrets)} configured value(s) "
                   "checked)" if not findings else "; ".join(sorted(set(findings))[:10]), True)


def check_config() -> list[dict]:
    out = []
    try:
        from app.config import load_settings

        s = load_settings()
        out.append(_result("config", "PASS" if s.execution_mode == "paper" else "FAIL",
                           f"settings valid; execution mode {s.execution_mode}", True))
    except Exception as exc:  # noqa: BLE001
        return [_result("config", "FAIL", f"invalid settings: {exc}", True)]
    env = PROJECT_ROOT / ".env"
    if env.exists():
        mode = stat.S_IMODE(env.stat().st_mode)
        out.append(_result("env_permissions", "PASS" if mode & 0o077 == 0 else "FAIL",
                           f".env mode {oct(mode)} (must be 600)", True))
    gi = PROJECT_ROOT / ".gitignore"
    ignored = gi.exists() and ".env" in gi.read_text().split()
    out.append(_result("gitignore", "PASS" if ignored else "FAIL",
                       ".env excluded from version control" if ignored else
                       ".env not in .gitignore", True))
    try:
        from app.robustness.baseline import load_frozen

        files = sorted(Path(s.robustness.baselines_dir).glob("*.json"))
        drift = [f.name for f in files if load_frozen(f).code_drift]
        out.append(_result("frozen_baseline", "FAIL" if drift else "PASS",
                           f"decision-path code changed vs {drift}: that is a new strategy "
                           "version (freeze a new baseline)" if drift else
                           f"{len(files)} frozen baseline(s) verified, no code drift", True))
    except Exception as exc:  # noqa: BLE001
        out.append(_result("frozen_baseline", "FAIL", f"cannot verify: {exc}", True))
    return out


def check_db_migration() -> dict:
    """Open a COPY of the live paper database with this code (never the
    original): schema upgrade, integrity and reconciliation must pass."""
    from app.config import load_settings
    from app.costs.schedule import load_fee_schedule
    from app.ops.reconciliation import full_reconciliation
    from app.paper.store import PaperStore, StoreError

    s = load_settings()
    db = Path(s.paper.db_path)
    if not db.exists():
        return _result("db_migration", "SKIPPED", f"no database at {db}; nothing to migrate",
                       True)
    with tempfile.TemporaryDirectory(prefix="gate-db-") as tmp:
        copy = Path(tmp) / "copy.db"
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dst = sqlite3.connect(copy)
        src.backup(dst)
        src.close()
        dst.close()
        try:
            store = PaperStore(copy)
        except StoreError as exc:
            return _result("db_migration", "FAIL", str(exc), True)
        try:
            rec = full_reconciliation(store, s, load_fee_schedule(s), date.today())
            bad = rec["counts"]["CRITICAL"] + rec["counts"]["ERROR"]
            return _result("db_migration", "FAIL" if bad else "PASS",
                           f"copy opened at schema v{store.schema_version()}; reconciliation "
                           f"{rec['status']} {rec['counts']}", True)
        finally:
            store.close()


def check_docs() -> dict:
    missing = [d for d in REQUIRED_DOCS if not (PROJECT_ROOT / d).exists()]
    broken = []
    for md in [PROJECT_ROOT / "README.md", *(PROJECT_ROOT / "docs").glob("*.md")]:
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", md.read_text()):
            if "://" in target or target.startswith("mailto:"):
                continue
            if not (md.parent / target).exists():
                broken.append(f"{md.name} -> {target}")
    ok = not missing and not broken
    return _result("docs", "PASS" if ok else "WARN",
                   "required docs present, relative links resolve" if ok else
                   f"missing {missing}; broken links {broken[:8]}", False)


def run_gate(skip_tests: bool = False) -> dict:
    results = [check_compile()]
    results.append(check_pytest("tests_full", [], skip_tests))
    results.append(check_pytest("paper_only_safety",
                                ["tests/test_paper_only.py", "tests/test_paper_scheduler.py::"
                                 "test_no_live_order_code_anywhere",
                                 "tests/test_agent.py::"
                                 "test_agent_has_no_shell_filesystem_or_broker_capability",
                                 "tests/test_agent.py::"
                                 "test_no_order_endpoint_called_through_groww_adapter",
                                 "tests/test_agent.py::test_rs500_rule_rejects_even_if_ai_says_"
                                 "consider",
                                 "tests/test_aieval.py::test_no_real_broker_order_path",
                                 "tests/test_aieval.py::test_ai_cannot_bypass_risk_rejection",
                                 "tests/test_aieval.py::test_evaluation_changes_no_parameters",
                                 "tests/test_integrated.py::"
                                 "test_no_real_order_endpoint_in_integration_code",
                                 "tests/test_integrated.py::"
                                 "test_rs500_risk_rejection_applies_to_both_arms",
                                 "tests/test_integrated.py::"
                                 "test_ai_arm_failures_are_isolated_from_the_baseline",
                                 "tests/test_learning.py::"
                                 "test_learning_has_no_llm_broker_or_config_write_path",
                                 "tests/test_learning.py::"
                                 "test_unapproved_or_failed_proposal_cannot_become_active",
                                 "tests/test_learning.py::"
                                 "test_active_configuration_never_changes",
                                 "tests/test_experiments.py::test_no_real_order_path_in_experiments",
                                 "tests/test_experiments.py::"
                                 "test_registry_content_is_immutable_and_tampering_detected",
                                 "tests/test_experiments.py::test_weakened_risk_control_is_invalid",
                                 "tests/test_monitoring.py::"
                                 "test_no_order_or_parameter_change_path_in_monitoring",
                                 "tests/test_monitoring.py::"
                                 "test_monitoring_never_modifies_trading_records",
                                 "tests/test_monitoring.py::"
                                 "test_llm_circuit_breaker_bounds_calls_and_isolates_baseline"],
                                skip_tests))
    results.append(check_pytest("recovery_and_reconciliation",
                                ["tests/test_failure_injection.py", "tests/test_ops.py",
                                 "tests/test_integrated.py::"
                                 "test_restart_with_open_positions_recovers_both_arms",
                                 "tests/test_integrated.py::"
                                 "test_financial_reconciliation_of_both_portfolios",
                                 "tests/test_monitoring.py::test_states_pause_recover_and_restart",
                                 "tests/test_monitoring.py::"
                                 "test_monitoring_pause_blocks_new_entries_only"],
                                skip_tests))
    results.append(check_optional_tool("lint", "ruff", ["check", "app", "tests"]))
    results.append(check_optional_tool("type_check", "mypy", ["app", "--ignore-missing-imports"]))
    results += check_dependencies()
    results.append(secret_scan())
    results += check_config()
    results.append(check_db_migration())
    results.append(check_docs())
    blocking_fail = [r["check"] for r in results if r["blocking"] and r["status"] == "FAIL"]
    blocking_skip = [r["check"] for r in results if r["blocking"] and r["status"] == "SKIPPED"
                     and r["check"] != "db_migration"]
    if blocking_fail:
        verdict = f"BLOCKED: {blocking_fail}"
    elif blocking_skip:
        verdict = f"NOT RELEASABLE: blocking checks skipped {blocking_skip}"
    else:
        verdict = "RELEASABLE (warnings and skipped optional tools listed above)"
    return {"ran_at": datetime.now().isoformat(timespec="seconds"), "verdict": verdict,
            "results": results}


def format_gate(report: dict) -> str:
    lines = [f"{'check':28} {'status':8} {'blocking':8} detail"]
    for r in report["results"]:
        lines.append(f"{r['check']:28} {r['status']:8} {'yes' if r['blocking'] else 'no':8} "
                     f"{r['detail']}")
    lines.append("")
    lines.append(f"VERDICT: {report['verdict']}")
    return "\n".join(lines)


def dump(report: dict) -> str:
    return json.dumps(report, indent=2)

