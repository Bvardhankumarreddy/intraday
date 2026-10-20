"""Integrated daily report (Phase 12), built only from persisted records.

Sources: both paper databases (sessions, signals, orders, positions,
snapshots, events, Phase 6 evaluation rows) and the AI arm's immutable agent
records. Nothing is re-computed from live data, and no wall-clock time is
added, so regenerating a day's report gives identical bytes. Every figure can
be traced to a record ID.

Labels used in the report:
- [FACT]: a verified fact from a tool output or a persisted record;
- [CALC]: a deterministic calculation (strategy, risk/cost engine, accounting);
- [AI]: the language model's interpretation. It is never a probability of
  profit.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.costs.money import D
from app.utils.fs import private_dir, write_preserving

LABEL = ("PAPER TRADING - SIMULATED. No real order was placed, modified or cancelled. Real "
         "orders are placed manually by the user. Not a promise of profit.")


def _rows(store, sql: str, params: tuple = ()) -> list[dict]:
    try:
        return [dict(r) for r in store.all(sql, params)]
    except Exception:  # noqa: BLE001 - table absent: nothing recorded
        return []


def _arm(store, day: str, settings) -> dict:
    sess = _rows(store, "SELECT * FROM sessions WHERE trading_date = ?", (day,))
    ev = _rows(store, "SELECT s.status, s.status_reason, s.kind, s.primary_eval FROM "
                      "eval_sessions s JOIN evaluations e USING (evaluation_id) WHERE "
                      "e.active = 1 AND s.trading_date = ?", (day,))
    pos = _rows(store, "SELECT * FROM positions WHERE trading_date = ? ORDER BY entry_time",
                (day,))
    closed = [p for p in pos if p["state"] == "CLOSED"]
    snaps = _rows(store, "SELECT * FROM snapshots WHERE trading_date = ? ORDER BY ts DESC "
                         "LIMIT 1", (day,))
    pf = _rows(store, "SELECT * FROM portfolio")
    signals = _rows(store, "SELECT * FROM signals WHERE trading_date = ? ORDER BY signal_time",
                    (day,))
    orders = _rows(store, "SELECT o.* FROM orders o JOIN signals s USING (signal_id) WHERE "
                          "s.trading_date = ? ORDER BY o.created_at", (day,))
    net = [D(p["net_pnl"]) for p in closed if p["net_pnl"] is not None]
    out = {
        "session": sess[0] if sess else None,
        "evaluation_status": ev[0] if ev else None,
        "portfolio": pf[0] if pf else None,
        "available_cash": str(D(pf[0]["cash"]) - D(pf[0]["reserved"])) if pf else None,
        "last_equity": snaps[0]["equity"] if snaps else None,
        "signals": [{k: s[k] for k in ("signal_id", "symbol", "signal_time", "review_status",
                                       "quantity", "entry_reference", "stop", "target",
                                       "planned_net_loss")} | {"reasons": s["reasons"]}
                    for s in signals],
        "orders": [{k: o[k] for k in ("order_id", "signal_id", "symbol", "state", "quantity",
                                      "created_at", "fill_price", "fill_time", "fill_quantity",
                                      "reason")} for o in orders],
        "positions": [{k: p[k] for k in ("position_id", "signal_id", "symbol", "state",
                                         "quantity", "entry_price", "entry_time", "exit_price",
                                         "exit_time", "exit_reason", "gross_pnl",
                                         "total_costs", "net_pnl", "ambiguous")}
                      for p in pos],
        "closed_trades": len(closed),
        "open_or_flagged_positions": len(pos) - len(closed),
        "gross_pnl": str(sum((D(p["gross_pnl"]) for p in closed), Decimal(0))),
        "total_costs": str(sum((D(p["total_costs"]) for p in closed), Decimal(0))),
        "net_pnl": str(sum(net, Decimal(0))),
        "unfilled_orders": sum(1 for o in orders if o["state"] in ("EXPIRED", "CANCELLED",
                                                                    "REJECTED")),
        "incidents": _rows(store, "SELECT ts, kind, level, message FROM events WHERE "
                                  "trading_date = ? AND (level IN ('WARNING', 'ERROR') OR kind "
                                  "IN ('RECOVERY', 'INTEGRATION')) ORDER BY id", (day,)),
        "data_quality": _rows(store, "SELECT COUNT(*) AS batches, SUM(stale) AS stale, "
                                     "SUM(error IS NOT NULL) AS errors, SUM(missing) AS missing "
                                     "FROM dq_batches WHERE trading_date = ?", (day,)),
        "reconciliation_issues": _rows(store, "SELECT code, ref, message FROM "
                                              "reconciliation_issues WHERE trading_date = ?",
                                       (day,)),
    }
    return out


def _candidates(b_store, day: str) -> list[dict]:
    recs = [json.loads(r["body"]) for r in _rows(
        b_store, "SELECT body FROM agent_recommendations WHERE market_date = ? AND symbol IS "
                 "NOT NULL AND window = 'intraday' ORDER BY created_at", (day,))]
    outcomes = {}
    for o in _rows(b_store, "SELECT o.* FROM agent_outcomes o JOIN agent_recommendations r "
                            "USING (recommendation_id) WHERE r.market_date = ?", (day,)):
        outcomes.setdefault(o["recommendation_id"], []).append(
            {"kind": o["kind"], "recorded_at": o["recorded_at"], "body": json.loads(o["body"])})
    out = []
    for r in recs:
        a = r.get("ai_assessment") or {}

        def claims(key):
            return [f"[{'FACT' if c['basis'] == 'verified_fact' else 'CALC' if c['basis'] == 'deterministic_calculation' else 'AI'}] "  # noqa: E501
                    f"{c['text']} ({', '.join(c['evidence_ids']) or 'no evidence'})"
                    for c in a.get(key, [])]

        out.append({
            "recommendation_id": r["recommendation_id"], "created_at": r["created_at"],
            "symbol": r["symbol"], "status": r["status"], "status_reasons": r["status_reasons"],
            "signal_id": r.get("signal_id"), "score": r.get("score"),
            "eligibility": {"screen": r.get("screen_status"), "signal": r.get("signal_status"),
                            "data_fresh": r["data_fresh"]},
            "financial_calc": {
                "quantity": r.get("quantity"), "entry_reference": r.get("entry_reference"),
                "stop": r.get("stop"), "target": r.get("target"),
                "entry_condition": r.get("entry_condition"), "valid_until": r.get("valid_until"),
                "estimated_costs_at_target": r.get("estimated_costs_at_target"),
                "estimated_spread_slippage": r.get("estimated_spread_slippage"),
                "planned_net_loss_at_stop": r.get("planned_net_loss_at_stop"),
                "net_reward_risk": r.get("net_reward_risk"),
                "fee_schedule_id": r.get("fee_schedule_id")},
            "risk_engine": {"status": r.get("risk_status"), "reasons": r.get("risk_reasons")},
            "ai": {"status": r["ai_status"], "model": r.get("model"),
                   "prompt_version": r.get("prompt_version"),
                   "recommendation": a.get("recommendation"),
                   "rationale": a.get("rationale"),
                   "observed_facts": claims("observed_facts"),
                   "supporting": claims("supporting_evidence"),
                   "contradicting": claims("contradicting_evidence"),
                   "entry_conditions": a.get("entry_conditions", []),
                   "invalidation_conditions": a.get("invalidation_conditions", []),
                   "missing_information": a.get("missing_information", []),
                   "unsupported_claims": r.get("unsupported_claims", []),
                   "untrusted_content_flags": r.get("untrusted_content_flags", [])},
            "outcomes_recorded_later": outcomes.get(r["recommendation_id"], []),
        })
    return out


def _reliability(settings, calendar, a: dict, b: dict, b_store, day: str) -> dict:
    d = date.fromisoformat(day)
    runs = _rows(b_store, "SELECT status, reason FROM agent_runs WHERE market_date = ? AND "
                          "window = 'intraday'", (day,))
    calls = _rows(b_store, "SELECT m.error_kind, m.latency_seconds, m.input_tokens, "
                           "m.output_tokens, m.cost_usd FROM agent_model_calls m JOIN "
                           "agent_runs r USING (run_id) WHERE r.market_date = ?", (day,))
    errors = [c for c in calls if c["error_kind"]]
    dq = (a["data_quality"] or [{}])[0]
    return {
        "scheduled_regular_session": calendar.is_regular_session(d),
        "application_started": a["session"] is not None,
        "data_mode": (a["session"] or {}).get("data_mode"),
        "data_source": (a["session"] or {}).get("data_source"),
        "data_available": bool(a["session"]) and not (dq.get("batches") and
                                                      dq.get("stale") == dq.get("batches")),
        "stale_data_batches": dq.get("stale"), "data_error_batches": dq.get("errors"),
        # What actually ran (from the run records), not just what is configured.
        "ai_provider": sorted({f"{r['llm_provider']} ({r['model']})" for r in _rows(
            b_store, "SELECT DISTINCT llm_provider, model FROM agent_runs WHERE market_date = ?",
            (day,))}) or f"none ran (configured {settings.llm.provider.value})",
        "ai_provider_available": None if not calls else len(errors) < len(calls),
        "model_requests": len(calls), "model_errors": len(errors),
        "model_error_kinds": sorted({c["error_kind"] for c in errors}),
        "model_latency_mean_s": round(sum(c["latency_seconds"] or 0 for c in calls
                                          if not c["error_kind"]) /
                                      max(1, len(calls) - len(errors)), 3) if calls else None,
        "model_tokens": [sum(c["input_tokens"] or 0 for c in calls),
                         sum(c["output_tokens"] or 0 for c in calls)],
        "model_cost_usd": round(sum(c["cost_usd"] for c in calls if c["cost_usd"] is not None),
                                6) if any(c["cost_usd"] is not None for c in calls) else None,
        "research_runs": len(runs),
        "research_runs_by_status": {s: sum(1 for r in runs if r["status"] == s)
                                    for s in sorted({r["status"] for r in runs})},
        "recommendations_generated": bool(runs),
        "session_status": {"A": (a["evaluation_status"] or {}).get("status")
                           or (a["session"] or {}).get("state"),
                           "B": (b["evaluation_status"] or {}).get("status")
                           or (b["session"] or {}).get("state")},
        "recovery_events": sum(1 for x in a["incidents"] + b["incidents"]
                               if x["kind"] == "RECOVERY"),
        "ai_arm_errors": [x["message"] for x in b["incidents"] if x["kind"] == "INTEGRATION"
                          and x["level"] == "ERROR"],
    }


def build_daily(settings, calendar, a_store, b_store, day: date) -> dict:
    from app.costs.schedule import load_fee_schedule

    ds = day.isoformat()
    a, b = _arm(a_store, ds, settings), _arm(b_store, ds, settings)
    try:
        fee = load_fee_schedule(settings)
        age = (day - fee.verified_on).days
        fee_info = {"schedule_id": fee.schedule_id, "version": fee.version,
                    "verified_on": fee.verified_on, "illustrative": fee.illustrative,
                    "age_days": age,
                    "warning": f"fee schedule verified {age} days before this session (limit "
                               f"{settings.evaluation.max_fee_schedule_age_days})"
                    if age > settings.evaluation.max_fee_schedule_age_days else
                    ("schedule verified after this session: current rates applied "
                     "retroactively" if age < 0 else None)}
    except Exception as exc:  # noqa: BLE001 - surfaced, never hidden
        fee_info = {"warning": f"fee schedule unavailable: {exc}"}
    ctx = _rows(b_store, "SELECT t.tool, t.call_id, t.output, r.run_id FROM agent_tool_calls t "
                         "JOIN agent_runs r USING (run_id) WHERE r.market_date = ? AND "
                         "r.window = 'intraday' AND t.tool IN ('get_market_status', "
                         "'get_data_health') AND t.ok = 1 ORDER BY r.started_at, t.call_id "
                         "LIMIT 2", (ds,))
    market = {}
    for c in ctx:
        res = json.loads(c["output"])["result"]
        if c["tool"] == "get_market_status":
            market["status"] = {k: res.get(k) for k in ("now", "is_regular_session",
                                                        "session_status", "holiday_name")}
        else:
            market["data_health"] = {"fresh_symbols": res.get("fresh_symbols"),
                                     "total_symbols": res.get("total_symbols")}
        market.setdefault("evidence", []).append(f"{c['run_id']}:{c['call_id']}")
    market["news"] = ("UNAVAILABLE: no approved, source-attributed news provider is "
                      "configured; no news is known to this system")
    cands = _candidates(b_store, ds)
    return {"trading_date": ds, "label": LABEL,
            "systems": {"A": "deterministic baseline paper arm",
                        "B": "AI-assisted paper arm (orders only with a prior CANDIDATE)"},
            "versions": {"strategy": (a["session"] or {}).get("strategy_version"),
                         "config": (a["session"] or {}).get("config_version"),
                         "execution_model": (a["session"] or {}).get(
                             "execution_model_version"),
                         "ai": sorted({f"{c['ai']['model']} / {c['ai']['prompt_version']}"
                                       for c in cands})},
            "fee_schedule": fee_info,
            "reliability": _reliability(settings, calendar, a, b, b_store, ds),
            "market_context": market, "candidates": cands, "arms": {"A": a, "B": b}}


def _md(r: dict) -> str:
    rel, A, B = r["reliability"], r["arms"]["A"], r["arms"]["B"]
    L = [f"# Integrated paper-trading report - {r['trading_date']}", "", f"**{r['label']}**",
         "", "Legend: [FACT] verified fact (tool or record ID), [CALC] deterministic "
         "calculation, [AI] model interpretation (not a probability of profit).", "",
         "## Session reliability", ""]
    for k, v in rel.items():
        L.append(f"- {k}: {v}")
    L += ["", f"Fee schedule: {r['fee_schedule']}", "", "## Market context", ""]
    m = r["market_context"]
    if m.get("status"):
        L.append(f"- [FACT] session {m['status']} ({', '.join(m.get('evidence', []))})")
    if m.get("data_health"):
        L.append(f"- [FACT] fresh symbols {m['data_health']}")
    L += [f"- News: {m['news']}", "", "## Candidate analysis and decisions", ""]
    if not r["candidates"]:
        L.append("No candidate was reviewed. A no-trade session is a valid outcome.")
    for c in r["candidates"]:
        f, ai = c["financial_calc"], c["ai"]
        L += [f"### {c['symbol']} - {c['status']} ({c['recommendation_id']}, original "
              f"timestamp {c['created_at']})",
              f"- Eligibility [FACT]: {c['eligibility']}; score {c['score']}",
              f"- [CALC] qty {f['quantity']}, entry {f['entry_reference']} "
              f"({f['entry_condition']}), stop {f['stop']}, target {f['target']}, valid until "
              f"{f['valid_until']}",
              f"- [CALC] costs at target {f['estimated_costs_at_target']}; "
              f"{f['estimated_spread_slippage']}; net loss at stop "
              f"{f['planned_net_loss_at_stop']}; net reward:risk {f['net_reward_risk']}",
              f"- [CALC] risk engine: {c['risk_engine']['status']} "
              f"{c['risk_engine']['reasons'] or ''}",
              f"- AI ({ai['status']}, {ai['model']}, {ai['prompt_version']}): "
              f"[AI] {ai['recommendation']} - {ai['rationale']}"]
        L += [f"  - supporting: {x}" for x in ai["supporting"]]
        L += [f"  - contradicting: {x}" for x in ai["contradicting"]]
        L += [f"  - entry condition: {x}" for x in ai["entry_conditions"]]
        L += [f"  - invalidated if: {x}" for x in ai["invalidation_conditions"]]
        L += [f"  - missing: {x}" for x in ai["missing_information"]]
        if ai["unsupported_claims"]:
            L.append(f"  - UNSUPPORTED CLAIMS: {ai['unsupported_claims']}")
        L.append(f"- Decision: {'; '.join(c['status_reasons'])}")
        for o in c["outcomes_recorded_later"]:
            L.append(f"- Outcome (recorded separately at {o['recorded_at']}): {o['kind']} "
                     f"{o['body']}")
        L.append("")
    L += ["## Paper arms", "", "| | A baseline | B AI-assisted |", "|---|---|---|"]
    for k in ("closed_trades", "open_or_flagged_positions", "unfilled_orders", "gross_pnl",
              "total_costs", "net_pnl", "available_cash", "last_equity"):
        L.append(f"| {k} | {A[k]} | {B[k]} |")
    L += ["", "Signals and orders per arm:", ""]
    for name, arm in (("A", A), ("B", B)):
        for s in arm["signals"]:
            L.append(f"- {name} {s['symbol']} {s['signal_time']}: {s['review_status']} qty "
                     f"{s['quantity']}")
        for o in arm["orders"]:
            L.append(f"- {name} order {o['order_id']} {o['state']} fill {o['fill_price']} at "
                     f"{o['fill_time']}")
    L += ["", "## Deviations, errors and reconciliation", ""]
    for name, arm in (("A", A), ("B", B)):
        L += [f"- {name} {x['level']} {x['kind']}: {x['message']}" for x in arm["incidents"]]
        L += [f"- {name} RECONCILIATION {x['code']} ({x['ref']}): {x['message']}"
              for x in arm["reconciliation_issues"]] or [f"- {name}: no reconciliation issue"]
    return "\n".join(L) + "\n"


def write_daily(settings, calendar, a_store, b_store, day: date) -> Path:
    body = build_daily(settings, calendar, a_store, b_store, day)
    out = private_dir(Path(settings.integrated.reports_dir) / day.isoformat())
    write_preserving(out / "integrated_daily.json",
                     json.dumps(body, indent=2, sort_keys=True, default=str) + "\n")
    path = out / "integrated_daily.md"
    write_preserving(path, _md(json.loads(json.dumps(body, default=str))))
    return path
