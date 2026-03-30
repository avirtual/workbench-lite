#!/usr/bin/env python3
"""activity_parser.py — JSONL-to-activity parser for basic-workbench.

Tails a JSONL symlink and writes curated activity events to .activity file.
Usage: python activity_parser.py <agent> [--sessions-dir DIR]
"""
import json, sys, signal, hashlib, re, os, time
from pathlib import Path

_PREVIEW_MAX = 10000

EVENT_TOOL_CALL = "agent.tool_call"
EVENT_TOOL_RESULT = "agent.tool_result"
EVENT_THINKING = "agent.thinking.jsonl"
EVENT_MESSAGE = "agent.message"
EVENT_SUBAGENT_PROGRESS = "agent.subagent_progress"
EVENT_TURN_COMPLETE = "agent.turn_complete"
EVENT_USER_MESSAGE = "agent.user_message"
EVENT_USAGE = "agent.usage"
EVENT_ACTION = "agent.action"


def _content_blocks(entry):
    c = entry.get("message", {}).get("content", [])
    return c if isinstance(c, list) else []

def _find_block(blocks, btype):
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == btype:
            return b
    return None


def classify(entry: dict) -> str | None:
    t = entry.get("type")
    if t == "assistant":
        blocks = _content_blocks(entry)
        if _find_block(blocks, "tool_use"):     return EVENT_TOOL_CALL
        if _find_block(blocks, "thinking"):      return EVENT_THINKING
        if _find_block(blocks, "text"):           return EVENT_MESSAGE
        return None
    if t == "user":
        if entry.get("isMeta"):
            if _find_block(_content_blocks(entry), "tool_result"):
                return EVENT_TOOL_RESULT
            return None
        return EVENT_USER_MESSAGE
    if t == "progress":
        if entry.get("data", {}).get("type") == "agent_progress":
            return EVENT_SUBAGENT_PROGRESS
        return None
    if t == "system" and entry.get("subtype") == "turn_duration":
        return EVENT_TURN_COMPLETE
    return None


def _prettify_tool_name(name: str) -> str:
    for prefix in ("mcp__workbench__", "mcp__basic_workbench__"):
        if name.startswith(prefix):
            return name[len(prefix):]
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return f"{parts[1]}:{parts[2]}"
    return name


def _summarize_tool_input(tool_name: str, tool_input: dict) -> str:
    raw = tool_name.split(":")[-1] if ":" in tool_name else tool_name
    if raw == "direct_message":
        return f"\u2192 {tool_input.get('to','?')}: {tool_input.get('body','')}"
    if raw == "post":
        return f"#{tool_input.get('channel','?')}: {tool_input.get('body','')}"
    if raw in ("memory_save", "memory_get"):
        return tool_input.get('key', '?')
    if raw == "recall":
        return f"recall({tool_input.get('query','?')!r})"
    if raw == "subscribe":
        return f"subscribe(#{tool_input.get('channel','?')})"
    if raw == "register":
        return f"register({tool_input.get('name','?')})"
    parts = []
    for k in ("file_path","path","pattern","command","query","key",
              "to","body","channel","skill","prompt","description"):
        v = tool_input.get(k)
        if v is not None:
            s = str(v)[:500]
            parts.append(f"{k}={s!r}")
    if not parts:
        s = json.dumps(tool_input)
        return s[:_PREVIEW_MAX] + ("..." if len(s) > _PREVIEW_MAX else "")
    return ", ".join(parts[:3])


def extract(event_type: str, entry: dict) -> dict | None:
    blocks = _content_blocks(entry)
    if event_type == EVENT_TOOL_CALL:
        b = _find_block(blocks, "tool_use")
        if not b: return None
        name = _prettify_tool_name(b.get("name", "unknown"))
        return {"tool": name, "input_summary": _summarize_tool_input(name, b.get("input", {}))}
    if event_type == EVENT_TOOL_RESULT:
        b = _find_block(blocks, "tool_result")
        if not b: return None
        rc = b.get("content", "")
        if isinstance(rc, list):
            rc = "\n".join(rb.get("text","") for rb in rc if isinstance(rb, dict) and rb.get("type")=="text")
        return {"is_error": bool(b.get("is_error")), "result_summary": str(rc)[:_PREVIEW_MAX]}
    if event_type == EVENT_THINKING:
        return {"output_tokens": entry.get("message",{}).get("usage",{}).get("output_tokens", 0)}
    if event_type == EVENT_MESSAGE:
        texts = [b.get("text","") for b in blocks if isinstance(b,dict) and b.get("type")=="text"]
        return {"text_preview": "\n".join(texts)[:_PREVIEW_MAX]}
    if event_type == EVENT_USER_MESSAGE:
        content = entry.get("message", {}).get("content", [])
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                (b.get("text","") if isinstance(b,dict) else b)
                for b in content if isinstance(b,(str,dict))
            )
        else:
            return None
        if not text or re.match(r'\s*<(command-name|local-command|command-message)', text):
            return None
        m = re.match(r'\[DM from (\w+) [^\]]*\]:\s*(.*)', text, re.DOTALL)
        if m: return {"text_preview": m.group(2).strip()[:_PREVIEW_MAX], "sender": m.group(1)}
        m = re.match(r'\[system[^\]]*\]\s*(.*)', text, re.DOTALL)
        if m: return {"text_preview": m.group(1).strip()[:_PREVIEW_MAX], "sender": "system"}
        m = re.match(r'\[#(\w+)[^\]]*\]\s*(.*)', text, re.DOTALL)
        if m: return {"text_preview": m.group(2).strip()[:_PREVIEW_MAX], "sender": f"#{m.group(1)}"}
        clean = re.sub(r'<[^>]+>', '', text).strip()
        return {"text_preview": clean[:_PREVIEW_MAX]} if clean else None
    if event_type == EVENT_SUBAGENT_PROGRESS:
        return {"prompt_preview": str(entry.get("data",{}).get("prompt",""))[:_PREVIEW_MAX]}
    if event_type == EVENT_TURN_COMPLETE:
        return {"duration_ms": entry.get("durationMs", 0)}
    return None


def event_id(entry: dict) -> str:
    ts = entry.get("ts") or entry.get("timestamp") or ""
    event = entry.get("event") or ""
    detail = (entry.get("input_summary") or entry.get("text_preview")
              or entry.get("result_summary") or entry.get("prompt_preview")
              or entry.get("tool") or "")[:50]
    return hashlib.md5(f"{ts}:{event}:{detail}".encode()).hexdigest()[:12]


def extract_usage(entry: dict) -> dict | None:
    msg = entry.get("message", {})
    usage = msg.get("usage")
    if not usage: return None
    stop = msg.get("stop_reason")
    if stop is None: return None  # streaming partial
    return {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
        "model": msg.get("model", ""),
        "request_id": entry.get("requestId", ""),
        "stop_reason": stop,
    }


def extract_current_action(entry: dict) -> dict | None:
    msg = entry.get("message", {})
    content = msg.get("content", [])
    if not isinstance(content, list): return None
    b = _find_block(content, "tool_use")
    if b:
        name = _prettify_tool_name(b.get("name", "unknown"))
        return {"action": name, "detail": _summarize_tool_input(name, b.get("input",{})), "state": "tool_use"}
    if _find_block(content, "thinking"):
        return {"action": "thinking", "detail": "", "state": "thinking"}
    b = _find_block(content, "text")
    if b:
        return {"action": "responding", "detail": b.get("text","")[:80], "state": "responding"}
    if msg.get("stop_reason") == "end_turn":
        return {"action": "idle", "detail": "", "state": "idle"}
    return None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(agent: str, sessions_dir: str | None = None):
    if sessions_dir is None:
        sessions_dir = os.environ.get("WB_SESSIONS_DIR", "/tmp/basic-wb-sessions")
    sessions = Path(sessions_dir)
    sessions.mkdir(parents=True, exist_ok=True)
    activity_path = sessions / f"{agent}.activity"
    jsonl_symlink = sessions / f"{agent}.jsonl"

    # Resume idx from existing activity file
    idx = 0
    if activity_path.exists():
        try:
            for line in open(activity_path):
                try: idx = max(idx, json.loads(line.strip()).get("idx", 0))
                except (json.JSONDecodeError, ValueError): pass
        except Exception: pass

    last_key, last_action = None, None
    counted_request_ids: set[str] = set()
    activity_f = open(activity_path, "a")

    def flush_and_exit(*_):
        activity_f.close(); sys.exit(0)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, flush_and_exit)

    def _emit(etype: str, payload: dict, ts: str):
        nonlocal idx; idx += 1
        payload.update(agent=agent, ts=ts, idx=idx, event=etype)
        activity_f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        activity_f.flush()

    def _process(raw: str):
        nonlocal last_key, last_action, counted_request_ids
        raw = raw.strip()
        if not raw: return
        try: entry = json.loads(raw)
        except json.JSONDecodeError: return
        ts = entry.get("timestamp") or entry.get("ts") or ""

        if entry.get("type") == "assistant":
            usage = extract_usage(entry)
            if usage:
                rid = usage.get("request_id", "")
                if not rid or rid not in counted_request_ids:
                    if rid:
                        if len(counted_request_ids) >= 10_000: counted_request_ids.clear()
                        counted_request_ids.add(rid)
                    _emit(EVENT_USAGE, usage, ts)
            action = extract_current_action(entry)
            if action and action != last_action:
                last_action = action
                _emit(EVENT_ACTION, dict(action), ts)

        # Skip streaming partials for classified events — only process the
        # final assistant message (with stop_reason) to avoid 3x duplicates.
        # ACTION events above still stream in real-time for UI responsiveness.
        if entry.get("type") == "assistant":
            stop = entry.get("message", {}).get("stop_reason")
            if stop is None:
                return  # streaming partial — skip

        etype = classify(entry)
        if not etype: return
        payload = extract(etype, entry)
        if not payload: return

        if etype == EVENT_TURN_COMPLETE:
            idle = {"action": "idle", "detail": "", "state": "idle"}
            if idle != last_action:
                last_action = idle
                _emit(EVENT_ACTION, dict(idle), ts)

        key = etype + ":" + (payload.get("input_summary") or payload.get("tool")
                             or payload.get("text_preview","")[:80] or "")
        if key and key == last_key: return
        last_key = key
        _emit(etype, payload, ts)

    # Tail loop — follow symlink, poll for new lines
    try:
        pos, current_target = 0, None
        while True:
            try: target = str(jsonl_symlink.resolve())
            except OSError: time.sleep(1); continue
            if target != current_target:
                current_target = target; pos = 0
            if not Path(current_target).exists():
                time.sleep(1); continue
            try: size = Path(current_target).stat().st_size
            except OSError: time.sleep(0.5); continue
            if size < pos: pos = 0
            if size <= pos: time.sleep(0.5); continue
            with open(current_target, "r") as f:
                f.seek(pos)
                for line in f: _process(line)
                pos = f.tell()
    except (KeyboardInterrupt, SystemExit): pass
    finally: activity_f.close()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="JSONL activity parser")
    p.add_argument("agent", help="Agent name")
    p.add_argument("--sessions-dir", default=None,
                   help="Sessions dir (default: $WB_SESSIONS_DIR or /tmp/basic-wb-sessions)")
    a = p.parse_args()
    run(a.agent, a.sessions_dir)
