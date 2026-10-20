# AI evaluation (Phase 11): does the AI add value?

Research evaluation only. Nothing here places, modifies or cancels an order.
Real orders are always placed manually by the user. A negative or
inconclusive result is a valid outcome, and nothing is tuned from a result.

## What is compared

| | Arm A: deterministic baseline | Arm B: AI-assisted |
|---|---|---|
| Strategy | frozen baseline `orb-baseline-v1` | same |
| Engine | unchanged Phase 4 engine (replay) / live paper engine (prospective) | same |
| Risk and costs | Phase 3 risk/cost engine, Groww fee schedule | same |
| Fills | candle-v1 / paper fills | same |
| Difference | takes every signal the risk engine approves | the AI may **veto** a signal |

**The AI can change only one thing: which deterministic signals are taken (a
veto).** It cannot add a trade, change entry, stop, target, quantity, costs or
risk limits, or approve something the risk engine rejected. Because of this,
any trading difference comes from removing baseline trades. In replay a veto
can also free cash or a trade slot for a later signal. The report attributes
the difference on matched signals:

- **selection**: baseline trades the AI vetoed;
- **capacity**: trades only arm B took because a veto freed room. This is an
  exposure change, not a better pick;
- **shared**: the same signals with different quantities, because the cash
  paths differ.

A **random-veto benchmark** asks whether the AI's vetoes beat vetoing the same
number of baseline trades at random. An AI that vetoes everything "improves"
on any losing baseline just by trading less. The protocol therefore requires
selection beyond random before it can conclude improvement.

Explanatory value (assessment quality) is reported separately from trading
value.

## Two evaluation modes

**Prospective (primary evidence).** Built from live paper records: LIVE,
primary sessions of the active Phase 6 evaluation, on or after the protocol's
`effective_date`, with status COMPLETED. Arm B takes a paper trade only if a
CANDIDATE recommendation for its signal was persisted **before** the entry
order. Recommendations are immutable and timestamped, so hindsight is
impossible. Reasons a trade is not taken: vetoed, not timely, or not reviewed.

```bash
python -m app.aieval prospective [--from 2026-10-21 --to 2026-12-31]
```

**Historical replay (secondary evidence).** Both arms run on a historical
dataset. For each new signal, arm B runs the real research agent at that
signal's timestamp, with point-in-time tools only (completed candles; report
files withheld). Each run is labelled SECONDARY, because the model may have
seen text covering those dates in training.

```bash
python -m app.aieval replay --data data/historical --split development
python -m app.aieval replay --data data/historical --split validation
python -m app.aieval replay --data data/historical --split holdout   # once only
```

Splits are chronological, after the indicator warm-up: development 30%,
validation 20%, holdout 50%. The holdout runs **once** per protocol version and
dataset, and only with the pinned AI system. A changed prompt, model, tool set
or agent code needs a new protocol version and fresh data.

Replay costs API calls: one research run per distinct signal time. It refuses
before any model call if more than `ai_eval.max_agent_runs` runs would be
needed.

Other commands:

```bash
python -m app.aieval protocol          # protocol, pins and any drift
python -m app.aieval status            # all evaluation runs
python -m app.aieval report --run AE-…  # regenerate a report from records
```

The dashboard view is **AI evaluation**.

## Protocol (predeclared)

`config/ai_eval/protocol.yaml` (version `ai-eval-v1`) fixes the following before
any real result:

- primary metrics: the per-session net P&L difference (B - A) and the
  expectancy difference;
- inclusion and exclusion rules, and how unfilled orders, open positions,
  missing data and AI failures are handled;
- minimum evidence: 20 sessions, 30 closed trades per arm, and 10 signals
  where the arms decided differently;
- statistics: a paired circular moving-block bootstrap over sessions (5,000
  resamples, 95%, fixed seed). Both arms of a session are resampled
  together, so shared market moves cancel;
- decision rule: see below;
- the AI-system pins: prompt version, model, tool-schema hash and agent-code
  hash.

Each run stores the protocol's SHA-256, the baseline fingerprint and the AI
versions. A test fails if the agent code changes without the pins being
updated.

### Decision rule

| Conclusion | When |
|---|---|
| PENDING_DATA | no eligible session |
| INCONCLUSIVE | below the minimum evidence (*insufficient evidence*, which is not evidence of no effect), or the CI includes zero and extends beyond ±₹1/session, or a positive difference that random vetoes explain, or unknown model cost |
| EVIDENCE_OF_IMPROVEMENT | CI lower bound > 0, AND selection beyond random vetoes, AND still positive after the model/API cost |
| NO_DEMONSTRATED_IMPROVEMENT | CI upper bound < 0 (worse), or the CI lies inside ±₹1/session (no meaningful difference) |

## Ledger and records

`data/ai_eval/ledger.db` holds:

- `aieval_runs`: one per evaluation; the run key is UNIQUE, so the same
  evaluation is never processed twice. A finished run is final.
- `aieval_decisions`: one per signal per system, holding the point-in-time
  inputs, data sources, versions, screening, proposal (entry condition,
  stop, target, expiry), indicative risk (whole-share quantity, costs, net
  reward:risk), the AI's output and IDs, the final decision and reason, and
  the evaluation status. Rows are hashed, and triggers abort UPDATE and
  DELETE.
- `aieval_outcomes`: append-only. Kinds are `TRADE_CLOSED` (full fee
  breakdown), `ORDER_NOT_FILLED`, and `PRICE_PATH_DIAGNOSTIC` (per share,
  hypothetical).

In replay, the agent's own immutable records (runs, tool calls, model calls
and recommendations) are kept in `data/ai_eval/<run>/agent.db`. Prospective
runs read them from the paper database.

## Metrics

Both arms are measured by the same functions on the same eligible sessions:

- sessions (complete, incomplete, failed);
- candidates evaluated, recommendations taken, rejected and no-trade
  decisions;
- paper orders (filled, partially filled, unfilled);
- gross P&L, costs and net P&L;
- average net per trade, win rate, average win and loss, expectancy, profit
  factor;
- max drawdown, exposure, risk-rule violations and data-quality failures.

Undefined metrics are `n/a` and are never infinity. Unfilled orders and open
positions are never closed trades. A position closed by data end is flagged
incomplete.

**Decision quality** comes from the agent's records:

- rule compliance: CANDIDATE violations, and the AI saying "consider" on
  risk-rejected or stale signals;
- evidence: claims by basis; unsupported claims, split into missing IDs,
  numbers found in no tool output, and probability language; contradicting
  evidence; disclosed missing information; source timestamps;
- consistency: changed without new information, and mixed groups of similar
  inputs;
- operations: API errors, invalid or truncated outputs, tool errors,
  latency, tokens, cost and duplicates.

**Model cost** is reported separately from trading costs. It needs
`llm.input_cost_per_mtok`, `llm.output_cost_per_mtok` (USD) and
`ai_eval.usd_inr_rate`, and is never guessed.

## ₹500 capital

At the frozen ₹500 rules, the risk/cost engine rejected every representative
setup in the Phase 9 audit. When no baseline signal is executable, a veto-only
AI cannot change any trade. The report states this structurally instead of
presenting a zero difference as a finding. Capital is never scaled up to make
either arm look better.

## Limitations

See section 10 of every report, and `docs/phase11_audit.md`.
