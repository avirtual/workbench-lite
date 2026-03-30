"""Messaging subsystem for basic-workbench.

Provides agent-to-agent direct messages and channel-based group messaging.
All state lives in SQLite (messages + subscriptions tables). No external
dependencies beyond the standard library.

Expected schema (created by workbench.py):

    CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_agent TEXT NOT NULL,
        to_agent TEXT,
        channel TEXT,
        body TEXT NOT NULL,
        type TEXT DEFAULT 'message',
        created_at TEXT NOT NULL
    );
    CREATE TABLE subscriptions (
        agent TEXT NOT NULL,
        channel TEXT NOT NULL,
        PRIMARY KEY (agent, channel)
    );
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger("messaging")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    """ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Direct messages
# ---------------------------------------------------------------------------

def send_dm(
    conn: sqlite3.Connection,
    from_agent: str,
    to_agent: str,
    body: str,
    msg_type: str = "message",
) -> dict:
    """Send a direct message from one agent to another.

    Returns the stored message as a dict (including its assigned id).
    """
    if not body or not body.strip():
        return {"error": "body cannot be empty"}
    ts = _now()
    cur = conn.execute(
        "INSERT INTO messages (from_agent, to_agent, body, type, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (from_agent, to_agent, body, msg_type, ts),
    )
    conn.commit()
    return {
        "id": cur.lastrowid,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "body": body,
        "type": msg_type,
        "created_at": ts,
    }


def read_inbox(
    conn: sqlite3.Connection,
    agent: str,
    after: int | None = None,
    limit: int = 50,
) -> list[dict]:
    """Read DMs addressed to *agent*, newest first, cursor-based.

    Args:
        agent: The receiving agent name.
        after: Return only messages with id > this value (cursor).
        limit: Max messages to return.

    Returns a list of message dicts.
    """
    if after is not None:
        rows = conn.execute(
            "SELECT id, from_agent, to_agent, body, type, created_at "
            "FROM messages WHERE to_agent = ? AND id > ? "
            "ORDER BY id ASC LIMIT ?",
            (agent, after, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, from_agent, to_agent, body, type, created_at "
            "FROM messages WHERE to_agent = ? "
            "ORDER BY id ASC LIMIT ?",
            (agent, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Channel posting
# ---------------------------------------------------------------------------

def post_to_channel(
    conn: sqlite3.Connection,
    from_agent: str,
    channel: str,
    body: str,
    msg_type: str = "message",
) -> dict:
    """Post a message to a named channel.

    Channels are created implicitly on first post. Returns the stored message.
    """
    if not body or not body.strip():
        return {"error": "body cannot be empty"}
    ts = _now()
    cur = conn.execute(
        "INSERT INTO messages (from_agent, channel, body, type, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (from_agent, channel, body, msg_type, ts),
    )
    conn.commit()
    return {
        "id": cur.lastrowid,
        "from_agent": from_agent,
        "channel": channel,
        "body": body,
        "type": msg_type,
        "created_at": ts,
    }


def read_channel(
    conn: sqlite3.Connection,
    channel: str,
    after: int | None = None,
    limit: int = 50,
) -> list[dict]:
    """Read messages from a channel, cursor-based.

    Args:
        channel: Channel name.
        after: Return only messages with id > this value (cursor).
        limit: Max messages to return.

    Returns a list of message dicts ordered by id ascending.
    """
    if after is not None:
        rows = conn.execute(
            "SELECT id, from_agent, channel, body, type, created_at "
            "FROM messages WHERE channel = ? AND id > ? "
            "ORDER BY id ASC LIMIT ?",
            (channel, after, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, from_agent, channel, body, type, created_at "
            "FROM messages WHERE channel = ? "
            "ORDER BY id ASC LIMIT ?",
            (channel, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

def subscribe(conn: sqlite3.Connection, agent: str, channel: str) -> dict:
    """Subscribe an agent to a channel.

    Idempotent -- subscribing twice is a no-op.
    Returns the current subscription list for the agent.
    """
    conn.execute(
        "INSERT OR IGNORE INTO subscriptions (agent, channel) VALUES (?, ?)",
        (agent, channel),
    )
    conn.commit()
    return {
        "status": "subscribed",
        "channel": channel,
        "subscriptions": list_subscriptions(conn, agent),
    }


def unsubscribe(conn: sqlite3.Connection, agent: str, channel: str) -> dict:
    """Unsubscribe an agent from a channel.

    Idempotent -- unsubscribing when not subscribed is a no-op.
    Returns the remaining subscription list.
    """
    conn.execute(
        "DELETE FROM subscriptions WHERE agent = ? AND channel = ?",
        (agent, channel),
    )
    conn.commit()
    return {
        "status": "unsubscribed",
        "channel": channel,
        "subscriptions": list_subscriptions(conn, agent),
    }


def list_subscriptions(conn: sqlite3.Connection, agent: str) -> list[str]:
    """Return the list of channels an agent is subscribed to."""
    rows = conn.execute(
        "SELECT channel FROM subscriptions WHERE agent = ? ORDER BY channel",
        (agent,),
    ).fetchall()
    return [r[0] if isinstance(r, (tuple, list)) else r["channel"] for r in rows]


# ---------------------------------------------------------------------------
# Channel listing
# ---------------------------------------------------------------------------

def list_channels(conn: sqlite3.Connection) -> list[dict]:
    """List all channels that have at least one message.

    Returns channel name, message count, and most recent activity timestamp.
    """
    rows = conn.execute(
        "SELECT channel, COUNT(*) AS message_count, MAX(created_at) AS last_activity "
        "FROM messages WHERE channel IS NOT NULL "
        "GROUP BY channel ORDER BY last_activity DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Convenience: check for new messages (DMs + subscribed channels)
# ---------------------------------------------------------------------------

def check(
    conn: sqlite3.Connection,
    agent: str,
    after: int | None = None,
) -> dict:
    """One-call poll: return new DMs and new messages in subscribed channels.

    Args:
        agent: The calling agent's name.
        after: Global cursor -- only messages with id > this value are returned.
               If None, returns all messages (use the max id from the result as
               your next cursor).

    Returns {"dms": [...], "channels": {channel_name: [...]}}
    """
    dms = read_inbox(conn, agent, after=after)
    subscribed = list_subscriptions(conn, agent)
    channels: dict[str, list[dict]] = {}
    for ch in subscribed:
        msgs = read_channel(conn, ch, after=after)
        if msgs:
            channels[ch] = msgs
    return {"dms": dms, "channels": channels}
