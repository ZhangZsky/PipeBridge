"""SSE 事件总线 — 后端变化检测 → SSE 推送的桥梁"""

import asyncio
import logging
from threading import Lock

logger = logging.getLogger('MediaHub')


class EventBus:
    """简单的发布/订阅事件总线，支持从后台线程安全地向 asyncio 消费者推送事件"""

    def __init__(self):
        self._subscribers = []
        self._lock = Lock()
        self._loop = None

    def set_loop(self, loop):
        self._loop = loop

    def subscribe(self):
        """创建并注册一个 asyncio.Queue，返回队列供 SSE 端点消费"""
        queue = asyncio.Queue()
        with self._lock:
            self._subscribers.append(queue)
        logger.debug(f"SSE 订阅者已注册，当前订阅者数: {len(self._subscribers)}")
        return queue

    def unsubscribe(self, queue):
        """移除订阅者队列"""
        with self._lock:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                pass
        logger.debug(f"SSE 订阅者已移除，当前订阅者数: {len(self._subscribers)}")

    def publish(self, event_type, data=None):
        """从任意线程安全地发布事件到所有订阅者"""
        event = {'type': event_type, 'data': data or {}}
        with self._lock:
            if not self._subscribers:
                return
            subscribers = list(self._subscribers)
        if self._loop and self._loop.is_running():
            for q in subscribers:
                try:
                    self._loop.call_soon_threadsafe(q.put_nowait, event)
                except Exception:
                    pass
        else:
            logger.debug(f"事件循环未运行，丢弃事件: {event_type}")

    @property
    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)


# 全局单例
event_bus = EventBus()
