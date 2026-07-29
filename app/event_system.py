"""事件系统 —— SSE 事件总线 + 后端变化检测线程

整合 event_bus 和 event_detector：
- EventBus: 发布/订阅事件总线，支持从后台线程安全地向 asyncio 消费者推送事件
- EventDetector: 后台线程，周期性检测音频/蓝牙/视频设备变化，变化时通过 EventBus 推送 SSE 事件
"""

import asyncio
import time
import logging
import threading
from threading import Lock

logger = logging.getLogger('PipeBridge')

# ============================================================================
# SSE 事件总线
# ============================================================================

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


# 全局事件总线单例
event_bus = EventBus()


# ============================================================================
# 后端变化检测线程
# ============================================================================

# 各类检测的间隔（秒）
_CHECK_INTERVALS = {
    'audio': 2,
    'bluetooth': 3,
    'video': 5,
}


class EventDetector:
    """后台线程：周期性检测音频/蓝牙/视频设备变化，变化时发布 SSE 事件"""

    def __init__(self):
        self._thread = None
        self._running = False
        self._snapshots = {}
        self._no_bt_hardware = False  # 无蓝牙硬件标记，避免反复尝试
        self._bt_hw_check_done = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name='event-detector')
        self._thread.start()
        logger.info("事件检测器已启动")

    def stop(self):
        self._running = False

    def _run(self):
        last_check = {}
        while self._running:
            now = time.time()
            for check_type, interval in _CHECK_INTERVALS.items():
                if now - last_check.get(check_type, 0) >= interval:
                    try:
                        getattr(self, f'_check_{check_type}')()
                    except Exception as e:
                        logger.debug(f"事件检测 {check_type} 异常: {e}")
                    last_check[check_type] = now
            time.sleep(1)

    def _check_audio(self):
        from audio_manager import get_audio_devices
        result = get_audio_devices()
        devices = result.get('devices', [])
        snapshot = ';'.join(sorted(
            f"{d.get('name', '')}|{d.get('state', '')}|{d.get('is_default', '')}|"
            f"{d.get('volume', 0)}|{d.get('muted', '')}"
            for d in devices
        ))
        if snapshot != self._snapshots.get('audio'):
            self._snapshots['audio'] = snapshot
            # 音频设备变化（如蓝牙/USB 声卡拔出）后重新静音蜂鸣器，
            # 防止 WirePlumber fallback 把蜂鸣器选为默认输出后 PC Speaker 长响
            try:
                from audio_manager import _mute_pcspkr_sinks
                _mute_pcspkr_sinks()
            except Exception as e:
                logger.debug(f"重新静音蜂鸣器失败: {e}")
            event_bus.publish('audio.changed')

    def _check_bluetooth(self):
        from bluetooth_manager import get_paired_devices
        # 无硬件时跳过轮询，避免反复尝试启动服务
        if self._no_bt_hardware:
            return
        # 首次检测蓝牙硬件状态，无硬件则标记并跳过后续检查
        if not self._bt_hw_check_done:
            try:
                from bluetooth_manager import get_all_controllers, check_bluetooth_hardware
                controllers = get_all_controllers()
                usb_devices = check_bluetooth_hardware()
                if not controllers and not usb_devices:
                    self._no_bt_hardware = True
                    self._bt_hw_check_done = True
                    logger.info("未检测到蓝牙硬件，跳过蓝牙事件检测")
                    return
            except Exception:
                pass
            self._bt_hw_check_done = True
        devices = get_paired_devices()
        snapshot = ';'.join(sorted(
            f"{d.get('mac', '')}|{d.get('connected', '')}|{d.get('rssi', '')}"
            for d in devices
        ))
        if snapshot != self._snapshots.get('bluetooth'):
            self._snapshots['bluetooth'] = snapshot
            event_bus.publish('bluetooth.changed')

    def _check_video(self):
        # 强制扫描以检测热插拔变化
        from video_manager import scan_video_devices
        result = scan_video_devices(force=True)
        devices = result.get('devices', [])
        snapshot = f"{len(devices)}|"
        for d in devices:
            snapshot += f"{d.get('name', '')}|{d.get('width', 0)}x{d.get('height', 0)}@{d.get('fps', 0)}|{d.get('is_default', '')};"
        if snapshot != self._snapshots.get('video'):
            self._snapshots['video'] = snapshot
            event_bus.publish('video.changed')


# 全局事件检测器单例
event_detector = EventDetector()
