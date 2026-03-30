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

from dotenv import load_dotenv
load_dotenv()

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse, FileResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

# MCP SDK
from mcp.server.fastmcp import FastMCP

log = logging.getLogger("workbench")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("WORKBENCH_PORT", "9800"))
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
# Agent identity tracking (MCP session → agent name)
# ---------------------------------------------------------------------------

_session_agent: dict[str, str] = {}   # session_key → agent_name
_agent_last_check: dict[str, int] = {}  # agent_name → last seen message id
_url_agent = __import__('contextvars').ContextVar('_url_agent', default=None)

from mcp.server.lowlevel.server import request_ctx as _mcp_request_ctx
from mcp.server.fastmcp import Context


def _get_session_key(ctx=None) -> str | None:
    """Get stable session key from MCP context."""
    if ctx:
        client_id = getattr(ctx, 'client_id', None)
        if client_id:
            return client_id
        session = getattr(ctx, 'session', None)
        if session:
            return str(id(session))
    # Fallback: low-level request context
    try:
        rctx = _mcp_request_ctx.get()
        meta = getattr(rctx, 'meta', None)
        if meta:
            cid = getattr(meta, 'client_id', None) or getattr(meta, 'clientId', None)
            if cid:
                return cid
        session = getattr(rctx, 'session', None)
        return str(id(session)) if session else None
    except LookupError:
        return None


def _get_caller(ctx=None) -> str | None:
    """Resolve agent name from MCP session. Returns None if not registered."""
    sk = _get_session_key(ctx)
    if sk and sk in _session_agent:
        return _session_agent[sk]
    # Try auto-bind from URL ?agent= parameter
    url_name = _url_agent.get()
    if url_name and sk:
        _session_agent[sk] = url_name
        log.info(f"Auto-bound agent '{url_name}' to session {sk[:8]}...")
        # Mark alive in DB
        with db() as conn:
            conn.execute("UPDATE agents SET status = 'alive', updated_at = ? WHERE name = ?",
                        (now_iso(), url_name))
        return url_name
    return None


# ---------------------------------------------------------------------------
# MCP Server with identity middleware
# ---------------------------------------------------------------------------

mcp = FastMCP("basic-workbench")


def _register_tools():
    """Register MCP tools. Identity auto-resolved from session — no name param needed."""
    import messaging
    import memory as mem

    @mcp.tool()
    async def register(name: str, ctx: Context = None) -> str:
        """Register your identity. Call this first with your agent name."""
        sk = _get_session_key(ctx)
        if sk:
            _session_agent[sk] = name
        with db() as conn:
            conn.execute("UPDATE agents SET status = 'alive', updated_at = ? WHERE name = ?",
                        (now_iso(), name))
            # Initialize check cursor to latest message so check() doesn't return history
            if name not in _agent_last_check:
                row = conn.execute("SELECT MAX(id) as max_id FROM messages").fetchone()
                _agent_last_check[name] = row["max_id"] or 0 if row else 0
        log.info(f"Agent '{name}' registered (session={sk[:8] if sk else '?'}...)")
        return json.dumps({"status": "registered", "name": name})

    @mcp.tool()
    async def check(ctx: Context = None) -> str:
        """Check for new messages — DMs and subscribed channels."""
        name = _get_caller(ctx)
        if not name:
            return json.dumps({"error": "Not registered. Call register(name=...) first."})
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
    async def read_inbox(after: int = 0, ctx: Context = None) -> str:
        """Read direct messages addressed to you."""
        name = _get_caller(ctx)
        if not name:
            return json.dumps({"error": "Not registered."})
        with db() as conn:
            msgs = messaging.read_inbox(conn, name, after)
        return json.dumps(msgs)

    @mcp.tool()
    async def direct_message(to: str, body: str, type: str = "message", ctx: Context = None) -> str:
        """Send a direct message to another agent."""
        name = _get_caller(ctx)
        if not name:
            return json.dumps({"error": "Not registered."})
        with db() as conn:
            result = messaging.send_dm(conn, name, to, body, type)
            # Inject into recipient's tmux so they see it immediately
            recipient = conn.execute("SELECT tmux_session, status FROM agents WHERE name = ?", (to,)).fetchone()
            if recipient and recipient["status"] == "alive" and recipient["tmux_session"]:
                _inject_message_to_tmux(recipient["tmux_session"], f"[DM from {name}] {body}")
        msg_id = result.get("id") if isinstance(result, dict) else result
        from events import event_bus
        event_bus.publish("new_message", {
            "id": msg_id, "from": name, "to": to, "body": body,
            "type": type, "channel": None, "ts": now_iso()
        })
        return json.dumps({"status": "sent", "id": msg_id})

    @mcp.tool()
    async def post(channel: str, body: str, type: str = "message", ctx: Context = None) -> str:
        """Post a message to a channel."""
        name = _get_caller(ctx)
        if not name:
            return json.dumps({"error": "Not registered."})
        with db() as conn:
            result = messaging.post_to_channel(conn, name, channel, body, type)
            # Inject into all alive agents' tmux (except sender)
            agents = conn.execute(
                "SELECT name, tmux_session FROM agents WHERE status = 'alive' AND name != ?",
                (name,)
            ).fetchall()
            for a in agents:
                if a["tmux_session"]:
                    _inject_message_to_tmux(a["tmux_session"], f"[#{channel} from {name}] {body}")
        msg_id = result.get("id") if isinstance(result, dict) else result
        from events import event_bus
        event_bus.publish("new_message", {
            "id": msg_id, "from": name, "to": None, "body": body,
            "type": type, "channel": channel, "ts": now_iso()
        })
        return json.dumps({"status": "posted", "id": msg_id, "channel": channel})

    @mcp.tool()
    async def subscribe(channel: str, ctx: Context = None) -> str:
        """Subscribe to a channel."""
        name = _get_caller(ctx)
        if not name:
            return json.dumps({"error": "Not registered."})
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
    async def memory_save(key: str, value: str, shared: bool = False, ctx: Context = None) -> str:
        """Save a key/value pair to memory."""
        name = _get_caller(ctx) or "anonymous"
        with db() as conn:
            mem.memory_save(conn, name, key, value, shared)
        return json.dumps({"status": "saved", "key": key})

    @mcp.tool()
    async def memory_get(key: str, ctx: Context = None) -> str:
        """Read a memory entry by key."""
        name = _get_caller(ctx) or "anonymous"
        with db() as conn:
            entry = mem.memory_get(conn, name, key)
        if entry is None:
            return json.dumps({"error": "not found", "key": key})
        return json.dumps(entry)

    @mcp.tool()
    async def memory_list(ctx: Context = None) -> str:
        """List all memory keys (own + shared)."""
        name = _get_caller(ctx) or "anonymous"
        with db() as conn:
            entries = mem.memory_list(conn, name)
        return json.dumps(entries)

    @mcp.tool()
    async def memory_delete(key: str, ctx: Context = None) -> str:
        """Delete a memory entry you own."""
        name = _get_caller(ctx) or "anonymous"
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
    async def quit(ctx: Context = None) -> str:
        """Stop yourself."""
        name = _get_caller(ctx)
        if not name:
            return json.dumps({"error": "Not registered."})
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

    # Build agent boot prompt — pass raw user prompt to agent_ops, render at launch time
    boot_prompt = _build_boot_prompt(name, role, prompt)

    def _do_spawn():
        with db() as conn:
            return agent_ops.spawn_agent(name, cwd, boot_prompt, model, conn,
                                        role=role, user_prompt=prompt)
    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _do_spawn)
    except Exception as e:
        log.error(f"Spawn failed for '{name}': {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

    if result.get("error"):
        return JSONResponse(result, status_code=400)

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
    def _do_restart():
        with db() as conn:
            # Rebuild boot prompt with current template
            agent = conn.execute("SELECT role, prompt FROM agents WHERE name = ?", (name,)).fetchone()
            if not agent:
                return {"error": f"Agent '{name}' not found"}
            user_prompt = agent["prompt"] or ""
            role = agent["role"] or "developer"
            boot_prompt = _build_boot_prompt(name, role, user_prompt)
            return agent_ops.restart_agent(name, conn, boot_prompt_override=boot_prompt)
    result = await asyncio.get_event_loop().run_in_executor(None, _do_restart)
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
        msgs = messaging.read_channel(conn, channel, after=None, limit=limit)
    return JSONResponse(msgs)


async def api_agent_messages(request: Request):
    name = request.path_params["name"]
    limit = int(request.query_params.get("limit", "100"))
    with db() as conn:
        msgs = conn.execute(
            "SELECT * FROM messages WHERE from_agent = ? OR to_agent = ? "
            "ORDER BY id DESC LIMIT ?", (name, name, limit)
        ).fetchall()
    # Reverse to chronological order (oldest first, newest at bottom)
    return JSONResponse([dict(m) for m in reversed(msgs)])


async def api_post_to_channel(request: Request):
    """POST /api/channels/:name/messages — operator posts to a channel."""
    import messaging
    channel = request.path_params["name"]
    body = await request.json()
    text = body.get("body", "").strip()
    if not text:
        return JSONResponse({"error": "body required"}, status_code=400)
    with db() as conn:
        result = messaging.post_to_channel(conn, "operator", channel, text)
        # Inject into all alive agents' tmux
        agents = conn.execute(
            "SELECT name, tmux_session FROM agents WHERE status = 'alive'"
        ).fetchall()
        for a in agents:
            if a["tmux_session"]:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda s=a["tmux_session"]: _inject_message_to_tmux(s, f"[#{channel} from operator] {text}")
                )
    from events import event_bus
    msg_id = result.get("id") if isinstance(result, dict) else result
    event_bus.publish("new_message", {
        "id": msg_id, "from": "operator", "to": None, "body": text,
        "channel": channel, "ts": now_iso()
    })
    return JSONResponse({"status": "posted"})


async def api_send_dm(request: Request):
    """POST /api/agents/:name/messages — operator sends DM to an agent.
    Also injects the message into the agent's tmux session so it sees it."""
    import messaging
    import agent_ops
    name = request.path_params["name"]
    body = await request.json()
    text = body.get("body", "").strip()
    if not text:
        return JSONResponse({"error": "body required"}, status_code=400)
    tmux_session = None
    with db() as conn:
        result = messaging.send_dm(conn, "operator", name, text)
        agent = conn.execute("SELECT tmux_session, status FROM agents WHERE name = ?", (name,)).fetchone()
        if agent and agent["status"] == "alive" and agent["tmux_session"]:
            tmux_session = agent["tmux_session"]
    # Inject into tmux outside the db context
    if tmux_session:
        inject_text = f'[DM from operator] {text}'
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: _inject_message_to_tmux(tmux_session, inject_text)
        )
    from events import event_bus
    msg_id = result.get("id") if isinstance(result, dict) else result
    event_bus.publish("new_message", {
        "id": msg_id, "from": "operator", "to": name, "body": text,
        "channel": None, "ts": now_iso()
    })
    return JSONResponse({"status": "sent"})


async def api_agent_activity(request: Request):
    """GET /api/agents/:name/activity — parsed activity from JSONL session."""
    name = request.path_params["name"]
    limit = int(request.query_params.get("limit", "50"))
    sessions_dir = Path(os.environ.get("WB_SESSIONS_DIR", "/tmp/basic-wb-sessions"))
    activity_file = sessions_dir / f"{name}.activity"
    if not activity_file.exists():
        return JSONResponse([])
    events = []
    try:
        with open(activity_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception:
        pass
    return JSONResponse(events[-limit:])


async def api_activity(request: Request):
    """GET /api/activity — unified feed of all messages (DMs + channels), most recent first."""
    limit = int(request.query_params.get("limit", "100"))
    with db() as conn:
        msgs = conn.execute(
            "SELECT id, from_agent, to_agent, channel, body, type, created_at "
            "FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    # Return in chronological order
    return JSONResponse([dict(m) for m in reversed(msgs)])


async def api_feed_stream(request: Request):
    from events import sse_stream_handler
    return await sse_stream_handler(request)


# ---------------------------------------------------------------------------
# Boot prompt builder
# ---------------------------------------------------------------------------

_DEVELOPER_PROMPT = """You are "{name}", an agent on this workbench.

Other agents: {agent_list}

COMMUNICATION (MCP tools — your identity is automatic, no need to pass your name):
- check() — check for new DMs and channel messages. Call this regularly.
- direct_message(to="agent_name", body="text") — DM another agent
- post(channel="channel_name", body="text") — post to a channel
- read_inbox() — read your DMs
- channels() — list available channels
- list_agents() — see who's online
- memory_save(key="k", value="v") — persist data
- memory_get(key="k") — recall data

You see all channels automatically — no need to subscribe.

RULES:
- DM first. Use direct messages for 1:1 communication. Use channels for broadcasts.
- Check for messages regularly with check(). Operator messages appear as DMs.
- When you see [DM from X], reply with direct_message(to="X").
- Post to #review when you want feedback on your work.
- Only message when you have something substantive to add, or when directly addressed. Don't ack for the sake of acking. Don't narrate what you're about to do — just do it.
- On startup, check memory_list() for saved state from previous sessions. Load relevant memories before scanning the repo.
- Save important findings with memory_save(key="...", value="...") so you don't lose them on restart.

{user_prompt}"""

_REVIEWER_PROMPT = """You are "{name}", a contrarian reviewer on this workbench.

Other agents: {agent_list}

COMMUNICATION (MCP tools — your identity is automatic, no need to pass your name):
- check() — check for new DMs and channel messages. Call this regularly.
- direct_message(to="agent_name", body="text") — DM another agent
- post(channel="channel_name", body="text") — post to a channel
- read_inbox() — read your DMs
- channels() — list available channels
- list_agents() — see who's online

You see all channels automatically — no need to subscribe.

RULES:
- DM first. Use direct messages for 1:1 communication.
- Check for messages regularly with check(). Operator messages appear as DMs.
- When you see [DM from X], reply with direct_message(to="X").
- Watch #review. When agents post there, critically review their work.
- Only message when you have something substantive to add, or when directly addressed. Don't ack for the sake of acking.

REVIEW FORMAT:
For each review, respond with severity-tagged findings:
- BLOCKING: Must fix before proceeding
- HIGH: Significant issue, needs attention
- MEDIUM: Worth addressing
- LOW: Minor improvement

Be thorough. Challenge assumptions. Find what others miss.

On startup:
1. Call memory_list() to check for saved state from previous sessions.
2. Call check() to see if there are messages waiting.

{user_prompt}"""


def _wrap_mcp_with_identity(app):
    """Middleware that extracts ?agent=NAME from URL and sets _url_agent contextvar."""
    from urllib.parse import parse_qs

    async def middleware(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            qs = parse_qs(scope.get("query_string", b"").decode())
            agent_name = qs.get("agent", [None])[0]
            token = _url_agent.set(agent_name)
            try:
                await app(scope, receive, send)
            finally:
                _url_agent.reset(token)
        else:
            await app(scope, receive, send)

    return middleware


def _inject_message_to_tmux(session: str, text: str):
    """Inject a message into an agent's tmux session as typed input."""
    import subprocess
    escaped = text.replace("'", "'\\''")
    subprocess.run(f"tmux send-keys -t {session} C-u", shell=True, capture_output=True)
    subprocess.run(f"tmux send-keys -t {session} -l '{escaped}'", shell=True, capture_output=True)
    time.sleep(0.3)
    subprocess.run(f"tmux send-keys -t {session} Enter", shell=True, capture_output=True)


def _build_boot_prompt(name: str, role: str, user_prompt: str) -> str:
    with db() as conn:
        agents = conn.execute("SELECT name FROM agents WHERE status = 'alive'").fetchall()
        # Get agent's saved memories to include in orient
        memories = conn.execute(
            "SELECT key, updated_at FROM memories WHERE owner = ? OR shared = 1 "
            "ORDER BY updated_at DESC LIMIT 20", (name,)
        ).fetchall()
    agent_list = ", ".join(a["name"] for a in agents) or "(none yet)"

    template = _REVIEWER_PROMPT if role == "reviewer" else _DEVELOPER_PROMPT
    prompt = template.format(name=name, agent_list=agent_list, user_prompt=user_prompt)

    # Append memory index if agent has saved memories
    if memories:
        prompt += "\n\nSAVED MEMORIES (load with memory_get before re-scanning):\n"
        for m in memories:
            prompt += f"  - {m['key']} (updated: {m['updated_at'][:10]})\n"
        prompt += "Load these with memory_get(key=...) BEFORE scanning the repo. Only re-scan if memories don't have what you need.\n"

    return prompt


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
    Route("/api/channels/{name}/messages", api_post_to_channel, methods=["POST"]),
    Route("/api/agents/{name}/messages", api_agent_messages, methods=["GET"]),
    Route("/api/agents/{name}/messages", api_send_dm, methods=["POST"]),
    Route("/api/agents/{name}/activity", api_agent_activity, methods=["GET"]),
    Route("/api/activity", api_activity, methods=["GET"]),
    Route("/api/feed/stream", api_feed_stream, methods=["GET"]),
    Mount("/static", StaticFiles(directory=str(SCRIPT_DIR / "static")), name="static"),
    # MCP server endpoint for Claude Code agents
    # Middleware extracts ?agent=NAME and sets _url_agent contextvar
    Mount("/", app=_wrap_mcp_with_identity(mcp.streamable_http_app())),
]

async def _lifespan(app):
    """Start MCP session manager + SSE heartbeat — Starlette Mount doesn't propagate lifespan."""
    import contextlib
    from events import event_bus
    async with contextlib.AsyncExitStack() as stack:
        await stack.enter_async_context(mcp.session_manager.run())
        event_bus.start()
        yield
        event_bus.stop()

app = Starlette(routes=routes, lifespan=_lifespan)


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

    # Auto-start activity parsers for any alive agents (survives server restart)
    import agent_ops
    with db() as conn:
        agent_ops.restart_activity_parsers(conn)

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
