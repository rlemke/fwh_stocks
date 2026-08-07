"""Backend-aware paths for the stocks cache + outputs.

On the fleet (``FW_STORAGE=s3`` / ``FW_DATA_ROOT=s3://afl-cache``) cached daily
bars and analyst snapshots land in the shared MinIO object store, so every runner
on every host resolves the same cache. A thin wrapper over
``facetwork.runtime.storage`` — the same shape migration / census-us / save-earth
use — rooted at ``$FW_DATA_ROOT/cache/stocks/``.

Cache artifacts follow the sidecar protocol from
``agent-spec/cache-layout.agent-spec.yaml``: stage, checksum, atomic publish, then
write ``<artifact>.meta.json`` LAST. A missing sidecar means "not cached".
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

CACHE_VERSION = 1
NAMESPACE = "stocks"


def is_remote(path: str) -> bool:
    return "://" in (path or "")


def _data_root() -> str:
    return os.environ.get("FW_DATA_ROOT") or get_output_base()


def join(*parts: str) -> str:
    parts = tuple(p for p in parts if p)
    if not parts:
        return ""
    base = parts[0].rstrip("/")
    rest = [p.strip("/") for p in parts[1:]]
    return "/".join([base, *[p for p in rest if p]])


def cache_root() -> str:
    ov = os.environ.get("FW_STOCKS_CACHE_DIR")
    if ov:
        return ov
    r = _data_root()
    return join(r, "cache", NAMESPACE, "cache") if is_remote(r) else join(r, "stocks-cache")


def output_root() -> str:
    ov = os.environ.get("FW_STOCKS_OUTPUT_DIR")
    if ov:
        return ov
    r = _data_root()
    return join(r, "cache", NAMESPACE, "output") if is_remote(r) else join(r, "stocks-output")


def cache_path(cache_type: str, relative_path: str) -> str:
    """``cache/<namespace>/<cache_type>/<relative_path>`` per the cache-layout spec."""
    return join(cache_root(), cache_type, relative_path)


def exists(path: str) -> bool:
    return _fws.get_storage_backend(path).exists(path)


def localize(path: str) -> str:
    if not is_remote(path):
        return path
    return _fws.localize(path)


def open_read(path: str, mode: str = "r", **kw) -> IO:
    return open(localize(path), mode, **kw)


@contextlib.contextmanager
def open_write(path: str, mode: str = "w", **kw) -> Iterator[IO]:
    if not is_remote(path):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        f = open(path, mode, **kw)
        try:
            yield f
        finally:
            f.close()
        return

    fd, tmp = tempfile.mkstemp(suffix="_" + os.path.basename(path))
    os.close(fd)
    f = open(tmp, mode, **kw)
    try:
        yield f
        f.close()
        with open(tmp, "rb") as src, _fws.get_storage_backend(path).open(path, "wb") as dst:
            dst.write(src.read())
    finally:
        if not f.closed:
            f.close()
        try:
            os.unlink(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Sidecar-backed JSON cache
#
# Read protocol: no sidecar => not cached. Write protocol: payload first, then
# the sidecar, so a crash between the two leaves an INCOMPLETE entry that the
# reader correctly treats as absent rather than as valid data.
# --------------------------------------------------------------------------


def _sidecar_path(path: str) -> str:
    return path + ".meta.json"


def read_json_cached(cache_type: str, relative_path: str, max_age_s: float | None = None) -> Any | None:
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
