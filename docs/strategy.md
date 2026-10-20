# Opening-range breakout: rules, indicators and scoring

This document describes what the code does. Passing tests show that the
code follows these rules. They are **not** evidence that the strategy makes
money. Every parameter is a hypothesis for backtesting (Phase 4).

## Screening (`app/screening/screener.py`)

Each instrument gets one result: `PASS`, `REJECTED` or `INSUFFICIENT_DATA`,
with every reason listed. A definitive failure means `REJECTED`. If there is
no failure but some input is missing, the result is `INSUFFICIENT_DATA`.

| Check | Rule | If input missing |
|---|---|---|
| Eligibility | exchange NSE, series in `allowed_series`, tradable | — |
| Price | latest price known at evaluation time within `min_price`..`max_price` (inclusive) | INSUFFICIENT_DATA |
| Freshness | today's last completed candle is not behind the evaluation time by more than `max_staleness_seconds` | INSUFFICIENT_DATA |
| Liquidity | mean traded value over `liquidity_lookback_sessions` completed sessions ≥ `min_avg_traded_value` | INSUFFICIENT_DATA if fewer sessions |
| Spread | quote spread ≤ `max_spread_pct` of mid | INSUFFICIENT_DATA if `require_spread`, else noted as unavailable |

"Latest price" means the quote's last traded price if it is newer than the
last completed candle, otherwise that candle's close. Before the session has
any candles, it is the previous session's close (labelled as such).

Traded value is approximated as close × volume per session, because the
current providers do not supply exchange turnover.

## Indicators (`app/strategies/indicators.py`)

| Indicator | Definition |
|---|---|
| Session VWAP | Σ(typical × volume) / Σvolume from the session open, typical = (H+L+C)/3. Resets each session. Unavailable until volume trades. |
| Opening range | High/low of the candles starting 09:15–09:29 (three 5m candles). Unavailable until all three exist and are completed. |
| Range width | (high − low) / midpoint × 100 |
| Relative volume (cumulative, default) | Volume from the open through the candle ÷ mean of the same window over up to `relative_volume_lookback_sessions` prior sessions |
| Relative volume (candle) | The candle's volume ÷ mean volume of the same time slot in prior sessions |
| EMA (optional) | α = 2/(n+1), seeded with the SMA of the first n completed closes, continuous across sessions |

In the relative-volume baseline, prior sessions whose data ends before the
time slot are skipped. In candle mode, sessions missing the exact slot are
skipped. Fewer than `relative_volume_min_sessions` usable sessions means
INSUFFICIENT_DATA.

## Signal rules (`app/strategies/opening_range_breakout.py`)

All of these must hold on one **completed** candle (the signal candle):

1. The opening range is complete.
2. Fresh breakout: the candle closes above the range high and the previous
   candle closed at or below it.
3. The candle closes at or before `last_signal_time` (14:30).
4. Relative volume ≥ `relative_volume_threshold` (1.5).
5. Close > VWAP. If `use_ema_filter` is on, close > EMA as well.
6. Close is no more than `max_extension_pct` (1%) above the range high.
7. The stop (per `stop_reference`) is below the entry, and risk per share is
   `min_risk_pct`..`max_risk_pct` (0.2%–3%) of the entry.

The first candle that passes every rule is the day's only signal. Later
breakouts and repeated scans therefore can't produce duplicates, and the
signal ID is deterministic (`NSE:SYMBOL:ORB:<candle start>`).

**Entry reference** is the signal candle's close, which is known only when
that candle ends (`signal_time`). It is a reference level, not a fill price.
Backtests must fill at the next available price plus slippage.

**Target** = entry + `min_reward_risk` × risk, rounded up to the paisa.
This is the planned reward/risk before costs. Phase 3 adds costs.

## Signal states

```
                 ┌── range forming / no breakout yet / rule failed, time left ──► WATCH
screen PASS ─────┤
                 ├── all rules pass on a completed candle ──► QUALIFIED_PRE_COST
                 │        ├── later candle trades at/below stop ──► INVALIDATED
                 │        ├── later candle reaches target ─────────► INVALIDATED
                 │        └── now > signal_time + signal_valid_minutes ► EXPIRED
                 ├── no valid breakout by last_signal_time ──► REJECTED
                 └── missing range candles / volume baseline / VWAP ──► INSUFFICIENT_DATA
screen REJECTED or INSUFFICIENT_DATA ──► NOT_EVALUATED
```

A scan's decision is `CANDIDATES_FOR_REVIEW` if any candidate is
`QUALIFIED_PRE_COST`, otherwise `NO_TRADE`. "Qualified" means only that the
Phase 2 rules passed. Quantity, affordability, costs and net P&L are not
evaluated until Phase 3.

## Score (`app/screening/ranking.py`)

The score is calculated only for `QUALIFIED_PRE_COST` candidates. It orders
them and cannot override a rejection. **It is not a probability of profit.**

| Component | Weight | 0 → 1 normalisation |
|---|---|---|
| Liquidity | 25 | log scale: screening minimum → 20× minimum |
| Relative volume | 25 | linear: threshold (1.5) → 3.0 |
| Breakout quality | 20 | mean of close location in candle range and (1 − extension / max extension) |
| Trend | 15 | close above VWAP: 0% → 1% (averaged with EMA distance if the filter is on) |
| Spread | 15 | 1 − spread / max spread |

A missing input scores 0 for that component and is flagged in its note.
Ties are broken by higher average traded value, then symbol A→Z.

## Example output (synthetic test data)

```
DECISION: CANDIDATES FOR REVIEW

#1 SYNTH (SYNTH Ltd)  score 56.0/100  [QUALIFIED_PRE_COST]
   signal       : 5m candle closed above opening-range high 70.50; review only after 09:40 and while price holds above 70.40
   entry ref    : 71.00   (signal candle close, known at candle end; backtests fill at the next available price, not this close)
   stop ref     : 70.40   (low of the signal candle)
   target ref   : 72.20   (entry + 2 x risk per share (before costs))
   risk/share   : 0.60   planned R:R 2.00 before costs
   valid until  : 09:50   exit by 15:10
   score        : liquidity 22.02/25, relative_volume 5/25, breakout_quality 12.14/20, trend 10.4/15, spread 6.43/15
   quantity/costs: NOT EVALUATED (Phase 3)
```

On the bundled mock data, scans usually return `NO TRADE`. Breakouts occur,
but the mock's volume is random and unrelated to price, so relative volume
rarely reaches 1.5. The mock exists to exercise the code, not to model real
market behaviour.
