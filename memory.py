"""Memory subsystem — simple key/value persistence for agents.

Each memory has an owner (agent name), a key, a value, and a shared flag.
Shared memories are visible to all agents. Private memories are only
visible to the owner.

Functions accept a SQLite connection (or context manager) so the caller
controls transactions.

Schema (created by the server on startup):

    CREATE TABLE memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL DEFAULT '',
        shared INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(owner, key)
    );
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    """ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def memory_save(
    conn: sqlite3.Connection,
    owner: str,
    key: str,
    value: str,
    shared: bool = False,
) -> dict:
    """Upsert a key/value pair.

    If the (owner, key) pair already exists, the value, shared flag, and
    updated_at timestamp are overwritten. Otherwise a new row is inserted.

    Returns a dict with the saved entry metadata.
    """
    now = _now()
    conn.execute(
        """
        INSERT INTO memories (owner, key, value, shared, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(owner, key) DO UPDATE SET
            value = excluded.value,
            shared = excluded.shared,
            updated_at = excluded.updated_at
        """,
        (owner, key, value, int(shared), now, now),
    )
    conn.commit()
    return {
        "key": key,
        "owner": owner,
        "shared": shared,
        "status": "saved",
    }


def memory_get(
    conn: sqlite3.Connection,
    owner: str,
    key: str,
) -> dict | None:
    """Retrieve a memory by key.

    Looks up the key among the owner's own memories first, then falls back
    to shared memories from any owner. Returns None if not found.
    """
    # Try own memory first (exact owner + key match).
    row = conn.execute(
        "SELECT owner, key, value, shared, created_at, updated_at "
        "FROM memories WHERE owner = ? AND key = ?",
        (owner, key),
    ).fetchone()

    # Fall back to shared memories from other owners.
    if row is None:
        row = conn.execute(
            "SELECT owner, key, value, shared, created_at, updated_at "
            "FROM memories WHERE key = ? AND shared = 1 AND owner != ?",
            (key, owner),
        ).fetchone()

    if row is None:
        return None

    return {
        "owner": row["owner"],
        "key": row["key"],
        "value": row["value"],
        "shared": bool(row["shared"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def memory_list(
    conn: sqlite3.Connection,
    owner: str,
) -> list[dict]:
    """List all keys visible to the owner.

    Returns the owner's own memories plus all shared memories from other
    owners. Each entry includes key, owner, shared flag, and timestamps
    (but not the full value, to keep the listing lightweight).
    """
    rows = conn.execute(
        """
        SELECT owner, key, shared, created_at, updated_at
        FROM memories
        WHERE owner = ? OR shared = 1
        ORDER BY updated_at DESC
        """,
        (owner,),
    ).fetchall()

    return [
        {
            "owner": r["owner"],
            "key": r["key"],
            "shared": bool(r["shared"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def memory_delete(
    conn: sqlite3.Connection,
    owner: str,
    key: str,
) -> dict:
    """Delete one of the owner's own memories.

    Only the owner can delete their own entries — shared memories owned by
    others cannot be deleted through this function.

    Returns a dict indicating whether a row was actually removed.
    """
    cursor = conn.execute(
        "DELETE FROM memories WHERE owner = ? AND key = ?",
        (owner, key),
    )
    conn.commit()
    deleted = cursor.rowcount > 0
    return {
        "key": key,
        "owner": owner,
        "deleted": deleted,
    }
