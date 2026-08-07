"""Composite selection — screen + consensus + optional LLM re-rank.

The three signals are combined into one score::

    score = w_screen * screen_score + w_analyst * analyst_score + w_llm * llm_adj

Every input to that sum is persisted per candidate, so any pick can be explained
after the fact and the names that were *not* picked remain available for
comparison. That audit trail is the point: it is what turns "we picked these ten"
into "here is whether this strategy beat the index, and why".

Only the LLM stage touches the network from this module, and only when enabled.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

from .providers import AnalystView
from .screener import Candidate, zscores

logger = logging.getLogger("stocks.selection")

DEFAULT_STAGE_WEIGHTS: dict[str, float] = {
    "screen": 0.60,
    "analyst": 0.30,
    "llm": 0.10,
}

# Consensus sub-weights. `recommendation_mean` is inverted at source (1 = strong
# buy), so a lower value is better and its weight is negative.
ANALYST_WEIGHTS: dict[str, float] = {
    "recommendation_mean": -0.5,
    "target_upside": 0.5,
}

ANALYST_SHORTLIST = 50   # how many screen survivors get a consensus lookup
LLM_SHORTLIST = 25       # how many of those go to the model
LLM_MODEL = "claude-sonnet-5"
MIN_ANALYST_COUNT = 3    # below this, coverage is too thin to be a signal


@dataclass
class ScoredCandidate:
    ticker: str
    rank: int = 0
    screen_score: float = 0.0
    analyst_score: float = 0.0
    llm_adj: float = 0.0
    score: float = 0.0
    factors: dict[str, float] = field(default_factory=dict)
    zscores: dict[str, float] = field(default_factory=dict)
    analyst: dict = field(default_factory=dict)
    llm_reason: str = ""
    name: str = ""
    sector: str = ""
    last_price: float = 0.0
    selected: bool = False
    manual: bool = False

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "rank": self.rank,
            "screen_score": round(self.screen_score, 6),
            "analyst_score": round(self.analyst_score, 6),
            "llm_adj": round(self.llm_adj, 6),
            "score": round(self.score, 6),
            "factors": {k: round(v, 6) for k, v in self.factors.items()},
            "zscores": {k: round(v, 6) for k, v in self.zscores.items()},
            "analyst": self.analyst,
            "llm_reason": self.llm_reason,
            "name": self.name,
            "sector": self.sector,
            "last_price": round(self.last_price, 4),
            "selected": self.selected,
            "manual": self.manual,
        }


# --------------------------------------------------------------------------
# Stage C — analyst consensus
# --------------------------------------------------------------------------


def score_analysts(views: dict[str, AnalystView]) -> dict[str, float]:
    """Cross-sectional consensus score per ticker.

    Tickers with thin or absent coverage are omitted from the result entirely
    rather than scored zero — zero is the mean of a z-score distribution, which
    would flatter a stock nobody covers into looking average.
    """
    usable = {
        t: v
        for t, v in views.items()
        if (v.analyst_count or 0) >= MIN_ANALYST_COUNT
        and (v.recommendation_mean is not None or v.target_upside is not None)
    }
    if not usable:
        return {}

    rec_raw = {t: v.recommendation_mean for t, v in usable.items() if v.recommendation_mean is not None}
    up_raw = {t: v.target_upside for t, v in usable.items() if v.target_upside is not None}

    rec_z = zscores(rec_raw)
    up_z = zscores(up_raw)

    out: dict[str, float] = {}
    for ticker in usable:
        total = 0.0
        weight_used = 0.0
        if ticker in rec_z:
            total += ANALYST_WEIGHTS["recommendation_mean"] * rec_z[ticker]
            weight_used += abs(ANALYST_WEIGHTS["recommendation_mean"])
        if ticker in up_z:
            total += ANALYST_WEIGHTS["target_upside"] * up_z[ticker]
            weight_used += abs(ANALYST_WEIGHTS["target_upside"])
        # Rescale so a ticker with only one of the two sub-signals is not
        # systematically muted relative to one with both.
        out[ticker] = total / weight_used if weight_used else 0.0
    return out


# --------------------------------------------------------------------------
# Stage D — optional LLM re-rank
# --------------------------------------------------------------------------

_LLM_SYSTEM = (
    "You are assisting a transparent, rules-based equity screening tool used for "
    "PAPER TRADING and strategy measurement. You are given candidates that already "
    "passed a quantitative momentum/quality screen, with their factor values.\n\n"
    "For each ticker return an adjustment in [-1.0, 1.0] and a one-sentence reason. "
    "Positive means the qualitative picture supports the quantitative signal; "
    "negative means it contradicts it (for example: the momentum comes from a "
    "one-off event unlikely to persist, a pending acquisition caps the upside, or "
    "there is a known accounting or regulatory overhang).\n\n"
    "Be conservative. Use 0.0 when you have no specific, substantive knowledge about "
    "a company — an invented rationale is worse than no adjustment. You are not "
    "predicting prices and not giving investment advice.\n\n"
    'Respond with JSON only: {"adjustments": [{"ticker": "X", "adjustment": 0.0, '
    '"reason": "..."}]}'
)


def llm_rerank(
    candidates: list[ScoredCandidate],
    *,
    model: str = LLM_MODEL,
    api_key: str | None = None,
) -> dict[str, tuple[float, str]]:
    """Ask Claude for a bounded adjustment plus a rationale per ticker.

    Returns ``{ticker: (adjustment, reason)}``. On any failure it returns an
    empty mapping and logs — the group still opens on the quantitative signal
    alone, because an unavailable optional stage must not block the run.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        logger.warning("LLM re-rank requested but ANTHROPIC_API_KEY is not set; skipping")
        return {}
    try:
        import anthropic
    except ImportError:
        logger.warning("LLM re-rank requested but the anthropic package is not installed; skipping")
        return {}

    payload = [
        {
            "ticker": c.ticker,
            "name": c.name,
            "sector": c.sector,
            "factors": {k: round(v, 4) for k, v in c.factors.items()},
            "screen_score": round(c.screen_score, 4),
            "analyst": c.analyst,
        }
        for c in candidates
    ]

    try:
        client = anthropic.Anthropic(api_key=key)
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=_LLM_SYSTEM,
            messages=[{"role": "user", "content": json.dumps({"candidates": payload})}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        data = json.loads(_extract_json(text))
    except Exception as exc:  # noqa: BLE001 - optional stage, degrade explicitly
        logger.warning("LLM re-rank failed (%s); continuing without it", exc)
        return {}

    out: dict[str, tuple[float, str]] = {}
    for item in data.get("adjustments", []):
        ticker = str(item.get("ticker", "")).upper()
        if not ticker:
            continue
        try:
            adj = float(item.get("adjustment", 0.0))
        except (TypeError, ValueError):
            continue
        out[ticker] = (max(-1.0, min(1.0, adj)), str(item.get("reason", ""))[:400])
    return out


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a response that may be fenced or prefaced."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("```")[1]
        if stripped.startswith("json"):
            stripped = stripped[4:]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in LLM response")
    return stripped[start : end + 1]


# --------------------------------------------------------------------------
# Composite
# --------------------------------------------------------------------------


def rank_candidates(
    screened: list[Candidate],
    *,
    analyst_views: dict[str, AnalystView] | None = None,
    llm_adjustments: dict[str, tuple[float, str]] | None = None,
    stage_weights: dict[str, float] | None = None,
    size: int = 10,
    pinned: list[str] | None = None,
) -> list[ScoredCandidate]:
    """Combine the stages and mark the selected group.

    Pinned tickers are always selected and flagged ``manual``. They bypass
    scoring entirely — an explicit human choice is not something to re-rank —
    and they occupy slots, so ``size`` remains the total group size.
    """
    weights = {**DEFAULT_STAGE_WEIGHTS, **(stage_weights or {})}
    analyst_views = analyst_views or {}
    llm_adjustments = llm_adjustments or {}
    pinned_upper = [t.strip().upper() for t in (pinned or []) if t.strip()]

    analyst_scores = score_analysts(analyst_views)

    scored: list[ScoredCandidate] = []
    for cand in screened:
        view = analyst_views.get(cand.ticker)
        adj, reason = llm_adjustments.get(cand.ticker, (0.0, ""))
        analyst_score = analyst_scores.get(cand.ticker, 0.0)
        sc = ScoredCandidate(
            ticker=cand.ticker,
            screen_score=cand.screen_score,
            analyst_score=analyst_score,
            llm_adj=adj,
            factors=dict(cand.factors),
            zscores=dict(cand.zscores),
            last_price=cand.last_price,
            llm_reason=reason,
            name=(view.name if view and view.name else cand.ticker),
            sector=(view.sector if view and view.sector else ""),
            analyst=_analyst_dict(view),
        )
        sc.score = (
            weights.get("screen", 0.0) * sc.screen_score
            + weights.get("analyst", 0.0) * sc.analyst_score
            + weights.get("llm", 0.0) * sc.llm_adj
        )
        scored.append(sc)

    scored.sort(key=lambda c: (-c.score, c.ticker))
    for i, sc in enumerate(scored, start=1):
        sc.rank = i

    by_ticker = {c.ticker: c for c in scored}
    selected: list[ScoredCandidate] = []

    for ticker in pinned_upper:
        cand = by_ticker.get(ticker)
        if cand is None:
            # Pinned but not in the screened universe (or filtered out). Carry it
            # anyway — the user asked for it explicitly.
            cand = ScoredCandidate(ticker=ticker, name=ticker, manual=True)
            scored.append(cand)
            by_ticker[ticker] = cand
        cand.manual = True
        cand.selected = True
        selected.append(cand)

    for cand in scored:
        if len(selected) >= size:
            break
        if cand.selected:
            continue
        cand.selected = True
        selected.append(cand)

    return scored


def _analyst_dict(view: AnalystView | None) -> dict:
    if view is None:
        return {}
    return {
        "recommendation_mean": view.recommendation_mean,
        "target_mean": view.target_mean,
        "target_upside": view.target_upside,
        "analyst_count": view.analyst_count,
    }
