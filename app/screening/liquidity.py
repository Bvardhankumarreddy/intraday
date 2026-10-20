"""Liquidity measures.

Average daily traded value is approximated as ``close x volume`` per session
because the current providers do not supply exchange-reported turnover. The
approximation is labelled wherever it is shown.
"""

from __future__ import annotations

import pandas as pd

from app.data.models import Quote

TRADED_VALUE_BASIS = "approx. close x volume per session"


def average_traded_value(daily: pd.DataFrame | None, sessions: int) -> tuple[float | None, int]:
    """Mean traded value over the last ``sessions`` completed sessions.

    Returns ``(value, sessions_used)``. ``value`` is None when no sessions are
    available; callers must also check ``sessions_used`` against the lookback.
    """
    if daily is None or daily.empty:
        return None, 0
    recent = daily.tail(sessions)
    return float((recent["close"] * recent["volume"]).mean()), len(recent)


def quote_spread_pct(quote: Quote | None) -> float | None:
    """Bid-ask spread in % of mid. None when unavailable - never zero."""
    return None if quote is None else quote.spread_pct
