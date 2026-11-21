"""Group-lifecycle tests against mongomock and the stub provider.

Exercises the whole open -> snapshot -> close path with no network and no real
MongoDB, so the invariants that matter operationally (snapshot idempotency per
trading day, pinned tickers, audit trail completeness) are covered offline.
"""

from __future__ import annotations

import datetime

import mongomock
import pytest

from stocks import db as dbmod
from stocks import portfolio, service
from stocks.providers import Quote


@pytest.fixture(autouse=True)
def stub_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("FW_STOCKS_PROVIDER", "stub")
    monkeypatch.setenv("FW_STOCKS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("FW_STOCKS_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("FW_DATA_ROOT", str(tmp_path))


@pytest.fixture
def db():
    database = mongomock.MongoClient()["facetwork_examples_test"]
    dbmod.ensure_indexes(database)
    return database


def open_small(db, **kwargs):
    params = {
        "name": "Test group",
        "universe": "dow30",
        "size": 5,
        "capital": 50_000.0,
        "horizon_days": 7,
    }
    params.update(kwargs)
    return service.open_group(db, **params)


# --------------------------------------------------------------------------
# open
# --------------------------------------------------------------------------


def test_open_group_persists_group_positions_and_candidates(db):
    result = open_small(db)
    group_id = result["group_id"]

    group = dbmod.get_group(db, group_id)
    assert group["status"] == dbmod.STATUS_OPEN
    assert group["strategy"]["size"] == 5
    assert group["strategy"]["capital"] == 50_000.0
    assert group["benchmark"] == "SPY"

    positions = dbmod.get_positions(db, group_id)
    assert len(positions) == 5
    assert all(p["entry_price"] > 0 and p["shares"] > 0 for p in positions)
    assert sum(p["cost"] for p in positions) == pytest.approx(50_000.0, abs=0.1)

    # The full ranked universe is retained, not just the picks — this is what
    # makes the strategy measurable after the fact.
    candidates = dbmod.get_candidates(db, group_id)
    assert len(candidates) > len(positions)
    assert sum(1 for c in candidates if c["selected"]) == 5
    assert [c["rank"] for c in candidates] == sorted(c["rank"] for c in candidates)


def test_open_group_writes_a_baseline_snapshot(db):
    group_id = open_small(db)["group_id"]
    snaps = dbmod.get_snapshots(db, group_id)
    assert len(snaps) == 1
    assert snaps[0]["kind"] == "open"
    assert snaps[0]["pl_abs"] == pytest.approx(0.0, abs=0.01)
    assert snaps[0]["snapshot_date"] == portfolio.trading_date()


def test_open_group_records_the_strategy_for_reproducibility(db):
    group_id = open_small(db, factor_weights={"mom_3m": 1.0}, use_llm=False)["group_id"]
    group = dbmod.get_group(db, group_id)
    assert group["strategy"]["factor_weights"] == {"mom_3m": 1.0}
    assert "stage_weights" in group["strategy"]
    assert group["provider"] == "stub"


def test_open_group_honours_a_pinned_ticker(db):
    result = open_small(db, size=3, pinned="KO")
    assert "KO" in result["tickers"]
    positions = dbmod.get_positions(db, result["group_id"])
    assert len(positions) == 3
    assert next(p for p in positions if p["ticker"] == "KO")["manual"] is True


def test_open_group_manual_universe_uses_only_pinned(db):
    result = open_small(db, universe="manual", pinned="AAPL,MSFT,KO", size=3)
    assert sorted(result["tickers"]) == ["AAPL", "KO", "MSFT"]


def test_open_group_manual_universe_without_pinned_fails_loudly(db):
    with pytest.raises(service.GroupError, match="pinned"):
        open_small(db, universe="manual", pinned="")


def test_open_group_rejects_bad_size_and_capital(db):
    with pytest.raises(service.GroupError, match="size"):
        open_small(db, size=0)
    with pytest.raises(service.GroupError, match="capital"):
        open_small(db, capital=0.0)


def test_open_group_ids_are_unique(db):
    a = open_small(db)["group_id"]
    b = open_small(db)["group_id"]
    assert a != b
    assert len(dbmod.list_groups(db)) == 2


def test_open_group_step_log_is_called(db):
    messages: list[str] = []
    open_small(db, step_log=lambda msg, level="info": messages.append(msg))
    assert any("Opened" in m for m in messages)
    assert any("Universe" in m for m in messages)


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------


def test_snapshot_is_idempotent_per_trading_day(db):
    group_id = open_small(db)["group_id"]
    service.snapshot_group(db, group_id)
    service.snapshot_group(db, group_id)
    snaps = dbmod.get_snapshots(db, group_id)
    assert len(snaps) == 1  # same trading date => corrected, not duplicated


def test_snapshot_records_a_gain(db, monkeypatch):
    group_id = open_small(db)["group_id"]
    _freeze_quotes(monkeypatch, db, group_id, multiplier=1.10)
    result = service.snapshot_group(db, group_id)
    assert result["pl_pct"] == pytest.approx(0.10, abs=1e-6)
    assert result["total_value"] == pytest.approx(55_000.0, abs=1.0)


def test_snapshot_records_a_loss(db, monkeypatch):
    group_id = open_small(db)["group_id"]
    _freeze_quotes(monkeypatch, db, group_id, multiplier=0.80)
    result = service.snapshot_group(db, group_id)
    assert result["pl_pct"] == pytest.approx(-0.20, abs=1e-6)


def test_snapshot_updates_position_marks(db, monkeypatch):
    group_id = open_small(db)["group_id"]
    _freeze_quotes(monkeypatch, db, group_id, multiplier=1.25)
    service.snapshot_group(db, group_id)
    positions = dbmod.get_positions(db, group_id)
    assert all(p["pl_pct"] == pytest.approx(0.25, abs=1e-6) for p in positions)
    group = dbmod.get_group(db, group_id)
    assert group["current_pl_pct"] == pytest.approx(0.25, abs=1e-6)


def test_snapshot_unknown_group_raises(db):
    with pytest.raises(service.GroupError, match="unknown group"):
        service.snapshot_group(db, "nope")


def test_snapshot_open_groups_covers_every_open_group(db):
    open_small(db, name="A")
    open_small(db, name="B")
    result = service.snapshot_open_groups(db)
    assert result["count"] == 2
    assert result["failed"] == []


def test_snapshot_open_groups_skips_closed_ones(db):
    a = open_small(db, name="A")["group_id"]
    open_small(db, name="B")
    service.close_group(db, a)
    assert service.snapshot_open_groups(db)["count"] == 1


def test_snapshot_open_groups_isolates_a_failing_group(db, monkeypatch):
    good = open_small(db, name="Good")["group_id"]
    bad = open_small(db, name="Bad")["group_id"]

    original = service.snapshot_group

    def flaky(database, group_id, **kwargs):
        if group_id == bad:
            raise RuntimeError("feed exploded")
        return original(database, group_id, **kwargs)

    monkeypatch.setattr(service, "snapshot_group", flaky)
    result = service.snapshot_open_groups(db)
    assert result["count"] == 1
    assert result["groups"][0]["group_id"] == good
    assert result["failed"][0]["group_id"] == bad


def test_snapshot_open_groups_raises_when_everything_fails(db, monkeypatch):
    open_small(db)
    monkeypatch.setattr(
        service, "snapshot_group", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    with pytest.raises(service.GroupError, match="every snapshot failed"):
        service.snapshot_open_groups(db)


def test_snapshot_with_no_open_groups_is_a_noop(db):
    assert service.snapshot_open_groups(db)["count"] == 0


# --------------------------------------------------------------------------
# close
# --------------------------------------------------------------------------


def test_close_group_records_final_pl_and_exits(db, monkeypatch):
    group_id = open_small(db)["group_id"]
    _freeze_quotes(monkeypatch, db, group_id, multiplier=1.20)

    result = service.close_group(db, group_id)
    assert result["pl_pct"] == pytest.approx(0.20, abs=1e-6)
    assert result["winners"] == 5

    group = dbmod.get_group(db, group_id)
    assert group["status"] == dbmod.STATUS_CLOSED
    assert group["closed_at"] is not None
    assert group["final"]["pl_abs"] == pytest.approx(10_000.0, abs=1.0)
    assert group["final"]["winners"] == 5
    assert group["final"]["losers"] == 0
    assert group["final"]["flat"] == 0

    positions = dbmod.get_positions(db, group_id)
    assert all(p.get("exit_price") for p in positions)
    assert all(p["pl_pct"] == pytest.approx(0.20, abs=1e-6) for p in positions)


def test_close_group_computes_alpha_against_the_benchmark(db, monkeypatch):
    group_id = open_small(db)["group_id"]
    # Portfolio +20%, benchmark +5% => alpha +15pp.
    _freeze_quotes(monkeypatch, db, group_id, multiplier=1.20, benchmark_multiplier=1.05)
    service.close_group(db, group_id)
    final = dbmod.get_group(db, group_id)["final"]
    assert final["benchmark_pl_pct"] == pytest.approx(0.05, abs=1e-6)
    assert final["alpha"] == pytest.approx(0.15, abs=1e-6)


def test_close_group_counts_winners_and_losers(db, monkeypatch):
    group_id = open_small(db)["group_id"]
    positions = dbmod.get_positions(db, group_id)
    # First two up, the rest down.
    prices = {
        p["ticker"]: p["entry_price"] * (1.5 if i < 2 else 0.5) for i, p in enumerate(positions)
    }
    _patch_quotes(monkeypatch, prices)
    service.close_group(db, group_id)
    final = dbmod.get_group(db, group_id)["final"]
    assert final["winners"] == 2
    assert final["losers"] == 3
    assert final["flat"] == 0


def test_close_group_counts_a_flat_position_as_neither(db, monkeypatch):
    group_id = open_small(db)["group_id"]
    _freeze_quotes(monkeypatch, db, group_id, multiplier=1.0)  # unchanged prices
    service.close_group(db, group_id)
    final = dbmod.get_group(db, group_id)["final"]
    assert final["winners"] == 0
    assert final["losers"] == 0
    assert final["flat"] == 5


def test_close_group_twice_raises(db):
    group_id = open_small(db)["group_id"]
    service.close_group(db, group_id)
    with pytest.raises(service.GroupError, match="already closed"):
        service.close_group(db, group_id)


def test_close_unknown_group_raises(db):
    with pytest.raises(service.GroupError, match="unknown group"):
        service.close_group(db, "nope")


def test_close_group_records_held_days(db):
    group_id = open_small(db)["group_id"]
    dbmod.set_group_fields(
        db,
        group_id,
        {"opened_at": datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=7)},
    )
    service.close_group(db, group_id)
    assert dbmod.get_group(db, group_id)["final"]["held_days"] == 7


# --------------------------------------------------------------------------
# db helpers
# --------------------------------------------------------------------------


def test_list_groups_filters_by_status(db):
    a = open_small(db, name="A")["group_id"]
    open_small(db, name="B")
    service.close_group(db, a)
    assert len(dbmod.list_open_groups(db)) == 1
    assert len(dbmod.list_groups(db, status=dbmod.STATUS_CLOSED)) == 1
    assert len(dbmod.list_groups(db)) == 2


def test_delete_group_removes_every_trace(db):
    group_id = open_small(db)["group_id"]
    dbmod.delete_group(db, group_id)
    assert dbmod.get_group(db, group_id) is None
    assert dbmod.get_positions(db, group_id) == []
    assert dbmod.get_snapshots(db, group_id) == []
    assert dbmod.get_candidates(db, group_id) == []


def test_snapshot_series_grows_across_trading_days(db, monkeypatch):
    """A multi-day P/L series is what the progress chart draws."""
    group_id = open_small(db)["group_id"]
    # Advance forward from today so the baseline snapshot stays the earliest
    # entry in the series; back-dating would sort it last. Steps of a week keep
    # the weekday, so a run on a weekend does not file two marks under Friday.
    base = datetime.datetime.now(datetime.UTC)
    for offset, mult in enumerate([1.01, 1.03, 0.99, 1.05], start=1):
        _freeze_quotes(monkeypatch, db, group_id, multiplier=mult)
        monkeypatch.setattr(
            service, "_now", lambda b=base, o=offset: b + datetime.timedelta(days=7 * o)
        )
        service.snapshot_group(db, group_id)

    snaps = dbmod.get_snapshots(db, group_id)
    assert len(snaps) == 5  # baseline + four days
    dates = [s["snapshot_date"] for s in snaps]
    assert dates == sorted(dates)
    assert len(set(dates)) == 5
    assert snaps[0]["pl_pct"] == pytest.approx(0.0, abs=1e-6)  # baseline
    assert snaps[-1]["pl_pct"] == pytest.approx(0.05, abs=1e-6)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _patch_quotes(monkeypatch, prices: dict[str, float], benchmark_price: float | None = None):
    as_of = "2026-03-02T21:00:00+00:00"

    def fake(tickers, provider=None):
        out = {t: Quote(ticker=t, price=prices[t], as_of=as_of) for t in tickers if t in prices}
        if benchmark_price is not None and "SPY" in tickers:
            out["SPY"] = Quote(ticker="SPY", price=benchmark_price, as_of=as_of)
        return out

    monkeypatch.setattr(service.market, "fetch_quotes", fake)


def _freeze_quotes(
    monkeypatch, db, group_id, *, multiplier: float, benchmark_multiplier: float = 1.0
):
    """Re-price every position at ``entry * multiplier``."""
    positions = dbmod.get_positions(db, group_id)
    prices = {p["ticker"]: p["entry_price"] * multiplier for p in positions}
    group = dbmod.get_group(db, group_id)
    bench_entry = group.get("benchmark_entry") or 0.0
    _patch_quotes(monkeypatch, prices, benchmark_price=bench_entry * benchmark_multiplier)


# --------------------------------------------------------------------------
# sector selection
# --------------------------------------------------------------------------


def _sector_of(db, group_id):
    from stocks.providers.stub_provider import StubProvider

    mapping = StubProvider().sector_map()
    return {p["ticker"]: mapping.get(p["ticker"], "") for p in dbmod.get_positions(db, group_id)}


def test_open_group_restricted_to_one_sector(db):
    from stocks.providers.stub_provider import StubProvider

    mapping = StubProvider().sector_map()
    target = max(set(mapping.values()), key=lambda s: list(mapping.values()).count(s))

    result = open_small(db, universe="sp500", sectors=target, size=3)
    picked = _sector_of(db, result["group_id"])
    assert picked, "no positions were opened"
    assert set(picked.values()) == {target}


def test_open_group_accepts_everyday_sector_names(db):
    result = open_small(db, universe="sp500", sectors="tech", size=2)
    picked = _sector_of(db, result["group_id"])
    assert set(picked.values()) == {"Information Technology"}
    group = dbmod.get_group(db, result["group_id"])
    # Stored canonically, so the run stays reproducible.
    assert group["strategy"]["sectors"] == ["Information Technology"]


def test_open_group_combination_of_sectors(db):
    result = open_small(db, universe="sp500", sectors="tech, medical", size=4)
    picked = _sector_of(db, result["group_id"])
    assert set(picked.values()) <= {"Information Technology", "Health Care"}


def test_open_group_rejects_an_ambiguous_sector(db):
    from stocks.sectors import SectorError

    with pytest.raises(SectorError, match="ambiguous"):
        open_small(db, sectors="services")


def test_open_group_rejects_an_unknown_sector(db):
    from stocks.sectors import SectorError

    with pytest.raises(SectorError, match="Unknown sector"):
        open_small(db, sectors="crypto")


def test_open_group_fails_when_no_ticker_matches_the_sector(db, monkeypatch):
    monkeypatch.setattr(
        "stocks.market.resolve_sector_map", lambda provider=None: {"AAPL": "Energy"}
    )
    with pytest.raises(service.GroupError, match="no ticker in universe"):
        open_small(db, universe="sp500", sectors="Utilities")


def test_sector_filter_is_not_silently_ignored_without_sector_data(db, monkeypatch):
    """Better to fail than to hand back a group from every sector."""
    monkeypatch.setattr("stocks.market.resolve_sector_map", lambda provider=None: {})
    with pytest.raises(service.GroupError, match="no sector data"):
        open_small(db, sectors="tech")


def test_pinned_ticker_survives_a_sector_filter_it_does_not_match(db):
    """An explicit request outranks the filter."""
    from stocks.providers.stub_provider import StubProvider

    mapping = StubProvider().sector_map()
    tech = [t for t, s in mapping.items() if s == "Information Technology"]
    other = next(t for t, s in mapping.items() if s != "Information Technology")

    result = open_small(db, universe="sp500", sectors="tech", size=3, pinned=other)
    tickers = {p["ticker"] for p in dbmod.get_positions(db, result["group_id"])}
    assert other in tickers
    assert tickers - {other} <= set(tech)


def test_max_per_sector_diversifies_the_group(db):
    result = open_small(db, universe="sp500", size=8, max_per_sector=1)
    picked = _sector_of(db, result["group_id"])
    counts: dict[str, int] = {}
    for sector in picked.values():
        if sector:
            counts[sector] = counts.get(sector, 0) + 1
    assert counts, "no classified positions"
    assert max(counts.values()) == 1


def test_max_per_sector_is_recorded_on_the_group(db):
    result = open_small(db, universe="sp500", size=4, max_per_sector=2)
    group = dbmod.get_group(db, result["group_id"])
    assert group["strategy"]["max_per_sector"] == 2
    assert group["strategy"]["sectors"] == []


def test_sector_cap_shortfall_is_reported_not_hidden(db):
    messages: list[tuple[str, str]] = []
    open_small(
        db,
        universe="dow30",
        size=30,
        max_per_sector=1,
        step_log=lambda msg, level="info": messages.append((msg, level)),
    )
    warnings = [m for m, lvl in messages if lvl == "warning" and "Sector cap" in m]
    assert warnings, f"no shortfall warning in {[m for m, _ in messages]}"


def test_no_sector_options_means_no_sector_lookup(db, monkeypatch):
    """Sector data must not be fetched when nothing asks for it."""
    called = []
    monkeypatch.setattr(
        "stocks.market.resolve_sector_map",
        lambda provider=None: called.append(1) or {},
    )
    open_small(db, universe="dow30", size=2)
    assert called == []
