"""Instrument eligibility: exchange, series and tradability."""

from __future__ import annotations

from app.config import Settings
from app.data.models import Instrument
from app.scan.models import Reason


def eligibility_rejections(instrument: Instrument, settings: Settings) -> list[Reason]:
    reasons = []
    if instrument.exchange != settings.market.exchange:
        reasons.append(
            Reason(code="wrong_exchange", message=f"listed on {instrument.exchange}, not NSE")
        )
    if instrument.series not in settings.screening.allowed_series:
        reasons.append(
            Reason(
                code="series_not_allowed",
                message=f"series {instrument.series} not in {settings.screening.allowed_series}",
            )
        )
    if not instrument.tradable:
        reasons.append(
            Reason(code="not_tradable", message="instrument is suspended or not normally tradable")
        )
    return reasons
