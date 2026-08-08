# fwh_stocks — stock-group paper-trading tracker

A Facetwork domain that selects a group of stocks with a transparent,
reproducible ranking strategy, records the entry prices, marks the group to
market daily, and on close writes per-position and overall profit/loss to
MongoDB alongside a benchmark comparison.

> ## ⚠️ Paper trading only — not investment advice
>
> This tool records **notional (simulated) positions**. It places no orders,
> holds no assets, connects to no broker, and moves no money.
>
> **Nothing produced by this software is a recommendation to buy or sell any
> security, and none of it is financial, investment, tax or legal advice.** Do
> not make investment decisions on the basis of its output. Consult a licensed
> financial advisor.
>
> **Past performance does not predict future results, and no selection rule
> predicts returns.** The scores are a transparent ranking over documented
> historical factors — they are not a forecast. The benchmark comparison exists
> so you can check whether the strategy actually beat simply holding the index,
> which is the only honest test of it.
>
> Prices come from a **delayed, unofficial feed** that can be wrong, stale or
> unavailable. Reported returns **exclude commissions, spreads, slippage,
> borrowing costs, taxes and dividends**, so real-world results would differ,
> generally for the worse.
>
> Provided as-is, without warranty of any kind. You are solely responsible for
> any use you make of it.

Web UI: **`/stocks`** in the Facetwork dashboard (Apps → Stock Groups).

## FFL at a glance

```ffl
namespace demo {
    use stocks.data
    use stocks.groups

    workflow WeeklyMomentum(name: String = "Momentum week") => (group_id: String) andThen {
        universe = ResolveUniverse(universe = "sp500")
        bars     = FetchDailyBars(universe = "sp500", lookback_days = 400) after universe
        opened   = OpenGroup(name = $.name, universe = "sp500", size = 10) after bars
        yield WeeklyMomentum(group_id = opened.group_id)
    }
}
```

Note the `after` clauses: the universe list and the price history go to the
shared cache, not into the step results, so the ordering is invisible to the
compiler and must be stated. Full gallery: [`docs/ffl-examples.md`](docs/ffl-examples.md).

## Install

```bash
fw install domain stocks --check          # clone + pip install -e + verify discovery
pip install -e '.[yfinance]'              # the default price provider
fw ffl seed --include stocks              # seed the workflows
fw runner start --domain stocks           # or: python -m facetwork.runtime.runner --registry --task-list stocks
```

## How stocks are selected

Four stages, combined into one composite score. Every intermediate value is
persisted per candidate, so any pick can be explained afterwards.

**A0. Kinds of stock (sectors)** — optionally restrict the pool to named GICS
sectors before anything expensive runs. Everyday names are accepted
(`tech`, `medical`, `banks`, `oil`, `reits`), and a combination is fine:
`sectors="tech, medical"`. Empty means every sector.

Classification comes free with the S&P 500 constituent table (a `GICS Sector`
column), so filtering a whole universe costs one cached call rather than one
request per ticker — and it runs *before* the price download, so asking for
Health Care fetches ~59 symbols instead of 503.

Ambiguous names are refused rather than guessed: `"services"` could mean
Communication Services or Consumer Discretionary, so it raises and names both.
Silently picking one would hand back a group you did not ask for.

**A1. `max_per_sector`** — a diversification cap (0 = no cap). An unconstrained
momentum screen routinely returns ten names from a single theme, which makes the
group a sector bet rather than a test of the strategy. The cap is a **hard**
limit: if it cannot be filled the group comes back *smaller* than `size` rather
than quietly exceeding it, and the shortfall is logged. Pinned tickers are exempt
from both the filter and the cap — an explicit request outranks them.

**A. Hard filters** — data-quality gates, never scored: price ≥ $5, 20-day
average dollar volume ≥ $10M, ≥ 252 trading days of history.

**B. Factor screen** (`screener.py`, pure functions over daily bars):

| Factor | Definition | Weight |
|---|---|---|
| `mom_12_1` | 12-month return excluding the most recent month | +0.35 |
| `mom_3m` | 3-month total return | +0.20 |
| `rel_strength` | 6-month return minus the benchmark's | +0.25 |
| `near_high` | last close ÷ 52-week intraday high | +0.20 |
| `vol_60d` | 60-day realized volatility | −0.15 |

Each is cross-sectionally z-scored across the survivors and winsorized at ±3.

**C. Analyst consensus** — recommendation mean and target upside, fetched for the
top 50 only. Tickers with fewer than 3 covering analysts are *excluded* from the
distribution rather than scored zero (zero is the mean, which would flatter an
uncovered name into looking average).

**D. Optional LLM re-rank** — Claude reviews the top 25 and returns a bounded
`[-1, +1]` adjustment plus a written reason stored on the pick. Off by default;
if `ANTHROPIC_API_KEY` is absent the group still opens on the quantitative signal.

```
score = 0.60 · screen_score + 0.30 · analyst_score + 0.10 · llm_adjustment
```

Weights are per-group and persisted on the group document, so a run stays
reproducible after the defaults change. Pinned tickers bypass scoring entirely
and are flagged `manual`.

The **complete ranked candidate list is saved**, not just the picks — that is
what lets you check afterwards how the names you didn't pick performed.

## How current values are determined

`provider.quotes()` returns the last trade or most recent close with an `as_of`
timestamp, which is stored on every snapshot so a stale feed is visible rather
than silently producing a flat line. Snapshots are upserted on
`(group_id, snapshot_date)` where the date is the **US/Eastern trading date**, so
a repeated run on the same day corrects rather than duplicates.

## Price providers

Everything depends only on the `PriceProvider` protocol in `providers/__init__.py`.

| `FW_STOCKS_PROVIDER` | Notes |
|---|---|
| `yfinance` *(default)* | Free, keyless, ~15-min delayed. Unofficial — can break without notice. |
| `stub` | Deterministic synthetic prices. Used by the test suite; opt-in only, never a fallback. |

Adding a paid feed (Polygon, Tiingo, Alpha Vantage) is one new module plus a
registry entry.

## Workflows

| Workflow | Purpose |
|---|---|
| `stocks.workflows.OpenStockGroup` | Resolve universe → cache bars → screen, rank, price, persist as open |
| `stocks.workflows.SnapshotStockGroups` | Mark one group, or every open group, to market. Run daily by cron. |
| `stocks.workflows.CloseStockGroup` | Final mark, realised per-position P/L, group totals, status closed |

Daily snapshot after the US close:

```cron
35 16 * * 1-5  cd /path/to/facetwork && ./fw ffl run \
    ~/fw_handlers/fwh_stocks/src/stocks/ffl/stocks.ffl \
    --workflow stocks.workflows.SnapshotStockGroups --inputs '{}'
```

Runs are idempotent per trading date, so a missed or doubled fire is harmless.

## Data model (MongoDB `FW_EXAMPLES_DATABASE`, default `facetwork_examples`)

| Collection | Contents |
|---|---|
| `stock_groups` | One per group: status, strategy as run, benchmark entry, current and final P/L |
| `stock_positions` | One per ticker: entry, shares, weight, marks, exit, realised P/L, why it was picked |
| `stock_snapshots` | One per (group, trading date): holdings, totals, benchmark — the P/L series |
| `stock_candidates` | The full ranked universe including unpicked names — the audit trail |

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `FW_STOCKS_PROVIDER` | `yfinance` | Price provider |
| `FW_EXAMPLES_DATABASE` | `facetwork_examples` | Domain database |
| `FW_STOCKS_CACHE_DIR` | `$FW_DATA_ROOT/cache/stocks/cache` | Bar/universe/analyst cache |
| `ANTHROPIC_API_KEY` | — | Required only for the LLM re-rank |

## Tests

```bash
pytest tests/          # no network, no MongoDB required
```

The suite runs entirely offline: the stub provider supplies deterministic prices
and `mongomock` backs the group-lifecycle tests.
