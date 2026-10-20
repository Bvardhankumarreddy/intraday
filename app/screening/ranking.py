"""Transparent 0-100 score for candidates that passed every mandatory rule.

The score only orders QUALIFIED_PRE_COST candidates. It never rescues a
rejected one, and it is not a probability of profit.

Each component is normalised to 0..1 and multiplied by its weight:

- liquidity: log-scaled, 0 at the screening minimum, 1 at
  ``liquidity_full_score_multiple`` x the minimum.
- relative volume: linear, 0 at the threshold, 1 at ``relative_volume_full_score``.
- breakout quality: mean of (a) where the signal candle closed in its range
  (1 = at the high) and (b) 1 - extension / max extension.
- trend: close distance above VWAP, linear to 1 at ``trend_full_score_pct``;
  averaged with the same measure for EMA when the EMA filter is enabled.
- spread: 1 - spread / max spread.

Missing inputs score 0 for that component (never a neutral or favourable
value) and are flagged in the component note.

Ties are broken by higher average traded value, then symbol (A-Z), so the
order is deterministic.
"""

from __future__ import annotations

import math

from app.config import Settings
from app.scan.models import Candidate, ScoreComponent, ScreenResult, SignalStatus
from app.strategies.opening_range_breakout import SignalEvaluation


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_candidate(
    screen: ScreenResult, ev: SignalEvaluation, settings: Settings
) -> tuple[float, list[ScoreComponent]]:
    w, s = settings.scoring, settings.strategy
    parts: list[ScoreComponent] = []

    def add(name: str, weight: float, value: float | None, note: str) -> None:
        points = 0.0 if value is None else weight * value
        parts.append(ScoreComponent(name=name, weight=weight, normalized=value,
                                    points=round(points, 2), note=note))

    atv, floor = screen.avg_traded_value, settings.screening.min_avg_traded_value
    if atv is None or atv <= 0:
        add("liquidity", w.weight_liquidity, None, "traded value unavailable; scored 0")
    else:
        base = floor if floor > 0 else 1e5
        value = _clip(math.log(atv / base) / math.log(w.liquidity_full_score_multiple))
        add("liquidity", w.weight_liquidity, value,
            f"Rs {atv / 1e5:,.1f} lakh avg traded value")

    rv = ev.indicators.relative_volume
    if rv is None:
        add("relative_volume", w.weight_relative_volume, None, "unavailable; scored 0")
    else:
        span = w.relative_volume_full_score - s.relative_volume_threshold
        value = 1.0 if span <= 0 else _clip((rv - s.relative_volume_threshold) / span)
        add("relative_volume", w.weight_relative_volume, value, f"relative volume {rv:.2f}")

    if ev.close_location is None or ev.extension_pct is None:
        add("breakout_quality", w.weight_breakout_quality, None, "unavailable; scored 0")
    else:
        value = (ev.close_location + _clip(1 - ev.extension_pct / s.max_extension_pct)) / 2
        add("breakout_quality", w.weight_breakout_quality, value,
            f"closed at {ev.close_location:.0%} of candle range, "
            f"{ev.extension_pct:.2f}% above range high")

    if ev.vwap_distance_pct is None:
        add("trend", w.weight_trend, None, "VWAP distance unavailable; scored 0")
    else:
        parts_t = [_clip(ev.vwap_distance_pct / w.trend_full_score_pct)]
        note = f"{ev.vwap_distance_pct:.2f}% above VWAP"
        if s.use_ema_filter:
            ema_part = (0.0 if ev.ema_distance_pct is None
                        else _clip(ev.ema_distance_pct / w.trend_full_score_pct))
            parts_t.append(ema_part)
            note += (", EMA unavailable" if ev.ema_distance_pct is None
                     else f", {ev.ema_distance_pct:.2f}% above EMA{s.ema_period}")
        add("trend", w.weight_trend, sum(parts_t) / len(parts_t), note)

    spread = screen.spread_pct
    if spread is None:
        add("spread", w.weight_spread, None, "spread unavailable; scored 0")
    else:
        value = _clip(1 - spread / settings.screening.max_spread_pct)
        add("spread", w.weight_spread, value, f"spread {spread:.3f}%")

    return round(sum(p.points for p in parts), 2), parts


def rank(candidates: list[Candidate]) -> list[Candidate]:
    """QUALIFIED_PRE_COST candidates, best first, deterministic order."""
    qualified = [c for c in candidates
                 if c.signal_status is SignalStatus.QUALIFIED_PRE_COST and c.score is not None]
    return sorted(
        qualified,
        key=lambda c: (-c.score, -(c.screen.avg_traded_value or 0.0), c.symbol),
    )
