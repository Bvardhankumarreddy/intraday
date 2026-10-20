# Phase 8: pre-implementation audit and risk register

Audit date: 2026-10-20. This was written **before** any Phase 8 change.
Earlier phases being marked complete does not make the system
production-ready. This register lists what is actually wrong or unproven.

## Baseline

- **Tests:** `pytest`, 398 passed, 0 failed (2026-10-20, before Phase 8).
- **Live state:**
  - The paper DB (`data/paper/paper.db`) is empty: 0 evaluations, 0 sessions.
  - The first live session is scheduled for 2026-10-21.
  - Two launchd agents are installed locally: the evaluation job (08:55 on
    weekdays) and the dashboard (always on, `localhost:8501`).
- **Tools available locally:** Python 3.13 venv and pytest.
  - **Not installed:** ruff, mypy, pip-audit, bandit, pyflakes,
    detect-secrets.
  - No notification provider exists.
  - The project is not a git repository.

## Architecture map (reviewed)

| Concern | Where | Notes |
|---|---|---|
| Market data | `app/data/` (`MarketDataProvider`; mock, CSV, memory, Groww) | Groww adapter: retries, `ConnectionState`, data-only wrapper |
| Strategy / screening | `app/strategies/`, `app/screening/`, `app/scan/` | frozen as `orb-baseline-v1` (Phase 7 code hash) |
| Risk / costs | `app/risk/`, `app/costs/` | ₹500, 0.5% risk, ₹15 daily loss, 2 trades/day, costs included in risk |
| Paper order lifecycle | `app/paper/engine.py`, `models.py`, `execution.py` | CREATED→PENDING→FILLED/REJECTED/EXPIRED; deterministic IDs |
| Persistence | `app/paper/store.py` (SQLite, stdlib) | `BEGIN IMMEDIATE` transactions; UNIQUE (signal, side); ledger invariant on start |
| Evaluation | `app/evaluation/` | sessions, data-quality ledger, signal audit, per-day reconciliation, reports |
| Scheduler | `app/paper/scheduler.py` | `flock` instance lock, bounded step failures (5), catch-up after restart |
| Dashboard | `app/dashboard.py` (Streamlit, localhost) | read-only views |
| Config / secrets | `app/config.py`, `.env` (600) | `SecretStr`; YAML refuses secret keys |
| Logging | `app/utils/logging.py` | JSON to stderr; key/value redaction |

## Order-endpoint review (paper-only boundary)

- `grep` finds no `place_order` / `modify_order` / `cancel_order` call
  anywhere in `app/` (an existing test enforces this).
- **The risk:** `GrowwSdkClient` holds a full `growwapi.GrowwAPI` object in
  `self._api`. The SDK exposes `place_order`, `modify_order`,
  `cancel_order`, holdings and positions. The wrapper only calls
  `get_quote` and `get_historical_candle_data`, but any code holding the
  wrapper can reach the order methods through the private attribute.
- There is no explicit "execution mode" setting. That avoids an ambiguous
  switch, but it also means nothing rejects an attempt to configure one.
  Unknown settings are silently ignored (`extra="ignore"`).

## Risk register (prioritised)

| # | Severity | Risk | Evidence | Remediation | Verification |
|---|---|---|---|---|---|
| R1 | HIGH | **Groww requests have no timeout.** A hung HTTP call blocks the live scheduler indefinitely. Health never changes, and no further steps or reports run. | `growwapi/groww/client.py`: `timeout: Optional[int] = None` ("Defaults to None (infinite)") on `get_quote` and `get_historical_candle_data`. Our client passes none. | Pass `data.request_timeout_seconds` (default 10 s) on every data call. | Test that the fake SDK receives `timeout=`. Failure-injection test for timeouts. |
| R2 | HIGH | **Reports are overwritten.** `save_report` uses `INSERT OR REPLACE`. `_resolve_old_sessions` regenerates old reports. `write_daily` / `write_cumulative` overwrite files. Earlier versions are lost. | `app/paper/store.py`, `app/evaluation/report.py`. | Append-only `report_history` table. Exported files archive the previous version when the content changes. | Tests: regenerate a report and check both versions are retained. |
| R3 | HIGH | **No health check or alerting.** Health exists only inside the running process. A stopped, stuck or degraded session is invisible until someone reads logs. | No health or alert module, no notifier. | `python -m app.ops health` checks config, DB, schema, integrity, reconciliation, session state, data freshness, provider circuit, report status, disk, and alerts. Persistent, deduplicated alerts with a pluggable notifier (log by default). Dashboard Operations view. | Tests for each status, alert dedup, notifier failure. |
| R4 | HIGH | **SQLite contention.** There is no `busy_timeout`, and the default rollback journal is used. A dashboard read during the engine's `BEGIN IMMEDIATE` makes the engine fail at once with "database is locked". Five such failures stop the scheduler. | `PaperStore.__init__`. | WAL journal, `busy_timeout`, `synchronous=FULL`. | Concurrent reader/writer test. |
| R5 | MEDIUM | **No circuit breaker or jitter.** With 30 symbols, an outage costs up to 4 attempts × (10 s timeout + backoff) per symbol per step, far longer than the 5-minute step. Steps overrun and pile up. | `GrowwMarketDataProvider._call`. | Circuit breaker (opens after N consecutive failed calls, fails fast during cooldown, half-open probe). Bounded jitter. Health exposes circuit state, failures, reconnects and recovery duration. | Rate-limit, outage and recovery tests. |
| R6 | MEDIUM | **No integrity check or schema version.** A corrupt or newer-schema DB is used blindly. | `PaperStore`. | `PRAGMA quick_check` on open. `meta.schema_version`: an older code version refuses a newer DB. Migrations are additive only. | Corrupt-file and newer-schema tests. |
| R7 | MEDIUM | **Startup checks only the cash ledger.** Orphan fills, closed positions without exits, stale pending orders, over-exposure and incomplete fills go undetected. | `verify_ledger`. | Portfolio-wide reconciliation (reuses Phase 6 checks, adds new ones, assigns severity). Critical issues refuse to start, never repair, and raise an alert. | Corruption and mismatch tests confirm records are unchanged. |
| R8 | MEDIUM | **Log redaction gaps.** `Authorization: Bearer <token>` keeps the token, JWT-like strings aren't caught, and actual secret values aren't redacted when they appear in free text (for example inside exception messages). | `app/utils/logging.py`. | Bearer and JWT patterns, exact-value redaction of loaded secrets, redaction of exception text. | Redaction tests. |
| R9 | MEDIUM | **Invalid credentials crash with a raw SDK exception.** The login error is not a `DataError`, so `evaluation run` dies with a traceback and the session is not recorded as failed. | `GrowwSdkClient.__init__` → `_login`. | Wrap login and authorisation failures in a sanitised `DataUnavailableError`. The scheduler records FAILED and raises an alert. | Expired / invalid credential test. |
| R10 | MEDIUM | **Config typos are silently ignored.** `risk: {max_daily_los: 5}` runs with the default limit. | Section models default to `extra="ignore"`. | Sections forbid unknown keys. An explicit `execution_mode: paper` setting rejects any other value at startup. | Config tests. |
| R11 | MEDIUM | **The SDK object with order methods is reachable.** | `GrowwSdkClient._api`. | Allowlist proxy: only `get_quote`, `get_historical_candle_data` and login are callable. Import-boundary test: only `app/data/groww_provider.py` may import `growwapi`. | Tests (including a full paper session against a fake SDK that records any order call). |
| R12 | MEDIUM | **No graceful shutdown.** SIGTERM/SIGINT kill the scheduler mid-sleep. The session is not marked interrupted, and the DB connection is not closed cleanly. Transactions do roll back. | `SessionScheduler`, CLIs. | Stop flag on SIGTERM/SIGINT, interruptible sleep, stop between steps, mark INTERRUPTED/INCOMPLETE, close resources. Positions are **not** closed. | Shutdown test. |
| R13 | MEDIUM | **No backup or restore procedure.** | — | Online SQLite backup with SHA-256 manifest. Verify (integrity, schema, reconciliation) into an isolated copy. Restore only to a new path, never over the active DB. | Backup, verify and restore tests. |
| R14 | MEDIUM | **World-readable data.** DB, logs, reports and lock file are 0644, with directories at 0755. | `ls -l data/`. | DB and lock files 0600, `data/` directories 0700 when the app creates them. `ops harden` fixes existing files. | Permission tests. |
| R15 | LOW | **Dependencies.** Ranges are not locked. growwapi pulls in unmaintained or unrelated packages (`clint` 0.5.1, `args` 0.1.0, `google` 3.0.0, a search scraper). certifi 2025.11.12 is about a year old. No local vulnerability scanner exists. | `pip list`. | `requirements.lock` (exact versions, checked by the gate); `requirements-dev.txt` for optional scanners; findings documented. **Not** claimed vulnerability-free. | Release gate compares installed versions with the lock. |
| R16 | LOW | **Dashboard has no authentication.** It is bound to localhost (fine on this Mac), but a server deployment must not expose it. | launchd plist `--server.address localhost`. | Documented in runbooks and security findings. | — |
| R17 | LOW | **Retention.** Logs and backups grow without bound. Trading records must never be auto-deleted. | — | `ops prune`: logs and backups only, dry run by default. | Test that trading records and reports are untouched. |
| R18 | INFO | Nine test-artifact folders remain in `data/evaluation/` (deleting them was blocked earlier). | `ls data/evaluation`. | The user deletes them. Documented. | — |

## Recovery review (what already works)

These are covered by Phase 5/6 tests:

- **Atomic units:**
  - signal + order creation + cash reservation;
  - entry fill (cash, order FILLED, position);
  - exit (SELL order, cash, position CLOSED, loss lock).
- **Idempotency:** primary keys and `UNIQUE(signal_id, side)` mean that a
  re-run step never duplicates a signal, order or fill.
- **Restart behaviour:**
  - restart resumes the session;
  - the scheduler catches up only to the latest step;
  - evaluation coverage counts distinct candle slots;
  - an interrupted evaluation session becomes INCOMPLETE.
- **Risk-lock persistence:** the daily-loss lock and the trade count are
  persisted (`sessions.locked`, orders), so they survive restarts.

Phase 8 adds failure-injection tests that exercise these paths explicitly.
Unexercised paths are not claimed to work.
