"""Cached market-data access — the only module that pairs a provider with storage.

Daily bars are expensive to refetch and immutable once the session closes, so
they are cached under ``cache/stocks/bars/`` with the sidecar protocol. Quotes
are never cached: a cached "current price" is a contradiction.

The cache key includes the provider name, so switching feeds does not silently
serve another provider's numbers.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from . import storage
from .providers import Bar, PriceProvider, Quote, get_provider

logger = logging.getLogger("stocks.market")

BARS_CACHE_TYPE = "bars"
UNIVERSE_CACHE_TYPE = "universe"
ANALYST_CACHE_TYPE = "analyst"
SECTOR_CACHE_TYPE = "sectors"

BARS_MAX_AGE_S = 12 * 3600  # refetch daily; a session's bars settle after the close
UNIVERSE_MAX_AGE_S = 7 * 86400  # constituents change a few times a year
SECTOR_MAX_AGE_S = 7 * 86400  # a company changes GICS sector very rarely
ANALYST_MAX_AGE_S = 24 * 3600

BENCHMARK_DEFAULT = "SPY"


def _bars_key(provider: str, ticker: str, start: date, end: date) -> str:
    safe = ticker.replace("/", "_")
    return f"{provider}/{safe}/{start.isoformat()}_{end.isoformat()}.json"


def _to_bars(rows: list[dict]) -> list[Bar]:
    return [Bar(**row) for row in rows]


def _from_bars(bars: list[Bar]) -> list[dict]:
    return [
        {
            "date": b.date,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        }
        for b in bars
    ]


def resolve_universe(name: str, provider: PriceProvider | None = None) -> list[str]:
    provider = provider or get_provider()
    cached = storage.read_json_cached(
        UNIVERSE_CACHE_TYPE, f"{provider.name}/{name}.json", max_age_s=UNIVERSE_MAX_AGE_S
    )
    if cached:
        return list(cached)
    tickers = provider.universe(name)
    storage.write_json_cached(
        UNIVERSE_CACHE_TYPE,
        f"{provider.name}/{name}.json",
        tickers,
        extra={"universe": name, "count": len(tickers)},
    )
    return tickers


def resolve_sector_map(provider: PriceProvider | None = None) -> dict[str, str]:
    """``{ticker: GICS sector}``, cached for a week.

    Returns ``{}`` when the provider cannot classify anything, which callers
    must treat as "no sector information" — never as "no ticker has a sector",
    since silently filtering everything out would look like an empty universe.
    """
    provider = provider or get_provider()
    key = f"{provider.name}.json"
    cached = storage.read_json_cached(SECTOR_CACHE_TYPE, key, max_age_s=SECTOR_MAX_AGE_S)
    if cached:
        return dict(cached)
    try:
        mapping = provider.sector_map()
    except Exception as exc:  # noqa: BLE001 - sector data is optional context
        logger.warning("could not load the sector map from %s: %s", provider.name, exc)
        return {}
    if mapping:
        storage.write_json_cached(
            SECTOR_CACHE_TYPE, key, mapping, extra={"ticker_count": len(mapping)}
        )
    return mapping


def fetch_daily_bars(
    tickers: list[str],
    *,
    lookback_days: int = 400,
    provider: PriceProvider | None = None,
    benchmark: str = BENCHMARK_DEFAULT,
    force: bool = False,
) -> dict[str, list[Bar]]:
    """Bars for *tickers* plus the benchmark, cache-first.

    Missing tickers are fetched in one batched provider call — the provider
    handles multi-symbol downloads, so there is no per-ticker fan-out here (and
    therefore no wide FFL foreach to strand).
    """
    provider = provider or get_provider()
    end = date.today()
    start = end - timedelta(days=lookback_days)

    wanted = list(dict.fromkeys([*tickers, benchmark]))
    out: dict[str, list[Bar]] = {}
    missing: list[str] = []

    for ticker in wanted:
        key = _bars_key(provider.name, ticker, start, end)
        cached = (
            None
            if force
            else storage.read_json_cached(BARS_CACHE_TYPE, key, max_age_s=BARS_MAX_AGE_S)
        )
        if cached:
            out[ticker] = _to_bars(cached)
        else:
            missing.append(ticker)

    if missing:
        logger.info("fetching %d/%d symbols from %s", len(missing), len(wanted), provider.name)
        fetched = provider.daily_bars(missing, start, end)
        for ticker, bars in fetched.items():
            out[ticker] = bars
            storage.write_json_cached(
                BARS_CACHE_TYPE,
                _bars_key(provider.name, ticker, start, end),
                _from_bars(bars),
                source={
                    "provider": provider.name,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
                extra={"ticker": ticker, "bar_count": len(bars)},
            )

    return out


def fetch_analyst(
    tickers: list[str],
    *,
    provider: PriceProvider | None = None,
    force: bool = False,
) -> dict:
    """Consensus views, cached for a day (one HTTP round-trip per ticker upstream)."""
    provider = provider or get_provider()
    from .providers import AnalystView

    out: dict[str, AnalystView] = {}
    missing: list[str] = []
    today = date.today().isoformat()

    for ticker in tickers:
        key = f"{provider.name}/{today}/{ticker}.json"
        cached = (
            None
            if force
            else storage.read_json_cached(ANALYST_CACHE_TYPE, key, max_age_s=ANALYST_MAX_AGE_S)
        )
        if cached:
            out[ticker] = AnalystView(**cached)
        else:
            missing.append(ticker)

    if missing:
        for ticker, view in provider.analyst(missing).items():
            out[ticker] = view
            storage.write_json_cached(
                ANALYST_CACHE_TYPE,
                f"{provider.name}/{today}/{ticker}.json",
                {
                    "ticker": view.ticker,
                    "recommendation_mean": view.recommendation_mean,
                    "target_mean": view.target_mean,
                    "current_price": view.current_price,
                    "analyst_count": view.analyst_count,
                    "name": view.name,
                    "sector": view.sector,
                },
            )
    return out


def fetch_quotes(tickers: list[str], provider: PriceProvider | None = None) -> dict[str, Quote]:
    """Current prices. Never cached — a stale "current" price is worse than none."""
    provider = provider or get_provider()
    return provider.quotes(list(dict.fromkeys(tickers)))


def fetch_splits(
    tickers: list[str], since: date, provider: PriceProvider | None = None
) -> dict[str, list[tuple[date, float]]]:
    """Splits after *since*; empty for a provider that cannot report them."""
    provider = provider or get_provider()
    lookup = getattr(provider, "splits", None)
    if lookup is None:
        return {}
    return lookup(list(dict.fromkeys(tickers)), since)
