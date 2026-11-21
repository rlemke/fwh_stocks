"""The static pages: built from the database, deterministic, honest about gaps."""

from __future__ import annotations

import mongomock
import pytest

from stocks import service, site


@pytest.fixture(autouse=True)
def _stub(monkeypatch, tmp_path):
    monkeypatch.setenv("FW_STOCKS_PROVIDER", "stub")
    monkeypatch.setenv("FW_STOCKS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("FW_STOCKS_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("FW_DATA_ROOT", str(tmp_path))


@pytest.fixture
def db():
    from stocks import db as dbmod

    database = mongomock.MongoClient()["stocks_site_test"]
    dbmod.ensure_indexes(database)
    return database


def _open(db):
    return service.open_group(
        db, name="Site test", universe="dow30", size=4, capital=40_000.0, horizon_days=7
    )


def test_build_writes_an_index_and_a_page_per_group(db, tmp_path):
    gid = _open(db)["group_id"]
    out = site.build_site(db, tmp_path / "site")
    tmp_path = tmp_path / "site"
    assert out["files"] == 2 and out["groups"] == 1 and out["open"] == 1
    index = (tmp_path / "index.html").read_text()
    page = (tmp_path / f"g/{gid}.html").read_text()
    assert f'href="g/{gid}.html"' in index
    assert "Paper trading only, not investment advice" in page
    assert "What the columns mean" in index


def test_build_is_deterministic(db, tmp_path):
    _open(db)
    site.build_site(db, tmp_path / "a")
    site.build_site(db, tmp_path / "b")
    pages = list((tmp_path / "a").rglob("*.html"))
    assert pages
    for p in pages:
        assert p.read_bytes() == (tmp_path / "b" / p.relative_to(tmp_path / "a")).read_bytes()


def test_build_refuses_an_empty_database(db, tmp_path):
    with pytest.raises(RuntimeError):
        site.build_site(db, tmp_path / "site")


def test_chart_starts_at_zero_on_the_entry_session_and_spaces_by_date():
    snaps = [
        {"snapshot_date": "2026-09-01", "pl_pct": 0.01, "benchmark_pl_pct": 0.0},
        {"snapshot_date": "2026-09-02", "pl_pct": 0.02, "benchmark_pl_pct": 0.01},
        {"snapshot_date": "2026-09-30", "pl_pct": 0.03, "benchmark_pl_pct": 0.02},
    ]
    svg = site.chart_svg(snaps, "G", "SPY", start="2026-08-31")
    pts = svg.split('points="')[1].split('"')[0].split()
    xs = [float(p.split(",")[0]) for p in pts]
    assert len(xs) == 4
    # 1 day, 1 day, then 28 days: the long gap is long on the page too.
    assert (xs[3] - xs[2]) == pytest.approx(28 * (xs[2] - xs[1]), abs=3.0)  # points are written to 0.1 px


def test_one_snapshot_has_no_chart():
    assert "<svg" not in site.chart_svg(
        [{"snapshot_date": "2026-09-01", "pl_pct": 0.0}], "G", "SPY"
    )


def test_handler_writes_pages_to_the_output_store(db, tmp_path, monkeypatch):
    from stocks.handlers import stocks_handlers as h

    _open(db)
    monkeypatch.setattr(h, "_db", lambda: db)
    logs = []
    out = h.handle(
        {
            "_facet_name": "stocks.site.Build",
            "_step_log": lambda m, level="info": logs.append(level),
        }
    )
    assert out["files"] == 2
    assert out["output_dir"].endswith("site")
    assert logs == ["success"]
