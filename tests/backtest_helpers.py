"""Synthetic backtest datasets with hand-calculable outcomes. All data invented.

Base scenario on DAY (2025-03-12), built on helpers.orb_today:
  opening range 09:15-09:30: high 70.50, low 69.50
  09:35 candle closes 71.00 (low 70.40) on volume 5000 -> signal at 09:40
  stop 70.40 (signal candle low), target 72.20 (2R)
  the entry fills at the 09:40 candle's OPEN (given per test in `after`)
Prior sessions: 20 flat days at 70.00, 1000 shares per candle
(traded value 75 x 1000 x 70 = Rs 52.5 lakh/day, above the Rs 50 lakh minimum).
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from app.backtesting.dataset import build_dataset
from app.config import Settings
from app.data.models import Instrument
from app.scan.snapshot import lookback_start
from tests.helpers import DAY, bars, orb_today

SESSION_CANDLES = 75


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def bt_settings(**over) -> Settings:
    base = {
        "capital": {"initial_capital": 2000},
        "risk": {"risk_pct": 10, "include_costs_in_risk": False, "max_daily_loss": 1000,
                 "max_aggregate_risk": 1000, "max_trades_per_day": 5},
        "screening": {"require_spread": False},
        "cost_review": {"apply_current_rates_to_past_dates": True, "min_net_reward_risk": 0.01},
    }
    return Settings(**deep_merge(base, over))


def pad(rows: list[tuple], n: int = SESSION_CANDLES) -> list[tuple]:
    """Extend to a full session with quiet candles at the last close."""
    c = rows[-1][3]
    return rows + [(c, c + 0.05, c - 0.05, c, 1000)] * (n - len(rows))


def flat_day(price: float = 70.0) -> list[tuple]:
    return [(price, price + 0.1, price - 0.1, price, 1000)] * SESSION_CANDLES


def scenario(after: list[tuple], **orb) -> list[tuple]:
    return pad(orb_today(after=after, **orb))


def make_dataset(calendar, settings: Settings, days: dict[date, list[tuple]] | None = None,
                 symbol: str = "SYNTH", prior: int = 20, extra: dict | None = None):
    """`days` maps session -> rows for one symbol; `extra` maps more symbols to
    their own `days` dicts. Prior flat sessions are added before the earliest day."""
    def frames_for(sym_days: dict[date, list[tuple]]) -> pd.DataFrame:
        first = min(sym_days)
        prior_days = calendar.trading_days(lookback_start(calendar, first, prior), first)[:-1]
        parts = [bars(d, flat_day()) for d in prior_days if d not in sym_days]
        parts += [bars(d, rows) for d, rows in sorted(sym_days.items())]
        return pd.concat(parts, ignore_index=True).sort_values("timestamp", kind="stable")

    all_days = {symbol: days or {DAY: scenario([])}}
    all_days.update(extra or {})
    frames = {sym: (Instrument(sym, f"{sym} Ltd"), frames_for(d).reset_index(drop=True))
              for sym, d in all_days.items()}
    return build_dataset(frames, settings, calendar, source="synthetic")
