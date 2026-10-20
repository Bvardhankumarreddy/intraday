# Operational runbooks

All commands run from the project folder with the project's venv:

```bash
cd nse-intraday-agent
PY=.venv/bin/python
```

The application is **research and paper trading only**. No procedure here
places, modifies or cancels a real order, and none closes paper positions to
"tidy up". Never put real credentials into these documents or into tickets.

**Health exit codes:**

| Command | 0 | 1 | 2 |
|---|---|---|---|
| `app.ops health` / `preflight` | HEALTHY | DEGRADED | UNHEALTHY |
| `app.ops gate` | RELEASABLE | not releasable | — |

---

## 1. Installation and secure configuration

**Prerequisites:**
- Python 3.13.
- Groww Trading API subscription (only for live data).

**Steps:**
1. Create the environment and install the locked dependencies:
   `python3.13 -m venv .venv && .venv/bin/pip install -r requirements.lock`.
2. Configure credentials:
   `cp .env.example .env && chmod 600 .env`, then fill in the placeholders.
   Never commit `.env`.
3. Optional settings: copy `config/settings.example.yaml` to
   `config/settings.yaml`. Unknown keys are rejected. Secrets are not
   allowed in YAML.
4. Harden permissions: `$PY -m app.ops harden`.

**Verification:**
- `$PY -m app.ops health` reports config PASS and permissions PASS.
- `$PY -m pytest` passes.

**Rollback:** delete `.venv` and re-create it. Configuration files are
untouched.

## 2. Pre-session health check (before 09:15 IST)

**Steps:** `$PY -m app.ops preflight`. This is a health check plus one
read-only provider call.

**Verification:** every check is PASS, except:
- `provider_probe` may say "data rejected … expected outside market hours";
- `session` may say "no session today" before the run starts.

**Escalation:**
- config FAIL → fix the setting (§13 for credentials).
- database FAIL → §8 / §9.
- `provider_probe` FAIL → §5.
- reconciliation FAIL → §7.
- **Do not start a session while health is UNHEALTHY.**

## 3. Starting and stopping

**Prerequisites:**
- An evaluation is started (`$PY -m app.evaluation start --name ...`).
- Preflight is clean.

**Start:** `$PY -m app.evaluation run`. Locally, launchd starts it at 08:55
on weekdays. Only one scheduler can run at a time; a second one exits with
`AlreadyRunning`.

**Stop:**
- Send SIGTERM (`kill <pid>`), press Ctrl+C, or use
  `launchctl kill TERM gui/$(id -u)/com.nseintraday.evaluation`.
- The scheduler stops **before its next step**, never mid-step.
- It records the session as INTERRUPTED, raises a WARNING alert and exits
  with code 3.
- Paper positions and pending orders stay exactly as persisted. They are
  **not** closed.

**Verification:** `$PY -m app.ops health` → session shows "interrupted";
`$PY -m app.paper status`.

**Resume:** run the start command again the same day. The session resumes,
catches up to the latest step and evaluates exits from real data. A session
that was not resumed is reported INCOMPLETE in the evaluation, never as a
quiet zero.

## 4. Monitoring data freshness

- **During the session:** `$PY -m app.ops health`. The `market_data` check
  shows:
  - fresh symbols at the last step;
  - provider state;
  - circuit state;
  - missing, invalid and stale candles at the latest step.
- **Dashboard:** `streamlit run app/dashboard.py`, Operations view. This
  makes no provider calls.
- **Stalled session:** `session` FAIL means "stalled" (no step for 15
  minutes). Check the process (§5), then restart (§3).
- **What happens when data is bad:**
  - when no symbol has fresh, valid data, **new signals are blocked** and
    the `data.blocked` ERROR alert opens; it resolves itself when data
    returns;
  - a position whose candles stop is FLAGGED. No exit is ever assumed.

## 5. Diagnosing provider failures

**Prerequisites:** `$PY -m app.ops health --json`, plus the logs in
`data/logs/` (JSON lines; secrets redacted).

| Symptom | Likely cause | Action |
|---|---|---|
| `provider_probe` FAIL "Groww login failed (…Authentication…)" | wrong, expired or revoked credentials | §13 rotation; check the subscription is active |
| "…Authorisation…" | subscription lapsed or no permission | renew the Groww Trading API subscription |
| circuit OPEN, "RateLimit" errors | over 10 requests/s or 300/min | reduce the universe; raise `data.min_seconds_between_calls`; the circuit probes again after the cooldown by itself |
| timeouts | network or Groww outage | wait; the circuit fails fast during the cooldown; health shows the recovery time when it ends |
| "data rejected … stale" during market hours | delayed feed | check Groww status; signals stay blocked (correct behaviour) |

**Escalation:** if the provider is unavailable for the whole session, the
evaluation marks it INCOMPLETE automatically. Do not substitute other
data.

## 6. Recovering after a crash

1. `$PY -m app.ops health`. A crash leaves the scheduler state RUNNING
   without new heartbeats, which health reports as `stalled`.
2. `$PY -m app.ops reconcile`. Expect no CRITICAL or ERROR findings: every
   financial change is one SQLite transaction, so a crash leaves either the
   whole change or none of it.
3. Restart (§3). Start-up validates the persisted state first. On a
   CRITICAL inconsistency it refuses to run (`PaperStateError`) and raises a
   CRITICAL alert. Then go to §9.

**Verification:** the day's report after the session; `reconcile` is clean.

## 7. Reconciling paper positions and cash

**Steps:**
- `$PY -m app.ops reconcile` checks every session. It writes
  `data/ops/reconciliation/reconciliation-<time>.json` and never
  overwrites an earlier one.
- `$PY -m app.evaluation reconcile --date D` runs the per-day Phase 6 checks.

**Valuation convention:**
- available = cash − reserved;
- equity = cash + Σ(quantity × last mark price) of open positions;
- unrealized P&L excludes exit costs.

**Finding severities:**

| Severity | Examples |
|---|---|
| CRITICAL | cash or reserved mismatch, orphan fill, exposure above capital, missing uniqueness constraint |
| ERROR | P&L or fee recomputation mismatch, stale pending order, position from a past session, limit exceeded |
| WARNING | stale mark price, fee schedule unavailable |

**Rule:** never edit a financial record to make reconciliation pass. Find
the cause (bug, manual edit, restore from the wrong backup), and record the
investigation. If the record itself is wrong, restore a verified backup
(§8) to a new path and compare.

## 8. Backup and restore (SQLite)

**Backup:**
- Run `$PY -m app.ops backup` before every session and before any upgrade.
- This uses SQLite's online backup API, so it is safe while the engine runs.
- The output goes to `data/backups/paper-<time>.db` plus a `.json` manifest
  (SHA-256, schema). Files are 0600 and never overwritten.
- Copy backups off the machine regularly (encrypted storage).

**Verify:** `$PY -m app.ops verify-backup data/backups/paper-<time>.db`. It
runs in a temporary copy: hash, integrity, schema and reconciliation must
all PASS.

**Restore:**
1. `$PY -m app.ops restore <backup> --to data/paper/restored.db`. This
   refuses to overwrite the active database or any existing file.
2. Stop the scheduler (§3).
3. Back up the current DB too (`$PY -m app.ops backup`).
4. Point the application at the restored file: set
   `paper.db_path: data/paper/restored.db` in `config/settings.yaml`. Or,
   with the scheduler stopped, move the old file aside (never delete it)
   and rename the restored one.
5. Run `$PY -m app.ops health` and `$PY -m app.ops reconcile`.

**Rollback:** point `paper.db_path` back to the previous file.

**Space:**
- DB growth, measured 2026-10-20: a 7-symbol mock-data replay session
  produced 316 KB, about 45 KB per symbol-session. At 50 symbols that
  extrapolates to about 2–3 MB per session (about 0.6 GB a year).
  Backups are the same size.
- Keep at least `ops.min_free_disk_mb` (500 MB) free. Health is DEGRADED
  below that and UNHEALTHY below a quarter of it.
- A write failure (for example a full disk) rolls the transaction back.
  Reports are written atomically, so the previous file stays intact.

## 9. Handling corrupt or inconsistent state

| Symptom | Response |
|---|---|
| `database` FAIL "corrupt / failed integrity check" | Stop. Keep the corrupt file (rename it, don't delete it). Restore the newest verified backup (§8). Sessions after that backup are lost from the DB, but their exported reports in `data/evaluation/` remain. Mark those sessions as needing review |
| "schema version … newer than this code" | Someone ran a newer version against this DB: roll the code forward (§14); never downgrade the DB |
| `PaperStateError` or CRITICAL `state.inconsistent` at start-up | The engine refused to run, and **nothing was repaired**. Run `$PY -m app.ops reconcile`, identify the records, and decide manually (restore or investigate). Never delete positions to "fix" the ledger |

## 10. Generating and verifying reports

- **Daily paper report:** generated at session close.
  - If generation fails, the session stays CLOSED with its records, and an
    ERROR alert `report.failed.<day>` opens.
  - Fix the cause, then run `$PY -m app.paper report --date D --regenerate`.
  - Earlier versions are kept in `report_history`, and the alert resolves.
- **Evaluation reports:** `$PY -m app.evaluation report --date D` and
  `$PY -m app.evaluation cumulative`. A changed file keeps its previous
  version in `.history/`.
- **Verify:** `$PY -m app.evaluation reconcile --date D` checks that report
  totals equal the position records. `$PY -m app.ops health` → `reports`
  check.

## 11. Updating dependencies

1. Back up (§8). Copy the project folder, or commit if git is set up.
2. Upgrade **one** package at a time; check its changelog for breaking
   changes. Never take a major version without a compatibility check.
   (`pandas<3` is required by growwapi.)
3. Run `.venv/bin/pip check`, then the full gate (§12).
4. Update the lock:
   `.venv/bin/pip freeze --exclude-editable > requirements.lock` (keep the
   header comments).

**Rollback:** `.venv/bin/pip install -r <previous requirements.lock>`, then
re-run the gate.

## 12. Running tests before a release (release gate)

`scripts/release_gate.sh` (or `$PY -m app.ops gate`). The checks:

| Check | Blocking | Note |
|---|---|---|
| compile | yes | |
| tests_full | yes | full pytest |
| paper_only_safety | yes | `tests/test_paper_only.py` + no-order-code test |
| recovery_and_reconciliation | yes | `tests/test_failure_injection.py`, `tests/test_ops.py` |
| pip_check | yes | |
| secret_scan | yes | |
| config | yes | |
| env_permissions | yes | |
| gitignore | yes | |
| frozen_baseline | yes | strategy code must match `orb-baseline-v1` unless a new baseline is frozen |
| db_migration | yes | opens a COPY of the live DB with the new code |
| lint (ruff), type_check (mypy), dependency_audit (pip-audit) | no | **SKIPPED** when the tool is not installed, and never shown as passed |
| lock_file | no | warns on drift from `requirements.lock` |
| docs | no | |

The verdict is RELEASABLE only when no blocking check failed or was skipped.
`--skip-tests` always gives "NOT RELEASABLE".

**Justified exceptions:** lint, type and audit are non-blocking because the
tools are not installed in this environment (Phase 8 decision: no
unrequested dependency changes). Install `requirements-dev.txt` to enable
them.

## 13. Rotating or revoking credentials

See [security.md](security.md#credential-rotation-and-revocation).

## 14. Rolling back a faulty release

**Prerequisites:** the previous release folder (or git tag), a verified
backup from before the upgrade, and the scheduler stopped.

**Steps:**
1. Restore the previous code (folder copy, or `git checkout <tag>`).
2. Reinstall its lock file.
3. If the new release raised the DB schema version, the old code refuses
   the DB ("newer schema"). Restore the pre-upgrade backup to a new path
   (§8) and point `paper.db_path` at it. Sessions recorded by the faulty
   release stay in the newer DB for review; they are not merged
   automatically.
4. `$PY -m app.ops health`, `$PY -m app.ops reconcile`,
   `$PY -m app.robustness verify`.

**Escalation:** if the frozen baseline no longer verifies, the strategy code
differs from the evaluated version. Do not continue the evaluation on it.

## 15. Retention

`$PY -m app.ops prune` is a dry run. `--apply` deletes only:
- `*.log` files older than `ops.log_retention_days`;
- backups beyond the newest `ops.keep_backups`.

The paper database, reports, evaluation and robustness outputs, and
baselines are never eligible.

## Server deployment notes (for the final phase)

- Replace launchd with systemd units (or containers), using
  `Restart=on-failure` for the scheduler. SIGTERM is handled gracefully;
  give it `TimeoutStopSec=30`.
- Keep the dashboard on localhost behind an authenticating reverse proxy,
  or reach it through an SSH tunnel.
- Run `app.ops preflight` from a timer before 09:15, `app.ops backup`
  before the session, and `app.ops health` every 5 minutes during the
  session. Health exit codes can drive the host's alerting. No notification
  provider is built in: alerts go to logs, the dashboard and the database.
- The database must be on local disk (SQLite WAL does not work over
  network file systems).
