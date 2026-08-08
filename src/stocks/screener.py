"""Factor screen — pure math over daily bars. No I/O, no network, no Mongo.

Two stages, deliberately separated:

*Hard filters* exclude names where the factor math would be meaningless (too
illiquid, too cheap, too little history). They are never scored — a filter is a
statement about data quality, not about attractiveness.

*Factors* are then cross-sectionally z-scored across the survivors, winsorized so
a single outlier cannot dominate the ranking, and combined into one
``screen_score``. Every intermediate value is returned, not just the score,
because "why was this picked?" is the product.

None of this predicts returns. It ranks by documented, backtestable
characteristics so the outcome can be measured against a benchmark.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from .providers import Bar

TRADING_DAYS_YEAR = 252
TRADING_DAYS_MONTH = 21

# Hard filters — data-quality gates, not opinions.
MIN_PRICE = 5.0
MIN_AVG_DOLLAR_VOLUME = 10_000_000.0
MIN_HISTORY_BARS = TRADING_DAYS_YEAR

# Default factor weights. Overridable per group and persisted on the group doc,
# so a run stays reproducible even after the defaults change.
DEFAULT_FACTOR_WEIGHTS: dict[str, float] = {
    "mom_12_1": 0.35,
    "mom_3m": 0.20,
    "rel_strength": 0.25,
    "near_high": 0.20,
    "vol_60d": -0.15,  # negative: volatility is a penalty, not a signal
}

WINSOR_LIMIT = 3.0


@dataclass
class Candidate:
    """One ticker's screen result — raw factors, z-scores, and the composite."""

    ticker: str
    last_price: float
    avg_dollar_volume: float
    bar_count: int
    factors: dict[str, float] = field(default_factory=dict)
    zscores: dict[str, float] = field(default_factory=dict)
    screen_score: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScreenResult:
    ranked: list[Candidate]
    excluded: dict[str, str]  # ticker -> human-readable reason
    weights: dict[str, float]

    def top(self, n: int) -> list[Candidate]:
        return self.ranked[: max(0, n)]


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------


def winsorize(value: float, limit: float = WINSOR_LIMIT) -> float:
    return max(-limit, min(limit, value))


def zscores(values: dict[str, float], limit: float = WINSOR_LIMIT) -> dict[str, float]:
    """Cross-sectional z-scores, winsorized.

    A zero-spread input (every value identical) yields all-zero scores rather
    than a division by zero — correct, since a factor with no dispersion carries
    no ranking information.
    """
    if not values:
        return {}
    xs = list(values.values())
    n = len(xs)
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / n
    sd = math.sqrt(var)
    if sd == 0:
        return dict.fromkeys(values, 0.0)
    return {k: winsorize((v - mean) / sd, limit) for k, v in values.items()}


# --------------------------------------------------------------------------
# Factors
# --------------------------------------------------------------------------


def _ret(bars: list[Bar], lookback: int, skip: int = 0) -> float | None:
    """Total return over *lookback* bars ending *skip* bars before the last one."""
    end_idx = len(bars) - 1 - skip
    start_idx = end_idx - lookback
    if start_idx < 0 or end_idx < 0:
        return None
    start = bars[start_idx].close
    if start <= 0:
        return None
    return bars[end_idx].close / start - 1.0


def realized_vol(bars: list[Bar], window: int = 60) -> float | None:
    """Annualised standard deviation of daily log returns over *window* bars."""
    closes = [b.close for b in bars[-(window + 1) :]]
    if len(closes) < 3:
        return None
    rets = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0 and closes[i] > 0
    ]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS_YEAR)


def avg_dollar_volume(bars: list[Bar], window: int = 20) -> float:
    recent = bars[-window:]
    if not recent:
        return 0.0
    return sum(b.close * b.volume for b in recent) / len(recent)


def near_high_ratio(bars: list[Bar], window: int = TRADING_DAYS_YEAR) -> float | None:
    recent = bars[-window:]
    if not recent:
        return None
    high = max(b.high for b in recent)
    if high <= 0:
        return None
    return recent[-1].close / high


def compute_factors(bars: list[Bar], benchmark_bars: list[Bar] | None) -> dict[str, float]:
    """Raw (un-normalised) factor values for one ticker.

    Missing factors are omitted rather than defaulted — a missing value must not
    be scored as zero, which would sit at the mean and look like an average name.
    """
    out: dict[str, float] = {}

    mom_12_1 = _ret(bars, lookback=TRADING_DAYS_YEAR - TRADING_DAYS_MONTH, skip=TRADING_DAYS_MONTH)
    if mom_12_1 is not None:
        out["mom_12_1"] = mom_12_1

    mom_3m = _ret(bars, lookback=TRADING_DAYS_MONTH * 3)
    if mom_3m is not None:
        out["mom_3m"] = mom_3m

    six_month = _ret(bars, lookback=TRADING_DAYS_MONTH * 6)
    if six_month is not None:
        bench = _ret(benchmark_bars, lookback=TRADING_DAYS_MONTH * 6) if benchmark_bars else None
        out["rel_strength"] = six_month - bench if bench is not None else six_month

    ratio = near_high_ratio(bars)
    if ratio is not None:
        out["near_high"] = ratio

    vol = realized_vol(bars)
    if vol is not None:
        out["vol_60d"] = vol

    return out


# --------------------------------------------------------------------------
# The screen
# --------------------------------------------------------------------------


def apply_hard_filters(
    ticker: str,
    bars: list[Bar],
    *,
    min_price: float = MIN_PRICE,
    min_dollar_volume: float = MIN_AVG_DOLLAR_VOLUME,
    min_history: int = MIN_HISTORY_BARS,
) -> str | None:
    """Return a rejection reason, or ``None`` when the ticker passes."""
    if len(bars) < min_history:
        return f"only {len(bars)} bars of history (need {min_history})"
    last = bars[-1].close
    if last < min_price:
        return f"price ${last:,.2f} below the ${min_price:,.2f} floor"
    adv = avg_dollar_volume(bars)
    if adv < min_dollar_volume:
        return f"20d average dollar volume ${adv:,.0f} below ${min_dollar_volume:,.0f}"
    return None


def screen(
    bars_by_ticker: dict[str, list[Bar]],
    *,
    benchmark: str = "SPY",
    weights: dict[str, float] | None = None,
    min_price: float = MIN_PRICE,
    min_dollar_volume: float = MIN_AVG_DOLLAR_VOLUME,
    min_history: int = MIN_HISTORY_BARS,
) -> ScreenResult:
    """Filter, factor, z-score and rank a universe of daily-bar series."""
    weights = dict(weights or DEFAULT_FACTOR_WEIGHTS)
    benchmark_bars = bars_by_ticker.get(benchmark)

    excluded: dict[str, str] = {}
    survivors: dict[str, Candidate] = {}

    for ticker, bars in bars_by_ticker.items():
        if ticker == benchmark:
            continue  # the yardstick is not a candidate
        if not bars:
            excluded[ticker] = "no price history returned"
            continue
        reason = apply_hard_filters(
            ticker,
            bars,
            min_price=min_price,
            min_dollar_volume=min_dollar_volume,
            min_history=min_history,
        )
        if reason:
            excluded[ticker] = reason
            continue
        factors = compute_factors(bars, benchmark_bars)
        if not factors:
            excluded[ticker] = "no factor could be computed"
            continue
        survivors[ticker] = Candidate(
            ticker=ticker,
            last_price=bars[-1].close,
            avg_dollar_volume=avg_dollar_volume(bars),
            bar_count=len(bars),
            factors=factors,
        )

    # Cross-sectional normalisation, one factor at a time so a ticker missing
    # factor X is simply absent from X's distribution.
    for name in weights:
        raw = {t: c.factors[name] for t, c in survivors.items() if name in c.factors}
        for ticker, z in zscores(raw).items():
            survivors[ticker].zscores[name] = z

    for cand in survivors.values():
        cand.screen_score = sum(
            weight * cand.zscores.get(name, 0.0) for name, weight in weights.items()
        )

    ranked = sorted(survivors.values(), key=lambda c: (-c.screen_score, c.ticker))
    return ScreenResult(ranked=ranked, excluded=excluded, weights=weights)
