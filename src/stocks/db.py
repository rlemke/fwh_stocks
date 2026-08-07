"""MongoDB access for the stocks domain.

Domain data lives in ``FW_EXAMPLES_DATABASE`` (default ``facetwork_examples``),
separate from the FFL runtime database — the same split the climate/census
dashboard apps use. Raw pymongo with hand-built documents; there is no ORM in
this codebase and adding one here would be out of keeping.

Indexes are created imperatively by :func:`ensure_indexes`, mirroring
``facetwork/runtime/mongo_store/base.py``. The unique index on
``(group_id, snapshot_date)`` is load-bearing: it is what makes a re-run on the
same trading day correct the existing snapshot instead of appending a duplicate.
"""

from __future__ import annotations

import os
from typing import Any

import pymongo

GROUPS = "stock_groups"
POSITIONS = "stock_positions"
SNAPSHOTS = "stock_snapshots"
CANDIDATES = "stock_candidates"

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"


def database_name() -> str:
    return os.environ.get("FW_EXAMPLES_DATABASE", "facetwork_examples")


def mongo_url() -> str:
    return (
        os.environ.get("FW_MONGODB_URL")
        or os.environ.get("MONGODB_URL")
        or "mongodb://localhost:27017"
    )


def get_db(client=None):
    """Return the stocks database.

    Pass an existing client (the dashboard passes ``store._db.client``) to reuse
    its connection pool; handlers with no client to hand open their own.
    """
    if client is None:
        client = pymongo.MongoClient(mongo_url())
    return client[database_name()]


def ensure_indexes(db) -> None:
    db[GROUPS].create_index([("group_id", pymongo.ASCENDING)], unique=True)
    db[GROUPS].create_index([("status", pymongo.ASCENDING), ("created_at", pymongo.DESCENDING)])
    db[POSITIONS].create_index(
        [("group_id", pymongo.ASCENDING), ("ticker", pymongo.ASCENDING)], unique=True
    )
    db[SNAPSHOTS].create_index(
        [("group_id", pymongo.ASCENDING), ("snapshot_date", pymongo.ASCENDING)], unique=True
    )
    db[SNAPSHOTS].create_index([("group_id", pymongo.ASCENDING), ("taken_at", pymongo.ASCENDING)])
    db[CANDIDATES].create_index(
        [("group_id", pymongo.ASCENDING), ("ticker", pymongo.ASCENDING)], unique=True
    )
    db[CANDIDATES].create_index([("group_id", pymongo.ASCENDING), ("rank", pymongo.ASCENDING)])


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def get_group(db, group_id: str) -> dict | None:
    return db[GROUPS].find_one({"group_id": group_id}, {"_id": 0})


def list_groups(db, status: str | None = None, limit: int = 200) -> list[dict]:
    query = {"status": status} if status else {}
    return list(
        db[GROUPS].find(query, {"_id": 0}).sort("created_at", pymongo.DESCENDING).limit(limit)
    )


def list_open_groups(db) -> list[dict]:
    return list_groups(db, status=STATUS_OPEN)


def get_positions(db, group_id: str) -> list[dict]:
    return list(db[POSITIONS].find({"group_id": group_id}, {"_id": 0}).sort("rank", 1))


def get_snapshots(db, group_id: str) -> list[dict]:
    return list(db[SNAPSHOTS].find({"group_id": group_id}, {"_id": 0}).sort("snapshot_date", 1))


def get_latest_snapshot(db, group_id: str) -> dict | None:
    return db[SNAPSHOTS].find_one(
        {"group_id": group_id}, {"_id": 0}, sort=[("snapshot_date", pymongo.DESCENDING)]
    )


def get_candidates(db, group_id: str, limit: int = 0) -> list[dict]:
    cursor = db[CANDIDATES].find({"group_id": group_id}, {"_id": 0}).sort("rank", 1)
    if limit:
        cursor = cursor.limit(limit)
    return list(cursor)


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def upsert_group(db, group: dict[str, Any]) -> None:
    db[GROUPS].update_one({"group_id": group["group_id"]}, {"$set": group}, upsert=True)


def set_group_fields(db, group_id: str, fields: dict[str, Any]) -> None:
    db[GROUPS].update_one({"group_id": group_id}, {"$set": fields})


def replace_positions(db, group_id: str, positions: list[dict]) -> None:
    """Positions are written once at open; a re-open replaces them wholesale."""
    db[POSITIONS].delete_many({"group_id": group_id})
    if positions:
        db[POSITIONS].insert_many([{**p, "group_id": group_id} for p in positions])


def update_position(db, group_id: str, ticker: str, fields: dict[str, Any]) -> None:
    db[POSITIONS].update_one({"group_id": group_id, "ticker": ticker}, {"$set": fields})


def replace_candidates(db, group_id: str, candidates: list[dict]) -> None:
    db[CANDIDATES].delete_many({"group_id": group_id})
    if candidates:
        db[CANDIDATES].insert_many([{**c, "group_id": group_id} for c in candidates])


def upsert_snapshot(db, group_id: str, snapshot_date: str, snapshot: dict[str, Any]) -> None:
    """Idempotent per trading day — a repeated same-day run corrects, never duplicates."""
    db[SNAPSHOTS].update_one(
        {"group_id": group_id, "snapshot_date": snapshot_date},
        {"$set": {**snapshot, "group_id": group_id, "snapshot_date": snapshot_date}},
        upsert=True,
    )


def delete_group(db, group_id: str) -> None:
    for collection in (GROUPS, POSITIONS, SNAPSHOTS, CANDIDATES):
        db[collection].delete_many({"group_id": group_id})
