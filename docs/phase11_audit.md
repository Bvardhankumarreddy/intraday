# Phase 11 audit: Phase 10 agent before extension, and Phase 11 status

Audit date: 2026-10-20. Every result below was observed in this repository.
Nothing is estimated.

## 1. Phase 10 inventory (what exists)

| Component | Location | Observed state |
|---|---|---|
| LLM provider | `app/agent/llm.py` | Anthropic Messages API (`claude-haiku-5-5`) and a deterministic mock. Live connectivity verified 2026-10-20 (`check-llm` OK; opt-in live test passed). |
| Prompts | `app/agent/prompts.py` | `research-v3` (was v1 at the start of Phase 11, see §2), `review-v1` |
| Tools | `app/agent/tools.py` | 11 read-only tools; no order, write, shell or network capability (tested) |
| Screening and strategy | `app/screening`, `app/strategies`, `app/scan` | frozen; unchanged |
| Risk and costs | `app/risk`, `app/costs` | frozen; unchanged |
| Backtest and paper | `app/backtesting`, `app/paper` | frozen; unchanged |
| Recommendation store | `app/agent/store.py` | immutable (triggers); outcomes append-only |
| Dashboards and reports | `app/dashboard.py`, `app/agent/views.py`, `app/agent/report.py` | present |

### Is the agent functional? (verified, not assumed)

A real-model research run on **synthetic** fixture data, with an isolated
database, ran the full workflow: 8 workflow tool calls, 2 model-requested tool
calls, and 3 model requests.

## 2. Defects found and fixed

| # | Defect | Evidence | Fix |
|---|---|---|---|
| 1 | **A truncated model response was accepted as valid.** At the 2000-token limit the API returns the parsable prefix of the cut-off tool call. It validated with the `candidates` section missing, so the run reported `ai OK` while the stock silently became `INVALID_OUTPUT`. | Real run: two responses with `stop_reason: max_tokens` | A submission from a `max_tokens` response is always rejected and sent back (research and review loops). The output limit was raised to 6000. Test `test_truncated_model_output_is_never_accepted`. |
| 2 | Thousands separators broke the unsupported-number check: "12,600" was read as 12 and 600 and flagged as a false positive. | Real run | `NUMBER` treats digit groups (including Indian 1,26,000) as one number. Test added. |
| 3 | In historical replay, `get_reports_summary` read the latest report files on disk, which would leak information from after the decision time. | Code review | `ToolContext.point_in_time`: reports after the decision are withheld; the robustness verdict is withheld. Test added. |
| 4 | The model put a per-candidate value (`wait_for_confirmation`) in the run-level `overall` field. The first submission failed validation in 5 of 5 runs, each costing one extra request. | Real runs | Schema field description and prompt `research-v3`. Re-measured: 4 of 5 runs still fail once and self-correct within budget, with no invalid output. Left as is: further prompt tuning on synthetic data is not justified. |
| 5 | The Phase 10 `compare` A arm is not the actual baseline. It uses agent-observed eligible recommendations, a fresh-capital quantity, and entry at the first candle after the agent's run (the agent runs every 3 candles), so its timing differs from the engine. | Code review | Superseded by Phase 11: arm A is the unchanged engine (replay) or the paper engine (prospective). `compare` remains as a quick Phase 10 view. |

The AI-system changes (fixes 1, 2 and 4) were made **before any real-data
evaluation**. The protocol pins were re-taken afterwards, and the protocol
header records the amendment.

## 3. Behaviour observed with the real model (synthetic data, not evidence)

Three replays of the same 5 synthetic signals, each of which the deterministic
rules approve:

- The model answered **`wait_for_confirmation` on all 15 assessments**. In
  practice that is a blanket veto: arm B took 0 trades, against 5 for arm A.
- All assessments cited contradicting evidence and disclosed missing
  information. There were no probability claims, no source references dated
  after the decision, and no candidate rule violations.
- Unsupported claims per run: 5, then 8, then 2. All were legitimate flags:
  facts without evidence IDs, and approximate numbers.
- About 120-136k input and 12-17k output tokens per 5 research runs. Mean
  latency was about 4-5 s per request.

Because a blanket veto "beats" any losing baseline just by not trading, the
protocol now requires the AI's vetoes to beat **random vetoes of the same
number of trades** before it can conclude improvement. On these runs the
benchmark reports `VETOED_ALL`: equivalent to not trading, so no selection
skill can be shown.

## 4. What the architecture can and cannot show

The AI is veto-only. It can remove baseline trades and nothing else. It cannot
add a trade or improve one. Any improvement must therefore come from avoiding
losers better than chance. At the frozen ₹500 rules, the Phase 9 audit found
no executable setup. With no executable baseline signal, the AI's trading value
is zero by construction, and reports say so structurally.

## 5. Tests (actual results, 2026-10-20)

- `tests/test_aieval.py`: 29 tests. `tests/test_agent.py`: 32 tests (2 added
  in this phase).
- Full suite via the release gate: **521 passed, 1 skipped** (the opt-in
  live-LLM test). Gate verdict RELEASABLE. Frozen baseline verified, with no
  code drift. Lint, type and dependency-audit tools are not installed
  (SKIPPED).
- Opt-in live LLM test: passed (`LLM_LIVE_TEST=1`).

## 6. Empirical status

| Mode | Command | Result |
|---|---|---|
| Historical replay | `python -m app.aieval replay --data data/historical --split development` | **PENDING_DATA**: `data/historical` does not exist |
| Prospective | `python -m app.aieval prospective` | **PENDING_DATA**: no active Phase 6 evaluation; no live paper session |

Final conclusion: **INCONCLUSIVE / PENDING DATA.** There is no evidence that
the AI improves on the baseline, and none that it does not. The evaluation
infrastructure is complete and tested.

Needed for a real result, in order:

1. Fill `config/universe.csv`.
2. Start a Phase 6 evaluation and run live paper sessions on or after
   2026-10-21. Set `agent.intraday_every_n_candles: 1` so the AI reviews each
   signal before its entry.
3. Optionally, fetch history for the replay.
4. Configure model prices and `ai_eval.usd_inr_rate` so that net incremental
   value can be computed.

## 7. Outstanding defects and limitations

- The real model's first submission often fails schema validation once
  (self-corrected within budget).
- Replay: the agent's risk tool sees a fresh-capital portfolio, while the
  engine re-checks with the arm's own portfolio.
- Only the top `agent.max_candidates` setups are reviewed per run.
- No verified news source.
- The number check skips integers up to 10 and numbers written as words.
- Prospective arm B does not re-use capacity freed by a veto.
- Lint, type-check and dependency-audit tools are not installed.
