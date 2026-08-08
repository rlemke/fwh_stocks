"""Position sizing and profit/loss math — pure functions, no I/O.

Everything here is notional. The tracker records what a hypothetical
equal-weight purchase would have been worth; it places no orders.

Two conventions worth stating because they are easy to get wrong:

* **Fractional shares.** Equal-weight with whole shares leaves an uneven cash
  residual per position, which silently distorts the weights and the P/L
  attribution. Fractional shares make ``weight`` exact and the arithmetic
  auditable.
* **US/Eastern trading dates.** A 16:30 ET snapshot is 20:30 or 21:30 UTC
  depending on daylight saving, so keying snapshots on the UTC date would file
  half the year's closes under the wrong day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .providers import Quote

MARKET_TZ = ZoneInfo("America/New_York")

__all__ = [
    "Holding",
    "SnapshotResult",
    "trading_date",
    "utc_now_iso",
    "size_positions",
    "build_snapshot",
    "close_positions",
    "pct",
]


@dataclass(frozen=True)
class Holding:
    ticker: str
    price: float
    shares: float
    entry_price: float
    value: float
    cost: float
    pl_abs: float
    pl_pct: float

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "price": round(self.price, 4),
            "shares": round(self.shares, 6),
            "entry_price": round(self.entry_price, 4),
            "value": round(self.value, 2),
            "cost": round(self.cost, 2),
            "pl_abs": round(self.pl_abs, 2),
            "pl_pct": round(self.pl_pct, 4),
        }


@dataclass(frozen=True)
class SnapshotResult:
    holdings: list[Holding]
    total_cost: float
    total_value: float
    pl_abs: float
    pl_pct: float
    benchmark_price: float | None
    benchmark_pl_pct: float | None
    as_of: str
    missing: list[str]

    def to_dict(self) -> dict:
        return {
            "holdings": [h.to_dict() for h in self.holdings],
            "total_cost": round(self.total_cost, 2),
            "total_value": round(self.total_value, 2),
            "pl_abs": round(self.pl_abs, 2),
            "pl_pct": round(self.pl_pct, 4),
            "benchmark_price": (
                round(self.benchmark_price, 4) if self.benchmark_price is not None else None
            ),
            "benchmark_pl_pct": (
                round(self.benchmark_pl_pct, 4) if self.benchmark_pl_pct is not None else None
            ),
            "as_of": self.as_of,
            "missing": list(self.missing),
        }


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def trading_date(moment: datetime | None = None) -> str:
    """The US/Eastern calendar date of *moment* (default: now), as ISO yyyy-mm-dd.

    This is the snapshot key. It is a calendar date, not an assertion that the
    market was open — a holiday snapshot simply records the prior close, and the
    upsert makes a repeat harmless.
    """
    moment = moment or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(MARKET_TZ).date().isoformat()


def pct(numerator: float, denominator: float) -> float:
    """Fractional change, guarding the zero-cost case."""
    if not denominator:
        return 0.0
    return numerator / denominator


def size_positions(
    picks: list[dict],
    capital: float,
    quotes: dict[str, Quote],
) -> tuple[list[dict], list[str]]:
    """Turn ranked picks into equal-weight notional positions.

    Returns ``(positions, skipped)``. A pick with no quote is skipped and named
    rather than silently dropped — the caller reports it, and the remaining
    capital is spread over the names that *did* price, so the group is still
    fully invested and its weights still sum to 1.
    """

    def _has_price(pick: dict) -> bool:
        quote = quotes.get(pick["ticker"])
        return bool(quote and quote.price > 0)

    priced = [p for p in picks if _has_price(p)]
    skipped = [p["ticker"] for p in picks if not _has_price(p)]
    if not priced:
        return [], skipped

    weight = 1.0 / len(priced)
    allocation = capital * weight

    positions: list[dict] = []
    for pick in priced:
        quote = quotes[pick["ticker"]]
        shares = allocation / quote.price
        positions.append(
            {
                **pick,
                "entry_price": round(quote.price, 4),
                "entry_at": quote.as_of,
                "shares": round(shares, 6),
                "weight": round(weight, 6),
                "cost": round(shares * quote.price, 2),
                "last_price": round(quote.price, 4),
            }
        )
    return positions, skipped


def build_snapshot(
    positions: list[dict],
    quotes: dict[str, Quote],
    *,
    benchmark_entry: float | None = None,
    benchmark_quote: Quote | None = None,
    as_of: str | None = None,
) -> SnapshotResult:
    """Value every position at the current quote and roll up group P/L.

    A position with no fresh quote is carried at its last known price and named
    in ``missing``, so the group total stays complete instead of collapsing
    toward zero when one symbol fails to price.
    """
    holdings: list[Holding] = []
    missing: list[str] = []
    total_cost = 0.0
    total_value = 0.0

    for pos in positions:
        ticker = pos["ticker"]
        entry = float(pos["entry_price"])
        shares = float(pos["shares"])
        quote = quotes.get(ticker)
        if quote and quote.price > 0:
            price = float(quote.price)
        else:
            price = float(pos.get("last_price") or entry)
            missing.append(ticker)

        cost = shares * entry
        value = shares * price
        pl_abs = value - cost
        holdings.append(
            Holding(
                ticker=ticker,
                price=price,
                shares=shares,
                entry_price=entry,
                value=value,
                cost=cost,
                pl_abs=pl_abs,
                pl_pct=pct(pl_abs, cost),
            )
        )
        total_cost += cost
        total_value += value

    benchmark_price = benchmark_quote.price if benchmark_quote else None
    benchmark_pl_pct = None
    if benchmark_price and benchmark_entry:
        benchmark_pl_pct = pct(benchmark_price - benchmark_entry, benchmark_entry)

    pl_abs = total_value - total_cost
    return SnapshotResult(
        holdings=holdings,
        total_cost=total_cost,
        total_value=total_value,
        pl_abs=pl_abs,
        pl_pct=pct(pl_abs, total_cost),
        benchmark_price=benchmark_price,
        benchmark_pl_pct=benchmark_pl_pct,
        as_of=as_of or utc_now_iso(),
        missing=missing,
    )


def close_positions(positions: list[dict], snapshot: SnapshotResult) -> list[dict]:
    """Stamp exit prices and realised P/L onto each position from the final snapshot."""
    by_ticker = {h.ticker: h for h in snapshot.holdings}
    closed: list[dict] = []
    for pos in positions:
        holding = by_ticker.get(pos["ticker"])
        if holding is None:
            continue
        closed.append(
            {
                **pos,
                "exit_price": round(holding.price, 4),
                "exit_at": snapshot.as_of,
                "last_price": round(holding.price, 4),
                "pl_abs": round(holding.pl_abs, 2),
                "pl_pct": round(holding.pl_pct, 4),
            }
        )
    return closed
