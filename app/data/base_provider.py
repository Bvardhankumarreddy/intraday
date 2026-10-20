"""Broker-agnostic market-data provider interface.

Providers are read-only. Nothing in this package may place, modify or cancel
orders.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from app.data.models import CandleSeries, DataMode, Instrument, Quote


class MarketDataProvider(ABC):
    name: str
    mode: DataMode

    @abstractmethod
    def list_instruments(self) -> list[Instrument]:
        """Instruments this provider can supply data for."""

    @abstractmethod
    def get_candles(self, symbol: str, interval: str, start: date, end: date) -> CandleSeries:
        """Validated OHLCV candles for ``start``..``end`` inclusive.

        Raises ``DataQualityError`` if the data fails validation and
        ``DataUnavailableError`` if it cannot be supplied.
        """

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        """Latest quote. Raises ``DataUnavailableError`` if not supported."""

    def get_instrument(self, symbol: str) -> Instrument:
        for instrument in self.list_instruments():
            if instrument.symbol == symbol:
                return instrument
        raise KeyError(f"unknown symbol {symbol!r} for provider {self.name}")
