"""GICS sectors — the canonical list, and resolution of everyday names onto it.

Sector comes free with the S&P 500 constituent list (a ``GICS Sector`` column),
so classifying a whole universe costs one cached HTTP call rather than a
per-ticker lookup.

Two things this module deliberately does NOT do:

* It does not invent a taxonomy. GICS is the standard the constituent data is
  published in, so it is used verbatim; a home-grown grouping would not match
  what any other tool calls the same stock.
* It does not guess on ambiguous input. "services" could reasonably mean
  Communication Services or Consumer Discretionary, and silently picking one
  would produce a group the caller did not ask for. Ambiguous and unknown names
  raise with the candidates named.
"""

from __future__ import annotations

__all__ = [
    "SECTORS",
    "SectorError",
    "resolve_sector",
    "resolve_sectors",
    "normalize_sector",
]


class SectorError(ValueError):
    """A sector name could not be resolved to exactly one GICS sector."""


#: The eleven GICS sectors, as they appear in the constituent data.
SECTORS: tuple[str, ...] = (
    "Communication Services",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Financials",
    "Health Care",
    "Industrials",
    "Information Technology",
    "Materials",
    "Real Estate",
    "Utilities",
)

# Everyday words -> GICS sector. Keys are lowercase and punctuation-free; see
# _key(). Kept deliberately generous, because "tech" and "medical" are what
# people actually type.
_ALIASES: dict[str, str] = {
    # Information Technology
    "tech": "Information Technology",
    "technology": "Information Technology",
    "informationtechnology": "Information Technology",
    "it": "Information Technology",
    "software": "Information Technology",
    "hardware": "Information Technology",
    "semiconductor": "Information Technology",
    "semiconductors": "Information Technology",
    "semis": "Information Technology",
    "chips": "Information Technology",
    # Health Care
    "health": "Health Care",
    "healthcare": "Health Care",
    "medical": "Health Care",
    "medicine": "Health Care",
    "pharma": "Health Care",
    "pharmaceutical": "Health Care",
    "pharmaceuticals": "Health Care",
    "biotech": "Health Care",
    "biotechnology": "Health Care",
    # Financials
    "financial": "Financials",
    "financials": "Financials",
    "finance": "Financials",
    "bank": "Financials",
    "banks": "Financials",
    "banking": "Financials",
    "insurance": "Financials",
    # Industrials
    "industrial": "Industrials",
    "industrials": "Industrials",
    "manufacturing": "Industrials",
    "aerospace": "Industrials",
    "defense": "Industrials",
    "transport": "Industrials",
    "transportation": "Industrials",
    # Consumer Discretionary
    "consumerdiscretionary": "Consumer Discretionary",
    "discretionary": "Consumer Discretionary",
    "retail": "Consumer Discretionary",
    "autos": "Consumer Discretionary",
    "automotive": "Consumer Discretionary",
    "luxury": "Consumer Discretionary",
    "travel": "Consumer Discretionary",
    "restaurants": "Consumer Discretionary",
    # Consumer Staples
    "consumerstaples": "Consumer Staples",
    "staples": "Consumer Staples",
    "food": "Consumer Staples",
    "beverage": "Consumer Staples",
    "beverages": "Consumer Staples",
    "grocery": "Consumer Staples",
    "household": "Consumer Staples",
    "tobacco": "Consumer Staples",
    # Energy
    "energy": "Energy",
    "oil": "Energy",
    "gas": "Energy",
    "oilandgas": "Energy",
    "petroleum": "Energy",
    # Utilities
    "utility": "Utilities",
    "utilities": "Utilities",
    "power": "Utilities",
    "electric": "Utilities",
    "electricity": "Utilities",
    # Real Estate
    "realestate": "Real Estate",
    "property": "Real Estate",
    "reit": "Real Estate",
    "reits": "Real Estate",
    # Materials
    "material": "Materials",
    "materials": "Materials",
    "mining": "Materials",
    "miners": "Materials",
    "chemicals": "Materials",
    "metals": "Materials",
    # Communication Services
    "communication": "Communication Services",
    "communications": "Communication Services",
    "communicationservices": "Communication Services",
    "telecom": "Communication Services",
    "telecoms": "Communication Services",
    "telecommunications": "Communication Services",
    "media": "Communication Services",
    "entertainment": "Communication Services",
}

# Words that genuinely map to more than one sector. Resolving these silently
# would hand back a group the caller did not ask for.
_AMBIGUOUS: dict[str, tuple[str, ...]] = {
    "service": ("Communication Services", "Consumer Discretionary"),
    "services": ("Communication Services", "Consumer Discretionary"),
    "consumer": ("Consumer Discretionary", "Consumer Staples"),
    "industry": ("Industrials", "Materials"),
}


def _key(name: str) -> str:
    """Fold a name to its lookup key: lowercase, alphanumerics only."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def normalize_sector(name: str) -> str | None:
    """Return the canonical GICS sector for *name*, or ``None`` if unresolved.

    Never raises and never guesses on an ambiguous name — use
    :func:`resolve_sector` when the caller should hear about a bad name.
    """
    key = _key(name)
    if not key:
        return None
    for sector in SECTORS:
        if _key(sector) == key:
            return sector
    if key in _AMBIGUOUS:
        return None
    return _ALIASES.get(key)


def resolve_sector(name: str) -> str:
    """Resolve one sector name, raising :class:`SectorError` if it cannot be."""
    key = _key(name)
    if key in _AMBIGUOUS:
        options = " or ".join(repr(s) for s in _AMBIGUOUS[key])
        raise SectorError(
            f"{name!r} is ambiguous - did you mean {options}? "
            "Name the sector exactly so the group is the one you asked for."
        )
    resolved = normalize_sector(name)
    if resolved is None:
        raise SectorError(f"Unknown sector {name!r}. Valid sectors: {', '.join(SECTORS)}.")
    return resolved


def resolve_sectors(names: str | list[str] | None) -> list[str]:
    """Resolve a comma/semicolon-separated list (or list) of sector names.

    Returns ``[]`` for empty input, which callers read as "every sector".
    Duplicates collapse; order follows :data:`SECTORS` so a group's recorded
    strategy is stable regardless of how the caller typed it.
    """
    if not names:
        return []
    if isinstance(names, str):
        parts = names.replace(";", ",").split(",")
    else:
        parts = list(names)
    resolved = {resolve_sector(p) for p in parts if str(p).strip()}
    return [s for s in SECTORS if s in resolved]
