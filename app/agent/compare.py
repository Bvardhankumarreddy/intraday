"""Does the AI add value? A (deterministic baseline) vs B (AI-assisted).

Both arms use the SAME research runs: the same time, universe, market data
and deterministic tool outputs. They differ only in selection:

- A: every intraday recommendation that passed every deterministic check
  (screen PASS, valid strategy setup, risk engine QUALIFIED_FOR_REVIEW, fresh
  data). That is what the existing strategy would take without AI selection.
- B: the recommendations with status CANDIDATE (A minus what the AI
  downgraded to WATCH or NO_TRADE). B is a subset of A by construction. Any
  B record outside A is counted as a rule violation, and should be 0.

Outcomes are hypothetical and identical for both arms:
- Phase 4 candle-execution rules (``entry_fill`` / ``exit_fill``);
- entry at the open of the first candle starting at or after the
  recommendation time (no look-ahead);
- exit at stop, target or the strategy's end-of-day time;
- the risk engine's whole-share quantity;
- Phase 3 ``round_trip`` costs.

Each signal is counted once (deduplicated by signal ID).

Also reported: abstention rate, rejection reasons, AI availability, latency,
cost, unsupported claims and per-session counts. The sample size is always
shown. Small samples are labelled as insufficient to judge value, and
nothing is tuned from the result.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd

from app.backtesting.execution import Candle, ExitReason, entry_fill, exit_fill
from app.costs.calculator import round_trip
from app.costs.money import D
from app.costs.schedule import load_fee_schedule
from app.data.models import DataError, parse_interval


def simulate(rec: dict, provider, settings, calendar, until: datetime | None = None) -> dict:
    """Hypothetical outcome of one recommendation from completed candles."""
    if rec.get("entry_reference") is None or rec.get("stop") is None or not rec.get("target"):
        return {"status": "NO_SETUP"}
    created = datetime.fromisoformat(rec["created_at"])
    day = created.date()
    iv = parse_interval(settings.strategy.candle_interval)
    try:
        frame = provider.get_candles(rec["symbol"], iv.label, day, day).frame
    except DataError as exc:
        return {"status": "NO_DATA", "error": str(exc)[:200]}
    if until is not None:
        frame = frame[frame["timestamp"] + iv.timedelta <= pd.Timestamp(until)]
    later = frame[frame["timestamp"] >= pd.Timestamp(created)]
    if later.empty:
        return {"status": "NO_DATA_AFTER_RECOMMENDATION"}
    ref, stop, target = D(rec["entry_reference"]), D(rec["stop"]), D(rec["target"])
    first = later.iloc[0]
    price, why = entry_fill(Candle.from_row(first), ref, stop, settings.backtest.max_entry_gap_pct)
    if price is None:
        return {"status": "NOT_FILLED", "reason": why}
    entry_time = first["timestamp"]
    eod = pd.Timestamp(datetime.combine(day, settings.strategy.eod_exit_time, tzinfo=calendar.tz))
    hi = lo = price
    for _, row in later.iterrows():
        c = Candle.from_row(row)
        hi, lo = max(hi, c.high), min(lo, c.low)
        hit = exit_fill(c, stop, target, row["timestamp"] == entry_time,
                        ExitReason.END_OF_DAY if row["timestamp"] >= eod else None)
        if hit is not None:
            exit_price, reason, ambiguous = hit
            break
    else:
        exit_price, reason, ambiguous = D(float(later.iloc[-1]["close"])), ExitReason.DATA_END, False
    qty = int(rec.get("quantity") or 0)
    out = {"status": "SIMULATED", "entry_time": entry_time.isoformat(), "entry_price": str(price),
           "exit_price": str(exit_price), "exit_reason": reason.value, "ambiguous": ambiguous,
           "gross_per_share": str(exit_price - price), "max_favourable": str(hi - price),
           "max_adverse": str(price - lo), "quantity": qty, "hypothetical": True}
    if qty > 0:
        rates = load_fee_schedule(settings).rates_on(
            day, settings.cost_review.apply_current_rates_to_past_dates)
        rt = round_trip(rates, price, exit_price, qty, settings.execution, None)
        out.update(gross_pnl=str(rt.gross_pnl), total_costs=str(rt.total_costs),
                   net_pnl=str(rt.net_pnl))
    return out


def _deterministic(body: dict) -> bool:
    return (body.get("window") == "intraday" and body.get("screen_status") == "PASS"
            and body.get("signal_status") == "QUALIFIED_PRE_COST"
            and body.get("risk_status") == "QUALIFIED_FOR_REVIEW" and body.get("data_fresh")
            and body.get("signal_id") is not None)


def compare(agent_store, provider, settings, calendar, start: date, end: date) -> dict:
    rows = [r for r in agent_store.store.all(
        "SELECT * FROM agent_recommendations WHERE market_date BETWEEN ? AND ? "
        "ORDER BY created_at", (start.isoformat(), end.isoformat()))]
    bodies = [json.loads(r["body"]) for r in rows]
    intraday = [b for b in bodies if b["window"] == "intraday" and b.get("symbol")]
    a, b = {}, {}
    for x in intraday:
        if _deterministic(x):
            a.setdefault(x["signal_id"], x)
        if x["status"] == "CANDIDATE":
            b.setdefault(x["signal_id"], x)
    violations = [sid for sid in b if sid not in a]

    def outcomes(group: dict) -> dict:
        sims = {sid: simulate(x, provider, settings, calendar) for sid, x in group.items()}
        nets = [Decimal(s["net_pnl"]) for s in sims.values() if "net_pnl" in s]
        return {"signals": len(group), "simulated_trades": len(nets),
                "net_pnl": str(sum(nets, Decimal(0))),
                "net_expectancy": str(round(sum(nets, Decimal(0)) / len(nets), 2)) if nets
                else None,
                "status_counts": dict(Counter(s["status"] for s in sims.values())),
                "detail": sims}

    oa, ob = outcomes(a), outcomes(b)
    models = [dict(m) for m in agent_store.store.all(
        "SELECT m.* FROM agent_model_calls m JOIN agent_runs r USING (run_id) WHERE "
        "r.market_date BETWEEN ? AND ?", (start.isoformat(), end.isoformat()))]
    lat = [m["latency_seconds"] for m in models if m["latency_seconds"] is not None]
    costs = [m["cost_usd"] for m in models if m["cost_usd"] is not None]
    unsupported = sum(len(x.get("unsupported_claims", [])) for x in intraday)
    sessions = sorted({x["market_date"] for x in intraday})
    per_session = [{"market_date": d,
                    "A": sum(1 for x in a.values() if x["market_date"] == d),
                    "B": sum(1 for x in b.values() if x["market_date"] == d)} for d in sessions]
    incremental = None
    if oa["simulated_trades"] and ob["net_pnl"] is not None:
        incremental = str(Decimal(ob["net_pnl"]) - Decimal(oa["net_pnl"]))
    n = len(a)
    return {
        "period": [start, end], "sessions_with_intraday_research": len(sessions),
        "A_deterministic": {k: v for k, v in oa.items() if k != "detail"},
        "B_ai_assisted": {k: v for k, v in ob.items() if k != "detail"},
        "incremental_net_pnl_B_minus_A": incremental,
        "ai_abstention_rate": round(1 - len(b) / n, 4) if n else None,
        "rule_violations_B_outside_A": len(violations),
        "rejection_reasons": dict(Counter(r.split(":")[0] for x in intraday
                                          for r in x.get("risk_reasons", [])).most_common(10)),
        "status_counts": dict(Counter(x["status"] for x in intraday)),
        "ai_status_counts": dict(Counter(x["ai_status"] for x in intraday)),
        "unsupported_claims": unsupported,
        "model_requests": len(models),
        "model_errors": sum(1 for m in models if m["error"]),
        "mean_latency_seconds": round(sum(lat) / len(lat), 3) if lat else None,
        "total_cost_usd": round(sum(costs), 6) if costs else None,
        "cost_note": "None = prices not configured (llm.*_cost_per_mtok); not guessed",
        "per_session": per_session,
        "assessment": ("insufficient observations: no deterministic-eligible signal in the "
                       "period, so AI selection cannot be evaluated") if n == 0 else
        (f"{n} eligible signal(s): far too few to conclude that AI adds or removes value; "
         "keep collecting with the baseline unchanged"),
        "method": "same runs, same data, same candle-v1 execution and Phase 3 costs; B is the "
                  "AI-filtered subset of A; hypothetical outcomes; no look-ahead",
        "details": {"A": oa["detail"], "B": ob["detail"]},
    }


def default_period(today: date, days: int = 30) -> tuple[date, date]:
    return today - timedelta(days=days), today
