# Security findings (Phase 8)

Review date: 2026-10-20. Scope: source, configuration, secrets, logging,
files, dependencies and the broker integration. The risk register with
evidence is in [phase8_audit.md](phase8_audit.md).

This is a research and paper-trading tool. **It never places, modifies or
cancels real orders**, and nothing in this review changes that.

## Findings and disposition

| # | Finding | Severity | Disposition |
|---|---|---|---|
| S1 | Groww SDK requests had no timeout (SDK default: infinite) | HIGH | **Fixed.** `data.request_timeout_seconds` (10 s) on every call; tested |
| S2 | The full SDK object, with order, holdings and positions methods, was reachable as `GrowwSdkClient._api` | MEDIUM | **Fixed.** Allowlist proxy (`data_only_sdk`); every non-data attribute raises `BlockedSdkCall`; only `app/data/groww_provider.py` may import `growwapi` (AST test) |
| S3 | Log redaction missed `Authorization: Bearer <token>`, JWTs, and secrets echoed in exception text | MEDIUM | **Fixed.** Pattern and exact-value redaction (secrets registered when settings load), applied to the whole log payload including tracebacks; tested |
| S4 | Login failures surfaced raw SDK exceptions (possibly with server text) and crashed `run` | MEDIUM | **Fixed.** Sanitised `DataUnavailableError` (exception type and status only, `from None`); session recorded FAILED; CRITICAL alert |
| S5 | Data, DB, logs and lock file were world-readable (0644/0755) | MEDIUM | **Fixed for new files** (DB, WAL and lock 0600; DB directory 0700; backups and reconciliation reports 0600). **Existing files:** run `python -m app.ops harden` (done once on 2026-10-20; see below). Health warns if group/other access returns |
| S6 | Unknown config keys silently ignored (typos weaken risk limits) | MEDIUM | **Fixed.** Every section forbids unknown keys, from YAML and `NSE_*` env alike |
| S7 | No explicit execution-mode guard | MEDIUM | **Fixed.** `execution_mode: Literal["paper"]`; any other value fails at load; the engine re-checks at construction. There is no live execution adapter |
| S8 | Reports could be silently overwritten | HIGH (integrity) | **Fixed.** Append-only `report_history`; exported files archive the earlier version to `.history/`; reconciliation reports never overwrite |
| S9 | Dashboard has no authentication | LOW locally | **Documented / accepted locally.** Bound to `localhost` by the launchd plist. **Must not** be exposed on a server without an authenticating reverse proxy (or an SSH tunnel) |
| S10 | Dependencies: version ranges, no lock; growwapi pulls unmaintained or unrelated packages (`clint` 0.5.1 from 2015, `args` 0.1.0, `google` 3.0.0, a search scraper unrelated to Google's SDKs); certifi 2025.11.12 | LOW–MEDIUM | **Partly mitigated.** `requirements.lock` pins the exact tested set and the gate checks it. Transitive packages come from `growwapi==1.5.0` and can't be removed without dropping the SDK. **Not resolved:** no vulnerability scanner is installed, so no advisory scan has been run. See "Dependencies" |
| S11 | `.env` holds live credentials | INFO | Correct place. Mode 600, in `.gitignore`, never read into YAML, `SecretStr` in memory, values redacted from logs. The gate fails if `.env` loosens or a configured secret value appears in any other file (logs and reports included) |
| S12 | Nine test-artifact folders in `data/evaluation/` | INFO | **Open.** Deleting them was blocked by the permission system. Remove manually: `rm -rf data/evaluation/EVAL-*` (all dated 2025-03-12, provider `memory`) |
| S13 | No version control | INFO | **Open.** Without git, rollback relies on backups and copies (see runbooks). Initialising a git repository is recommended before server deployment |

## Secrets handling (verified)

- Secrets live only in the environment or `.env`. A YAML file containing a
  secret key is rejected at load.
- `.env.example` contains placeholders only.
- Secrets never appear in `Settings.version()`, settings dumps, frozen
  baselines or reports. They are excluded explicitly; tests cover this.
- Logs: key-based, pattern-based and exact-value redaction.
- The gate's `secret_scan` checks every non-binary file outside `.venv`,
  including `data/` logs and reports. It looks for the exact configured
  secret values, private keys, JWTs, filled-in `NSE_BROKER_*=` lines and
  long quoted secret assignments. It reports file names only, never values.

## Credential rotation and revocation

**Prerequisites:** access to the Groww account's Trading API page.

**Routine rotation (recommended every 90 days, or on any suspicion):**

1. Stop scheduled runs: `launchctl bootout gui/$(id -u)/com.nseintraday.evaluation`
   (on a server, stop the service).
2. In Groww's API-keys page, generate a new TOTP token and secret, or a new
   API key and secret.
3. Edit `.env` (`chmod 600 .env` afterwards) and replace `NSE_BROKER_API_KEY`
   and `NSE_BROKER_TOTP_SECRET` (or `NSE_BROKER_API_SECRET`).
4. Verify: `python -m app.ops preflight`. The `provider_probe` check must
   be PASS. Outside market hours, "data rejected … expected outside market
   hours" also counts as PASS.
5. Revoke the old key in Groww, then re-run the preflight check. It must
   still PASS with the new key.
6. Re-enable the schedule.

**Suspected leak:**

1. Revoke the key in Groww **first**.
2. Rotate as above.
3. Run `python -m app.ops gate --skip-tests` and look at `secret_scan` to
   find where the value appeared.
4. Rotate any log or report file that contained it, then delete or
   overwrite it. Log retention also applies: `python -m app.ops prune`.

Escalation: if Groww shows any order or account activity you did not make,
contact Groww support immediately. This application cannot place orders,
so such activity did not come from it.

## Dependencies

- `requirements.txt`: compatible ranges (existing convention).
- `requirements.lock`: the exact versions tested, compared by the gate.
- Known constraints:
  - `pandas<3` is required by growwapi 1.5.0.
  - `numpy<2.4` avoids pandas-2.3 deprecation noise.
- `pip check`: no broken requirements.
- **Advisory scanning: NOT performed.** pip-audit, safety and bandit are
  not installed. The gate reports `dependency_audit: SKIPPED`.
  - Install them deliberately with `pip install -r requirements-dev.txt` and
    re-run the gate.
  - Even a clean scan would not prove the dependencies are free of
    vulnerabilities.
- Outdated at review time:
  - certifi 2025.11.12 → 2026.10.18 (CA bundle; data-only, low risk to
    upgrade).
  - numpy 2.3.5 → 2.5.x and pandas 2.3.3 → 3.0.x are pinned on purpose
    (see above).
  - Upgrade only through the runbook "Updating dependencies".

## Architectural limitations (paper-only boundary)

- **The order code still ships.** The Groww SDK package itself contains
  order-placement code, because it is one package for data and trading.
  Isolation is by construction and by tests:
  - one importing module;
  - the allowlist proxy;
  - AST checks that no `place_order`-style attribute is accessed in `app/`;
  - a full paper session through the adapter against a recording fake SDK
    (zero order calls).

  A future code change *could* bypass these, which is why the tests run in
  the release gate. They do not protect against a malicious or compromised
  `growwapi` package itself; that is a supply-chain risk (see S10).
- **No order-execution adapter.** Paper fills are computed in-process
  (`app/paper/execution.py`). Adding live execution would need new code,
  failing the `execution_mode` validation, the import-boundary tests and the
  no-order-method tests. That is deliberate.
- **The credential can place orders.** The Groww API key is an account
  credential capable of trading if used by other software. Protect `.env`
  accordingly; the application's own restrictions do not limit the key.
