# Monitoring, drift detection and production reliability (Phase 15)

Monitoring observes, alerts and enforces safe degraded modes. It **never**
changes a strategy parameter, promotes or rolls back a version, or touches a
broker. Paper trading only. Paper results are never profits.

## 1. Verified architecture and data flow

Existing monitoring (verified):

- Phase 8 `app/ops`: `health_report`; `AlertManager` (deduplication by key,
  repeat cooldown, escalation, resolve, log notifier); reconciliation
  `consistency_checks`; backups; retention.
- The paper engine's heartbeat, including provider health such as the Groww
  adapter's circuit, failures and outages.
- Phase 6 per-step data-quality batches (`dq_batches`).
- The agent's run, tool and model audit tables; the Phase 14 registry.

What was missing:

- per-incident alerts with rules, thresholds and audit history; recovery
  notifications;
- AI reliability and version monitoring;
- drift detection;
- explicit degraded states that actually pause paper decisions;
- a breaker for model calls.

```
every integrated step ─▶ IntegratedEngine.step ─▶ SessionMonitor.after_step(scope=step)
session end           ─▶ end_evaluation_session ─▶ SessionMonitor.after_step(scope=full)
on demand             ─▶ python -m app.monitoring run [--full]
        │
        ▼ checks.py (reads records only)
  data · calendar · ai · execution · versions · storage   (+ performance/drift when full)
        │ findings (rule, threshold, evidence, steps, effect)
        ▼
  AlertCenter.sync ─▶ monitor_alerts + monitor_alert_events ─▶ LogNotifier + OutboxNotifier
  state.update     ─▶ monitor_state_events (NORMAL / DEGRADED_* / PAUSED / RECOVERY / REVIEW)
  snapshot         ─▶ monitor_snapshots (retention-bounded)
        │
        ▼ next step: SessionMonitor.paused() ─▶ pause gates on paused arms (no new entries)
```

## 2. Files

| File | Purpose |
|---|---|
| `app/monitoring/checks.py` | Data, calendar, AI, execution, version, storage and performance checks |
| `app/monitoring/drift.py` | KS test, bootstrap CI; data, performance and possible-concept drift classification (numpy only) |
| `app/monitoring/alerts.py` | Monitoring DB; alert center (incident IDs, deduplication, cooldown, escalation, grouping, recovery, audit); log and outbox notifiers; input sanitising |
| `app/monitoring/state.py` | Safe degraded states, recovery, sticky manual review, audited time-limited overrides |
| `app/monitoring/monitor.py` | Orchestration; `SessionMonitor` hooks for the integrated engine |
| `app/monitoring/__main__.py`, `views.py` | CLI; dashboard view "Monitoring" |
| `app/integrated/engine.py` | `attach_monitor` (pause gates for every arm, checks after each step); LLM circuit breaker |
| `app/paper/engine.py` | A gate may return a status (`PAUSED_BY_MONITORING`) so pauses are labelled correctly |
| `app/experiments/runtime.py` | Integrated sessions attach monitoring |
| `tests/test_monitoring.py` | 21 tests |

## 3. Metrics, alert rules and thresholds

All thresholds live in the `monitoring:` settings section. They are
configurable; the defaults and their rationale are below.

| Area | Rule (key) | Default threshold | Severity, effect |
|---|---|---|---|
| Data | `data.no_step`: no processing step during an open session | heartbeat age ≤ `ops.max_step_gap_minutes` (15) | CRITICAL, DEGRADED_DATA |
| Data | `data.stale`: no symbol with fresh, valid candles | `data.max_staleness_seconds` | CRITICAL, DEGRADED_DATA |
| Data | `data.invalid_records`: duplicate, out-of-order or invalid OHLCV | > 0 records | CRITICAL, DEGRADED_DATA |
| Data | `data.missing_candles` | > 5% of expected | WARNING |
| Data | `data.provider_failing`: circuit open or ≥ 3 consecutive failures | — | WARNING |
| Data | `data.symbol_mapping`: configured symbols with no data in session | 0 | WARNING |
| Data | `data.spread_invalid`, `data.quotes_stale` | spread ≤ 3× screen limit; quote age ≤ 120 s | WARNING |
| Calendar | `calendar.year_missing`, `calendar.inconsistent` (data on a closed day) | — | WARNING; CRITICAL, PAUSED (all arms) |
| AI | `ai.failure_rate` (timeouts, rate limits, errors) over the last ~30 runs, minimum 10 requests | 20% warning, 50% critical | WARNING; CRITICAL, DEGRADED_AI |
| AI | `ai.latency` p95; `ai.cost` per day (only with prices configured) | 30 s; configurable USD | WARNING |
| AI | `ai.invalid_outputs`, `ai.unsupported_claims`, `ai.duplicates` | 20%, 30%, > 0 | WARNING |
| AI | `ai.no_trade_shift`, `ai.volume_shift` (last session vs earlier) | change ≥ 0.4; ×3 | INFO |
| Versions | `version.ai_mismatch`: recommendations from a prompt or model other than the active version | exact match | CRITICAL, PAUSED (AI arm) |
| Versions | `version.integrity`, `version.multiple_active`, `version.rollback_not_restored`, `version.code_mismatch` | — | CRITICAL, PAUSED (AI arms) |
| Execution | `exec.inconsistent.<arm>`: ledger, duplicates or accounting (`consistency_checks` CRITICAL) | 0 | CRITICAL, PAUSED (that arm) |
| Execution | `exec.reconciliation.<arm>` discrepancies | 0 | WARNING |
| Storage | `storage.db.<name>` failed `quick_check`; `storage.disk`; `storage.backup_age` | ok; ≥ 500 MB; ≤ 48 h | CRITICAL (pause); CRITICAL; WARNING |
| Performance | `perf.drift.<arm>`: recent expectancy CI entirely below reference CI; ≥ 30 trades each side | 95% | WARNING, MANUAL_REVIEW_REQUIRED (that arm) |
| Performance | `perf.data_drift.<arm>.<feature>`: KS p < 0.01 with ≥ 30 values each side (relative volume, opening-range width) | — | INFO |
| Performance | `perf.losing_streak.<arm>`: information only, never drift by itself | ≥ 6 | INFO |

**Drift kinds are never conflated.** DATA_DRIFT means an input distribution
changed. PERFORMANCE_DRIFT means results deteriorated, with enough evidence.
POSSIBLE_CONCEPT_DRIFT is a hypothesis only: performance drifted while the
tested inputs did not change. INSUFFICIENT_EVIDENCE is too few observations.
NO_DRIFT_DETECTED is a lack of evidence, not proof of stability.

**LLM circuit breaker** (integrated sessions). Only research runs that
actually called the model count:

- After `llm_breaker_failures` (3) unavailable runs in a row, no model calls
  are made for `llm_breaker_cooldown_minutes` (15). The AI arm opens no new
  position while it is open.
- Then one half-open trial run is made. A successful answer closes the
  breaker.
- This sits on top of the provider's bounded retries and backoff (Phase 10).

## 4. Safe degraded modes

| State | When | Effect |
|---|---|---|
| NORMAL | no blocking finding | — |
| DEGRADED_DATA | stale, missing or invalid required data | the engines already block new signals; agent abstains |
| DEGRADED_AI | AI failure rate critical | AI arm opens nothing new; deterministic baseline continues, labelled deterministic |
| PAPER_TRADING_PAUSED | critical inconsistency (records, versions, calendar, storage) | the named arms open no new paper entries (`PAUSED_BY_MONITORING`); open positions keep their stop, target and EOD exits |
| MANUAL_REVIEW_REQUIRED | performance drift with sufficient evidence | the affected arm pauses new entries; **sticky** until a person runs `clear-review` |
| RECOVERY | blocking findings cleared | stays until `recovery_passes` (3) consecutive clean evaluations, then NORMAL |

Rules:

- Every transition is appended, with reason, actor and findings, to
  `monitor_state_events`. The state is reconstructed from that log after a
  restart.
- Overrides need a person, a reason and `--confirm "OVERRIDE <state>"`. They
  last at most `override_max_minutes` (120) and record the OS user.
  Integrity failures (registry, calendar, database) cannot be overridden.
- Monitoring never changes strategy parameters, never promotes, and never
  moves from poor performance to a modified strategy. Changes go through
  Phase 13 proposals and Phase 14 promotion.
- If the monitoring state is unreadable, the AI arms pause (fail safe). The
  deterministic baseline is not paused for a monitoring failure.

## 5. Dashboard, reports and configuration

- Dashboard view **Monitoring**: system health, research-agent quality,
  paper performance with sample size, period and drift classification,
  version management, open alerts and state history. Unavailable values
  show "unavailable", never 0. No secret is displayed, and the view is
  read-only: all controls are CLI commands with confirmations.
- The project has no web authentication. Run the dashboard bound to
  localhost (Streamlit's default). Do not expose it publicly without adding
  a reverse proxy with authentication (deployment step).
- Configuration: the `monitoring:` section in `config/settings.example.yaml`.
  The new database `data/monitoring/monitoring.db` is created on first use,
  so no migration is needed.

## 6. Commands

```bash
python -m app.monitoring run [--full]          # one evaluation (integrated sessions do this every step)
python -m app.monitoring status | alerts [--all] | states
python -m app.monitoring history --alert AL-..
python -m app.monitoring ack --alert AL-.. --by NAME --note ".."
python -m app.monitoring override --to NORMAL --minutes 60 --by NAME --reason ".." --confirm "OVERRIDE <state>"
python -m app.monitoring clear-review --by NAME --reason ".." --confirm "CLEAR MANUAL_REVIEW_REQUIRED"
python -m pytest tests/test_monitoring.py -q
```

**External alert channel (not configured).** Every notification is appended
to `monitoring.alert_outbox` (JSON lines, fsynced). A future e-mail or chat
forwarder can tail that file. Until one is configured and tested,
notifications are **LOGGED or WRITTEN, never "delivered"**.

## 7. Example alerts (synthetic inputs, real code)

```json
{"event": "RAISED", "severity": "CRITICAL", "component": "market-data",
 "rule": "no symbol has fresh, valid completed candles", "threshold": "freshness <= 120s",
 "evidence": {"provider": {"circuit": "open", "consecutive_failures": 4, "last_error": "timeout"},
              "symbols_ok": 0, "symbols_total": 12},
 "session": "2025-03-12", "steps": ["inspect provider health in the heartbeat",
 "check the data subscription and network"], "status": "OPEN"}

{"event": "RAISED", "severity": "CRITICAL", "component": "llm",
 "rule": "model request failure rate (timeouts, rate limits, errors)",
 "threshold": "< 0.2 (critical 0.5)", "evidence": {"kinds": {"timeout": 5}, "rate": 0.625, "requests": 8},
 "steps": ["check provider status and the API key (never logged)",
           "deterministic baseline continues; AI arm opens no new positions"], "status": "OPEN"}

{"event": "RAISED", "severity": "WARNING", "component": "performance",
 "rule": "performance deterioration supported by sufficient evidence",
 "threshold": "recent CI entirely below reference CI; >= 30 trades each",
 "evidence": {"classification": "PERFORMANCE_DRIFT", "recent_expectancy": -3.299,
              "recent_ci": [-3.9482, -2.693], "reference_expectancy": 1.9172,
              "reference_ci": [1.1939, 2.5936], "recent_trades": 40, "reference_trades": 40},
 "steps": ["review recent sessions and data quality first",
           "no parameter is changed automatically; candidate changes go through Phase 13/14"]}
```

**First run on the real project (2026-10-20, about 10:40 IST):**

- Today is Dussehra in the NSE calendar. Monitoring reported the market as
  closed (an official closure), not a data gap.
- It raised two WARNINGs ("consistency check could not run"), because
  neither paper portfolio has ever been initialised. That was a false
  positive in the new code. It was fixed and a test added; the next run
  resolved both alerts with recovery notifications.
- Final state: NORMAL, 0 findings.

## 8. Recovery and rollback procedures

1. `python -m app.monitoring status`: read the state, the paused arms and
   the open alerts with their investigation steps.
2. Fix the cause. Data problems: provider, subscription, network.
   Records: `python -m app.ops reconcile`, and never edit records by hand;
   restore from `python -m app.ops backup` copies if a database is corrupt.
   Versions: `python -m app.experiments verify`.
3. Monitoring re-checks after every step and moves to RECOVERY, then to
   NORMAL after 3 clean evaluations. No manual reset is needed for
   transient faults.
4. For a version regression: `python -m app.experiments rollback --id EXP-..
   --by NAME --reason ".." --confirm "ROLLBACK EXP-.."`. Monitoring then
   verifies that the restored version is active (`version.rollback_not_restored`).
5. For performance drift: review, record any Phase 13 annotation, then
   `clear-review`. Any change goes through Phase 13 and 14. Nothing is
   retuned automatically.

## 9. Tests (actual results)

- `tests/test_monitoring.py`: **21 passed**.
- Full suite and gate: see §12.

Covered:

- stale, missing, malformed, duplicate and out-of-order data;
- holiday vs gap;
- timeouts, rate limits and recovery;
- cost threshold, with unavailable vs 0;
- the circuit breaker bounding calls while the baseline is isolated;
- drift with insufficient samples, no false positive on identical
  distributions, data drift, concept drift as a hypothesis, losing streak
  only INFO;
- KS correctness;
- deduplication, cooldown, escalation, recovery and audit;
- honest delivery status;
- log-injection and secret sanitising;
- pause, recovery, restart reconstruction and immutable state history;
- sticky review and limited overrides (no integrity override);
- pause blocking new entries only;
- a normal session unaffected;
- version mismatch and registry tamper;
- storage corruption;
- no modification of trading records;
- no order or parameter path;
- the dashboard showing "unavailable";
- an uninitialised portfolio not counted as a failure.

## 10. Known limitations and risks

- **Never observed during a live session**: no session has run (empty
  universe; today is a holiday).
- **No external alert delivery.** Only the log and the local outbox.
- **No web authentication.** Controls are CLI-only, and the dashboard is
  read-only.
- Data-drift checks cover relative volume and opening-range width only.
  Other features have no history yet.
- The AI behaviour baseline (NO_TRADE share, volume) compares the last
  session with earlier ones. It is weak until many sessions exist.
- Performance drift needs at least 30 closed trades in each window. At ₹500
  capital, with typical setups rejected on costs, it may stay
  INSUFFICIENT_EVIDENCE for a long time. That is the correct reading.

## 11. Final platform readiness assessment (Phases 1–15)

**Not production-ready, and not a trading system.** It is a tested
paper-trading research platform awaiting its first observations.

| Category | Items |
|---|---|
| **Verified (automated tests, real code)** | paper-only boundary and no order paths; deterministic strategy, risk and cost engines (frozen baseline, no drift); paper accounting and reconciliation; immutable recommendation, evaluation, learning and registry records; integrated two-arm sessions with isolation; AI gate (veto only); restart recovery; reproducible reports; experiment validation and promotion with human confirmation; monitoring, alerts and degraded modes |
| **Verified against real external systems** | Anthropic API: connectivity check and live structured-output test passed (2026-10-20); real-model research runs on synthetic data. Monitoring: one run on the real project (holiday detected correctly) |
| **Tested only in simulation** | every trading, comparison, learning, promotion and drift result (synthetic fixtures, mock or real model on synthetic data) |
| **Observed in prospective paper trading** | **nothing**: 0 live sessions (empty `config/universe.csv`) |
| **Unverified assumptions** | Groww live data behaviour during market hours; real spreads and slippage vs assumptions; whether the ORB strategy has any edge (Phase 7 PENDING_DATA); whether the AI adds value (Phase 11 and 12 PENDING); fee schedule beyond its verification date |
| **Outstanding risks** | ₹500 capital makes typical setups cost-infeasible (Phase 9 and 12 evidence); the real model was a blanket veto on synthetic setups; no external alerting; no web authentication; lint, type and dependency-audit tools not installed; registry not initialised (user decision) |

Nothing here guarantees profitability or future accuracy. Real orders remain
entirely manual.

## 12. Phase 15 checklist

| Requirement | Status |
|---|---|
| Inspect existing system, reuse, plan | VERIFIED (§1) |
| Market-data health: availability, age, missing, duplicate, out-of-order, OHLCV, gaps vs closures, mapping, quotes and spread, provider errors and outages | VERIFIED (rate-limit detail as exposed by the provider's health) |
| Block on stale or invalid data; never substitute; resume after checks | VERIFIED |
| AI reliability: success and failure, timeouts, rate limits, retries, latency, tokens, cost, invalid, unsupported, tool errors, rejections, duplicates, version mismatches, behaviour changes | VERIFIED |
| Bounded retries and backoff (Phase 10) plus circuit breaker; deterministic-only labelled | VERIFIED |
| Performance drift with minimum samples, windows, uncertainty; data, concept, performance and operational kinds distinguished | VERIFIED |
| Alerts: severities, IDs, evidence, rule and threshold, version and session, steps, status, audit; deduplication, grouping, cooldown, recovery | VERIFIED |
| Delivery via existing mechanisms plus a tested local interface; no false "delivered" | VERIFIED; external channel INCOMPLETE (not configured) |
| Safe degraded states, logged transitions, verified recovery, audited and limited overrides | VERIFIED |
| Phase 14 integration: versions per recommendation, mismatch, single active, rollback verification, history kept; never promotes | VERIFIED |
| Dashboards distinguish unavailable from zero; insufficient samples labelled | VERIFIED |
| Security: no secrets in alerts or logs, input sanitising, monitoring cannot alter records; protected admin controls | VERIFIED (CLI confirmations); web auth INCOMPLETE (none in project) |
| Restart recovery, duplicate handling, state reconstruction, retention | VERIFIED |
| Regression suite | VERIFIED (§9) |
| Observed on live sessions | BLOCKED: no live session (empty universe) |
