import asyncio
import time
import logging
import threading
from threading import Lock

logger = logging.getLogger('PipeBridge')

_MAX_QUEUE_SIZE = 100
_MAX_SUBSCRIBERS = 20
_SUBSCRIBER_IDLE_TIMEOUT = 120
_MAX_EARLY_BUFFER = 50

class _TrackedQueue:
    def __init__(self, maxsize=0):
        self.queue = asyncio.Queue(maxsize=maxsize)
        self.last_active = time.time()

    def mark_active(self):
        self.last_active = time.time()

class EventBus:
    def __init__(self):
        self._subscribers = []
        self._lock = Lock()
        self._loop = None
        self._early_buffer = []

    def set_loop(self, loop):
        with self._lock:
            self._loop = loop
            buffered = self._early_buffer
            self._early_buffer = []
            subscribers = list(self._subscribers)
        # 事件循环就绪后，冲刷启动早期缓冲的事件（去重保留最后一次同类型事件语义由前端全量刷新兜底）
        if buffered and loop and loop.is_running():
            logger.info(f"事件循环就绪，冲刷 {len(buffered)} 条早期缓冲事件")
            for event in buffered:
                for tracked in subscribers:
                    def _flush_put(q=tracked.queue, t=tracked, e=event):
                        try:
                            q.put_nowait(e)
                            t.mark_active()
                        except asyncio.QueueFull:
                            pass
                        except Exception:
                            pass
                    loop.call_soon_threadsafe(_flush_put)

    def subscribe(self):
        with self._lock:
            if len(self._subscribers) >= _MAX_SUBSCRIBERS:
                logger.warning(f"SSE 订阅者已达上限 {_MAX_SUBSCRIBERS}，拒绝新连接")
                return None
            tracked = _TrackedQueue(maxsize=_MAX_QUEUE_SIZE)
            self._subscribers.append(tracked)
            logger.debug(f"SSE 订阅者已注册，当前订阅者数: {len(self._subscribers)}")
            return tracked

    def unsubscribe(self, tracked):
        if tracked is None:
            return
        with self._lock:
            try:
                self._subscribers.remove(tracked)
            except ValueError:
                pass
        logger.debug(f"SSE 订阅者已移除，当前订阅者数: {len(self._subscribers)}")

    def publish(self, event_type, data=None):
        event = {'type': event_type, 'data': data or {}}
        with self._lock:
            loop_ready = bool(self._loop and self._loop.is_running())
            # 事件循环尚未就绪：无论是否有订阅者，都缓冲到有界队列，待 set_loop 后冲刷
            if not loop_ready:
                self._early_buffer.append(event)
                if len(self._early_buffer) > _MAX_EARLY_BUFFER:
                    dropped = self._early_buffer.pop(0)
                    logger.debug(f"早期缓冲已满，丢弃最旧事件: {dropped.get('type', 'unknown')}")
                logger.debug(f"事件循环未就绪，事件已缓冲待冲刷: {event_type}")
                return
            if not self._subscribers:
                return
            now = time.time()
            stale = [s for s in self._subscribers
                     if now - s.last_active > _SUBSCRIBER_IDLE_TIMEOUT]
            for s in stale:
                self._subscribers.remove(s)
                logger.warning(f"清理超时 SSE 订阅者（闲置 {now - s.last_active:.0f}s）")
            if stale:
                logger.debug(f"清理 {len(stale)} 个僵尸订阅者，剩余: {len(self._subscribers)}")

            subscribers = list(self._subscribers)
        for tracked in subscribers:
            def _safe_put(q=tracked.queue, t=tracked):
                try:
                    q.put_nowait(event)
                    t.mark_active()
                except asyncio.QueueFull:
                    logger.warning(f"SSE 队列已满，丢弃事件: {event.get('type', 'unknown')}")
                except Exception:
                    pass
            self._loop.call_soon_threadsafe(_safe_put)

    @property
    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)

event_bus = EventBus()

# 间隔说明：
# - audio 由 pw_mon_listener 实时推送，这里仅作兜底（处理 pw-mon 漏报或异常重启）
# - bluetooth 由 AutoReconnectManager 的 DBus PropertiesChanged 信号实时推送，
#   这里 30s 仅作兜底（处理信号漏报或 BlueZ 重启）
# - video 暂无等价实时事件流，保持较短轮询
_CHECK_INTERVALS = {
    'audio': 15,
    'bluetooth': 30,
    'video': 5,
}

class EventDetector:
    def __init__(self):
        self._thread = None
        self._running = False
        self._snapshots = {}
        self._no_bt_hardware = False
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
        # 兜底检测：pw_mon_listener 已实时推送 audio.changed（带 payload），
        # 此处仅处理 pw-mon 漏报或异常重启的情况，发布无 payload 事件让前端全量刷新
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
            event_bus.publish('audio.changed')

    def _check_bluetooth(self):
        from bluetooth_manager import get_paired_devices
        if self._no_bt_hardware:
            return
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
        from video_manager import scan_video_devices
        result = scan_video_devices(force=True)
        devices = result.get('devices', [])
        snapshot = f"{len(devices)}|"
        for d in devices:
            snapshot += f"{d.get('name', '')}|{d.get('width', 0)}x{d.get('height', 0)}@{d.get('fps', 0)}|{d.get('is_default', '')};"
        if snapshot != self._snapshots.get('video'):
            self._snapshots['video'] = snapshot
            event_bus.publish('video.changed')

event_detector = EventDetector()
