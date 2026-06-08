import time
import re
import shlex
import threading
import logging

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop

from utils import run_command, pw_dump, find_pw_node
import config
import platform_paths
from auto_reconnect import AutoReconnectManager
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError, MediaHubError, PairingNeedPinError, ProfileUnavailableError

logger = logging.getLogger('MediaHub')

# GLib 主循环（后台线程运行，用于派发 D-Bus Agent 方法调用）
_glib_loop = None
_glib_loop_thread = None


def _ensure_glib_loop():
    """确保 GLib 主循环在后台线程中运行，D-Bus Agent 方法调用依赖它"""
    global _glib_loop, _glib_loop_thread
    if _glib_loop is not None:
        return
    try:
        from gi.repository import GLib
        _glib_loop = GLib.MainLoop()
        _glib_loop_thread = threading.Thread(target=_glib_loop.run, daemon=True, name='dbus-glib')
        _glib_loop_thread.start()
        logger.debug("GLib 主循环已启动，D-Bus Agent 方法调用可正常派发")
    except ImportError:
        logger.warning("无法导入 GLib，蓝牙配对可能无法正常工作，请安装 python3-gi")

from wp_config_manager import WPConfigManager

BLUEZ_SERVICE = 'org.bluez'
BLUEZ_IFACE_ADAPTER = 'org.bluez.Adapter1'
BLUEZ_IFACE_DEVICE = 'org.bluez.Device1'
BLUEZ_IFACE_AGENT_MANAGER = 'org.bluez.AgentManager1'
BLUEZ_IFACE_AGENT = 'org.bluez.Agent1'
DBUS_PROP_IFACE = 'org.freedesktop.DBus.Properties'
BLUEZ_IFACE_BATTERY = 'org.bluez.Battery1'

_DEVICE_TYPE_UUIDS = {
    # 音频输出
    '110B': 'audio-headphones', '110A': 'audio-headphones',
    '110C': 'audio-headphones', '110D': 'audio-headphones', '110E': 'audio-headphones',
    '1203': 'audio-speakers',
    # 音频输入输出（耳机/免提）
    '1108': 'audio-headset', '111E': 'audio-headset', '111F': 'audio-headset',
    '1112': 'audio-headset',
    # 音视频
    '1116': 'audio-video',
    # 输入设备
    '1124': 'input-keyboard', '1125': 'input-keyboard', '1126': 'input-mouse',
    '1120': 'input-keyboard', '1122': 'input-mouse', '1123': 'input-joystick',
    # 电话
    '1104': 'phone', '1105': 'phone', '1111': 'phone',
    # LE Audio (BAP/CAP)
    '184E': 'le-audio', '184F': 'le-audio', '1850': 'le-audio',
}

_BT_APPEARANCE = {
    # 音频设备
    0x0400: '通用音频', 0x0401: '可穿戴耳机', 0x0402: '手持耳机',
    0x0403: '耳机', 0x0404: '便携音箱', 0x0405: '书架音箱',
    0x0406: '广播音箱', 0x0407: 'Soundbar', 0x0408: '有源音箱',
    0x0409: '智能音箱', 0x040A: '扩展低音',
    0x040B: 'Soundbar 前置', 0x040C: 'Soundbar 后置',
    0x0410: '助听器-左耳', 0x0411: '助听器-右耳', 0x0412: '助听器-双耳',
    # 遥控/游戏
    0x0340: '通用遥控', 0x0341: '遥控', 0x0342: '游戏手柄',
    0x0343: '电视遥控', 0x0344: '传感器遥控',
    # 键鼠
    0x0180: '通用键盘', 0x0181: '键盘', 0x0182: '小键盘',
    0x0190: '通用鼠标', 0x0191: '鼠标', 0x0192: '轨迹球',
    # 穿戴
    0x03C0: '通用手表', 0x03C1: '手表', 0x03C2: '怀表',
    0x03C3: '智能手环', 0x03C4: '智能戒指',
    # 显示/电话
    0x07C0: '通用显示器', 0x07C1: '显示器',
    0x0700: '通用电话', 0x0701: '手机', 0x0702: '无绳电话',
    0x0703: '智能手机',
    # 医疗
    0x0540: '心率传感器', 0x0580: '血压计', 0x0900: '通用标签',
    # LE Audio 外观 (BT 5.2+)
    0x0941: 'LE Audio 耳机', 0x0942: 'LE Audio 音箱',
    0x0943: 'LE Audio 助听器',
}

_BT_MANUFACTURER = {
    0x0001: 'Ericsson', 0x0002: 'Nokia', 0x0003: 'Intel', 0x0004: 'IBM',
    0x0005: 'Toshiba', 0x0006: '3Com', 0x0007: 'Microsoft', 0x0008: 'Lucent',
    0x0009: 'Motorola', 0x000A: 'Infineon', 0x000B: 'Cambridge Silicon Radio',
    0x000D: 'Texas Instruments', 0x000E: 'Ceva', 0x000F: 'Broadcom',
    0x0010: 'RTL', 0x0011: 'Widcomm', 0x0012: 'Zeevo', 0x0013: 'Atmel',
    0x0015: 'Qualcomm', 0x0017: 'Marvell', 0x0018: 'Integrated System Solution',
    0x0019: 'SiRF', 0x001A: 'Tzero', 0x001B: 'Samsung', 0x001D: 'Apple',
    0x001E: 'Staccato', 0x001F: 'Option N.V.', 0x0020: 'Maxim',
    0x0023: 'Nordic Semiconductor', 0x0024: 'Panasonic', 0x0025: 'Gennum',
    0x003D: 'Realtek', 0x003F: 'MediaTek', 0x0059: 'Cypress',
    0x006B: 'Huawei', 0x0075: 'Samsung', 0x0087: 'Actions',
    0x00A9: 'Zhuhai Jieli', 0x011B: 'Bose', 0x012D: 'Sony',
    0x0145: 'JBL', 0x0198: 'Harman', 0x01A7: 'Logitech',
    0x023D: 'Xiaomi', 0x02C7: 'Sennheiser', 0x05F1: 'Jabra',
    0x0817: 'Anker', 0x09D6: 'Edifier',
    # 常见中国品牌
    0x0159: 'Razer', 0x050F: 'Vivo', 0x05DC: 'Oppo',
    0x071F: 'Realme', 0x09FF: 'OnePlus', 0x0B76: 'Soundcore',
    0x0D37: 'Baseus', 0x0B05: 'ASUS', 0x0489: 'Foxconn',
    0x0078: 'Zhongxing', 0x0291: 'Lenovo', 0x0411: 'Hisense',
    0x00E5: 'TCL', 0x0B3E: 'QCY', 0x0035: 'Silicon Labs',
    0x0259: 'Amlogic', 0x0A6C: 'Rokid', 0x0C0E: 'Honor',
}

_bus = None
_bus_lock = threading.Lock()
_auto_reconnect_manager = None
_reconnect_lock = threading.Lock()
_connecting_devices_lock = {}
_connecting_devices_lock_lock = threading.Lock()
_wpc = WPConfigManager()


# 提取蓝牙 UUID 短码
def _extract_bt_uuid_short(uuid_str):
    s = str(uuid_str).upper().replace('-', '')
    if len(s) == 32 and s[8:] == '00001000800000805F9B34FB':
        return s[4:8]
    if len(s) >= 8:
        return s.lstrip('0')[:4] or '0'
    return s


# Appearance 值中属于音频设备的集合
_AUDIO_APPEARANCES = {
    0x0400, 0x0401, 0x0402, 0x0403, 0x0404, 0x0405, 0x0406, 0x0407,
    0x0408, 0x0409, 0x040A, 0x040B, 0x040C,
    0x0410, 0x0411, 0x0412,
    0x0941, 0x0942, 0x0943,
}

# UUID 短码中属于音频设备的集合
_AUDIO_UUID_SHORTS = {
    '1108', '110A', '110B', '110C', '110D', '110E',
    '1112', '1116', '111E', '111F', '1203',
    '184E', '184F', '1850',
}


# 根据 UUID 推断设备类型
def _guess_type_from_uuids(uuids):
    priority = ['input-keyboard', 'input-mouse', 'audio-headset', 'audio-headphones',
                'audio-speakers', 'audio-video', 'le-audio', 'phone']
    matched = {_DEVICE_TYPE_UUIDS.get(_extract_bt_uuid_short(u)) for u in uuids}
    for t in priority:
        if t in matched:
            return t
    return None


# 判断蓝牙是否手动关闭
def _is_manual_power_off():
    return not config.get_bt_power_enabled()


# 获取 D-Bus 系统总线
def _get_system_bus():
    global _bus
    with _bus_lock:
        if _bus is None:
            DBusGMainLoop(set_as_default=True)
            _bus = dbus.SystemBus()
            _ensure_glib_loop()
        return _bus


# 获取 BlueZ D-Bus 对象
def _get_object(path):
    bus = _get_system_bus()
    if bus is None:
        raise dbus.exceptions.DBusException("无法连接到系统D-Bus")
    return bus.get_object(BLUEZ_SERVICE, path)


# 获取接口全部属性
def _get_properties(interface, path):
    return _get_object(path).GetAll(interface, dbus_interface=DBUS_PROP_IFACE)


# 获取接口单个属性
def _get_property(interface, path, prop_name):
    return _get_object(path).Get(interface, prop_name, dbus_interface=DBUS_PROP_IFACE)


# 设置接口属性
def _set_property(interface, path, prop_name, value):
    return _get_object(path).Set(interface, prop_name, value, dbus_interface=DBUS_PROP_IFACE)


# 获取 BlueZ 管理对象
def _get_managed_objects():
    bus = _get_system_bus()
    if bus is None:
        return {}
    return _get_object('/').GetManagedObjects(dbus_interface='org.freedesktop.DBus.ObjectManager')


# 查找适配器 D-Bus 路径
def _find_adapter_path():
    try:
        for path, ifaces in _get_managed_objects().items():
            if BLUEZ_IFACE_ADAPTER in ifaces:
                return path
    except dbus.exceptions.DBusException as e:
        logger.debug(f"查找适配器失败: {e}")
    return None


# 按名称查找适配器路径
def _find_adapter_path_for_controller(ctrl_name):
    try:
        for path, ifaces in _get_managed_objects().items():
            if BLUEZ_IFACE_ADAPTER in ifaces and path.endswith(ctrl_name):
                return path
    except dbus.exceptions.DBusException:
        pass
    return None


# 查找所有适配器路径
def _find_all_adapter_paths():
    paths = []
    try:
        for path, ifaces in _get_managed_objects().items():
            if BLUEZ_IFACE_ADAPTER in ifaces:
                paths.append(path)
    except dbus.exceptions.DBusException as e:
        logger.debug(f"查找适配器失败: {e}")
    return paths


# 按 MAC 查找设备路径
def _find_device_path(mac):
    dev_name = 'dev_' + mac.replace(':', '_').upper()
    try:
        for path, ifaces in _get_managed_objects().items():
            if BLUEZ_IFACE_DEVICE in ifaces and path.endswith('/' + dev_name):
                return path
    except dbus.exceptions.DBusException as e:
        logger.debug(f"查找设备路径失败: {e}")
    return None


# 从路径提取 MAC 地址
def _mac_from_path(path):
    last = path.split('/')[-1]
    if last.startswith('dev_'):
        last = last[4:]
    return last.replace('_', ':').upper()


# 确保蓝牙守护进程运行
def _ensure_bluetoothd():
    status = run_command(f"{platform_paths.CMD_SYSTEMCTL} is-active bluetooth 2>/dev/null")
    if "active" not in status["stdout"]:
        run_command(f"{platform_paths.CMD_SYSTEMCTL} start bluetooth 2>/dev/null")
        time.sleep(1)
    return True


# 适配器上电
def _power_on_adapter():
    adapter = _find_adapter_path()
    if not adapter:
        return False
    try:
        _set_property(BLUEZ_IFACE_ADAPTER, adapter, 'Powered', dbus.Boolean(True))
        time.sleep(0.5)
        return bool(_get_property(BLUEZ_IFACE_ADAPTER, adapter, 'Powered'))
    except dbus.exceptions.DBusException as e:
        logger.warning(f"适配器上电失败: {e}")
        return False


# 获取重连管理器单例
def _get_reconnect_manager():
    global _auto_reconnect_manager
    with _reconnect_lock:
        if _auto_reconnect_manager is None:
            import audio_manager
            _auto_reconnect_manager = AutoReconnectManager(
                bus=_get_system_bus(),
                activate_sink_callback=audio_manager.activate_bluez_sink
            )
            _auto_reconnect_manager.start()
        return _auto_reconnect_manager


# 启用/禁用自动重连
def set_reconnect_enabled(enabled):
    _get_reconnect_manager().set_enabled(enabled)


# 获取自动重连状态
def get_reconnect_status():
    try:
        return _get_reconnect_manager().get_status()
    except Exception:
        return {'monitoring': False, 'reconnecting_devices': [], 'manual_disconnects': []}


# 蓝牙 Agent 基类
class _BaseBluezAgent(dbus.service.Object):
    # 初始化 Agent
    def __init__(self, bus, path):
        dbus.service.Object.__init__(self, bus, path)

    # Agent 释放回调
    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='', out_signature='')
    def Release(self):
        logger.debug("Agent Release 被调用")

    # 显示 PIN 码回调
    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='os', out_signature='')
    def DisplayPinCode(self, device, pin_code):
        logger.debug(f"Agent DisplayPinCode: device={device}, pin={pin_code}")

    # 显示 Passkey 回调
    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='ouq', out_signature='')
    def DisplayPasskey(self, device, passkey, entered):
        logger.debug(f"Agent DisplayPasskey: device={device}, passkey={passkey}, entered={entered}")

    # 授权服务回调（自动允许，确保 A2DP/HFP 等配对后服务可用）
    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='os', out_signature='')
    def AuthorizeService(self, device, uuid):
        logger.debug(f"Agent AuthorizeService: device={device}, uuid={uuid} (自动授权)")

    # 取消配对回调
    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='', out_signature='')
    def Cancel(self):
        logger.debug("Agent Cancel 被调用")


# 持久 Agent 自动接受配对，支持临时 PIN 码
class _PersistentAgent(_BaseBluezAgent):
    def __init__(self, bus, path):
        super().__init__(bus, path)
        self._pairing_pin = None  # 临时 PIN，配对时设置

    # 设置当前配对使用的 PIN 码（线程安全，配对前调用）
    def set_pairing_pin(self, pin):
        self._pairing_pin = pin

    # 清除临时 PIN
    def clear_pairing_pin(self):
        self._pairing_pin = None

    # 请求 PIN 码：优先使用用户提供的 PIN，否则返回默认 0000
    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='o', out_signature='s')
    def RequestPinCode(self, device):
        pin = self._pairing_pin or '0000'
        logger.debug(f"持久Agent RequestPinCode: device={device}, pin={'***' if self._pairing_pin else '0000'}")
        return pin

    # 请求数字 Passkey：优先使用用户 PIN，否则返回 0
    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='o', out_signature='u')
    def RequestPasskey(self, device):
        key = int(self._pairing_pin) if self._pairing_pin and self._pairing_pin.isdigit() else 0
        logger.debug(f"持久Agent RequestPasskey: device={device}, key={'***' if self._pairing_pin else '0'}")
        return dbus.UInt32(key)

    # 自动确认 SSP 配对
    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='ou', out_signature='')
    def RequestConfirmation(self, device, passkey):
        logger.debug(f"持久Agent RequestConfirmation: device={device}, passkey={passkey} (自动确认)")


_agent_manager = None
_agent_lock = threading.Lock()
_agent_registered = False


# 注册持久蓝牙 Agent（确保 GLib 主循环运行后再注册）
def ensure_agent():
    global _agent_manager, _agent_registered
    with _agent_lock:
        if _agent_registered and _agent_manager is not None:
            return True
        try:
            _ensure_glib_loop()
            bus = _get_system_bus()
            if bus is None:
                return False
            # 先清理旧的 D-Bus 对象（防止 AlreadyExists）
            if _agent_manager is not None:
                try:
                    _agent_manager.remove_from_connection()
                except Exception:
                    pass
                _agent_manager = None
            agent_obj = _PersistentAgent(bus, '/mediahub/agent')
            agent_mgr = dbus.Interface(
                bus.get_object(BLUEZ_SERVICE, '/org/bluez'),
                BLUEZ_IFACE_AGENT_MANAGER
            )
            agent_mgr.RegisterAgent('/mediahub/agent', 'KeyboardDisplay')
            agent_mgr.RequestDefaultAgent('/mediahub/agent')
            _agent_manager = agent_obj
            _agent_registered = True
            logger.info("持久蓝牙 Agent 已注册，可处理入站连接")
            return True
        except dbus.exceptions.DBusException as e:
            logger.warning(f"注册持久 Agent 失败: {e}")
            # 尝试清理残留状态
            if _agent_manager is not None:
                try:
                    _agent_manager.remove_from_connection()
                except Exception:
                    pass
                _agent_manager = None
            return False


# 注销持久蓝牙 Agent
def release_agent():
    global _agent_manager, _agent_registered
    with _agent_lock:
        if _agent_manager is None:
            return
        try:
            bus = _get_system_bus()
            agent_mgr = dbus.Interface(
                bus.get_object(BLUEZ_SERVICE, '/org/bluez'),
                BLUEZ_IFACE_AGENT_MANAGER
            )
            agent_mgr.UnregisterAgent('/mediahub/agent')
        except dbus.exceptions.DBusException:
            pass
        try:
            _agent_manager.remove_from_connection()
        except Exception:
            pass
        _agent_manager = None
        _agent_registered = False
        logger.info("持久蓝牙 Agent 已注销")


# 通过 dbus-send 调用 Pair()，持久 Agent 处理认证回调
def _dbus_pair_device(mac, pin=None, timeout=30):
    """使用 dbus-send 调用 org.bluez.Device1.Pair()，由持久 Agent 处理认证

    关键设计：
    - 持久 Agent 始终保持注册为默认 Agent，处理所有认证回调
    - 使用 dbus-send（独立进程）调用 Pair()，避免 GLib 主循环消息竞争
    - dbus-send 不注册自己的 agent，不会覆盖持久 Agent
    - PIN 码通过持久 Agent 的 set_pairing_pin() 传递
    """
    # 确保 Agent 已注册且为默认
    if not ensure_agent():
        raise CommandError('蓝牙 Agent 未注册，无法配对')

    device_path = _find_device_path(mac)
    if not device_path:
        raise DeviceNotFoundError(f'设备 {mac} 未找到，请先扫描')

    # 设置临时 PIN 码（Agent 回调会读取）
    with _agent_lock:
        if _agent_manager is not None:
            _agent_manager.set_pairing_pin(pin)
        else:
            raise CommandError('蓝牙 Agent 不可用')

    try:
        # 使用 dbus-send 调用 Pair()，独立进程不影响 GLib 主循环
        # dbus-send 不会注册自己的 agent，持久 Agent 始终处理认证回调
        cmd = f"dbus-send --system --print-reply --dest={BLUEZ_SERVICE} {shlex.quote(device_path)} {BLUEZ_IFACE_DEVICE}.Pair"
        logger.debug(f"执行配对命令: {cmd}")
        result = run_command(cmd, timeout=timeout)
        output = result.get('stdout', '') + result.get('stderr', '')
        logger.debug(f"dbus-send pair 输出: {output[:500]}")

        # dbus-send 成功返回表示配对成功
        if result.get('success', False) or 'method return' in output:
            # 配对成功，设置信任
            try:
                _set_property(BLUEZ_IFACE_DEVICE, device_path, 'Trusted', dbus.Boolean(True))
            except dbus.exceptions.DBusException:
                pass
            alias = mac
            try:
                alias = str(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias')) or mac
            except dbus.exceptions.DBusException:
                pass
            return {
                'data': f'设备 {alias} 配对成功',
                'output': '',
                'device_name': alias
            }

        # 检查 dbus-send 错误输出中的 D-Bus 错误
        error_name = ''
        if 'Error' in output:
            # 提取 D-Bus 错误名，如 "org.bluez.Error.AlreadyExists"
            import re as _re
            m = _re.search(r'Error\.(\w+)', output)
            if m:
                error_name = m.group(1)

        # 已配对
        if 'AlreadyExists' in output or error_name == 'AlreadyExists':
            alias = mac
            try:
                alias = str(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias')) or mac
            except dbus.exceptions.DBusException:
                pass
            return {
                'data': f'设备 {alias} 已配对',
                'output': '',
                'device_name': alias
            }

        # 认证失败 - 可能需要 PIN
        if 'AuthenticationFailed' in output or error_name == 'AuthenticationFailed':
            alias = mac
            try:
                alias = str(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias')) or mac
            except dbus.exceptions.DBusException:
                pass
            if not pin:
                raise PairingNeedPinError('需要输入PIN码', device_name=alias)
            raise CommandError('配对验证失败，PIN码可能不正确')

        # 配对进行中
        if 'InProgress' in output or error_name == 'InProgress':
            raise CommandError('配对正在进行中，请稍后重试')

        # 其他错误 - 先检查设备是否实际已配对
        try:
            actually_paired = bool(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Paired'))
            if actually_paired:
                alias = mac
                try:
                    alias = str(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias')) or mac
                except dbus.exceptions.DBusException:
                    pass
                return {
                    'data': f'设备 {alias} 配对成功',
                    'output': '',
                    'device_name': alias
                }
        except dbus.exceptions.DBusException:
            pass

        error_msg = _translate_pairing_error(output)
        raise CommandError(error_msg)

    except (PairingNeedPinError, CommandError):
        raise
    except Exception as e:
        logger.error(f"配对异常: {e}")
        raise CommandError(f'配对失败: {str(e)[:200]}')

    finally:
        # 清除临时 PIN
        with _agent_lock:
            if _agent_manager is not None:
                _agent_manager.clear_pairing_pin()


# 快速扫描使设备出现在 BlueZ managed objects 中
def _quick_discover_device(mac):
    adapters = _find_all_adapter_paths()
    if not adapters:
        return False
    try:
        adapter = dbus.Interface(_get_object(adapters[0]), BLUEZ_IFACE_ADAPTER)
        adapter.StartDiscovery()
        # 等待设备出现（最多8秒）
        for _ in range(16):
            time.sleep(0.5)
            if _find_device_path(mac):
                try:
                    adapter.StopDiscovery()
                except dbus.exceptions.DBusException:
                    pass
                return True
        adapter.StopDiscovery()
    except dbus.exceptions.DBusException as e:
        logger.debug(f"快速扫描失败: {e}")
    return False


def _connect_device_interactive(mac):
    # 设备未发现时先快速扫描
    if not _find_device_path(mac):
        logger.info(f"设备 {mac} 尚未发现，自动快速扫描...")
        found = _quick_discover_device(mac)
        if not found:
            raise DeviceNotFoundError(f'设备 {mac} 未找到，请先扫描')

    # 连接不需要自定义 Agent，使用持久 Agent 即可
    try:
        bus = _get_system_bus()
        device_path = _find_device_path(mac)
        if not device_path:
            raise DeviceNotFoundError(f'设备 {mac} 未找到')
        device = dbus.Interface(bus.get_object(BLUEZ_SERVICE, device_path), BLUEZ_IFACE_DEVICE)
        device.Connect()

        # 等待连接完成
        conn_start = time.time()
        while time.time() - conn_start < 20:
            time.sleep(0.5)
            try:
                connected = _get_property(BLUEZ_IFACE_DEVICE, device_path, 'Connected')
                if connected:
                    alias = mac
                    try:
                        alias = str(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias'))
                    except Exception:
                        pass
                    return {'data': f'设备 {alias} 连接成功', 'output': '', 'device_name': alias}
            except dbus.exceptions.DBusException:
                pass

        raise CommandError('连接超时')

    except dbus.exceptions.DBusException as e:
        error_msg = str(e)
        # 已连接
        if 'already' in error_msg.lower() and 'connected' in error_msg.lower():
            alias = mac
            try:
                dp = _find_device_path(mac)
                if dp:
                    alias = str(_get_property(BLUEZ_IFACE_DEVICE, dp, 'Alias'))
            except Exception:
                pass
            return {'data': f'设备 {alias} 已连接', 'output': '', 'device_name': alias}
        # profile 不可用
        if 'profile-unavailable' in error_msg or 'br-connection-profile' in error_msg:
            raise ProfileUnavailableError('蓝牙音频 profile 不可用，请检查 WirePlumber 和 libspa-0.2-bluetooth', device_name=mac)
        raise CommandError(_translate_connection_error(error_msg))


# 交互式配对设备（使用 D-Bus Pair()，不再释放/重建 Agent）
def _pair_device_interactive(mac, pin=None):
    return _dbus_pair_device(mac, pin=pin)


# 获取所有蓝牙控制器
def get_all_controllers():
    controllers = []
    try:
        for path, ifaces in _get_managed_objects().items():
            if BLUEZ_IFACE_ADAPTER in ifaces:
                props = ifaces[BLUEZ_IFACE_ADAPTER]
                controllers.append({"name": path.split('/')[-1], "mac": str(props.get('Address', ''))})
    except dbus.exceptions.DBusException as e:
        logger.debug(f"D-Bus 获取控制器失败: {e}")
    return controllers


# 获取控制器详细信息
def get_controller_details(controller_name):
    details = {
        "name": controller_name, "status": "DOWN", "mac": "", "type": "",
        "features": "", "packet_types": "", "link_policy": "", "link_mode": "",
        "hci_version": "", "manufacturer": "", "bus": "", "powered": False
    }

    # 解析 hciconfig
    hci_result = run_command(f"{platform_paths.CMD_HCICONFIG} -a {shlex.quote(controller_name)} 2>/dev/null")
    if hci_result['stdout']:
        _parse_hciconfig(hci_result['stdout'], details)

    # D-Bus 属性
    adapter_path = _find_adapter_path_for_controller(controller_name)
    if adapter_path:
        try:
            props = _get_properties(BLUEZ_IFACE_ADAPTER, adapter_path)
            if not details['mac']:
                details["mac"] = str(props.get('Address', ''))
            details["powered"] = bool(props.get('Powered', False))
            details["discoverable"] = bool(props.get('Discoverable', False))
            details["pairable"] = bool(props.get('Pairable', False))
            details["discovering"] = bool(props.get('Discovering', False))
            details["alias"] = str(props.get('Alias', '')) or str(props.get('Name', ''))
            if not details['type']:
                supported_tech = props.get('SupportedTechnologies')
                if isinstance(supported_tech, list):
                    tech_names = {'br-edr': 'BR/EDR', 'le': 'LE'}
                    types = [tech_names.get(t.lower(), t) for t in supported_tech]
                    details['type'] = ' + '.join(types)
                elif isinstance(supported_tech, str):
                    details['type'] = supported_tech
                if not details['type']:
                    uuids = props.get('UUIDs', [])
                    has_le = any('00001800' in u or '00001801' in u or 'le' in u.lower() for u in uuids)
                    details['type'] = ('BR/EDR + LE' if has_le else 'BR/EDR') if uuids else ''
            if props.get('Class'):
                details["device_class"] = '0x{:06X}'.format(int(props['Class']))
            details["status"] = "UP" if details["powered"] else "DOWN"
        except dbus.exceptions.DBusException as e:
            logger.debug(f"获取适配器属性失败: {e}")

    # sysfs 回退
    if not details['type']:
        sysfs_type = run_command(f"cat {platform_paths.SYSFS_BLUETOOTH}/{controller_name}/type 2>/dev/null")
        if sysfs_type['stdout']:
            type_map = {'1': 'BR/EDR', '2': 'AMP', '3': 'LE'}
            details['type'] = type_map.get(sysfs_type['stdout'].strip(), sysfs_type['stdout'].strip())

    # hcitool 回退
    if not details['hci_version'] or not details['manufacturer']:
        hci_info = run_command(f"{platform_paths.CMD_HCITOOL} info {shlex.quote(controller_name)} 2>/dev/null")
        if hci_info['stdout']:
            _parse_hcitool_info(hci_info['stdout'], details)

    return details


HCI_VERSION_NAMES = {
    '0': 'Bluetooth 1.0B', '1': 'Bluetooth 1.1', '2': 'Bluetooth 1.2',
    '3': 'Bluetooth 2.0 + EDR', '4': 'Bluetooth 2.1 + EDR', '5': 'Bluetooth 3.0 + HS',
    '6': 'Bluetooth 4.0', '7': 'Bluetooth 4.1', '8': 'Bluetooth 4.2',
    '9': 'Bluetooth 5.0', '0xa': 'Bluetooth 5.1', '0xb': 'Bluetooth 5.2',
    '0xc': 'Bluetooth 5.3', '0xd': 'Bluetooth 5.4',
    '5.1': 'Bluetooth 5.1', '5.2': 'Bluetooth 5.2', '5.3': 'Bluetooth 5.3', '5.4': 'Bluetooth 5.4',
}


# 解析 hciconfig 输出
def _parse_hciconfig(output, details):
    for line in output.split('\n'):
        line = line.strip()
        m = re.search(r'Bus:\s*(\S+)', line)
        if m:
            details['bus'] = m.group(1)
            continue
        m = re.search(r'Type:\s*(\S+)', line)
        if m:
            details['type'] = m.group(1)
            continue
        m = re.search(r'BD Address:\s*([0-9A-Fa-f:]+)', line)
        if m:
            details['mac'] = m.group(1)
            continue
        m = re.match(r'Features( page \d+)?:\s+(0x[0-9a-fA-F\s]+)', line)
        if m:
            parts = line.split('Features')[1].strip()
            details['features'] = (details['features'] + ' | ' + parts) if details['features'] else parts
            continue
        m = re.search(r'Packet type:\s*(.+)', line)
        if m:
            details['packet_types'] = m.group(1).strip()
            continue
        m = re.search(r'Link policy:\s*(.+)', line)
        if m:
            details['link_policy'] = m.group(1).strip()
            continue
        m = re.search(r'Link mode:\s*(.+)', line)
        if m:
            details['link_mode'] = m.group(1).strip()
            continue
        m = re.search(r'HCI Version:\s*([\d.]+)\s*\(([^)]+)\)', line)
        if m:
            details['hci_version'] = f"v{m.group(1)} ({HCI_VERSION_NAMES.get(m.group(1), m.group(1))})"
            continue
        m = re.search(r'Revision:\s*(\S+)', line)
        if m and not details.get('hci_revision'):
            details['hci_revision'] = m.group(1)
            continue
        m = re.search(r'Manufacturer:\s*(.+?)\s*\((\d+)\)', line)
        if m:
            details['manufacturer'] = m.group(1).strip()
            details['manufacturer_id'] = m.group(2)


# 解析 hcitool info 输出
def _parse_hcitool_info(output, details):
    for line in output.split('\n'):
        line = line.strip()
        m = re.search(r'HCI Ver:\s*([\d.]+)\s*\(([^)]+)\)', line)
        if m and not details['hci_version']:
            details['hci_version'] = f"v{m.group(1)} ({HCI_VERSION_NAMES.get(m.group(1), m.group(1))})"
            continue
        m = re.search(r'Manufacturer:\s*(.+?)\s*\((\d+)\)', line)
        if m and not details['manufacturer']:
            details['manufacturer'] = m.group(1).strip()
            details['manufacturer_id'] = m.group(2)


# 检测 USB 蓝牙硬件
def check_bluetooth_hardware():
    result = run_command(f"{platform_paths.CMD_LSUSB} 2>/dev/null | grep -iE 'bluetooth|wireless|radio'")
    usb_devices = []
    if result["stdout"]:
        for line in result["stdout"].split('\n'):
            if line.strip():
                match = re.search(r'Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+([0-9a-fA-F:]+)\s+(.+)$', line)
                if match:
                    usb_devices.append({
                        "bus": match.group(1), "device": match.group(2),
                        "id": match.group(3), "name": match.group(4).strip()
                    })
    return usb_devices


# 获取蓝牙综合状态
def get_bluetooth_status():
    _ensure_bluetoothd()
    usb_devices = check_bluetooth_hardware()
    controllers = get_all_controllers()
    # 使用本地缓存避免对同一控制器重复调用 get_controller_details
    _details_cache = {}
    def _get_details(name):
        if name not in _details_cache:
            _details_cache[name] = get_controller_details(name)
        return _details_cache[name]

    controller_details = [_get_details(c["name"]) for c in controllers]

    result = run_command(f"{platform_paths.CMD_SYSTEMCTL} is-active bluetooth 2>/dev/null || echo inactive")
    service_active = "active" in result["stdout"]
    any_powered = any(c.get("powered", False) for c in controller_details)

    # 服务运行但未上电时自动上电
    if service_active and controller_details and not any_powered and not _is_manual_power_off():
        logger.info("蓝牙服务运行中但适配器未上电，自动上电...")
        _power_on_adapter()
        # 上电后清除缓存，强制重新查询
        _details_cache.clear()
        controller_details = [_get_details(c["name"]) for c in controllers]
        any_powered = any(c.get("powered", False) for c in controller_details)

    if service_active and controller_details and any_powered:
        status = "active"
    elif service_active and controller_details:
        status = "service_running"
    elif usb_devices or controller_details:
        status = "hardware_detected"
    else:
        status = "not_detected"

    return {
        "status": status, "service_active": service_active,
        "btctl_installed": bool(run_command(f"which {platform_paths.CMD_BLUETOOTHCTL} 2>/dev/null")['stdout']),
        "controllers": controller_details, "usb_devices": usb_devices
    }


# 确保控制器上电
def ensure_controller_up():
    adapter_path = _find_adapter_path()
    if adapter_path:
        try:
            _set_property(BLUEZ_IFACE_ADAPTER, adapter_path, 'Powered', dbus.Boolean(True))
            time.sleep(0.5)
        except dbus.exceptions.DBusException as e:
            logger.debug(f"适配器上电失败: {e}")
    controllers = get_all_controllers()
    return controllers[0]["name"] if controllers else "hci0"


def check_bluetooth_audio_ready():
    """检查蓝牙音频环境是否就绪（MediaEndpoint1 已注册）"""
    try:
        for path, ifaces in _get_managed_objects().items():
            if 'org.bluez.MediaEndpoint1' in ifaces:
                return True
    except dbus.exceptions.DBusException:
        pass
    return False


def ensure_wireplumber_bluez_config():
    """确保 WirePlumber 蓝牙配置存在且格式正确"""
    return _wpc.deploy_bluez_config()


# 获取已连接蓝牙设备
def check_bluetooth_connections():
    connected = []
    try:
        for path, ifaces in _get_managed_objects().items():
            if BLUEZ_IFACE_DEVICE in ifaces:
                props = ifaces[BLUEZ_IFACE_DEVICE]
                if props.get('Connected', False):
                    mac = _mac_from_path(path)
                    name = str(props.get('Alias', '') or props.get('Name', mac))
                    connected.append({"mac": mac, "name": name})
    except dbus.exceptions.DBusException as e:
        logger.debug(f"检查连接失败: {e}")
    return connected


_activating_devices = set()
_activating_devices_lock = threading.Lock()


# 蓝牙保活与音频激活
def keep_bluetooth_alive():
    if _is_manual_power_off():
        return
    _ensure_bluetoothd()
    connected = check_bluetooth_connections()
    for dev in connected:
        device_path = _find_device_path(dev['mac'])
        if device_path:
            try:
                _set_property(BLUEZ_IFACE_DEVICE, device_path, 'Trusted', dbus.Boolean(True))
            except dbus.exceptions.DBusException:
                pass
    # 检查已连接设备是否有音频 sink，没有则激活
    sink_check = run_command(f"pactl list sinks short 2>/dev/null", timeout=5)
    sink_stdout = sink_check.get('stdout') or ''
    pw_check = run_command(f"pw-dump 2>/dev/null | grep -c 'bluez'", timeout=5)
    pw_stdout = pw_check.get('stdout') or ''
    for dev in connected:
        mac_us = dev['mac'].replace(':', '_')
        if mac_us not in sink_stdout:
            if not pw_stdout or pw_stdout.strip() == '0':
                with _activating_devices_lock:
                    if dev['mac'] in _activating_devices:
                        continue
                    _activating_devices.add(dev['mac'])
                threading.Thread(target=_activate_audio, args=(dev['mac'],), daemon=True).start()


# 激活蓝牙设备的音频 sink
def _activate_audio(mac):
    try:
        _trust_and_activate_audio(mac)
    finally:
        with _activating_devices_lock:
            _activating_devices.discard(mac)


# 安装蓝牙驱动
def install_bluetooth_driver():
    pkgs = ["bluez", "bluez-tools", "libspa-0.2-bluetooth", "pipewire", "pipewire-pulse", "wireplumber"]
    missing = []
    for pkg in pkgs:
        check = run_command(f"dpkg -s {shlex.quote(pkg)} 2>/dev/null | grep -c '^Status: install ok installed'")
        if not check["stdout"] or "0" in check["stdout"]:
            missing.append(pkg)
    if missing:
        result = run_command(f"apt-get update -qq && apt-get install -y -qq {' '.join(shlex.quote(p) for p in missing)}", timeout=180)
        if not result["success"]:
            raise CommandError(result["stderr"] or "蓝牙驱动安装失败")
    run_command(f"{platform_paths.CMD_SYSTEMCTL} enable bluetooth 2>/dev/null")
    run_command(f"{platform_paths.CMD_SYSTEMCTL} start bluetooth 2>/dev/null")
    time.sleep(2)
    _ensure_bluetoothd()
    ensure_controller_up()
    _power_on_adapter()
    config.set_bt_power_enabled(True)
    return "蓝牙驱动安装成功"


# 扫描蓝牙设备
def scan_devices():
    if _is_manual_power_off():
        raise InvalidParamError("蓝牙电源已关闭，请先开启电源")

    _ensure_bluetoothd()
    adapter_paths = _find_all_adapter_paths()
    if not adapter_paths:
        return []

    # 上电所有适配器
    for adapter_path in adapter_paths:
        try:
            _set_property(BLUEZ_IFACE_ADAPTER, adapter_path, 'Powered', dbus.Boolean(True))
        except dbus.exceptions.DBusException as e:
            logger.debug(f"设置适配器属性失败: {e}")
    time.sleep(0.5)

    # 收集扫描发现的设备
    collected = []

    def on_interfaces_added(path, interfaces):
        if BLUEZ_IFACE_DEVICE in interfaces:
            props = interfaces[BLUEZ_IFACE_DEVICE]
            mac = _mac_from_path(path)
            name = str(props.get('Alias', '') or props.get('Name', mac))
            rssi = props.get('RSSI')
            if mac not in [d['mac'] for d in collected]:
                collected.append({"mac": mac, "name": name, "rssi": rssi if rssi is not None else None})

    bus = _get_system_bus()
    signal_match = bus.add_signal_receiver(
        on_interfaces_added,
        dbus_interface='org.freedesktop.DBus.ObjectManager',
        signal_name='InterfacesAdded'
    )

    try:
        # 执行扫描
        for adapter_path in adapter_paths:
            adapter = None
            try:
                adapter = dbus.Interface(_get_object(adapter_path), BLUEZ_IFACE_ADAPTER)
                adapter.StartDiscovery()
                time.sleep(8)
            except dbus.exceptions.DBusException as e:
                logger.debug(f"扫描适配器失败: {e}")
            finally:
                if adapter is not None:
                    try:
                        adapter.StopDiscovery()
                    except Exception:
                        pass
    finally:
        signal_match.remove()

    # 合并 managed objects 中的已有设备
    all_devices = list(collected)
    seen_macs = {d["mac"] for d in all_devices}
    try:
        for path, ifaces in _get_managed_objects().items():
            if BLUEZ_IFACE_DEVICE in ifaces:
                mac = _mac_from_path(path)
                if mac not in seen_macs and mac != "00:00:00:00:00:00":
                    props = ifaces[BLUEZ_IFACE_DEVICE]
                    name = str(props.get('Alias', '') or props.get('Name', mac))
                    seen_macs.add(mac)
                    all_devices.append({"mac": mac, "name": name})
    except dbus.exceptions.DBusException:
        pass

    # 补充缓存中的别名
    cached = config.get_cached_paired_devices()
    for d in all_devices:
        mac = d["mac"].upper()
        if mac in cached:
            d["alias"] = cached[mac].get("alias", "")
            if d.get("name") == "Unknown" or not d.get("name"):
                d["name"] = cached[mac].get("alias") or cached[mac].get("name", d.get("name", "Unknown"))

    config.set_last_scan(all_devices)
    return all_devices


# 丰富设备详细信息
def _enrich_device_info(mac, name=""):
    _ensure_bluetoothd()
    cached = config.get_cached_paired_devices().get(mac.upper(), {})
    device_info = {
        "mac": mac.upper(), "name": name or cached.get("alias") or cached.get("name", "Unknown"),
        "connected": False, "type": "", "paired": True, "trusted": False, "blocked": False,
        "alias": cached.get("alias", ""), "icon": "", "vendor": "", "battery": "", "is_audio": False
    }

    device_path = _find_device_path(mac)
    if not device_path:
        return device_info

    try:
        props = _get_properties(BLUEZ_IFACE_DEVICE, device_path)
        device_info["connected"] = bool(props.get('Connected', False))
        device_info["paired"] = bool(props.get('Paired', False))
        device_info["trusted"] = bool(props.get('Trusted', False))
        device_info["blocked"] = bool(props.get('Blocked', False))
        device_info["services_resolved"] = bool(props.get('ServicesResolved', False))
        if props.get('Class'):
            device_info["device_class"] = '0x{:06X}'.format(int(props['Class']))
        if props.get('RSSI') is not None:
            device_info["rssi"] = str(props['RSSI']) + " dBm"
        else:
            # 连接后 GetAll 可能不返回 RSSI，尝试单独获取
            try:
                rssi_val = _get_property(BLUEZ_IFACE_DEVICE, device_path, 'RSSI')
                if rssi_val is not None:
                    device_info["rssi"] = str(rssi_val) + " dBm"
            except dbus.exceptions.DBusException:
                pass
            # 如果 D-Bus 仍无 RSSI，对已连接设备尝试 hcitool 获取
            if 'rssi' not in device_info and device_info.get('connected'):
                try:
                    out = run_command(f"hcitool rssi {mac} 2>/dev/null", timeout=3)
                    if out and 'RSSI return value' in out:
                        import re as _re
                        m = _re.search(r'-?\d+', out.split('RSSI return value')[-1])
                        if m:
                            device_info["rssi"] = m.group() + " dBm"
                except Exception:
                    pass
        if props.get('TxPower'):
            device_info["tx_power"] = str(props['TxPower']) + " dBm"
        if props.get('Alias'):
            alias = str(props['Alias']).strip()
            if alias:
                device_info["alias"] = alias
        if props.get('Icon'):
            device_info["icon"] = str(props['Icon']).strip()
            device_info["type"] = device_info["icon"]
        if props.get('UUIDs'):
            uuids = props['UUIDs']
            if uuids:
                device_info["uuid"] = [str(u) for u in uuids]
                if not device_info.get("type"):
                    device_info["type"] = _guess_type_from_uuids(uuids)
                for u in uuids:
                    if _extract_bt_uuid_short(u) in _AUDIO_UUID_SHORTS:
                        device_info["is_audio"] = True
                        break
        if props.get('Modalias'):
            device_info["modalias"] = str(props['Modalias']).strip()
        if props.get('Name') and (not device_info.get("name") or device_info.get("name") == "Unknown"):
            device_info["name"] = str(props['Name']).strip()
        if props.get('Appearance'):
            appearance_val = int(props['Appearance'])
            device_info["appearance"] = _BT_APPEARANCE.get(appearance_val, f'0x{appearance_val:04X}')
            if appearance_val in _AUDIO_APPEARANCES:
                device_info["is_audio"] = True
                if not device_info.get("type"):
                    device_info["type"] = 'audio-headset' if appearance_val in (0x0401, 0x0402, 0x0403, 0x0410, 0x0411, 0x0412) else 'audio-speakers'
        if props.get('AddressType'):
            addr_type = str(props['AddressType']).strip()
            device_info["address_type"] = '公网' if addr_type == 'public' else '随机'
        if props.get('Adapter'):
            device_info["adapter_path"] = str(props['Adapter'])
        if props.get('ManufacturerData'):
            mfr_data = dict(props['ManufacturerData'])
            mfr_ids = list(mfr_data.keys())
            if mfr_ids:
                mfr_id = int(str(mfr_ids[0]))
                mfr_name = _BT_MANUFACTURER.get(mfr_id, f'0x{mfr_id:04X}')
                device_info["vendor"] = mfr_name
                device_info["manufacturer_id"] = f"0x{mfr_id:04X}"
        try:
            battery_props = _get_properties(BLUEZ_IFACE_BATTERY, device_path)
            if battery_props.get('Percentage') is not None:
                device_info["battery"] = str(int(battery_props['Percentage'])) + '%'
        except dbus.exceptions.DBusException:
            pass
    except dbus.exceptions.DBusException as e:
        logger.debug(f"获取设备信息失败: {e}")

    return device_info


# 获取已配对蓝牙设备
def get_paired_devices():
    _ensure_bluetoothd()
    devices = []
    seen = set()

    try:
        for path, ifaces in _get_managed_objects().items():
            if BLUEZ_IFACE_DEVICE in ifaces:
                props = ifaces[BLUEZ_IFACE_DEVICE]
                if props.get('Paired', False):
                    mac = _mac_from_path(path)
                    if mac not in seen:
                        seen.add(mac)
                        name = str(props.get('Alias', '') or props.get('Name', mac))
                        devices.append(_enrich_device_info(mac, name))
    except dbus.exceptions.DBusException as e:
        logger.debug(f"获取已配对设备失败: {e}")

    # 补充缓存中不在 D-Bus 的设备
    for mac, info in config.get_cached_paired_devices().items():
        mac = mac.upper()
        if mac not in seen:
            seen.add(mac)
            devices.append({
                "mac": mac, "name": info.get("alias") or info.get("name", mac),
                "connected": False, "type": "", "paired": True, "trusted": False,
                "blocked": False, "alias": info.get("alias", ""), "icon": "",
                "vendor": "", "battery": "", "is_audio": info.get("is_audio", False)
            })

    return devices


# 翻译配对相关的 D-Bus 错误消息为中文
def _translate_pairing_error(msg):
    translations = {
        'AlreadyExists': '设备已配对，请先删除后重试',
        'InProgress': '配对正在进行中，请稍后重试',
        'DoesNotExist': '设备未找到，请重新扫描',
        'NotReady': '蓝牙未就绪，请稍后重试',
        'Failed': '配对失败，请确认设备处于可配对模式',
        'AuthenticationFailed': '配对验证失败',
        'AuthenticationRejected': '配对被拒绝',
        'AuthenticationTimeout': '配对验证超时',
        'ConnectionAttemptFailed': '连接尝试失败',
        'NotSupported': '操作不支持',
        'InvalidArgs': '参数无效',
    }
    for key, cn in translations.items():
        if key in msg:
            return cn
    if '设备' in msg and '未找到' in msg:
        return msg
    return '配对失败，请重试'


# 翻译连接相关的 D-Bus 错误消息为中文
def _translate_connection_error(msg):
    translations = {
        'AlreadyConnected': '设备已连接',
        'InProgress': '连接正在进行中，请稍后',
        'DoesNotExist': '设备未找到，请重新扫描',
        'NotReady': '蓝牙未就绪，请稍后',
        'Failed': '连接失败，请重试',
        'NotAvailable': '蓝牙服务不可用',
        'ResourceNotAvailable': '资源不可用，请检查蓝牙服务',
        'NotSupported': '操作不支持',
        'AuthenticationFailed': '连接验证失败',
        'AuthenticationRejected': '连接被拒绝',
        'AuthenticationTimeout': '连接验证超时',
        'ConnectionAttemptFailed': '连接尝试失败',
    }
    for key, cn in translations.items():
        if key.lower().replace('_', '') in msg.lower().replace('_', '').replace(' ', ''):
            return cn
    return '连接失败，请重试'


# 翻译断开/删除相关的 D-Bus 错误消息为中文
def _translate_disconnect_error(msg):
    translations = {
        'DoesNotExist': '设备未找到',
        'NotConnected': '设备未连接',
        'Failed': '操作失败',
        'NotReady': '蓝牙未就绪',
    }
    for key, cn in translations.items():
        if key.lower().replace('_', '') in msg.lower().replace('_', '').replace(' ', ''):
            return cn
    return '操作失败，请重试'


# 配对并自动连接设备
def pair_device(mac, pin=None):
    if _is_manual_power_off():
        raise InvalidParamError("蓝牙电源已关闭，请先开启电源")
    _ensure_bluetoothd()
    if not get_all_controllers():
        raise DeviceNotFoundError("未检测到蓝牙控制器")
    ensure_controller_up()
    if not _power_on_adapter():
        raise CommandError("蓝牙控制器无法上电")
    time.sleep(0.5)

    # 获取设备名称
    device_name = mac
    device_path = _find_device_path(mac)
    if device_path:
        try:
            alias = _get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias')
            if alias:
                device_name = alias
        except dbus.exceptions.DBusException:
            pass

    # 检查设备是否已配对，已配对则直接尝试连接
    device_path = _find_device_path(mac)
    if device_path:
        try:
            already_paired = bool(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Paired'))
            already_connected = bool(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Connected'))
            logger.debug(f"设备 {mac} 配对状态: paired={already_paired}, connected={already_connected}")
            if already_paired and already_connected:
                # 已配对且已连接，直接返回
                logger.debug(f"设备 {mac} 已配对且已连接")
                device_info = _enrich_device_info(mac, device_name)
                return {
                    "data": f"设备 {device_name} 已配对并已连接",
                    "connected": True, "device_name": device_name
                }
            if already_paired:
                # 已配对但未连接，尝试连接
                logger.debug(f"设备 {mac} 已配对，尝试连接")
                connected = False
                try:
                    _connect_device_interactive(mac)
                    connected = True
                except Exception as e:
                    logger.warning(f"已配对设备连接失败: {e}")
                if connected:
                    try:
                        _set_property(BLUEZ_IFACE_DEVICE, device_path, 'Trusted', dbus.Boolean(True))
                    except dbus.exceptions.DBusException:
                        pass
                    threading.Thread(target=_trust_and_activate_audio, args=(mac,), daemon=True).start()
                    device_info = _enrich_device_info(mac, device_name)
                    return {
                        "data": f"设备 {device_name} 已连接",
                        "connected": True, "device_name": device_name
                    }
                else:
                    # 连接失败，删除旧配对记录后重新配对
                    logger.debug(f"设备 {mac} 已配对但连接失败，删除旧记录重新配对")
                    try:
                        adapter_path = _find_adapter_path()
                        if adapter_path:
                            adapter = dbus.Interface(_get_object(adapter_path), BLUEZ_IFACE_ADAPTER)
                            adapter.RemoveDevice(device_path)
                    except dbus.exceptions.DBusException:
                        # D-Bus 删除失败时回退到 bluetoothctl
                        try:
                            run_command(f"{platform_paths.CMD_BLUETOOTHCTL} remove {shlex.quote(mac)} 2>/dev/null", timeout=10)
                        except Exception:
                            pass
                    time.sleep(1)
        except dbus.exceptions.DBusException:
            pass

    # 配对前确认设备在 D-Bus 中可见，否则触发短暂扫描
    if not _find_device_path(mac):
        logger.debug(f"设备 {mac} 不在 D-Bus 中，触发短暂扫描")
        try:
            adapter_paths = _find_all_adapter_paths()
            for ap in adapter_paths:
                adapter = dbus.Interface(_get_system_bus().get_object(BLUEZ_SERVICE, ap), BLUEZ_IFACE_ADAPTER)
                adapter.StartDiscovery()
            time.sleep(3)
            for ap in adapter_paths:
                try:
                    adapter = dbus.Interface(_get_system_bus().get_object(BLUEZ_SERVICE, ap), BLUEZ_IFACE_ADAPTER)
                    adapter.StopDiscovery()
                except dbus.exceptions.DBusException:
                    pass
        except Exception as e:
            logger.debug(f"短暂扫描失败: {e}")
        if not _find_device_path(mac):
            raise DeviceNotFoundError(f"未找到设备 {device_name}，请重新扫描后重试")

    try:
        _pair_device_interactive(mac, pin=pin)
    except InvalidParamError as e:
        if not pin and isinstance(e, PairingNeedPinError):
            raise PairingNeedPinError('需要PIN码', device_name=getattr(e, 'device_name', None) or device_name)
        raise
    except CommandError as e:
        raise CommandError(e.message, command=e.command)

    # 配对成功
    device_info = _enrich_device_info(mac, device_name)
    config.add_paired_device(mac, alias=device_name, name=device_name, is_audio=device_info.get("is_audio", False))
    time.sleep(0.5)

    # 配对成功后自动连接
    connected = False
    try:
        _connect_device_interactive(mac)
        connected = True
    except Exception as e:
        logger.warning(f"配对后自动连接失败: {e}")

    if connected:
        device_path2 = _find_device_path(mac)
        if device_path2:
            try:
                _set_property(BLUEZ_IFACE_DEVICE, device_path2, 'Trusted', dbus.Boolean(True))
            except dbus.exceptions.DBusException:
                pass
        threading.Thread(target=_trust_and_activate_audio, args=(mac,), daemon=True).start()

    return {
        "data": f"设备 {device_name} 配对{'并已连接' if connected else '成功'}",
        "connected": connected, "device_name": device_name
    }


# 信任设备并激活音频 sink
def _trust_and_activate_audio(mac):
    device_path = _find_device_path(mac)
    if device_path:
        try:
            _set_property(BLUEZ_IFACE_DEVICE, device_path, 'Trusted', dbus.Boolean(True))
        except dbus.exceptions.DBusException:
            pass

    # 等待音频 sink 出现（最多10秒）
    mac_us = mac.replace(':', '_')
    for _ in range(10):
        time.sleep(1)
        check = run_command(f"pactl list sinks short 2>/dev/null | grep -c '{mac_us}'")
        if check["stdout"] and check["stdout"].strip() != "0":
            break
        pw_check = run_command(f"pw-dump 2>/dev/null | grep -c '{mac_us}'")
        if pw_check["stdout"] and pw_check["stdout"].strip() != "0":
            break

    import audio_manager
    audio_manager.activate_bluez_sink(mac)
    try:
        _get_reconnect_manager()
    except Exception:
        pass


# 连接蓝牙设备
def connect_device(mac):
    if _is_manual_power_off():
        raise InvalidParamError("蓝牙电源已关闭，请先开启电源")
    with _connecting_devices_lock_lock:
        if mac not in _connecting_devices_lock:
            _connecting_devices_lock[mac] = threading.Lock()
        lock = _connecting_devices_lock[mac]
    with lock:
        try:
            _ensure_bluetoothd()
            ensure_controller_up()
            if not _power_on_adapter():
                raise CommandError("蓝牙控制器无法上电")
            time.sleep(0.5)

            result = _connect_device_interactive(mac)

            logger.debug(f"连接结果: 成功")
            device_path = _find_device_path(mac)
            if device_path:
                try:
                    _set_property(BLUEZ_IFACE_DEVICE, device_path, 'Trusted', dbus.Boolean(True))
                except dbus.exceptions.DBusException:
                    pass
            threading.Thread(target=_trust_and_activate_audio, args=(mac,), daemon=True).start()
            return result or {'data': f'设备 {mac} 连接成功', 'device_name': mac}
        finally:
            with _connecting_devices_lock_lock:
                _connecting_devices_lock.pop(mac, None)


# 断开蓝牙设备
def disconnect_device(mac):
    device_path = _find_device_path(mac)
    if not device_path:
        raise DeviceNotFoundError(f"设备 {mac} 未找到")
    try:
        device = dbus.Interface(_get_object(device_path), BLUEZ_IFACE_DEVICE)
        device.Disconnect()
        _get_reconnect_manager().mark_manual_disconnect(mac)
        return f"设备 {mac} 已断开"
    except dbus.exceptions.DBusException as e:
        error_msg = str(e)
        if 'not connected' in error_msg.lower():
            _get_reconnect_manager().mark_manual_disconnect(mac)
            return f"设备 {mac} 已断开"
        raise CommandError(_translate_disconnect_error(error_msg) or f"断开设备 {mac} 失败")


# 删除蓝牙设备
def remove_device(mac):
    adapter_path = _find_adapter_path()
    if not adapter_path:
        raise DeviceNotFoundError("未找到蓝牙适配器")
    device_path = _find_device_path(mac)
    if device_path:
        try:
            adapter = dbus.Interface(_get_object(adapter_path), BLUEZ_IFACE_ADAPTER)
            adapter.RemoveDevice(device_path)
            config.remove_paired_device(mac)
            return f"设备 {mac} 已删除"
        except dbus.exceptions.DBusException as e:
            if 'Does Not Exist' in str(e):
                config.remove_paired_device(mac)
                return f"设备 {mac} 已删除"
            raise CommandError(_translate_disconnect_error(str(e)))
    config.remove_paired_device(mac)
    return f"设备 {mac} 已删除"


# 设置蓝牙设备别名
def set_device_alias(mac, alias):
    if not mac or not alias:
        raise InvalidParamError("MAC 地址和别名不能为空")
    alias = alias.strip()
    if not alias:
        raise InvalidParamError("别名不能为空")
    device_path = _find_device_path(mac)
    if not device_path:
        raise DeviceNotFoundError(f"设备 {mac} 未找到")
    try:
        _get_object(device_path).Set(BLUEZ_IFACE_DEVICE, 'Alias', dbus.String(alias), dbus_interface=DBUS_PROP_IFACE)
        config.add_paired_device(mac, alias=alias, name=alias, is_audio=None)
        return f"别名已设为 {alias}"
    except dbus.exceptions.DBusException as e:
        raise CommandError(str(e)[:200])


# 开关蓝牙电源
def set_power(enabled):
    _ensure_bluetoothd()
    controllers = get_all_controllers()
    if not controllers:
        raise DeviceNotFoundError("未检测到蓝牙控制器")
    success = 0
    for ctrl in controllers:
        adapter_path = _find_adapter_path_for_controller(ctrl["name"])
        if adapter_path:
            try:
                _set_property(BLUEZ_IFACE_ADAPTER, adapter_path, 'Powered', dbus.Boolean(enabled))
                success += 1
            except dbus.exceptions.DBusException:
                pass
    if success > 0:
        config.set_bt_power_enabled(enabled)
        return f"蓝牙电源已{'开启' if enabled else '关闭'} ({success}/{len(controllers)})"
    raise CommandError("电源操作失败")


# 设置蓝牙可发现
def set_discoverable(enabled):
    _ensure_bluetoothd()
    controllers = get_all_controllers()
    if not controllers:
        raise DeviceNotFoundError("未检测到蓝牙控制器")
    success = 0
    for ctrl in controllers:
        adapter_path = _find_adapter_path_for_controller(ctrl["name"])
        if adapter_path:
            try:
                _set_property(BLUEZ_IFACE_ADAPTER, adapter_path, 'Discoverable', dbus.Boolean(enabled))
                success += 1
            except dbus.exceptions.DBusException:
                pass
    if success > 0:
        return f"可发现已{'开启' if enabled else '关闭'} ({success}/{len(controllers)})"
    raise CommandError("可发现设置失败")


from bt_audio_profiles import *  # noqa: F401,F403
