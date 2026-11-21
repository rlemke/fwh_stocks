"""Static pages for the paper-trading stock groups.

The dashboard's /stocks app needs the live database; this writes the same
record as plain HTML for the public maps site: an index of every group and one
page per group with its profit and loss against the benchmark, its holdings
and the strategy it ran. The chart is inline SVG, so the pages load nothing.

Output depends only on the data (no build time is printed), so a day with no
new snapshot republishes nothing.

    python -m stocks.site --out DIR
"""

from __future__ import annotations

import argparse
import html
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import db as dbmod
from . import portfolio

REPO_URL = "https://github.com/rlemke/fwh_stocks"
FFL_URL = REPO_URL + "/blob/main/src/stocks/ffl/stocks_site.ffl"
USD = "$"

DISCLAIMER = (
    "<p><b>Paper trading only, not investment advice.</b> These are notional "
    "(simulated) positions recorded to measure a selection strategy. No orders were "
    "placed and no money moved. Nothing here is a recommendation to buy or sell any "
    "security. Past performance does not predict future results, and the scores are "
    "a ranking over historical factors, not a forecast: the benchmark column is there "
    "to check whether the strategy beat simply holding the index.</p>"
    "<p>Prices come from a delayed, unofficial feed (Yahoo Finance through yfinance) "
    "and can be wrong. Figures exclude commissions, spreads, slippage, taxes and "
    "dividends.</p>"
)

CSS = """
body{font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;color:#1d2330;background:#fafafa}
main{max-width:1100px;margin:0 auto;padding:1.2rem 1.2rem 3rem}
h1{font-size:1.6rem;margin:.2rem 0 .3rem}h2{font-size:1.15rem;margin:1.8rem 0 .5rem}
a{color:#1f5fbf}.muted{color:#667085}.small{font-size:12.5px}
.badge{display:inline-block;background:#fff4d6;border:1px solid #e8c36a;color:#7a5600;border-radius:4px;padding:1px 7px;font-size:12px;font-weight:600}
.kpis{display:flex;flex-wrap:wrap;gap:.7rem;margin:1rem 0}
.kpi{background:#fff;border:1px solid #e3e6ec;border-radius:8px;padding:.6rem .9rem;min-width:150px}
.kpi .l{font-size:12px;color:#667085}.kpi .v{font-size:1.35rem;font-weight:600}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #e3e6ec;font-size:13.5px}
th,td{padding:.35rem .55rem;border-bottom:1px solid #eef0f4;text-align:left;vertical-align:top}
th{background:#f3f5f8;font-weight:600;font-size:12.5px}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.gain{color:#0a7a3d}.loss{color:#b42318}.flat{color:#667085}
.wrap{overflow-x:auto}.note{background:#fff;border:1px solid #e3e6ec;border-radius:8px;padding:.2rem 1rem;margin-top:2rem;font-size:13px;color:#475467}
dl.defs{font-size:13px;color:#475467}dl.defs dt{font-weight:600;color:#1d2330;margin-top:.4rem}dl.defs dd{margin:0 0 0 1rem}
svg text{font:11px system-ui,sans-serif;fill:#667085}
footer{margin-top:2rem;font-size:12px;color:#667085}
"""


def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def pct(v: float | None, digits: int = 2) -> str:
    """A fraction shown as a signed percent, coloured by sign."""
    if v is None:
        return '<span class="flat">n/a</span>'
    cls = "gain" if v > 0 else "loss" if v < 0 else "flat"
    return f'<span class="{cls}">{v * 100:+.{digits}f}%</span>'


def money(v: float | None) -> str:
    return '<span class="flat">n/a</span>' if v is None else USD + f"{v:,.2f}"


def signed_money(v: float | None) -> str:
    if v is None:
        return '<span class="flat">n/a</span>'
    cls = "gain" if v > 0 else "loss" if v < 0 else "flat"
    sign = "+" if v > 0 else "-" if v < 0 else ""
    return f'<span class="{cls}">{sign}{USD}{abs(v):,.2f}</span>'


def _day(x: Any) -> str:
    if isinstance(x, datetime):
        return x.date().isoformat()
    return str(x or "")[:10]


def _as_date(x: Any) -> date | None:
    try:
        return date.fromisoformat(_day(x))
    except ValueError:
        return None


def group_view(db, g: dict) -> dict:
    """The numbers a group row shows.

    Closed groups read their recorded final result; open groups read the latest
    snapshot, whose benchmark change covers the same days as its P/L.
    """
    snaps = dbmod.get_snapshots(db, g["group_id"])
    last = snaps[-1] if snaps else {}
    final = g.get("final") or {}
    closed = g.get("status") == dbmod.STATUS_CLOSED
    if closed:
        value, pl_abs, pl, bench = (
            final.get(k) for k in ("total_value", "pl_abs", "pl_pct", "benchmark_pl_pct")
        )
    else:
        value = last.get("total_value", g.get("initial_value"))
        pl_abs, pl, bench = last.get("pl_abs"), last.get("pl_pct"), last.get("benchmark_pl_pct")
    alpha = round(pl - bench, 6) if pl is not None and bench is not None else None
    return {
        "group": g,
        "snaps": snaps,
        "closed": closed,
        "value": value,
        "pl_abs": pl_abs,
        "pl": pl,
        "bench": bench,
        "alpha": alpha,
        "first": snaps[0]["snapshot_date"] if snaps else None,
        "last": snaps[-1]["snapshot_date"] if snaps else None,
        "planned_end": _day(g.get("target_close")),
    }


def _nice_step(span: float) -> float:
    for s in (1, 2, 5, 10, 20, 25, 50, 100):
        if span / s <= 8:
            return float(s)
    return 200.0


def chart_svg(snaps: list[dict], name: str, benchmark: str, start: str = "") -> str:
    """Percent change from entry for the group and its benchmark on one axis.

    One axis, because both are percentages off the same start; a second axis
    would let the two lines be made to look like anything. The x axis is the
    calendar, so days with no snapshot show as a gap in time rather than being
    squeezed out, and the lines start at 0% on the day the group opened.
    """
    pts = [(s["snapshot_date"], s.get("pl_pct"), s.get("benchmark_pl_pct")) for s in snaps]
    if start and pts and start < pts[0][0]:
        pts.insert(0, (start, 0.0, 0.0))
    if len(pts) < 2:
        return '<p class="muted small">Only one snapshot so far; the chart appears with the second.</p>'
    w, h, ml, mr, mt, mb = 900, 300, 52, 16, 22, 34
    vals = [v * 100 for _, a, b in pts for v in (a, b) if v is not None] + [0.0]
    lo, hi = min(vals), max(vals)
    pad = max((hi - lo) * 0.08, 0.5)
    lo, hi = lo - pad, hi + pad
    n = len(pts)
    day0 = date.fromisoformat(pts[0][0]).toordinal()
    span = max(1, date.fromisoformat(pts[-1][0]).toordinal() - day0)

    def x(i: int) -> float:
        return ml + (w - ml - mr) * (date.fromisoformat(pts[i][0]).toordinal() - day0) / span

    def y(v: float) -> float:
        return mt + (h - mt - mb) * (hi - v) / (hi - lo)

    def line(idx: int, colour: str) -> str:
        segs, cur = [], []
        for i, p in enumerate(pts):
            if p[idx] is None:
                if cur:
                    segs.append(cur)
                cur = []
            else:
                cur.append(f"{x(i):.1f},{y(p[idx] * 100):.1f}")
        if cur:
            segs.append(cur)
        return "".join(
            f'<polyline fill="none" stroke="{colour}" stroke-width="2" points="{" ".join(s)}"/>'
            for s in segs
        )

    step = _nice_step(hi - lo)
    ticks, t = [], step * (lo // step)
    while t <= hi:
        if t >= lo:
            ticks.append(t)
        t += step
    grid = "".join(
        f'<line x1="{ml}" x2="{w - mr}" y1="{y(t):.1f}" y2="{y(t):.1f}" '
        f'stroke="{"#98a2b3" if abs(t) < 1e-9 else "#eef0f4"}"/>'
        f'<text x="{ml - 6}" y="{y(t) + 4:.1f}" text-anchor="end">{t:+.0f}%</text>'
        for t in ticks
    )
    labels, last_x = [], -1e9
    for i in range(n):
        if x(i) - last_x >= 105 and x(i) <= w - mr - 40:
            labels.append(i)
            last_x = x(i)
    xl = "".join(
        f'<text x="{x(i):.1f}" y="{h - 12}" text-anchor="middle">{_e(pts[i][0])}</text>'
        for i in labels
    )
    legend = (
        f'<rect x="{ml}" y="6" width="14" height="3" fill="#1f5fbf"/>'
        f'<text x="{ml + 18}" y="11">{_e(name)}</text>'
        f'<rect x="{ml + 300}" y="6" width="14" height="3" fill="#e07b00"/>'
        f'<text x="{ml + 318}" y="11">{_e(benchmark)} (benchmark)</text>'
    )
    return (
        f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" '
        f'aria-label="Profit and loss of {_e(name)} against {_e(benchmark)}">'
        f'{legend}{grid}{xl}{line(1, "#1f5fbf")}{line(2, "#e07b00")}</svg>'
    )


def _page(title: str, body: str, root: str) -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{_e(title)}</title><style>{CSS}</style></head><body><main>"
        f'<p class="small"><a href="{root}index.html">Stock groups</a> '
        '<span class="badge">Paper trading</span></p>'
        f"{body}<div class=note>{DISCLAIMER}</div>"
        "<footer>Built by <code>stocks.publish.PublishSite</code> · "
        f'<a href="{FFL_URL}">view FFL</a> · <a href="{REPO_URL}">source repo</a></footer>'
        "</main></body></html>\n"
    )


COLUMNS = (
    ("Value", "the group's notional value at the last snapshot (closed groups: at the close)."),
    ("P/L, P/L %", "profit or loss on the money put in, in dollars and as a percent of it."),
    ("Benchmark", "how much the benchmark index fund (SPY unless stated) moved over the same days."),
    ("vs benchmark", "the group's P/L % minus the benchmark's. Positive means the picks beat holding the index."),
    (
        "Planned end",
        "the end of the holding period the group was opened with. A group stays open, "
        "and is marked every weekday, until someone closes it.",
    ),
)


def _defs() -> str:
    items = "".join(f"<dt>{_e(a)}</dt><dd>{_e(b)}</dd>" for a, b in COLUMNS)
    return f"<dl class=defs>{items}</dl>"


def _table(views: list[dict], open_: bool) -> str:
    if not views:
        return "<p class=muted>None.</p>"
    head = (
        "<tr><th>Group</th><th>Opened</th>"
        + (
            "<th>Planned end</th><th>Last snapshot</th>"
            if open_
            else "<th>Closed</th><th class=n>Days held</th>"
        )
        + "<th class=n>Holdings</th><th class=n>Value</th><th class=n>P/L</th><th class=n>P/L %</th>"
        "<th class=n>Benchmark</th><th class=n>vs benchmark</th></tr>"
    )
    rows = []
    for v in views:
        g = v["group"]
        s = g.get("strategy") or {}
        if open_:
            mid = f"<td>{_e(v['planned_end'])}</td><td>{_e(v['last'])}</td>"
        else:
            held = (g.get("final") or {}).get("held_days")
            mid = f"<td>{_e(_day(g.get('closed_at')))}</td><td class=n>{_e(held)}</td>"
        rows.append(
            f'<tr><td><a href="g/{_e(g["group_id"])}.html">{_e(g.get("name"))}</a>'
            f'<div class="muted small">{_e(s.get("universe"))} · top {_e(s.get("size"))}</div></td>'
            f"<td>{_e(_day(g.get('opened_at')))}</td>{mid}"
            f"<td class=n>{_e(g.get('position_count'))}</td>"
            f"<td class=n>{money(v['value'])}</td><td class=n>{signed_money(v['pl_abs'])}</td>"
            f"<td class=n>{pct(v['pl'])}</td><td class=n>{pct(v['bench'])}</td>"
            f"<td class=n>{pct(v['alpha'])}</td></tr>"
        )
    return f"<div class=wrap><table>{head}{''.join(rows)}</table></div>"


def index_html(views: list[dict]) -> str:
    opn = [v for v in views if not v["closed"]]
    cls = [v for v in views if v["closed"]]
    beat = sum(1 for v in cls if v["alpha"] is not None and v["alpha"] > 0)
    last = max((v["last"] for v in views if v["last"]), default=None)
    kpis = (
        f"<div class=kpi><div class=l>Open groups</div><div class=v>{len(opn)}</div></div>"
        f"<div class=kpi><div class=l>Closed groups</div><div class=v>{len(cls)}</div></div>"
        "<div class=kpi><div class=l>Closed groups that beat the benchmark</div>"
        f"<div class=v>{beat} of {len(cls)}</div></div>"
    )
    body = (
        "<h1>Stock groups</h1>"
        "<p class=muted>Groups of stocks picked by a factor screen (momentum, relative strength, "
        "closeness to the 52-week high, volatility), analyst consensus and, optionally, a "
        "language-model re-rank, then marked to market after the close each weekday and "
        f"compared with the index. Latest snapshot: <b>{_e(last or 'none')}</b>.</p>"
        f"<div class=kpis>{kpis}</div>"
        "<h2>Open groups</h2>" + _table(opn, True)
        + "<h2>Closed groups</h2>" + _table(cls, False)
        + "<h2>What the columns mean</h2>" + _defs()
    )
    return _page("Stock groups (paper trading)", body, "")


def _entry_session(g: dict) -> str:
    """The trading session whose prices the group was bought at."""
    opened = g.get("opened_at")
    if not opened:
        return ""
    moment = opened if isinstance(opened, datetime) else datetime.fromisoformat(str(opened))
    return portfolio.trading_date(moment)


def _why(p: dict, g: dict) -> str:
    if p.get("manual"):
        return "Pinned by hand."
    if p.get("llm_reason"):
        return p["llm_reason"]
    why = f"Rank {p.get('rank')} of {g.get('screened_count')} screened"
    mom = (p.get("factors") or {}).get("mom_12_1")
    if mom is not None:
        why += f"; 12-1 month momentum {mom * 100:+.1f}%"
    return why + "."


def group_html(db, v: dict) -> str:
    g = v["group"]
    s = g.get("strategy") or {}
    bench = g.get("benchmark") or "SPY"
    positions = sorted(
        dbmod.get_positions(db, g["group_id"]), key=lambda p: (-(p.get("pl_abs") or 0), p["ticker"])
    )
    status = "Closed " + _day(g.get("closed_at")) if v["closed"] else "Open, marked each weekday"
    kp = [
        ("Value", money(v["value"]), "cost " + money(g.get("initial_value"))),
        ("Profit / loss", signed_money(v["pl_abs"]), pct(v["pl"]) + " on cost"),
        (f"Benchmark ({_e(bench)})", pct(v["bench"]), "over the same days"),
        ("vs benchmark", pct(v["alpha"]), "group minus benchmark"),
        ("Snapshots", str(len(v["snaps"])), f"{_e(v['first'])} to {_e(v['last'])}"),
    ]
    kpis = "".join(
        f"<div class=kpi><div class=l>{a}</div><div class=v>{b}</div>"
        f"<div class='muted small'>{c}</div></div>"
        for a, b, c in kp
    )
    late = ""
    if not v["closed"] and v["planned_end"] and v["last"] and v["last"] > v["planned_end"]:
        late = (
            f"<p class=small>Planned to end on {_e(v['planned_end'])} after "
            f"{_e(g.get('horizon_days'))} days. It has not been closed, so it is still "
            "marked every weekday.</p>"
        )
    prow = "".join(
        f"<tr><td><b>{_e(p.get('ticker'))}</b></td><td>{_e(p.get('name'))}</td>"
        f"<td>{_e(p.get('sector'))}</td><td class=n>{(p.get('shares') or 0):.4f}</td>"
        f"<td class=n>{money(p.get('entry_price'))}</td>"
        f"<td class=n>{money(p.get('exit_price') or p.get('last_price'))}</td>"
        f"<td class=n>{signed_money(p.get('pl_abs'))}</td><td class=n>{pct(p.get('pl_pct'))}</td>"
        f"<td class=small>{_e(_why(p, g))}</td></tr>"
        for p in positions
    )
    capital = s.get("capital") or 0
    strat = [
        (
            "Universe",
            f"{s.get('universe')}: {g.get('universe_size')} tickers, "
            f"{g.get('screened_count')} passed the filters, {g.get('excluded_count')} excluded",
        ),
        ("Group size", s.get("size")),
        ("Sectors", ", ".join(s.get("sectors") or []) or "all sectors"),
        ("Max per sector", s.get("max_per_sector") or "no cap"),
        ("Notional capital", USD + f"{capital:,.0f}, equal weight, fractional shares"),
        ("Factor weights", json.dumps(s.get("factor_weights") or {}, sort_keys=True)),
        ("Stage weights", json.dumps(s.get("stage_weights") or {}, sort_keys=True)),
        ("Language-model re-rank", "used" if s.get("use_llm") else "not used"),
        ("Price provider", g.get("provider")),
        ("Opened", _day(g.get("opened_at"))),
        ("Holding period", f"{g.get('horizon_days')} days, planned end {v['planned_end']}"),
    ]
    if g.get("skipped"):
        strat.append(("Skipped at open (no quote)", ", ".join(g["skipped"])))
    srows = "".join(f"<tr><td>{_e(a)}</td><td>{_e(b)}</td></tr>" for a, b in strat)
    body = (
        f"<h1>{_e(g.get('name'))}</h1><p class=muted>{_e(status)} · benchmark {_e(bench)}</p>"
        f"{late}<div class=kpis>{kpis}</div>"
        "<h2>Profit and loss since the start</h2>"
        "<p class='muted small'>Percent change from the entry prices, with the benchmark over the "
        "same days. Both start at 0% on the session whose prices the group was bought at; a "
        "straight stretch is a run of days with no snapshot.</p>"
        + chart_svg(v["snaps"], g.get("name") or "", bench, _entry_session(g))
        + "<h2>Holdings</h2><p class='muted small'>Sorted by profit or loss in dollars.</p>"
        "<div class=wrap><table><tr><th>Ticker</th><th>Name</th><th>Sector</th>"
        "<th class=n>Shares</th><th class=n>Entry</th>"
        f"<th class=n>{'Exit' if v['closed'] else 'Last'}</th><th class=n>P/L</th>"
        f"<th class=n>P/L %</th><th>Why it was picked</th></tr>{prow}</table></div>"
        f"<h2>Strategy as run</h2><div class=wrap><table>{srows}</table></div>"
    )
    return _page(f"{g.get('name')}: stock group (paper trading)", body, "../")


def build_site(db, out_dir: str | Path) -> dict[str, Any]:
    """Write index.html and g/GROUP_ID.html under *out_dir*."""
    out = Path(out_dir)
    (out / "g").mkdir(parents=True, exist_ok=True)
    groups = dbmod.list_groups(db, limit=10_000)
    if not groups:
        raise RuntimeError("no stock groups in the database")
    groups.sort(
        key=lambda g: (_day(g.get("opened_at")), g.get("name") or "", g["group_id"]), reverse=True
    )
    views = [group_view(db, g) for g in groups]
    paths = ["index.html"]
    (out / "index.html").write_text(index_html(views), encoding="utf-8")
    for v in views:
        rel = f"g/{v['group']['group_id']}.html"
        (out / rel).write_text(group_html(db, v), encoding="utf-8")
        paths.append(rel)
    return {
        "paths": paths,
        "files": len(paths),
        "bytes": sum((out / p).stat().st_size for p in paths),
        "groups": len(views),
        "open": sum(1 for v in views if not v["closed"]),
        "latest": max((v["last"] for v in views if v["last"]), default=""),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stocks.site", description="Write the static stock-group pages.")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    r = build_site(dbmod.get_db(), a.out)
    print(json.dumps({k: v for k, v in r.items() if k != "paths"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
