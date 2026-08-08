"""Group lifecycle — open, snapshot, close.

The one place that composes provider + screener + selection + portfolio + Mongo.
Handlers are thin wrappers over these three functions; the dashboard never calls
them directly (it submits FFL workflows), so this module is the single
implementation of what a group *is*.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from . import db as dbmod
from . import market, portfolio, screener, selection
from .sectors import resolve_sectors

logger = logging.getLogger("stocks.service")

DEFAULT_UNIVERSE = "sp500"
DEFAULT_SIZE = 10
DEFAULT_CAPITAL = 100_000.0
DEFAULT_HORIZON_DAYS = 7
DEFAULT_BENCHMARK = "SPY"
DEFAULT_LOOKBACK_DAYS = 400


class GroupError(RuntimeError):
    """A group operation could not complete. Raised, never swallowed."""


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value) -> datetime | None:
    """Normalise a datetime read back from MongoDB to UTC-aware.

    pymongo stores datetimes as naive UTC and returns them that way, so any
    arithmetic against a ``datetime.now(timezone.utc)`` would raise "can't
    subtract offset-naive and offset-aware datetimes". Normalising on read is
    the fix; the alternative (storing strings) loses range queries.
    """
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _group_id(name: str) -> str:
    slug = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")[:40]
    return f"{slug or 'group'}-{uuid.uuid4().hex[:8]}"


def _parse_tickers(raw: str | list[str] | None) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        parts = raw.replace(";", ",").replace(" ", ",").split(",")
    else:
        parts = list(raw)
    return [p.strip().upper() for p in parts if p and p.strip()]


# --------------------------------------------------------------------------
# Open
# --------------------------------------------------------------------------


def open_group(
    db,
    *,
    name: str,
    universe: str = DEFAULT_UNIVERSE,
    size: int = DEFAULT_SIZE,
    capital: float = DEFAULT_CAPITAL,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    benchmark: str = DEFAULT_BENCHMARK,
    pinned: str | list[str] | None = None,
    use_llm: bool = False,
    sectors: str | list[str] | None = None,
    max_per_sector: int = 0,
    factor_weights: dict[str, float] | None = None,
    stage_weights: dict[str, float] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    force_refresh: bool = False,
    step_log=None,
) -> dict:
    """Select a group, price it, and persist it as ``open``.

    Writes the group, its positions, and the FULL ranked candidate list — the
    candidates are what make the strategy measurable after the fact.
    """

    def log(message: str, level: str = "info") -> None:
        logger.info(message)
        if step_log:
            step_log(message, level=level)

    if size < 1:
        raise GroupError(f"size must be at least 1, got {size}")
    if capital <= 0:
        raise GroupError(f"capital must be positive, got {capital}")

    provider = market.get_provider()
    pinned_list = _parse_tickers(pinned)

    universe_tickers = (
        pinned_list if universe == "manual" else market.resolve_universe(universe, provider)
    )
    if not universe_tickers:
        raise GroupError(
            "manual universe requires at least one pinned ticker"
            if universe == "manual"
            else f"universe {universe!r} resolved to no tickers"
        )
    # Sector data classifies the whole universe in one cached call, so the
    # filter runs BEFORE the expensive bar download rather than after it: asking
    # for Health Care fetches ~59 symbols, not 503.
    wanted_sectors = resolve_sectors(sectors)
    sector_of = market.resolve_sector_map(provider) if (wanted_sectors or max_per_sector) else {}

    if wanted_sectors:
        if not sector_of:
            raise GroupError(
                "sector filtering was requested but no sector data is available from "
                f"provider {provider.name!r} - refusing to silently ignore the filter"
            )
        allowed = set(wanted_sectors)
        kept = [t for t in universe_tickers if sector_of.get(t) in allowed]
        unclassified = [t for t in universe_tickers if t not in sector_of]
        if not kept:
            raise GroupError(
                f"no ticker in universe {universe!r} is in {', '.join(wanted_sectors)}"
            )
        log(
            f"Sector filter {', '.join(wanted_sectors)}: {len(kept)} of "
            f"{len(universe_tickers)} tickers kept"
            + (f" ({len(unclassified)} unclassified, excluded)" if unclassified else "")
        )
        universe_tickers = kept

    # Pinned names must be priced even when they sit outside the universe or the
    # chosen sectors — an explicit request outranks the filter.
    universe_tickers = list(dict.fromkeys([*universe_tickers, *pinned_list]))
    log(f"Universe {universe}: {len(universe_tickers)} tickers (provider={provider.name})")

    bars = market.fetch_daily_bars(
        universe_tickers,
        lookback_days=lookback_days,
        provider=provider,
        benchmark=benchmark,
        force=force_refresh,
    )
    log(f"Fetched daily bars for {len(bars)} symbols")

    result = screener.screen(bars, benchmark=benchmark, weights=factor_weights)
    if not result.ranked:
        raise GroupError(
            f"no candidate passed the hard filters ({len(result.excluded)} excluded) — "
            "check the price feed before trusting this"
        )
    log(f"Screen: {len(result.ranked)} passed, {len(result.excluded)} filtered out")

    shortlist = [c.ticker for c in result.top(selection.ANALYST_SHORTLIST)]
    try:
        views = market.fetch_analyst(shortlist, provider=provider, force=force_refresh)
        log(f"Analyst consensus for {len(views)} of {len(shortlist)} shortlisted")
    except Exception as exc:  # noqa: BLE001 - a bonus signal, not a hard dependency
        log(f"Analyst lookup failed, continuing on the factor screen alone: {exc}", level="warning")
        views = {}

    llm_adjustments: dict[str, tuple[float, str]] = {}
    if use_llm:
        preliminary = selection.rank_candidates(
            result.ranked,
            analyst_views=views,
            stage_weights=stage_weights,
            size=size,
            sector_of=sector_of,
        )
        llm_adjustments = selection.llm_rerank(preliminary[: selection.LLM_SHORTLIST])
        log(f"LLM re-rank returned {len(llm_adjustments)} adjustments")

    scored = selection.rank_candidates(
        result.ranked,
        analyst_views=views,
        llm_adjustments=llm_adjustments,
        stage_weights=stage_weights,
        size=size,
        pinned=pinned_list,
        sector_of=sector_of,
        max_per_sector=max_per_sector,
    )
    picks = [c.to_dict() for c in scored if c.selected]
    if not picks:
        raise GroupError("selection produced no picks")
    if max_per_sector and len(picks) < size:
        # Say so rather than letting the group look like the screen ran dry.
        log(
            f"Sector cap of {max_per_sector} per sector filled {len(picks)} of "
            f"{size} slots; the group is smaller by design.",
            level="warning",
        )

    quotes = market.fetch_quotes([p["ticker"] for p in picks] + [benchmark], provider=provider)
    positions, skipped = portfolio.size_positions(picks, capital, quotes)
    if not positions:
        raise GroupError(f"no pick could be priced (skipped: {', '.join(skipped) or 'none'})")
    if skipped:
        log(f"Skipped (no quote): {', '.join(skipped)}", level="warning")

    benchmark_quote = quotes.get(benchmark)
    opened_at = _now()
    group_id = _group_id(name)

    group = {
        "group_id": group_id,
        "name": name,
        "status": dbmod.STATUS_OPEN,
        "created_at": opened_at,
        "opened_at": opened_at,
        "closed_at": None,
        "horizon_days": int(horizon_days),
        "target_close": opened_at + timedelta(days=int(horizon_days)),
        "benchmark": benchmark,
        "benchmark_entry": round(benchmark_quote.price, 4) if benchmark_quote else None,
        "provider": provider.name,
        "strategy": {
            "universe": universe,
            "size": int(size),
            "capital": float(capital),
            "use_llm": bool(use_llm),
            "sectors": wanted_sectors,
            "max_per_sector": int(max_per_sector),
            "factor_weights": result.weights,
            "stage_weights": {**selection.DEFAULT_STAGE_WEIGHTS, **(stage_weights or {})},
            "lookback_days": int(lookback_days),
            "pinned": pinned_list,
        },
        "universe_size": len(universe_tickers),
        "screened_count": len(result.ranked),
        "excluded_count": len(result.excluded),
        "position_count": len(positions),
        "skipped": skipped,
        "initial_value": round(sum(p["cost"] for p in positions), 2),
    }

    dbmod.upsert_group(db, group)
    dbmod.replace_positions(db, group_id, positions)
    dbmod.replace_candidates(db, group_id, [c.to_dict() for c in scored])

    # Snapshot zero — the baseline every later P/L is measured against.
    snapshot = portfolio.build_snapshot(
        positions,
        quotes,
        benchmark_entry=group["benchmark_entry"],
        benchmark_quote=benchmark_quote,
    )
    dbmod.upsert_snapshot(
        db,
        group_id,
        portfolio.trading_date(opened_at),
        {
            **snapshot.to_dict(),
            "taken_at": opened_at,
            "source": provider.name,
            "kind": "open",
        },
    )

    log(f"Opened {group_id}: {len(positions)} positions, ${group['initial_value']:,.2f}", "success")
    return {
        "group_id": group_id,
        "name": name,
        "position_count": len(positions),
        "initial_value": group["initial_value"],
        "tickers": [p["ticker"] for p in positions],
    }


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------


def snapshot_group(db, group_id: str, *, step_log=None, kind: str = "interim") -> dict:
    """Price an open group and upsert today's snapshot."""
    group = dbmod.get_group(db, group_id)
    if group is None:
        raise GroupError(f"unknown group {group_id!r}")
    positions = dbmod.get_positions(db, group_id)
    if not positions:
        raise GroupError(f"group {group_id!r} has no positions")

    provider = market.get_provider()
    benchmark = group.get("benchmark") or DEFAULT_BENCHMARK
    quotes = market.fetch_quotes([p["ticker"] for p in positions] + [benchmark], provider=provider)

    snapshot = portfolio.build_snapshot(
        positions,
        quotes,
        benchmark_entry=group.get("benchmark_entry"),
        benchmark_quote=quotes.get(benchmark),
    )
    taken_at = _now()
    dbmod.upsert_snapshot(
        db,
        group_id,
        portfolio.trading_date(taken_at),
        {**snapshot.to_dict(), "taken_at": taken_at, "source": provider.name, "kind": kind},
    )

    # Carry the marks onto the positions so the table renders without a join.
    for holding in snapshot.holdings:
        dbmod.update_position(
            db,
            group_id,
            holding.ticker,
            {
                "last_price": round(holding.price, 4),
                "pl_abs": round(holding.pl_abs, 2),
                "pl_pct": round(holding.pl_pct, 4),
            },
        )
    dbmod.set_group_fields(
        db,
        group_id,
        {
            "last_snapshot_at": taken_at,
            "current_value": round(snapshot.total_value, 2),
            "current_pl_abs": round(snapshot.pl_abs, 2),
            "current_pl_pct": round(snapshot.pl_pct, 4),
        },
    )

    message = f"Snapshot {group_id}: ${snapshot.total_value:,.2f} ({snapshot.pl_pct * 100:+.2f}%)"
    if snapshot.missing:
        message += f" — no quote for {', '.join(snapshot.missing)}"
    logger.info(message)
    if step_log:
        step_log(message, level="warning" if snapshot.missing else "success")

    return {
        "group_id": group_id,
        "snapshot_date": portfolio.trading_date(taken_at),
        "total_value": round(snapshot.total_value, 2),
        "pl_abs": round(snapshot.pl_abs, 2),
        "pl_pct": round(snapshot.pl_pct, 4),
        "benchmark_pl_pct": snapshot.benchmark_pl_pct,
        "missing": snapshot.missing,
    }


def snapshot_open_groups(db, group_id: str = "", *, step_log=None) -> dict:
    """Snapshot one group, or every open group when *group_id* is blank.

    One group failing does not abort the rest — a delisted symbol in one group
    must not stop the others from recording their day.
    """
    groups = [dbmod.get_group(db, group_id)] if group_id else dbmod.list_open_groups(db)
    groups = [g for g in groups if g]
    if group_id and not groups:
        raise GroupError(f"unknown group {group_id!r}")

    done, failed = [], []
    for group in groups:
        try:
            done.append(snapshot_group(db, group["group_id"], step_log=step_log))
        except Exception as exc:  # noqa: BLE001 - isolate per group
            failed.append({"group_id": group["group_id"], "error": str(exc)})
            logger.warning("snapshot failed for %s: %s", group["group_id"], exc)
            if step_log:
                step_log(f"Snapshot failed for {group['group_id']}: {exc}", level="error")

    if groups and not done:
        raise GroupError(
            f"every snapshot failed ({len(failed)} groups) — the price feed is likely down"
        )
    return {"count": len(done), "groups": done, "failed": failed}


# --------------------------------------------------------------------------
# Close
# --------------------------------------------------------------------------


def close_group(db, group_id: str, *, step_log=None) -> dict:
    """Take a final snapshot, stamp exit prices and realised P/L, mark closed."""
    group = dbmod.get_group(db, group_id)
    if group is None:
        raise GroupError(f"unknown group {group_id!r}")
    if group.get("status") == dbmod.STATUS_CLOSED:
        raise GroupError(f"group {group_id!r} is already closed")

    snapshot_group(db, group_id, step_log=step_log, kind="close")

    positions = dbmod.get_positions(db, group_id)
    final = dbmod.get_latest_snapshot(db, group_id) or {}
    closed_at = _now()

    total_cost = float(final.get("total_cost") or 0.0)
    total_value = float(final.get("total_value") or 0.0)
    pl_abs = total_value - total_cost
    pl_pct = portfolio.pct(pl_abs, total_cost)
    benchmark_pl_pct = final.get("benchmark_pl_pct")

    for pos in positions:
        holding = next((h for h in final.get("holdings", []) if h["ticker"] == pos["ticker"]), None)
        if holding is None:
            continue
        dbmod.update_position(
            db,
            group_id,
            pos["ticker"],
            {
                "exit_price": holding["price"],
                "exit_at": final.get("as_of"),
                "pl_abs": holding["pl_abs"],
                "pl_pct": holding["pl_pct"],
            },
        )

    holdings = final.get("holdings", [])
    # Flat is neither a win nor a loss — deriving losers as (total - winners)
    # would report an unchanged position as a loser.
    winners = sum(1 for h in holdings if h["pl_abs"] > 0)
    losers = sum(1 for h in holdings if h["pl_abs"] < 0)
    opened_at = _as_utc(group.get("opened_at"))
    dbmod.set_group_fields(
        db,
        group_id,
        {
            "status": dbmod.STATUS_CLOSED,
            "closed_at": closed_at,
            "final": {
                "total_cost": round(total_cost, 2),
                "total_value": round(total_value, 2),
                "pl_abs": round(pl_abs, 2),
                "pl_pct": round(pl_pct, 4),
                "benchmark_pl_pct": benchmark_pl_pct,
                # Excess return vs the benchmark. This is the number that says
                # whether the strategy was worth running at all.
                "alpha": (
                    round(pl_pct - benchmark_pl_pct, 4) if benchmark_pl_pct is not None else None
                ),
                "winners": winners,
                "losers": losers,
                "flat": len(holdings) - winners - losers,
                "held_days": (closed_at - opened_at).days if opened_at else None,
            },
        },
    )

    message = f"Closed {group_id}: {pl_pct * 100:+.2f}% (${pl_abs:,.2f}), {winners} winners"
    logger.info(message)
    if step_log:
        step_log(message, level="success")

    return {
        "group_id": group_id,
        "pl_abs": round(pl_abs, 2),
        "pl_pct": round(pl_pct, 4),
        "benchmark_pl_pct": benchmark_pl_pct,
        "winners": winners,
    }
