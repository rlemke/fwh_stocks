"""Backend-aware paths for the stocks domain.

⚠️ A THIN SHIM over ``facetwork.domains.storage``. This module was one of 21
near-copies across the fwh_* repos; the shared layer owns the behaviour now and
this file keeps the import path, the public names, and anything genuinely
specific to stocks.

⚠️ The arguments below pin where this domain's data ALREADY sits in the object
store. They are not style — changing one orphans that data rather than moving
it — and they were verified against the previous module across local, s3:// and
hdfs:// before the switch.
"""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterator
from typing import IO, Any
from facetwork.config import get_output_base
from facetwork.runtime import storage as _fws

from facetwork.domains.storage import domain_storage, is_remote, join  # noqa: F401

_S = domain_storage("stocks")


def cache_root() -> str:
    return _S.cache_root()


def output_root() -> str:
    return _S.output_root()


def exists(path: str) -> bool:
    return _S.exists(path)


def localize(path: str) -> str:
    return _S.localize(path)


def open_read(path: str, mode: str = "r", **kw):
    return _S.open_read(path, mode, **kw)


def open_write(path: str, mode: str = "w", **kw):
    return _S.open_write(path, mode, **kw)


CACHE_VERSION = 1


NAMESPACE = "stocks"


def _data_root() -> str:
    return os.environ.get("FW_DATA_ROOT") or get_output_base()


def cache_path(cache_type: str, relative_path: str) -> str:
    """``cache/<namespace>/<cache_type>/<relative_path>`` per the cache-layout spec."""
    return join(cache_root(), cache_type, relative_path)


def _sidecar_path(path: str) -> str:
    return path + ".meta.json"


def read_json_cached(
    cache_type: str, relative_path: str, max_age_s: float | None = None
) -> Any | None:
    """Return the cached JSON payload, or ``None`` when absent/incomplete/stale."""
    path = cache_path(cache_type, relative_path)
    side = _sidecar_path(path)
    if not exists(side) or not exists(path):
        return None
    try:
        with open_read(side) as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None
    if max_age_s is not None:
        age = time.time() - float(meta.get("generated_at_epoch") or 0)
        if age > max_age_s:
            return None
    try:
        with open_read(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json_cached(
    cache_type: str,
    relative_path: str,
    payload: Any,
    *,
    source: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Write *payload* plus its sidecar. Returns the artifact path."""
    path = cache_path(cache_type, relative_path)
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    raw = body.encode()

    with open_write(path) as fh:
        fh.write(body)

    now = time.time()
    meta = {
        "version": CACHE_VERSION,
        "namespace": NAMESPACE,
        "cache_type": cache_type,
        "relative_path": relative_path,
        "kind": "file",
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "generated_at_epoch": now,
    }
    if source:
        meta["source"] = source
    if extra:
        meta["extra"] = extra

    # Sidecar LAST — this ordering is the whole correctness argument.
    with open_write(_sidecar_path(path)) as fh:
        json.dump(meta, fh, separators=(",", ":"), sort_keys=True)
    return path
