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
#   这里 1s 兜底检测设备列表/服务状态/音频端点变化（处理信号漏报或 BlueZ 重启）
# - video 暂无等价实时事件流，保持较短轮询
_CHECK_INTERVALS = {
    'audio': 2,
    'bluetooth': 1,
    'video': 3,
    'system': 3,
}

class EventDetector:
    def __init__(self):
        self._thread = None
        self._udev_thread = None
        self._udev_proc = None
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
        # 启动 udev 实时监听：视频设备热插拔时立即推送事件（类似音频的 pw-mon）
        self._start_udev_monitor()
        logger.info("事件检测器已启动")

    def stop(self):
        self._running = False
        if self._udev_proc:
            try:
                self._udev_proc.terminate()
            except Exception:
                pass
            self._udev_proc = None

    def _start_udev_monitor(self):
        """启动 udev 监听线程，视频设备插拔时立即发布 video.changed 事件"""
        try:
            import subprocess
            # 监听 video4linux（摄像头/采集卡）和 drm（显示器/GPU 输出）子系统
            self._udev_proc = subprocess.Popen(
                ['udevadm', 'monitor', '--kernel', '--subsystem-match=video4linux',
                 '--subsystem-match=drm', '--subsystem-match=usb'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1
            )
            self._udev_thread = threading.Thread(target=self._udev_monitor_loop, daemon=True, name='udev-monitor')
            self._udev_thread.start()
            logger.info("udev 视频设备实时监听已启动")
        except Exception as e:
            logger.warning(f"udev 监听启动失败，视频设备将依赖 {max(_CHECK_INTERVALS.values())}s 轮询兜底: {e}")

    def _udev_monitor_loop(self):
        """读取 udevadm monitor 输出，检测到视频相关设备变化时立即推送事件"""
        if not self._udev_proc or not self._udev_proc.stdout:
            return
        import subprocess
        try:
            for line in self._udev_proc.stdout:
                if not self._running:
                    break
                line = line.strip()
                if not line:
                    continue
                # udevadm monitor 输出格式：KERNEL[时间] 子系统/动作
                # 只关心 add/remove/change 事件（排除 bind/unbind 等噪声）
                if any(k in line for k in ('add', 'remove', 'change')):
                    # 延迟 500ms 再发布，等待设备节点稳定
                    time.sleep(0.5)
                    event_bus.publish('video.changed')
                    # 同时触发音频刷新（USB 声卡可能也变了）
                    if 'usb' in line:
                        event_bus.publish('audio.changed')
        except Exception as e:
            logger.debug(f"udev 监听循环结束: {e}")

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
            # 曾判定无硬件，但 USB 适配器可能已插入，重新检测一次
            try:
                from bluetooth_manager import get_all_controllers, check_bluetooth_hardware
                controllers = get_all_controllers()
                usb_devices = check_bluetooth_hardware()
                if controllers or usb_devices:
                    self._no_bt_hardware = False
                    self._bt_hw_check_done = True
                    logger.info("检测到蓝牙硬件已插入，恢复蓝牙事件检测")
                    event_bus.publish('bluetooth.changed')
            except Exception:
                pass
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

        # 检测 USB 蓝牙适配器热插拔：控制器数量或 USB 设备变化时触发事件
        try:
            from bluetooth_manager import get_all_controllers, check_bluetooth_hardware
            controllers = get_all_controllers()
            usb_devices = check_bluetooth_hardware()
            adapter_snapshot = f"{len(controllers)}|{len(usb_devices)}|{'|'.join(sorted(d.get('id', '') for d in usb_devices))}|{'|'.join(sorted(c.get('mac', '') for c in controllers))}"
            if adapter_snapshot != self._snapshots.get('bluetooth_adapter'):
                self._snapshots['bluetooth_adapter'] = adapter_snapshot
                logger.info(f"蓝牙适配器状态变化: controllers={len(controllers)} usb={len(usb_devices)}")
                event_bus.publish('bluetooth.changed')
                # 适配器变化后重置设备快照，强制下次刷新
                self._snapshots.pop('bluetooth', None)
        except Exception:
            pass

        devices = get_paired_devices()
        snapshot = ';'.join(sorted(
            f"{d.get('mac', '')}|{d.get('connected', '')}|{d.get('rssi', '')}"
            for d in devices
        ))
        if snapshot != self._snapshots.get('bluetooth'):
            self._snapshots['bluetooth'] = snapshot
            event_bus.publish('bluetooth.changed')

        # 检测蓝牙整体状态（service_active + audio_ready）变化，
        # 让蓝牙服务启动/音频端点就绪的变化由 1s 检测（而非 _check_system 的 3s），
        # 避免蓝牙页面"启动中→就绪"状态数秒不更新
        try:
            # 直接复用 get_bluetooth_status 的最终 status 字段作为快照，
            # 与前端 status 判定（service_active + any_powered + audio_ready）完全一致，
            # 避免仅用 svc_active|audio_ready 导致的维度不匹配漏发，
            # 确保"启动中→就绪"变化能可靠触发 bluetooth.changed。
            from bluetooth_manager import get_bluetooth_status
            bt = get_bluetooth_status()
            status_snapshot = str(bt.get('status'))
            if status_snapshot != self._snapshots.get('bt_status'):
                self._snapshots['bt_status'] = status_snapshot
                event_bus.publish('bluetooth.changed')
        except Exception:
            pass

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

    def _check_system(self):
        """检测系统关键服务状态变化，变化时发布 system.changed 事件"""
        from utils import run_command
        import platform_paths
        # 检测关键服务运行状态：PipeWire / WirePlumber / 蓝牙 / D-Bus
        services = ['pipewire', 'wireplumber', 'bluetooth', 'dbus']
        parts = []
        for svc in services:
            r = run_command(f"{platform_paths.CMD_SYSTEMCTL} is-active {svc}", timeout=3)
            parts.append(f"{svc}:{r['stdout'].strip()}")
        # 检测蓝牙音频端点就绪状态
        try:
            from bluetooth_manager import check_bluetooth_audio_ready
            parts.append(f"bt_audio:{'yes' if check_bluetooth_audio_ready() else 'no'}")
        except Exception:
            pass
        snapshot = '|'.join(parts)
        if snapshot != self._snapshots.get('system'):
            self._snapshots['system'] = snapshot
            event_bus.publish('system.changed')

event_detector = EventDetector()
