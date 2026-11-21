"""Thin dispatchers for the stocks facets.

Every handler here does three things and nothing else: coerce the FFL payload,
call :mod:`stocks.service`, and shape the return. All domain logic lives in the
service/screener/portfolio modules so the terminal and the runtime exercise one
code path.

The single ``handle(payload)`` entry point with a ``_DISPATCH`` table is
deliberate: the runner's handler cache is keyed on (module, entrypoint), so
several facets sharing a module must share one entrypoint.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from typing import Any

from .. import db as dbmod
from .. import market
from ..service import GroupError, close_group, open_group, snapshot_open_groups

logger = logging.getLogger("stocks.handlers")

DATA_NS = "stocks.data"
NS = "stocks.groups"
SITE_NS = "stocks.site"


def _db():
    db = dbmod.get_db()
    dbmod.ensure_indexes(db)
    return db


def _json_param(value: Any, default: dict | None = None) -> dict:
    """FFL passes structured config as a JSON string; empty means 'use defaults'."""
    if not value:
        return dict(default or {})
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise GroupError(f"could not parse JSON parameter {value!r}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise GroupError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _log_wrapper(params: dict) -> Callable[..., None] | None:
    return params.get("_step_log")


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


def handle_resolve_universe(params: dict[str, Any]) -> dict[str, Any]:
    step_log = _log_wrapper(params)
    universe = params.get("universe") or "sp500"
    try:
        if universe == "manual":
            # Nothing to resolve; the pinned list is the universe.
            if step_log:
                step_log("Manual universe: tickers come from the pinned list", level="info")
            return {"ticker_count": 0}
        tickers = market.resolve_universe(universe)
        if step_log:
            step_log(f"ResolveUniverse: {universe} -> {len(tickers)} tickers", level="success")
        return {"ticker_count": len(tickers)}
    except Exception as exc:
        if step_log:
            step_log(f"ResolveUniverse failed: {exc}", level="error")
        raise


def handle_fetch_daily_bars(params: dict[str, Any]) -> dict[str, Any]:
    step_log = _log_wrapper(params)
    universe = params.get("universe") or "sp500"
    benchmark = params.get("benchmark") or "SPY"
    pinned = [
        t.strip().upper()
        for t in str(params.get("pinned") or "").replace(";", ",").split(",")
        if t.strip()
    ]
    try:
        tickers = pinned if universe == "manual" else market.resolve_universe(universe)
        tickers = list(dict.fromkeys([*tickers, *pinned]))
        if not tickers:
            raise GroupError(f"universe {universe!r} resolved to no tickers")
        bars = market.fetch_daily_bars(
            tickers,
            lookback_days=int(params.get("lookback_days") or 400),
            benchmark=benchmark,
            force=bool(params.get("force_refresh")),
        )
        if step_log:
            step_log(
                f"FetchDailyBars: {len(bars)} of {len(tickers) + 1} symbols cached",
                level="success" if len(bars) > len(tickers) / 2 else "warning",
            )
        return {"symbol_count": len(bars)}
    except Exception as exc:
        if step_log:
            step_log(f"FetchDailyBars failed: {exc}", level="error")
        raise


def handle_open_group(params: dict[str, Any]) -> dict[str, Any]:
    step_log = _log_wrapper(params)
    try:
        result = open_group(
            _db(),
            name=params.get("name") or "Untitled group",
            universe=params.get("universe") or "sp500",
            size=int(params.get("size") or 10),
            capital=float(params.get("capital") or 100_000.0),
            horizon_days=int(params.get("horizon_days") or 7),
            benchmark=params.get("benchmark") or "SPY",
            pinned=params.get("pinned") or "",
            use_llm=bool(params.get("use_llm")),
            sectors=params.get("sectors") or "",
            max_per_sector=int(params.get("max_per_sector") or 0),
            factor_weights=_json_param(params.get("factor_weights")) or None,
            stage_weights=_json_param(params.get("stage_weights")) or None,
            lookback_days=int(params.get("lookback_days") or 400),
            force_refresh=bool(params.get("force_refresh")),
            step_log=step_log,
        )
        return {
            "group_id": result["group_id"],
            "ticker_count": result["position_count"],
            "initial_value": result["initial_value"],
            "tickers": ", ".join(result["tickers"]),
        }
    except Exception as exc:
        if step_log:
            step_log(f"OpenGroup failed: {exc}", level="error")
        raise


def handle_snapshot_groups(params: dict[str, Any]) -> dict[str, Any]:
    step_log = _log_wrapper(params)
    try:
        result = snapshot_open_groups(
            _db(), group_id=(params.get("group_id") or "").strip(), step_log=step_log
        )
        return {
            "groups_snapshotted": result["count"],
            "failed_count": len(result["failed"]),
        }
    except Exception as exc:
        if step_log:
            step_log(f"SnapshotGroups failed: {exc}", level="error")
        raise


def handle_close_group(params: dict[str, Any]) -> dict[str, Any]:
    step_log = _log_wrapper(params)
    group_id = (params.get("group_id") or "").strip()
    if not group_id:
        raise GroupError("close requires a group_id")
    try:
        result = close_group(_db(), group_id, step_log=step_log)
        return {
            "group_id": result["group_id"],
            "pl_abs": result["pl_abs"],
            "pl_pct": result["pl_pct"],
            "benchmark_pl_pct": (
                result["benchmark_pl_pct"] if result["benchmark_pl_pct"] is not None else 0.0
            ),
            "winners": result["winners"],
        }
    except Exception as exc:
        if step_log:
            step_log(f"CloseGroup failed: {exc}", level="error")
        raise


def handle_site_build(params: dict[str, Any]) -> dict[str, Any]:
    """Render the static pages locally, then copy them to the domain's output store.

    Staged locally because an object store has no partial writes; every page is
    regenerated each run, so a crash halfway is repaired by the next run.
    """
    import tempfile
    from pathlib import Path

    from ..site import build_site
    from ..storage import _S, is_remote, join

    dest = join(_S.output_root(), "site")
    with tempfile.TemporaryDirectory(prefix="stocks_site_") as tmp:
        out = build_site(_db(), tmp)
        for rel in out["paths"]:
            target = join(dest, rel)
            if not is_remote(target):
                os.makedirs(os.path.dirname(target), exist_ok=True)
            _S.write_bytes(target, Path(tmp, rel).read_bytes())
    log = _log_wrapper(params)
    if log:
        log(
            f"Wrote {out['files']} pages ({out['groups']} groups, latest {out['latest']}) to {dest}",
            level="success",
        )
    return {"output_dir": dest, "files": out["files"], "latest": out["latest"]}


_DISPATCH: dict[str, Callable[[dict], dict]] = {
    f"{SITE_NS}.Build": handle_site_build,
    f"{DATA_NS}.ResolveUniverse": handle_resolve_universe,
    f"{DATA_NS}.FetchDailyBars": handle_fetch_daily_bars,
    f"{NS}.OpenGroup": handle_open_group,
    f"{NS}.SnapshotGroups": handle_snapshot_groups,
    f"{NS}.CloseGroup": handle_close_group,
}


def handle(payload: dict) -> dict:
    facet = payload["_facet_name"]
    handler = _DISPATCH.get(facet)
    if handler is None:
        raise ValueError(f"Unknown facet: {facet}")
    return handler(payload)


def register_handlers(runner) -> None:
    """RegistryRunner path — one module_uri/entrypoint pair per facet."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )


def register_poller(poller) -> None:
    """Legacy AgentPoller path."""
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
