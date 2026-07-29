import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from event_system import event_bus

logger = logging.getLogger('PipeBridge')
router = APIRouter(prefix='/api', tags=['events'])

@router.get('/events')
async def event_stream(request: Request):
    tracked = event_bus.subscribe()
    if tracked is None:
        return StreamingResponse(
            iter(["event: error\ndata: {\"message\": \"SSE 连接数已达上限\"}\n\n"]),
            media_type='text/event-stream',
        )

    async def generate():
        queue = tracked.queue
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    tracked.mark_active()
                    data = json.dumps(event, ensure_ascii=False)
                    yield f"event: {event['type']}\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                except ConnectionError:
                    break
        finally:
            event_bus.unsubscribe(tracked)

    return StreamingResponse(
        generate(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        }
    )
