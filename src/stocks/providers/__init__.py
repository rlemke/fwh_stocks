"""Market-data provider seam.

Everything else in the domain depends only on the :class:`PriceProvider`
protocol and the three dataclasses below, so swapping yfinance for a paid feed
(Polygon, Tiingo, Alpha Vantage) is one new module plus ``FW_STOCKS_PROVIDER``.

Providers are deliberately *thin*: fetch and normalise, no scoring, no caching
policy, no Mongo. Caching lives in :mod:`stocks.market`, scoring in
:mod:`stocks.screener`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

__all__ = [
    "Bar",
    "Quote",
    "AnalystView",
    "PriceProvider",
    "ProviderError",
    "get_provider",
    "available_providers",
]


class ProviderError(RuntimeError):
    """A provider could not satisfy a request.

    Raised explicitly rather than returning empty data — a silent empty result
    would be scored as "no signal" and could quietly select an arbitrary group.
    """


@dataclass(frozen=True)
class Bar:
    """One daily OHLCV bar. ``close`` is split/dividend adjusted where the
    provider offers it, because the factor math compares prices across a year."""

    date: str  # ISO yyyy-mm-dd, the exchange's trading date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Quote:
    """A current (usually delayed) price.

    ``as_of`` is carried all the way into the snapshot document so a stale feed
    is visible in the UI instead of silently producing a flat P/L line.
    """

    ticker: str
    price: float
    as_of: str  # ISO-8601 UTC
    currency: str = "USD"
    previous_close: float | None = None
    stale: bool = False


@dataclass(frozen=True)
class AnalystView:
    """Sell-side consensus for one ticker. Every field is optional — coverage is
    patchy, and absent fields must be treated as missing, never as zero."""

    ticker: str
    recommendation_mean: float | None = None  # 1.0 strong buy .. 5.0 strong sell
    target_mean: float | None = None
    current_price: float | None = None
    analyst_count: int | None = None
    name: str | None = None
    sector: str | None = None

    @property
    def target_upside(self) -> float | None:
        """Fractional upside to the mean target, or ``None`` if uncomputable."""
        if not self.target_mean or not self.current_price:
            return None
        if self.current_price <= 0:
            return None
        return (self.target_mean - self.current_price) / self.current_price


@runtime_checkable
class PriceProvider(Protocol):
    """The whole external-data surface of this domain."""

    name: str

    def universe(self, name: str) -> list[str]:
        """Ticker list for a named universe (``sp500``, ``nasdaq100``, ``dow30``)."""
        ...

    def sector_map(self) -> dict[str, str]:
        """``{ticker: GICS sector}`` for everything the provider can classify.

        Whole-universe rather than per-ticker on purpose: sector filtering has
        to run before the expensive stages, so it must not cost one request per
        symbol. Tickers absent from the result are simply unclassified.
        """
        ...

    def daily_bars(self, tickers: list[str], start: date, end: date) -> dict[str, list[Bar]]:
        """Daily bars per ticker. Tickers with no data are omitted, not empty-listed."""
        ...

    def quotes(self, tickers: list[str]) -> dict[str, Quote]:
        """Current/delayed price per ticker."""
        ...

    def analyst(self, tickers: list[str]) -> dict[str, AnalystView]:
        """Consensus view per ticker; may be empty when the feed has no coverage."""
        ...


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------

_REGISTRY: dict[str, str] = {
    "yfinance": "stocks.providers.yfinance_provider:YFinanceProvider",
    "stub": "stocks.providers.stub_provider:StubProvider",
}

DEFAULT_PROVIDER = "yfinance"


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


def get_provider(name: str | None = None) -> PriceProvider:
    """Instantiate the configured provider.

    Resolution order: explicit *name*, then ``FW_STOCKS_PROVIDER``, then
    ``yfinance``. An unknown name is an error rather than a silent fallback —
    a typo must not quietly switch the system onto synthetic data.
    """
    key = (name or os.environ.get("FW_STOCKS_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    spec = _REGISTRY.get(key)
    if spec is None:
        raise ProviderError(
            f"Unknown price provider {key!r}. Available: {', '.join(available_providers())}"
        )
    module_name, _, class_name = spec.partition(":")
    import importlib

    module = importlib.import_module(module_name)
    return getattr(module, class_name)()
