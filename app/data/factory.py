"""Build the configured market-data provider."""

from __future__ import annotations

from datetime import datetime

from app.config import DataProviderName, Settings
from app.data.base_provider import MarketDataProvider
from app.data.historical_provider import HistoricalCSVProvider
from app.data.mock_provider import MockMarketDataProvider
from app.data.models import DataUnavailableError, Instrument
from app.utils.market_calendar import MarketCalendar


def build_provider(
    settings: Settings, calendar: MarketCalendar, as_of: datetime | None = None
) -> MarketDataProvider:
    if settings.data.provider is DataProviderName.MOCK:
        return MockMarketDataProvider(calendar, seed=settings.data.mock_seed, as_of=as_of)
    if settings.data.provider is DataProviderName.HISTORICAL:
        return HistoricalCSVProvider(
            settings.data.historical_dir, calendar, settings.data.max_candle_jump_pct
        )
    if settings.data.provider is DataProviderName.GROWW:
        return build_groww_provider(settings, calendar)
    raise ValueError(f"unsupported provider {settings.data.provider}")


def load_universe(path) -> list[Instrument]:
    import pandas as pd

    if not path.exists():
        raise DataUnavailableError(f"universe file not found: {path}")
    table = pd.read_csv(path, dtype=str, comment="#").fillna("")
    if table.empty:
        raise DataUnavailableError(f"universe file {path} lists no symbols")
    return [Instrument(r["symbol"].strip(), r.get("name", "").strip() or r["symbol"].strip())
            for _, r in table.iterrows()]


def build_groww_provider(settings: Settings, calendar: MarketCalendar) -> MarketDataProvider:
    """Live Groww data. Fails loudly when prerequisites are missing; never
    falls back to mock or historical data."""
    from app.data.groww_provider import GrowwMarketDataProvider, GrowwSdkClient

    if settings.broker_api_key is None or (settings.broker_totp_secret is None
                                           and settings.broker_api_secret is None):
        raise DataUnavailableError(
            "live Groww data needs NSE_BROKER_API_KEY plus NSE_BROKER_TOTP_SECRET (TOTP) or "
            "NSE_BROKER_API_SECRET (approval key) in the environment, and an active Groww "
            "Trading API subscription")
    return groww_provider(settings, calendar, load_universe(settings.data.universe_file))


def groww_provider(settings: Settings, calendar: MarketCalendar,
                   instruments: list[Instrument], client=None) -> MarketDataProvider:
    """The read-only Groww adapter for an explicit instrument list (e.g. the
    universe builder). This module is the only place that constructs it."""
    from app.data.groww_provider import GrowwMarketDataProvider, GrowwSdkClient

    if client is None:
        secret = lambda v: v.get_secret_value() if v is not None else None  # noqa: E731
        client = GrowwSdkClient(settings.broker_api_key.get_secret_value(),
                                api_secret=secret(settings.broker_api_secret),
                                totp_secret=secret(settings.broker_totp_secret),
                                timeout=settings.data.request_timeout_seconds)
    return GrowwMarketDataProvider(client, calendar, instruments, settings)
