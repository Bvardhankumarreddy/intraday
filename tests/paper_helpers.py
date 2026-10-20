"""Paper-engine fixtures built on the Phase 4 synthetic scenarios (all invented).

Base expectations (see tests/backtest_helpers.py and test_backtesting.py):
signal 09:40 for SYNTH, entry ref 71.00, stop 70.40, target 72.20, qty 28 on
Rs 2,000; candle fill at the 09:40 open 71.05; target exit 72.20 -> net 15.69.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.data.memory_provider import InMemoryProvider
from app.data.models import Quote
from app.paper.engine import PaperEngine
from app.paper.scheduler import step_times
from app.paper.store import PaperStore
from tests.backtest_helpers import bt_settings, make_dataset, scenario

NEUTRAL = (71.05, 71.3, 70.9, 71.2, 1000)
TARGET = (71.2, 72.3, 71.1, 72.1, 1000)
STOP = (71.2, 71.3, 70.3, 70.5, 1000)
GAP_DOWN = (70.0, 70.2, 69.8, 70.1, 1000)
BOTH = (71.2, 72.3, 70.3, 71.0, 1000)


def paper_settings(tmp_path, **over):
    base = {"paper": {"lock_file": str(tmp_path / "paper.lock"), "grace_seconds": 1}}
    for k, v in over.items():
        base[k] = {**base.get(k, {}), **v}
    return bt_settings(**base)


def build(calendar, tmp_path, after=None, days=None, extra=None, quotes=None, db=None, **over):
    s = paper_settings(tmp_path, **over)
    ds = make_dataset(calendar, s, days or {date(2025, 3, 12): scenario(after or [])},
                      extra=extra)
    provider = ds.provider(calendar)
    provider.quotes = quotes or {}
    store = PaperStore(db or (tmp_path / "paper.db"))
    return PaperEngine(provider, s, calendar, store), provider, store


def steps(engine: PaperEngine, day: date, until: str | None = None) -> list[datetime]:
    """Step times for the session (optionally up to HH:MM inclusive)."""
    times = step_times(engine.calendar, day, engine.step_td,
                       timedelta(seconds=engine.settings.paper.grace_seconds))
    if until:
        h, m = map(int, until.split(":"))
        times = [t for t in times if (t.hour, t.minute) <= (h, m) or
                 (t.hour, t.minute) == (h, m)]
        limit = datetime.combine(day, datetime.min.time().replace(hour=h, minute=m),
                                 tzinfo=engine.calendar.tz) + timedelta(seconds=59)
        times = [t for t in times if t <= limit]
    return times


def run(engine: PaperEngine, day: date, until: str | None = None) -> None:
    open_dt, _ = engine.calendar.session_bounds(day)
    engine.open_session(day, open_dt - timedelta(minutes=15))
    for t in steps(engine, day, until):
        engine.step(t)


def quote(symbol: str, ts: datetime, ask: float, bid: float | None = None) -> Quote:
    from app.data.models import DataMode

    return Quote(symbol=symbol, last_price=ask, timestamp=ts, mode=DataMode.SIMULATED,
                 source="fixture", bid=bid if bid is not None else ask - 0.05, ask=ask,
                 bid_qty=500, ask_qty=500)


__all__ = ["InMemoryProvider", "PaperStore"]
