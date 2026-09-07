"""SSE log streaming — captures Python logging and pushes to web UI in real-time."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Set

from fastapi import Request
from fastapi.responses import StreamingResponse

# Moscow timezone
_MSK = timezone(timedelta(hours=4))

# ── In-memory log buffer ─────────────────────────────────────────────

MAX_BUFFER = 500
_log_buffer: deque[dict] = deque(maxlen=MAX_BUFFER)
_subscribers: Set[asyncio.Queue] = set()


class SSELogHandler(logging.Handler):
    """Custom logging handler that broadcasts log records to SSE subscribers."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.fromtimestamp(record.created, tz=_MSK).strftime("%H:%M:%S")
            entry = {
                "time": ts,
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
            }
            _log_buffer.append(entry)

            # Push to all connected SSE clients
            data = json.dumps(entry, ensure_ascii=False)
            dead = set()
            for q in _subscribers:
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    dead.add(q)
            for q in dead:
                _subscribers.discard(q)
        except Exception:
            pass


def install_log_handler() -> None:
    """Install the SSE log handler on the root logger."""
    root = logging.getLogger()
    # Avoid duplicate handlers
    if any(isinstance(h, SSELogHandler) for h in root.handlers):
        return
    handler = SSELogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    # Only capture INFO and above (skip DEBUG noise)
    handler.setLevel(logging.INFO)
    root.addHandler(handler)


async def log_stream(request: Request) -> StreamingResponse:
    """SSE endpoint that streams log entries to the browser."""

    async def event_generator():
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        _subscribers.add(q)
        try:
            # First, send the last N buffered entries so new clients see history
            for entry in _log_buffer:
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"

            # Then stream live entries
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield ": keepalive\n\n"
        finally:
            _subscribers.discard(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
