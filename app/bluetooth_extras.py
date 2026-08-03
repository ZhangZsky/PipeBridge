import os
import re
import time
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

    def __init__(self, bus, max_retries=1, base_delay=5, max_delay=5):
        self._bus = bus
        self._disconnected_devices = {}
        self._timers = {}
        self._manual_disconnects = {}
        self._lock = threading.RLock()
        self._running = False
        self._enabled = True
        self._signal_match = None
        self._adapter_signal_match = None
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        # 蓝牙状态实时推送节流：DBus 信号（尤其 RSSI）可能高频触发，
        # 合并到 200ms 一次全量刷新事件，避免刷屏
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
            # 同时监听适配器(Adapter1)属性变化：Powered/Discoverable/Pairable/Discovering
            # 等外部改动也能实时驱动前端刷新（原来只监听 Device1 导致适配器状态不实时）
            self._adapter_signal_match = self._bus.add_signal_receiver(
                self._on_adapter_properties_changed,
                dbus_interface='org.freedesktop.DBus.Properties',
                signal_name='PropertiesChanged',
                arg0=BLUEZ_IFACE_ADAPTER,
                path_keyword='path'
            )
        except dbus.exceptions.DBusException as e:
            logger.warning(f"注册适配器信号监听失败: {e}")
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
            except Exception:
                pass
        if self._adapter_signal_match:
            try:
                self._adapter_signal_match.remove()
            except Exception:
                pass

    def set_enabled(self, enabled):
        self._enabled = enabled
        if not enabled:
            with self._lock:
                for mac, timer in self._timers.items():
                    if timer:
                        timer.cancel()
                self._timers.clear()
                self._disconnected_devices.clear()

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
        with self._lock:
            self._manual_disconnects[mac] = _time.time()
            self._disconnected_devices.pop(mac, None)
            timer = self._timers.pop(mac, None)
            if timer:
                timer.cancel()

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
                except Exception:
                    pass
            self._publish_timer = threading.Timer(0.2, _fire)
            self._publish_timer.daemon = True
            self._publish_timer.start()

    def _on_properties_changed(self, interface, changed, invalidated, path):
        if interface != BLUEZ_IFACE_DEVICE:
            return

        # 只要是设备属性变化（Connected/RSSI/Paired/Trusted 等）就实时推送前端，
        # 由 DBus 信号驱动取代后端高频轮询；节流避免刷屏
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
        # 适配器(Adapter1)属性变化：Powered/Discoverable/Pairable/Discovering/Alias 等，
        # 无论谁改动（本程序、命令行、其它进程）都实时推送前端刷新状态与开关
        if interface != BLUEZ_IFACE_ADAPTER:
            return
        self._publish_bt_changed()

    def _handle_disconnect(self, mac):
        with self._lock:
            self._cleanup_expired_manual_disconnects()
            if mac in self._manual_disconnects:
                del self._manual_disconnects[mac]
                return
            if mac in self._disconnected_devices:
                return
            stale = [m for m, info in self._disconnected_devices.items()
                     if info.get('retry_count', 0) >= self.max_retries]
            for m in stale:
                self._disconnected_devices.pop(m, None)
            try:
                import config as _config
                if _config.is_reconnect_blacklisted(mac):
                    logger.debug(f"设备 {mac} 在重连黑名单中，跳过重连")
                    return
            except Exception:
                pass
            self._disconnected_devices[mac] = {'retry_count': 0}
        logger.info(f"设备 {mac} 已断开，计划重连")
        self._schedule_reconnect(mac)

    def _handle_connect(self, mac):
        with self._lock:
            self._disconnected_devices.pop(mac, None)
            if mac in self._timers:
                self._timers[mac].cancel()
                del self._timers[mac]
        logger.info(f"设备 {mac} 已连接，停止重连")

    def _schedule_reconnect(self, mac):
        with self._lock:
            if mac not in self._disconnected_devices:
                return
            info = self._disconnected_devices[mac]
            if info['retry_count'] >= self.max_retries:
                logger.warning(f"设备 {mac} 重连已达上限({self.max_retries}次)，停止")
                self._disconnected_devices.pop(mac, None)
                return
            delay = min(self.base_delay, self.max_delay)
            timer = self._timers.pop(mac, None)
            if timer:
                timer.cancel()
            timer = threading.Timer(delay, self._try_reconnect, args=(mac,))
            timer.daemon = True
            self._timers[mac] = timer
            timer.start()

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
                return

            props = self._bus.get_object('org.bluez', device_path).GetAll(
                BLUEZ_IFACE_DEVICE, dbus_interface='org.freedesktop.DBus.Properties')
            if not props.get('Paired', False) and not props.get('Trusted', False):
                logger.debug(f"设备 {mac} 非已配对/信任设备，跳过重连")
                with self._lock:
                    self._disconnected_devices.pop(mac, None)
                    self._timers.pop(mac, None)
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
            return

        except dbus.exceptions.DBusException as e:
            error_name = getattr(e, 'get_dbus_name', lambda: '')() or ''
            error_str = str(e)

            if 'UnknownObject' in error_name or 'UnknownObject' in error_str:
                logger.debug(f"设备 {mac} D-Bus 对象已消失，停止重连")
                with self._lock:
                    self._disconnected_devices.pop(mac, None)
                    self._timers.pop(mac, None)
                return

            if 'profile-unavailable' in error_str or 'br-connection-profile' in error_str:
                logger.warning(f"设备 {mac} 音频 profile 不可用，停止重连")
                with self._lock:
                    self._disconnected_devices.pop(mac, None)
                    self._timers.pop(mac, None)
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

RECEIVE_DIR = os.path.join(os.path.expanduser('~'), 'Downloads', 'bluetooth')
SEND_TMP_DIR = '/tmp/pipebridge_obex_send'

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

def _ensure_dirs():
    os.makedirs(RECEIVE_DIR, exist_ok=True)
    os.makedirs(SEND_TMP_DIR, exist_ok=True)

def _next_transfer_id():
    global _transfer_counter
    with _transfers_lock:
        _transfer_counter += 1
        return f't{_transfer_counter}'

# 文件传输实时推送：进度更新可能高频（obexctl 逐百分比），节流合并到
# 200ms 一次；状态变更（active/complete/error/cancelled）用 immediate=True 立即推
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
        except Exception:
            pass
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


def _check_obexctl():
    result = run_command('which obexctl 2>/dev/null', timeout=3)
    if not result['success'] or not result['stdout'].strip():
        raise CommandError('obexctl 未安装，请安装 bluez-obexd 包')
    return True

def _ensure_obex_service():
    result = run_command('pgrep -x obexd 2>/dev/null', timeout=3)
    if result['stdout'].strip():
        return True
    result = run_command('systemctl --user start obex 2>/dev/null', timeout=5)
    if not result['success']:
        result = run_command('obexd -r /tmp/obex-inbox -r ~/Downloads/bluetooth 2>/dev/null &', timeout=3)
    time.sleep(0.5)
    result = run_command('pgrep -x obexd 2>/dev/null', timeout=3)
    return bool(result['stdout'].strip())

def send_file(mac, file_path, file_name=None):
    _check_obexctl()
    _ensure_obex_service()

    if not os.path.exists(file_path):
        raise CommandError(f'文件不存在: {file_path}')

    if not file_name:
        file_name = os.path.basename(file_path)

    file_size = os.path.getsize(file_path)
    transfer_id = _next_transfer_id()

    transfer = {
        'id': transfer_id,
        'mac': mac,
        'file_name': file_name,
        'file_size': file_size,
        'direction': 'send',
        'status': TRANSFER_QUEUED,
        'progress': 0,
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
                            _notify_transfer_changed()
            except Exception:
                pass

        reader = threading.Thread(target=_read_output, daemon=True)
        reader.start()

        time.sleep(0.5)

        commands = f"connect {mac}\n"
        proc.stdin.write(commands)
        proc.stdin.flush()
        time.sleep(2)

        connect_ok = any('Connection successful' in l or 'Connected: yes' in l for l in output_lines[-10:])
        connect_fail = any('Connection failed' in l or 'Error' in l for l in output_lines[-10:])

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
            except Exception:
                pass
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
        except (BrokenPipeError, OSError):
            pass

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
            elif 'not connected' in output.lower() or 'Connection failed' in output:
                transfer['status'] = TRANSFER_ERROR
                transfer['error'] = '连接失败，设备可能不支持文件传输'
                transfer['completed_at'] = time.time()
                logger.warning(f"OBEX 连接失败: {transfer['file_name']} -> {mac}")
            elif 'No route' in output or 'Host is down' in output:
                transfer['status'] = TRANSFER_ERROR
                transfer['error'] = '设备不可达，请确认设备在线'
                transfer['completed_at'] = time.time()
                logger.warning(f"OBEX 设备不可达: {transfer['file_name']} -> {mac}")
            else:
                transfer['status'] = TRANSFER_ERROR
                transfer['error'] = '传输超时或结果未知'
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
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                pass

    finally:
        try:
            if file_path.startswith(SEND_TMP_DIR):
                os.unlink(file_path)
        except OSError:
            pass

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
    try:
        for entry in os.listdir(RECEIVE_DIR):
            full_path = os.path.join(RECEIVE_DIR, entry)
            if os.path.isfile(full_path):
                _receive_known_files.add(entry)
    except OSError:
        pass

    _cycle_count = 0
    while _obex_server_running:
        time.sleep(2)
        if not _obex_server_running:
            break
        _cycle_count += 1
        if _cycle_count % 30 == 0:
            try:
                current_files = set(os.listdir(RECEIVE_DIR))
                _receive_known_files.intersection_update(current_files)
            except OSError:
                pass
        try:
            for entry in os.listdir(RECEIVE_DIR):
                if entry in _receive_known_files:
                    continue
                full_path = os.path.join(RECEIVE_DIR, entry)
                if not os.path.isfile(full_path):
                    continue
                _receive_known_files.add(entry)
                file_size = os.path.getsize(full_path)
                transfer_id = _next_transfer_id()
                transfer = {
                    'id': transfer_id,
                    'mac': '',
                    'file_name': entry,
                    'file_size': file_size,
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
                logger.info(f"OBEX 接收文件: {entry} ({_format_file_size(file_size)})")
                _notify_transfer_changed(immediate=True)
        except OSError:
            pass

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
    logger.info("OBEX 接收服务已就绪")
    return {'message': '接收服务已启动', 'receive_dir': RECEIVE_DIR}

def stop_obex_server():
    global _obex_server_running
    _obex_server_running = False
    logger.info("OBEX 接收服务已停止")
    return {'message': '接收服务已停止'}

def is_obex_server_running():
    return _obex_server_running

def get_received_files():
    _ensure_dirs()
    files = []
    try:
        for entry in os.listdir(RECEIVE_DIR):
            full_path = os.path.join(RECEIVE_DIR, entry)
            if os.path.isfile(full_path):
                stat = os.stat(full_path)
                files.append({
                    'name': entry,
                    'size': stat.st_size,
                    'modified': stat.st_mtime,
                    'path': full_path,
                })
    except OSError:
        pass
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

    with open(dest, 'wb') as f:
        shutil.copyfileobj(upload_file.file, f)

    return dest
