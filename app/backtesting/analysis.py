"""Chronological validation: period splits, walk-forward and sensitivity.

- Periods are contiguous, ordered date ranges (development < validation <
  test) and must not overlap. Nothing is shuffled. Each period is simulated
  separately from fresh capital; earlier sessions are visible only as
  indicator/liquidity lookback history, never as trading days.
- Sensitivity varies ONE parameter at a time around the base settings and
  reports each run's metrics. It does not pick a winner.
- Walk-forward: for each fold, candidate parameter sets are compared on the
  training window by expectancy, but only among sets with at least
  ``min_trades`` trades (otherwise the base settings are kept). The chosen
  set is then run once on the following unseen test window. The test
  window's results are what count.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.backtesting.dataset import HistoricalDataset
from app.backtesting.engine import BacktestResult, run_backtest
from app.backtesting.metrics import compute_metrics
from app.config import Settings, settings_from_values
from app.utils.market_calendar import MarketCalendar


@dataclass(frozen=True)
class Period:
    name: str
    start: date
    end: date


def validate_periods(periods: list[Period]) -> None:
    for p in periods:
        if p.start > p.end:
            raise ValueError(f"{p.name}: start after end")
    for a, b in zip(periods, periods[1:]):
        if b.start <= a.end:
            raise ValueError(f"{b.name} must start after {a.name} ends (no overlap, "
                             "chronological order)")


def run_periods(dataset: HistoricalDataset, settings: Settings, calendar: MarketCalendar,
                periods: list[Period]) -> dict[str, tuple[BacktestResult, dict]]:
    validate_periods(periods)
    out = {}
    for p in periods:
        result = run_backtest(dataset, settings, calendar, p.start, p.end)
        out[p.name] = (result, compute_metrics(result, settings.backtest.min_trades_for_evidence))
    return out


def _with(settings: Settings, section: str, key: str, value) -> Settings:
    # Explicit values only: an NSE_* environment variable must not override
    # the grid value (Settings(**data) would let it).
    data = settings.model_dump()
    data[section][key] = value
    return settings_from_values(data)


SENSITIVITY_GRID: list[tuple[str, str, list]] = [
    ("strategy", "opening_range_minutes", [15, 30]),
    ("strategy", "relative_volume_threshold", [1.2, 1.5, 2.0]),
    ("strategy", "stop_reference", ["signal_candle_low", "opening_range_low",
                                    "opening_range_mid"]),
    ("strategy", "min_reward_risk", [1.5, 2.0, 3.0]),
    ("execution", "slippage_pct_per_side", [0.02, 0.05, 0.10]),
    ("execution", "assumed_spread_pct", [0.05, 0.10, 0.20]),
]


def sensitivity(dataset: HistoricalDataset, settings: Settings, calendar: MarketCalendar,
                start: date, end: date, grid=None) -> list[dict]:
    rows = []
    for section, key, values in grid or SENSITIVITY_GRID:
        for value in values:
            s = _with(settings, section, key, value)
            m = compute_metrics(run_backtest(dataset, s, calendar, start, end),
                                s.backtest.min_trades_for_evidence)
            rows.append({"parameter": f"{section}.{key}", "value": value,
                         "is_base": getattr(getattr(settings, section), key) == value, **m})
    return rows


def walk_forward(dataset: HistoricalDataset, settings: Settings, calendar: MarketCalendar,
                 train_sessions: int, test_sessions: int, grid=None) -> list[dict]:
    days = dataset.sessions()
    min_trades = settings.backtest.min_trades_for_evidence
    candidates = [("base", settings)] + [
        (f"{sec}.{key}={v}", _with(settings, sec, key, v))
        for sec, key, values in (grid or SENSITIVITY_GRID) for v in values
    ]
    folds, i = [], 0
    while i + train_sessions + test_sessions <= len(days):
        train = (days[i], days[i + train_sessions - 1])
        test = (days[i + train_sessions], days[i + train_sessions + test_sessions - 1])
        scored = []
        for label, s in candidates:
            m = compute_metrics(run_backtest(dataset, s, calendar, *train), min_trades)
            if m["trades"] >= min_trades and m["expectancy_per_trade"] is not None:
                scored.append((m["expectancy_per_trade"], label, s))
        if scored:
            _, label, chosen = max(scored, key=lambda x: (x[0], x[1]))
        else:
            label, chosen = "base (no candidate met the minimum trade count)", settings
        test_metrics = compute_metrics(run_backtest(dataset, chosen, calendar, *test), min_trades)
        folds.append({"train_start": train[0], "train_end": train[1], "test_start": test[0],
                      "test_end": test[1], "selected": label, **test_metrics})
        i += test_sessions
    return folds
