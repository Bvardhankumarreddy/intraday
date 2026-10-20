"""Dashboard view: operational monitoring (Phase 15). Read-only; no secrets are
shown; unavailable values are shown as "unavailable", never as zero."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def _v(x):
    return "unavailable" if x is None else x


def render(st, settings) -> None:
    st.subheader("Monitoring: health, AI quality, performance drift, versions")
    st.info("Monitoring observes and alerts. It never changes the strategy or promotes a "
            "version. Paper trading only.")
    db = Path(settings.monitoring.db_path)
    if not db.exists():
        st.info("No monitoring data yet: `python -m app.monitoring run` (integrated sessions "
                "run it after every step).")
        return
    from app.monitoring import state
    from app.monitoring.alerts import AlertCenter, MonitorStore

    ms = MonitorStore(db)
    cur = state.current(ms)
    snaps = ms.all("SELECT * FROM monitor_snapshots ORDER BY ts DESC LIMIT 1")
    snap = json.loads(snaps[0]["body"]) if snaps else {}
    m = snap.get("metrics", {})
    color = st.success if cur["state"] == "NORMAL" else st.error \
        if cur["state"] == "PAPER_TRADING_PAUSED" else st.warning
    color(f"System state: {cur['state']} since {cur['since']} - {cur['reason']}")
    if cur.get("paused_arms"):
        st.error(f"Paused arms (no new paper entries): {cur['paused_arms']}")

    st.markdown("### System health")
    d = m.get("data", {})
    cols = st.columns(4)
    cols[0].metric("Session", "open" if d.get("in_session") else d.get("market", "closed"))
    cols[1].metric("Heartbeat age (s)", _v(d.get("heartbeat_age_seconds")))
    cols[2].metric("Missing candles %", _v(d.get("missing_candle_pct")))
    cols[3].metric("Free disk (MB)", _v((m.get("storage") or {}).get("free_disk_mb")))
    st.json({"provider": (d.get("heartbeat") or {}).get("provider"),
             "databases": m.get("storage"), "last_snapshot": snap.get("ts")}, expanded=False)

    st.markdown("### Research-agent quality")
    a = m.get("ai", {})
    cols = st.columns(4)
    cols[0].metric("Model requests", _v(a.get("model_requests")))
    cols[1].metric("Failure rate", _v(a.get("failure_rate")))
    cols[2].metric("Latency p95 (s)", _v(a.get("latency_p95")))
    cols[3].metric("Cost today (USD)", _v(a.get("cost_usd_today")))
    st.json({k: a.get(k) for k in ("invalid_rate", "unsupported_rate",
                                   "deterministic_rejections", "duplicate_candidates",
                                   "no_trade_share_last", "no_trade_share_ref", "error_kinds",
                                   "retried_requests", "tokens")}, expanded=False)

    st.markdown("### Paper-trading performance (drift needs enough evidence)")
    full = ms.all("SELECT body FROM monitor_snapshots WHERE scope = 'full' ORDER BY ts DESC "
                  "LIMIT 1")
    perf = json.loads(full[0]["body"])["metrics"].get("performance", {}) if full else {}
    if perf:
        st.dataframe(pd.DataFrame([{
            "arm": arm, "closed trades": p.get("closed_trades"), "sessions": p.get("sessions"),
            "period": p.get("period"), "classification": p.get("classification",
                                                                 p.get("kind")),
            "recent expectancy": (p.get("performance") or {}).get("recent_expectancy"),
            "reference expectancy": (p.get("performance") or {}).get("reference_expectancy"),
            "losing streak": p.get("recent_losing_streak")} for arm, p in perf.items()]),
            width="stretch")
    else:
        st.info("No full evaluation yet (runs at session end or `run --full`).")

    st.markdown("### Version management")
    st.json(m.get("versions", {}), expanded=True)

    st.markdown("### Open alerts")
    alerts = AlertCenter(ms, settings, notifiers=[]).open()
    if alerts:
        st.dataframe(pd.DataFrame([{k: x[k] for k in ("alert_id", "severity", "component",
                                                      "rule", "threshold", "occurrences",
                                                      "detected_at", "session", "version")}
                                   for x in alerts]), width="stretch")
    else:
        st.success("No open alert.")
    st.markdown("### State history")
    st.dataframe(pd.DataFrame(ms.all("SELECT ts, from_state, to_state, actor, reason FROM "
                                     "monitor_state_events ORDER BY id DESC LIMIT 50")),
                 width="stretch")
