# Cost model, position sizing and risk controls (Phase 3)

These are estimates of what a trade would cost. They are not a contract note.
Re-verify the fee schedule against the official sources before relying on it.

## Fee schedule

`broker.name` selects `config/fees/<broker>_nse_equity_intraday.yaml`.
Supplied schedules:

- `groww`: Groww's NSE intraday charges, verified 2026-10-19.
- `illustrative`: an example model for when no broker is selected. Its
  brokerage is invented, and every result is labelled ILLUSTRATIVE.

An unknown broker, a missing rate, a negative rate, a float (unquoted) rate
or invalid dates all fail validation. Nothing defaults to zero.

### Groww rates (as configured)

| Charge | Rate | Side | GST? | Source |
|---|---|---|---|---|
| Brokerage | lower of 0.1% or ₹20 per order; if under ₹5, lower of ₹5 or 2.5% of value | each order | yes | Groww help: intraday charges; groww.in/pricing |
| STT | 0.025% | sell | no | NSE/FATAX/73524 (Finance Act 2026: unchanged) |
| Stamp duty | 0.003% | buy | no | Groww pricing/help |
| NSE transaction charge | 0.0030699% (₹306.99/crore) | both | yes | NSE/FA/73061, effective 1 Mar 2026 |
| NSE IPFT | 0.0000001% (₹0.01/crore) | both | yes | NSE/FA/73061 |
| SEBI turnover fee | 0.0001% (₹10/crore) | both | yes | Groww pricing; SEBI fee schedule |
| GST | 18% of brokerage + exchange + IPFT + SEBI | each order | — | Groww pricing |

Groww's page still shows NSE's pre-March-2026 split (0.00297% + IPFT
0.0001%). The combined ₹307 per crore is identical, so the cost is the same.

**Not included** because they don't apply to a normally closed intraday
trade: DP charges (delivery only) and the ₹50 + GST auto square-off charge.
The auto square-off applies only if a position is still open at **3:20 PM**.
The strategy's exit-by time of 15:10 is earlier than that.

### Effective dates

Each rate applies only within its effective period. Where the start date
couldn't be confirmed, the period starts on the verification date. A trade
date with no rate in effect gives `INSUFFICIENT_DATA`. For research on past
dates, set `cost_review.apply_current_rates_to_past_dates: true`. Every
result then carries a "rate applied retroactively" warning.

## Calculation

All money values use `Decimal`.

```
per order (buy and sell separately):
  turnover   = price × quantity
  brokerage  = max(min(rate × turnover, cap), min(min_per_order, min_pct × turnover))
  each charge on its side only: rate × turnover, rounded per order
  GST        = 18% × (brokerage + GST-applicable charges), rounded per order
gross_pnl    = (exit − entry) × qty
execution    = half-spread × turnover on each fill + slippage × turnover on each fill
total_costs  = buy fees + sell fees + execution
net_pnl      = gross_pnl − total_costs
net_return % = net_pnl / buy turnover × 100
```

**Rounding:** Groww does not publish per-charge rounding rules. Every
component is rounded half-up to the paisa per order, and GST is calculated
on the already-rounded components. This is a documented assumption. Rupee
rounding is supported per charge (`rounding: rupee`) if a broker uses it.

**Spread and slippage** are separate, labelled lines. Fees use reference
prices; the half-spread is charged once per fill, not also built into the
prices, so it is never double-counted. With a fresh quote (no older than
`data.max_staleness_seconds`), the quoted spread is used. Otherwise
`execution.assumed_spread_pct` is used and labelled as assumed.

### Worked example (verified in tests)

Buy 4 @ ₹71.00, sell 4 @ ₹72.20, 0.05% spread, 0.05% slippage per fill:

| | Buy | Sell |
|---|---|---|
| Brokerage | 5.00 (minimum) | 5.00 (minimum) |
| STT | 0.00 | 0.07 |
| Stamp duty | 0.01 | 0.00 |
| Exchange | 0.01 | 0.01 |
| IPFT / SEBI | 0.00 / 0.00 | 0.00 / 0.00 |
| GST (18% of 5.01) | 0.90 | 0.90 |
| **Fees** | **5.92** | **5.98** |

Spread 0.14 + slippage 0.28 gives total costs of **₹12.32**. Gross P&L is
**+₹4.80**, so **net P&L is −₹7.52**: a "winning" trade loses money.

## Break-even

The search runs over exit prices in whole paise, using bisection between
the entry and 3× entry, then steps down while a lower paisa still qualifies.
This finds the smallest price with net P&L ≥ 0, or ≥ a desired profit. Tests
compare the result with a brute-force scan and check that net P&L is ≥ 0 at
the price and < 0 one paisa below it. No qualifying price within the bound
raises an error.

## Position sizing

```
risk_budget            = available_capital × risk_pct / 100
risk_per_share         = entry − stop               (must be > 0)
risk_based_quantity    = floor(risk_budget / risk_per_share)
capital_based_quantity = floor(available_capital / entry)   (and max_position_value)
final quantity         = largest q ≤ both, such that
   q × entry + buy fees + buy execution ≤ available capital, and
   (include_costs_in_risk) net loss at the stop incl. all costs ≤ risk_budget
```

Available capital is capital − reserve − capital in open hypothetical
positions − today's realized loss. Quantity never depends on the score.
There is no margin or leverage: `allow_margin: true` is rejected.

## Daily risk state

`DailyRiskState` is a research ledger. It cannot see or control orders
placed at the broker.

- It locks when realized P&L reaches −`max_daily_loss`. A locked state
  rejects every candidate.
- `roll_to(now)` keeps the same state, lock included, for the whole exchange
  date. It returns a fresh state only on a later date, so a lock never
  resets mid-session.
- Unknown realized P&L (`None`) is never treated as zero. Candidates are
  `INSUFFICIENT_DATA`.
- Other checks: max trades per day, max concurrent positions, aggregate open
  risk, a single trade above the daily limit, realized loss + open risk + new
  trade over the daily limit, duplicate or previously rejected signals.

## Final review status

Only `QUALIFIED_PRE_COST` candidates (Phase 2) are evaluated:

| Status | Meaning |
|---|---|
| QUALIFIED_FOR_REVIEW | sized ≥ 1 share, net reward/risk ≥ minimum, every limit respected |
| INVALID_SETUP | stop/entry/target inconsistent, or signal expired |
| INSUFFICIENT_DATA | fee rates missing or not in effect, or realized P&L unknown |
| REJECTED_RISK_LIMIT | risk per share > budget, daily lock or any portfolio limit |
| REJECTED_CAPITAL | can't afford one share including buy-side costs |
| REJECTED_COSTS | costs push net loss at the stop over the budget, net P&L at target ≤ 0, or net R:R too low |

If several apply, the first in this order wins: INVALID_SETUP >
INSUFFICIENT_DATA > REJECTED_RISK_LIMIT > REJECTED_CAPITAL > REJECTED_COSTS.
Every reason is listed either way. The scan decision is `NO_TRADE` unless at
least one candidate is `QUALIFIED_FOR_REVIEW`.

## What the numbers say about ₹500 at Groww

For the example setup (entry 71.00, stop 70.40, target 72.20) under default
settings, with the daily and aggregate limits scaled with capital:

| Capital | Qty | Net at target | Net R:R | Status |
|---|---|---|---|---|
| ₹500 | 0 | — | — | REJECTED_COSTS (1 share loses ₹4.96 at the stop vs ₹2.50 budget) |
| ₹1,000 | 1 | −₹3.22 | — | REJECTED_COSTS |
| ₹5,000 | 17 | +₹5.73 | 0.23 | REJECTED_COSTS |
| ₹1,00,000 | 590 | +₹561 | 1.12 | REJECTED_COSTS |
| ₹2,00,000 | 1,243 | +₹1,235 | 1.23 | REJECTED_COSTS |

Below a few thousand rupees, the ₹5 / 2.5% minimum brokerage dominates. At
any capital, a 0.85% stop leaves little room once an assumed 0.10% spread,
0.05% slippage per fill and STT are deducted, so net R:R stays below 1.5.
These are consequences of the configured assumptions, shown as found. They
are not evidence about the strategy in real markets, which needs backtesting
(Phase 4).
