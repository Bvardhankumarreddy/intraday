# Integrated AI-assisted paper trading (Phase 12)

**Paper trading only.** Two virtual portfolios run side by side. No real order is
ever placed, modified or cancelled. Real orders are placed manually by the user.
No result here is a promise of profit, and a no-trade session is a valid
outcome.

## What runs

At every candle close, plus the grace period, the existing `SessionScheduler`
(instance lock, holidays, bounded retries, graceful stop, restart catch-up and
Phase 6 recording) calls `IntegratedEngine.step(now)`:

1. **One data snapshot.** `StepSnapshot` wraps the configured provider. Every
   consumer in the step gets the identical response, so both arms see the same
   information. The data mode is never changed.
2. **Baseline arm (A).** The existing `PaperEngine` on `paper.db_path`,
   unchanged. It runs the health check, fills, exits, the screen and ORB
   strategy, the Phase 3 risk/cost check, orders and the reports.
3. **Research agent (Phase 10).** It runs every
   `integrated.agent_every_n_candles` candles (default 1), at the same
   timestamp. It works only from approved read-only tools and calls the model
   only when its inputs changed. Its immutable recommendations are stored in
   the AI arm's database, so its risk tool sees the AI arm's own portfolio.
4. **AI-assisted arm (B).** An identical `PaperEngine` on
   `integrated.ai_db_path`. It applies one extra condition, the AI gate: a
   signal the risk engine approved becomes a paper order only if a
   **CANDIDATE recommendation for that signal was persisted at or before this
   step**. Otherwise the signal is recorded as `NOT_CONFIRMED_BY_AI`, with the
   reason.

| | A baseline | B AI-assisted |
|---|---|---|
| Data | same snapshot | same snapshot |
| Strategy, screen, signals | frozen ORB | same code and settings |
| Sizing, costs, risk limits | Phase 3 engine | same engine (its own portfolio state) |
| Fills, spread, slippage | paper execution model | same |
| Extra condition | none | prior CANDIDATE recommendation (veto only) |

The AI can only **block** a signal. It never creates, sizes or prices an order,
and it never overrides a risk rejection. There is no duplicated strategy, fee,
risk, accounting or execution logic: both arms are the same Phase 5 engine.

## Fallback and safety policy

| Situation | Baseline A | AI arm B | Agent |
|---|---|---|---|
| Model timeout, rate limit or error | continues | no new position; open positions keep their stop, target and EOD exits | records UNAVAILABLE; INTEGRATION warning event |
| Invalid or truncated model output | continues | no new position | INVALID_OUTPUT (counted) |
| Stale or invalid market data | blocks new signals | blocks new signals | abstains without a model call |
| AI-arm engine error | continues | retried each step; after `paper.max_consecutive_failures`, stopped for the day and the session is INCOMPLETE; CRITICAL alert | not affected |
| Baseline engine error | scheduler retries, then aborts (as before) | not stepped | not run |
| Holiday, weekend, special session | not traded | not traded | not run |
| Restart mid-session | resumes from persisted state | resumes from persisted state | crashed run ABANDONED, never reused |

Baseline output is always labelled as the deterministic baseline and is never
shown as AI-generated. Data mode (live, delayed, historical or simulated) is
recorded per session and never switched silently. Non-live sessions never count
as primary evidence.

## Operating procedure

```bash
# once: freeze the protocol and start evaluations in both paper databases
python -m app.integrated start --name "integrated-v1" --from 2026-10-21

# each trading day (blocks until the session ends; Ctrl+C stops safely, rerun resumes)
python -m app.integrated run

# reports
python -m app.integrated report --date 2026-10-21    # daily integrated report
python -m app.integrated cumulative                  # baseline vs AI (Phase 11 protocol)
python -m app.integrated status

# rehearsal on mock or historical data (separate replay databases, never primary)
python -m app.integrated replay --date 2026-10-19 [--data data/historical]

# dashboard: view "Integrated"
.venv/bin/streamlit run app/dashboard.py
```

`start` refuses if the AI system (prompt, model, tool or agent-code hash) differs
from the Phase 11 protocol pins. It records:

- the protocol SHA-256;
- the frozen baseline fingerprint, and whether the live configuration
  matches it;
- the configuration version;
- the AI versions;
- the configured data provider;
- the start date.

Prerequisites for a live session:

1. Fill `config/universe.csv`. **Today it is empty, so no session can run.**
2. Configure the Groww data credentials in `.env`.
3. Configure the Anthropic key in `.env`. Without it, the deterministic mock is
   used and labelled as such.
4. `python -m app.ops health` must not FAIL.

Do not change the prompt, model, tools or strategy during the evaluation. A
change is a new version and needs a new protocol version and a fresh period.
Recommendations carry their versions, and the cumulative report flags any
mid-evaluation change.

## Records

- `paper.db` (A) and `ai_paper.db` (B) each hold their own sessions, signals,
  orders, positions, snapshots, events and Phase 6 evaluation tables. B also
  holds the agent's immutable runs, tool calls, model calls, recommendations
  and append-only outcomes.
- The cumulative comparison writes an immutable decision ledger
  (`ai_eval.ledger_db`) and a reproducible report (`data/ai_eval/<run>/`).
- Daily integrated report: `integrated.reports_dir/<date>/integrated_daily.{md,json}`.
  It is built only from records, labels [FACT], [CALC] and [AI], and gives
  identical bytes when regenerated.

## Limitations

- After the first veto the two portfolios diverge, so later signals can be
  sized differently. The cumulative report separates selection, capacity and
  shared-trade effects, and adds a random-veto benchmark.
- No verified news source: news is reported as UNAVAILABLE.
- Historical candles carry no bid/ask. Live quotes are used when the provider
  supplies them; otherwise the configured spread assumption applies.
- At the frozen ₹500 rules, the risk engine rejects typical setups on costs.
  In the synthetic fixture, costs were ₹12.80 against a ₹7.20 gross target.
  Neither arm can trade then, and the AI cannot change that.
- An observation window of at least 20–30 actual NSE sessions is needed
  before any judgement. It is a starting point, not proof.
