"""yfinance-backed provider — the default real-data backend.

Free, keyless, and covers everything this domain needs (daily bars, delayed
quotes, sell-side consensus). It is an *unofficial* Yahoo Finance scraper, so it
can break without notice and its quotes are ~15 minutes delayed. That is
acceptable for a daily-close, week-horizon tracker and unacceptable for anything
latency-sensitive — which is exactly why the provider seam exists.

Import of ``yfinance``/``pandas`` is deferred into the methods so the package
remains importable (and the offline test suite runnable) without them installed.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, date, datetime

import requests

from . import AnalystView, Bar, ProviderError, Quote

logger = logging.getLogger("stocks.providers.yfinance")

USER_AGENT = "facetwork-stocks/1.0 (+https://github.com/rlemke/facetwork)"
HTTP_TIMEOUT = 30

# Constituent lists. Plain CSV over HTTPS — no pandas, no HTML scraping, and the
# datasets repo is stable and versioned.
_UNIVERSE_SOURCES: dict[str, str] = {
    "sp500": (
        "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
        "main/data/constituents.csv"
    ),
}

# Small universes are inlined rather than fetched — a network round-trip to
# learn 30 symbols that change a few times a decade is not worth the failure mode.
_UNIVERSE_STATIC: dict[str, list[str]] = {
    "dow30": [
        "AAPL", "AMGN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS", "DOW",
        "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM", "MRK",
        "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT",
    ],
}


def _http_get(url: str) -> bytes:
    resp = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.content


def _normalise(ticker: str) -> str:
    """Yahoo uses ``BRK-B`` where the constituent lists use ``BRK.B``."""
    return ticker.strip().upper().replace(".", "-")


class YFinanceProvider:
    """Delayed Yahoo Finance data via the ``yfinance`` package."""

    name = "yfinance"

    # -- universes ---------------------------------------------------------

    def universe(self, name: str) -> list[str]:
        key = (name or "sp500").strip().lower()

        if key in _UNIVERSE_STATIC:
            return list(_UNIVERSE_STATIC[key])

        url = _UNIVERSE_SOURCES.get(key)
        if url is None:
            known = sorted({*_UNIVERSE_SOURCES, *_UNIVERSE_STATIC})
            raise ProviderError(f"Unknown universe {name!r}. Available: {', '.join(known)}")

        try:
            body = _http_get(url).decode("utf-8", "replace")
        except Exception as exc:  # network/HTTP — explicit, never an empty list
            raise ProviderError(f"Could not fetch the {key} constituent list: {exc}") from exc

        rows = list(csv.DictReader(io.StringIO(body)))
        tickers = [_normalise(r["Symbol"]) for r in rows if r.get("Symbol")]
        if not tickers:
            raise ProviderError(f"The {key} constituent list came back empty ({url})")
        return tickers

    # -- prices ------------------------------------------------------------

    def daily_bars(self, tickers: list[str], start: date, end: date) -> dict[str, list[Bar]]:
        if not tickers:
            return {}
        yf = self._yf()
        symbols = [_normalise(t) for t in tickers]

        try:
            frame = yf.download(
                tickers=symbols,
                start=start.isoformat(),
                end=end.isoformat(),
                interval="1d",
                auto_adjust=True,   # split/dividend adjusted — the factor math needs this
                actions=False,
                group_by="ticker",
                threads=True,
                progress=False,
            )
        except Exception as exc:
            raise ProviderError(f"yfinance bar download failed: {exc}") from exc

        if frame is None or frame.empty:
            raise ProviderError(
                f"yfinance returned no bars for {len(symbols)} symbols "
                f"({start}..{end}) — the feed may be blocked or rate-limited"
            )

        # With group_by="ticker" the columns are a 2-level MultiIndex even for a
        # SINGLE symbol, so branch on the frame's shape, not on how many symbols
        # were requested.
        grouped = getattr(frame.columns, "nlevels", 1) > 1

        out: dict[str, list[Bar]] = {}
        for symbol in symbols:
            try:
                sub = frame[symbol] if grouped else frame
            except KeyError:
                continue
            if "Close" not in sub.columns:
                continue
            sub = sub.dropna(subset=["Close"])
            if sub.empty:
                continue
            bars: list[Bar] = []
            for idx, row in sub.iterrows():
                bars.append(
                    Bar(
                        date=idx.date().isoformat(),
                        open=float(row["Open"]),
                        high=float(row["High"]),
                        low=float(row["Low"]),
                        close=float(row["Close"]),
                        volume=float(row.get("Volume") or 0.0),
                    )
                )
            if bars:
                out[symbol] = bars

        if not out:
            raise ProviderError("yfinance returned a frame with no usable rows")
        missing = len(symbols) - len(out)
        if missing:
            logger.warning("yfinance had no bars for %d of %d symbols", missing, len(symbols))
        return out

    def quotes(self, tickers: list[str]) -> dict[str, Quote]:
        if not tickers:
            return {}
        yf = self._yf()
        symbols = [_normalise(t) for t in tickers]
        now = datetime.now(UTC).replace(microsecond=0).isoformat()

        out: dict[str, Quote] = {}
        try:
            handles = yf.Tickers(" ".join(symbols))
        except Exception as exc:
            raise ProviderError(f"yfinance quote lookup failed: {exc}") from exc

        for symbol in symbols:
            handle = handles.tickers.get(symbol)
            if handle is None:
                continue
            price = prev = None
            try:
                fast = handle.fast_info
                price = fast.get("lastPrice") or fast.get("last_price")
                prev = fast.get("previousClose") or fast.get("previous_close")
            except Exception:  # noqa: BLE001 - fall through to the history path
                logger.debug("fast_info unavailable for %s", symbol)
            if price is None:
                try:
                    hist = handle.history(period="5d", interval="1d", auto_adjust=True)
                    if not hist.empty:
                        price = float(hist["Close"].iloc[-1])
                        if len(hist) > 1:
                            prev = float(hist["Close"].iloc[-2])
                except Exception:  # noqa: BLE001
                    logger.warning("no quote available for %s", symbol)
            if price is None:
                continue
            out[symbol] = Quote(
                ticker=symbol,
                price=float(price),
                as_of=now,
                previous_close=float(prev) if prev is not None else None,
            )

        if not out:
            raise ProviderError(
                f"yfinance returned no quotes for any of {len(symbols)} symbols — "
                "refusing to snapshot with no prices"
            )
        return out

    # -- consensus ---------------------------------------------------------

    def analyst(self, tickers: list[str]) -> dict[str, AnalystView]:
        """One HTTP round-trip per ticker — call this for a shortlist only."""
        if not tickers:
            return {}
        yf = self._yf()
        out: dict[str, AnalystView] = {}
        for raw in tickers:
            symbol = _normalise(raw)
            try:
                info = yf.Ticker(symbol).info or {}
            except Exception as exc:  # noqa: BLE001 - patchy coverage is expected
                logger.info("no analyst info for %s: %s", symbol, exc)
                out[symbol] = AnalystView(ticker=symbol)
                continue
            out[symbol] = AnalystView(
                ticker=symbol,
                recommendation_mean=_as_float(info.get("recommendationMean")),
                target_mean=_as_float(info.get("targetMeanPrice")),
                current_price=_as_float(
                    info.get("currentPrice") or info.get("regularMarketPrice")
                ),
                analyst_count=_as_int(info.get("numberOfAnalystOpinions")),
                name=info.get("longName") or info.get("shortName"),
                sector=info.get("sector"),
            )
        return out

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _yf():
        try:
            import yfinance as yf
        except ImportError as exc:  # pragma: no cover - depends on host install
            raise ProviderError(
                "yfinance is not installed. Install the extra: "
                "pip install -e '.[yfinance]', or set FW_STOCKS_PROVIDER=stub."
            ) from exc
        return yf


def _as_float(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # drop NaN


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
