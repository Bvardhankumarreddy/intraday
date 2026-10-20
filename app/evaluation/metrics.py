"""Evaluation metrics, computed only from persisted, reconciled records.

Scope: by default the PRIMARY evaluation - LIVE sessions on the frozen
configuration. Replay, simulated, delayed or changed-config sessions are
reported separately and never mixed in.

Session counts: scheduled = every regular NSE trading day from the
evaluation start to the last synced day; completed / incomplete / failed /
running by status. Sessions with trades = sessions with at least one closed
paper trade.

Trades are CLOSED paper positions from sessions in scope. Eligible trades
exclude any trade with a reconciliation issue (reported as excluded).
Open or flagged positions are never counted as trades; unfilled, rejected
and expired orders never count towards the win rate.

- win / loss: net P&L > 0 / < 0; net P&L == 0 counts as neither
- win_rate = wins / eligible trades (None with no trades)
- avg_net = expectancy = sum(net) / eligible trades
- avg_win / avg_loss = mean net of wins / losses (None if none)
- profit_factor = sum(win net) / |sum(loss net)|; None ("undefined") with no losses
- max_drawdown: peak-to-trough of the equity series built from end-of-step
  snapshots of all in-scope sessions in time order (cash + marked positions)
- avg_holding_minutes = mean(exit time - entry time)
- exits by reason (TARGET, STOP, STOP_GAP, AMBIGUOUS_STOP_FIRST, END_OF_DAY...)
- evidence: "INSUFFICIENT SAMPLE" below ``backtest.min_trades_for_evidence``
  closed trades; no statistical confidence or profitability is inferred.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from decimal import Decimal

from app.backtesting.engine import EquityPoint
from app.backtesting.metrics import max_drawdown
from app.config import Settings
from app.costs.money import D, round_money
from app.paper.store import PaperStore


def in_scope_sessions(store: PaperStore, evaluation_id: str, primary_only: bool = True):
    sql = "SELECT * FROM eval_sessions WHERE evaluation_id = ?"
    if primary_only:
        sql += " AND primary_eval = 1 AND kind = 'LIVE'"
    return store.all(sql + " ORDER BY trading_date, kind", (evaluation_id,))


def _trades(store: PaperStore, dates: list[str]):
    if not dates:
        return []
    marks = ", ".join("?" for _ in dates)
    return store.all(f"SELECT * FROM positions WHERE trading_date IN ({marks}) "
                     "AND state = 'CLOSED' ORDER BY exit_time", tuple(dates))


def cumulative_metrics(store: PaperStore, evaluation_id: str, settings: Settings,
                       primary_only: bool = True) -> dict:
    sessions = in_scope_sessions(store, evaluation_id, primary_only)
    dates = [s["trading_date"] for s in sessions]
    statuses = Counter(s["status"] for s in sessions)
    issue_refs = {r["ref"] for r in store.all("SELECT ref FROM reconciliation_issues")}

    closed = _trades(store, dates)
    eligible = [p for p in closed if p["position_id"] not in issue_refs]
    nets = [D(p["net_pnl"]) for p in eligible]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    n = len(nets)

    def mean(values):
        return str(round_money(sum(values, Decimal(0)) / len(values))) if values else None

    snaps = []
    for d in dates:
        snaps += [EquityPoint(datetime.fromisoformat(r["ts"]), D(r["cash"]),
                              D(r["position_value"]), D(r["equity"]))
                  for r in store.snapshots(datetime.fromisoformat(d).date())]
    snaps.sort(key=lambda e: e.time)
    initial = D(store.portfolio()["initial_capital"]) if store.one(
        "SELECT 1 FROM portfolio") else Decimal(0)
    dd, dd_pct = max_drawdown(snaps[0].equity if snaps else initial, snaps)

    signals = _count(store, "SELECT COUNT(*) AS n FROM signals WHERE trading_date IN", dates)
    rejected = store.all(_in("SELECT review_status, reasons FROM signals WHERE "
                             "review_status != 'QUALIFIED_FOR_REVIEW' AND trading_date IN",
                             dates), tuple(dates)) if dates else []
    orders = store.all(_in("SELECT o.side, o.state FROM orders o JOIN signals s "
                           "USING (signal_id) WHERE s.trading_date IN", dates),
                       tuple(dates)) if dates else []
    buy_states = Counter(o["state"] for o in orders if o["side"] == "BUY")
    dq = store.one(_in("SELECT COUNT(*) AS batches, SUM(missing > 0) AS missing_batches, "
                       "SUM(duplicates) AS duplicates, SUM(out_of_order) AS out_of_order, "
                       "SUM(invalid_ohlc + invalid_volume) AS invalid, SUM(stale) AS stale, "
                       "SUM(error IS NOT NULL) AS provider_errors FROM dq_batches WHERE "
                       "trading_date IN", dates), tuple(dates)) if dates else None
    steps = store.one(_in("SELECT COUNT(*) AS steps, SUM(signals_blocked) AS blocked, "
                          "SUM(provider_state IN ('DISCONNECTED')) AS down FROM eval_steps "
                          "WHERE trading_date IN", dates), tuple(dates)) if dates else None
    errors = _count(store, "SELECT COUNT(*) AS n FROM events WHERE level = 'ERROR' AND "
                    "trading_date IN", dates)
    min_trades = settings.backtest.min_trades_for_evidence
    return {
        "scope": "primary (LIVE, frozen config)" if primary_only else "all session kinds",
        "sessions": {
            "scheduled": len(sessions),
            "completed": statuses.get("COMPLETED", 0),
            "incomplete": statuses.get("INCOMPLETE", 0),
            "failed": statuses.get("FAILED", 0),
            "running_or_scheduled": statuses.get("RUNNING", 0) + statuses.get("SCHEDULED", 0),
            "with_trades": len({p["trading_date"] for p in closed}),
        },
        "funnel": {
            "signals": signals,
            "signals_rejected": len(rejected),
            "orders_created": sum(buy_states.values()),
            "orders_filled": buy_states.get("FILLED", 0),
            "orders_unfilled": sum(v for k, v in buy_states.items() if k != "FILLED"),
            "closed_trades": len(closed),
            "trades_excluded_reconciliation": len(closed) - n,
        },
        "trades": {
            "eligible": n,
            "wins": len(wins), "losses": len(losses),
            "win_rate_pct": str(round_money(Decimal(len(wins)) / n * 100)) if n else None,
            "gross_pnl": str(sum((D(p["gross_pnl"]) for p in eligible), Decimal(0))),
            "total_costs": str(sum((D(p["total_costs"]) for p in eligible), Decimal(0))),
            "net_pnl": str(sum(nets, Decimal(0))),
            "avg_net_per_trade": mean(nets),
            "expectancy_per_trade": mean(nets),
            "avg_win": mean(wins), "avg_loss": mean(losses),
            "profit_factor": (str(round_money(sum(wins, Decimal(0)) / -sum(losses, Decimal(0))))
                              if losses else None),
            "profit_factor_note": "" if losses else "undefined: no losing trades",
            "max_drawdown": str(dd), "max_drawdown_pct": str(dd_pct),
            "avg_holding_minutes": str(round_money(Decimal(sum(
                (datetime.fromisoformat(p["exit_time"]) - datetime.fromisoformat(p["entry_time"]))
                .total_seconds() / 60 for p in eligible)) / n)) if n else None,
            "exits": dict(sorted(Counter(p["exit_reason"] for p in eligible).items())),
        },
        "rejections": dict(sorted(Counter(r["review_status"] for r in rejected).items())),
        "data_quality": {k: (dq[k] or 0) for k in dq.keys()} if dq else {},
        "monitoring": {
            "steps": (steps["steps"] or 0) if steps else 0,
            "steps_signals_blocked": (steps["blocked"] or 0) if steps else 0,
            "steps_provider_down": (steps["down"] or 0) if steps else 0,
        },
        "operational_errors": errors,
        "reconciliation_issues": len(issue_refs),
        "evidence": ("INSUFFICIENT SAMPLE" if n < min_trades else "sample meets minimum")
        + f" ({n} eligible closed trades; minimum {min_trades}). Not evidence of profitability.",
    }


def _in(sql: str, dates: list[str]) -> str:
    return f"{sql} ({', '.join('?' for _ in dates)})"


def _count(store: PaperStore, sql: str, dates: list[str]) -> int:
    if not dates:
        return 0
    return store.one(_in(sql, dates), tuple(dates))["n"]


def daily_series(store: PaperStore, evaluation_id: str, primary_only: bool = True) -> list[dict]:
    """Per-session rows: status, trades, costs, net and cumulative net."""
    rows, cumulative = [], Decimal(0)
    for s in in_scope_sessions(store, evaluation_id, primary_only):
        closed = store.all("SELECT * FROM positions WHERE trading_date = ? AND state = 'CLOSED'",
                           (s["trading_date"],))
        net = sum((D(p["net_pnl"]) for p in closed), Decimal(0))
        cumulative += net
        snaps = store.all("SELECT equity FROM snapshots WHERE trading_date = ? ORDER BY ts",
                          (s["trading_date"],))
        rows.append({
            "trading_date": s["trading_date"], "kind": s["kind"], "status": s["status"],
            "status_reason": s["status_reason"], "trades": len(closed),
            "gross_pnl": str(sum((D(p["gross_pnl"]) for p in closed), Decimal(0))),
            "costs": str(sum((D(p["total_costs"]) for p in closed), Decimal(0))),
            "net_pnl": str(net), "cumulative_net_pnl": str(cumulative),
            "end_equity": snaps[-1]["equity"] if snaps else None,
            "signals": store.one("SELECT COUNT(*) AS n FROM signals WHERE trading_date = ?",
                                 (s["trading_date"],))["n"],
            "config_version": s["config_version"], "fee_schedule_id": s["fee_schedule_id"],
            "fee_model_flag": s["fee_model_flag"],
        })
    return rows


def as_json(data) -> str:
    return json.dumps(data, indent=2, sort_keys=True, default=str)
