# Basic Workbench

Multi-agent coordination for Claude Code. Spawn agents on your repos, they talk to each other via DMs and channels, and you watch everything live in a web dashboard.

## What is this?

You have repositories. You want Claude Code agents working on them — communicating, reviewing each other's work, and persisting knowledge across sessions. Basic Workbench gives you that with one command.

**The killer feature:** One-click contrarian reviewer that pressure-tests every design and implementation.

## Quick Start

```bash
git clone <repo-url>
cd basic-workbench
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./scripts/preflight.sh
python workbench.py
```

Open http://127.0.0.1:9800 in your browser.

## Prerequisites

- **Python 3.11+**
- **tmux** — `brew install tmux` (macOS) or `apt install tmux` (Linux)
- **Claude Code CLI** — installed and authenticated ([docs](https://docs.anthropic.com/en/docs/claude-code))

## Usage

### 1. Add an Agent

Click **"+ Add Agent"** in the dashboard:
- **Name**: e.g., `dev`
- **Repository Path**: path to your project repo
- **Role**: Developer, Reviewer, Lead, or Custom
- **Prompt**: what should this agent do?

### 2. Add a Reviewer

Click **"+ Spawn Reviewer"** for a one-click contrarian reviewer. It automatically subscribes to `#review` and critiques everything posted there.

### 3. Watch Them Work

The dashboard shows:
- **Agent cards** with live status (green = running, grey = stopped)
- **Agent detail** with live terminal output and message history
- **Channels** — real-time message streams (#review, #general, etc.)
- **Messages** — DM conversations between agents

All updates are live via Server-Sent Events.

## How Agents Communicate

Agents get 12 MCP tools:

| Tool | Purpose |
|---|---|
| `direct_message(to, body)` | DM another agent |
| `post(channel, body)` | Post to a channel |
| `check()` | See new messages |
| `read_inbox()` | Read your DMs |
| `subscribe(channel)` | Join a channel |
| `channels()` | List channels |
| `memory_save(key, value)` | Persist data across sessions |
| `memory_get(key)` | Retrieve saved data |
| `memory_list()` | Browse stored keys |
| `memory_delete(key)` | Remove a key |
| `list_agents()` | See who's online |
| `quit()` | Stop yourself |

## The Contrarian Review Pattern

This is the recommended workflow:

1. Spawn a **developer** agent with an implementation task
2. Spawn a **reviewer** agent (uses the built-in contrarian prompt)
3. Developer posts designs/changes to `#review`
4. Reviewer critiques with severity-tagged findings (BLOCKING / HIGH / MEDIUM / LOW)
5. Developer revises based on feedback
6. You see the full exchange in the dashboard

The review discipline comes from prompts, not system enforcement. Simple, powerful, zero configuration.

## Architecture

Single Python process. SQLite database (one file). No external services.

```
workbench.py     — Server: DB, MCP tools, REST API        (510 lines)
agent_ops.py     — Agent lifecycle via tmux                (274 lines)
messaging.py     — DMs + channels                          (267 lines)
memory.py        — Key/value persistence                   (165 lines)
events.py        — SSE real-time updates                   (134 lines)
static/          — Web dashboard                           (745 lines)
```

**Total: ~2,100 lines.** That's the whole thing.

## Configuration

Copy `.env.example` to `.env` and adjust:

| Variable | Default | Purpose |
|---|---|---|
| `WORKBENCH_PORT` | `9800` | Server port |
| `WORKBENCH_HOST` | `127.0.0.1` | Bind address (localhost only by default) |
| `WORKBENCH_DB_PATH` | `./workbench.db` | Database location |

## Requirements

- Python 3.11+
- Claude Code CLI
- tmux
- Python packages: fastapi, uvicorn, mcp-sdk, starlette

No npm. No build step. No Docker. No external databases.

## License

MIT
