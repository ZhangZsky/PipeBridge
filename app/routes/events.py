"""SSE 事件流端点 — GET /api/events"""

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from event_bus import event_bus

logger = logging.getLogger('MediaHub')
router = APIRouter(prefix='/api', tags=['events'])


@router.get('/events')
async def event_stream(request: Request):
    """SSE 事件流：后端检测到设备变化时主动推送，前端监听后按需刷新数据"""

    async def generate():
        queue = event_bus.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    data = json.dumps(event, ensure_ascii=False)
                    yield f"event: {event['type']}\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    # 心跳保活，防止代理/负载均衡器关闭空闲连接
                    yield ": heartbeat\n\n"
        finally:
            event_bus.unsubscribe(queue)

    return StreamingResponse(
        generate(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        }
    )
