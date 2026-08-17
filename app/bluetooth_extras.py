import os
import re
import time
import shlex
import shutil
import logging
import threading
import subprocess

import dbus

from utils import run_command
from exceptions import CommandError

logger = logging.getLogger('PipeBridge')

BLUEZ_SERVICE = 'org.bluez'
BLUEZ_IFACE_DEVICE = 'org.bluez.Device1'
BLUEZ_IFACE_ADAPTER = 'org.bluez.Adapter1'

class AutoReconnectManager:
    _MANUAL_DISCONNECT_TTL = 1800

    def __init__(self, bus, max_retries=3, base_delay=3, max_delay=15):
        self._bus = bus
        self._disconnected_devices = {}
        self._timers = {}
        self._manual_disconnects = {}
        self._lock = threading.RLock()
        self._running = False
        # 初始关闭，由 _get_reconnect_manager() 从配置同步开关状态
        # （config.py 中 auto_reconnect 默认为 True，即默认开启自动重连）
        self._enabled = False
        self._signal_match = None
        self._adapter_signal_match = None
        self._iface_added_match = None
        self._iface_removed_match = None
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        # 蓝牙状态实时推送节流：DBus 信号(尤其 RSSI)可能高频触发，合并到 200ms 一次全量刷新事件避免刷屏
        self._publish_timer = None
        self._publish_lock = threading.Lock()

    def start(self):
        if self._running:
            return
        self._running = True
        try:
            self._signal_match = self._bus.add_signal_receiver(
                self._on_properties_changed,
                dbus_interface='org.freedesktop.DBus.Properties',
                signal_name='PropertiesChanged',
                arg0=BLUEZ_IFACE_DEVICE,
                path_keyword='path'
            )
        except dbus.exceptions.DBusException as e:
            logger.warning(f"注册蓝牙信号监听失败: {e}")
        try:
            # 同时监听适配器(Adapter1)属性变化 Powered/Discoverable/Pairable/Discovering 外部改动实时驱动前端刷新
            self._adapter_signal_match = self._bus.add_signal_receiver(
                self._on_adapter_properties_changed,
                dbus_interface='org.freedesktop.DBus.Properties',
                signal_name='PropertiesChanged',
                arg0=BLUEZ_IFACE_ADAPTER,
                path_keyword='path'
            )
        except dbus.exceptions.DBusException as e:
            logger.warning(f"注册适配器信号监听失败: {e}")
        try:
            # 监听 ObjectManager InterfacesAdded/InterfacesRemoved 手机首次连接/配对时 BlueZ 新增 Device1 不触发 PropertiesChanged 命中蓝牙设备对象即实时推送
            self._iface_added_match = self._bus.add_signal_receiver(
                self._on_interfaces_added,
                dbus_interface='org.freedesktop.DBus.ObjectManager',
                signal_name='InterfacesAdded'
            )
            self._iface_removed_match = self._bus.add_signal_receiver(
                self._on_interfaces_removed,
                dbus_interface='org.freedesktop.DBus.ObjectManager',
                signal_name='InterfacesRemoved'
            )
        except dbus.exceptions.DBusException as e:
            logger.warning(f"注册设备增删信号监听失败: {e}")
        logger.debug("蓝牙自动重连监控已启动")

    def stop(self):
        with self._lock:
            self._running = False
            for mac, timer in self._timers.items():
                if timer:
                    timer.cancel()
            self._timers.clear()
        with self._publish_lock:
            if self._publish_timer:
                self._publish_timer.cancel()
                self._publish_timer = None
        if self._signal_match:
            try:
                self._signal_match.remove()
            except Exception as e:
                logger.debug(f"移除信号匹配失败: {e}")
        if self._adapter_signal_match:
            try:
                self._adapter_signal_match.remove()
            except Exception as e:
                logger.debug(f"移除适配器信号匹配失败: {e}")
        if self._iface_added_match:
            try:
                self._iface_added_match.remove()
            except Exception as e:
                logger.debug(f"移除接口添加信号匹配失败: {e}")
        if self._iface_removed_match:
            try:
                self._iface_removed_match.remove()
            except Exception as e:
                logger.debug(f"移除接口移除信号匹配失败: {e}")

    def set_enabled(self, enabled):
        self._enabled = enabled
        had_pending = False
        if not enabled:
            with self._lock:
                for mac, timer in self._timers.items():
                    if timer:
                        timer.cancel()
                self._timers.clear()
                had_pending = bool(self._disconnected_devices)
                self._disconnected_devices.clear()
        # 关闭自动重连清空重连队列：主动推送，让"正在重连"指示器立即消失
        if had_pending:
            self._publish_bt_changed()

    def get_status(self):
        with self._lock:
            self._cleanup_expired_manual_disconnects()
            return {
                'monitoring': self._running and self._enabled,
                'reconnecting_devices': list(self._disconnected_devices.keys()),
                'manual_disconnects': list(self._manual_disconnects.keys())
            }

    def _cleanup_expired_manual_disconnects(self):
        import time as _time
        now = _time.time()
        expired = [mac for mac, ts in self._manual_disconnects.items()
                   if now - ts > self._MANUAL_DISCONNECT_TTL]
        for mac in expired:
            del self._manual_disconnects[mac]

    def mark_manual_disconnect(self, mac):
        import time as _time
        mac = mac.upper()
        removed = False
        with self._lock:
            self._manual_disconnects[mac] = _time.time()
            if self._disconnected_devices.pop(mac, None) is not None:
                removed = True
            timer = self._timers.pop(mac, None)
            if timer:
                timer.cancel()
        # 手动断开使设备移出重连队列：主动推送刷新指示器
        if removed:
            self._publish_bt_changed()

    def _publish_bt_changed(self):
        # 节流合并：200ms 内的多次属性变化只推一次 bluetooth.changed
        with self._publish_lock:
            if self._publish_timer:
                return
            def _fire():
                with self._publish_lock:
                    self._publish_timer = None
                try:
                    from event_system import event_bus
                    event_bus.publish('bluetooth.changed')
                except Exception as e:
                    logger.debug(f"发布蓝牙变更事件失败: {e}")
            self._publish_timer = threading.Timer(0.2, _fire)
            self._publish_timer.daemon = True
            self._publish_timer.start()

    def _on_properties_changed(self, interface, changed, invalidated, path):
        if interface != BLUEZ_IFACE_DEVICE:
            return

        # 设备属性变化(Connected/RSSI/Paired/Trusted 等)实时推送前端 DBus 信号驱动取代高频轮询 节流避免刷屏
        self._publish_bt_changed()

        if not self._running or not self._enabled:
            return

        connected = changed.get('Connected')
        if connected is None:
            return

        mac = path.split('/')[-1]
        if mac.startswith('dev_'):
            mac = mac[4:].replace('_', ':').upper()
        else:
            return

        if not connected:
            self._handle_disconnect(mac)
        else:
            self._handle_connect(mac)

    def _on_adapter_properties_changed(self, interface, changed, invalidated, path):
        # 适配器(Adapter1)属性变化 Powered/Discoverable/Pairable/Discovering/Alias 任何来源改动都实时推送前端刷新
        if interface != BLUEZ_IFACE_ADAPTER:
            return
        self._publish_bt_changed()

    def _on_interfaces_added(self, path, interfaces):
        # 新设备对象加入(如手机首次连接/配对) BlueZ 通过 InterfacesAdded 上报 命中 Device1 即推送避免手动扫描
        try:
            if BLUEZ_IFACE_DEVICE not in interfaces:
                return
        except TypeError:
            return
        self._publish_bt_changed()
        # 蓝牙音频节点由 PipeWire 在连接后异步创建，一并触发音频刷新作双保险。
        try:
            from event_system import event_bus
            event_bus.publish('audio.changed')
        except Exception as e:
            logger.debug(f"发布音频变更事件失败: {e}")

    def _on_interfaces_removed(self, path, interfaces):
        # 设备对象移除：命中 Device1 接口即实时推送前端刷新。
        try:
            if BLUEZ_IFACE_DEVICE not in interfaces:
                return
        except TypeError:
            return
        self._publish_bt_changed()
        try:
            from event_system import event_bus
            event_bus.publish('audio.changed')
        except Exception as e:
            logger.debug(f"发布音频变更事件失败: {e}")

    # 断开去抖窗口(秒)：信号质量差的蓝牙设备可能产生短暂 Connected 抖动(断开→1~2秒内自行恢复)，
    # 立即记录日志并调度重连会造成虚假的"断开-重连"循环刷屏，且重连定时器可能在设备已自行
    # 恢复后才触发，干扰正在进行的 profile 协商。去抖窗口内若设备自行恢复则静默忽略此次断开。
    _DISCONNECT_DEBOUNCE = 2.0

    def _handle_disconnect(self, mac):
        with self._lock:
            self._cleanup_expired_manual_disconnects()
            if mac in self._manual_disconnects:
                del self._manual_disconnects[mac]
                return
            if mac in self._disconnected_devices:
                return
            # 标记为"断开待确认"状态，阻止后续重复触发；真正调度重连前先经过去抖窗口
            self._disconnected_devices[mac] = {'retry_count': 0}
        # 加入"断开待确认"队列后立即推送，使前端重连指示器实时反映(_on_properties_changed 的
        # 那次 publish 早于本次入队，节流窗口内读不到该设备，故此处需补发一次)
        self._publish_bt_changed()
        # 去抖：延迟一小段时间后检查设备是否仍处于断开状态。
        # 若设备已自行恢复(_handle_connect 已将其从 _disconnected_devices 移除)，则静默忽略。
        timer = threading.Timer(self._DISCONNECT_DEBOUNCE, self._debounced_disconnect_confirm, args=(mac,))
        timer.daemon = True
        with self._lock:
            self._timers[mac] = timer
        timer.start()

    def _debounced_disconnect_confirm(self, mac):
        """去抖确认：经过去抖窗口后检查设备是否仍断开，是则真正记录并调度重连。"""
        if not self._running or not self._enabled:
            return
        dropped = False
        with self._lock:
            # 设备在去抖窗口内自行恢复(_handle_connect 已移除记录)，静默忽略
            if mac not in self._disconnected_devices:
                return
            # 去抖窗口内被标记为手动断开，跳过
            if mac in self._manual_disconnects:
                self._disconnected_devices.pop(mac, None)
                dropped = True
        if dropped:
            self._publish_bt_changed()
            return
        logger.info(f"设备 {mac} 已断开，计划重连")
        self._schedule_reconnect(mac)

    def _handle_connect(self, mac):
        removed = False
        with self._lock:
            if self._disconnected_devices.pop(mac, None) is not None:
                removed = True
            if mac in self._timers:
                self._timers[mac].cancel()
                del self._timers[mac]
        logger.info(f"设备 {mac} 已连接，停止重连")
        # 设备移出重连队列("正在重连 N 个"中 N 减少)：主动推送，
        # 避免重连指示器残留(该状态变化不经 D-Bus Connected 信号时无兜底刷新)。
        if removed:
            self._publish_bt_changed()

    def _schedule_reconnect(self, mac):
        reached_limit = False
        with self._lock:
            if mac not in self._disconnected_devices:
                return
            info = self._disconnected_devices[mac]
            if info['retry_count'] >= self.max_retries:
                logger.warning(f"设备 {mac} 重连已达上限({self.max_retries}次)，停止")
                self._disconnected_devices.pop(mac, None)
                reached_limit = True
            else:
                # 指数退避: delay = min(base_delay * 2^retry_count, max_delay)
                # retry_count=0 → 3s, retry_count=1 → 6s, retry_count=2 → 12s
                delay = min(self.base_delay * (2 ** info['retry_count']), self.max_delay)
                timer = self._timers.pop(mac, None)
                if timer:
                    timer.cancel()
                timer = threading.Timer(delay, self._try_reconnect, args=(mac,))
                timer.daemon = True
                self._timers[mac] = timer
                timer.start()
        # 达上限移出重连队列：主动推送刷新指示器
        if reached_limit:
            self._publish_bt_changed()

    def _try_reconnect(self, mac):
        if not self._running or not self._enabled:
            return
        with self._lock:
            if mac not in self._disconnected_devices:
                return

        try:
            device_path = None
            for path, ifaces in self._bus.get_object('org.bluez', '/').GetManagedObjects(
                    dbus_interface='org.freedesktop.DBus.ObjectManager').items():
                if BLUEZ_IFACE_DEVICE in ifaces:
                    p = ifaces[BLUEZ_IFACE_DEVICE]
                    if str(p.get('Address', '')).upper() == mac:
                        device_path = path
                        break

            if not device_path:
                logger.debug(f"设备 {mac} D-Bus 对象已消失，停止重连")
                with self._lock:
                    self._disconnected_devices.pop(mac, None)
                    self._timers.pop(mac, None)
                self._publish_bt_changed()
                return

            props = self._bus.get_object('org.bluez', device_path).GetAll(
                BLUEZ_IFACE_DEVICE, dbus_interface='org.freedesktop.DBus.Properties')
            if not props.get('Paired', False) and not props.get('Trusted', False):
                logger.debug(f"设备 {mac} 非已配对/信任设备，跳过重连")
                with self._lock:
                    self._disconnected_devices.pop(mac, None)
                    self._timers.pop(mac, None)
                self._publish_bt_changed()
                return

            from bluetooth_manager import connect_device
            from exceptions import InvalidParamError, CommandError, DeviceNotFoundError, ProfileUnavailableError
            connect_device(mac, is_auto_reconnect=True)
            logger.info(f"设备 {mac} 重连成功")
            self._handle_connect(mac)

        except (InvalidParamError, CommandError, DeviceNotFoundError, ProfileUnavailableError) as e:
            logger.warning(f"设备 {mac} 重连失败（不可恢复）: {e}")
            with self._lock:
                self._disconnected_devices.pop(mac, None)
                self._timers.pop(mac, None)
            self._publish_bt_changed()
            return

        except dbus.exceptions.DBusException as e:
            error_name = getattr(e, 'get_dbus_name', lambda: '')() or ''
            error_str = str(e)

            if 'UnknownObject' in error_name or 'UnknownObject' in error_str:
                logger.debug(f"设备 {mac} D-Bus 对象已消失，停止重连")
                with self._lock:
                    self._disconnected_devices.pop(mac, None)
                    self._timers.pop(mac, None)
                self._publish_bt_changed()
                return

            if 'profile-unavailable' in error_str or 'br-connection-profile' in error_str:
                logger.warning(f"设备 {mac} 音频 profile 不可用，停止重连")
                with self._lock:
                    self._disconnected_devices.pop(mac, None)
                    self._timers.pop(mac, None)
                self._publish_bt_changed()
                return

            logger.warning(f"设备 {mac} 重连失败: {e}")
            with self._lock:
                if mac in self._disconnected_devices:
                    self._disconnected_devices[mac]['retry_count'] += 1
            self._schedule_reconnect(mac)

        except Exception as e:
            logger.error(f"设备 {mac} 重连异常: {e}")
            with self._lock:
                if mac in self._disconnected_devices:
                    self._disconnected_devices[mac]['retry_count'] += 1
            self._schedule_reconnect(mac)

def _resolve_receive_dir():
    # 接收文件保存目录【唯一以飞牛 fnOS 授权的数据共享路径 TRIM_DATA_SHARE_PATHS 为准】。
    # TRIM_DATA_SHARE_PATHS 可能包含多个以 ':' 分隔的授权路径，取第一个非空段并在其下建 bluetooth 子目录。
    # 不做任何多级回退：若该环境变量缺失或目录不可创建/不可写，直接抛错，避免文件落到未授权/不可控位置。
    share_paths = os.environ.get('TRIM_DATA_SHARE_PATHS', '').strip()
    if not share_paths:
        raise RuntimeError(
            "未设置 TRIM_DATA_SHARE_PATHS，无法确定蓝牙接收保存路径。"
            "该应用要求由飞牛 fnOS 注入授权的数据共享路径。"
        )
    base = next((seg.strip() for seg in share_paths.split(':') if seg.strip()), '')
    if not base:
        raise RuntimeError(f"TRIM_DATA_SHARE_PATHS 无有效路径段: {share_paths!r}")
    target = os.path.join(base, 'bluetooth')
    os.makedirs(target, exist_ok=True)
    if not os.access(target, os.W_OK):
        raise RuntimeError(f"蓝牙接收目录不可写: {target}")
    logger.info(f"蓝牙接收目录(飞牛数据共享): {target}")
    return target

def _resolve_send_tmp_dir():
    # 发送临时目录：使用飞牛应用临时目录 TRIM_PKGTMP
    pkgtmp = os.environ.get('TRIM_PKGTMP', '').strip()
    if not pkgtmp:
        raise RuntimeError(
            "未设置 TRIM_PKGTMP，无法确定蓝牙发送临时目录。该应用要求由飞牛 fnOS 注入应用临时目录。"
        )
    return os.path.join(pkgtmp, 'pipebridge_obex_send')

RECEIVE_DIR = _resolve_receive_dir()
SEND_TMP_DIR = _resolve_send_tmp_dir()

# 单个上传文件大小上限（默认 2GB），可通过环境变量覆盖
_MAX_UPLOAD_SIZE = int(os.environ.get('PIPEBRIDGE_MAX_UPLOAD_BYTES', str(2 * 1024 * 1024 * 1024)))

TRANSFER_QUEUED = 'queued'
TRANSFER_ACTIVE = 'active'
TRANSFER_COMPLETE = 'complete'
TRANSFER_ERROR = 'error'
TRANSFER_CANCELLED = 'cancelled'

_transfers = {}
_transfers_lock = threading.Lock()
_transfer_counter = 0
_MAX_COMPLETED_TRANSFERS = 200

_obex_server_running = False
_receive_monitor_thread = None
_receive_known_files = set()
_receive_pending_sizes = {}

def _ensure_dirs():
    os.makedirs(RECEIVE_DIR, exist_ok=True)
    os.makedirs(SEND_TMP_DIR, exist_ok=True)

def _next_transfer_id():
    global _transfer_counter
    with _transfers_lock:
        _transfer_counter += 1
        return f't{_transfer_counter}'

# 文件传输实时推送 进度高频更新节流合并到 200ms 一次 状态变更(active/complete/error/cancelled)用 immediate=True 立即推
_transfer_notify_timer = None
_transfer_notify_lock = threading.Lock()

def _notify_transfer_changed(immediate=False):
    global _transfer_notify_timer
    def _fire():
        global _transfer_notify_timer
        with _transfer_notify_lock:
            _transfer_notify_timer = None
        try:
            from event_system import event_bus
            event_bus.publish('filetransfer.changed')
        except Exception as e:
            logger.debug(f"发布文件传输变更事件失败: {e}")
    with _transfer_notify_lock:
        if immediate:
            if _transfer_notify_timer:
                _transfer_notify_timer.cancel()
                _transfer_notify_timer = None
        elif _transfer_notify_timer:
            return
        if immediate:
            threading.Thread(target=_fire, daemon=True).start()
            return
        _transfer_notify_timer = threading.Timer(0.2, _fire)
        _transfer_notify_timer.daemon = True
        _transfer_notify_timer.start()


def _update_transfer_rate(transfer, transferred_bytes):
    # 基于两次采样计算瞬时速率(B/s)与 ETA(秒)写入 transfer；调用方需持有 _transfers_lock，采样间隔过短(<0.4s)时跳过避免速率抖动
    now = time.time()
    last_ts = transfer.get('_last_ts', 0)
    last_bytes = transfer.get('_last_bytes', 0)
    if last_ts and (now - last_ts) >= 0.4 and transferred_bytes >= last_bytes:
        delta_bytes = transferred_bytes - last_bytes
        delta_t = now - last_ts
        speed = delta_bytes / delta_t if delta_t > 0 else 0
        # 与历史速率做轻度平滑，避免瞬时跳变
        prev = transfer.get('speed', 0)
        transfer['speed'] = int(prev * 0.4 + speed * 0.6) if prev else int(speed)
        total = transfer.get('file_size', 0)
        remaining = max(0, total - transferred_bytes)
        transfer['eta'] = int(remaining / transfer['speed']) if transfer['speed'] > 0 else 0
        transfer['_last_ts'] = now
        transfer['_last_bytes'] = transferred_bytes
    elif not last_ts:
        transfer['_last_ts'] = now
        transfer['_last_bytes'] = transferred_bytes


def _check_obexctl():
    result = run_command('which obexctl 2>/dev/null', timeout=3)
    if not result['success'] or not result['stdout'].strip():
        raise CommandError('obexctl 未安装，请安装 bluez-obexd 包')
    return True

def _find_obexd_binary():
    # 定位 obexd 可执行文件：obexd 通常不在 PATH 中，需探测常见安装路径
    found = shutil.which('obexd')
    if found:
        return found
    for cand in (
        '/usr/lib/bluetooth/obexd',
        '/usr/libexec/bluetooth/obexd',
        '/usr/lib/bluez/obexd',
        '/usr/local/lib/bluetooth/obexd',
    ):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None

def _ensure_obex_service():
    # 确保 obexd 在用户会话总线上可用并注册 OBEX Agent：就绪以会话总线上 org.bluez.obex 名称可访问为准(非 pgrep)；obexd 须带 -a 自动接受根并绑定用户会话总线否则推送无授权回调被 Forbidden 拒绝；obexd 与 Agent 须挂同一条会话总线否则注册无效
    from bluetooth_agent import (
        _get_session_bus_address, ensure_obex_agent, obexd_service_available,
    )

    # 触发 _get_pw_env 推断/新建会话总线并写回 os.environ 保证后续 get_session_bus 与 run_command 使用同一条总线
    from utils import _get_pw_env
    _get_pw_env()
    session_addr = _get_session_bus_address()
    if session_addr:
        # 强制写入(覆盖 root/systemd 环境下可能残留的失效地址)，保证后续 D-Bus 与子进程用同一条会话总线
        os.environ['DBUS_SESSION_BUS_ADDRESS'] = session_addr

    # 就绪判断：直接问会话总线上的 org.bluez.obex 是否可用（可含按需激活）
    # 关键：obexd 的落盘目录(Root)必须与监控线程盯的 RECEIVE_DIR 一致，否则收到文件但前端无记录。
    # systemctl --user start obex 会用 unit 自带的默认 Root(通常 ~/.cache/obexd 或 ~/Downloads)，
    # 无法保证等于 RECEIVE_DIR，故这里【不走 systemd】，统一用真实路径手动拉起 obexd 并显式 -r RECEIVE_DIR。
    if not obexd_service_available():
        obexd_bin = _find_obexd_binary()
        if obexd_bin:
            env_prefix = ''
            if session_addr:
                env_prefix = f'DBUS_SESSION_BUS_ADDRESS={shlex.quote(session_addr)} '
            # -a 自动接受根、-r 指定接收目录(与 RECEIVE_DIR 强一致)、-n 不做 D-Bus 名称重复检查前的探测
            run_command(
                f'{env_prefix}{shlex.quote(obexd_bin)} -a -r {shlex.quote(RECEIVE_DIR)} >/dev/null 2>&1 &',
                timeout=3
            )
            # 等待手动拉起的 obexd 在会话总线就绪（最多 ~5s）
            for _ in range(10):
                time.sleep(0.5)
                if obexd_service_available():
                    break
            if not obexd_service_available():
                # 手动拉起未就绪，最后回退交给 user systemd(其 Root 不可控，收文件会落到别处，
                # 但监控线程已同时扫描 obexd 默认目录做兜底)
                logger.warning("手动拉起 obexd 未就绪，回退 systemctl --user start obex(接收目录可能非预期)")
                run_command('systemctl --user start obex 2>/dev/null', timeout=5)
                for _ in range(6):
                    time.sleep(0.5)
                    if obexd_service_available():
                        break
        else:
            logger.warning("未找到 obexd 可执行文件，回退 systemctl --user start obex")
            run_command('systemctl --user start obex 2>/dev/null', timeout=5)
            for _ in range(10):
                time.sleep(0.5)
                if obexd_service_available():
                    break

    ok = obexd_service_available()
    if not ok:
        logger.warning("obexd 未能在会话总线就绪，入站文件推送将被拒绝")
        return False

    # obexd 就绪后注册 OBEX Agent，使入站推送被自动接受
    try:
        if not ensure_obex_agent():
            logger.warning("OBEX Agent 注册未成功，入站文件推送可能被拒绝")
            return False
    except Exception as e:
        logger.warning(f"注册 OBEX Agent 异常: {e}")
        return False
    return True

def send_file(mac, file_path, file_name=None, device_name=None):
    _check_obexctl()
    _ensure_obex_service()

    # 路径安全校验 仅允许发送应用临时目录内已落盘文件 防止通过构造 file_path 读取任意系统文件
    real_path = os.path.realpath(file_path)
    if not real_path.startswith(os.path.realpath(SEND_TMP_DIR) + os.sep):
        raise CommandError('非法的文件路径')
    if not os.path.isfile(real_path):
        raise CommandError(f'文件不存在: {file_path}')
    file_path = real_path

    if not file_name:
        file_name = os.path.basename(file_path)

    file_size = os.path.getsize(file_path)
    transfer_id = _next_transfer_id()

    transfer = {
        'id': transfer_id,
        'mac': mac,
        'device_mac': mac,
        'device_name': device_name or mac,
        'file_name': file_name,
        'file_size': file_size,
        'direction': 'send',
        'status': TRANSFER_QUEUED,
        'progress': 0,
        'speed': 0,          # 瞬时速率 B/s
        'eta': 0,            # 预计剩余秒数
        '_last_bytes': 0,    # 上次采样已传字节
        '_last_ts': 0,       # 上次采样时间戳
        'created_at': time.time(),
    }

    with _transfers_lock:
        _transfers[transfer_id] = transfer
        completed = [tid for tid, t in _transfers.items()
                     if t['status'] in (TRANSFER_COMPLETE, TRANSFER_ERROR, TRANSFER_CANCELLED)]
        if len(completed) > _MAX_COMPLETED_TRANSFERS:
            completed.sort(key=lambda tid: _transfers[tid].get('created_at', 0))
            for tid in completed[:len(completed) - _MAX_COMPLETED_TRANSFERS]:
                del _transfers[tid]

    t = threading.Thread(target=_do_send_wrapped, args=(transfer_id, mac, file_path), daemon=True)
    t.start()

    # 新任务入列，立即推一次让前端出现该条目
    _notify_transfer_changed(immediate=True)
    return transfer

def _do_send_wrapped(transfer_id, mac, file_path):
    # 统一出口通知：无论 _do_send 从哪个分支返回，最终态都会立即推送前端
    try:
        _do_send(transfer_id, mac, file_path)
    finally:
        _notify_transfer_changed(immediate=True)

def _do_send(transfer_id, mac, file_path):
    with _transfers_lock:
        transfer = _transfers.get(transfer_id)
        if not transfer:
            return
        transfer['status'] = TRANSFER_ACTIVE
        transfer['started_at'] = time.time()

    _notify_transfer_changed(immediate=True)

    # 优先走 D-Bus obex.Client1（比 obexctl 交互式稳定），失败再回退 obexctl。
    try:
        if _dbus_send_file(transfer_id, mac, file_path):
            return
    except Exception as e:
        logger.warning(f"D-Bus 发送异常，回退 obexctl: {e}")

    proc = None
    try:
        proc = subprocess.Popen(
            ['obexctl'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        output_lines = []

        def _read_output():
            try:
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        output_lines.append(line)
                        logger.debug(f"obexctl: {line}")
                        m = re.search(r'Transfer\s+(\d+)%', line)
                        if m:
                            pct = int(m.group(1))
                            with _transfers_lock:
                                t = _transfers.get(transfer_id)
                                if t:
                                    t['progress'] = pct
                                 # obexctl 只给百分比，用 file_size 估算已传字节
                                    fs = t.get('file_size', 0)
                                    _update_transfer_rate(t, int(fs * pct / 100))
                            _notify_transfer_changed()
            except Exception as e:
                logger.debug(f"读取obexctl输出失败: {e}")

        reader = threading.Thread(target=_read_output, daemon=True)
        reader.start()

        time.sleep(0.5)

        # 必须显式指定 opp(Object Push Profile)会话 否则 obexctl 只建通用 OBEX 会话 手机端不弹接收文件授权提示
        commands = f"connect {mac} opp\n"
        proc.stdin.write(commands)
        proc.stdin.flush()

        # OPP 会话协商 + 手机端弹窗可能较慢，轮询等待连接结果而非固定 sleep。
        connect_ok = False
        connect_fail = False
        connect_deadline = time.time() + 12
        while time.time() < connect_deadline:
            time.sleep(0.5)
            recent = output_lines[-15:]
            if any(('Connection successful' in l) or ('Connected: yes' in l)
                   or re.search(r'/org/bluez/obex/(session|client)', l) for l in recent):
                connect_ok = True
                break
            if any(('Connection failed' in l) or ('Failed to connect' in l)
                   or ('Error' in l) or ('not available' in l.lower()) for l in recent):
                connect_fail = True
                break
            # 传输已开始（手机已接受）也视为连接成功
            if any('Transfer' in l for l in recent):
                connect_ok = True
                break

        if connect_fail and not connect_ok:
            with _transfers_lock:
                transfer = _transfers.get(transfer_id)
                if transfer:
                    transfer['status'] = TRANSFER_ERROR
                    transfer['error'] = '蓝牙连接失败，设备可能不支持 OBEX 文件传输'
                    transfer['completed_at'] = time.time()
            logger.warning(f"OBEX 连接失败: {mac}")
            try:
                proc.stdin.write("quit\n")
                proc.stdin.flush()
            except Exception as e:
                logger.debug(f"写入quit命令失败: {e}")
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            return

        send_cmd = f"send {file_path}\n"
        proc.stdin.write(send_cmd)
        proc.stdin.flush()

        file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        timeout = max(60, file_size // 50000 + 30)
        start_wait = time.time()

        while time.time() - start_wait < timeout:
            time.sleep(1)
            with _transfers_lock:
                t = _transfers.get(transfer_id)
                if t and t['status'] == TRANSFER_CANCELLED:
                    break
            recent = output_lines[-5:] if len(output_lines) >= 5 else output_lines
            if any('Transfer successful' in l or 'Transfer complete' in l for l in recent):
                break
            if any('Transfer failed' in l or 'Error' in l for l in recent):
                break

        try:
            proc.stdin.write("disconnect\n")
            proc.stdin.flush()
            time.sleep(0.5)
            proc.stdin.write("quit\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            logger.debug(f"写入obexctl命令失败: {e}")

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        reader.join(timeout=3)

        output = '\n'.join(output_lines)
        with _transfers_lock:
            transfer = _transfers.get(transfer_id)
            if not transfer:
                return

            if transfer['status'] == TRANSFER_CANCELLED:
                pass
            elif 'Transfer successful' in output or 'Transfer complete' in output:
                transfer['status'] = TRANSFER_COMPLETE
                transfer['progress'] = 100
                transfer['completed_at'] = time.time()
                logger.info(f"OBEX 发送成功: {transfer['file_name']} -> {mac}")
            elif 'Transfer failed' in output:
                transfer['status'] = TRANSFER_ERROR
                transfer['error'] = '传输失败，对方可能拒绝了文件或连接中断'
                transfer['completed_at'] = time.time()
                logger.warning(f"OBEX 传输失败: {transfer['file_name']} -> {mac}")
            elif 'not connected' in output.lower() or 'Connection failed' in output or 'Failed to connect' in output:
                transfer['status'] = TRANSFER_ERROR
                transfer['error'] = '连接失败，设备可能不支持 OPP 文件传输或未开启接收'
                transfer['completed_at'] = time.time()
                logger.warning(f"OBEX 连接失败: {transfer['file_name']} -> {mac}")
            elif 'No route' in output or 'Host is down' in output:
                transfer['status'] = TRANSFER_ERROR
                transfer['error'] = '设备不可达，请确认设备在线'
                transfer['completed_at'] = time.time()
                logger.warning(f"OBEX 设备不可达: {transfer['file_name']} -> {mac}")
            else:
                transfer['status'] = TRANSFER_ERROR
                transfer['error'] = '传输超时或对方未确认接收，请在手机上确认接收文件后重试'
                transfer['completed_at'] = time.time()
                logger.warning(f"OBEX 传输结果未知: {transfer['file_name']} -> {mac}, 输出: {output[:300]}")

    except Exception as e:
        with _transfers_lock:
            transfer = _transfers.get(transfer_id)
            if transfer:
                transfer['status'] = TRANSFER_ERROR
                transfer['error'] = str(e)[:200]
                transfer['completed_at'] = time.time()
        logger.error(f"OBEX 发送异常: {e}")
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception as e:
                logger.debug(f"终止进程失败: {e}")
            try:
                proc.wait(timeout=3)
            except Exception as e:
                logger.debug(f"等待进程退出失败: {e}")

    finally:
        try:
            if file_path.startswith(SEND_TMP_DIR):
                os.unlink(file_path)
        except OSError as e:
            logger.debug(f"清理临时文件失败: {e}")

def _dbus_send_file(transfer_id, mac, file_path):
    # 通过用户会话总线 org.bluez.obex.Client1 发送文件(OPP)：创建 target=opp 会话 -> ObjectPush1.SendFile -> 轮询 Transfer1 状态；成功/明确失败返回 True 并更新 transfer，无法建立或超时返回 False 交 obexctl 回退
    from bluetooth_agent import get_session_bus, OBEX_SERVICE, OBEX_IFACE_TRANSFER

    bus = get_session_bus()
    if bus is None:
        logger.debug("无会话总线，跳过 D-Bus 发送")
        return False

    client = dbus.Interface(
        bus.get_object(OBEX_SERVICE, '/org/bluez/obex'),
        'org.bluez.obex.Client1'
    )

    session_path = None
    try:
        session_path = client.CreateSession(mac, {'Target': dbus.String('opp')})
        push = dbus.Interface(
            bus.get_object(OBEX_SERVICE, session_path),
            'org.bluez.obex.ObjectPush1'
        )
        transfer_path, props = push.SendFile(file_path)

        total = 0
        try:
            total = int(props.get('Size', 0))
        except (TypeError, ValueError):
            total = 0

        transfer_props = dbus.Interface(
            bus.get_object(OBEX_SERVICE, transfer_path),
            'org.freedesktop.DBus.Properties'
        )

        file_size = os.path.getsize(file_path) if os.path.exists(file_path) else total
        timeout = max(60, file_size // 50000 + 30)
        start_wait = time.time()

        while time.time() - start_wait < timeout:
            with _transfers_lock:
                t = _transfers.get(transfer_id)
                if t and t['status'] == TRANSFER_CANCELLED:
                    try:
                        dbus.Interface(
                            bus.get_object(OBEX_SERVICE, transfer_path),
                            OBEX_IFACE_TRANSFER
                        ).Cancel()
                    except dbus.exceptions.DBusException as e:
                        logger.debug(f"取消传输失败: {e}")
                    return True

            try:
                status = str(transfer_props.Get(OBEX_IFACE_TRANSFER, 'Status'))
            except dbus.exceptions.DBusException:
                # 传输对象消失通常意味着已完成
                status = 'complete'

            if total > 0:
                try:
                    transferred = int(transfer_props.Get(OBEX_IFACE_TRANSFER, 'Transferred'))
                except dbus.exceptions.DBusException:
                    transferred = 0
                pct = min(99, int(transferred * 100 / total))
                with _transfers_lock:
                    t = _transfers.get(transfer_id)
                    if t and status not in ('complete', 'error'):
                        t['progress'] = pct
                        _update_transfer_rate(t, transferred)
                _notify_transfer_changed()

            if status == 'complete':
                with _transfers_lock:
                    t = _transfers.get(transfer_id)
                    if t:
                        t['status'] = TRANSFER_COMPLETE
                        t['progress'] = 100
                        t['completed_at'] = time.time()
                logger.info(f"OBEX(D-Bus) 发送成功: {file_path} -> {mac}")
                return True
            if status == 'error':
                with _transfers_lock:
                    t = _transfers.get(transfer_id)
                    if t:
                        t['status'] = TRANSFER_ERROR
                        t['error'] = '传输失败，对方可能拒绝了文件或连接中断'
                        t['completed_at'] = time.time()
                logger.warning(f"OBEX(D-Bus) 传输失败: {file_path} -> {mac}")
                return True

            time.sleep(1)

        logger.warning(f"OBEX(D-Bus) 传输超时，回退 obexctl: {file_path} -> {mac}")
        return False

    except dbus.exceptions.DBusException as e:
        logger.warning(f"OBEX(D-Bus) 会话/发送失败，将回退 obexctl: {e}")
        return False
    finally:
        if session_path is not None:
            try:
                client.RemoveSession(session_path)
            except dbus.exceptions.DBusException as e:
                logger.debug(f"移除OBEX会话失败: {e}")

def cancel_transfer(transfer_id):
    with _transfers_lock:
        transfer = _transfers.get(transfer_id)
        if not transfer:
            raise CommandError(f'传输 {transfer_id} 不存在')
        if transfer['status'] in (TRANSFER_COMPLETE, TRANSFER_ERROR, TRANSFER_CANCELLED):
            raise CommandError('传输已结束，无法取消')
        transfer['status'] = TRANSFER_CANCELLED
        transfer['completed_at'] = time.time()
    _notify_transfer_changed(immediate=True)
    return {'message': f'传输 {transfer_id} 已取消'}

def get_transfers():
    with _transfers_lock:
        return list(_transfers.values())

def clear_transfers():
    with _transfers_lock:
        to_remove = [tid for tid, t in _transfers.items()
                     if t['status'] in (TRANSFER_COMPLETE, TRANSFER_ERROR, TRANSFER_CANCELLED)]
        for tid in to_remove:
            del _transfers[tid]
    return {'message': f'已清除 {len(to_remove)} 条记录'}

def _monitor_received_files():
    _ensure_dirs()
    _receive_known_files.clear()
    _receive_pending_sizes.clear()

    # 监控目录集合：受控接收目录 RECEIVE_DIR + obexd 未受控启动时可能落盘的默认目录(兜底)
    def _scan_dirs():
        dirs = [RECEIVE_DIR]
        try:
            recv_real = os.path.realpath(RECEIVE_DIR)
            for d in _resolve_obexd_default_dirs():
                if os.path.realpath(d) != recv_real and d not in dirs:
                    dirs.append(d)
        except Exception as e:
            logger.debug(f"解析obexd默认目录失败: {e}")
        return dirs

    # 已知键用 (目录, 文件名) 唯一标识，避免不同目录同名文件互相覆盖；
    # 完成判定后记录内容指纹 (key,size,mtime)，同名文件被新一次传输覆盖(指纹变化)时可再次上报。
    def _key(dirpath, name):
        return (os.path.realpath(dirpath), name)

    for dirpath in _scan_dirs():
        try:
            for entry in os.listdir(dirpath):
                full_path = os.path.join(dirpath, entry)
                if os.path.isfile(full_path):
                    try:
                        st = os.stat(full_path)
                        _receive_known_files.add((_key(dirpath, entry), st.st_size, int(st.st_mtime)))
                    except OSError:
                        _receive_known_files.add((_key(dirpath, entry), -1, -1))
        except OSError as e:
            logger.debug(f"扫描接收目录失败: {e}")

    _cycle_count = 0
    while _obex_server_running:
        time.sleep(2)
        if not _obex_server_running:
            break
        _cycle_count += 1
        # 周期性清理已删除文件的已知记录，防止集合无限膨胀
        if _cycle_count % 30 == 0:
            try:
                existing = set()
                for dirpath in _scan_dirs():
                    for entry in os.listdir(dirpath):
                        existing.add(_key(dirpath, entry))
                _receive_known_files.intersection_update(
                    {rec for rec in _receive_known_files if rec[0] in existing}
                )
            except OSError as e:
                logger.debug(f"清理已知文件记录失败: {e}")
        try:
            for dirpath in _scan_dirs():
                try:
                    entries = os.listdir(dirpath)
                except OSError:
                    continue
                for entry in entries:
                    # 跳过隐藏/临时文件（obexd 传输中常以 . 开头或 .part/.tmp 结尾）
                    if entry.startswith('.') or entry.endswith('.part') or entry.endswith('.tmp'):
                        continue
                    full_path = os.path.join(dirpath, entry)
                    if not os.path.isfile(full_path):
                        continue
                    try:
                        st = os.stat(full_path)
                    except OSError:
                        continue
                    size_now = st.st_size
                    mtime_now = int(st.st_mtime)
                    fingerprint = (_key(dirpath, entry), size_now, mtime_now)
                    # 内容指纹已知 → 此前已上报过，跳过(支持同名覆盖后指纹变化再次上报)
                    if fingerprint in _receive_known_files:
                        continue
                    # H2完整性判定：文件大小需在两个检测周期内保持稳定，避免把仍在写入的文件误判为已完成
                    pending_key = _key(dirpath, entry)
                    prev_size = _receive_pending_sizes.get(pending_key)
                    if prev_size != size_now or size_now == 0:
                        _receive_pending_sizes[pending_key] = size_now
                        continue
                    # 连续两轮稳定，确认接收完成
                    _receive_pending_sizes.pop(pending_key, None)
                    _receive_known_files.add(fingerprint)
                    file_size = size_now
                    transfer_id = _next_transfer_id()
                    transfer = {
                        'id': transfer_id,
                        'mac': '',
                        'device_mac': '',
                        'device_name': '本机接收',
                        'file_name': entry,
                        'file_size': file_size,
                        'save_dir': os.path.realpath(dirpath),
                        'direction': 'receive',
                        'status': TRANSFER_COMPLETE,
                        'progress': 100,
                        'created_at': time.time(),
                        'completed_at': time.time(),
                    }
                    with _transfers_lock:
                        _transfers[transfer_id] = transfer
                        completed = [tid for tid, t in _transfers.items()
                                     if t['status'] in (TRANSFER_COMPLETE, TRANSFER_ERROR, TRANSFER_CANCELLED)]
                        if len(completed) > _MAX_COMPLETED_TRANSFERS:
                            completed.sort(key=lambda tid: _transfers[tid].get('created_at', 0))
                            for tid in completed[:len(completed) - _MAX_COMPLETED_TRANSFERS]:
                                del _transfers[tid]
                    where = '' if os.path.realpath(dirpath) == os.path.realpath(RECEIVE_DIR) else f' @ {dirpath}'
                    logger.info(f"OBEX 接收文件: {entry} ({_format_file_size(file_size)}){where}")
                    _notify_transfer_changed(immediate=True)
        except OSError as e:
            logger.debug(f"扫描接收目录失败: {e}")
        except Exception:
            # 监控线程是常驻 daemon，任何非 OSError 的未预期异常都不应使其崩溃退出，
            # 否则接收监控将静默失效直到重启服务。
            logger.exception("OBEX 接收监控循环发生未预期异常，本轮跳过")

def _format_file_size(size):
    if size < 1024:
        return f'{size} B'
    if size < 1048576:
        return f'{size / 1024:.1f} KB'
    return f'{size / 1048576:.1f} MB'

def start_obex_server():
    global _obex_server_running, _receive_monitor_thread
    if _obex_server_running:
        return {'message': '接收服务已在运行', 'receive_dir': RECEIVE_DIR}

    _ensure_dirs()
    obex_ok = _ensure_obex_service()
    if not obex_ok:
        raise CommandError('OBEX 服务启动失败，请确认 bluez-obexd 已安装')

    _obex_server_running = True
    _receive_monitor_thread = threading.Thread(target=_monitor_received_files, daemon=True)
    _receive_monitor_thread.start()
    # 开启接收时自动置为可被发现 否则手机侧无法搜索到本机 discoverable 设置失败不阻断接收服务启动
    discoverable = False
    try:
        from bluetooth_manager import set_discoverable
        set_discoverable(True)
        discoverable = True
    except Exception as e:
        logger.warning(f"开启接收时设置可发现失败: {e}")
    logger.info("OBEX 接收服务已就绪")
    return {'message': '接收服务已启动', 'receive_dir': RECEIVE_DIR, 'discoverable': discoverable}

def stop_obex_server():
    global _obex_server_running
    _obex_server_running = False
    logger.info("OBEX 接收服务已停止")
    return {'message': '接收服务已停止'}

def is_obex_server_running():
    return _obex_server_running

def get_obex_agent_ready():
    # OBEX Agent 是否就绪(决定入站文件推送能否被自动接受)，只读不触发注册
    try:
        from bluetooth_agent import is_obex_agent_ready
        return bool(is_obex_agent_ready())
    except Exception:
        return False

def fix_obex_agent():
    # 一键修复 OBEX Agent：确保 obexd 就绪并(重新)注册 Agent
    try:
        ok = _ensure_obex_service()
        ready = get_obex_agent_ready()
        return {
            'success': bool(ok and ready),
            'obex_agent_ready': ready,
            'message': '接收授权已就绪' if ready else 'OBEX Agent 注册失败，请检查 obexd 与会话总线'
        }
    except Exception as e:
        logger.warning(f"修复 OBEX Agent 失败: {e}")
        return {'success': False, 'obex_agent_ready': False, 'message': f'修复失败: {e}'}

def get_max_upload_size():
    return _MAX_UPLOAD_SIZE

def get_received_files():
    _ensure_dirs()
    files = []
    # 接收文件唯一保存在 RECEIVE_DIR(obexd 受控落盘)，只扫描该目录
    try:
        for entry in os.listdir(RECEIVE_DIR):
            full_path = os.path.join(RECEIVE_DIR, entry)
            if not os.path.isfile(full_path):
                continue
            stat = os.stat(full_path)
            files.append({
                'name': entry,
                'size': stat.st_size,
                'modified': stat.st_mtime,
                'save_dir': RECEIVE_DIR,
            })
    except OSError as e:
        logger.debug(f"列出接收文件失败: {e}")
    files.sort(key=lambda f: f['modified'], reverse=True)
    return files

def save_upload_file(upload_file):
    _ensure_dirs()
    file_name = upload_file.filename or 'unknown'
    safe_name = ''.join(c for c in file_name if c.isalnum() or c in '._-').strip()
    if not safe_name:
        safe_name = 'upload_file'
    dest = os.path.join(SEND_TMP_DIR, safe_name)
    if os.path.exists(dest):
        name, ext = os.path.splitext(safe_name)
        dest = os.path.join(SEND_TMP_DIR, f'{name}_{int(time.time())}{ext}')

    # M1：磁盘空间预检，避免大文件写满临时目录后才失败
    try:
        free_bytes = shutil.disk_usage(SEND_TMP_DIR).free
    except OSError:
        free_bytes = None

    # M1：流式写入 + 大小上限校验，超限立即中止并清理半写文件
    written = 0
    chunk_size = 1024 * 1024
    try:
        with open(dest, 'wb') as f:
            while True:
                chunk = upload_file.file.read(chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_UPLOAD_SIZE:
                    raise CommandError(
                        f'文件超过大小上限（{_MAX_UPLOAD_SIZE // (1024 * 1024)} MB）'
                    )
                if free_bytes is not None and written > free_bytes:
                    raise CommandError('临时目录磁盘空间不足，无法保存待发送文件')
                f.write(chunk)
    except Exception:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError as e:
            logger.debug(f"清理目标文件失败: {e}")
        raise

    return dest
