"""Phase 8 test fakes. Everything here is invented; no real provider, broker
or account is contacted.

- ``fake_growwapi_module``: a stand-in for the ``growwapi`` package. Its
  ``GrowwAPI`` serves 5-minute candles in Groww's observed format (epoch
  seconds, CUMULATIVE session volume) and daily candles from a synthetic
  dataset. Its ``place_order`` / ``modify_order`` / ``cancel_order`` /
  holdings / positions methods RECORD any call, so tests can prove they are
  never reached.
- ``FlakyProvider``: wraps a provider and makes ``get_candles`` /
  ``get_quote`` fail while ``failing`` is True (simulated outage).
"""

from __future__ import annotations

import types
from datetime import datetime

import pandas as pd

from app.data.models import DataUnavailableError

ORDER_METHODS = ("place_order", "modify_order", "cancel_order", "get_holdings_for_user",
                 "get_positions_for_user", "get_order_list", "place_smart_order")


def _groww_rows(frame: pd.DataFrame, start: datetime, end: datetime, daily: bool) -> list:
    ts = frame["timestamp"]
    sel = frame[(ts >= pd.Timestamp(start)) & (ts <= pd.Timestamp(end))] if not daily else \
        frame[(ts.dt.date >= start.date()) & (ts.dt.date <= end.date())]
    rows, cum, last_day = [], 0, None
    for _, r in sel.iterrows():
        t = r["timestamp"]
        if daily:
            epoch = int(pd.Timestamp(t.date()).tz_localize(t.tz).timestamp())  # 00:00 IST
            vol = int(r["volume"])
        else:
            epoch = int(t.timestamp())
            if t.date() != last_day:
                cum, last_day = 0, t.date()
            cum += int(r["volume"])
            vol = cum  # Groww intraday volume is cumulative within the session
        rows.append([epoch, float(r["open"]), float(r["high"]), float(r["low"]),
                     float(r["close"]), vol])
    return rows


def fake_growwapi_module(dataset, login_error: Exception | None = None):
    """Returns (module, record). ``record['order_calls']`` lists every
    attempted call to an order/account method."""
    record = {"order_calls": [], "logins": 0, "timeouts": []}

    class GrowwAPI:
        EXCHANGE_NSE, SEGMENT_CASH = "NSE", "CASH"

        def __init__(self, token):
            self.token = token

        @staticmethod
        def get_access_token(api_key, totp=None, secret=None):
            record["logins"] += 1
            if login_error is not None:
                raise login_error
            return "fake-session-token"

        def get_quote(self, trading_symbol, exchange, segment, timeout=None):
            record["timeouts"].append(timeout)
            # No trade time: the adapter rejects it and the engine uses candles.
            return {"last_price": 71.0, "last_trade_time": None}

        def get_historical_candle_data(self, trading_symbol, exchange, segment, start_time,
                                       end_time, interval_in_minutes, timeout=None):
            record["timeouts"].append(timeout)
            tz = dataset.symbols[trading_symbol].intraday["timestamp"].dt.tz
            start = pd.Timestamp(start_time).tz_localize(tz)
            end = pd.Timestamp(end_time).tz_localize(tz)
            data = dataset.symbols[trading_symbol]
            daily = interval_in_minutes == 1440
            return {"candles": _groww_rows(data.daily if daily else data.intraday, start, end,
                                           daily)}

    def _recorder(name):
        def method(self, *a, **k):
            record["order_calls"].append(name)
            return {"status": "SHOULD NEVER HAPPEN"}
        return method

    for name in ORDER_METHODS:
        setattr(GrowwAPI, name, _recorder(name))
    return types.SimpleNamespace(GrowwAPI=GrowwAPI), record


class FlakyProvider:
    """Delegates to ``base``; while ``failing`` is True every data call raises."""

    def __init__(self, base, error: str = "simulated outage: connection refused"):
        self.base = base
        self.failing = False
        self.error = error
        self.calls_failed = 0

    @property
    def name(self):
        return self.base.name

    @property
    def mode(self):
        return self.base.mode

    @property
    def last_raw(self):
        return getattr(self.base, "last_raw", {})

    def list_instruments(self):
        return self.base.list_instruments()

    def _check(self):
        if self.failing:
            self.calls_failed += 1
            raise DataUnavailableError(self.error)

    def get_candles(self, *a, **k):
        self._check()
        return self.base.get_candles(*a, **k)

    def get_quote(self, *a, **k):
        self._check()
        return self.base.get_quote(*a, **k)

    def health(self):
        return {"state": "DISCONNECTED" if self.failing else "CONNECTED",
                "last_error": self.error if self.failing else None}
