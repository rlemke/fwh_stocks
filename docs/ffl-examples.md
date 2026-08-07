# FFL examples — `stocks`

Complete, compile-checked scenarios against this domain's own facets. Every
fenced `ffl` block below is meant to compile:

```bash
fw ffl compile --check --primary <block>.ffl \
  --library ~/fw_handlers/fwh_stocks/src/stocks/ffl/stocks.ffl
```

The facets in play:

| Namespace | Facet | Returns |
|---|---|---|
| `stocks.data` | `ResolveUniverse(universe)` | `ticker_count` |
| `stocks.data` | `FetchDailyBars(universe, pinned, benchmark, lookback_days, force_refresh)` | `symbol_count` |
| `stocks.groups` | `OpenGroup(name, universe, size, capital, …)` | `group_id, ticker_count, initial_value, tickers` |
| `stocks.groups` | `SnapshotGroups(group_id)` | `groups_snapshotted, failed_count` |
| `stocks.groups` | `CloseGroup(group_id)` | `group_id, pl_abs, pl_pct, benchmark_pl_pct, winners` |

---

## 1. The minimal workflow

Snapshot every open group. This is what the daily cron entry runs.

```ffl
namespace ex.minimal {
    use stocks.groups

    workflow DailyMark() => (count: Long) andThen {
        snap = SnapshotGroups(group_id = "")
        yield DailyMark(count = snap.groups_snapshotted)
    }
}
```

## 2. `$`-scoping — reading the workflow's own parameters

`$` is the block's immediate container, so `$.size` reads the workflow's `size`
parameter. `opened.group_id` reads a previous step's return.

```ffl
namespace ex.scoping {
    use stocks.groups

    workflow OpenSized(
        name: String = "Sized group",
        size: Int = 10,
        capital: Double = 100000.0
    ) => (group_id: String, value: Double) andThen {
        opened = OpenGroup(name = $.name, size = $.size, capital = $.capital)
        yield OpenSized(group_id = opened.group_id, value = opened.initial_value)
    }
}
```

## 3. `after` — ordering an invisible dependency

`FetchDailyBars` writes price history to the shared cache and returns only a
count. `OpenGroup` reads that cache but takes no value from the fetch, so the
compiler cannot see the edge — state it with `after`. Writing `after` where a
value *does* flow would trip `AFTER_REDUNDANT`.

```ffl
namespace ex.after {
    use stocks.data
    use stocks.groups

    workflow OpenFromFreshBars(name: String = "Fresh") => (group_id: String) andThen {
        universe = ResolveUniverse(universe = "sp500")
        bars     = FetchDailyBars(universe = "sp500", force_refresh = true) after universe
        opened   = OpenGroup(name = $.name, universe = "sp500") after bars
        yield OpenFromFreshBars(group_id = opened.group_id)
    }
}
```

## 4. `when` — branch on the outcome

Every `when` needs a default case, and it must be last.

```ffl
namespace ex.branch {
    use stocks.groups

    event facet Notify(subject: String) => (sent: Boolean)

    workflow CloseAndReport(group_id: String) => (sent: Boolean) andThen {
        closed = CloseGroup(group_id = $.group_id)
        note = Notify(subject = "group closed") when {
            closed.pl_pct > closed.benchmark_pl_pct => Notify(subject = "beat the benchmark")
            closed.pl_pct > 0.0 => Notify(subject = "profitable, trailed the benchmark")
            default => Notify(subject = "closed at a loss")
        }
        yield CloseAndReport(sent = note.sent)
    }
}
```

## 5. `foreach` — several universes in one run

Loop variables are read as `$.<varname>` inside the block.

```ffl
namespace ex.fanout {
    use stocks.data

    workflow WarmCaches(universes: Json) => (done: Boolean) andThen {
        foreach u in $.universes {
            warmed = FetchDailyBars(universe = $.u, lookback_days = 400)
        }
        yield WarmCaches(done = true)
    }
}
```

> Prefer a batched call over a per-ticker fan-out. The provider downloads many
> symbols in one request, so fanning out per ticker adds scheduling risk and
> buys nothing.

## 6. `catch` — degrade instead of failing the run

```ffl
namespace ex.resilient {
    use stocks.groups

    workflow SafeMark(group_id: String) => (count: Long) andThen {
        snap = SnapshotGroups(group_id = $.group_id) catch {
            yield SafeMark(count = 0)
        }
        yield SafeMark(count = snap.groups_snapshotted)
    }
}
```

## 7. The full lifecycle in one workflow

Open, mark, and close in sequence — useful for a same-session smoke test.

```ffl
namespace ex.lifecycle {
    use stocks.data
    use stocks.groups

    workflow SmokeTest(name: String = "Smoke") => (pl_pct: Double) andThen {
        universe = ResolveUniverse(universe = "dow30")
        bars     = FetchDailyBars(universe = "dow30") after universe
        opened   = OpenGroup(name = $.name, universe = "dow30", size = 3) after bars
        marked   = SnapshotGroups(group_id = opened.group_id)
        closed   = CloseGroup(group_id = opened.group_id) after marked
        yield SmokeTest(pl_pct = closed.pl_pct)
    }
}
```

---

## Syntax cheat sheet

| Want | Write |
|---|---|
| A workflow's own parameter | `$.param` |
| A previous step's return | `step_name.field` (never a bare `step_name`) |
| A `foreach` loop variable | `$.varname` |
| Order behind a cache write | `step = F(…) after producer` |
| Order behind a value | nothing — the reference already orders it |
| Branch | `when { cond => …  default => … }` (default last) |
| Bound fan-out width | `foreach x in $.xs limit 4` |

Workflows, facets and schemas must live inside a `namespace`. Numeric types are
`Int`, `Long`, `Double`; also `String`, `Boolean`, `Json`.
