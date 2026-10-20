"""Streamlit dashboard - paper trading, evaluation and robustness views (read-only).

Run:  .venv/bin/streamlit run app/dashboard.py

Reads the paper database only. It cannot place, modify or cancel orders, and
it shows simulated (paper) positions only - never real broker holdings.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

# Streamlit runs this file as a script with only app/ on sys.path; add the
# project root so `import app...` works from any working directory.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.paper import views  # noqa: E402
from app.paper.report import ASSUMPTIONS, LABEL  # noqa: E402
from app.paper.store import PaperStore  # noqa: E402

st.set_page_config(page_title="NSE Paper Trading", layout="wide")
settings = load_settings()
db_path = Path(os.environ.get("NSE_PAPER_DB", settings.paper.db_path))

st.title("NSE intraday research - paper trading")
st.error(f"**PAPER TRADING - SIMULATED.** {LABEL} These are not your broker holdings.")
view = st.sidebar.radio("View", ["Paper session", "Evaluation", "Integrated", "AI research",
                                "AI evaluation", "Learning", "Experiments", "Monitoring",
                                "Robustness", "Operations"])

if view == "Monitoring":
    from app.monitoring import views as monitoring_views

    monitoring_views.render(st, settings)
    st.stop()

if view == "Experiments":
    from app.experiments import views as experiment_views

    experiment_views.render(st, settings)
    st.stop()

if view == "Learning":
    from app.learning import views as learning_views

    learning_views.render(st, settings)
    st.stop()

if view == "Integrated":
    from app.integrated import views as integrated_views

    integrated_views.render(st, settings)
    st.stop()

if view == "AI evaluation":
    from app.aieval import views as aieval_views

    aieval_views.render(st, settings)
    st.stop()

if view == "Operations":
    from app.ops import views as ops_views

    ops_views.render(st, settings, db_path)
    st.stop()

if view == "Robustness":
    from app.robustness import views as robustness_views

    robustness_views.render(st, settings)
    st.stop()

if view == "AI research":
    from app.agent import views as agent_views

    if not db_path.exists():
        st.info("No paper database yet; run `python -m app.agent run --window ...` first.")
        st.stop()
    agent_views.render(st, settings, PaperStore(db_path))
    st.stop()

if not db_path.exists():
    st.info(f"No paper database yet at `{db_path}`. Run a session first, e.g. "
            "`python -m app.paper replay --date YYYY-MM-DD`.")
    st.stop()

store = PaperStore(db_path)

if view == "Evaluation":
    from app.evaluation.metrics import cumulative_metrics, daily_series
    from app.evaluation.recorder import EvaluationRecorder
    from app.utils.market_calendar import MarketCalendar

    recorder = EvaluationRecorder(store, settings, MarketCalendar(settings.market))
    evaluations = store.all("SELECT * FROM evaluations ORDER BY created_at DESC")
    if not evaluations:
        st.info("No evaluation yet. Start one: `python -m app.evaluation start --name ...`")
        st.stop()
    ev = next(e for e in evaluations if e["evaluation_id"] == st.sidebar.selectbox(
        "Evaluation", [e["evaluation_id"] for e in evaluations]))
    primary_only = st.sidebar.checkbox("Primary only (LIVE, frozen config)", value=True)
    st.subheader(f"Evaluation {ev['name']} ({ev['evaluation_id']})")
    st.caption(f"Started {ev['start_date']} - frozen config {ev['frozen_config_version']} - "
               f"current config {settings.version()}"
               + ("" if ev["frozen_config_version"] == settings.version()
                  else " (DIFFERS: new sessions are not primary)"))
    m = cumulative_metrics(store, ev["evaluation_id"], settings, primary_only)
    st.warning(m["evidence"])
    c = st.columns(5)
    c[0].metric("Scheduled sessions", m["sessions"]["scheduled"],
                f"target {settings.evaluation.observation_target_sessions}")
    c[1].metric("Completed", m["sessions"]["completed"])
    c[2].metric("Incomplete", m["sessions"]["incomplete"])
    c[3].metric("Failed", m["sessions"]["failed"])
    c[4].metric("Sessions with trades", m["sessions"]["with_trades"])
    c = st.columns(5)
    t = m["trades"]
    c[0].metric("Net P&L (closed, eligible)", f"Rs {t['net_pnl']}")
    c[1].metric("Total costs", f"Rs {t['total_costs']}")
    c[2].metric("Win rate", f"{t['win_rate_pct']}%" if t["win_rate_pct"] else "n/a")
    c[3].metric("Profit factor", t["profit_factor"] or "undefined")
    c[4].metric("Max drawdown", f"Rs {t['max_drawdown']}")
    f = m["funnel"]
    st.markdown(f"**Funnel:** {f['signals']} signals -> {f['signals_rejected']} rejected -> "
                f"{f['orders_created']} paper orders -> {f['orders_filled']} filled -> "
                f"{f['closed_trades']} closed ({f['trades_excluded_reconciliation']} excluded "
                "by reconciliation)")
    series = pd.DataFrame(daily_series(store, ev["evaluation_id"], primary_only))
    tabs = st.tabs(["Sessions", "P&L", "Data quality", "Rejections", "Config changes"])
    with tabs[0]:
        st.dataframe(series, width="stretch")
    with tabs[1]:
        if not series.empty:
            chart = series.set_index("trading_date")[["net_pnl", "cumulative_net_pnl"]] \
                .astype(float)
            st.bar_chart(chart["net_pnl"])
            st.line_chart(chart["cumulative_net_pnl"])
            eq = series.set_index("trading_date")["end_equity"].dropna().astype(float)
            if not eq.empty:
                st.line_chart(pd.DataFrame({"equity": eq, "drawdown": eq - eq.cummax()}))
    with tabs[2]:
        st.json(m["data_quality"])
        st.json(m["monitoring"])
        st.dataframe(pd.DataFrame([dict(r) for r in store.all(
            "SELECT * FROM dq_batches WHERE missing > 0 OR duplicates > 0 OR out_of_order > 0 "
            "OR invalid_ohlc > 0 OR invalid_volume > 0 OR stale = 1 OR error IS NOT NULL "
            "ORDER BY ts DESC LIMIT 500")]), width="stretch")
    with tabs[3]:
        st.json(m["rejections"])
    with tabs[4]:
        st.dataframe(pd.DataFrame([dict(r) for r in store.all(
            "SELECT * FROM config_changes WHERE evaluation_id = ? ORDER BY id",
            (ev["evaluation_id"],))]), width="stretch")
    st.stop()

sessions = views.sessions(store)
if sessions.empty:
    st.info("No paper sessions recorded yet.")
    st.stop()

day = date.fromisoformat(st.sidebar.selectbox("Trading session", sessions["trading_date"]))
row = sessions[sessions["trading_date"] == day.isoformat()].iloc[0]
summary = views.portfolio_summary(store, day)
health = views.data_health(store, day)

st.subheader(f"Session {day} - {row['state']}")
c = st.columns(4)
c[0].metric("Data source", f"{row['data_source']}", row["data_mode"].upper())
c[1].metric("Last market-data time", str(health["last_candle_or_price_time"] or "none"))
c[2].metric("Data health", "OK" if not health["unhealthy"] else
            f"{len(health['unhealthy'])} symbol(s) not OK")
c[3].metric("Daily loss lock", "LOCKED" if summary["locked"] else "open",
            summary["lock_reason"] or "")
if row["data_mode"] != "live":
    st.warning(f"Data mode is {row['data_mode'].upper()}: this session did not use live data.")

st.subheader("Paper portfolio")
c = st.columns(6)
fmt = lambda v: "unknown" if v is None else f"Rs {v}"  # noqa: E731
c[0].metric("Cash", fmt(summary["cash"]))
c[1].metric("Reserved", fmt(summary["reserved"]))
c[2].metric("Equity", fmt(summary["equity"]))
c[3].metric("Realized P&L today (net)", fmt(summary["realized_pnl_today"]))
c[4].metric("Unrealized P&L (gross)", fmt(summary["unrealized_pnl"]))
c[5].metric("Daily P&L", fmt(summary["daily_pnl"]))

tabs = st.tabs(["Watchlist", "Signals", "Orders", "Positions", "Events", "Report",
                "Assumptions"])
with tabs[0]:
    st.dataframe(views.watchlist_table(store, day), width="stretch")
with tabs[1]:
    st.dataframe(views.signals_table(store, day), width="stretch")
with tabs[2]:
    st.dataframe(views.orders_table(store, day), width="stretch")
with tabs[3]:
    st.caption("Simulated paper positions. Costs are estimates from the Phase 3 fee schedule "
               "plus assumed spread/slippage.")
    st.dataframe(views.positions_table(store, day), width="stretch")
with tabs[4]:
    st.dataframe(views.events_table(store, day), width="stretch")
with tabs[5]:
    report = store.report(day)
    if report is None:
        st.info("No daily report yet (generated when the session closes).")
    else:
        st.json(report["totals"])
        st.json(report["statistics"])
        st.download_button("Download report (JSON)", json.dumps(report, indent=2),
                           file_name=f"paper_{day}.json", mime="application/json")
with tabs[6]:
    for a in ASSUMPTIONS:
        st.markdown(f"- {a}")
    st.markdown("- A stop does not guarantee the loss; gaps and missing data can make it worse.")
    st.markdown("- Simulated results are not evidence of real profitability.")
