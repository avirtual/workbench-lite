"""Agent lifecycle operations for Basic Workbench.

Spawn, stop, restart Claude Code agents in tmux sessions. Each agent gets
its own tmux session and an MCP config pointing to the workbench server.
"""
from __future__ import annotations
import json, logging, os, re, subprocess, time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("agent_ops")

# Read port from env so MCP config matches the running server
_PORT = int(os.environ.get("WORKBENCH_PORT", "9800"))
_HOST = os.environ.get("WORKBENCH_HOST", "127.0.0.1")
_MCP_URL = f"http://{_HOST}:{_PORT}/mcp"
_SESSIONS_DIR = Path(os.environ.get("WB_SESSIONS_DIR", "/tmp/basic-wb-sessions"))
_SCRIPT_DIR = Path(__file__).parent

import uuid

def _generate_session_id(name: str) -> str:
    """Generate a deterministic session ID for a Claude Code agent."""
    ns = uuid.uuid5(uuid.NAMESPACE_DNS, "workbench-lite.session")
    return str(uuid.uuid5(ns, f"{name}-{time.time()}"))


def _claude_projects_dir(cwd: str) -> Path:
    """Get the Claude Code projects dir for a given working directory."""
    encoded = cwd.replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded


def _start_activity_parser(name: str, session_id: str, cwd: str):
    """Create JSONL symlink and start activity parser for an agent."""
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # Find the JSONL file — Claude Code stores it at ~/.claude/projects/{encoded-cwd}/{session-id}.jsonl
    jsonl_path = _claude_projects_dir(cwd) / f"{session_id}.jsonl"
    symlink_path = _SESSIONS_DIR / f"{name}.jsonl"

    # Create/update symlink
    try:
        if symlink_path.is_symlink() or symlink_path.exists():
            symlink_path.unlink()
        symlink_path.symlink_to(jsonl_path)
    except Exception as e:
        log.warning(f"Failed to create JSONL symlink for {name}: {e}")
        return

    # Start activity parser as background process
    parser_script = _SCRIPT_DIR / "activity_parser.py"
    if parser_script.exists():
        import subprocess as _sp
        _sp.Popen(
            ["python3", str(parser_script), name, "--sessions-dir", str(_SESSIONS_DIR)],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )
        log.info(f"Activity parser started for {name}")

# -- tmux helpers ----------------------------------------------------------

def _run(cmd: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)

def tmux_exists(session: str) -> bool:
    """Check if a tmux session exists."""
    return _run(f"tmux has-session -t {session} 2>/dev/null").returncode == 0

def _tmux_kill(session: str):
    if tmux_exists(session):
        _run(f"tmux detach-client -s {session} 2>/dev/null")
        _run(f"tmux kill-session -t {session}")

def tmux_capture(session: str, lines: int = 50) -> str:
    """Capture the last N lines from a tmux pane."""
    try:
        return _run(f"tmux capture-pane -t {session} -p -S -{lines}", timeout=5).stdout
    except subprocess.TimeoutExpired:
        return "(tmux capture timed out)"

def _tmux_send(session: str, text: str):
    _run(f"tmux send-keys -t {session} C-u")
    escaped = text.replace("'", "'\\''")
    _run(f"tmux send-keys -t {session} -l '{escaped}'")
    if len(text) > 200:
        time.sleep(1.0)

def _tmux_enter(session: str):
    _run(f"tmux send-keys -t {session} Enter")

# -- MCP config ------------------------------------------------------------

def _write_mcp_config(name: str, workbench_url: str) -> Path:
    """Write per-agent MCP config JSON, return the file path."""
    mcp_dir = Path("/tmp/basic-wb-mcp")
    mcp_dir.mkdir(parents=True, exist_ok=True)
    mcp_file = mcp_dir / f"{name}.json"
    config = {"mcpServers": {"workbench": {
        "type": "http", "url": f"{workbench_url}?agent={name}",
    }}}
    desired = json.dumps(config, indent=2) + "\n"
    if not mcp_file.exists() or mcp_file.read_text() != desired:
        mcp_file.write_text(desired)
    return mcp_file

# -- Boot prompt templates -------------------------------------------------

_DEFAULT_PROMPT = '''\
You are "{name}", an agent on this workbench.
Other agents online: {agent_list}

Communication — use your MCP tools:
- direct_message(to, body) to message another agent
- post(channel, body) to broadcast to a channel
- check() to see new messages

{user_prompt}'''

_REVIEWER_PROMPT = '''\
You are "{name}", a contrarian reviewer on this workbench.
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
On startup: subscribe(channel="#review"), then check() to watch for posts.'''

def _build_boot_prompt(name, user_prompt, role, db_conn):
    rows = db_conn.execute(
        "SELECT name, status, role FROM agents WHERE name != ?", (name,)
    ).fetchall()
    agent_list = ", ".join(
        f"{r['name']} ({r['role'] or 'developer'}, {r['status']})" for r in rows
    ) or "(none)"
    tpl = _REVIEWER_PROMPT if role == "reviewer" else _DEFAULT_PROMPT
    return tpl.format(name=name, agent_list=agent_list, user_prompt=user_prompt or "")

# -- Wait for Claude Code prompt -------------------------------------------

def _wait_for_prompt(session: str, timeout: int = 120) -> bool:
    """Poll tmux until Claude Code's input prompt appears and stabilises.
    Handles common startup dialogs (trust, permissions, MCP approval)."""
    start, handled = time.time(), set()
    while time.time() - start < timeout:
        content = tmux_capture(session, lines=30)
        lines = content.strip().split("\n")
        # Trust dialog
        if "trust" not in handled and any("Yes, I trust this folder" in l for l in lines):
            _tmux_enter(session); handled.add("trust"); time.sleep(2); continue
        # Permissions dialog
        if "perms" not in handled and any("Yes, I accept" in l for l in lines):
            _run(f"tmux send-keys -t {session} Down"); time.sleep(0.5)
            _tmux_enter(session); handled.add("perms"); time.sleep(2); continue
        # MCP server approval
        if "mcp" not in handled and any("Enter to confirm" in l for l in lines) \
                and any("MCP server" in l for l in lines):
            _tmux_enter(session); handled.add("mcp"); time.sleep(2); continue
        # Prompt detection
        tail = [l.strip() for l in lines[-5:] if l.strip()]
        if any(l == ">" or l.startswith("> ") or "bypass permissions on" in l
               or l.startswith("\u276f Try") for l in tail):
            time.sleep(2)
            if tmux_capture(session, lines=30) == content:
                return True
        time.sleep(1)
    return False

# -- Helpers ---------------------------------------------------------------

_now = lambda: datetime.now(timezone.utc).isoformat()
_session_name = lambda name: f"wb-{name}"

def _validate_name(name: str) -> str | None:
    if not name or not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$', name):
        return f"Invalid agent name '{name}'."
    return f"Agent name too long (max 64)." if len(name) > 64 else None

# -- Internal: launch + orient ---------------------------------------------

def _launch_and_orient(name, cwd, model, prompt, role, db_conn, workbench_url):
    """Create tmux session, start Claude Code, wait for prompt, inject orient."""
    session = _session_name(name)
    mcp_file = _write_mcp_config(name, workbench_url)
    session_id = _generate_session_id(name)
    model_flag = f" --model {model}" if model and model.lower() != "default" else ""
    cmd = f"claude --dangerously-skip-permissions{model_flag} --session-id {session_id} --mcp-config {mcp_file}"

    _tmux_kill(session)
    _run(f"tmux new-session -d -s {session} -x 200 -y 50 -c '{cwd}' {cmd}")
    _run(f"tmux set-option -t {session} history-limit 10000")
    time.sleep(1)

    if not tmux_exists(session):
        return {"error": "Agent process exited immediately. Is Claude Code CLI installed?"}
    if not _wait_for_prompt(session, timeout=120):
        _tmux_kill(session)
        return {"error": "Timed out waiting for Claude Code prompt."}

    # Start activity parser (tails JSONL → .activity file)
    _start_activity_parser(name, session_id, cwd)

    # Store session_id in DB for later reference
    db_conn.execute("UPDATE agents SET updated_at = ? WHERE name = ?", (json.dumps({"session_id": session_id}), name))

    # Write orient file and send to Claude
    orient_path = f"/tmp/basic-wb-orient-{name}.md"
    Path(orient_path).write_text(prompt)
    _tmux_send(session, f"Read {orient_path} — this is your boot file. Follow all instructions in it.")
    time.sleep(0.5)
    _tmux_enter(session)
    return {"status": "ok", "agent": name, "session": session}

# -- Public API ------------------------------------------------------------

def spawn_agent(name: str, cwd: str, prompt: str, model: str, db_conn, *,
                role: str | None = None,
                workbench_url: str = _MCP_URL) -> dict:
    """Spawn a new Claude Code agent in a tmux session.
    If a stopped agent with the same name exists, revives it via restart."""
    err = _validate_name(name)
    if err:
        return {"error": err}

    existing = db_conn.execute(
        "SELECT name, status, prompt, model, cwd, role FROM agents WHERE name = ?", (name,)
    ).fetchone()
    if existing:
        if existing["status"] == "alive":
            return {"error": f"Agent '{name}' is already running."}
        if existing["status"] == "stopped":
            db_conn.execute(
                "UPDATE agents SET prompt=?, model=?, cwd=?, role=?, updated_at=? WHERE name=?",
                (prompt or existing["prompt"], model or existing["model"],
                 cwd or existing["cwd"], role or existing["role"], _now(), name))
            db_conn.commit()
            return restart_agent(name, db_conn, workbench_url=workbench_url)

    cwd = str(Path(cwd).resolve()) if cwd else os.getcwd()
    result = _launch_and_orient(name, cwd, model, prompt, role, db_conn, workbench_url)
    if "error" in result:
        return result

    now = _now()
    db_conn.execute(
        "INSERT INTO agents (name, status, model, cwd, prompt, role, created_at, updated_at) "
        "VALUES (?, 'alive', ?, ?, ?, ?, ?, ?)",
        (name, model or "sonnet", cwd, prompt, role or "developer", now, now))
    db_conn.commit()
    # Auto-subscribe reviewer agents to #review
    if role == "reviewer":
        db_conn.execute(
            "INSERT OR IGNORE INTO subscriptions (agent, channel) VALUES (?, ?)",
            (name, "review"))
        db_conn.commit()
    log.info(f"Agent '{name}' spawned (model={model}, role={role}, cwd={cwd})")
    return result


def stop_agent(name: str, db_conn) -> dict:
    """Stop an agent: kill tmux session, set DB status to 'stopped'."""
    agent = db_conn.execute(
        "SELECT name, status FROM agents WHERE name = ?", (name,)
    ).fetchone()
    if not agent:
        return {"error": f"Agent '{name}' not found."}
    if agent["status"] == "stopped":
        return {"status": "ok", "note": "already stopped"}
    _tmux_kill(_session_name(name))
    db_conn.execute(
        "UPDATE agents SET status='stopped', updated_at=? WHERE name=?", (_now(), name))
    db_conn.commit()
    log.info(f"Agent '{name}' stopped")
    return {"status": "ok"}


def restart_agent(name: str, db_conn, *,
                  workbench_url: str = _MCP_URL) -> dict:
    """Restart an agent: stop + spawn with the same config from DB."""
    agent = db_conn.execute(
        "SELECT name, status, model, cwd, prompt, role FROM agents WHERE name = ?", (name,)
    ).fetchone()
    if not agent:
        return {"error": f"Agent '{name}' not found."}

    _tmux_kill(_session_name(name))
    time.sleep(0.5)

    cwd = agent["cwd"] or os.getcwd()
    result = _launch_and_orient(
        name, cwd, agent["model"] or "sonnet",
        agent["prompt"] or "", agent["role"], db_conn, workbench_url)
    status = "alive" if "error" not in result else "stopped"
    db_conn.execute(
        "UPDATE agents SET status=?, updated_at=? WHERE name=?", (status, _now(), name))
    db_conn.commit()
    if "error" not in result:
        log.info(f"Agent '{name}' restarted")
    return result


def list_agents(db_conn) -> list[dict]:
    """List all agents, reconciling DB status with live tmux state."""
    rows = db_conn.execute(
        "SELECT name, status, model, cwd, prompt, role, created_at, updated_at FROM agents"
    ).fetchall()
    result = []
    for a in rows:
        name, db_status = a["name"], a["status"]
        alive = tmux_exists(_session_name(name))
        live = "alive" if alive and db_status == "stopped" else \
               "stopped" if not alive and db_status == "alive" else db_status
        if live != db_status:
            db_conn.execute(
                "UPDATE agents SET status=?, updated_at=? WHERE name=?", (live, _now(), name))
            db_conn.commit()
        result.append({
            "name": name, "status": live, "model": a["model"], "cwd": a["cwd"],
            "prompt": a["prompt"] or "", "role": a["role"] or "developer",
            "created_at": a["created_at"], "updated_at": a["updated_at"],
            "tmux_session": _session_name(name),
        })
    return result
