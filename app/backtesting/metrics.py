"""Backtest performance metrics. Definitions (all money in INR, net of costs
unless stated):

- trades: closed simulated round trips.
- win / loss: net_pnl > 0 / net_pnl < 0. A trade with net_pnl == 0 is neither.
- win_rate: wins / trades. None with zero trades.
- avg_win / avg_loss: mean net_pnl of wins / losses. None if there are none.
- expectancy: mean net_pnl per trade. None with zero trades.
- profit_factor: sum of winning net_pnl / |sum of losing net_pnl|. None when
  there are no losing trades (undefined, never reported as infinity) or no
  trades; ``profit_factor_note`` says which.
- max_drawdown: largest peak-to-trough fall of the equity curve (cash +
  mark-to-market of open positions, sampled at every candle close, starting
  from initial capital). Reported in INR and as % of the peak.
- avg_holding_minutes: mean of exit time - entry time.
- cost_pct_of_traded_value: total costs / (buy + sell turnover) x 100.
- cost_pct_of_gross_profit: total costs / sum of positive gross P&L x 100.
  None if no trade had a gross profit.
- net_return_pct: (final cash - initial capital) / initial capital x 100.
- evidence: "INSUFFICIENT EVIDENCE" when trades < min_trades_for_evidence.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

from app.backtesting.engine import BacktestResult, EquityPoint
from app.costs.money import round_money

CAPITAL_INFEASIBLE = {"REJECTED_CAPITAL", "REJECTED_COSTS"}


def _mean(values: list[Decimal]) -> Decimal | None:
    return round_money(sum(values, Decimal(0)) / len(values)) if values else None


def max_drawdown(initial: Decimal, curve: list[EquityPoint]) -> tuple[Decimal, Decimal]:
    """(drawdown in INR, drawdown as % of the running peak)."""
    peak = initial
    worst, worst_pct = Decimal(0), Decimal(0)
    for point in curve:
        peak = max(peak, point.equity)
        dd = peak - point.equity
        if dd > worst:
            worst = dd
            worst_pct = dd / peak * 100 if peak > 0 else Decimal(0)
    return round_money(worst), round_money(worst_pct)


def compute_metrics(result: BacktestResult, min_trades: int) -> dict:
    trades = result.trades
    nets = [t.net_pnl for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    gross_profit = sum((t.gross_pnl for t in trades if t.gross_pnl > 0), Decimal(0))
    total_costs = sum((t.total_costs for t in trades), Decimal(0))
    traded = sum((t.buy_turnover + t.sell_turnover for t in trades), Decimal(0))

    if not trades:
        pf, pf_note = None, "undefined: no trades"
    elif not losses:
        pf, pf_note = None, "undefined: no losing trades"
    else:
        pf = round_money(sum(wins, Decimal(0)) / -sum(losses, Decimal(0)))
        pf_note = ""

    dd, dd_pct = max_drawdown(result.initial_capital, result.equity)
    statuses = Counter(r.status for r in result.rejected)
    n = len(trades)
    return {
        "initial_capital": result.initial_capital,
        "final_capital": result.final_cash,
        "total_gross_pnl": sum((t.gross_pnl for t in trades), Decimal(0)),
        "total_costs": total_costs,
        "total_net_pnl": sum(nets, Decimal(0)),
        "net_return_pct": round_money((result.final_cash - result.initial_capital)
                                      / result.initial_capital * 100),
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven_trades": n - len(wins) - len(losses),
        "win_rate_pct": round_money(Decimal(len(wins)) / n * 100) if n else None,
        "avg_win": _mean(wins),
        "avg_loss": _mean(losses),
        "expectancy_per_trade": _mean(nets),
        "profit_factor": pf,
        "profit_factor_note": pf_note,
        "max_drawdown": dd,
        "max_drawdown_pct": dd_pct,
        "avg_holding_minutes": round_money(Decimal(sum(t.holding_minutes for t in trades)) / n)
        if n else None,
        "cost_pct_of_traded_value": round_money(total_costs / traded * 100) if traded else None,
        "cost_pct_of_gross_profit": round_money(total_costs / gross_profit * 100)
        if gross_profit else None,
        "rejected_signals": len(result.rejected),
        "rejected_by_status": dict(sorted(statuses.items())),
        "capital_or_cost_infeasible_setups": sum(statuses[s] for s in CAPITAL_INFEASIBLE),
        "ambiguous_executions": sum(t.ambiguous for t in trades),
        "incomplete_executions": sum(t.incomplete for t in trades),
        "exit_reasons": dict(sorted(Counter(t.exit_reason for t in trades).items())),
        "sessions": len(result.sessions),
        "evidence": "INSUFFICIENT EVIDENCE" if n < min_trades else "sample meets minimum",
        "min_trades_for_evidence": min_trades,
    }
