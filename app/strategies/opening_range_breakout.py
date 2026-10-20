"""Long-only opening-range breakout (ORB), evaluated on completed candles.

Rules (all must hold on the same completed candle, the "signal candle"):

1. The opening range (first ``opening_range_minutes``) is complete.
2. The candle is a fresh breakout: it closes above the opening-range high
   and the previous candle closed at or below it.
3. The candle closes at or before ``last_signal_time``.
4. Relative volume >= threshold, with enough baseline sessions.
5. Close > session VWAP (and > EMA when the EMA filter is enabled).
6. Close is no more than ``max_extension_pct`` above the range high.
7. A stop below the entry exists, and risk per share is within
   ``min_risk_pct``..``max_risk_pct`` of the entry.

The first candle meeting every rule is the day's only signal, so repeated
scans and later breakouts cannot create duplicates. The signal is known only
once the candle has closed. The entry reference is that close; backtests must
fill at the next available price, never at the signal close.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from app.config import Settings, StopReference
from app.data.models import day_mask
from app.scan.models import Indicators, Reason, SignalStatus, TradeSetup
from app.scan.snapshot import MarketSnapshot
from app.strategies.indicators import (
    OpeningRange,
    ema,
    opening_range,
    relative_volume_detail,
    session_vwap,
)
from app.utils.market_calendar import MarketCalendar

# Reasons that mean "could not evaluate" rather than "evaluated and failed".
DATA_CODES = {"relative_volume_unavailable", "vwap_unavailable", "ema_unavailable"}


@dataclass
class SignalEvaluation:
    status: SignalStatus
    indicators: Indicators
    setup: TradeSetup | None = None
    qualification: list[Reason] = field(default_factory=list)
    rejections: list[Reason] = field(default_factory=list)
    # Scoring inputs (signal candle only).
    close_location: float | None = None  # 0 = closed at low, 1 = at high
    extension_pct: float | None = None
    vwap_distance_pct: float | None = None
    ema_distance_pct: float | None = None


def evaluate(snap: MarketSnapshot, settings: Settings, calendar: MarketCalendar) -> SignalEvaluation:
    cfg = settings.strategy
    if snap.intraday is None:
        reasons = [r for r in snap.data_errors if r.code == "intraday_data"] or [
            Reason(code="no_intraday", message="no intraday data")
        ]
        return SignalEvaluation(SignalStatus.INSUFFICIENT_DATA, Indicators(), rejections=reasons)

    frame = snap.intraday.copy()
    if cfg.use_ema_filter:
        frame["ema"] = ema(frame["close"], cfg.ema_period)  # continuous across sessions
    # VWAP resets every session and only today's values are used, so it is
    # computed on today's candles alone (identical values, less work).
    today = frame[day_mask(frame["timestamp"], snap.session_date)].copy()
    today["vwap"] = session_vwap(today)
    session_open, _ = calendar.session_bounds(snap.session_date)

    rng = opening_range(today, session_open, cfg.opening_range_minutes, snap.interval)
    if rng is None:
        or_end = session_open + pd.Timedelta(minutes=cfg.opening_range_minutes)
        if snap.as_of < or_end:
            return SignalEvaluation(
                SignalStatus.WATCH,
                _indicators(today, None),
                rejections=[Reason(code="opening_range_forming",
                                   message=f"opening range completes at {or_end:%H:%M}")],
            )
        return SignalEvaluation(
            SignalStatus.INSUFFICIENT_DATA,
            _indicators(today, None),
            rejections=[Reason(code="opening_range_incomplete",
                               message="opening-range candles are missing")],
        )

    cutoff = datetime.combine(snap.session_date, cfg.last_signal_time, tzinfo=calendar.tz)
    after = today[today["timestamp"] >= pd.Timestamp(rng.end)]
    prev_close = float(today[today["timestamp"] < pd.Timestamp(rng.end)]["close"].iloc[-1])

    # Fresh breakout: closes above the range high after a close at/below it.
    closes = after["close"].astype(float)
    prev = closes.shift(1, fill_value=prev_close)
    fresh = (closes > rng.high) & (prev <= rng.high)

    last_failure: tuple[pd.Timestamp, list[Reason]] | None = None
    for _, candle in after[fresh].iterrows():
        result = _check_breakout(candle, frame, rng, snap, settings, cutoff)
        if isinstance(result, list):
            last_failure = (candle["timestamp"], result)
            continue
        return _track_signal(result, candle, after, snap)

    # No qualifying signal so far. Indicators (incl. relative volume) are
    # reported at the last completed candle for the audit trail.
    indicators = _indicators(today, rng)
    if not today.empty:
        lv = relative_volume_detail(frame, snap.session_date, today["timestamp"].iloc[-1],
                                    cfg.relative_volume_lookback_sessions,
                                    cfg.relative_volume_mode)
        indicators = indicators.model_copy(update={
            "relative_volume": lv.ratio, "relative_volume_sessions": lv.sessions,
            "relative_volume_today": lv.today_volume,
            "relative_volume_baseline": lv.baseline_volume})
    last_end = snap.last_candle_end
    if last_failure is None:
        reasons = [Reason(code="no_breakout",
                          message=f"no completed candle has closed above the range high "
                                  f"{rng.high:.2f}")]
        data_problem = False
    else:
        ts, failed = last_failure
        reasons = [Reason(code=r.code, message=f"breakout at {ts:%H:%M}: {r.message}")
                   for r in failed]
        data_problem = any(r.code in DATA_CODES for r in failed)
    if data_problem:
        status = SignalStatus.INSUFFICIENT_DATA
    elif last_end is not None and last_end < cutoff:
        status = SignalStatus.WATCH
    else:
        status = SignalStatus.REJECTED
        reasons.append(Reason(code="past_signal_cutoff",
                              message=f"no valid breakout by {cfg.last_signal_time:%H:%M}"))
    return SignalEvaluation(status, indicators, rejections=reasons)


def _check_breakout(
    candle: pd.Series,
    frame: pd.DataFrame,
    rng: OpeningRange,
    snap: MarketSnapshot,
    settings: Settings,
    cutoff: datetime,
) -> list[Reason] | tuple[TradeSetup, SignalEvaluation]:
    cfg = settings.strategy
    fails: list[Reason] = []
    passed: list[Reason] = []
    close, high, low = float(candle["close"]), float(candle["high"]), float(candle["low"])
    start = candle["timestamp"]
    end = (start + snap.interval.timedelta).to_pydatetime()

    passed.append(Reason(code="breakout",
                         message=f"{start:%H:%M} candle closed {close:.2f} above range high "
                                 f"{rng.high:.2f}"))
    if end > cutoff:
        fails.append(Reason(code="after_cutoff",
                            message=f"candle closed {end:%H:%M}, after {cfg.last_signal_time:%H:%M}"))

    rv_detail = relative_volume_detail(frame, snap.session_date, start,
                                       cfg.relative_volume_lookback_sessions,
                                       cfg.relative_volume_mode)
    rv, rv_n = rv_detail.ratio, rv_detail.sessions
    if rv is None or rv_n < cfg.relative_volume_min_sessions:
        fails.append(Reason(code="relative_volume_unavailable",
                            message=f"relative volume baseline has {rv_n} sessions, "
                                    f"need {cfg.relative_volume_min_sessions}"))
    elif rv < cfg.relative_volume_threshold:
        fails.append(Reason(code="low_relative_volume",
                            message=f"relative volume {rv:.2f} < {cfg.relative_volume_threshold}"))
    else:
        passed.append(Reason(code="relative_volume",
                             message=f"relative volume {rv:.2f} ({cfg.relative_volume_mode.value}, "
                                     f"{rv_n} sessions)"))

    vwap = candle["vwap"]
    if pd.isna(vwap):
        fails.append(Reason(code="vwap_unavailable", message="VWAP unavailable"))
    elif close <= vwap:
        fails.append(Reason(code="below_vwap", message=f"close {close:.2f} <= VWAP {vwap:.2f}"))
    else:
        passed.append(Reason(code="above_vwap", message=f"close {close:.2f} > VWAP {vwap:.2f}"))

    ema_value = candle.get("ema") if cfg.use_ema_filter else None
    if cfg.use_ema_filter:
        if pd.isna(ema_value):
            fails.append(Reason(code="ema_unavailable",
                                message=f"EMA{cfg.ema_period} unavailable (insufficient candles)"))
        elif close <= ema_value:
            fails.append(Reason(code="below_ema",
                                message=f"close {close:.2f} <= EMA{cfg.ema_period} {ema_value:.2f}"))
        else:
            passed.append(Reason(code="above_ema",
                                 message=f"close {close:.2f} > EMA{cfg.ema_period} {ema_value:.2f}"))

    extension = (close - rng.high) / rng.high * 100
    if extension > cfg.max_extension_pct:
        fails.append(Reason(code="overextended",
                            message=f"close is {extension:.2f}% above range high "
                                    f"(max {cfg.max_extension_pct}%)"))

    stop, stop_rule = _stop(cfg.stop_reference, rng, low)
    risk = close - stop
    risk_pct = risk / close * 100
    if risk <= 0:
        fails.append(Reason(code="invalid_stop",
                            message=f"stop {stop:.2f} is not below entry {close:.2f}"))
    elif risk_pct < cfg.min_risk_pct:
        fails.append(Reason(code="stop_too_tight",
                            message=f"risk {risk_pct:.2f}% of entry < {cfg.min_risk_pct}%"))
    elif risk_pct > cfg.max_risk_pct:
        fails.append(Reason(code="stop_too_wide",
                            message=f"risk {risk_pct:.2f}% of entry > {cfg.max_risk_pct}%"))

    if fails:
        return fails

    # Round the target up to the paisa so planned reward/risk is never below
    # the configured minimum.
    target = math.ceil((close + cfg.min_reward_risk * risk) * 100 - 1e-6) / 100
    exit_by = datetime.combine(snap.session_date, cfg.eod_exit_time, tzinfo=end.tzinfo)
    setup = TradeSetup(
        symbol=snap.instrument.symbol,
        exchange=snap.instrument.exchange,
        signal_candle_start=start.to_pydatetime(),
        signal_time=end,
        valid_until=end + timedelta(minutes=cfg.signal_valid_minutes),
        entry_trigger=(f"{snap.interval.label} candle closed above opening-range high "
                       f"{rng.high:.2f}; review only after {end:%H:%M} and while price holds "
                       f"above {stop:.2f}"),
        entry_reference=close,
        entry_basis=("signal candle close, known at candle end; backtests fill at the next "
                     "available price, not this close"),
        stop_reference=stop,
        stop_rule=stop_rule,
        target_reference=target,
        target_rule=f"entry + {cfg.min_reward_risk:g} x risk per share (before costs)",
        risk_per_share=round(risk, 4),
        planned_reward_risk=round((target - close) / risk, 4),
        exit_by=exit_by,
    )
    passed.append(Reason(code="valid_stop",
                         message=f"stop {stop:.2f} ({stop_rule}), risk {risk:.2f}/share "
                                 f"= {risk_pct:.2f}%"))
    candle_range = high - low
    evaluation = SignalEvaluation(
        status=SignalStatus.QUALIFIED_PRE_COST,
        indicators=Indicators(
            as_of_candle=start.to_pydatetime(),
            close=close,
            vwap=float(vwap),
            ema=None if ema_value is None or pd.isna(ema_value) else float(ema_value),
            opening_range_high=rng.high,
            opening_range_low=rng.low,
            opening_range_width_pct=rng.width_pct,
            relative_volume=rv,
            relative_volume_sessions=rv_n,
            relative_volume_today=rv_detail.today_volume,
            relative_volume_baseline=rv_detail.baseline_volume,
        ),
        setup=setup,
        qualification=passed,
        close_location=(close - low) / candle_range if candle_range > 0 else 1.0,
        extension_pct=extension,
        vwap_distance_pct=(close - vwap) / vwap * 100,
        ema_distance_pct=None if ema_value is None or pd.isna(ema_value)
        else (close - ema_value) / ema_value * 100,
    )
    return setup, evaluation


def _track_signal(
    result: tuple[TradeSetup, SignalEvaluation],
    candle: pd.Series,
    after: pd.DataFrame,
    snap: MarketSnapshot,
) -> SignalEvaluation:
    """Follow completed candles after the signal to see if it still stands."""
    setup, evaluation = result
    later = after[after["timestamp"] > candle["timestamp"]]
    stop_hit = later[later["low"] <= setup.stop_reference]
    target_hit = later[later["high"] >= setup.target_reference]
    if not stop_hit.empty:
        evaluation.status = SignalStatus.INVALIDATED
        evaluation.rejections.append(Reason(
            code="stop_traded",
            message=f"price traded at/below stop {setup.stop_reference:.2f} at "
                    f"{stop_hit['timestamp'].iloc[0]:%H:%M}"))
    elif not target_hit.empty:
        evaluation.status = SignalStatus.INVALIDATED
        evaluation.rejections.append(Reason(
            code="target_traded",
            message=f"target {setup.target_reference:.2f} already reached at "
                    f"{target_hit['timestamp'].iloc[0]:%H:%M}"))
    elif snap.as_of > setup.valid_until:
        evaluation.status = SignalStatus.EXPIRED
        evaluation.rejections.append(Reason(
            code="expired",
            message=f"signal valid until {setup.valid_until:%H:%M}; now {snap.as_of:%H:%M}"))
    return evaluation


def _stop(reference: StopReference, rng: OpeningRange, candle_low: float) -> tuple[float, str]:
    if reference is StopReference.SIGNAL_CANDLE_LOW:
        return candle_low, "low of the signal candle"
    if reference is StopReference.OPENING_RANGE_LOW:
        return rng.low, "opening-range low"
    return round((rng.high + rng.low) / 2, 2), "opening-range midpoint"


def _indicators(today: pd.DataFrame, rng: OpeningRange | None) -> Indicators:
    if today.empty:
        return Indicators(
            opening_range_high=rng.high if rng else None,
            opening_range_low=rng.low if rng else None,
        )
    last = today.iloc[-1]
    return Indicators(
        as_of_candle=last["timestamp"].to_pydatetime(),
        close=float(last["close"]),
        vwap=None if pd.isna(last["vwap"]) else float(last["vwap"]),
        ema=None if "ema" not in last or pd.isna(last["ema"]) else float(last["ema"]),
        opening_range_high=rng.high if rng else None,
        opening_range_low=rng.low if rng else None,
        opening_range_width_pct=rng.width_pct if rng else None,
    )
