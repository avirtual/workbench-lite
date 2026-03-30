"""
Basic Workbench — Minimal multi-agent coordination for Claude Code.

Single-process server: SQLite DB + MCP tools + REST API + static web UI.
Agents communicate via DMs and channels, persist knowledge in shared memory,
and the operator watches everything live in a web dashboard.
"""

import asyncio
import json
import logging
import os
import signal
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse, FileResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

# MCP SDK
from mcp.server import Server as MCPServer
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent

log = logging.getLogger("workbench")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("WORKBENCH_PORT", "9000"))
HOST = os.environ.get("WORKBENCH_HOST", "127.0.0.1")
DB_PATH = os.environ.get("WORKBENCH_DB_PATH", "./workbench.db")
SEED_DEMO = os.environ.get("WB_SEED_DEMO", "0") == "1"
SCRIPT_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL DEFAULT 'stopped',
    model TEXT DEFAULT 'sonnet',
    cwd TEXT,
    prompt TEXT,
    role TEXT,
    tmux_session TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_agent TEXT NOT NULL,
    to_agent TEXT,
    channel TEXT,
    body TEXT NOT NULL,
    type TEXT DEFAULT 'message',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_to ON messages(to_agent);
CREATE INDEX IF NOT EXISTS idx_msg_channel ON messages(channel);
CREATE INDEX IF NOT EXISTS idx_msg_created ON messages(created_at);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    shared INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner, key)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    agent TEXT NOT NULL,
    channel TEXT NOT NULL,
    PRIMARY KEY (agent, channel)
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    log.info(f"Database initialized at {DB_PATH}")


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------------------
# Agent identity tracking (MCP connection → agent name)
# ---------------------------------------------------------------------------

_agent_connections: dict[str, str] = {}  # connection_id → agent_name
_agent_last_check: dict[str, int] = {}   # agent_name → last seen message id


def identify_agent(connection_id: str, name: str):
    _agent_connections[connection_id] = name


def get_agent_name(connection_id: str) -> str | None:
    return _agent_connections.get(connection_id)


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = MCPServer("basic-workbench")


def _register_tools():
    """Register all 12 MCP tools."""
    import messaging
    import memory as mem

    @mcp.tool()
    async def register(name: str) -> str:
        """Register with the workbench. Call this first with your agent name."""
        # We'll get the connection ID from context in the actual MCP handler
        with db() as conn:
            agent = conn.execute("SELECT name FROM agents WHERE name = ?", (name,)).fetchone()
            if agent:
                conn.execute("UPDATE agents SET status = 'alive', updated_at = ? WHERE name = ?",
                           (now_iso(), name))
        return json.dumps({"status": "registered", "name": name})

    @mcp.tool()
    async def check(name: str) -> str:
        """Check for new messages — DMs and subscribed channels."""
        last_id = _agent_last_check.get(name, 0)
        with db() as conn:
            result = messaging.check(conn, name, last_id)
            if result["dms"] or result["channels"]:
                max_id = max(
                    [m["id"] for m in result["dms"]] +
                    [m["id"] for msgs in result["channels"].values() for m in msgs] +
                    [last_id]
                )
                _agent_last_check[name] = max_id
        return json.dumps(result)

    @mcp.tool()
    async def read_inbox(name: str, after: int = 0) -> str:
        """Read direct messages addressed to you."""
        with db() as conn:
            msgs = messaging.read_inbox(conn, name, after)
        return json.dumps(msgs)

    @mcp.tool()
    async def direct_message(name: str, to: str, body: str, type: str = "message") -> str:
        """Send a direct message to another agent."""
        with db() as conn:
            msg_id = messaging.send_dm(conn, name, to, body, type)
        # Fire SSE event
        from events import event_bus
        event_bus.publish("new_message", {
            "id": msg_id, "from": name, "to": to, "body": body,
            "type": type, "channel": None, "ts": now_iso()
        })
        return json.dumps({"status": "sent", "id": msg_id})

    @mcp.tool()
    async def post(name: str, channel: str, body: str, type: str = "message") -> str:
        """Post a message to a channel."""
        with db() as conn:
            msg_id = messaging.post_to_channel(conn, name, channel, body, type)
        from events import event_bus
        event_bus.publish("new_message", {
            "id": msg_id, "from": name, "to": None, "body": body,
            "type": type, "channel": channel, "ts": now_iso()
        })
        return json.dumps({"status": "posted", "id": msg_id, "channel": channel})

    @mcp.tool()
    async def subscribe(name: str, channel: str) -> str:
        """Subscribe to a channel."""
        with db() as conn:
            messaging.subscribe(conn, name, channel)
        return json.dumps({"status": "subscribed", "channel": channel})

    @mcp.tool()
    async def channels() -> str:
        """List available channels."""
        with db() as conn:
            result = messaging.list_channels(conn)
        return json.dumps(result)

    @mcp.tool()
    async def memory_save(name: str, key: str, value: str, shared: bool = False) -> str:
        """Save a key/value pair to memory."""
        with db() as conn:
            mem.memory_save(conn, name, key, value, shared)
        return json.dumps({"status": "saved", "key": key})

    @mcp.tool()
    async def memory_get(name: str, key: str) -> str:
        """Read a memory entry by key."""
        with db() as conn:
            entry = mem.memory_get(conn, name, key)
        if entry is None:
            return json.dumps({"error": "not found", "key": key})
        return json.dumps(entry)

    @mcp.tool()
    async def memory_list(name: str) -> str:
        """List all memory keys (own + shared)."""
        with db() as conn:
            entries = mem.memory_list(conn, name)
        return json.dumps(entries)

    @mcp.tool()
    async def memory_delete(name: str, key: str) -> str:
        """Delete a memory entry you own."""
        with db() as conn:
            mem.memory_delete(conn, name, key)
        return json.dumps({"status": "deleted", "key": key})

    @mcp.tool()
    async def list_agents() -> str:
        """List all agents and their status."""
        import agent_ops
        with db() as conn:
            agents = agent_ops.list_agents(conn)
        return json.dumps(agents)

    @mcp.tool()
    async def quit(name: str) -> str:
        """Stop yourself."""
        import agent_ops
        with db() as conn:
            agent_ops.stop_agent(name, conn)
        from events import event_bus
        event_bus.publish("agent_status_change", {
            "agent": name, "status": "stopped", "ts": now_iso()
        })
        return json.dumps({"status": "stopped", "agent": name})


# ---------------------------------------------------------------------------
# REST API (for web UI)
# ---------------------------------------------------------------------------

async def api_list_agents(request: Request):
    import agent_ops
    agents = await asyncio.get_event_loop().run_in_executor(None, lambda: (
        agent_ops.list_agents(_connect())
    ))
    return JSONResponse(agents)


async def api_get_agent(request: Request):
    import agent_ops
    name = request.path_params["name"]
    with db() as conn:
        agent = conn.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
        if not agent:
            return JSONResponse({"error": "not found"}, status_code=404)
        result = dict(agent)
        # Get recent output from tmux
        if agent["status"] == "alive" and agent["tmux_session"]:
            output = await asyncio.get_event_loop().run_in_executor(
                None, lambda: agent_ops.tmux_capture(agent["tmux_session"])
            )
            result["output"] = output
        else:
            result["output"] = ""
        # Get recent messages
        msgs = conn.execute(
            "SELECT * FROM messages WHERE from_agent = ? OR to_agent = ? "
            "ORDER BY created_at DESC LIMIT 50", (name, name)
        ).fetchall()
        result["messages"] = [dict(m) for m in msgs]
    return JSONResponse(result)


async def api_spawn_agent(request: Request):
    import agent_ops
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)

    cwd = body.get("cwd", os.getcwd())
    prompt = body.get("prompt", "")
    role = body.get("role", "developer")
    model = body.get("model", "sonnet")

    with db() as conn:
        existing = conn.execute("SELECT name FROM agents WHERE name = ?", (name,)).fetchone()
        if existing:
            return JSONResponse({"error": f"agent '{name}' already exists"}, status_code=409)

    # Build agent boot prompt
    boot_prompt = _build_boot_prompt(name, role, prompt)

    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: agent_ops.spawn_agent(name, cwd, boot_prompt, model, role)
    )

    from events import event_bus
    event_bus.publish("agent_status_change", {
        "agent": name, "status": "alive", "ts": now_iso()
    })
    return JSONResponse(result)


async def api_stop_agent(request: Request):
    import agent_ops
    name = request.path_params["name"]
    with db() as conn:
        agent_ops.stop_agent(name, conn)
    from events import event_bus
    event_bus.publish("agent_status_change", {
        "agent": name, "status": "stopped", "ts": now_iso()
    })
    return JSONResponse({"status": "stopped", "agent": name})


async def api_restart_agent(request: Request):
    import agent_ops
    name = request.path_params["name"]
    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: agent_ops.restart_agent(name)
    )
    from events import event_bus
    event_bus.publish("agent_status_change", {
        "agent": name, "status": "alive", "ts": now_iso()
    })
    return JSONResponse(result)


async def api_list_channels(request: Request):
    import messaging
    with db() as conn:
        result = messaging.list_channels(conn)
    return JSONResponse(result)


async def api_channel_messages(request: Request):
    import messaging
    channel = request.path_params["name"]
    limit = int(request.query_params.get("limit", "100"))
    with db() as conn:
        msgs = messaging.read_channel(conn, channel, limit)
    return JSONResponse(msgs)


async def api_agent_messages(request: Request):
    name = request.path_params["name"]
    limit = int(request.query_params.get("limit", "100"))
    with db() as conn:
        msgs = conn.execute(
            "SELECT * FROM messages WHERE from_agent = ? OR to_agent = ? "
            "ORDER BY created_at DESC LIMIT ?", (name, name, limit)
        ).fetchall()
    return JSONResponse([dict(m) for m in msgs])


async def api_feed_stream(request: Request):
    from events import sse_stream_handler
    return await sse_stream_handler(request)


# ---------------------------------------------------------------------------
# Boot prompt builder
# ---------------------------------------------------------------------------

_DEVELOPER_PROMPT = """You are "{name}", an agent on this workbench.

Other agents online: {agent_list}

Communication — use your MCP tools:
- direct_message(to, body) to message another agent
- post(channel, body) to broadcast to a channel
- check() to see new messages

{user_prompt}"""

_REVIEWER_PROMPT = """You are "{name}", a contrarian reviewer on this workbench.

Your job: when other agents post to #review, critically evaluate their work.

For each review, respond with:
## Findings
List each finding with severity (BLOCKING / HIGH / MEDIUM / LOW):
- What's wrong or risky
- Why it matters
- Concrete mitigation

Be thorough. Challenge assumptions. Find what others miss.
If the work is solid, say so — but always look for what could go wrong.

Other agents: {agent_list}

On startup, subscribe to #review:
1. Call subscribe(channel="#review")
2. Call check() periodically to watch for new posts.

{user_prompt}"""


def _build_boot_prompt(name: str, role: str, user_prompt: str) -> str:
    with db() as conn:
        agents = conn.execute("SELECT name FROM agents WHERE status = 'alive'").fetchall()
    agent_list = ", ".join(a["name"] for a in agents) or "(none yet)"

    template = _REVIEWER_PROMPT if role == "reviewer" else _DEVELOPER_PROMPT
    return template.format(name=name, agent_list=agent_list, user_prompt=user_prompt)


# ---------------------------------------------------------------------------
# Static files & App
# ---------------------------------------------------------------------------

def _serve_index(request: Request):
    index_path = SCRIPT_DIR / "static" / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return HTMLResponse("<h1>Basic Workbench</h1><p>Static files not found.</p>")


routes = [
    Route("/", _serve_index),
    Route("/api/agents", api_list_agents, methods=["GET"]),
    Route("/api/agents", api_spawn_agent, methods=["POST"]),
    Route("/api/agents/{name}", api_get_agent, methods=["GET"]),
    Route("/api/agents/{name}", api_stop_agent, methods=["DELETE"]),
    Route("/api/agents/{name}/restart", api_restart_agent, methods=["POST"]),
    Route("/api/channels", api_list_channels, methods=["GET"]),
    Route("/api/channels/{name}/messages", api_channel_messages, methods=["GET"]),
    Route("/api/agents/{name}/messages", api_agent_messages, methods=["GET"]),
    Route("/api/feed/stream", api_feed_stream, methods=["GET"]),
    Mount("/static", StaticFiles(directory=str(SCRIPT_DIR / "static")), name="static"),
]

app = Starlette(routes=routes)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    init_db()
    _register_tools()

    if HOST != "127.0.0.1":
        log.warning(f"⚠ Server binding to {HOST} — accessible beyond localhost!")

    log.info(f"Basic Workbench starting on http://{HOST}:{PORT}")

    # Handle graceful shutdown
    def _shutdown(sig, frame):
        log.info("Shutting down...")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
