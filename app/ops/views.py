"""Read-only dashboard view of operational health and alerts (Phase 8)."""

from __future__ import annotations

from datetime import datetime

import pandas as pd


def render(st, settings, db_path) -> None:
    from app.ops.health import health_report
    from app.utils.market_calendar import MarketCalendar

    st.subheader("Operations health")
    st.caption("Health reflects dependencies and persisted state, not just a running process. "
               "No provider call is made from the dashboard (use `python -m app.ops "
               "preflight` for a live probe).")
    rep = health_report(settings, datetime.now(MarketCalendar(settings.market).tz), db_path)
    colour = {"HEALTHY": st.success, "DEGRADED": st.warning}.get(rep["status"], st.error)
    colour(f"**{rep['status']}** - checked {rep['checked_at']}")
    st.table(pd.DataFrame([{"check": c["check"], "status": c["status"], "detail": c["detail"]}
                           for c in rep["checks"]]))
    alerts = next((c for c in rep["checks"] if c["check"] == "alerts"), None)
    if alerts and alerts.get("data", {}).get("open"):
        st.markdown("#### Open alerts")
        st.dataframe(pd.DataFrame(alerts["data"]["open"]), width="stretch")
    rec = next((c for c in rep["checks"] if c["check"] == "reconciliation"), None)
    if rec and rec.get("data", {}).get("findings"):
        st.markdown("#### Reconciliation findings (records are never altered)")
        st.dataframe(pd.DataFrame(rec["data"]["findings"]), width="stretch")
    md = next((c for c in rep["checks"] if c["check"] == "market_data"), None)
    if md and md.get("data", {}).get("provider"):
        st.markdown("#### Market-data provider (last step)")
        st.json(md["data"]["provider"])
