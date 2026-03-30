"""
events.py -- SSE event bus for Basic Workbench.

Pub/sub for real-time browser updates. Per-client asyncio.Queue fanout.
Slow clients are dropped (browser EventSource auto-reconnects). Ring buffer
(500 events) supports Last-Event-ID replay. 15s heartbeat detects stale clients.
Event types: new_message, agent_status_change, system.heartbeat
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime, timezone
from typing import AsyncIterator

from starlette.requests import Request
from starlette.responses import StreamingResponse

_seq: int = 0


def _next_seq() -> int:
    global _seq
    _seq += 1
    return _seq


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EventBus:
    """In-process pub/sub with per-client asyncio.Queue fanout."""

    BUFFER_SIZE = 500
    HEARTBEAT_SEC = 15
    CLIENT_QUEUE_MAX = 64

    def __init__(self) -> None:
        self._clients: list[asyncio.Queue] = []
        self._buffer: deque[tuple[int, str, dict]] = deque(maxlen=self.BUFFER_SIZE)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._hb_task: asyncio.Task | None = None

    def start(self) -> None:
        """Call once from app lifespan (inside a running event loop)."""
        self._loop = asyncio.get_running_loop()
        if self._hb_task is None or self._hb_task.done():
            self._hb_task = asyncio.ensure_future(self._heartbeat())

    def stop(self) -> None:
        if self._hb_task and not self._hb_task.done():
            self._hb_task.cancel()

    def publish(self, event_type: str, data: dict) -> None:
        """Broadcast an event. Thread-safe via loop.call_soon_threadsafe."""
        seq = _next_seq()
        data.setdefault("ts", _now_iso())
        record = (seq, event_type, data)
        try:
            asyncio.get_running_loop()
            self._fanout(record)
        except RuntimeError:
            if self._loop and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._fanout, record)

    def _fanout(self, record: tuple[int, str, dict]) -> None:
        self._buffer.append(record)
        dead: list[asyncio.Queue] = []
        for q in self._clients:
            try:
                q.put_nowait(record)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            try:
                self._clients.remove(q)
            except ValueError:
                pass

    async def subscribe(self, last_id: int = 0) -> AsyncIterator[tuple[int, str, dict]]:
        """Yield (seq, event_type, data). Replays missed events when last_id > 0."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self.CLIENT_QUEUE_MAX)
        self._clients.append(q)
        try:
            if last_id:
                for rec in self._buffer:
                    if rec[0] > last_id:
                        yield rec
            while True:
                yield await q.get()
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            try:
                self._clients.remove(q)
            except ValueError:
                pass

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.HEARTBEAT_SEC)
            self.publish("system.heartbeat", {"ts": _now_iso()})

    @property
    def client_count(self) -> int:
        return len(self._clients)


event_bus = EventBus()


def _format_sse(seq: int, event_type: str, data: dict) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    return f"id: {seq}\nevent: {event_type}\ndata: {payload}\n\n".encode()


async def sse_stream_handler(request: Request) -> StreamingResponse:
    """GET /api/feed/stream -- Server-Sent Events endpoint."""
    last_id = int(request.query_params.get("last_id", 0))

    async def generate() -> AsyncIterator[bytes]:
        yield _format_sse(0, "system.heartbeat", {"ts": _now_iso()})
        async for seq, etype, data in event_bus.subscribe(last_id):
            yield _format_sse(seq, etype, data)
            if await request.is_disconnected():
                break

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
