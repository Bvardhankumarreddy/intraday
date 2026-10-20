"""Stock screener: PASS, REJECTED or INSUFFICIENT_DATA with explicit reasons.

Every check runs (where its inputs exist) so the user sees all reasons, not
just the first. Any definitive rejection makes the result REJECTED; otherwise
any missing input makes it INSUFFICIENT_DATA.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.config import Settings
from app.scan.models import Reason, ScreenResult, ScreenStatus
from app.scan.snapshot import MarketSnapshot
from app.screening.liquidity import TRADED_VALUE_BASIS, average_traded_value, quote_spread_pct
from app.screening.universe import eligibility_rejections
from app.utils.market_calendar import MarketCalendar


def screen(snap: MarketSnapshot, settings: Settings, calendar: MarketCalendar) -> ScreenResult:
    cfg = settings.screening
    passed: list[Reason] = []
    rejected: list[Reason] = eligibility_rejections(snap.instrument, settings)
    missing: list[Reason] = list(snap.data_errors)

    # -- freshness --------------------------------------------------------
    stale = _staleness(snap, settings, calendar)
    if stale:
        missing.append(Reason(code="stale_data", message=stale))

    # -- price band -------------------------------------------------------
    price, price_time, price_label = _last_price(snap)
    if price is None:
        missing.append(Reason(code="no_price", message="no price available at evaluation time"))
    elif not cfg.min_price <= price <= cfg.max_price:
        rejected.append(
            Reason(
                code="price_out_of_band",
                message=f"{price_label} Rs {price:.2f} outside Rs {cfg.min_price:g}-{cfg.max_price:g}",
            )
        )
    else:
        passed.append(
            Reason(code="price_in_band", message=f"{price_label} Rs {price:.2f} within band")
        )

    # -- liquidity ----------------------------------------------------------
    atv, sessions = average_traded_value(snap.daily, cfg.liquidity_lookback_sessions)
    lakh = cfg.min_avg_traded_value / 1e5
    if atv is None or sessions < cfg.liquidity_lookback_sessions:
        missing.append(
            Reason(
                code="insufficient_liquidity_history",
                message=f"{sessions} of {cfg.liquidity_lookback_sessions} sessions available "
                "for average traded value",
            )
        )
    elif atv < cfg.min_avg_traded_value:
        rejected.append(
            Reason(
                code="illiquid",
                message=f"avg traded value Rs {atv / 1e5:,.1f} lakh < Rs {lakh:,.1f} lakh "
                f"({TRADED_VALUE_BASIS})",
            )
        )
    else:
        passed.append(
            Reason(
                code="liquid",
                message=f"avg traded value Rs {atv / 1e5:,.1f} lakh over {sessions} sessions "
                f"({TRADED_VALUE_BASIS})",
            )
        )

    # -- spread -------------------------------------------------------------
    spread = quote_spread_pct(snap.quote)
    if spread is None:
        why = snap.quote_issue or "quote has no bid/ask"
        if cfg.require_spread:
            missing.append(Reason(code="spread_unavailable", message=f"spread unavailable: {why}"))
        else:
            passed.append(
                Reason(
                    code="spread_not_required",
                    message=f"spread unavailable ({why}); not required by configuration",
                )
            )
    elif spread > cfg.max_spread_pct + 1e-9:
        rejected.append(
            Reason(
                code="wide_spread",
                message=f"spread {spread:.3f}% > {cfg.max_spread_pct:.3f}%",
            )
        )
    else:
        passed.append(Reason(code="tight_spread", message=f"spread {spread:.3f}%"))

    if rejected:
        status = ScreenStatus.REJECTED
    elif missing:
        status = ScreenStatus.INSUFFICIENT_DATA
    else:
        status = ScreenStatus.PASS
    return ScreenResult(
        status=status,
        last_price=price,
        last_price_time=price_time,
        avg_traded_value=atv,
        avg_traded_value_sessions=sessions,
        spread_pct=spread,
        spread_observed_at=snap.quote.timestamp if spread is not None else None,
        passed=passed,
        rejections=rejected + missing,
    )


def _last_price(snap: MarketSnapshot) -> tuple[float | None, datetime | None, str]:
    """Most recent price known at ``as_of``: quote, last completed candle, or
    the previous session's close (before today's first candle completes)."""
    candle_price = candle_time = None
    today = snap.today
    if not today.empty:
        candle_price = float(today["close"].iloc[-1])
        candle_time = snap.last_candle_end
    if snap.quote is not None and (candle_time is None or snap.quote.timestamp >= candle_time):
        return snap.quote.last_price, snap.quote.timestamp, "last traded price"
    if candle_price is not None:
        return candle_price, candle_time, "last completed candle close"
    if snap.daily is not None and not snap.daily.empty:
        return float(snap.daily["close"].iloc[-1]), None, "previous session close"
    return None, None, ""


def _staleness(snap: MarketSnapshot, settings: Settings, calendar: MarketCalendar) -> str | None:
    """Message if today's candles lag the evaluation time, else None."""
    if snap.intraday is None or not calendar.is_trading_day(snap.session_date):
        return None
    open_dt, close_dt = calendar.session_bounds(snap.session_date)
    step = snap.interval.timedelta
    effective = min(snap.as_of, close_dt)
    if effective < open_dt + step:
        return None  # no candle could have completed yet
    expected_end = open_dt + ((effective - open_dt) // step) * step
    last_end = snap.last_candle_end if not snap.today.empty else None
    tolerance = timedelta(seconds=settings.data.max_staleness_seconds)
    if last_end is None:
        return f"no completed candles today; expected data up to {expected_end:%H:%M}"
    if last_end < expected_end - tolerance:
        return f"latest candle ends {last_end:%H:%M}, expected {expected_end:%H:%M}"
    return None
