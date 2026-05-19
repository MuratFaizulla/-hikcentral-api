from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

import state

router = APIRouter()


@router.get("/api/events/stream", tags=["Records"])
async def events_stream():
    """
    SSE-поток новых проходов. Бэкенд опрашивает HikCentral каждые 10 сек
    и пушит новые записи всем подключённым клиентам.
    """
    state._sse_loop = asyncio.get_event_loop()

    queue: asyncio.Queue = asyncio.Queue()
    with state._sse_clients_lock:
        state._sse_clients.append(queue)

    async def generate():
        try:
            yield "event: connected\ndata: {}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            with state._sse_clients_lock:
                try:
                    state._sse_clients.remove(queue)
                except ValueError:
                    pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
