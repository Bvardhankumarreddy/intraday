"""Scan: snapshot -> screen -> strategy -> score -> rank -> decision."""

from __future__ import annotations

from datetime import datetime

from app.config import Settings
from app.data.base_provider import MarketDataProvider
from app.scan.models import (
    Candidate,
    Reason,
    ReviewStatus,
    ScanDecision,
    ScanResult,
    ScreenStatus,
    SignalStatus,
)
from app.risk.engine import RiskCostEngine
from app.risk.interfaces import TradeEvaluationRequest
from app.risk.risk_limits import DailyRiskState
from app.scan.snapshot import build_snapshot
from app.screening.ranking import rank, score_candidate
from app.screening.screener import screen
from app.strategies.opening_range_breakout import evaluate
from app.utils.market_calendar import MarketCalendar


def run_scan(
    provider: MarketDataProvider,
    settings: Settings,
    calendar: MarketCalendar,
    as_of: datetime,
    risk_engine: RiskCostEngine | None = None,
    risk_state: DailyRiskState | None = None,
    symbols: set[str] | None = None,
) -> ScanResult:
    """Screen, evaluate and rank every instrument at ``as_of``.

    Without ``risk_engine`` the result is the Phase 2 technical view
    (QUALIFIED_PRE_COST, costs not evaluated). With it, every ranked candidate
    is also sized and costed, and the decision is CANDIDATES_FOR_REVIEW only if
    at least one is QUALIFIED_FOR_REVIEW. Each candidate is assessed against
    the same daily state, as if it were the only new trade. ``symbols``
    optionally restricts the scan to a subset of instruments.
    """
    as_of = as_of.astimezone(calendar.tz)
    version = settings.version()
    candidates: list[Candidate] = []
    seen_signals: set[str] = set()

    instruments = provider.list_instruments()
    if symbols is not None:
        instruments = [i for i in instruments if i.symbol in symbols]
    for instrument in sorted(instruments, key=lambda i: i.symbol):
        snap = build_snapshot(provider, instrument, settings, calendar, as_of)
        screened = screen(snap, settings, calendar)
        candidate = Candidate(
            symbol=instrument.symbol,
            name=instrument.name,
            exchange=instrument.exchange,
            evaluated_at=as_of,
            data_mode=snap.mode,
            data_source=snap.source,
            last_candle_end=snap.last_candle_end,
            data_warnings=snap.warnings,
            screen=screened,
            signal_status=SignalStatus.NOT_EVALUATED,
            rejection_reasons=list(screened.rejections),
            qualification_reasons=list(screened.passed),
            config_version=version,
        )
        if screened.status is ScreenStatus.PASS:
            ev = evaluate(snap, settings, calendar)
            candidate.signal_status = ev.status
            candidate.indicators = ev.indicators
            candidate.setup = ev.setup
            candidate.qualification_reasons += ev.qualification
            candidate.rejection_reasons += ev.rejections
            if ev.setup is not None and ev.setup.signal_id in seen_signals:
                candidate.signal_status = SignalStatus.REJECTED
                candidate.rejection_reasons.append(
                    Reason(code="duplicate_signal", message=f"duplicate of {ev.setup.signal_id}"))
            elif ev.status is SignalStatus.QUALIFIED_PRE_COST:
                seen_signals.add(ev.setup.signal_id)
                candidate.score, candidate.score_components = score_candidate(
                    screened, ev, settings)
        candidates.append(candidate)

    ranked = rank(candidates)
    notes = [
        f"Data mode: {provider.mode.value.upper()}. Prices are not live unless marked LIVE.",
        "Scores order setups that passed every rule; they are not probabilities of profit.",
    ]
    reviewable: list[Candidate] = []
    if risk_engine is None:
        notes.append("Position size, affordability, transaction costs and net P&L are NOT "
                     "evaluated. Do not treat any candidate as affordable or profitable.")
        decision = ScanDecision.CANDIDATES_FOR_REVIEW if ranked else ScanDecision.NO_TRADE
    else:
        state = (risk_state or DailyRiskState.new_session(as_of.date())).roll_to(as_of, calendar)
        for candidate in ranked:
            request = TradeEvaluationRequest.from_candidate(candidate, settings)
            report = risk_engine.evaluate(request, state)
            candidate.risk_report = report
            candidate.risk_cost_status = report.status
            candidate.qualification_reasons += report.qualification_reasons
            candidate.rejection_reasons += report.rejection_reasons
        reviewable = [c for c in ranked
                      if c.risk_cost_status is ReviewStatus.QUALIFIED_FOR_REVIEW]
        decision = ScanDecision.CANDIDATES_FOR_REVIEW if reviewable else ScanDecision.NO_TRADE
        notes.append("Costs and net P&L are estimates from the configured fee schedule plus "
                     "assumed spread/slippage; a stop does not guarantee the maximum loss.")
    if not calendar.holidays_loaded:
        notes.append("No NSE holiday list loaded: weekdays are assumed to be trading days.")
    return ScanResult(
        evaluated_at=as_of,
        session_date=as_of.date().isoformat(),
        data_mode=provider.mode,
        data_source=provider.name,
        config_version=version,
        decision=decision,
        ranked=ranked,
        candidates=candidates,
        risk_evaluated=risk_engine is not None,
        reviewable=reviewable,
        notes=notes,
    )

