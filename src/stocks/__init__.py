"""Stocks domain — Facetwork workflows + handlers for a stock-group tracker.

Selects a group of stocks with a transparent, reproducible ranking strategy
(factor screen + analyst consensus + an optional LLM re-rank), records the entry
prices, snapshots the group's value daily over a chosen horizon, and on close
writes per-position and overall profit/loss to MongoDB alongside a benchmark
comparison.

This is a PAPER-TRADING tracker. It places no orders, holds no positions, and is
not investment advice. Prices come from a delayed, swappable feed.

Discovered by the Facetwork runner via the ``facetwork.domains`` entry point in
pyproject.toml::

    [project.entry-points."facetwork.domains"]
    stocks = "stocks:domain"
"""

from __future__ import annotations

from pathlib import Path

from facetwork.domains import DomainPackage

from .handlers import register_all_registry_handlers

domain = DomainPackage(
    name="stocks",
    ffl_dir=Path(__file__).parent / "ffl",
    register_handlers=register_all_registry_handlers,
)
