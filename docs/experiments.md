# Experiments, validation and controlled promotion (Phase 14)

**Core rule:** no candidate becomes active without passing predeclared
validation criteria, prospective shadow and paper-canary evidence, and a
named person's explicit approval. A newer model, a revised prompt or a
changed parameter is never assumed to be better. Everything is paper trading.
No component here can place, modify or cancel a real order.

## 1. Verified architecture and integration points

| Need | Existing component (verified) | Phase 14 use |
|---|---|---|
| Deterministic baseline | frozen `orb-baseline-v1` and code hash (Phase 7) | arm A; must not drift |
| Recommendation evaluation | `app/aieval` replay (`run_arm`, `AgentDecider`), metrics, bootstrap (Phase 11) | three-arm validation |
| Integrated paper trading | `app/integrated` (Phase 12) | shadow and canary arms; runtime version resolution |
| Learning proposals | `app/learning` (Phase 13) | a proposal's test evidence can be an experiment run |
| Agent | `ResearchAgent` | **added:** an optional versioned prompt variant and a model tool allow-list (defaults unchanged) |

Gap found: before this phase the AI system's version was implicit in the code
(prompt constant, settings). There was no registry, no "active" concept, no
candidate configuration and no promotion control.

## 2. Files

| File | Purpose |
|---|---|
| `app/experiments/registry.py` | Content-addressed versions; experiments; hash-chained, append-only log; integrity verification; active-version resolution |
| `app/experiments/candidate.py` | Candidate spec (YAML); version components; non-weakening check for mandatory controls |
| `app/experiments/validate.py` | Splits with embargo; three arms; acceptance criteria; classification; multiple-testing adjustment; reproducibility reruns; single-use final set |
| `app/experiments/promotion.py` | Approval, shadow, canary, activation, rollback, rejection, with evidence checks |
| `app/experiments/runtime.py` | Which AI system an integrated session runs; attaches shadow and canary arms |
| `app/experiments/report.py` | Reproducible experiment report |
| `app/experiments/__main__.py`, `views.py` | CLI; dashboard view "Experiments" |
| `config/experiments/acceptance.yaml` | Predeclared, versioned acceptance rules (`accept-v1`) with rationale |
| `app/agent/workflow.py`, `app/agent/tools.py` | Prompt-variant and tool allow-list parameters |
| `app/integrated/engine.py`, `app/integrated/__main__.py` | `ExtraArm` (shadow and canary); session refuses on registry integrity failure |
| `app/aieval/replay.py` | `AgentDecider` passes a version's prompt and tools |
| `config/ai_eval/protocol.yaml` | AI-system pins re-taken for the agent hooks (amendment recorded) |
| `tests/test_experiments.py` | 18 tests |

## 3. Experiment lifecycle and promotion state machine

```
DRAFT → VALIDATING → VALIDATED → AWAITING_APPROVAL → APPROVED* → SHADOW → PAPER_CANARY → ACTIVE*
  └──────────┴────────────┴──────────────┴──────────────┴───────────┴──────────┴──→ REJECTED
ACTIVE → ROLLED_BACK* (restores the previously active version)   ACTIVE → SUPERSEDED
* = a named person + reason + --confirm "<APPROVE|ACTIVATE|ROLLBACK> <experiment id>"
```

| Step | Requires |
|---|---|
| validate (validation set) | DRAFT. FAIL, INCONCLUSIVE or INVALID → REJECTED automatically (evidence kept) |
| validate `--final` | a passing validation. Runs once. PASS → VALIDATED, otherwise REJECTED |
| request-approval / approve | VALIDATED / AWAITING_APPROVAL + confirmation phrase |
| shadow | APPROVED, and the candidate must not change deterministic strategy settings (those need a new frozen baseline) |
| canary | ≥ `min_shadow_sessions` sessions of shadow research, not mostly failed runs |
| activate | ≥ `canary_sessions` completed canary sessions; canary net ≥ approved AI arm − `canary_max_net_shortfall_inr`; 0 rule violations; confirmation phrase |
| rollback | ACTIVE; restores the version recorded as "previous" at activation; confirmation phrase |

**Authorization is never a flag.** The active version comes from the latest
verified `ACTIVATED` or `ROLLED_BACK_TO` event in a SHA-256 hash chain.
`verify()` checks three things:

- every hash;
- that every status matches its last event;
- that every activation, approval and rollback carries a person (not
  system, auto, validator or monitor) and the exact phrase.

A direct database edit is detected. The integrated runner then **refuses to
start** (`IntegrityError`), and the dashboard shows the failure.

**One active version per context** (`paper-ai`): a second `init` is refused,
and activation supersedes the previous owner.

**Shadow:** the candidate's agent researches at the same timestamps and
writes to `experiments.shadow_db_path`. It gates nothing and orders nothing
(tested).

**Paper canary:** the candidate's agent plus its own gated paper portfolio
(`experiments.canary_db_path`). It is bounded to `canary_sessions` completed
sessions, then stops opening sessions. Its failures are isolated from the
baseline and from the approved arm.

**Version/code binding:** a version records the agent-code hash. If the
running code differs, the runner refuses (`VersionMismatch`), so a code
change needs a new version through this workflow.

## 4. Validation method (leakage-resistant)

- Frozen baseline historical settings and the Phase 7 loader: benchmark
  separation, the corporate-action policy, point-in-time membership when
  provided, and the data audit (survivorship and other limits are listed in
  every report).
- After the warm-up, sessions are split chronologically (40 / 30 / 30) with
  a **1-session embargo** between splits (`embargo_sessions`). Validation is
  split into 3 consecutive folds. Nothing is shuffled. Each window starts
  from fresh capital.
- AI decisions happen at signal time with point-in-time tools. No
  preprocessing is fitted, so nothing can learn from a later period.
- Arms: A (deterministic), APPROVED (active version) and CANDIDATE, with
  identical sessions, data, fills, costs and risk engine.
- Criteria (`accept-v1`, all mandatory unless noted; thresholds are
  configurable, not guarantees):

  | Criterion | What it checks |
  |---|---|
  | minimum evidence | at least 20 sessions, and 30 closed trades in each AI arm |
  | net difference CI > 0 | per-session difference vs APPROVED, paired bootstrap, **Bonferroni-adjusted** for every experiment validated on an overlapping period |
  | drawdown not worse | by more than ₹5 |
  | fold consistency | ≥ 60% of folds not worse |
  | conservative costs | still better with spread and slippage doubled (AI decisions held fixed) |
  | not carried by best sessions | still better without its best 2 sessions |
  | not worse than deterministic | not worse than A |
  | AI quality | 0 rule violations; unsupported-claim rate not more than +5 pts; invalid-output rate ≤ 10%; optional API cost per recommendation |

- Classification:
  - **INVALID**: a weakened mandatory control; changed code, baseline or
    acceptance rules; a different dataset between validation and final;
    irreproducible reruns; final-set reuse.
  - **INCONCLUSIVE**: minimum evidence not met.
  - **FAIL**: a mandatory criterion failed.
  - **PASS**: everything else.
- Reproducibility: arm A is rerun and must match by trade hash. The
  candidate arm is replayed from its recorded decisions and must match. The
  data, configuration, prompt text, model and code hashes are stored.

## 5. Configuration and schema

New `experiments:` settings section (see `config/settings.example.yaml`). The
new database `data/experiments/registry.db` (tables `versions`,
`experiments`, `registry_events`) is created on first use. No existing
database changes, so no migration is needed.

## 6. Commands

```bash
python -m app.experiments init --by NAME --confirm "INIT paper-ai"     # once: current system = approved v1
python -m app.experiments create --spec config/experiments/<candidate>.yaml --by NAME
python -m app.experiments validate --id EXP-.. --data data/historical          # validation set
python -m app.experiments validate --id EXP-.. --data data/historical --final  # final set, once
python -m app.experiments request-approval --id EXP-.. --by NAME --reason ".."
python -m app.experiments approve  --id EXP-.. --by NAME --reason ".." --confirm "APPROVE EXP-.."
python -m app.experiments shadow   --id EXP-.. --by NAME --reason ".."   # then run integrated sessions
python -m app.experiments canary   --id EXP-.. --by NAME --reason ".."   # bounded paper canary
python -m app.experiments activate --id EXP-.. --by NAME --reason ".." --confirm "ACTIVATE EXP-.."
python -m app.experiments rollback --id EXP-.. --by NAME --reason ".." --confirm "ROLLBACK EXP-.."
python -m app.experiments reject   --id EXP-.. --by NAME --reason ".."
python -m app.experiments status | show --id EXP-.. | verify | history
```

Candidate spec example:

```yaml
name: research-v4-less-conservative
hypothesis: A prompt that vetoes fewer deterministically eligible setups raises net P&L after costs.
reason: Phase 13 finding ai:vetoes_positive_expectancy
prompt: {version: research-v4, file: config/experiments/prompts/research-v4.txt}
llm: {model: claude-haiku-5-5}        # optional
tools: null                           # optional allow-list
agent: {}                             # optional limits
settings: {}                          # research-only deterministic overrides (never weaker controls)
```

Validation with the real model costs API calls: about 2 research runs per
signal time. It refuses above `experiments.max_agent_runs`.

## 7. Example baseline-vs-candidate report (SYNTHETIC)

This comes from a deliberately rigged synthetic test, with lowered minimums:
the candidate mock skips the synthetic losing days, and 15 validation
sessions were used. **It shows the machinery, not a real improvement.**

```
# Experiment EXP-20250101-6ab939de - validation - **PASS**
| Metric | A deterministic | APPROVED AI | CANDIDATE |
| closed_trades | 7 | 7 | 4 |
| net_pnl | -42.56 | -42.56 | 62.76 |
| max_drawdown_inr | 76.85 | 76.85 | 3.81 |
- Per-session difference: mean 7.0213, CI [2.3887, 11.57] at adjusted confidence 0.95
- Folds: +34.65, +35.83, +34.84
- Difference under doubled spread and slippage (AI decisions fixed): 117.35
- Difference without the best sessions: 70.67
| minimum_evidence | PASS | ... | net_difference_ci_above_zero | PASS | ... (all 10 mandatory PASS)
```

The same machinery classifies these cases correctly:

- a candidate that skips the winners: **FAIL**;
- the same candidate with the real minimum sample: **INCONCLUSIVE**;
- a candidate raising `max_trades_per_day`: **INVALID**;
- a simulated agent-code change: **INVALID**.

## 8. Tests (actual results)

- `tests/test_experiments.py`: **18 passed** (about 67 s).
- Full suite and gate (`bash scripts/release_gate.sh`, 2026-10-20): **585 passed, 1
  skipped** (the opt-in live-LLM test). RELEASABLE. Frozen baseline verified, no code
  drift. Lint, type-check and dependency audit SKIPPED (tools not installed).

Covered:

- reproducible configurations (content-addressed versions, arm hashes);
- chronological splits with embargo, and folds;
- PASS requiring every mandatory criterion;
- single-use final set;
- INCONCLUSIVE, FAIL and INVALID (weakened control, code change);
- weakened-control detection;
- multiple-testing adjustment;
- classification precedence;
- immutability and tamper detection, with runtime refusal;
- approvals requiring a person and the phrase;
- strategy-changing versions refused at runtime;
- the full shadow → canary → activate → rollback lifecycle with isolation
  and restart;
- one active version per context;
- no order path;
- the dashboard view.

## 9. Known limitations and risks

- **Nothing has been evaluated on real data.** There is no historical data
  folder and no live session.
- **The registry is not initialised on this machine.** Registering the
  current system as approved v1 is a person's decision:
  `python -m app.experiments init --by <you> --confirm "INIT paper-ai"`.
  Until then the integrated runner uses the built-in system, labelled
  "unregistered".
- After `init`, **any agent-code change needs a new version** through this
  workflow before the AI arm will run (intentional).
- Real-model validation costs money and is not perfectly reproducible
  (sampling). Reproducibility is guaranteed by replaying the recorded
  decisions, not by re-asking the model.
- Bonferroni is conservative. With many candidates on one period, very few
  can pass; the intended remedy is fresh data.
- Strategy-parameter candidates can be validated and approved, but
  deployment needs a new frozen baseline (Phase 7). The runtime never
  applies them.
- After a rollback, a SUPERSEDED experiment's status stays SUPERSEDED even
  though its version is active again. The active version is always read from
  the event log.

## 10. Phase 14 checklist

| Requirement | Status |
|---|---|
| Inspect existing implementation and plan | VERIFIED (§1) |
| Versioned experiments for model, prompt, tools, ranking/filter, strategy parameters, research risk/cost assumptions | VERIFIED (risk and cost only as stricter-or-equal) |
| Experiment records (hypothesis, versions, configuration, prompt, model, code, data, periods, criteria, metrics, decision, approver) | VERIFIED |
| Reproducible from recorded configuration and data | VERIFIED (deterministic reruns; replayed AI decisions) |
| Immutable baseline; three-way comparison; restorable previous version | VERIFIED |
| Chronological splits, embargo, folds, no fitted preprocessing, point-in-time decisions, final set untouched | VERIFIED |
| Survivorship, corporate actions, changing universe | VERIFIED where the data supports it (Phase 7 audit); limitations reported |
| Predeclared, versioned, configurable acceptance rules with rationale | VERIFIED |
| PASS / FAIL / INCONCLUSIVE / INVALID with evidence | VERIFIED |
| Uncertainty intervals and multiple-testing adjustment | VERIFIED |
| State machine with rejection and rollback; human approval auditable; shadow and canary isolated; one active version | VERIFIED |
| A flag or database update alone cannot promote | VERIFIED (hash chain plus tamper detection) |
| Risk and execution boundaries; no real-order credentials or paths | VERIFIED |
| Reports distinguish backtest, prospective paper and live | VERIFIED (evaluation kind labelled; live trading never used) |
| Regression suite and security checks | VERIFIED (see gate) |
| A real candidate validated on real data | BLOCKED: no data; registry `init` awaits the user |
