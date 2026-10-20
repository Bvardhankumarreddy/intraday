"""Prospective comparison from live paper records (Phase 11; primary evidence).

Arm A is what the existing paper engine actually did in LIVE, primary
sessions of the active Phase 6 evaluation, on or after the protocol's
``effective_date``. Only sessions whose status the protocol includes
(COMPLETED) count. Others are reported and excluded.

Arm B takes a paper trade of arm A only if the research agent had already
persisted a CANDIDATE recommendation for that signal before the entry order
was created. Recommendations are immutable and timestamped, so this cannot
be decided with hindsight. B is a filter on A's paper trades:
- the AI can only veto, so B never has a trade A lacks;
- capacity a veto would have freed is NOT re-used (that would need a
  counterfactual paper engine). This is listed as a limitation.
Reasons B did not take a trade: vetoed (the AI's recommendation was WATCH /
NO_TRADE / REJECTED), not timely (its first CANDIDATE came after the entry),
or not reviewed (no recommendation for the signal).

Both arms use the same paper fills and Phase 3 costs, and realized equity at
each exit.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from app.aieval import metrics as M
from app.aieval.ledger import Ledger, canonical, sha
from app.aieval.protocol import ai_versions, load_protocol, pin_mismatches
from app.aieval.quality import decision_quality, load_agent_records
from app.costs.money import D
from app.utils.fs import private_dir


def _trade(p) -> dict:
    qty = int(p["quantity"])
    return {"signal_id": p["signal_id"], "symbol": p["symbol"],
            "session": date.fromisoformat(p["trading_date"]), "entry_time": p["entry_time"],
            "exit_time": p["exit_time"], "entry_price": D(p["entry_price"]),
            "exit_price": D(p["exit_price"]), "stop": D(p["stop"]), "quantity": qty,
            "gross": D(p["gross_pnl"]), "costs": D(p["total_costs"]), "net": D(p["net_pnl"]),
            "buy_turnover": D(p["entry_price"]) * qty, "exit_reason": p["exit_reason"],
            "incomplete": p["exit_reason"] == "DATA_END", "ambiguous": bool(p["ambiguous"])}


def _orders(rows) -> dict:
    states = Counter(o["state"] for o in rows)
    partial = sum(1 for o in rows if o["fill_quantity"] and o["fill_quantity"] < o["quantity"])
    return {"paper_orders": len(rows), "orders_filled": states.get("FILLED", 0),
            "orders_partially_filled": partial,
            "orders_unfilled": sum(states.get(s, 0) for s in ("EXPIRED", "CANCELLED",
                                                              "REJECTED")),
            "order_states": dict(states)}


def evaluate_prospective(live, store, ledger: Ledger, start: date | None = None,
                         end: date | None = None, *, llm=None, protocol_path=None,
                         now: datetime | None = None) -> dict:
    from app.agent.llm import build_llm
    from app.evaluation.metrics import in_scope_sessions
    from app.robustness.baseline import load_frozen, strategy_fingerprint
    from app.utils.market_calendar import MarketCalendar

    protocol, _, psha = load_protocol(protocol_path or live.ai_eval.protocol_file)
    frozen = load_frozen(protocol.baseline_path())
    calendar = MarketCalendar(live.market)
    now = (now or datetime.now(calendar.tz)).astimezone(calendar.tz)
    versions = ai_versions(live, llm or build_llm(live))
    live_fp, _ = strategy_fingerprint(live)
    meta = {"mode": "PROSPECTIVE", "protocol_version": protocol.protocol_version,
            "protocol_sha256": psha, "baseline_name": frozen.name,
            "baseline_fingerprint": frozen.fingerprint,
            "live_config_matches_frozen_baseline": live_fp == frozen.fingerprint,
            "baseline_code_drift": frozen.code_drift, "ai_versions": versions,
            "ai_system_drift": pin_mismatches(protocol, versions),
            "effective_date": protocol.effective_date}
    labels = ["PROSPECTIVE", "PRIMARY_EVIDENCE: recommendations persisted before outcomes"]
    if not meta["live_config_matches_frozen_baseline"]:
        labels.append("LIVE_CONFIG_DIFFERS_FROM_FROZEN_BASELINE")
    if meta["ai_system_drift"]:
        labels.append("AI_SYSTEM_DRIFT: " + "; ".join(meta["ai_system_drift"]))

    try:
        ev = store.one("SELECT * FROM evaluations WHERE active = 1")
    except Exception:  # noqa: BLE001 - no evaluation tables yet
        ev = None
    rows = [dict(r) for r in in_scope_sessions(store, ev["evaluation_id"])] if ev else []
    lo = max(protocol.effective_date, start) if start else protocol.effective_date
    rows = [r for r in rows if date.fromisoformat(r["trading_date"]) >= lo
            and (end is None or date.fromisoformat(r["trading_date"]) <= end)]
    status_counts = Counter(r["status"] for r in rows)
    eligible = [r["trading_date"] for r in rows
                if r["status"] in protocol.inclusion.prospective_session_status]
    meta.update(evaluation_id=ev["evaluation_id"] if ev else None, period=[lo, end])
    if not eligible:
        reason = ("no active Phase 6 evaluation" if ev is None else
                  f"no eligible LIVE session on or after {lo} (status counts "
                  f"{dict(status_counts) or 'none'})")
        return {"status": "PENDING_DATA", "meta": meta, "labels": labels,
                "session_status_counts": dict(status_counts),
                "conclusion": {"conclusion": "PENDING_DATA", "reason": reason}}

    marks = ", ".join("?" for _ in eligible)
    flagged = set()
    if protocol.inclusion.exclude_reconciliation_flagged:
        flagged = {r["ref"] for r in store.all("SELECT ref FROM reconciliation_issues")}
    positions = [dict(p) for p in store.all(
        f"SELECT * FROM positions WHERE trading_date IN ({marks}) AND state = 'CLOSED'",
        tuple(eligible))]
    excluded = [p["position_id"] for p in positions if p["position_id"] in flagged]
    positions = [p for p in positions if p["position_id"] not in flagged]
    signals = [dict(s) for s in store.all(
        f"SELECT * FROM signals WHERE trading_date IN ({marks}) ORDER BY signal_time",
        tuple(eligible))]
    orders = [dict(o) for o in store.all(
        f"SELECT o.* FROM orders o JOIN signals s USING (signal_id) WHERE s.trading_date IN "
        f"({marks}) AND o.side = 'BUY'", tuple(eligible))]
    recs, models, tools, runs = load_agent_records(store, eligible)
    cands: dict[str, list[dict]] = {}
    any_rec: dict[str, list[dict]] = {}
    for r in recs:
        if r.get("signal_id"):
            any_rec.setdefault(r["signal_id"], []).append(r)
            if r["status"] == "CANDIDATE":
                cands.setdefault(r["signal_id"], []).append(r)

    session_key = canonical({"m": meta["mode"], "p": psha, "e": meta["evaluation_id"],
                             "s": eligible, "pos": sorted(p["position_id"] for p in positions),
                             "recs": len(recs)})
    run_id = "AE-" + sha(session_key)[:12]
    ledger.start_run({"run_id": run_id, "run_key": session_key, "mode": "PROSPECTIVE",
                      "split": None, "period_start": eligible[0], "period_end": eligible[-1],
                      "dataset_id": meta["evaluation_id"],
                      "protocol_version": protocol.protocol_version, "protocol_sha256": psha,
                      "baseline_name": frozen.name, "baseline_fingerprint": frozen.fingerprint,
                      "ai_versions": canonical(versions), "labels": canonical(labels),
                      "started_at": now.isoformat()})
    try:
        out = _evaluate(live, protocol, meta, labels, ledger, run_id, now, eligible, rows,
                        status_counts, positions, excluded, signals, orders, cands, any_rec,
                        recs, models, tools, runs)
    except Exception as exc:
        ledger.finish_run(run_id, "FAILED", now, f"{type(exc).__name__}: {exc}"[:500])
        raise
    from app.aieval.report import write_reports

    path, digest = write_reports(out, private_dir(Path(live.ai_eval.output_dir) / run_id))
    ledger.finish_run(run_id, "COMPLETED", now, None, out["conclusion"]["conclusion"],
                      str(path), digest)
    return out


def _evaluate(live, protocol, meta, labels, ledger, run_id, now, eligible, rows, status_counts,
              positions, excluded, signals, orders, cands, any_rec, recs, models, tools, runs):
    pos_by_sig = {p["signal_id"]: p for p in positions}
    orders_by_sig: dict[str, list[dict]] = {}
    for o in orders:
        orders_by_sig.setdefault(o["signal_id"], []).append(o)
    b_taken, reasons = set(), Counter()
    decided = {"A": Counter(), "B": Counter()}
    for s in signals:
        sid = s["signal_id"]
        ords = sorted(orders_by_sig.get(sid, []), key=lambda o: o["created_at"])
        if ords:
            a_final, a_why = "TAKEN", f"risk engine approved; paper order {ords[0]['state']}"
        elif s["review_status"] != "QUALIFIED_FOR_REVIEW":
            a_final, a_why = "REJECTED_BY_RISK", f"risk/cost engine: {s['review_status']}"
        else:
            a_final, a_why = "NOT_EVALUATED", "qualified but no paper order was created"
        entry_at = datetime.fromisoformat(ords[0]["created_at"]) if ords else None
        timely = [r for r in cands.get(sid, []) if entry_at is not None
                  and datetime.fromisoformat(r["created_at"]) <= entry_at]
        if a_final != "TAKEN":
            b_final, b_why = a_final, "same as the baseline (the AI can only veto)"
        elif timely:
            b_final, b_why = "TAKEN", f"CANDIDATE {timely[0]['recommendation_id']} before entry"
            b_taken.add(sid)
        elif cands.get(sid):
            b_final, b_why = "AI_NOT_TIMELY", "first CANDIDATE recommendation came after entry"
        elif any_rec.get(sid):
            b_final, b_why = "VETOED_BY_AI", ("AI-assisted status: " + ", ".join(
                sorted({r["status"] for r in any_rec[sid]})))
        else:
            b_final, b_why = "NOT_REVIEWED", "no AI recommendation for this signal"
        reasons[b_final] += 1
        decided["A"][a_final] += 1
        decided["B"][b_final] += 1
        common = {"signal_id": sid, "symbol": s["symbol"], "exchange": "NSE",
                  "decision_time": s["signal_time"], "market_date": s["trading_date"],
                  "mode": "PROSPECTIVE",
                  "proposal": {"entry_reference": s["entry_reference"], "stop": s["stop"],
                               "target": s["target"], "expiry": s["valid_until"],
                               "quantity": s["quantity"],
                               "planned_net_loss": s["planned_net_loss"]},
                  "risk_engine": {"status": s["review_status"],
                                  "reasons": json.loads(s["reasons"]) if s["reasons"]
                                  and s["reasons"].startswith("[") else s["reasons"]},
                  "paper_orders": [o["order_id"] for o in ords],
                  "evaluation_status": "EVALUATED"}
        for system, final, why in (("BASELINE", a_final, a_why), ("AI_ASSISTED", b_final,
                                                                   b_why)):
            body = {**common, "system": system, "final_decision": final, "reason": why}
            if system == "AI_ASSISTED":
                body["ai_recommendations"] = [r["recommendation_id"] for r in
                                              any_rec.get(sid, [])]
            did = ledger.add_decision(run_id, system, body)
            p = pos_by_sig.get(sid)
            if p is not None and final == "TAKEN":
                ledger.add_outcome(did, "TRADE_CLOSED", now, _trade(p))

    sessions = [date.fromisoformat(d) for d in eligible]
    ta = [_trade(p) for p in positions]
    tb = [t for t in ta if t["signal_id"] in b_taken]
    initial = D(live.capital.initial_capital)
    ma, mb = M.arm_metrics(ta, sessions, initial), M.arm_metrics(tb, sessions, initial)
    statuses = {"complete_sessions": status_counts.get("COMPLETED", 0),
                "incomplete_sessions": status_counts.get("INCOMPLETE", 0),
                "failed_sessions": status_counts.get("FAILED", 0)}
    b_orders = [o for o in orders if o["signal_id"] in b_taken]
    for m, dec, ords in ((ma, decided["A"], orders), (mb, decided["B"], b_orders)):
        m.update({**statuses, "signals_evaluated": len(signals), "decisions": dict(dec),
                  "recommendations_taken": dec["TAKEN"],
                  "rejected_candidates": len(signals) - dec["TAKEN"] - dec["NOT_EVALUATED"],
                  "no_trade_decisions": len(signals) - dec["TAKEN"], **_orders(ords),
                  "risk_rule_violations": {"total": sum(
                      1 for t in (ta if m is ma else tb)
                      if next((s["review_status"] for s in signals
                               if s["signal_id"] == t["signal_id"]), None)
                      != "QUALIFIED_FOR_REVIEW")},
                  "data_quality_failures": sum(1 for r in rows if r["status"] != "COMPLETED")})
    table_a, table_b = M.session_table(ta, sessions), M.session_table(tb, sessions)
    st = protocol.statistics
    boot = M.paired_bootstrap(table_a, table_b, sessions, st.bootstrap_resamples,
                              st.confidence, st.seed, st.block_length)
    quality = decision_quality(recs, models, tools, runs)
    usd, fx = quality["operational"]["cost_usd"], live.ai_eval.usd_inr_rate
    model_inr = round(usd * fx, 4) if usd is not None and fx else None
    # Signals A took and B did not (B can never take one A did not).
    discordant = sum(reasons[k] for k in ("VETOED_BY_AI", "AI_NOT_TIMELY", "NOT_REVIEWED"))
    structural = None if ta else ("no baseline paper trade in the eligible sessions; a "
                                  "veto-only AI cannot change any trade")
    selection = M.random_veto_benchmark(ta, tb, st.bootstrap_resamples, st.seed, st.confidence)
    concl = M.assess(protocol, len(sessions), ma, mb, discordant, boot, model_inr, structural,
                     selection)
    net_diff = round(mb["net_pnl"] - ma["net_pnl"], 2)
    equity = {}
    for arm, table in (("A", table_a), ("B", table_b)):
        cum, pts = initial, []
        for d in sessions:
            cum += table[d][0]
            pts.append((d.isoformat(), M._f(cum)))
        equity[arm] = pts
    return {
        "status": "OK", "run_id": run_id, "meta": meta, "labels": labels,
        "protocol": protocol.model_dump(mode="json"),
        "systems": {
            "A": "deterministic baseline: the live paper engine's own decisions and fills",
            "B": "AI-assisted: arm A's paper trades whose signal had a CANDIDATE AI "
                 "recommendation persisted before the entry order",
            "ai_may_change": "which deterministic signals are taken (veto only)",
            "ai_may_not_change": "entry, stop, target, quantity, costs, risk limits, fills, "
                                 "data, or add a signal",
        },
        "coverage": {"sessions": len(sessions), "period": [eligible[0], eligible[-1]],
                     "session_status_counts": dict(status_counts),
                     "excluded_reconciliation_flagged": excluded,
                     "limitations": [
                         "capacity freed by an AI veto is not re-used (B is a filter on A's "
                         "paper trades)",
                         "the agent runs every agent.intraday_every_n_candles candles, so a "
                         "signal can be entered before the AI reviews it (AI_NOT_TIMELY)"]},
        "arms": {"A": ma, "B": mb}, "equity": equity,
        "incremental": {
            "net_pnl_difference": net_diff,
            "expectancy_difference": round(mb["expectancy"] - ma["expectancy"], 2)
            if ma["expectancy"] is not None and mb["expectancy"] is not None else None,
            "max_drawdown_difference": round(mb["max_drawdown_inr"] - ma["max_drawdown_inr"], 2),
            "transaction_cost_difference": round(mb["total_costs"] - ma["total_costs"], 2),
            "trade_count_difference": mb["closed_trades"] - ma["closed_trades"],
            "exposure_difference": round(mb["exposure_buy_turnover"]
                                         - ma["exposure_buy_turnover"], 2),
            "b_not_taken_reasons": dict(reasons),
            "model_cost_usd": usd, "usd_inr_rate": fx, "model_cost_inr": model_inr,
            "model_tokens": {"input": quality["operational"]["input_tokens"],
                             "output": quality["operational"]["output_tokens"]},
            "net_incremental_after_model_cost_inr": round(net_diff - model_inr, 2)
            if model_inr is not None else None,
            "net_incremental_note": None if model_inr is not None else
            "not computed: model prices or ai_eval.usd_inr_rate not configured; never guessed",
            "decomposition": M.decompose(ta, tb),
            "explanatory_value": "reported separately in 'quality'; it is not trading value"},
        "statistics": boot, "discordant_signals": discordant,
        "random_veto_benchmark": selection, "quality": quality,
        "capital": {"initial_capital": float(initial),
                    "risk_status_counts": dict(Counter(s["review_status"] for s in signals)),
                    "executable_baseline_signals": decided["A"]["TAKEN"]},
        "per_session": [{"session": d, "A_trades": table_a[d][1], "A_net": M._f(table_a[d][0]),
                         "B_trades": table_b[d][1], "B_net": M._f(table_b[d][0])}
                        for d in sessions],
        "regimes": M.regime_table(None, sessions, table_a, table_b),
        "conclusion": concl,
    }


# -- Phase 12: integrated mode (each arm has its own paper portfolio) ----------------------


def _eligible_dates(store, protocol, lo: date, end: date | None) -> tuple[dict, dict]:
    from app.evaluation.metrics import in_scope_sessions

    try:
        ev = store.one("SELECT * FROM evaluations WHERE active = 1")
    except Exception:  # noqa: BLE001 - no evaluation tables yet
        ev = None
    rows = [dict(r) for r in in_scope_sessions(store, ev["evaluation_id"])] if ev else []
    rows = {r["trading_date"]: r for r in rows if date.fromisoformat(r["trading_date"]) >= lo
            and (end is None or date.fromisoformat(r["trading_date"]) <= end)}
    return rows, (dict(ev) if ev else None)


def _arm_records(store, eligible: list[str], flagged_ok: bool):
    marks = ", ".join("?" for _ in eligible)
    flagged = {r["ref"] for r in store.all("SELECT ref FROM reconciliation_issues")} \
        if flagged_ok else set()
    pos = [dict(p) for p in store.all(
        f"SELECT * FROM positions WHERE trading_date IN ({marks}) AND state = 'CLOSED'",
        tuple(eligible))]
    excluded = [p["position_id"] for p in pos if p["position_id"] in flagged]
    pos = [p for p in pos if p["position_id"] not in flagged]
    sig = {s["signal_id"]: dict(s) for s in store.all(
        f"SELECT * FROM signals WHERE trading_date IN ({marks})", tuple(eligible))}
    orders = [dict(o) for o in store.all(
        f"SELECT o.* FROM orders o JOIN signals s USING (signal_id) WHERE s.trading_date IN "
        f"({marks}) AND o.side = 'BUY'", tuple(eligible))]
    return pos, excluded, sig, orders


def evaluate_integrated(live, a_store, b_store, ledger: Ledger, start: date | None = None,
                        end: date | None = None, *, llm=None, protocol_path=None,
                        now: datetime | None = None) -> dict:
    """Arm A = baseline paper database, arm B = AI-assisted paper database, both
    run side by side by ``app.integrated`` on the same data snapshots. Only
    sessions COMPLETED in BOTH arms count; the rest are reported."""
    from app.agent.llm import build_llm
    from app.paper.engine import GATE_REJECTED
    from app.robustness.baseline import load_frozen, strategy_fingerprint
    from app.utils.market_calendar import MarketCalendar

    protocol, _, psha = load_protocol(protocol_path or live.ai_eval.protocol_file)
    frozen = load_frozen(protocol.baseline_path())
    calendar = MarketCalendar(live.market)
    now = (now or datetime.now(calendar.tz)).astimezone(calendar.tz)
    versions = ai_versions(live, llm or build_llm(live))
    live_fp, _ = strategy_fingerprint(live)
    lo = max(protocol.effective_date, start) if start else protocol.effective_date
    rows_a, ev_a = _eligible_dates(a_store, protocol, lo, end)
    rows_b, ev_b = _eligible_dates(b_store, protocol, lo, end)
    ok = set(protocol.inclusion.prospective_session_status)
    all_dates = sorted(set(rows_a) | set(rows_b))
    eligible = [d for d in all_dates if rows_a.get(d, {}).get("status") in ok
                and rows_b.get(d, {}).get("status") in ok]
    session_table = [{"session": d, "A": rows_a.get(d, {}).get("status", "NOT_RECORDED"),
                      "B": rows_b.get(d, {}).get("status", "NOT_RECORDED"),
                      "eligible": d in eligible} for d in all_dates]
    meta = {"mode": "PROSPECTIVE_INTEGRATED", "protocol_version": protocol.protocol_version,
            "protocol_sha256": psha, "baseline_name": frozen.name,
            "baseline_fingerprint": frozen.fingerprint,
            "live_config_matches_frozen_baseline": live_fp == frozen.fingerprint,
            "baseline_code_drift": frozen.code_drift, "ai_versions": versions,
            "ai_system_drift": pin_mismatches(protocol, versions),
            "effective_date": protocol.effective_date,
            "evaluation_ids": {"A": ev_a and ev_a["evaluation_id"],
                               "B": ev_b and ev_b["evaluation_id"]}, "period": [lo, end]}
    labels = ["PROSPECTIVE_INTEGRATED", "PRIMARY_EVIDENCE: both arms ran live side by side; "
              "recommendations persisted before outcomes"]
    if not meta["live_config_matches_frozen_baseline"]:
        labels.append("LIVE_CONFIG_DIFFERS_FROM_FROZEN_BASELINE")
    if meta["ai_system_drift"]:
        labels.append("AI_SYSTEM_DRIFT: " + "; ".join(meta["ai_system_drift"]))
    if not eligible:
        reason = ("no active Phase 6 evaluation in "
                  + ("both arms" if not ev_a and not ev_b else "arm A" if not ev_a else "arm B")
                  if not (ev_a and ev_b) else
                  f"no session COMPLETED in both arms on or after {lo}")
        return {"status": "PENDING_DATA", "meta": meta, "labels": labels,
                "sessions": session_table,
                "conclusion": {"conclusion": "PENDING_DATA", "reason": reason}}

    flag = protocol.inclusion.exclude_reconciliation_flagged
    pos_a, ex_a, sig_a, ord_a = _arm_records(a_store, eligible, flag)
    pos_b, ex_b, sig_b, ord_b = _arm_records(b_store, eligible, flag)
    recs, models, tools, runs = load_agent_records(b_store, eligible)
    # Prompt or model changed mid-evaluation: those recommendations are not the
    # frozen AI system (never silently mixed).
    off_version = sorted({f"{r['prompt_version']}/{r['model']}" for r in recs
                          if r.get("prompt_version") != protocol.ai_system.prompt_version
                          or r.get("model") not in (protocol.ai_system.model, None)})
    if off_version:
        labels.append("AI_VERSION_CHANGED_DURING_EVALUATION: " + ", ".join(off_version))

    key = canonical({"m": meta["mode"], "p": psha, "e": meta["evaluation_ids"], "s": eligible,
                     "a": sorted(p["position_id"] for p in pos_a),
                     "b": sorted(p["position_id"] for p in pos_b), "r": len(recs)})
    run_id = "AE-" + sha(key)[:12]
    ledger.start_run({"run_id": run_id, "run_key": key, "mode": meta["mode"], "split": None,
                      "period_start": eligible[0], "period_end": eligible[-1],
                      "dataset_id": f"{meta['evaluation_ids']['A']}|{meta['evaluation_ids']['B']}",
                      "protocol_version": protocol.protocol_version, "protocol_sha256": psha,
                      "baseline_name": frozen.name, "baseline_fingerprint": frozen.fingerprint,
                      "ai_versions": canonical(versions), "labels": canonical(labels),
                      "started_at": now.isoformat()})
    try:
        orders_a = {o["signal_id"]: o for o in ord_a}
        orders_b = {o["signal_id"]: o for o in ord_b}
        pa = {p["signal_id"]: p for p in pos_a}
        pb = {p["signal_id"]: p for p in pos_b}
        decided = {"A": Counter(), "B": Counter()}
        discordant = 0
        for sid in sorted(set(sig_a) | set(sig_b)):
            s = sig_a.get(sid) or sig_b[sid]
            finals = {}
            for arm, sigs, ords in (("A", sig_a, orders_a), ("B", sig_b, orders_b)):
                row = sigs.get(sid)
                if row is None:
                    finals[arm] = ("NOT_RECORDED", "signal not recorded by this arm")
                elif sid in ords:
                    finals[arm] = ("TAKEN", f"paper order {ords[sid]['state']}")
                elif row["review_status"] == GATE_REJECTED:
                    why = json.loads(row["reasons"])[-1] if row["reasons"].startswith("[") \
                        else row["reasons"]
                    finals[arm] = ("NOT_REVIEWED" if "no AI recommendation" in why
                                   else "VETOED_BY_AI", why)
                elif row["review_status"] != "QUALIFIED_FOR_REVIEW":
                    finals[arm] = ("REJECTED_BY_RISK", row["review_status"])
                else:
                    finals[arm] = ("NOT_EVALUATED", "approved but no paper order (cash)")
                decided[arm][finals[arm][0]] += 1
            discordant += (finals["A"][0] == "TAKEN") != (finals["B"][0] == "TAKEN")
            for system, arm, pos in (("BASELINE", "A", pa), ("AI_ASSISTED", "B", pb)):
                row = (sig_a if arm == "A" else sig_b).get(sid, s)
                body = {"signal_id": sid, "symbol": s["symbol"], "exchange": "NSE",
                        "decision_time": s["signal_time"], "market_date": s["trading_date"],
                        "mode": meta["mode"], "system": system,
                        "proposal": {"entry_reference": row["entry_reference"],
                                     "stop": row["stop"], "target": row["target"],
                                     "expiry": row["valid_until"], "quantity": row["quantity"],
                                     "planned_net_loss": row["planned_net_loss"]},
                        "risk_engine": {"status": row["review_status"],
                                        "reasons": row["reasons"]},
                        "final_decision": finals[arm][0], "reason": finals[arm][1],
                        "evaluation_status": "EVALUATED"}
                if arm == "B":
                    body["ai_recommendations"] = [r["recommendation_id"] for r in recs
                                                  if r.get("signal_id") == sid]
                did = ledger.add_decision(run_id, system, body)
                if sid in pos:
                    ledger.add_outcome(did, "TRADE_CLOSED", now, _trade(pos[sid]))

        sessions = [date.fromisoformat(d) for d in eligible]
        ta, tb = [_trade(p) for p in pos_a], [_trade(p) for p in pos_b]
        initial = D(live.capital.initial_capital)
        ma, mb = M.arm_metrics(ta, sessions, initial), M.arm_metrics(tb, sessions, initial)
        for m, rows, sigs, ords, dec, trades in (
                (ma, rows_a, sig_a, ord_a, decided["A"], ta),
                (mb, rows_b, sig_b, ord_b, decided["B"], tb)):
            st_counts = Counter(r["status"] for r in rows.values())
            m.update({"complete_sessions": st_counts.get("COMPLETED", 0),
                      "incomplete_sessions": st_counts.get("INCOMPLETE", 0),
                      "failed_sessions": st_counts.get("FAILED", 0),
                      "signals_evaluated": len(sigs), "decisions": dict(dec),
                      "recommendations_taken": dec["TAKEN"],
                      "rejected_candidates": len(sigs) - dec["TAKEN"],
                      "no_trade_decisions": len(sigs) - dec["TAKEN"], **_orders(ords),
                      "risk_rule_violations": {"total": sum(
                          1 for t in trades if sigs.get(t["signal_id"], {}).get("review_status")
                          != "QUALIFIED_FOR_REVIEW")},
                      "data_quality_failures": sum(1 for r in rows.values()
                                                   if r["status"] != "COMPLETED")})
        st = protocol.statistics
        table_a, table_b = M.session_table(ta, sessions), M.session_table(tb, sessions)
        boot = M.paired_bootstrap(table_a, table_b, sessions, st.bootstrap_resamples,
                                  st.confidence, st.seed, st.block_length)
        quality = decision_quality(recs, models, tools, runs)
        usd, fx = quality["operational"]["cost_usd"], live.ai_eval.usd_inr_rate
        model_inr = round(usd * fx, 4) if usd is not None and fx else None
        selection = M.random_veto_benchmark(ta, tb, st.bootstrap_resamples, st.seed,
                                            st.confidence)
        structural = None if ta else ("no baseline paper trade in the eligible sessions; a "
                                      "veto-only AI cannot change any trade")
        concl = M.assess(protocol, len(sessions), ma, mb, discordant, boot, model_inr,
                         structural, selection)
        net_diff = round(mb["net_pnl"] - ma["net_pnl"], 2)
        equity = {}
        for arm, table in (("A", table_a), ("B", table_b)):
            cum, pts = initial, []
            for d in sessions:
                cum += table[d][0]
                pts.append((d.isoformat(), M._f(cum)))
            equity[arm] = pts

        def diff(k):
            return round(mb[k] - ma[k], 2) if ma[k] is not None and mb[k] is not None else None

        out = {
            "status": "OK", "run_id": run_id, "meta": meta, "labels": labels,
            "protocol": protocol.model_dump(mode="json"),
            "systems": {
                "A": "deterministic baseline: the existing paper engine on its own portfolio",
                "B": "AI-assisted: an identical paper engine on its own portfolio, ordering a "
                     "risk-approved signal only with a CANDIDATE recommendation persisted "
                     "before the order decision; same data snapshot, costs and fills",
                "ai_may_change": "which deterministic signals are taken (veto only)",
                "ai_may_not_change": "entry, stop, target, quantity, costs, risk limits, "
                                     "fills, data, or add a signal"},
            "coverage": {"sessions": len(sessions), "period": [eligible[0], eligible[-1]],
                         "session_status": session_table,
                         "excluded_reconciliation_flagged": {"A": ex_a, "B": ex_b},
                         "limitations": [
                             "the AI arm's portfolio diverges from the baseline after its "
                             "first veto, so later signals can be sized differently or "
                             "taken only by one arm (reported as capacity/shared effects)"]},
            "arms": {"A": ma, "B": mb}, "equity": equity,
            "incremental": {
                "net_pnl_difference": net_diff, "expectancy_difference": diff("expectancy"),
                "max_drawdown_difference": diff("max_drawdown_inr"),
                "transaction_cost_difference": diff("total_costs"),
                "trade_count_difference": mb["closed_trades"] - ma["closed_trades"],
                "exposure_difference": diff("exposure_buy_turnover"),
                "model_cost_usd": usd, "usd_inr_rate": fx, "model_cost_inr": model_inr,
                "model_tokens": {"input": quality["operational"]["input_tokens"],
                                 "output": quality["operational"]["output_tokens"]},
                "net_incremental_after_model_cost_inr": round(net_diff - model_inr, 2)
                if model_inr is not None else None,
                "net_incremental_note": None if model_inr is not None else
                "not computed: model prices or ai_eval.usd_inr_rate not configured; never "
                "guessed",
                "decomposition": M.decompose(ta, tb),
                "explanatory_value": "reported separately in 'quality'; it is not trading "
                                     "value"},
            "statistics": boot, "discordant_signals": discordant,
            "random_veto_benchmark": selection, "quality": quality,
            "capital": {"initial_capital": float(initial),
                        "risk_status_counts": dict(Counter(s["review_status"]
                                                           for s in sig_a.values())),
                        "executable_baseline_signals": decided["A"]["TAKEN"]},
            "per_session": [{"session": d, "A_trades": table_a[d][1],
                             "A_net": M._f(table_a[d][0]), "B_trades": table_b[d][1],
                             "B_net": M._f(table_b[d][0])} for d in sessions],
            "regimes": M.regime_table(None, sessions, table_a, table_b),
            "conclusion": concl,
        }
    except Exception as exc:
        ledger.finish_run(run_id, "FAILED", now, f"{type(exc).__name__}: {exc}"[:500])
        raise
    from app.aieval.report import write_reports

    path, digest = write_reports(out, private_dir(Path(live.ai_eval.output_dir) / run_id))
    ledger.finish_run(run_id, "COMPLETED", now, None, out["conclusion"]["conclusion"],
                      str(path), digest)
    return out
