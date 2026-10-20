"""Dashboard view: integrated baseline + AI-assisted paper trading (Phase 12).
Read-only. Missing values are shown as missing, never as zero."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd

from app.costs.money import D


def _rows(store, sql, params=()):
    try:
        return [dict(r) for r in store.all(sql, params)]
    except Exception:  # noqa: BLE001
        return []


def render(st, settings) -> None:
    from app.paper.store import PaperStore

    st.subheader("Integrated paper trading: baseline vs AI-assisted (simulated)")
    st.error("PAPER TRADING - SIMULATED. Two virtual portfolios; no real order is placed. Real "
             "orders are placed manually by you.")
    a_path, b_path = Path(settings.paper.db_path), Path(settings.integrated.ai_db_path)
    if not b_path.exists() or not a_path.exists():
        st.info("No integrated session yet: `python -m app.integrated start --name ...` then "
                "`python -m app.integrated run` (or `replay --date D`).")
        return
    a, b = PaperStore(a_path), PaperStore(b_path)
    proto = b.get_meta("integrated_protocol")
    st.markdown("**Protocol and versions**")
    st.json(proto or {"note": "no integrated evaluation started"}, expanded=False)

    st.markdown("**System and session health**")
    cols = st.columns(3)
    hb = a.get_meta("heartbeat") or {}
    sched = a.get_meta("scheduler") or {}
    cols[0].metric("Scheduler", sched.get("state", "n/a"))
    cols[1].metric("Last step", str(hb.get("time", "n/a"))[:19])
    cols[2].metric("Fresh symbols", str(hb.get("fresh_symbols", "n/a")))
    calls = _rows(b, "SELECT started_at, error_kind, model FROM agent_model_calls ORDER BY "
                     "started_at DESC LIMIT 20")
    if calls:
        bad = sum(1 for c in calls if c["error_kind"])
        st.caption(f"AI provider {settings.llm.provider.value}: last {len(calls)} requests, "
                   f"{bad} failed; latest {calls[0]['started_at'][:19]} "
                   f"({calls[0]['model']})")
    else:
        st.caption(f"AI provider {settings.llm.provider.value}: no model request recorded yet")

    days = sorted({r["trading_date"] for r in _rows(a, "SELECT trading_date FROM sessions")}
                  | {r["trading_date"] for r in _rows(b, "SELECT trading_date FROM sessions")},
                  reverse=True)
    if not days:
        st.info("No session recorded yet.")
        return
    day = st.sidebar.selectbox("Session", days)
    sess = {n: _rows(s, "SELECT state, data_mode, data_source FROM sessions WHERE "
                        "trading_date = ?", (day,)) for n, s in (("A", a), ("B", b))}
    st.write({n: (v[0] if v else "not recorded") for n, v in sess.items()})

    st.markdown("**Recommendations (original timestamps; never edited)**")
    recs = [json.loads(r["body"]) for r in _rows(
        b, "SELECT body FROM agent_recommendations WHERE market_date = ? ORDER BY created_at",
        (day,))]
    if recs:
        st.dataframe(pd.DataFrame([{
            "created_at": r["created_at"], "symbol": r["symbol"] or "(run)",
            "status": r["status"], "AI": r["ai_status"], "risk": r.get("risk_status"),
            "qty": r.get("quantity"), "entry": r.get("entry_reference"), "stop": r.get("stop"),
            "target": r.get("target"), "unsupported claims": len(r["unsupported_claims"])}
            for r in recs]), width="stretch")
    else:
        st.info("No recommendation recorded for this session.")

    st.markdown("**Risk-engine approvals and rejections; paper orders**")
    for name, store in (("A baseline", a), ("B AI-assisted", b)):
        sig = _rows(store, "SELECT symbol, signal_time, review_status, quantity FROM signals "
                           "WHERE trading_date = ?", (day,))
        st.write(name, pd.DataFrame(sig) if sig else "no signal (a no-trade session is valid)")

    st.markdown("**Portfolios (virtual)**")
    pf = {}
    for name, store in (("A baseline", a), ("B AI-assisted", b)):
        p = _rows(store, "SELECT * FROM portfolio")
        pf[name] = {"cash": p[0]["cash"], "reserved": p[0]["reserved"],
                    "available": str(D(p[0]["cash"]) - D(p[0]["reserved"]))} if p else "n/a"
    st.write(pf)

    st.markdown("**Daily and cumulative net P&L (closed trades, after all costs)**")
    series = defaultdict(dict)
    for name, store in (("A baseline", a), ("B AI-assisted", b)):
        for r in _rows(store, "SELECT trading_date, SUM(CAST(net_pnl AS REAL)) AS net, "
                              "SUM(CAST(total_costs AS REAL)) AS costs, COUNT(*) AS n FROM "
                              "positions WHERE state = 'CLOSED' GROUP BY trading_date"):
            series[r["trading_date"]][name] = r["net"]
            series[r["trading_date"]][name + " costs"] = r["costs"]
            series[r["trading_date"]][name + " trades"] = r["n"]
    if series:
        frame = pd.DataFrame.from_dict(series, orient="index").sort_index()
        st.dataframe(frame, width="stretch")
        nets = frame[[c for c in frame.columns if not c.endswith(("costs", "trades"))]]
        st.line_chart(nets.fillna(0).cumsum())
    else:
        st.info("No closed paper trade yet in either arm.")

    st.markdown("**Baseline vs AI-assisted (latest cumulative evaluation)**")
    from app.aieval.ledger import Ledger

    db = Path(settings.ai_eval.ledger_db)
    runs = [r for r in Ledger(db).runs() if r["mode"] == "PROSPECTIVE_INTEGRATED"
            and r["results_path"]] if db.exists() else []
    if runs:
        res = json.loads(Path(runs[-1]["results_path"]).read_text())
        st.markdown(f"**{res['conclusion']['conclusion']}**: {res['conclusion']['reason']}")
        unmet = [k for k, v in res["conclusion"].get("minimum_evidence", {}).items()
                 if not v["met"]]
        if unmet:
            st.warning("Sample-size warning: below the predeclared minimum for "
                       + ", ".join(unmet))
        st.json({k: res["incremental"][k] for k in ("net_pnl_difference",
                                                    "transaction_cost_difference",
                                                    "max_drawdown_difference",
                                                    "model_cost_usd")})
    else:
        st.info("No cumulative comparison yet: `python -m app.integrated cumulative`.")

    st.markdown("**Data-quality and execution incidents**")
    inc = []
    for name, store in (("A", a), ("B", b)):
        inc += [{"arm": name, **e} for e in _rows(
            store, "SELECT ts, kind, level, message FROM events WHERE trading_date = ? AND "
                   "(level IN ('WARNING', 'ERROR') OR kind IN ('RECOVERY', 'INTEGRATION'))",
            (day,))]
    st.dataframe(pd.DataFrame(inc), width="stretch") if inc else st.success(
        "No incident recorded for this session.")
    if date.fromisoformat(day) and len(days) < 20:
        st.warning(f"{len(days)} session(s) recorded: far too few to judge either system.")
