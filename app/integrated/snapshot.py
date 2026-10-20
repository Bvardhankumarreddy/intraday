"""One consistent market-data view per step (Phase 12).

The baseline engine, the research agent and the AI-assisted engine all read
market data in the same step. Wrapping the configured provider in
``StepSnapshot`` gives every consumer the identical response for the same
request within a step, so the arms are compared on the same information.
It also avoids tripling requests to a rate-limited live provider. ``reset()``
is called at the start of every step, and nothing is carried over between
steps. Errors are cached too, so every consumer sees the same failure.

It never changes the data mode or source: ``name`` and ``mode`` are the
wrapped provider's own.
"""

from __future__ import annotations

from app.data.base_provider import MarketDataProvider


class StepSnapshot(MarketDataProvider):
    def __init__(self, inner: MarketDataProvider):
        self.inner = inner
        self.name = inner.name
        self.mode = inner.mode
        self._cache: dict[tuple, tuple[bool, object]] = {}
        self.requests = 0  # calls that reached the wrapped provider

    def reset(self) -> None:
        self._cache.clear()

    def _get(self, key: tuple, fn):
        if key not in self._cache:
            self.requests += 1
            try:
                self._cache[key] = (True, fn())
            except Exception as exc:  # noqa: BLE001 - re-raised identically to everyone
                self._cache[key] = (False, exc)
        ok, value = self._cache[key]
        if not ok:
            raise value
        return value

    def list_instruments(self):
        return self._get(("instruments",), self.inner.list_instruments)

    def get_candles(self, symbol, interval, start, end):
        return self._get(("candles", symbol, interval, start, end),
                         lambda: self.inner.get_candles(symbol, interval, start, end))

    def get_quote(self, symbol):
        return self._get(("quote", symbol), lambda: self.inner.get_quote(symbol))

    # ``health`` and other provider-specific attributes (e.g. a mock's clock) are
    # delegated, so ``hasattr(snapshot, "health")`` matches the wrapped provider.
    def __getattr__(self, name):
        return getattr(self.inner, name)
