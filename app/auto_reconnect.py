import logging
import threading
import dbus

logger = logging.getLogger('MediaBridge')

BLUEZ_SERVICE = 'org.bluez'
BLUEZ_IFACE_DEVICE = 'org.bluez.Device1'


class AutoReconnectManager:
    """蓝牙设备自动重连管理器"""

    # 手动断开标记的 TTL（秒），超时后自动清除，防止永久阻止重连
    _MANUAL_DISCONNECT_TTL = 1800  # 30 分钟

    # 初始化重连管理器
    def __init__(self, bus, activate_sink_callback=None, max_retries=5, base_delay=3, max_delay=60):
        self._bus = bus
        self._activate_sink = activate_sink_callback
        self._disconnected_devices = {}
        self._timers = {}
        self._manual_disconnects = {}  # mac -> timestamp
        self._lock = threading.RLock()
        self._running = False
        self._enabled = True
        self._signal_match = None
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay

    # 启动信号监听
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
        logger.debug("蓝牙自动重连监控已启动")

    # 停止监听和定时器
    def stop(self):
        with self._lock:
            self._running = False
            for mac, timer in self._timers.items():
                if timer:
                    timer.cancel()
            self._timers.clear()
        if self._signal_match:
            try:
                self._signal_match.remove()
            except Exception:
                pass

    # 启用/禁用自动重连
    def set_enabled(self, enabled):
        self._enabled = enabled
        if not enabled:
            with self._lock:
                for mac, timer in self._timers.items():
                    if timer:
                        timer.cancel()
                self._timers.clear()
                self._disconnected_devices.clear()

    # 获取监控状态
    def get_status(self):
        with self._lock:
            self._cleanup_expired_manual_disconnects()
            return {
                'monitoring': self._running and self._enabled,
                'reconnecting_devices': list(self._disconnected_devices.keys()),
                'manual_disconnects': list(self._manual_disconnects.keys())
            }

    # 清理过期的手动断开标记
    def _cleanup_expired_manual_disconnects(self):
        import time as _time
        now = _time.time()
        expired = [mac for mac, ts in self._manual_disconnects.items()
                   if now - ts > self._MANUAL_DISCONNECT_TTL]
        for mac in expired:
            del self._manual_disconnects[mac]

    # 标记手动断开
    def mark_manual_disconnect(self, mac):
        import time as _time
        mac = mac.upper()
        with self._lock:
            self._manual_disconnects[mac] = _time.time()
            self._disconnected_devices.pop(mac, None)
            timer = self._timers.pop(mac, None)
            if timer:
                timer.cancel()

    def _on_properties_changed(self, interface, changed, invalidated, path):
        if interface != BLUEZ_IFACE_DEVICE:
            return
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

    # 处理设备断开事件
    def _handle_disconnect(self, mac):
        with self._lock:
            self._cleanup_expired_manual_disconnects()
            if mac in self._manual_disconnects:
                del self._manual_disconnects[mac]
                return
            if mac in self._disconnected_devices:
                return
            # 清理已完成重连但未从 _disconnected_devices 移除的条目
            stale = [m for m, info in self._disconnected_devices.items()
                     if info.get('retry_count', 0) >= self.max_retries]
            for m in stale:
                self._disconnected_devices.pop(m, None)
            # 检查重连黑名单
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

    # 处理设备连接事件
    def _handle_connect(self, mac):
        with self._lock:
            # 设备连接成功，从断开列表移除
            self._disconnected_devices.pop(mac, None)
            # 注意：不清除 _manual_disconnects，该标记由 TTL 过期自动清除
            # 避免设备短暂重连又断开时触发非预期自动重连
            if mac in self._timers:
                self._timers[mac].cancel()
                del self._timers[mac]
        logger.info(f"设备 {mac} 已连接，停止重连")

    # 调度延迟重连（指数退避）
    def _schedule_reconnect(self, mac):
        with self._lock:
            if mac not in self._disconnected_devices:
                return
            info = self._disconnected_devices[mac]
            if info['retry_count'] >= self.max_retries:
                logger.warning(f"设备 {mac} 重连已达上限({self.max_retries}次)，停止")
                self._disconnected_devices.pop(mac, None)
                return
            delay = min(self.base_delay * (2 ** info['retry_count']), self.max_delay)
            timer = self._timers.pop(mac, None)
            if timer:
                timer.cancel()
            timer = threading.Timer(delay, self._try_reconnect, args=(mac,))
            timer.daemon = True
            self._timers[mac] = timer
            timer.start()

    # 尝试重连设备
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

            # 检查是否已配对/信任
            props = self._bus.get_object('org.bluez', device_path).GetAll(
                BLUEZ_IFACE_DEVICE, dbus_interface='org.freedesktop.DBus.Properties')
            if not props.get('Paired', False) and not props.get('Trusted', False):
                logger.debug(f"设备 {mac} 非已配对/信任设备，跳过重连")
                with self._lock:
                    self._disconnected_devices.pop(mac, None)
                    self._timers.pop(mac, None)
                return

            device = dbus.Interface(self._bus.get_object('org.bluez', device_path), BLUEZ_IFACE_DEVICE)
            # 使用线程执行 Connect，避免阻塞
            connect_result = [None]
            connect_error = [None]
            def _do_connect():
                try:
                    device.Connect()
                    connect_result[0] = True
                except Exception as e:
                    connect_error[0] = e
            t = threading.Thread(target=_do_connect, daemon=True)
            t.start()
            t.join(timeout=15)  # 15秒超时
            if t.is_alive():
                # 超时，连接仍在进行中，不阻塞，继续轮询等待结果
                logger.debug(f"Connect 调用超时(15s)，继续轮询等待连接结果: {mac}")
            elif connect_error[0]:
                raise connect_error[0]
            logger.info(f"设备 {mac} 重连成功")
            self._handle_connect(mac)

            # 重连成功后激活音频 Sink
            if self._activate_sink:
                try:
                    self._activate_sink(mac)
                except Exception as e:
                    logger.warning(f"重连后激活音频 Sink 失败: {e}")

        except dbus.exceptions.DBusException as e:
            error_name = getattr(e, 'get_dbus_name', lambda: '')() or ''
            error_str = str(e)

            # 设备对象不存在
            if 'UnknownObject' in error_name or 'UnknownObject' in error_str:
                logger.debug(f"设备 {mac} D-Bus 对象已消失，停止重连")
                with self._lock:
                    self._disconnected_devices.pop(mac, None)
                    self._timers.pop(mac, None)
                return

            # profile 不可用，无法通过重连解决
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
