"""AVRCP 媒体键桥接(场景二：对端状态/音量同步)。

架构说明
========
场景一(接收音箱按键)已移交 r1.toolbox 直接监听 /dev/input/event*：
    R1 播音乐→蓝牙音箱外放，音箱物理按键=AVRCP Controller passthrough 命令，
    BlueZ 经内建 uinput 机制映射成标准 Linux input 设备(名含 "(AVRCP)")。
    r1.toolbox 自身已有 /dev/input 监听基础设施(触摸屏)，可直接监听 AVRCP
    按键设备，无需经 PipeBridge SSE 中转。
    因此本模块不再监听 /dev/input，避免两个进程抢同一个 fd。

场景二(Controller)：手机/音箱播放→R1 接收(A2DP Sink)，对端在 BlueZ 上暴露
    org.bluez.MediaPlayer1 对象。本模块监听其 PropertiesChanged(Status/Track/
    Position)，Status 变化推送 status 事件；并通过 ObjectManager 动态发现增删。
    同时监听 org.bluez.MediaTransport1.Volume(AVRCP 绝对音量 0-127)。

事件契约(r1.toolbox 依此解析)：event_bus.publish("mediakey", {...})
    {"status": "playing|paused|stopped", "source": "<MAC>"}
    {"action": "volume", "value": <0-127>, "source": "<MAC>"}

架构：D-Bus 主循环(GLib.MainLoop)，随 app 生命周期启动/停止。
"""

import logging
import threading

# dbus 为可降级依赖:缺失时容错导入,避免顶层硬 import 崩溃整个应用。
# AVRCP 桥接的启动入口由上层 try/except 兜底,dbus 缺失时该功能自动不启用。
try:
    import dbus
    HAS_DBUS = True
except ImportError:
    dbus = None
    HAS_DBUS = False

logger = logging.getLogger('PipeBridge')

BLUEZ_SERVICE = 'org.bluez'
IFACE_MEDIA_PLAYER = 'org.bluez.MediaPlayer1'
IFACE_MEDIA_TRANSPORT = 'org.bluez.MediaTransport1'
IFACE_ADAPTER = 'org.bluez.Adapter1'
IFACE_DEVICE = 'org.bluez.Device1'
DBUS_PROP_IFACE = 'org.freedesktop.DBus.Properties'
DBUS_OM_IFACE = 'org.freedesktop.DBus.ObjectManager'


def _publish(payload):
    """统一推送 mediakey 事件，任何异常都吞掉，绝不影响信号回调。"""
    try:
        from event_system import event_bus
        event_bus.publish('mediakey', payload)
        logger.debug(f"AVRCP 推送 mediakey 事件: {payload}")
    except Exception as e:
        logger.warning(f"AVRCP 推送 mediakey 事件失败(忽略): {e}")


def _mac_from_path(path):
    """从 D-Bus 对象路径解析设备 MAC，如 /org/bluez/hci0/dev_AA_.../player0。"""
    try:
        for seg in str(path).split('/'):
            if seg.startswith('dev_'):
                return seg[4:].replace('_', ':')
    except Exception as e:
        logger.debug(f"从 D-Bus 路径解析设备地址失败: {e}")
    return ''


class AVRCPBridge:
    """AVRCP 桥接(场景二)：对端播放状态/音量的 D-Bus 信号监听。"""

    def __init__(self):
        self._bus = None
        self._transport_volume = {}         # transport path -> 上次 Volume，去抖
        self._mp_status = {}                # MediaPlayer1 path -> 上次 Status，去抖
        self._signal_matches = []
        self._lock = threading.RLock()
        self._started = False

    # ---------- 生命周期 ----------
    def start(self):
        with self._lock:
            if self._started:
                return
            try:
                from bluetooth_manager import _get_system_bus
                self._bus = _get_system_bus()
            except Exception as e:
                logger.warning(f"AVRCP 获取系统总线失败({e})，场景二降级不启用")
                self._bus = None

            self._ensure_glib_loop()
            if self._bus is not None:
                self._subscribe_signals()
                self._scan_existing_media_players()
            self._started = True
            logger.info("AVRCP 媒体键桥接已启动(场景二 D-Bus 信号)")

    def stop(self):
        with self._lock:
            if not self._started:
                return
            for m in self._signal_matches:
                try:
                    m.remove()
                except Exception as e:
                    logger.debug(f"移除 D-Bus 信号匹配器失败: {e}")
            self._signal_matches = []
            self._started = False
            logger.info("AVRCP 媒体键桥接已停止")

    def _ensure_glib_loop(self):
        # 复用 bluetooth_agent 的 GLib 主循环(全局单例)，D-Bus 信号回调依赖它派发
        try:
            from bluetooth_agent import _ensure_glib_loop
            _ensure_glib_loop()
        except Exception as e:
            logger.warning(f"AVRCP 启动 GLib 主循环失败(场景二信号可能收不到): {e}")

    # ---------- 场景二：对端状态/音量 D-Bus 信号 ----------
    def _subscribe_signals(self):
        try:
            m1 = self._bus.add_signal_receiver(
                self._on_properties_changed,
                dbus_interface=DBUS_PROP_IFACE,
                signal_name='PropertiesChanged',
                path_keyword='path')
            self._signal_matches.append(m1)
            m2 = self._bus.add_signal_receiver(
                self._on_interfaces_added,
                dbus_interface=DBUS_OM_IFACE,
                signal_name='InterfacesAdded')
            self._signal_matches.append(m2)
            m3 = self._bus.add_signal_receiver(
                self._on_interfaces_removed,
                dbus_interface=DBUS_OM_IFACE,
                signal_name='InterfacesRemoved')
            self._signal_matches.append(m3)
            logger.info("AVRCP D-Bus 信号监听已注册(PropertiesChanged/ObjectManager)")
        except Exception as e:
            logger.warning(f"AVRCP 信号订阅失败(降级): {e}")

    def _on_properties_changed(self, interface, changed, invalidated, path=None):
        try:
            if interface == IFACE_MEDIA_PLAYER:
                self._handle_mediaplayer_props(path, changed)
            elif interface == IFACE_MEDIA_TRANSPORT:
                self._handle_transport_props(path, changed)
        except Exception as e:
            logger.warning(f"AVRCP PropertiesChanged 回调异常(忽略): {e}")

    def _handle_mediaplayer_props(self, path, changed):
        # 对端 MediaPlayer1 Status 变化 -> 推送播放状态同步
        if 'Status' not in changed:
            return
        status = str(changed['Status']).lower()
        with self._lock:
            if self._mp_status.get(path) == status:
                return
            self._mp_status[path] = status
        _publish({'status': status, 'source': _mac_from_path(path)})

    def _handle_transport_props(self, path, changed):
        # AVRCP 绝对音量(0-127)变化 -> 推送 volume 事件
        if 'Volume' not in changed:
            return
        try:
            vol = int(changed['Volume'])
        except Exception:
            return
        with self._lock:
            if self._transport_volume.get(path) == vol:
                return
            self._transport_volume[path] = vol
        _publish({'action': 'volume', 'value': vol, 'source': _mac_from_path(path)})

    def _on_interfaces_added(self, path, interfaces):
        try:
            if IFACE_MEDIA_PLAYER in interfaces:
                logger.info(f"AVRCP 发现新 MediaPlayer1(对端): {path}")
                props = interfaces.get(IFACE_MEDIA_PLAYER, {})
                if 'Status' in props:
                    self._handle_mediaplayer_props(str(path), {'Status': props['Status']})
        except Exception as e:
            logger.warning(f"AVRCP InterfacesAdded 回调异常(忽略): {e}")

    def _on_interfaces_removed(self, path, interfaces):
        try:
            spath = str(path)
            with self._lock:
                if IFACE_MEDIA_PLAYER in interfaces:
                    self._mp_status.pop(spath, None)
                if IFACE_MEDIA_TRANSPORT in interfaces:
                    self._transport_volume.pop(spath, None)
            if IFACE_MEDIA_PLAYER in interfaces:
                logger.info(f"AVRCP MediaPlayer1 已移除(对端断开): {path}")
        except Exception as e:
            logger.warning(f"AVRCP InterfacesRemoved 回调异常(忽略): {e}")

    def _scan_existing_media_players(self):
        # 启动时扫描已存在的对端 MediaPlayer1 对象，初始化状态快照
        try:
            from bluetooth_manager import _get_managed_objects
            for path, ifaces in _get_managed_objects().items():
                if IFACE_MEDIA_PLAYER in ifaces:
                    props = ifaces.get(IFACE_MEDIA_PLAYER, {})
                    if 'Status' in props:
                        self._mp_status[str(path)] = str(props['Status']).lower()
                    logger.info(f"AVRCP 初始发现 MediaPlayer1(对端): {path}")
        except Exception as e:
            logger.debug(f"AVRCP 扫描已存在 MediaPlayer1 失败(忽略): {e}")

    # ---------- 反控对端(可选)：供 HTTP 层调用 ----------
    def send_command_to_peer(self, command):
        """向对端 MediaPlayer1 发送控制命令(Play/Pause/Next/Previous)。

        遍历所有已发现的对端 MediaPlayer1 对象并调用其方法。返回是否至少成功一个。
        """
        method = {'play': 'Play', 'pause': 'Pause', 'playpause': 'Pause',
                  'stop': 'Stop', 'next': 'Next', 'previous': 'Previous'}.get(
                      str(command).lower())
        if not method:
            logger.warning(f"AVRCP send_command_to_peer 不支持的命令: {command}")
            return False
        if self._bus is None:
            return False
        ok = False
        try:
            from bluetooth_manager import _get_managed_objects
            for path, ifaces in _get_managed_objects().items():
                if IFACE_MEDIA_PLAYER not in ifaces:
                    continue
                try:
                    player = dbus.Interface(
                        self._bus.get_object(BLUEZ_SERVICE, path), IFACE_MEDIA_PLAYER)
                    getattr(player, method)()
                    ok = True
                    logger.debug(f"AVRCP 已向对端 {path} 发送命令 {method}")
                except Exception as e:
                    logger.debug(f"AVRCP 向对端 {path} 发送 {method} 失败(忽略): {e}")
        except Exception as e:
            logger.warning(f"AVRCP send_command_to_peer 异常(忽略): {e}")
        return ok


# 全局单例，随 app 生命周期启动/停止
avrcp_bridge = AVRCPBridge()


def start():
    avrcp_bridge.start()


def stop():
    avrcp_bridge.stop()