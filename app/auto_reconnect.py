import time
import logging
import threading
import dbus

logger = logging.getLogger('MediaHub')

BLUEZ_SERVICE = 'org.bluez'
BLUEZ_IFACE_DEVICE = 'org.bluez.Device1'


class AutoReconnectManager:
    """蓝牙设备自动重连管理器"""

    def __init__(self, bus, activate_sink_callback=None):
        self._bus = bus
        self._activate_sink = activate_sink_callback
        self._disconnected_devices = {}
        self._timers = {}
        self._manual_disconnects = set()
        self._lock = threading.Lock()
        self._running = False
        self._enabled = True
        self._signal_match = None

    def start(self):
        if self._running:
            return
        self._running = True
        try:
            self._signal_match = self._bus.add_signal_receiver(
                self._on_properties_changed,
                dbus_interface='org.freedesktop.DBus.Properties',
                signal_name='PropertiesChanged',
                arg0=BLUEZ_IFACE_DEVICE
            )
        except dbus.exceptions.DBusException as e:
            logger.warning(f"注册蓝牙信号监听失败: {e}")
        logger.debug("蓝牙自动重连监控已启动")

    def stop(self):
        with self._lock:
            self._running = False
        if self._signal_match:
            try:
                self._signal_match.remove()
            except Exception:
                pass
        with self._lock:
            for mac, timer in self._timers.items():
                if timer:
                    timer.cancel()
            self._timers.clear()

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
            return {
                'monitoring': self._running and self._enabled,
                'reconnecting_devices': list(self._disconnected_devices.keys()),
                'manual_disconnects': list(self._manual_disconnects)
            }

    def mark_manual_disconnect(self, mac):
        mac = mac.upper()
        with self._lock:
            self._manual_disconnects.add(mac)
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

    def _handle_disconnect(self, mac):
        with self._lock:
            if mac in self._manual_disconnects:
                self._manual_disconnects.discard(mac)
                return
            if mac in self._disconnected_devices:
                return
            self._disconnected_devices[mac] = {'retry_count': 0}
        logger.info(f"设备 {mac} 已断开，计划重连")
        self._schedule_reconnect(mac)

    def _handle_connect(self, mac):
        with self._lock:
            self._disconnected_devices.pop(mac, None)
            self._manual_disconnects.discard(mac)
            timer = self._timers.pop(mac, None)
            if timer:
                timer.cancel()

    def _schedule_reconnect(self, mac, delay=5):
        with self._lock:
            if mac not in self._disconnected_devices:
                return
            info = self._disconnected_devices[mac]
            if info['retry_count'] >= 3:
                logger.warning(f"设备 {mac} 重连已达上限，停止")
                self._disconnected_devices.pop(mac, None)
                return
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
            device.Connect()
            logger.info(f"设备 {mac} 重连成功")
            self._handle_connect(mac)

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
