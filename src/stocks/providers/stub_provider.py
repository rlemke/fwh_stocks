"""Deterministic synthetic provider — the test/offline backend.

Generates a reproducible price history per ticker from a hash of the symbol, so
the whole domain (screener, ranking, snapshots, P/L) can be exercised with no
network and no API key. The test suite requires this; the tools-pattern spec
requires the suite to pass offline.

It is explicitly synthetic. ``FW_STOCKS_PROVIDER=stub`` is opt-in, never a
fallback, so real runs can never silently degrade to fabricated prices.
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, date, datetime, timedelta

from . import AnalystView, Bar, ProviderError, Quote

# A small, fixed universe. Real symbols so the shape is familiar, synthetic
# prices so nothing here should ever be mistaken for market data.
_UNIVERSES: dict[str, list[str]] = {
    "sp500": [
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "GOOGL",
        "META",
        "TSLA",
        "BRK-B",
        "JPM",
        "V",
        "UNH",
        "XOM",
        "JNJ",
        "WMT",
        "MA",
        "PG",
        "AVGO",
        "HD",
        "CVX",
        "MRK",
        "ABBV",
        "COST",
        "PEP",
        "KO",
        "ADBE",
        "CSCO",
        "CRM",
        "MCD",
        "TMO",
        "ACN",
    ],
    "nasdaq100": [
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "GOOGL",
        "META",
        "TSLA",
        "AVGO",
        "COST",
        "ADBE",
        "CSCO",
        "PEP",
        "AMD",
        "INTC",
        "QCOM",
        "TXN",
    ],
    "dow30": [
        "AAPL",
        "MSFT",
        "JPM",
        "V",
        "UNH",
        "JNJ",
        "WMT",
        "PG",
        "HD",
        "CVX",
        "MRK",
        "KO",
        "MCD",
        "CSCO",
        "IBM",
        "GS",
    ],
}

BENCHMARK = "SPY"

#: Left out of the synthetic sector map on purpose, so the unclassified-ticker
#: path is exercised by the offline suite rather than only in production.
_UNCLASSIFIED = "ACN"


def _seed(ticker: str) -> int:
    return int(hashlib.sha256(ticker.encode()).hexdigest()[:12], 16)


class StubProvider:
    """Synthetic, deterministic, offline."""

    name = "stub"

    # -- universes ---------------------------------------------------------

    def universe(self, name: str) -> list[str]:
        key = (name or "sp500").strip().lower()
        tickers = _UNIVERSES.get(key)
        if tickers is None:
            raise ProviderError(
                f"Unknown universe {name!r}. Available: {', '.join(sorted(_UNIVERSES))}"
            )
        return list(tickers)

    def sector_map(self) -> dict[str, str]:
        """Deterministic synthetic sectors, spread across all eleven.

        Assigned by hash so the mapping is stable across hosts and runs, which
        is what lets sector-filter tests assert on exact membership. One ticker
        is deliberately left unclassified so the "unknown sector" path stays
        covered.
        """
        from ..sectors import SECTORS

        out: dict[str, str] = {}
        for tickers in _UNIVERSES.values():
            for ticker in tickers:
                if ticker == _UNCLASSIFIED:
                    continue
                out[ticker] = SECTORS[_seed(ticker) % len(SECTORS)]
        return out

    # -- prices ------------------------------------------------------------

    def _series(self, ticker: str, days: int) -> list[Bar]:
        """A reproducible geometric walk with a per-ticker drift and volatility."""
        seed = _seed(ticker)
        base = 20.0 + (seed % 400)  # $20 .. $420 starting level
        drift = ((seed >> 8) % 200 - 90) / 100_000.0  # ~-0.09% .. +0.11% per day
        vol = 0.008 + ((seed >> 16) % 220) / 10_000.0  # 0.8% .. 3.0% daily
        liquidity = 1.0 + ((seed >> 24) % 100) / 10.0

        bars: list[Bar] = []
        price = base
        today = date.today()
        # Walk business days backwards, then emit forwards.
        days_back: list[date] = []
        cursor = today
        while len(days_back) < days:
            if cursor.weekday() < 5:
                days_back.append(cursor)
            cursor -= timedelta(days=1)
        for i, day in enumerate(reversed(days_back)):
            # Deterministic pseudo-noise: a sine mix, not an RNG, so the series
            # is identical on every host and every Python version.
            noise = math.sin(i * 0.7 + seed % 97) * 0.6 + math.sin(i * 0.13 + seed % 31) * 0.4
            price = max(1.0, price * (1.0 + drift + vol * noise))
            high = price * (1.0 + vol / 2)
            low = price * (1.0 - vol / 2)
            open_ = (high + low) / 2
            volume = 500_000 * liquidity * (1.0 + 0.3 * math.sin(i * 0.21))
            bars.append(
                Bar(
                    date=day.isoformat(),
                    open=round(open_, 4),
                    high=round(high, 4),
                    low=round(low, 4),
                    close=round(price, 4),
                    volume=round(volume, 1),
                )
            )
        return bars

    def daily_bars(self, tickers: list[str], start: date, end: date) -> dict[str, list[Bar]]:
        span = max(1, (end - start).days)
        days = max(1, int(span * 5 / 7))
        out: dict[str, list[Bar]] = {}
        for ticker in tickers:
            bars = self._series(ticker, days)
            out[ticker] = [
                b for b in bars if start.isoformat() <= b.date <= end.isoformat()
            ] or bars
        return out

    def quotes(self, tickers: list[str]) -> dict[str, Quote]:
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        out: dict[str, Quote] = {}
        for ticker in tickers:
            bars = self._series(ticker, 3)
            out[ticker] = Quote(
                ticker=ticker,
                price=bars[-1].close,
                as_of=now,
                previous_close=bars[-2].close if len(bars) > 1 else None,
            )
        return out

    # -- consensus ---------------------------------------------------------

    def analyst(self, tickers: list[str]) -> dict[str, AnalystView]:
        out: dict[str, AnalystView] = {}
        for ticker in tickers:
            seed = _seed(ticker)
            last = self._series(ticker, 2)[-1].close
            # Leave roughly one in seven with no coverage, mirroring real gaps.
            if seed % 7 == 0:
                out[ticker] = AnalystView(ticker=ticker, name=f"{ticker} Inc.", sector="Synthetic")
                continue
            out[ticker] = AnalystView(
                ticker=ticker,
                recommendation_mean=1.0 + (seed % 300) / 100.0,
                target_mean=round(last * (0.85 + (seed % 60) / 100.0), 4),
                current_price=last,
                analyst_count=3 + (seed % 40),
                name=f"{ticker} Inc.",
                sector="Synthetic",
            )
        return out

    # -- corporate actions -------------------------------------------------

    #: Tests set this to {ticker: [(date, ratio), ...]}; the synthetic market has no splits.
    split_table: dict[str, list[tuple[date, float]]] = {}

    def splits(self, tickers: list[str], since: date) -> dict[str, list[tuple[date, float]]]:
        out = {}
        for t in tickers:
            rows = [(d, r) for d, r in self.split_table.get(t, []) if d > since]
            if rows:
                out[t] = sorted(rows)
        return out
