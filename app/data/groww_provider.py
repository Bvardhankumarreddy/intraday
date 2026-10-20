"""Groww live market-data adapter (read-only).

STATUS: implemented from Groww's Python-SDK documentation and checked against
a live Groww account on 2026-10-19 (after market hours: authentication,
quote, 5-minute and daily candles for RELIANCE and SBIN). Not yet observed
during market hours. Requirements:
- An active Groww Trading API subscription (paid; the free trial excludes
  live quotes and historical candles per Groww's blog).
- ``pip install growwapi pyotp`` and credentials in the environment, never in
  source or YAML: NSE_BROKER_API_KEY plus NSE_BROKER_TOTP_SECRET (TOTP, no
  daily approval) or NSE_BROKER_API_SECRET (approval key).

Documented facts this adapter relies on:
- get_quote(exchange, segment, trading_symbol): one instrument per call;
  fields last_price, bid_price, bid_quantity, offer_price, offer_quantity,
  last_trade_time, volume, ohlc. Bid/offer are None outside market hours.
- Historical candles: 5-minute interval, max 15 days per request, last 3
  months only; each candle = [epoch seconds, open, high, low, close, volume].
- Live-data rate limit: 10 requests/s, 300/min.

Observed on the live API (2026-10-19), differing from or missing in the docs:
- ``last_trade_time`` is in epoch SECONDS (docs say milliseconds). The unit
  is detected from the magnitude, so either works.
- Intraday candle volume is CUMULATIVE for the session (it rises to the
  day's total). It is converted to per-candle volume by differencing within
  each session; the first candle keeps its value (it may include pre-open
  auction volume). A decrease means the assumption broke: the batch is
  rejected instead of producing negative volume.
- Candle timestamps are candle START times (first 09:15, last 15:25).
- The 15:20 candle was missing on every day checked. It is not filled; its
  volume ends up in the 15:25 candle's difference, and the gap is reported.
- Daily candles carry per-day volume and are stamped 00:00 IST.

Safety: the client wrapper exposes data methods only. The SDK object itself
is wrapped in an allowlist proxy (``data_only_sdk``): only quote, historical
candles and the exchange/segment constants are reachable. Every other SDK
attribute, including the order, holdings and positions methods, raises
``BlockedSdkCall``. Tests enforce this.

Resilience (Phase 8):
- every data request carries a timeout (``data.request_timeout_seconds``);
  the SDK's own default is no timeout at all;
- bounded retries with exponential backoff and +/- jitter for transient
  errors only (timeouts, connection errors, rate limits);
- a circuit breaker: after ``circuit_failure_threshold`` consecutive failed
  calls it OPENS and fails fast (no network call) for
  ``circuit_cooldown_seconds``, then allows one probe (HALF_OPEN): success
  closes it, failure re-opens it. ``health()`` reports the circuit, failure
  counts, reconnect attempts, outages and the last recovery duration;
- login and authorisation failures become a sanitised
  ``DataUnavailableError`` (no response text, no credentials).
Market data calls are read-only and idempotent, so retrying them cannot
duplicate any side effect.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from app.config import Settings
from app.data.base_provider import MarketDataProvider
from app.data.models import (
    CANDLE_COLUMNS,
    CandleSeries,
    DataMode,
    DataQualityError,
    DataUnavailableError,
    Instrument,
    Quote,
    parse_interval,
)
from app.data.validation import validate_candles, validate_quote
from app.utils.logging import get_logger
from app.utils.market_calendar import MarketCalendar

log = get_logger(__name__)
UTC = ZoneInfo("UTC")
# Documented max range per request, by interval in minutes.
MAX_DAYS_PER_REQUEST = {1: 7, 5: 15, 10: 30, 60: 150, 240: 365, 1440: 1080}


class ConnectionState(StrEnum):
    UNKNOWN = "UNKNOWN"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"  # recent calls needed retries
    DISCONNECTED = "DISCONNECTED"  # last call failed after all retries


class CircuitState(StrEnum):
    CLOSED = "CLOSED"  # calls flow normally
    OPEN = "OPEN"  # failing fast during the cooldown; no network calls
    HALF_OPEN = "HALF_OPEN"  # cooldown over: one probe call decides


class BlockedSdkCall(PermissionError):
    """An SDK attribute outside the market-data allowlist was requested."""


SDK_ALLOWLIST = frozenset({"get_quote", "get_historical_candle_data", "EXCHANGE_NSE",
                           "SEGMENT_CASH"})


def data_only_sdk(api: Any) -> Any:
    """Wrap a ``growwapi.GrowwAPI`` instance so only market-data methods are
    reachable. The SDK object is held in a closure, not an attribute."""

    class DataOnlyGrowwAPI:
        __slots__ = ()

        def __getattr__(self, name: str):
            if name not in SDK_ALLOWLIST:
                raise BlockedSdkCall(
                    f"Groww SDK attribute {name!r} is blocked: this application uses market "
                    "data only; paper trading never calls broker execution endpoints")
            return getattr(api, name)

        def __repr__(self) -> str:
            return "<DataOnlyGrowwAPI>"

    return DataOnlyGrowwAPI()


class GrowwDataClient(Protocol):
    """The only calls this project makes. Implemented by ``GrowwSdkClient``
    for the real SDK and by fakes in tests."""

    def quote(self, trading_symbol: str) -> dict[str, Any]: ...

    def candles(self, trading_symbol: str, start: datetime, end: datetime,
                interval_minutes: int) -> dict[str, Any]: ...


TRANSIENT = (TimeoutError, ConnectionError, OSError)


def is_transient(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    return isinstance(exc, TRANSIENT) or "ratelimit" in name or "timeout" in name \
        or "temporar" in name


def is_auth_failure(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    return "authenticat" in name or "authoris" in name or "authoriz" in name


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.5
    max_delay: float = 8.0

    def delay(self, attempt: int) -> float:
        """Bounded exponential backoff for attempt 1, 2, ... (no jitter, so
        behaviour is deterministic and testable)."""
        return min(self.max_delay, self.base_delay * 2 ** (attempt - 1))


class GrowwMarketDataProvider(MarketDataProvider):
    name = "groww"
    mode = DataMode.LIVE

    def __init__(self, client: GrowwDataClient, calendar: MarketCalendar,
                 instruments: list[Instrument], settings: Settings,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], datetime] | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 rng: random.Random | None = None):
        self.client = client
        self.calendar = calendar
        self.instruments = instruments
        self.settings = settings
        d = settings.data
        self.retry = RetryPolicy(d.retry_max_attempts, d.retry_base_delay_seconds,
                                 d.retry_max_delay_seconds)
        self.sleep = sleep
        self.clock = clock or (lambda: datetime.now(calendar.tz))
        self.min_gap = d.min_seconds_between_calls
        self.state = ConnectionState.UNKNOWN
        self.last_error: str | None = None
        self.last_success_at: datetime | None = None
        self.last_quote_at: dict[str, datetime] = {}
        self._last_call = 0.0
        # Data-quality accounting: raw batches as received (before validation)
        # and the number of retried calls (reconnect attempts).
        self.last_raw: dict[str, pd.DataFrame] = {}
        self.retries = 0
        # Circuit breaker and outage accounting.
        self.monotonic = monotonic
        self.rng = rng or random.Random()
        self.jitter = d.retry_jitter_fraction
        self.circuit = CircuitState.CLOSED
        self.circuit_threshold = d.circuit_failure_threshold
        self.circuit_cooldown = d.circuit_cooldown_seconds
        self.consecutive_failures = 0
        self.circuit_opened = 0
        self._opened_at = 0.0
        self.outages = 0
        self.outage_started: datetime | None = None
        self.last_recovery_seconds: float | None = None
        self.calls_failed_fast = 0

    # -- call policy ----------------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        base = self.retry.delay(attempt)
        if self.jitter:
            base *= 1 + self.jitter * self.rng.uniform(-1, 1)
        return max(0.0, min(self.retry.max_delay, base))

    def _gate(self, what: str) -> int:
        """Attempts allowed for this call under the circuit breaker."""
        if self.circuit is CircuitState.OPEN:
            remaining = self.circuit_cooldown - (self.monotonic() - self._opened_at)
            if remaining > 0:
                self.calls_failed_fast += 1
                raise DataUnavailableError(
                    f"Groww {what} not attempted: circuit OPEN after "
                    f"{self.consecutive_failures} consecutive failures; next probe in "
                    f"{remaining:.0f}s (last error: {self.last_error})")
            self.circuit = CircuitState.HALF_OPEN
            log.info("groww circuit half-open: probing", extra={"call": what})
        return 1 if self.circuit is CircuitState.HALF_OPEN else self.retry.max_attempts

    def _failed(self, what: str, exc: Exception, attempt: int, counts: bool) -> None:
        self.state = ConnectionState.DISCONNECTED
        if self.outage_started is None:
            self.outage_started = self.clock()
        if counts:
            self.consecutive_failures += 1
            if self.circuit is CircuitState.HALF_OPEN or \
                    self.consecutive_failures >= self.circuit_threshold:
                if self.circuit is not CircuitState.OPEN:
                    self.circuit_opened += 1
                self.circuit = CircuitState.OPEN
                self._opened_at = self.monotonic()
                log.error("groww circuit OPEN", extra={
                    "call": what, "consecutive_failures": self.consecutive_failures,
                    "cooldown_s": self.circuit_cooldown})
        log.warning("groww call failed", extra={"call": what, "error": str(exc),
                                                "error_type": type(exc).__name__,
                                                "attempt": attempt})

    def _call(self, what: str, fn: Callable[[], Any]) -> Any:
        attempts = self._gate(what)
        for attempt in range(1, attempts + 1):
            wait = self.min_gap - (self.monotonic() - self._last_call)
            if wait > 0:
                self.sleep(wait)
            self._last_call = self.monotonic()
            try:
                out = fn()
            except BlockedSdkCall:
                raise  # a safety violation, never retried or hidden
            except Exception as exc:  # noqa: BLE001 - classified below
                self.last_error = f"{what}: {type(exc).__name__}: {exc}"
                transient = is_transient(exc)
                if not transient or attempt == attempts:
                    # Outage-type failures (transient, auth) count towards the
                    # circuit; symbol-specific errors (e.g. bad request) don't.
                    self._failed(what, exc, attempt, transient or is_auth_failure(exc))
                    raise DataUnavailableError(
                        f"Groww {what} failed after {attempt} attempt(s): "
                        f"{type(exc).__name__}: {exc}") from exc
                self.state = ConnectionState.DEGRADED
                self.retries += 1
                self.sleep(self._backoff(attempt))
                continue
            if attempt == 1 and self.circuit is CircuitState.CLOSED:
                self.state = ConnectionState.CONNECTED
            elif self.circuit is CircuitState.HALF_OPEN:
                self.state = ConnectionState.CONNECTED
            self.circuit = CircuitState.CLOSED
            self.consecutive_failures = 0
            self.last_success_at = self.clock()
            if self.outage_started is not None:
                self.last_recovery_seconds = (self.last_success_at
                                              - self.outage_started).total_seconds()
                self.outages += 1
                log.info("groww data recovered", extra={
                    "call": what, "recovery_seconds": self.last_recovery_seconds})
                self.outage_started = None
            return out
        raise AssertionError("unreachable")

    # -- MarketDataProvider ---------------------------------------------------

    def list_instruments(self) -> list[Instrument]:
        return list(self.instruments)

    def get_quote(self, symbol: str) -> Quote:
        raw = self._call(f"quote {symbol}", lambda: self.client.quote(symbol))
        received = self.clock()
        try:
            ts_raw = raw["last_trade_time"]
            price = float(raw["last_price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DataQualityError(f"Groww quote for {symbol} missing fields: {exc}") from exc
        if ts_raw is None:
            raise DataQualityError(f"Groww quote for {symbol} has no last_trade_time")
        quote = Quote(
            symbol=symbol,
            last_price=price,
            timestamp=epoch_to_datetime(ts_raw).astimezone(self.calendar.tz),
            mode=self.mode,
            source=self.name,
            bid=_num(raw.get("bid_price")),
            ask=_num(raw.get("offer_price")),
            bid_qty=_int(raw.get("bid_quantity")),
            ask_qty=_int(raw.get("offer_quantity")),
            received_at=received,
        )
        report = validate_quote(quote, received, self.settings.data.max_staleness_seconds)
        if not report.is_valid:
            raise DataQualityError(f"Groww quote for {symbol} rejected: "
                                   + "; ".join(i.message for i in report.errors))
        self.last_quote_at[symbol] = quote.timestamp
        return quote

    def get_candles(self, symbol: str, interval: str, start: date, end: date) -> CandleSeries:
        iv = parse_interval(interval)
        minutes = 1440 if iv.is_daily else iv.minutes
        if minutes not in MAX_DAYS_PER_REQUEST:
            raise DataUnavailableError(f"Groww does not document a {iv.label} interval")
        span = MAX_DAYS_PER_REQUEST[minutes]
        rows: list[list] = []
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(end, chunk_start + timedelta(days=span - 1))
            s_dt = datetime.combine(chunk_start, datetime.min.time(), tzinfo=self.calendar.tz)
            e_dt = datetime.combine(chunk_end, datetime.max.time().replace(microsecond=0),
                                    tzinfo=self.calendar.tz)
            raw = self._call(f"candles {symbol}",
                             lambda: self.client.candles(symbol, s_dt, e_dt, minutes))
            rows += list(raw.get("candles") or [])
            chunk_start = chunk_end + timedelta(days=1)
        if not rows:
            raise DataUnavailableError(f"Groww returned no {iv.label} candles for {symbol}")
        try:
            frame = pd.DataFrame(rows, columns=CANDLE_COLUMNS)
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="s", utc=True) \
                .dt.tz_convert(self.calendar.tz)
            for col in CANDLE_COLUMNS[1:]:
                frame[col] = pd.to_numeric(frame[col])
            if iv.is_daily:  # one row per session, stamped at the session open
                frame["timestamp"] = [pd.Timestamp(self.calendar.session_bounds(t.date())[0])
                                      for t in frame["timestamp"]]
            elif frame["timestamp"].is_monotonic_increasing and \
                    not frame["timestamp"].duplicated().any():
                # Out-of-order or duplicate rows are left as received, so the
                # validator below rejects them with the right reason.
                frame["volume"] = cumulative_to_candle_volume(frame, symbol)
        except (ValueError, TypeError) as exc:
            raise DataQualityError(f"Groww candles for {symbol} malformed: {exc}") from exc
        if not iv.is_daily:
            self.last_raw[symbol] = frame.copy()
        report = validate_candles(frame, iv, self.calendar, self.settings.data.max_candle_jump_pct)
        if not report.is_valid:
            raise DataQualityError(f"Groww candles for {symbol} rejected: "
                                   + "; ".join(i.message for i in report.errors), report.errors)
        return CandleSeries(symbol=symbol, interval=iv, frame=frame, mode=self.mode,
                            source=self.name, retrieved_at=self.clock(),
                            warnings=[i.message for i in report.warnings])

    def health(self) -> dict:
        return {"state": self.state.value, "last_error": self.last_error,
                "last_success_at": self.last_success_at, "retries": self.retries,
                "last_quote_at": dict(self.last_quote_at),
                "circuit": self.circuit.value, "consecutive_failures": self.consecutive_failures,
                "circuit_opened": self.circuit_opened, "calls_failed_fast": self.calls_failed_fast,
                "outages_recovered": self.outages, "outage_started": self.outage_started,
                "last_recovery_seconds": self.last_recovery_seconds}


def epoch_to_datetime(value: float | int) -> datetime:
    """Epoch seconds or milliseconds (detected by magnitude) to UTC datetime."""
    v = float(value)
    return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, UTC)


def cumulative_to_candle_volume(frame: pd.DataFrame, symbol: str) -> pd.Series:
    """Groww reports intraday volume cumulatively per session. Difference it
    within each session; reject the batch if it ever decreases."""
    session = frame["timestamp"].dt.date
    diffs = frame["volume"].groupby(session).diff()
    if (diffs < 0).any():
        first = frame.loc[diffs < 0, "timestamp"].iloc[0]
        raise DataQualityError(
            f"Groww candles for {symbol}: cumulative volume decreased at {first}; the "
            "cumulative-volume assumption does not hold for this batch")
    return diffs.fillna(frame["volume"]).astype("int64")


def _num(v) -> float | None:
    return None if v in (None, "", 0, 0.0) else float(v)


def _int(v) -> int | None:
    return None if v is None else int(v)


class GrowwSdkClient:
    """Thin wrapper over the official ``growwapi`` SDK - DATA METHODS ONLY.

    Authentication (secrets come from the environment only):
    - TOTP (preferred, no daily approval): ``api_key`` is the TOTP token and
      ``totp_secret`` generates the 6-digit code with ``pyotp``.
    - Approval key: ``api_key`` + ``api_secret``; needs daily approval on
      Groww's API-keys page.
    If a call fails with an authentication error (e.g. an expired token), it
    logs in again once and retries. Login failures raise a sanitised
    ``DataUnavailableError``.

    The SDK instance is only ever held through ``data_only_sdk``; no
    attribute of this wrapper exposes the full SDK. Every request passes
    ``timeout`` (seconds).

    Method names and arguments were checked against growwapi 1.5.0:
    ``get_quote(trading_symbol, exchange, segment, timeout)`` and
    ``get_historical_candle_data(trading_symbol, exchange, segment,
    start_time, end_time, interval_in_minutes, timeout)`` (deprecated by
    Groww but working on 2026-10-19).
    """

    def __init__(self, api_key: str, api_secret: str | None = None,
                 totp_secret: str | None = None, timeout: float = 10.0):
        try:
            from growwapi import GrowwAPI  # optional dependency
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise DataUnavailableError("growwapi is not installed (pip install growwapi)") from exc
        if not totp_secret and not api_secret:
            raise DataUnavailableError("Groww login needs a TOTP secret or an API secret")
        self._get_token = GrowwAPI.get_access_token
        self._connect = lambda token: data_only_sdk(GrowwAPI(token))
        self._exchange, self._segment = GrowwAPI.EXCHANGE_NSE, GrowwAPI.SEGMENT_CASH
        self._key = api_key
        self._secret = api_secret
        self._totp_secret = totp_secret
        self.timeout = timeout
        self.auth_method = "totp" if totp_secret else "approval"
        self._api = self._connect(self._login())

    def _login(self) -> str:
        try:
            if self._totp_secret:
                import pyotp

                code = pyotp.TOTP(self._totp_secret).now()
                return self._get_token(api_key=self._key, totp=code)
            return self._get_token(api_key=self._key, secret=self._secret)
        except Exception as exc:  # noqa: BLE001 - sanitised: no response text or credentials
            status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            raise DataUnavailableError(
                f"Groww login failed ({type(exc).__name__}"
                + (f", status {status}" if status else "") + "): check that the "
                "NSE_BROKER_* credentials are valid, not revoked, and that the Trading API "
                "subscription is active") from None

    def _with_relogin(self, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - only auth errors are retried here
            if "authentication" not in type(exc).__name__.lower():
                raise
            self._api = self._connect(self._login())
            return fn()

    def quote(self, trading_symbol: str) -> dict[str, Any]:
        return self._with_relogin(lambda: self._api.get_quote(
            trading_symbol=trading_symbol, exchange=self._exchange, segment=self._segment,
            timeout=self.timeout))

    def candles(self, trading_symbol: str, start: datetime, end: datetime,
                interval_minutes: int) -> dict[str, Any]:
        return self._with_relogin(lambda: self._api.get_historical_candle_data(
            trading_symbol=trading_symbol, exchange=self._exchange, segment=self._segment,
            start_time=start.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=end.strftime("%Y-%m-%d %H:%M:%S"), interval_in_minutes=interval_minutes,
            timeout=self.timeout))
