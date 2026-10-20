"""Daily and cumulative evaluation reports, built only from persisted records.

The daily report has eight sections: session summary, market-data health,
signal decisions, paper orders and fills, P&L and costs, risk-control
events, errors and recovery, and missing information / limitations. It
contains no wall-clock time, so it can be regenerated identically.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from app.config import Settings
from app.evaluation.metrics import as_json, cumulative_metrics, daily_series
from app.paper.report import ASSUMPTIONS, LABEL, build_report
from app.paper.store import PaperStore
from app.utils.fs import write_preserving

LIMITATIONS = [
    "Paper fills are simulated. Without fresh bid/ask, entries fill at the next 5-minute "
    "candle's open plus assumed spread and slippage; real fills can differ.",
    "Exits are evaluated on completed 5-minute candles, so a stop is detected up to five "
    "minutes late (the fill price is still the stop, or the open on a gap).",
    "When a candle touches both stop and target, the stop is assumed first (conservative).",
    "Partial fills, queue position and market impact are not modelled.",
    "Groww's 5-minute feed omits the 15:20 candle; it is reported as missing, never filled.",
    "A stop does not cap the loss; gaps and missing data can make it worse.",
]


def daily_report(store: PaperStore, day: date, settings: Settings, evaluation_id: str) -> dict:
    sessions = store.all("SELECT * FROM eval_sessions WHERE evaluation_id = ? AND "
                         "trading_date = ? ORDER BY kind", (evaluation_id, day.isoformat()))
    paper = build_report(store, day, settings) if store.session(day) else None
    dq_rows = store.all("SELECT * FROM dq_batches WHERE trading_date = ? ORDER BY ts, symbol",
                        (day.isoformat(),))
    last_ts = max((r["ts"] for r in dq_rows), default=None)
    final = [r for r in dq_rows if r["ts"] == last_ts]
    steps = store.all("SELECT * FROM eval_steps WHERE trading_date = ? ORDER BY ts",
                      (day.isoformat(),))
    audit = store.all("SELECT * FROM signal_audit WHERE trading_date = ? ORDER BY ts, symbol",
                      (day.isoformat(),))
    events = store.events(day)
    issues = store.all("SELECT * FROM reconciliation_issues WHERE trading_date = ?",
                       (day.isoformat(),))
    decisive = [dict(a) for a in audit if a["decision"] not in (
        "NO_SETUP_YET", "ALREADY_PROCESSED")]
    missing_info = []
    if paper is None:
        missing_info.append("no paper session ran on this date")
    if not dq_rows:
        missing_info.append("no market-data batches were recorded")
    missing_info += [f"{r['symbol']}: {r['missing']} of {r['expected']} candles missing"
                     for r in final if r["missing"]]
    missing_info += [s["fee_model_flag"] for s in sessions if s["fee_model_flag"]]
    return {
        "label": LABEL,
        "trading_date": day.isoformat(),
        "1_session_summary": [
            {k: s[k] for k in ("session_id", "kind", "status", "status_reason", "primary_eval",
                               "provider", "data_mode", "config_version", "strategy_version",
                               "execution_model_version", "fee_schedule_id", "fee_verified_on",
                               "started_at", "ended_at", "steps_expected", "steps_executed")}
            for s in sessions],
        "2_market_data_health": {
            "final_batches": [{k: r[k] for k in ("symbol", "expected", "received", "missing",
                                                 "duplicates", "out_of_order", "invalid_ohlc",
                                                 "invalid_volume", "gaps", "stale", "error")}
                              for r in final],
            "steps": len(steps),
            "steps_signals_blocked": sum(r["signals_blocked"] for r in steps),
            "blocked_reasons": sorted({r["blocked_reason"] for r in steps if r["blocked_reason"]}),
            "incidents": [{"ts": e["ts"], "message": e["message"]}
                          for e in events if e["kind"] == "HEALTH"],
        },
        "3_signal_decisions": {
            "evaluations_recorded": len(audit),
            "decisions": decisive,
            "decision_counts": dict(pd.Series([a["decision"] for a in audit]).value_counts()
                                    .sort_index().astype(int)) if audit else {},
        },
        "4_orders_and_fills": {
            "orders": paper["orders"] if paper else [],
            "unfilled_or_expired": paper["unfilled_or_expired"] if paper else [],
        },
        "5_pnl_and_costs": {
            "closed_positions": paper["closed_positions"] if paper else [],
            "unresolved_positions": paper["unresolved_positions"] if paper else [],
            "totals": paper["totals"] if paper else None,
        },
        "6_risk_control_events": [{"ts": e["ts"], "message": e["message"]}
                                  for e in events if e["kind"] == "RISK"],
        "7_errors_and_recovery": {
            "errors": [{"ts": e["ts"], "message": e["message"]}
                       for e in events if e["level"] == "ERROR"],
            "recovery": [{"ts": e["ts"], "message": e["message"]}
                         for e in events if e["kind"] == "RECOVERY"],
            "reconciliation_issues": [dict(i) for i in issues],
        },
        "8_missing_information_and_limitations": {
            "missing": missing_info,
            "execution_assumptions": ASSUMPTIONS,
            "limitations": LIMITATIONS,
        },
    }


def _clean(obj):
    """JSON-safe copy (numpy ints from pandas -> int)."""
    return json.loads(json.dumps(obj, default=lambda o: int(o) if hasattr(o, "__int__")
                                 else str(o)))


def write_daily(store: PaperStore, day: date, settings: Settings, evaluation_id: str,
                out_dir: Path | None = None) -> Path:
    body = _clean(daily_report(store, day, settings, evaluation_id))
    root = Path(out_dir or settings.evaluation.output_dir) / evaluation_id / "daily"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{day.isoformat()}.json"
    write_preserving(path, as_json(body) + "\n")  # earlier versions go to .history/
    return path


def write_cumulative(store: PaperStore, settings: Settings, evaluation_id: str,
                     out_dir: Path | None = None) -> Path:
    root = Path(out_dir or settings.evaluation.output_dir) / evaluation_id
    root.mkdir(parents=True, exist_ok=True)
    primary = cumulative_metrics(store, evaluation_id, settings, primary_only=True)
    every = cumulative_metrics(store, evaluation_id, settings, primary_only=False)
    ev = store.one("SELECT * FROM evaluations WHERE evaluation_id = ?", (evaluation_id,))
    changes = [dict(r) for r in store.all(
        "SELECT * FROM config_changes WHERE evaluation_id = ? ORDER BY id", (evaluation_id,))]
    write_preserving(root / "cumulative.json", as_json(_clean({
        "label": LABEL, "evaluation_id": evaluation_id, "name": ev["name"],
        "start_date": ev["start_date"], "frozen_config_version": ev["frozen_config_version"],
        "observation_target_sessions": settings.evaluation.observation_target_sessions,
        "primary": primary, "all_kinds": every, "config_changes": changes,
    })) + "\n")
    _csv(pd.DataFrame(daily_series(store, evaluation_id, True)), root / "sessions_primary.csv")
    _csv(pd.DataFrame(daily_series(store, evaluation_id, False)), root / "sessions_all.csv")
    trades = store.all("SELECT p.*, s.kind FROM positions p JOIN eval_sessions s ON "
                       "s.trading_date = p.trading_date AND s.evaluation_id = ? "
                       "ORDER BY p.entry_time", (evaluation_id,))
    _csv(pd.DataFrame([dict(t) for t in trades]), root / "trades.csv")
    _csv(pd.DataFrame([dict(r) for r in store.all(
        "SELECT * FROM signal_audit ORDER BY ts, symbol")]), root / "signal_audit.csv")
    _csv(pd.DataFrame([dict(r) for r in store.all(
        "SELECT * FROM dq_batches ORDER BY ts, symbol")]), root / "data_quality.csv")
    return root / "cumulative.json"


def _csv(frame: pd.DataFrame, path: Path) -> None:
    write_preserving(path, frame.to_csv(index=False))
