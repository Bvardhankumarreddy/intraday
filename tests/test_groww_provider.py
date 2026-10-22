"""Groww adapter against fixtures in the documented response formats.

These tests do NOT prove the adapter works with a live Groww account; they
verify retries, parsing, validation and safety with recorded-format data.
"""

import inspect
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.data.groww_provider import (
    ConnectionState,
    GrowwMarketDataProvider,
    GrowwSdkClient,
    RetryPolicy,
)
from app.data.models import DataMode, DataQualityError, DataUnavailableError, Instrument

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2025, 3, 12, 9, 40, 2, tzinfo=IST)
EPOCH_0915 = int(datetime(2025, 3, 12, 9, 15, tzinfo=IST).timestamp())


class FakeClient:
    def __init__(self, quote=None, candles=None, failures=None):
        self._quote = quote
        self._candles = candles or []
        self.failures = list(failures or [])
        self.calls = []

    def _maybe_fail(self):
        if self.failures:
            raise self.failures.pop(0)

    def quote(self, trading_symbol):
        self.calls.append(("quote", trading_symbol))
        self._maybe_fail()
        return self._quote

    def candles(self, trading_symbol, start, end, interval_minutes):
        self.calls.append(("candles", trading_symbol, start, end, interval_minutes))
        self._maybe_fail()
        return {"candles": self._candles}


def make(client, calendar, sleeps=None, **data):
    # Jitter off so backoff delays are exact (production default: +/-25%).
    s = Settings(data={"min_seconds_between_calls": 0, "retry_jitter_fraction": 0, **data})
    return GrowwMarketDataProvider(client, calendar, [Instrument("ABC", "ABC Ltd")], s,
                                   sleep=(sleeps.append if sleeps is not None else
                                          lambda _: None),
                                   clock=lambda: NOW)


def documented_quote(ms_ago=1000, unit="s"):
    """Observed live format: last_trade_time in epoch SECONDS (docs say ms)."""
    ts = (int(NOW.timestamp() * 1000) - ms_ago) / (1000 if unit == "s" else 1)
    return {"last_price": 71.1, "bid_price": 71.05, "bid_quantity": 300, "offer_price": 71.15,
            "offer_quantity": 250, "last_trade_time": ts,
            "volume": 120000, "ohlc": {"open": 70, "high": 71.3, "low": 69.8, "close": 70.2}}


def candles(n=4):
    """Observed live format: volume is CUMULATIVE within the session.
    Per-candle volumes 1000, 1001, 1002, 1003 -> cumulative 1000, 2001, 3003, 4006."""
    cum, rows = 0, []
    for i in range(n):
        cum += 1000 + i
        rows.append([EPOCH_0915 + 300 * i, 70 + i * 0.1, 70.3 + i * 0.1, 69.9 + i * 0.1,
                     70.1 + i * 0.1, cum])
    return rows


def test_quote_parsing_and_timestamps(calendar):
    p = make(FakeClient(quote=documented_quote()), calendar)
    q = p.get_quote("ABC")
    assert (q.bid, q.ask, q.bid_qty, q.ask_qty) == (71.05, 71.15, 300, 250)
    assert q.mode is DataMode.LIVE and q.source == "groww"
    assert q.timestamp == datetime(2025, 3, 12, 9, 40, 1, tzinfo=IST)  # exchange time
    assert q.received_at == NOW                                         # local receipt time
    assert p.state is ConnectionState.CONNECTED


def test_quote_timestamp_unit_detected(calendar):
    for unit in ("s", "ms"):
        q = make(FakeClient(quote=documented_quote(unit=unit)), calendar).get_quote("ABC")
        assert q.timestamp == datetime(2025, 3, 12, 9, 40, 1, tzinfo=IST)


def test_cumulative_volume_decrease_rejected(calendar):
    rows = candles()
    rows[2][5] = rows[1][5] - 1  # cumulative volume going down breaks the assumption
    with pytest.raises(DataQualityError, match="cumulative volume decreased"):
        make(FakeClient(candles=rows), calendar).get_candles("ABC", "5m", date(2025, 3, 12),
                                                             date(2025, 3, 12))


def test_bid_ask_from_depth_when_top_fields_null(calendar):
    # Observed live 2026-10-22: bid_price/offer_price null, depth populated.
    raw = documented_quote() | {"bid_price": None, "bid_quantity": None, "offer_price": None,
                                "offer_quantity": None,
                                "depth": {"buy": [{"price": 71.05, "quantity": 1741},
                                                  {"price": 71.0, "quantity": 9}],
                                          "sell": [{"price": 71.15, "quantity": 312}]}}
    q = make(FakeClient(quote=raw), calendar).get_quote("ABC")
    assert (q.bid, q.ask, q.bid_qty, q.ask_qty) == (71.05, 71.15, 1741, 312)
    empty = raw | {"depth": {"buy": [], "sell": []}}
    q = make(FakeClient(quote=empty), calendar).get_quote("ABC")
    assert (q.bid, q.ask) == (None, None)  # still unavailable; nothing estimated


def test_null_cumulative_volume_carried_forward(calendar):
    # Observed live 2026-10-21: a candle with prices but volume None.
    rows = candles()
    rows[2][5] = None
    s = make(FakeClient(candles=rows), calendar).get_candles("ABC", "5m", date(2025, 3, 12),
                                                             date(2025, 3, 12))
    assert s.frame["volume"].tolist() == [1000, 1001, 0, 2005]  # session total unchanged
    assert s.frame["close"].iloc[2] == pytest.approx(70.3)       # prices kept
    assert any("null cumulative volume" in w for w in s.warnings)
    rows = candles()
    rows[0][5] = None                                            # first candle of the session
    s = make(FakeClient(candles=rows), calendar).get_candles("ABC", "5m", date(2025, 3, 12),
                                                             date(2025, 3, 12))
    assert s.frame["volume"].tolist() == [0, 2001, 1002, 1003]


def test_null_price_still_rejected(calendar):
    rows = candles()
    rows[1][4] = None
    with pytest.raises(DataQualityError):
        make(FakeClient(candles=rows), calendar).get_candles("ABC", "5m", date(2025, 3, 12),
                                                             date(2025, 3, 12))


def test_stale_quote_rejected(calendar):
    p = make(FakeClient(quote=documented_quote(ms_ago=600_000)), calendar)  # 10 minutes old
    with pytest.raises(DataQualityError, match="old"):
        p.get_quote("ABC")


def test_crossed_or_incomplete_quote_rejected(calendar):
    crossed = documented_quote() | {"bid_price": 71.3, "offer_price": 71.1}
    with pytest.raises(DataQualityError):
        make(FakeClient(quote=crossed), calendar).get_quote("ABC")
    with pytest.raises(DataQualityError):
        make(FakeClient(quote={"last_price": 70}), calendar).get_quote("ABC")


def test_timeout_then_recovery_with_bounded_backoff(calendar):
    sleeps = []
    client = FakeClient(quote=documented_quote(),
                        failures=[TimeoutError("t1"), ConnectionError("c2")])
    p = make(client, calendar, sleeps)
    assert p.get_quote("ABC").ask == 71.15
    assert sleeps == [0.5, 1.0]                    # base 0.5, doubling
    assert p.state is ConnectionState.DEGRADED     # recovered after retries


def test_gives_up_after_max_attempts(calendar):
    sleeps = []
    client = FakeClient(quote=documented_quote(), failures=[TimeoutError()] * 10)
    p = make(client, calendar, sleeps)
    with pytest.raises(DataUnavailableError, match="after 4 attempt"):
        p.get_quote("ABC")
    assert sleeps == [0.5, 1.0, 2.0] and p.state is ConnectionState.DISCONNECTED
    assert "TimeoutError" in p.last_error


def test_non_transient_error_not_retried(calendar):
    class AuthError(Exception):
        pass

    client = FakeClient(quote=documented_quote(), failures=[AuthError("bad token")])
    p = make(client, calendar, [])
    with pytest.raises(DataUnavailableError):
        p.get_quote("ABC")
    assert len(client.calls) == 1


def test_backoff_is_bounded():
    r = RetryPolicy(max_attempts=10, base_delay=0.5, max_delay=8)
    assert [r.delay(i) for i in range(1, 8)] == [0.5, 1, 2, 4, 8, 8, 8]


def test_candle_parsing(calendar):
    p = make(FakeClient(candles=candles()), calendar)
    series = p.get_candles("ABC", "5m", date(2025, 3, 12), date(2025, 3, 12))
    f = series.frame
    assert f["timestamp"].iloc[0] == datetime(2025, 3, 12, 9, 15, tzinfo=IST)
    assert list(f["volume"]) == [1000, 1001, 1002, 1003]
    assert series.mode is DataMode.LIVE


def test_duplicate_and_out_of_order_candles_rejected(calendar):
    dup = candles() + [candles()[1]]
    with pytest.raises(DataQualityError, match="duplicate"):
        make(FakeClient(candles=dup), calendar).get_candles("ABC", "5m", date(2025, 3, 12),
                                                            date(2025, 3, 12))
    rows = candles()
    rows[1], rows[2] = rows[2], rows[1]
    with pytest.raises(DataQualityError, match="order"):
        make(FakeClient(candles=rows), calendar).get_candles("ABC", "5m", date(2025, 3, 12),
                                                             date(2025, 3, 12))


def test_requests_split_to_documented_15_day_limit(calendar):
    client = FakeClient(candles=candles())
    p = make(client, calendar)
    with pytest.raises(DataQualityError):  # same fixture rows twice -> duplicates
        p.get_candles("ABC", "5m", date(2025, 1, 1), date(2025, 1, 20))
    spans = [(c[2].date(), c[3].date()) for c in client.calls]
    assert spans == [(date(2025, 1, 1), date(2025, 1, 15)), (date(2025, 1, 16), date(2025, 1, 20))]


def test_no_data_is_unavailable_not_invented(calendar):
    with pytest.raises(DataUnavailableError):
        make(FakeClient(candles=[]), calendar).get_candles("ABC", "5m", date(2025, 3, 12),
                                                           date(2025, 3, 12))


def test_factory_never_falls_back(calendar, monkeypatch):
    from app.data.factory import build_provider

    s = Settings(data={"provider": "groww"})
    with pytest.raises(DataUnavailableError, match="NSE_BROKER_API_KEY"):
        build_provider(s, calendar)


def test_sdk_wrapper_has_no_order_methods():
    source = inspect.getsource(GrowwSdkClient)
    public = [m for m in dir(GrowwSdkClient) if not m.startswith("_")]
    assert public == ["candles", "quote"]
    for word in ("place_order", "modify_order", "cancel_order", "order"):
        assert word not in source.lower().replace("ordered", "")


# -- SDK client authentication (fake SDK; no real credentials) ----------------


class FakeGrowwAPI:
    EXCHANGE_NSE, SEGMENT_CASH = "NSE", "CASH"
    logins: list = []
    fail_next_quote = 0

    def __init__(self, token):
        self.token = token

    @staticmethod
    def get_access_token(api_key, totp=None, secret=None):
        FakeGrowwAPI.logins.append({"api_key": api_key, "totp": totp, "secret": secret})
        return f"token-{len(FakeGrowwAPI.logins)}"

    def get_quote(self, trading_symbol, exchange, segment, timeout=None):
        if FakeGrowwAPI.fail_next_quote:
            FakeGrowwAPI.fail_next_quote -= 1
            raise type("GrowwAPIAuthenticationException", (Exception,), {})("expired")
        return {"token_used": self.token, "symbol": trading_symbol}


@pytest.fixture
def fake_sdk(monkeypatch):
    import sys
    import types

    FakeGrowwAPI.logins = []
    FakeGrowwAPI.fail_next_quote = 0
    monkeypatch.setitem(sys.modules, "growwapi", types.SimpleNamespace(GrowwAPI=FakeGrowwAPI))
    return FakeGrowwAPI


def test_totp_login_uses_generated_code(fake_sdk):
    import pyotp

    secret = pyotp.random_base32()
    client = GrowwSdkClient("totp-token", totp_secret=secret)
    login = fake_sdk.logins[0]
    assert client.auth_method == "totp"
    assert login["api_key"] == "totp-token" and login["secret"] is None
    assert login["totp"].isdigit() and len(login["totp"]) == 6
    assert pyotp.TOTP(secret).verify(login["totp"], valid_window=1)


def test_approval_login_when_no_totp(fake_sdk):
    client = GrowwSdkClient("api-key", api_secret="s3cret")
    assert client.auth_method == "approval"
    assert fake_sdk.logins[0] == {"api_key": "api-key", "totp": None, "secret": "s3cret"}


def test_totp_preferred_when_both_present(fake_sdk):
    import pyotp

    client = GrowwSdkClient("k", api_secret="s", totp_secret=pyotp.random_base32())
    assert client.auth_method == "totp" and fake_sdk.logins[0]["secret"] is None


def test_relogin_once_on_expired_token(fake_sdk):
    import pyotp

    client = GrowwSdkClient("k", totp_secret=pyotp.random_base32())
    fake_sdk.fail_next_quote = 1
    out = client.quote("ABC")
    assert out["token_used"] == "token-2" and len(fake_sdk.logins) == 2


def test_no_credentials_rejected(fake_sdk):
    with pytest.raises(DataUnavailableError):
        GrowwSdkClient("k")


def test_totp_secret_never_in_settings_dump(monkeypatch):
    monkeypatch.setenv("NSE_BROKER_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    s = Settings()
    assert "JBSWY3DPEHPK3PXP" not in repr(s)
    assert "JBSWY3DPEHPK3PXP" not in s.version() and "broker_totp_secret" not in str(
        s.model_dump(mode="json", exclude={"broker_api_key", "broker_api_secret",
                                           "broker_totp_secret", "llm_api_key"}))
