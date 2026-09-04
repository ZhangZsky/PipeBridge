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
        loop_running = bool(loop and loop.is_running())
        logger.debug(
            f"事件循环 set_loop 已调用: running={loop_running}, 待冲刷={len(buffered)} 条")
        # 事件循环就绪后，冲刷启动早期缓冲的事件（去重保留最后一次同类型事件语义由前端全量刷新兜底）
        if buffered and loop_running:
            for event in buffered:
                for tracked in subscribers:
                    def _flush_put(q=tracked.queue, t=tracked, e=event):
                        try:
                            q.put_nowait(e)
                            t.mark_active()
                        except asyncio.QueueFull:
                            # 背压策略：丢弃旧事件腾出空间
                            try:
                                q.get_nowait()
                                q.put_nowait(e)
                                t.mark_active()
                            except (asyncio.QueueEmpty, asyncio.QueueFull):
                                pass
                        except Exception as e:
                            logger.debug(f"SSE事件入队失败: {e}")
                    loop.call_soon_threadsafe(_flush_put)
        elif buffered and not loop_running:
            # loop 未 running 却传入：保留缓冲，避免丢事件，等待下次 set_loop
            with self._lock:
                self._early_buffer = buffered + self._early_buffer
            logger.warning(
                f"set_loop 收到未运行的事件循环，{len(buffered)} 条事件继续缓冲")

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
            def _safe_put(q=tracked.queue, t=tracked, e=event):
                try:
                    q.put_nowait(e)
                    t.mark_active()
                except asyncio.QueueFull:
                    # 背压策略：丢弃最旧事件腾出空间，保留最新状态
                    # 防止僵尸客户端（不消费但连接未断开）导致事件无限堆积
                    try:
                        dropped = q.get_nowait()
                        q.put_nowait(e)
                        t.mark_active()
                        logger.debug(
                            f"SSE 队列已满，丢弃旧事件({dropped.get('type', 'unknown')}) "
                            f"以腾出空间，订阅者将看到最新状态")
                    except asyncio.QueueEmpty:
                        logger.warning(f"SSE 队列异常(满但取不出)，跳过事件: {e.get('type', 'unknown')}")
                    except asyncio.QueueFull:
                        logger.warning(f"SSE 队列仍满，丢弃事件: {e.get('type', 'unknown')}")
                except Exception as e:
                    logger.debug(f"SSE事件投递失败: {e}")
            self._loop.call_soon_threadsafe(_safe_put)

    @property
    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)

event_bus = EventBus()

# 轮询兜底间隔：所有类型统一 1s，保证任意变化前端延迟 < 1s。
# audio/video 已有实时推送（pw-mon / udev），此处仅兜底漏报或服务重启；
# bluetooth/system 无独立实时流，依赖此 1s 轮询。间隔统一后逻辑更简单、时差一致。
_CHECK_INTERVAL = 1
_CHECK_TYPES = ('audio', 'bluetooth', 'video', 'system')

class EventDetector:
    def __init__(self):
        self._thread = None
        self._udev_thread = None
        self._udev_proc = None
        self._running = False
        self._snapshots = {}
        self._no_bt_hardware = False
        self._bt_hw_check_done = False
        # 缓存上次成功的蓝牙音频就绪判定：check_bluetooth_audio_ready 偶发抛异常时用它填充，
        # 避免 system 快照中 bt_audio 项"时有时无"导致每秒抖动、进而饿死前端防抖使系统页停留时不刷新。
        self._last_bt_audio_ready = None

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
            except Exception as e:
                logger.debug(f"终止udev进程失败: {e}")
            self._udev_proc = None

    def _start_udev_monitor(self):
        # 启动 udev 监听线程，视频设备插拔时立即发布 video.changed 事件
        self._udev_thread = threading.Thread(target=self._udev_monitor_loop, daemon=True, name='udev-monitor')
        self._udev_thread.start()

    def _udev_monitor_loop(self):
        # 常驻监听循环：拉起 udevadm monitor 子进程读取设备事件，子进程异常退出后自愈重启，
        # 避免快腿静默死亡导致\"USB 声卡重插自动恢复音量\"等实时功能永久失效（慢腿轮询不覆盖此功能）。
        while self._running:
            try:
                self._consume_udev_stream()
            except Exception as e:
                logger.debug(f"udev 监听本轮异常: {e}")
            finally:
                # 回收本轮子进程，避免重启时累积僵尸
                proc = self._udev_proc
                self._udev_proc = None
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
            if not self._running:
                break
            time.sleep(_CHECK_INTERVAL)

    def _consume_udev_stream(self):
        # 单次拉起 udevadm monitor 并读取其输出；子进程结束或读到 EOF 后返回，由外层循环决定是否重启。
        import subprocess
        try:
            # 仅监听 drm（显示器/GPU 输出）和 usb（声卡）子系统；
            # 不监听 video4linux，避免触发 uvcvideo 探测导致内核日志刷屏
            self._udev_proc = subprocess.Popen(
                ['udevadm', 'monitor', '--udev',
                 '--subsystem-match=drm', '--subsystem-match=usb'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1
            )
        except FileNotFoundError:
            logger.warning(f"未找到 udevadm 命令，视频设备将依赖 {_CHECK_INTERVAL}s 轮询兜底")
            self._udev_proc = None
            self._running = False  # 命令不存在，无重启意义，停掉快腿仅保留慢腿
            return
        except Exception as e:
            logger.warning(f"udev 监听启动失败，本轮将依赖 {_CHECK_INTERVAL}s 轮询兜底: {e}")
            self._udev_proc = None
            return
        logger.info("udev 显示/USB 设备实时监听已启动")
        for line in self._udev_proc.stdout:
            if not self._running:
                break
            line = line.strip()
            if not line:
                continue
            # udevadm monitor 输出格式为 KERNEL[时间] 子系统/动作，只关心 add/remove/change（排除 bind/unbind 噪声）
            if any(k in line for k in ('add', 'remove', 'change')):
                # 延迟 500ms 再发布，等待设备节点稳定
                time.sleep(0.5)
                event_bus.publish('video.changed')
                # 同时触发音频刷新（USB 声卡可能也变了）
                if 'usb' in line:
                    event_bus.publish('audio.changed')
                    # USB 声卡重插(add)时,尝试恢复该设备曾保存的音量(config.device_volumes)。
                    # 已彻底移除"默认设备"概念,不再恢复默认;仅恢复用户设定过的音量记忆。
                    if ' add' in f' {line} ':
                        self._try_restore_volume_on_replug()

    def _try_restore_volume_on_replug(self):
        # USB 声卡重插后尝试恢复其保存的音量。
        # 逐个匹配 config.device_volumes 中记忆的设备,设备已在线则调 restore_device_volume 恢复。
        # 设备尚未就绪则本次跳过,后续 udev/轮询事件会再次触发,天然重试。
        try:
            import config
            volumes = config.get_device_volumes()
            if not volumes:
                return
            # 再等一小段,确保 PipeWire 已为新声卡建好节点
            time.sleep(1.0)
            from audio_manager import restore_device_volume
            restored_any = False
            for dev_name in list(volumes.keys()):
                try:
                    if restore_device_volume(dev_name):
                        restored_any = True
                except Exception:
                    continue
            if restored_any:
                logger.info("USB 声卡重插:已恢复保存的设备音量")
                event_bus.publish('audio.changed')
        except Exception as e:
            logger.debug(f"USB 重插恢复设备音量失败: {e}")

    def _run(self):
        # 所有类型统一 1s 检测，直接每轮全检，无需 per-type 计时
        while self._running:
            # 无 SSE 订阅者（web 全部关闭）时暂停全检，消除空转；
            # 清空快照，使下次客户端连接时因快照失配自动全量刷新
            if event_bus.subscriber_count == 0:
                if self._snapshots:
                    self._snapshots.clear()
                time.sleep(_CHECK_INTERVAL)
                continue
            for check_type in _CHECK_TYPES:
                try:
                    getattr(self, f'_check_{check_type}')()
                except Exception as e:
                    logger.debug(f"事件检测 {check_type} 异常: {e}")
            time.sleep(_CHECK_INTERVAL)

    def _check_audio(self):
        # 兜底检测：pw_mon_listener 已实时推送 audio.changed，此处仅处理漏报或异常重启并发布无 payload 事件促前端全量刷新
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
        from bluetooth_manager import (get_paired_devices, get_all_controllers,
                                        check_bluetooth_hardware)
        # 单次取值复用控制器与 USB 适配器信息，避免本轮检测内对 BlueZ D-Bus / lsusb 的重复高频调用
        try:
            controllers = get_all_controllers()
            usb_devices = check_bluetooth_hardware()
        except Exception:
            controllers, usb_devices = [], []

        if self._no_bt_hardware:
            # 曾判定无硬件，但 USB 适配器可能已插入，重新检测一次
            if controllers or usb_devices:
                self._no_bt_hardware = False
                self._bt_hw_check_done = True
                logger.info("检测到蓝牙硬件已插入，恢复蓝牙事件检测")
                event_bus.publish('bluetooth.changed')
            else:
                return
        if not self._bt_hw_check_done:
            if not controllers and not usb_devices:
                self._no_bt_hardware = True
                self._bt_hw_check_done = True
                logger.info("未检测到蓝牙硬件，跳过蓝牙事件检测")
                return
            self._bt_hw_check_done = True

        # 检测 USB 蓝牙适配器热插拔：控制器数量或 USB 设备变化时触发事件
        adapter_snapshot = f"{len(controllers)}|{len(usb_devices)}|{'|'.join(sorted(d.get('id', '') for d in usb_devices))}|{'|'.join(sorted(c.get('mac', '') for c in controllers))}"
        if adapter_snapshot != self._snapshots.get('bluetooth_adapter'):
            self._snapshots['bluetooth_adapter'] = adapter_snapshot
            logger.debug(f"蓝牙适配器状态变化: controllers={len(controllers)} usb={len(usb_devices)}")
            event_bus.publish('bluetooth.changed')
            # 适配器变化后重置设备快照，强制下次刷新
            self._snapshots.pop('bluetooth', None)

        devices = get_paired_devices()
        snapshot = ';'.join(sorted(
            f"{d.get('mac', '')}|{d.get('connected', '')}|{d.get('rssi', '')}|{d.get('battery', '')}"
            for d in devices
        ))
        if snapshot != self._snapshots.get('bluetooth'):
            self._snapshots['bluetooth'] = snapshot
            event_bus.publish('bluetooth.changed')

        # 检测蓝牙整体状态（service_active + audio_ready）变化，用 1s 检测避免蓝牙页面"启动中→就绪"数秒不更新
        try:
            # 复用 get_bluetooth_status 的最终 status 字段作为快照，与前端判定完全一致，确保"启动中→就绪"可靠触发 bluetooth.changed
            from bluetooth_manager import get_bluetooth_status
            bt = get_bluetooth_status()
            status_snapshot = str(bt.get('status'))
            if status_snapshot != self._snapshots.get('bt_status'):
                self._snapshots['bt_status'] = status_snapshot
                event_bus.publish('bluetooth.changed')
        except Exception as e:
            logger.debug(f"检查蓝牙状态失败: {e}")

    def _check_video(self):
        from video_manager import scan_video_devices
        result = scan_video_devices()
        devices = result.get('devices', [])
        snapshot = f"{len(devices)}|"
        for d in devices:
            snapshot += f"{d.get('name', '')}|{d.get('width', 0)}x{d.get('height', 0)}@{d.get('fps', 0)}|{d.get('is_default', '')};"
        if snapshot != self._snapshots.get('video'):
            self._snapshots['video'] = snapshot
            event_bus.publish('video.changed')

    def _check_system(self):
        # 检测系统关键服务状态变化，变化时发布 system.changed 事件
        from utils import run_command
        import platform_paths
        # pipewire/wireplumber 是以 root 身份通过 nohup 启动的用户级进程（非 systemd 服务），
        # systemctl is-active 会恒返回 inactive 造成误报，故用 pgrep -x 检测进程存活。
        parts = []
        for svc in ('pipewire', 'wireplumber'):
            pg = run_command(f"pgrep -x {svc} 2>/dev/null", timeout=3)
            parts.append(f"{svc}:{'active' if pg['stdout'].strip() else 'inactive'}")
        # bluetooth/dbus 是系统级 systemd 服务，一次 systemctl 批量查询避免多次子进程开销。
        sys_services = ['bluetooth', 'dbus']
        r = run_command(f"{platform_paths.CMD_SYSTEMCTL} is-active {' '.join(sys_services)}", timeout=3)
        states = r['stdout'].strip().splitlines()
        parts += [f"{svc}:{states[i].strip() if i < len(states) else 'unknown'}"
                  for i, svc in enumerate(sys_services)]
        # 检测蓝牙音频端点就绪状态
        try:
            from bluetooth_manager import check_bluetooth_audio_ready
            self._last_bt_audio_ready = check_bluetooth_audio_ready()
        except Exception as e:
            logger.debug(f"检查蓝牙音频就绪状态失败: {e}")
        # 始终以缓存值参与快照(异常时沿用上次成功值)，保证 bt_audio 项恒存在，快照不抖动。
        # 首次尚无缓存(None)时统一记为 unknown，与 yes/no 区分且稳定。
        if self._last_bt_audio_ready is None:
            parts.append("bt_audio:unknown")
        else:
            parts.append(f"bt_audio:{'yes' if self._last_bt_audio_ready else 'no'}")
        # 检测蓝牙硬件(USB 适配器 + BlueZ 控制器)变化：系统页概览展示"蓝牙硬件/蓝牙音频"，
        # 而蓝牙服务状态在热插拔时可能不变，若快照不含硬件项则插上适配器后系统页永不刷新。
        try:
            from bluetooth_manager import check_bluetooth_hardware, get_all_controllers
            usb = check_bluetooth_hardware()
            ctrls = get_all_controllers()
            parts.append(f"bt_hw:{len(usb)}|{len(ctrls)}")
        except Exception as e:
            logger.debug(f"检查蓝牙硬件状态失败: {e}")
            parts.append("bt_hw:err")
        snapshot = '|'.join(parts)
        if snapshot != self._snapshots.get('system'):
            self._snapshots['system'] = snapshot
            event_bus.publish('system.changed')

event_detector = EventDetector()
