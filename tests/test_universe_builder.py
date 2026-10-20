"""Universe builder: fixed rules, data up to the build date only, no network."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from app.config import Settings
from app.data.factory import load_universe
from app.data.models import DataUnavailableError
from app.data.universe_builder import build, evaluate, static_candidates, write

TODAY = date(2026, 10, 21)


def instruments() -> pd.DataFrame:
    base = {"exchange": "NSE", "segment": "CASH", "series": "EQ", "instrument_type": "EQ",
            "is_intraday": 1, "buy_allowed": 1, "sell_allowed": 1, "isin": "X"}
    rows = [{**base, "trading_symbol": s, "name": f"{s} Ltd"}
            for s in ("CHEAPLIQ", "MIDLIQ", "THIN", "PRICEY", "NEW", "BROKEN")]
    rows += [{**base, "trading_symbol": "SMEONE", "name": "x", "series": "SM"},
             {**base, "trading_symbol": "NIFTYBEES", "name": "Nippon India Nifty"},
             {**base, "trading_symbol": "ABCBANKETF", "name": "Abc Bank Etf"},
             {**base, "trading_symbol": "NOMIS", "name": "x", "is_intraday": 0},
             {**base, "trading_symbol": "OPT", "name": "x", "segment": "FNO"}]
    return pd.DataFrame(rows)


def candles(close: float, volume: float, n: int = 25, end: date = TODAY) -> pd.DataFrame:
    days = [end - timedelta(days=i) for i in range(n, -1, -1)]  # includes TODAY
    return pd.DataFrame({"timestamp": pd.to_datetime(days).tz_localize("Asia/Kolkata"),
                         "open": close, "high": close, "low": close, "close": close,
                         "volume": volume})


class FakeProvider:
    data = {"CHEAPLIQ": candles(60, 200_000), "MIDLIQ": candles(95, 80_000),
            "THIN": candles(70, 1_000), "PRICEY": candles(450, 500_000),
            "NEW": candles(70, 500_000, n=5)}

    def __init__(self, inst):
        self.inst, self.calls = inst, []

    def get_candles(self, sym, interval, start, end):
        assert interval == "1d" and end < TODAY  # never asks for the build day itself
        self.calls.append(sym)
        if sym not in self.data:
            raise DataUnavailableError("no candles")
        f = self.data[sym]
        return type("S", (), {"frame": f[f["timestamp"].dt.date <= end]})()


def test_static_filters_keep_only_intraday_nse_equities():
    got = static_candidates(instruments())["trading_symbol"].tolist()
    assert got == sorted(["CHEAPLIQ", "MIDLIQ", "THIN", "PRICEY", "NEW", "BROKEN"])


def test_selection_rules_and_ranking(calendar):
    s = Settings()
    r = build(s, calendar, FakeProvider, instruments(), top=50, today=TODAY, progress=None)
    assert [x["symbol"] for x in r["selected"]] == ["CHEAPLIQ", "MIDLIQ"]  # by traded value
    reasons = {x["symbol"]: x["reason"] for x in r["all"]}
    assert "avg traded value" in reasons["THIN"] and "outside" in reasons["PRICEY"]
    assert "only" in reasons["NEW"] and "no usable data" in reasons["BROKEN"]
    assert r["counts"] == {"static_candidates": 6, "data_errors": 1, "passed": 2, "selected": 2}
    top1 = build(s, calendar, FakeProvider, instruments(), top=1, today=TODAY, progress=None)
    assert [x["symbol"] for x in top1["selected"]] == ["CHEAPLIQ"]


def test_build_day_candle_is_never_used():
    f = candles(60, 200_000)
    f.loc[f.index[-1], "close"] = 500  # a candle on the build day itself
    assert evaluate("X", f, Settings(), TODAY)["last_close"] == 60


def test_written_file_loads_and_records_rules(tmp_path, calendar):
    r = build(Settings(), calendar, FakeProvider, instruments(), top=50, today=TODAY,
              progress=None)
    path = tmp_path / "universe.csv"
    report = write(r, path, tmp_path / "reports")
    assert [i.symbol for i in load_universe(path)] == ["CHEAPLIQ", "MIDLIQ"]
    text = path.read_text()
    assert text.startswith("# Built") and "Survivorship" in text and "Rules:" in text
    assert report.exists()


def test_circuit_open_stops_without_writing(calendar):
    class Down(FakeProvider):
        def get_candles(self, *a, **k):
            raise DataUnavailableError("Groww candles X not attempted: circuit OPEN after 5")

    with pytest.raises(RuntimeError, match="without writing"):
        build(Settings(), calendar, Down, instruments(), top=50, today=TODAY, progress=None)
