"""Synthetic Phase 7 fixtures. ALL DATA IS INVENTED to test implementation
logic. Nothing here is evidence about the strategy or the market.

``write_csv_dataset`` builds a CSV data folder for one symbol (SYNTH) over 25
weekdays in Feb-Mar 2025 (no holidays in the test calendar file):
  - days 0-4: flat warm-up sessions (price 70, 1000 shares per candle)
  - days 5-24: an ORB breakout (09:35 candle, volume 20000) on every other
    day, alternating a target exit and a stop exit; flat sessions in between.
With relative-volume and liquidity lookbacks of 5 sessions the warm-up is 5
sessions, leaving 20 tradable sessions.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from app.config import MarketConfig
from app.robustness.baseline import freeze
from app.utils.market_calendar import MarketCalendar
from tests.backtest_helpers import bt_settings, flat_day, pad
from tests.helpers import bars, orb_today

NEUTRAL = (71.05, 71.3, 70.9, 71.2, 1000)
TARGET = (71.2, 72.3, 71.1, 72.1, 1000)
STOP = (71.2, 71.3, 70.3, 70.5, 1000)
PROTOCOL = Path(__file__).resolve().parent.parent / "config" / "robustness" / "protocol.yaml"


def e2e_days() -> list[date]:
    cal = MarketCalendar(MarketConfig(), holidays={})
    return cal.trading_days(date(2025, 2, 3), date(2025, 3, 7))


def session_rows(i: int) -> list[tuple]:
    if i < 5 or i % 2 == 0:
        return flat_day()
    after = [NEUTRAL, TARGET] if (i // 2) % 2 == 0 else [NEUTRAL, STOP]
    return pad(orb_today(breakout_volume=20000, after=after))


def write_csv_dataset(folder: Path, symbols: tuple[str, ...] = ("SYNTH",),
                      rows_for=session_rows) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for sym in symbols:
        frame = pd.concat([bars(d, rows_for(i)) for i, d in enumerate(e2e_days())],
                          ignore_index=True)
        frame["timestamp"] = frame["timestamp"].map(lambda t: t.isoformat())
        frame.to_csv(folder / f"{sym}_5m.csv", index=False)
    pd.DataFrame({"symbol": list(symbols), "name": [f"{s} synthetic" for s in symbols]}) \
        .to_csv(folder / "instruments.csv", index=False)
    return folder


def e2e_settings(tmp_path: Path, **over):
    holidays = tmp_path / "holidays.yaml"
    holidays.write_text("[]\n")
    base = {
        "market": {"holidays_file": str(holidays)},
        "strategy": {"relative_volume_lookback_sessions": 5, "relative_volume_min_sessions": 5,
                     "relative_volume_threshold": 1.2},
        "screening": {"liquidity_lookback_sessions": 5},
        "risk": {"max_trades_per_day": 5},
    }
    for k, v in over.items():
        base[k] = {**base.get(k, {}), **v}
    return bt_settings(**base)


def small_protocol(tmp_path: Path) -> Path:
    """The real predeclared protocol with fewer bootstrap resamples (speed)."""
    text = PROTOCOL.read_text().replace("bootstrap_resamples: 5000", "bootstrap_resamples: 300")
    path = tmp_path / "protocol.yaml"
    path.write_text(text)
    return path


def frozen_baseline(tmp_path: Path, name: str = "test-baseline", **over) -> Path:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return freeze(name, e2e_settings(tmp_path, **over), small_protocol(tmp_path),
                  tmp_path / "baselines", datetime(2025, 1, 1, tzinfo=ZoneInfo("Asia/Kolkata")))
