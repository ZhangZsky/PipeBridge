"""SSE 事件总线 — 后端变化检测 → SSE 推送的桥梁"""

import asyncio
import logging
import time
from threading import Lock

logger = logging.getLogger('MediaHub')

# 每个 SSE 订阅者队列的最大容量，防止慢消费者导致内存无限增长
_MAX_QUEUE_SIZE = 100
# 最大 SSE 订阅者数量，防止恶意/异常客户端耗尽内存
_MAX_SUBSCRIBERS = 20
# 订阅者最大闲置时间（秒），超过后自动清理僵尸队列
_SUBSCRIBER_IDLE_TIMEOUT = 120


class _TrackedQueue:
    """带最后活跃时间追踪的 asyncio.Queue 包装"""

    def __init__(self, maxsize=0):
        self.queue = asyncio.Queue(maxsize=maxsize)
        self.last_active = time.time()

    def mark_active(self):
        self.last_active = time.time()


class EventBus:
    """简单的发布/订阅事件总线，支持从后台线程安全地向 asyncio 消费者推送事件"""

    def __init__(self):
        self._subscribers = []
        self._lock = Lock()
        self._loop = None

    def set_loop(self, loop):
        self._loop = loop

    def subscribe(self):
        """创建并注册一个带容量限制的 asyncio.Queue，返回队列供 SSE 端点消费"""
        with self._lock:
            if len(self._subscribers) >= _MAX_SUBSCRIBERS:
                logger.warning(f"SSE 订阅者已达上限 {_MAX_SUBSCRIBERS}，拒绝新连接")
                return None
            tracked = _TrackedQueue(maxsize=_MAX_QUEUE_SIZE)
            self._subscribers.append(tracked)
            logger.debug(f"SSE 订阅者已注册，当前订阅者数: {len(self._subscribers)}")
            return tracked

    def unsubscribe(self, tracked):
        """移除订阅者队列"""
        if tracked is None:
            return
        with self._lock:
            try:
                self._subscribers.remove(tracked)
            except ValueError:
                pass
        logger.debug(f"SSE 订阅者已移除，当前订阅者数: {len(self._subscribers)}")

    def publish(self, event_type, data=None):
        """从任意线程安全地发布事件到所有订阅者"""
        event = {'type': event_type, 'data': data or {}}
        with self._lock:
            if not self._subscribers:
                return
            # 清理超时的僵尸订阅者
            now = time.time()
            stale = [s for s in self._subscribers
                     if now - s.last_active > _SUBSCRIBER_IDLE_TIMEOUT]
            for s in stale:
                self._subscribers.remove(s)
                logger.warning(f"清理超时 SSE 订阅者（闲置 {now - s.last_active:.0f}s）")
            if stale:
                logger.debug(f"清理 {len(stale)} 个僵尸订阅者，剩余: {len(self._subscribers)}")

            subscribers = list(self._subscribers)
        if self._loop and self._loop.is_running():
            for tracked in subscribers:
                # 包装 put_nowait 以捕获 QueueFull（call_soon_threadsafe 调度的异常无法在外层捕获）
                def _safe_put(q=tracked.queue, t=tracked):
                    try:
                        q.put_nowait(event)
                        t.mark_active()
                    except asyncio.QueueFull:
                        logger.warning(f"SSE 队列已满，丢弃事件: {event.get('type', 'unknown')}")
                    except Exception:
                        pass
                self._loop.call_soon_threadsafe(_safe_put)
        else:
            logger.debug(f"事件循环未运行，丢弃事件: {event_type}")

    @property
    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)


# 全局单例
event_bus = EventBus()
