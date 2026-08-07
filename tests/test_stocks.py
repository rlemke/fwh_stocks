"""Offline tests for the stocks domain.

Every test here runs with no network and no MongoDB, as the tools-pattern spec
requires: the stub provider supplies deterministic prices and the scoring,
sizing and P/L math are pure functions.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import pytest
from stocks import portfolio, screener, selection
from stocks.providers import AnalystView, Bar, ProviderError, Quote, get_provider
from stocks.providers.stub_provider import StubProvider

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def make_bars(
    closes: list[float], volume: float = 5_000_000.0, high_mult: float = 1.01
) -> list[Bar]:
    """Bars from a close series.

    ``high_mult`` models the intraday high sitting above the close, which is what
    the real 52-week high is. Pass ``1.0`` when a test wants the high to equal the
    close so the expected ratio is exact.
    """
    start = date(2024, 1, 1)
    return [
        Bar(
            date=(start + timedelta(days=i)).isoformat(),
            open=c,
            high=c * high_mult,
            low=c * 0.99,
            close=c,
            volume=volume,
        )
        for i, c in enumerate(closes)
    ]


def flat_bars(n: int, price: float = 100.0, volume: float = 5_000_000.0) -> list[Bar]:
    return make_bars([price] * n, volume=volume)


def trending_bars(n: int, start: float = 100.0, daily: float = 0.001) -> list[Bar]:
    return make_bars([start * (1 + daily) ** i for i in range(n)])


# --------------------------------------------------------------------------
# provider factory + stub
# --------------------------------------------------------------------------


def test_get_provider_rejects_unknown_name_instead_of_falling_back():
    with pytest.raises(ProviderError):
        get_provider("definitely-not-a-provider")


def test_get_provider_honours_env(monkeypatch):
    monkeypatch.setenv("FW_STOCKS_PROVIDER", "stub")
    assert get_provider().name == "stub"


def test_stub_provider_is_deterministic():
    a = StubProvider().daily_bars(["AAPL"], date(2024, 1, 1), date(2025, 1, 1))
    b = StubProvider().daily_bars(["AAPL"], date(2024, 1, 1), date(2025, 1, 1))
    assert [x.close for x in a["AAPL"]] == [x.close for x in b["AAPL"]]


def test_stub_universe_unknown_raises():
    with pytest.raises(ProviderError):
        StubProvider().universe("ftse100")


def test_stub_provides_enough_history_to_screen():
    provider = StubProvider()
    tickers = provider.universe("dow30")
    bars = provider.daily_bars(tickers, date.today() - timedelta(days=560), date.today())
    assert all(len(v) >= screener.MIN_HISTORY_BARS for v in bars.values())


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------


def test_zscores_center_and_scale():
    z = screener.zscores({"a": 1.0, "b": 2.0, "c": 3.0})
    assert z["b"] == pytest.approx(0.0)
    assert z["a"] == pytest.approx(-z["c"])
    assert sum(z.values()) == pytest.approx(0.0)


def test_zscores_zero_spread_is_zero_not_nan():
    z = screener.zscores({"a": 5.0, "b": 5.0})
    assert z == {"a": 0.0, "b": 0.0}
    assert all(not math.isnan(v) for v in z.values())


def test_zscores_empty_input():
    assert screener.zscores({}) == {}


def test_zscores_are_winsorized():
    values = {f"t{i}": 1.0 for i in range(50)}
    values["outlier"] = 10_000.0
    z = screener.zscores(values)
    assert z["outlier"] == pytest.approx(screener.WINSOR_LIMIT)
    assert all(abs(v) <= screener.WINSOR_LIMIT for v in z.values())


# --------------------------------------------------------------------------
# factors
# --------------------------------------------------------------------------


def test_realized_vol_of_flat_series_is_zero():
    assert screener.realized_vol(flat_bars(100)) == pytest.approx(0.0)


def test_realized_vol_is_positive_for_a_moving_series():
    bars = make_bars([100 + (5 if i % 2 else -5) for i in range(100)])
    assert screener.realized_vol(bars) > 0


def test_near_high_ratio_at_the_high_is_one():
    bars = make_bars([100 * 1.001**i for i in range(300)], high_mult=1.0)
    assert screener.near_high_ratio(bars) == pytest.approx(1.0, rel=1e-9)


def test_near_high_ratio_below_the_high():
    bars = make_bars([100] * 100 + [200] + [150] * 50, high_mult=1.0)
    assert screener.near_high_ratio(bars) == pytest.approx(0.75, rel=1e-9)


def test_near_high_ratio_uses_the_intraday_high_not_the_close():
    # A spike that closed well off its high still counts as the 52-week high.
    bars = make_bars([100] * 150, high_mult=1.0)
    bars[50] = Bar(date=bars[50].date, open=100, high=200, low=100, close=100, volume=1e6)
    assert screener.near_high_ratio(bars) == pytest.approx(0.5, rel=1e-9)


def test_avg_dollar_volume():
    assert screener.avg_dollar_volume(flat_bars(30, 10.0, 1_000.0)) == pytest.approx(10_000.0)


def test_momentum_skips_the_most_recent_month():
    # Flat for a year, then a spike inside the final month. 12-1 momentum must
    # not see the spike; 3-month momentum must.
    closes = [100.0] * 260 + [300.0] * 10
    factors = screener.compute_factors(make_bars(closes), None)
    assert factors["mom_12_1"] == pytest.approx(0.0, abs=1e-9)
    assert factors["mom_3m"] > 0


def test_relative_strength_subtracts_the_benchmark():
    stock = trending_bars(300, daily=0.002)
    bench = trending_bars(300, daily=0.001)
    with_bench = screener.compute_factors(stock, bench)["rel_strength"]
    without = screener.compute_factors(stock, None)["rel_strength"]
    assert with_bench < without
    assert with_bench > 0


def test_missing_factors_are_omitted_not_zeroed():
    factors = screener.compute_factors(flat_bars(30), None)
    assert "mom_12_1" not in factors  # not enough history
    assert "near_high" in factors


# --------------------------------------------------------------------------
# hard filters
# --------------------------------------------------------------------------


def test_filter_rejects_short_history():
    reason = screener.apply_hard_filters("X", flat_bars(100))
    assert reason and "history" in reason


def test_filter_rejects_penny_stock():
    reason = screener.apply_hard_filters("X", flat_bars(300, price=1.0, volume=1e9))
    assert reason and "below the" in reason


def test_filter_rejects_illiquid():
    reason = screener.apply_hard_filters("X", flat_bars(300, price=100.0, volume=10.0))
    assert reason and "dollar volume" in reason


def test_filter_passes_a_healthy_name():
    assert screener.apply_hard_filters("X", flat_bars(300, 100.0, 5_000_000.0)) is None


# --------------------------------------------------------------------------
# the screen
# --------------------------------------------------------------------------


def test_screen_ranks_stronger_momentum_first():
    bars = {
        "STRONG": trending_bars(300, daily=0.003),
        "WEAK": trending_bars(300, daily=0.0001),
        "SPY": trending_bars(300, daily=0.001),
    }
    result = screener.screen(bars, benchmark="SPY")
    assert [c.ticker for c in result.ranked] == ["STRONG", "WEAK"]
    assert result.ranked[0].screen_score > result.ranked[1].screen_score


def test_screen_excludes_the_benchmark_from_candidates():
    bars = {"AAA": trending_bars(300), "SPY": trending_bars(300)}
    result = screener.screen(bars, benchmark="SPY")
    assert "SPY" not in {c.ticker for c in result.ranked}
    assert "SPY" not in result.excluded


def test_screen_records_exclusion_reasons():
    bars = {"GOOD": trending_bars(300), "SHORT": flat_bars(50)}
    result = screener.screen(bars, benchmark="SPY")
    assert "SHORT" in result.excluded
    assert {c.ticker for c in result.ranked} == {"GOOD"}


def test_screen_top_n():
    bars = {f"T{i}": trending_bars(300, daily=0.0005 * i) for i in range(1, 8)}
    result = screener.screen(bars, benchmark="SPY")
    assert len(result.top(3)) == 3
    assert len(result.top(0)) == 0


def test_screen_over_the_stub_universe_produces_a_full_ranking():
    provider = StubProvider()
    tickers = provider.universe("sp500")
    bars = provider.daily_bars(tickers, date.today() - timedelta(days=560), date.today())
    result = screener.screen(bars, benchmark="SPY")
    assert len(result.ranked) >= 20
    scores = [c.screen_score for c in result.ranked]
    assert scores == sorted(scores, reverse=True)


# --------------------------------------------------------------------------
# analyst scoring
# --------------------------------------------------------------------------


def test_analyst_target_upside():
    view = AnalystView(ticker="X", target_mean=120.0, current_price=100.0)
    assert view.target_upside == pytest.approx(0.2)


def test_analyst_target_upside_none_when_incomputable():
    assert AnalystView(ticker="X").target_upside is None
    assert AnalystView(ticker="X", target_mean=120.0, current_price=0.0).target_upside is None


def test_analyst_thin_coverage_is_excluded_not_zeroed():
    views = {
        "COVERED": AnalystView("COVERED", recommendation_mean=1.5, target_mean=120,
                               current_price=100, analyst_count=20),
        "THIN": AnalystView("THIN", recommendation_mean=1.0, target_mean=200,
                            current_price=100, analyst_count=1),
    }
    scores = selection.score_analysts(views)
    assert "THIN" not in scores
    assert "COVERED" in scores


def test_analyst_lower_recommendation_mean_scores_higher():
    views = {
        "BUY": AnalystView("BUY", recommendation_mean=1.2, target_mean=110,
                           current_price=100, analyst_count=10),
        "HOLD": AnalystView("HOLD", recommendation_mean=3.5, target_mean=110,
                            current_price=100, analyst_count=10),
    }
    scores = selection.score_analysts(views)
    assert scores["BUY"] > scores["HOLD"]


def test_analyst_no_usable_coverage_returns_empty():
    assert selection.score_analysts({"X": AnalystView("X")}) == {}


# --------------------------------------------------------------------------
# composite ranking
# --------------------------------------------------------------------------


def _cands(**scores) -> list[screener.Candidate]:
    return [
        screener.Candidate(ticker=t, last_price=100.0, avg_dollar_volume=1e8,
                           bar_count=300, screen_score=s)
        for t, s in scores.items()
    ]


def test_rank_selects_the_top_n():
    scored = selection.rank_candidates(_cands(A=3.0, B=2.0, C=1.0, D=0.0), size=2)
    assert [c.ticker for c in scored if c.selected] == ["A", "B"]
    assert [c.rank for c in scored] == [1, 2, 3, 4]


def test_rank_pinned_ticker_is_always_selected():
    scored = selection.rank_candidates(_cands(A=3.0, B=2.0, C=-9.0), size=2, pinned=["C"])
    selected = {c.ticker for c in scored if c.selected}
    assert selected == {"C", "A"}
    assert next(c for c in scored if c.ticker == "C").manual is True


def test_rank_pinned_ticker_outside_the_universe_is_carried():
    scored = selection.rank_candidates(_cands(A=3.0), size=2, pinned=["ZZZZ"])
    zz = next(c for c in scored if c.ticker == "ZZZZ")
    assert zz.selected and zz.manual


def test_rank_pinned_is_case_insensitive_and_ignores_blanks():
    scored = selection.rank_candidates(_cands(A=3.0, B=1.0), size=1, pinned=["  b ", ""])
    assert {c.ticker for c in scored if c.selected} == {"B"}


def test_rank_llm_adjustment_can_reorder():
    cands = _cands(A=1.0, B=0.9)
    weights = {"screen": 0.5, "analyst": 0.0, "llm": 5.0}
    scored = selection.rank_candidates(
        cands, size=1, stage_weights=weights,
        llm_adjustments={"B": (1.0, "strong pipeline"), "A": (-1.0, "one-off gain")},
    )
    assert [c.ticker for c in scored if c.selected] == ["B"]
    assert next(c for c in scored if c.ticker == "B").llm_reason == "strong pipeline"


def test_rank_persists_every_component_for_audit():
    scored = selection.rank_candidates(
        _cands(A=2.0, B=1.0),
        analyst_views={"A": AnalystView("A", recommendation_mean=1.1, target_mean=130,
                                        current_price=100, analyst_count=15),
                       "B": AnalystView("B", recommendation_mean=3.0, target_mean=105,
                                        current_price=100, analyst_count=15)},
        size=1,
    )
    top = scored[0].to_dict()
    assert {"screen_score", "analyst_score", "llm_adj", "score", "factors", "analyst"} <= top.keys()
    assert scored[0].analyst["analyst_count"] == 15


def test_rank_unselected_candidates_are_retained():
    scored = selection.rank_candidates(_cands(A=3.0, B=2.0, C=1.0), size=1)
    assert len(scored) == 3
    assert sum(1 for c in scored if c.selected) == 1


def test_extract_json_handles_a_fenced_response():
    assert selection._extract_json('```json\n{"adjustments": []}\n```') == '{"adjustments": []}'


def test_llm_rerank_without_a_key_degrades_quietly(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert selection.llm_rerank(_scored(["A"])) == {}


def _scored(tickers: list[str]) -> list[selection.ScoredCandidate]:
    return [selection.ScoredCandidate(ticker=t) for t in tickers]


# --------------------------------------------------------------------------
# trading dates
# --------------------------------------------------------------------------


def test_trading_date_uses_eastern_not_utc():
    # 20:30 UTC on 2 July is 16:30 EDT the SAME day — the UTC date already
    # agrees here, so use a case where it does not: 01:00 UTC is the prior
    # evening in New York.
    moment = datetime(2024, 7, 3, 1, 0, tzinfo=UTC)
    assert portfolio.trading_date(moment) == "2024-07-02"


def test_trading_date_at_the_close_is_the_same_day():
    assert portfolio.trading_date(datetime(2024, 7, 2, 20, 30, tzinfo=UTC)) == "2024-07-02"
    assert portfolio.trading_date(datetime(2024, 1, 2, 21, 30, tzinfo=UTC)) == "2024-01-02"


def test_trading_date_accepts_naive_datetime_as_utc():
    assert portfolio.trading_date(datetime(2024, 7, 3, 1, 0)) == "2024-07-02"


# --------------------------------------------------------------------------
# sizing
# --------------------------------------------------------------------------


def _quotes(prices: dict[str, float]) -> dict[str, Quote]:
    return {t: Quote(ticker=t, price=p, as_of="2024-07-01T20:30:00+00:00") for t, p in prices.items()}


def test_size_positions_equal_weights_and_full_investment():
    picks = [{"ticker": t} for t in ("A", "B", "C", "D")]
    positions, skipped = portfolio.size_positions(picks, 100_000.0, _quotes({"A": 10, "B": 20, "C": 50, "D": 200}))
    assert skipped == []
    assert len(positions) == 4
    assert all(p["weight"] == pytest.approx(0.25) for p in positions)
    assert sum(p["cost"] for p in positions) == pytest.approx(100_000.0, abs=0.05)
    assert positions[0]["shares"] == pytest.approx(2500.0)


def test_size_positions_skips_unpriced_and_reallocates():
    picks = [{"ticker": t} for t in ("A", "B", "C")]
    positions, skipped = portfolio.size_positions(picks, 90_000.0, _quotes({"A": 10, "B": 20}))
    assert skipped == ["C"]
    assert len(positions) == 2
    assert all(p["weight"] == pytest.approx(0.5) for p in positions)
    assert sum(p["cost"] for p in positions) == pytest.approx(90_000.0, abs=0.05)


def test_size_positions_with_no_prices_returns_nothing():
    positions, skipped = portfolio.size_positions([{"ticker": "A"}], 1000.0, {})
    assert positions == []
    assert skipped == ["A"]


def test_size_positions_ignores_a_zero_price():
    positions, skipped = portfolio.size_positions(
        [{"ticker": "A"}], 1000.0, _quotes({"A": 0.0})
    )
    assert positions == [] and skipped == ["A"]


def test_size_positions_preserves_pick_metadata():
    positions, _ = portfolio.size_positions(
        [{"ticker": "A", "rank": 1, "llm_reason": "why"}], 1000.0, _quotes({"A": 10})
    )
    assert positions[0]["rank"] == 1
    assert positions[0]["llm_reason"] == "why"


# --------------------------------------------------------------------------
# snapshots + P/L
# --------------------------------------------------------------------------


def _positions(entries: dict[str, tuple[float, float]]) -> list[dict]:
    """{ticker: (entry_price, shares)}"""
    return [
        {"ticker": t, "entry_price": e, "shares": s, "last_price": e}
        for t, (e, s) in entries.items()
    ]


def test_snapshot_profit():
    positions = _positions({"A": (10.0, 100.0), "B": (20.0, 50.0)})  # cost 1000 + 1000
    snap = portfolio.build_snapshot(positions, _quotes({"A": 11.0, "B": 22.0}))
    assert snap.total_cost == pytest.approx(2000.0)
    assert snap.total_value == pytest.approx(2200.0)
    assert snap.pl_abs == pytest.approx(200.0)
    assert snap.pl_pct == pytest.approx(0.10)


def test_snapshot_loss():
    positions = _positions({"A": (100.0, 10.0)})
    snap = portfolio.build_snapshot(positions, _quotes({"A": 75.0}))
    assert snap.pl_abs == pytest.approx(-250.0)
    assert snap.pl_pct == pytest.approx(-0.25)
    assert snap.holdings[0].pl_pct == pytest.approx(-0.25)


def test_snapshot_carries_last_price_when_a_quote_is_missing():
    positions = [{"ticker": "A", "entry_price": 10.0, "shares": 100.0, "last_price": 12.0}]
    snap = portfolio.build_snapshot(positions, {})
    assert snap.missing == ["A"]
    assert snap.total_value == pytest.approx(1200.0)  # not zero


def test_snapshot_benchmark_comparison():
    positions = _positions({"A": (10.0, 100.0)})
    snap = portfolio.build_snapshot(
        positions,
        _quotes({"A": 11.0}),
        benchmark_entry=400.0,
        benchmark_quote=Quote("SPY", 408.0, "2024-07-05T20:30:00+00:00"),
    )
    assert snap.pl_pct == pytest.approx(0.10)
    assert snap.benchmark_pl_pct == pytest.approx(0.02)


def test_snapshot_benchmark_absent_is_none_not_zero():
    snap = portfolio.build_snapshot(_positions({"A": (10.0, 1.0)}), _quotes({"A": 10.0}))
    assert snap.benchmark_pl_pct is None
    assert snap.benchmark_price is None


def test_snapshot_of_an_empty_group_does_not_divide_by_zero():
    snap = portfolio.build_snapshot([], {})
    assert snap.total_cost == 0.0 and snap.pl_pct == 0.0


def test_snapshot_serialises_to_json_safe_types():
    snap = portfolio.build_snapshot(_positions({"A": (10.0, 100.0)}), _quotes({"A": 11.0}))
    import json
    assert json.loads(json.dumps(snap.to_dict()))["total_value"] == 1100.0


def test_close_positions_stamps_exit_and_realised_pl():
    positions = _positions({"A": (10.0, 100.0), "B": (20.0, 50.0)})
    snap = portfolio.build_snapshot(positions, _quotes({"A": 12.0, "B": 18.0}))
    closed = portfolio.close_positions(positions, snap)
    by_ticker = {p["ticker"]: p for p in closed}
    assert by_ticker["A"]["exit_price"] == pytest.approx(12.0)
    assert by_ticker["A"]["pl_abs"] == pytest.approx(200.0)
    assert by_ticker["B"]["pl_abs"] == pytest.approx(-100.0)
    assert all("exit_at" in p for p in closed)


def test_pct_guards_zero_denominator():
    assert portfolio.pct(5.0, 0.0) == 0.0


def test_full_offline_round_trip_open_snapshot_close():
    """Screen the stub universe, open a group, snapshot it, close it."""
    provider = StubProvider()
    tickers = provider.universe("sp500")
    bars = provider.daily_bars(tickers, date.today() - timedelta(days=560), date.today())
    result = screener.screen(bars, benchmark="SPY")

    shortlist = [c.ticker for c in result.top(selection.ANALYST_SHORTLIST)]
    views = provider.analyst(shortlist)
    scored = selection.rank_candidates(result.ranked, analyst_views=views, size=5, pinned=["MSFT"])

    picks = [c.to_dict() for c in scored if c.selected]
    assert len(picks) == 5
    assert "MSFT" in {p["ticker"] for p in picks}

    quotes = provider.quotes([p["ticker"] for p in picks] + ["SPY"])
    positions, skipped = portfolio.size_positions(picks, 100_000.0, quotes)
    assert not skipped
    assert sum(p["cost"] for p in positions) == pytest.approx(100_000.0, abs=0.1)

    snap = portfolio.build_snapshot(
        positions, quotes,
        benchmark_entry=quotes["SPY"].price,
        benchmark_quote=quotes["SPY"],
    )
    assert snap.pl_abs == pytest.approx(0.0, abs=0.01)      # same prices => flat
    assert snap.benchmark_pl_pct == pytest.approx(0.0)

    closed = portfolio.close_positions(positions, snap)
    assert len(closed) == len(positions)
    assert all("exit_price" in p for p in closed)
