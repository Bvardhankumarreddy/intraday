"""End-of-day paper-trading report, built only from persisted state.

The body contains no wall-clock time, so regenerating it from the same
database gives an identical result. Metric definitions match
``app/backtesting/metrics.py`` (win: net > 0, loss: net < 0; profit factor
undefined without losses; drawdown from the stored equity snapshots).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.utils.fs import write_preserving
from app.backtesting.engine import EquityPoint
from app.backtesting.metrics import max_drawdown
from app.config import PROJECT_ROOT, Settings
from app.costs.money import D, round_money
from app.paper.execution import PAPER_EXECUTION_MODEL_VERSION
from app.paper.models import OrderState, PositionState
from app.paper.store import PaperStore

LABEL = ("PAPER TRADING - SIMULATED. No real orders were placed. Fills are modelled from "
         "market data under stated assumptions and are not real executions.")
REPORTS_DIR = PROJECT_ROOT / "data" / "paper" / "reports"

ASSUMPTIONS = [
    f"Execution model {PAPER_EXECUTION_MODEL_VERSION}.",
    "Entries fill at a fresh quote's ask (spread already in the price) or, without one, at the "
    "next candle's open with half the assumed spread charged.",
    "Exits use completed 5-minute candles only: stop, gap through the stop at the open, target, "
    "stop assumed first when a candle touches both, time exit at the first candle at/after the "
    "exit time.",
    "Fees come from the Phase 3 fee schedule; slippage and (candle fills) spread are assumptions.",
    "Positions whose data is missing are FLAGGED, never closed with an invented price.",
]


def _sum(values) -> Decimal:
    return sum((D(v) for v in values), Decimal(0))


def build_report(store: PaperStore, day: date, settings: Settings) -> dict:
    session = store.session(day)
    if session is None:
        raise ValueError(f"no paper session for {day}")
    signals = store.signals(day)
    orders = store.orders(day)
    closed = store.positions(day, (PositionState.CLOSED,))
    unresolved = store.positions(day, (PositionState.OPEN, PositionState.FLAGGED))
    events = store.events(day)
    snaps = store.snapshots(day)

    nets = [D(p["net_pnl"]) for p in closed]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    n = len(nets)
    min_trades = settings.backtest.min_trades_for_evidence
    cost_totals: dict[str, Decimal] = {}
    for p in closed:
        for side in json.loads(p["costs"]).values():
            for k, v in side.items():
                if k != "total":
                    cost_totals[k] = cost_totals.get(k, Decimal(0)) + D(v)
    curve = [EquityPoint(r["ts"], D(r["cash"]), D(r["position_value"]), D(r["equity"]))
             for r in snaps]
    start_cash = D(session["start_cash"])
    dd, dd_pct = max_drawdown(start_cash, curve)
    end_equity = curve[-1].equity if curve else start_cash

    def order_row(o):
        return {k: o[k] for k in ("order_id", "signal_id", "symbol", "side", "kind", "quantity",
                                  "state", "created_at", "reference_price", "fill_price",
                                  "fill_time", "fill_basis", "reason", "est_fees", "est_spread",
                                  "est_slippage", "execution_model_version")}

    return {
        "label": LABEL,
        "trading_date": day.isoformat(),
        "session": {k: session[k] for k in (
            "state", "data_mode", "data_source", "config_version", "strategy_version",
            "execution_model_version", "fee_schedule_id", "start_cash", "locked", "lock_reason")},
        "signals_generated": len(signals),
        "signals": [{"signal_id": s["signal_id"], "symbol": s["symbol"],
                     "signal_time": s["signal_time"], "review_status": s["review_status"],
                     "quantity": s["quantity"], "reasons": json.loads(s["reasons"])}
                    for s in signals],
        "signals_rejected": sum(s["review_status"] != "QUALIFIED_FOR_REVIEW" for s in signals),
        "orders": [order_row(o) for o in orders],
        "fills": [order_row(o) for o in orders if o["state"] == OrderState.FILLED.value],
        "unfilled_or_expired": [order_row(o) for o in orders if o["state"] in (
            OrderState.EXPIRED.value, OrderState.REJECTED.value, OrderState.CANCELLED.value)],
        "closed_positions": [
            {k: p[k] for k in ("position_id", "symbol", "quantity", "entry_price", "entry_time",
                               "exit_price", "exit_time", "exit_reason", "stop", "target",
                               "planned_net_loss", "gross_pnl", "total_costs", "net_pnl",
                               "ambiguous")} | {"costs": json.loads(p["costs"])}
            for p in closed],
        "unresolved_positions": [{k: p[k] for k in ("position_id", "symbol", "state",
                                                    "flag_reason", "entry_price", "quantity")}
                                 for p in unresolved],
        "totals": {
            "gross_pnl": str(_sum(p["gross_pnl"] for p in closed)),
            "total_costs": str(_sum(p["total_costs"] for p in closed)),
            "net_pnl": str(sum(nets, Decimal(0))),
            "cost_breakdown": {k: str(v) for k, v in sorted(cost_totals.items())},
            "start_cash": str(start_cash),
            "end_equity": str(end_equity),
            "max_drawdown": str(dd),
            "max_drawdown_pct": str(dd_pct),
        },
        "statistics": {
            "closed_trades": n,
            "win_rate_pct": str(round_money(Decimal(len(wins)) / n * 100)) if n else None,
            "expectancy_per_trade": str(round_money(sum(nets, Decimal(0)) / n)) if n else None,
            "profit_factor": (str(round_money(sum(wins, Decimal(0)) / -sum(losses, Decimal(0))))
                              if losses else None),
            "evidence": "INSUFFICIENT EVIDENCE" if n < min_trades else "sample meets minimum",
        },
        "operational_errors": [{"ts": e["ts"], "message": e["message"]}
                               for e in events if e["level"] == "ERROR"],
        "data_events": [{"ts": e["ts"], "message": e["message"]}
                        for e in events if e["kind"] == "HEALTH"],
        "assumptions": ASSUMPTIONS,
    }


def export_report(store: PaperStore, day: date, out_dir: Path | None = None) -> Path:
    """Write the stored report as JSON (exact) and Markdown (readable)."""
    body = store.report(day)
    if body is None:
        raise ValueError(f"no stored report for {day}")
    out = Path(out_dir or REPORTS_DIR)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"paper_{day.isoformat()}.json"
    write_preserving(path, json.dumps(body, indent=2, sort_keys=True) + "\n")
    t = body["totals"]
    lines = [
        f"# Paper trading report - {day.isoformat()}", "", f"**{body['label']}**", "",
        f"- Data: {body['session']['data_source']} [{body['session']['data_mode'].upper()}]",
        f"- Signals: {body['signals_generated']} generated, {body['signals_rejected']} rejected",
        f"- Fills: {len(body['fills'])}; unfilled/expired: {len(body['unfilled_or_expired'])}",
        f"- Closed positions: {len(body['closed_positions'])}; unresolved: "
        f"{len(body['unresolved_positions'])}",
        f"- Gross P&L Rs {t['gross_pnl']}, costs Rs {t['total_costs']}, "
        f"**net P&L Rs {t['net_pnl']}**",
        f"- Equity Rs {t['start_cash']} -> Rs {t['end_equity']}; max drawdown Rs "
        f"{t['max_drawdown']} ({t['max_drawdown_pct']}%)",
        f"- Evidence: {body['statistics']['evidence']}",
        f"- Operational errors: {len(body['operational_errors'])}; data events: "
        f"{len(body['data_events'])}",
        "", "## Assumptions", *[f"- {a}" for a in body["assumptions"]], "",
    ]
    write_preserving(out / f"paper_{day.isoformat()}.md", "\n".join(lines))
    return path


def regenerate_report(store: PaperStore, day: date, settings, now) -> dict:
    """Rebuild a day's report from persisted records (e.g. after a failed
    generation). Earlier versions stay in ``report_history``."""
    body = build_report(store, day, settings)
    with store.transaction():
        store.save_report(day, now, body)
        store.event(now, day, "REPORT", "INFO", "daily report generated (regenerated)")
    from app.ops.alerts import AlertManager

    AlertManager(store, settings).resolve(f"report.failed.{day}", now)
    return body
