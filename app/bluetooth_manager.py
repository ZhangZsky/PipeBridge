import time
import re
import shlex
import threading
import logging

import dbus
from dbus.mainloop.glib import DBusGMainLoop

from utils import run_command, pw_dump, start_pw_service, _pw_socket_exists
import config
import platform_paths
from bluetooth_extras import AutoReconnectManager
from system_manager import WPConfigManager
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError, MediaBridgeError, PairingNeedPinError, ProfileUnavailableError

logger = logging.getLogger('MediaBridge')

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
_connecting_lock = threading.Lock()  # _connecting_devices_lock 字典的访问锁
_wpc = WPConfigManager()
_pairing_lock = threading.Lock()  # 配对串行锁，防止 PIN 被并发覆盖


# 提取蓝牙 UUID 短码
def _extract_bt_uuid_short(uuid_str):
    s = str(uuid_str).upper().replace('-', '')
    # 标准 Bluetooth Base UUID: 0000XXXX-0000-1000-8000-00805F9B34FB
    # 去掉 '-' 后为 0000XXXX00001000800000805F9B34FB，s[8:] 应为 00001000800000805F9B34FB
    if len(s) == 32 and s[8:] == '00001000800000805F9B34FB':
        return s[4:8]
    if len(s) >= 8:
        # 非 Base UUID，取前 4 位十六进制作为短码
        return s[:4].lstrip('0') or '0'
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

# A2DP Source/Sink UUID — 用于判断蓝牙设备音频角色
_A2DP_SOURCE_UUID = '110A'  # 设备作为音频源（如手机发送音频到系统）→ source
_A2DP_SINK_UUID = '110B'    # 设备作为音频接收端（如耳机/音箱接收系统音频）→ sink

# Appearance 值中属于手机类的范围 — 手机主要作为 A2DP Source
_PHONE_APPEARANCE_RANGE = range(0x0700, 0x0710)


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
            # GLib 主循环在 bluetooth_agent 中管理
            from bluetooth_agent import _ensure_glib_loop
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
_mo_cache = None
_mo_cache_time = 0
_MO_CACHE_TTL = 2.0  # GetManagedObjects 缓存秒数


def _get_managed_objects():
    global _mo_cache, _mo_cache_time  # 声明全局变量，否则赋值会创建局部变量导致读取时 UnboundLocalError
    now = time.time()
    if _mo_cache is not None and (now - _mo_cache_time) < _MO_CACHE_TTL:
        return _mo_cache
    bus = _get_system_bus()
    if bus is None:
        return {}
    try:
        _mo_cache = _get_object('/').GetManagedObjects(
            dbus_interface='org.freedesktop.DBus.ObjectManager'
        )
        _mo_cache_time = now
        return _mo_cache
    except dbus.exceptions.DBusException as e:
        logger.debug(f"GetManagedObjects 失败: {e}")
        return {}


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
    dev_name = 'dev_' + mac.replace(':', '_').replace('-', '_').upper()  # 兼容短横线分隔符
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
_bt_start_fail_time = 0  # 上次启动失败的时间戳
_BT_START_RETRY_INTERVAL = 60  # 启动失败后重试间隔（秒）

def _ensure_bluetoothd():
    global _bt_start_fail_time
    status = run_command(f"{platform_paths.CMD_SYSTEMCTL} is-active bluetooth 2>/dev/null")
    if "active" not in status["stdout"]:
        # 启动失败后一段时间内不再重试，避免无硬件环境下反复尝试
        now = time.time()
        if now - _bt_start_fail_time < _BT_START_RETRY_INTERVAL:
            return False
        run_command(f"{platform_paths.CMD_SYSTEMCTL} start bluetooth 2>/dev/null")
        time.sleep(1)
        status = run_command(f"{platform_paths.CMD_SYSTEMCTL} is-active bluetooth 2>/dev/null")
        if "active" not in status["stdout"]:
            logger.error("蓝牙服务启动失败")
            _bt_start_fail_time = now
            return False
    return True


# 适配器上电
def _power_on_adapter():
    adapter = _find_adapter_path()
    if not adapter:
        logger.warning("适配器上电失败: 未找到适配器路径")
        return False
    try:
        _set_property(BLUEZ_IFACE_ADAPTER, adapter, 'Powered', dbus.Boolean(True))
        time.sleep(0.5)
        return bool(_get_property(BLUEZ_IFACE_ADAPTER, adapter, 'Powered'))
    except dbus.exceptions.DBusException as e:
        logger.warning(f"适配器上电失败: {e}")
        # 增加诊断：检查 rfkill、固件、适配器状态
        try:
            rfkill = run_command("rfkill list 2>/dev/null", timeout=3)
            if rfkill['stdout']:
                logger.warning(f"rfkill 状态: {rfkill['stdout'][:300]}")
            hciconfig = run_command(f"{platform_paths.CMD_HCICONFIG} -a 2>/dev/null", timeout=3)
            if hciconfig['stdout']:
                logger.warning(f"hciconfig 状态: {hciconfig['stdout'][:300]}")
            # 检查 dmesg 中的蓝牙固件错误
            dmesg = run_command("dmesg 2>/dev/null | grep -iE 'bluetooth|firmware|btusb|hci' | tail -10", timeout=3)
            if dmesg['stdout']:
                logger.warning(f"内核蓝牙日志: {dmesg['stdout'][:500]}")
        except Exception:
            pass
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


# 检查蓝牙音频环境是否就绪（WirePlumber 蓝牙模块已加载且有设备连接）
def check_bluetooth_audio_ready():
    # WirePlumber 0.5+ 使用 SPA bluez5 插件直接管理蓝牙音频，不通过 D-Bus MediaEndpoint1
    # 检查 PipeWire 中是否有 bluez5 设备节点（表示蓝牙音频模块已加载且有设备连接）
    # 使用 pw_dump() 享受缓存（1秒TTL，失败时10秒缓存），避免每次独立调用 pw-dump 超时
    try:
        pw_data = pw_dump()
        if pw_data:
            for obj in pw_data:
                if isinstance(obj, dict):
                    props = obj.get('info', {}).get('props', {})
                    name = props.get('node.name', '').lower()
                    factory = props.get('factory.name', '').lower()
                    if 'bluez' in name or 'bluez' in factory:
                        return True
    except Exception:
        pass
    # fallback: 检查 D-Bus MediaEndpoint1（旧版 WirePlumber 0.4.x 机制）
    try:
        for path, ifaces in _get_managed_objects().items():
            if 'org.bluez.MediaEndpoint1' in ifaces:
                return True
    except dbus.exceptions.DBusException:
        pass
    return False


# 连接前预检蓝牙音频环境，未就绪时自动修复
# 返回 (ready, detail)：ready 表示是否就绪，detail 为诊断信息
def _ensure_bluetooth_audio_ready():
    if check_bluetooth_audio_ready():
        logger.info("[音频预检] 蓝牙音频环境已就绪 (MediaEndpoint1 已注册)")
        return True, '已就绪'

    logger.warning("[音频预检] 蓝牙音频环境未就绪，尝试自动修复...")

    # 1. 先确保 PipeWire 运行且 socket 存在（WirePlumber 依赖 PipeWire socket）
    pw_check = run_command("pgrep -x pipewire 2>/dev/null")
    pw_running = bool(pw_check['success'] and pw_check['stdout'].strip())
    if not pw_running or not _pw_socket_exists():
        logger.info("[音频预检] PipeWire 未运行或 socket 缺失，启动 PipeWire...")
        start_pw_service('pipewire')
        # 等待 socket 创建（最多 5 秒）
        for _ in range(10):
            if _pw_socket_exists():
                break
            time.sleep(0.5)

    # PipeWire socket 仍未就绪，WirePlumber 启动也无意义
    if not _pw_socket_exists():
        detail = f"PipeWire运行={'是' if pw_running else '否'}, PipeWire socket缺失, MediaEndpoint1未注册"
        logger.error(f"[音频预检] PipeWire socket 未就绪: {detail}")
        return False, detail

    # 2. 检查 WirePlumber 是否运行，未运行则启动
    wp_check = run_command("pgrep -x wireplumber 2>/dev/null")
    if not (wp_check['success'] and wp_check['stdout'].strip()):
        logger.info("[音频预检] WirePlumber 未运行，尝试启动...")
        start_pw_service('wireplumber')
        time.sleep(2)

    # 3. 部署 WirePlumber 蓝牙配置（会按需重启 WirePlumber）
    try:
        ensure_wireplumber_bluez_config()
    except Exception as e:
        logger.warning(f"[音频预检] 部署 WirePlumber 蓝牙配置失败: {e}")

    # 4. 等待 MediaEndpoint1 注册（最多 8 秒）
    logger.info("[音频预检] 等待 MediaEndpoint1 注册...")
    for _ in range(16):
        if check_bluetooth_audio_ready():
            logger.info("[音频预检] 修复成功，MediaEndpoint1 已注册")
            return True, '修复后已就绪'
        time.sleep(0.5)

    # 仍未就绪，收集诊断信息
    logger.error("[音频预检] 修复失败，8秒后 MediaEndpoint1 仍未注册")
    wp_recheck = run_command("pgrep -x wireplumber 2>/dev/null")
    wp_running = bool(wp_recheck['success'] and wp_recheck['stdout'].strip())

    spa_result = run_command("dpkg -L libspa-0.2-bluetooth 2>/dev/null | grep -E '\\.so$' | head -1")
    spa_ok = bool(spa_result['success'] and spa_result['stdout'].strip())

    detail = f"PipeWire socket={'存在' if _pw_socket_exists() else '缺失'}, WirePlumber运行={'是' if wp_running else '否'}, SPA插件={'存在' if spa_ok else '缺失'}, MediaEndpoint1未注册"
    logger.error(f"[音频预检] 诊断: {detail}")
    return False, detail


# 确保 WirePlumber 蓝牙配置存在且格式正确
def ensure_wireplumber_bluez_config():
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


_activating_devices = {}  # mac -> 激活开始时间戳
_activating_devices_lock = threading.Lock()
_ACTIVATING_TIMEOUT = 30  # 激活超时秒数，超时后自动清除残留条目


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
    pw_data = pw_dump()
    for dev in connected:
        mac_us = dev['mac'].replace(':', '_')
        # 在 pw_dump 中查找是否有对应的 bluez sink 节点
        has_sink = any(
            isinstance(obj, dict) and obj.get('type') == 'PipeWire:Interface:Node'
            and 'bluez' in obj.get('info', {}).get('props', {}).get('node.name', '').lower()
            and mac_us.lower() in obj.get('info', {}).get('props', {}).get('node.name', '').lower()
            for obj in pw_data
        )
        if not has_sink:
            with _activating_devices_lock:
                if dev['mac'] in _activating_devices:
                    # 检查是否超时，超时则清除残留条目
                    if time.time() - _activating_devices[dev['mac']] > _ACTIVATING_TIMEOUT:
                        _activating_devices.pop(dev['mac'], None)
                    else:
                        continue
                _activating_devices[dev['mac']] = time.time()
            threading.Thread(target=_activate_audio, args=(dev['mac'],), daemon=True).start()


# 激活蓝牙设备的音频 sink
def _activate_audio(mac):
    try:
        _trust_and_activate_audio(mac)
    finally:
        with _activating_devices_lock:
            _activating_devices.pop(mac, None)


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
            discovery_started = False  # 跟踪是否成功启动扫描
            try:
                adapter = dbus.Interface(_get_object(adapter_path), BLUEZ_IFACE_ADAPTER)
                adapter.StartDiscovery()
                discovery_started = True
                time.sleep(8)
            except dbus.exceptions.DBusException as e:
                logger.debug(f"扫描适配器失败: {e}")
            finally:
                if adapter is not None and discovery_started:
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
                    rssi = props.get('RSSI')
                    dev_entry = {"mac": mac, "name": name}
                    if rssi is not None:
                        dev_entry["rssi"] = rssi
                    all_devices.append(dev_entry)
    except dbus.exceptions.DBusException:
        pass

    # 补充缓存中的别名和 RSSI
    cached = config.get_cached_paired_devices()
    for d in all_devices:
        mac = d["mac"].upper()
        if mac in cached:
            d["alias"] = cached[mac].get("alias", "")
            if d.get("name") == "Unknown" or not d.get("name"):
                d["name"] = cached[mac].get("alias") or cached[mac].get("name", d.get("name", "Unknown"))
            # 扫描未获取到 RSSI 时使用缓存值
            if d.get("rssi") is None:
                cached_rssi = cached[mac].get("rssi", "")
                if cached_rssi:
                    d["rssi"] = cached_rssi

    config.set_last_scan(all_devices)
    return all_devices


# 丰富设备详细信息
def _enrich_device_info(mac, name=""):
    _ensure_bluetoothd()
    cached = config.get_cached_paired_devices().get(mac.upper(), {})
    device_info = {
        "mac": mac.upper(), "name": name or cached.get("alias") or cached.get("name", "Unknown"),
        "connected": False, "type": "", "paired": True, "trusted": False, "blocked": False,
        "alias": cached.get("alias", ""), "icon": "", "vendor": "", "battery": "", "is_audio": False,
        "bt_audio_role": "sink"
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
            # GetAll 可能不返回 RSSI（未连接设备或扫描结束后），尝试单独获取
            try:
                rssi_val = _get_property(BLUEZ_IFACE_DEVICE, device_path, 'RSSI')
                if rssi_val is not None:
                    device_info["rssi"] = str(rssi_val) + " dBm"
            except dbus.exceptions.DBusException:
                pass
            # 如果 D-Bus 仍无 RSSI，对已连接设备尝试 hcitool 获取
            if 'rssi' not in device_info and device_info.get('connected'):
                try:
                    out = run_command(f"{platform_paths.CMD_HCITOOL} rssi {mac} 2>/dev/null", timeout=3)
                    out_text = out.get('stdout', '') + out.get('stderr', '')
                    if 'RSSI return value' in out_text:
                        m = re.search(r'-?\d+', out_text.split('RSSI return value')[-1])
                        if m:
                            device_info["rssi"] = m.group() + " dBm"
                except Exception:
                    pass
            # 如果仍无 RSSI，使用缓存中的值
            if 'rssi' not in device_info:
                cached_rssi = cached.get('rssi', '')
                if cached_rssi:
                    device_info["rssi"] = cached_rssi
        # 成功获取到 RSSI 时更新缓存
        if device_info.get('rssi'):
            try:
                config.update_device_rssi(mac, device_info['rssi'])
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
                uuid_shorts = {_extract_bt_uuid_short(u) for u in uuids}
                if uuid_shorts & _AUDIO_UUID_SHORTS:
                    device_info["is_audio"] = True
                # 基于 A2DP Source/Sink UUID 判断音频角色
                has_a2dp_sink = _A2DP_SINK_UUID in uuid_shorts
                has_a2dp_source = _A2DP_SOURCE_UUID in uuid_shorts
                if has_a2dp_sink:
                    device_info["bt_audio_role"] = 'sink'
                elif has_a2dp_source:
                    device_info["bt_audio_role"] = 'source'
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
            # 手机类 Appearance → 音频角色为 source
            if appearance_val in _PHONE_APPEARANCE_RANGE:
                device_info["bt_audio_role"] = 'source'
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
# 按精确度排序：长 key 优先匹配，避免 'Failed' 误匹配 'AuthenticationFailed' 等
def _translate_pairing_error(msg):
    translations = [
        ('AlreadyExists', '设备已配对，请先删除后重试'),
        ('ConnectionAttemptFailed', '连接尝试失败'),
        ('AuthenticationRejected', '配对被拒绝'),
        ('AuthenticationTimeout', '配对验证超时'),
        ('AuthenticationFailed', '配对验证失败'),
        ('InProgress', '配对正在进行中，请稍后重试'),
        ('DoesNotExist', '设备未找到，请重新扫描'),
        ('NotSupported', '操作不支持'),
        ('InvalidArgs', '参数无效'),
        ('NotReady', '蓝牙未就绪，请稍后重试'),
        ('Failed', '配对失败，请确认设备处于可配对模式'),
    ]
    msg_norm = msg.lower().replace('_', '').replace(' ', '')
    for key, cn in translations:
        if key.lower().replace('_', '') in msg_norm:
            return cn
    if '设备' in msg and '未找到' in msg:
        return msg
    return '配对失败，请重试'


# 翻译连接相关的 D-Bus 错误消息为中文
# 按精确度排序：长 key 优先匹配，避免 'Failed' 误匹配 'ConnectionAttemptFailed'
def _translate_connection_error(msg):
    translations = [
        ('AlreadyConnected', '设备已连接'),
        ('ConnectionAttemptFailed', '连接尝试失败'),
        ('ResourceNotAvailable', '资源不可用，请检查蓝牙服务'),
        ('AuthenticationRejected', '连接被拒绝'),
        ('AuthenticationTimeout', '连接验证超时'),
        ('AuthenticationFailed', '连接验证失败'),
        ('InProgress', '连接正在进行中，请稍后'),
        ('DoesNotExist', '设备未找到，请重新扫描'),
        ('NotAvailable', '蓝牙服务不可用'),
        ('NotSupported', '操作不支持'),
        ('NotReady', '蓝牙未就绪，请稍后'),
        ('Failed', '连接失败，请重试'),
    ]
    msg_norm = msg.lower().replace('_', '').replace(' ', '')
    for key, cn in translations:
        if key.lower().replace('_', '') in msg_norm:
            return cn
    return '连接失败，请重试'


# 翻译断开/删除相关的 D-Bus 错误消息为中文
def _translate_disconnect_error(msg):
    translations = [
        ('DoesNotExist', '设备未找到'),
        ('NotConnected', '设备未连接'),
        ('NotReady', '蓝牙未就绪'),
        ('Failed', '操作失败'),
    ]
    msg_norm = msg.lower().replace('_', '').replace(' ', '')
    for key, cn in translations:
        if key.lower().replace('_', '') in msg_norm:
            return cn
    return '操作失败，请重试'


# 配对并自动连接设备
def pair_device(mac, pin=None):
    logger.info(f"[配对入口] pair_device({mac}, pin={'有' if pin else '无'})")
    if _is_manual_power_off():
        logger.warning(f"[配对入口] {mac} 蓝牙电源已关闭")
        raise InvalidParamError("蓝牙电源已关闭，请先开启电源")
    _ensure_bluetoothd()
    if not get_all_controllers():
        logger.error(f"[配对入口] {mac} 未检测到蓝牙控制器")
        raise DeviceNotFoundError("未检测到蓝牙控制器")
    ensure_controller_up()
    if not _power_on_adapter():
        logger.error(f"[配对入口] {mac} 蓝牙控制器无法上电")
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
    if device_path:
        try:
            already_paired = bool(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Paired'))
            already_connected = bool(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Connected'))
            logger.info(f"[配对入口] {mac} 状态检查: paired={already_paired}, connected={already_connected}")
            if already_paired and already_connected:
                # 已配对且已连接，直接返回
                logger.info(f"[配对入口] {mac} 已配对且已连接，直接返回")
                device_info = _enrich_device_info(mac, device_name)
                return {
                    "data": f"设备 {device_name} 已配对并已连接",
                    "connected": True, "device_name": device_name
                }
            if already_paired:
                # 已配对但未连接，尝试连接
                logger.info(f"[配对入口] {mac} 已配对但未连接，尝试连接...")
                connected = False
                try:
                    connect_device(mac)  # 使用带互斥锁的连接方法
                    connected = True
                except Exception as e:
                    logger.warning(f"[配对入口] {mac} 已配对设备连接失败: {e}")
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
                    logger.info(f"[配对入口] {mac} 已配对但连接失败，删除旧记录重新配对")
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
        logger.info(f"[配对入口] {mac} 不在 D-Bus 中，触发短暂扫描...")
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
            logger.error(f"[配对入口] {mac} 扫描后仍未在 D-Bus 中发现")
            raise DeviceNotFoundError(f"未找到设备 {device_name}，请重新扫描后重试")

    logger.info(f"[配对入口] {mac} 开始执行配对...")
    try:
        _pair_device_interactive(mac, pin=pin)
    except InvalidParamError as e:
        if not pin and isinstance(e, PairingNeedPinError):
            raise PairingNeedPinError('需要PIN码', device_name=getattr(e, 'device_name', None) or device_name)
        raise
    except CommandError as e:
        raise CommandError(e.message, command=e.command)

    # 配对成功
    logger.info(f"[配对入口] {mac} 配对成功，保存配置并尝试自动连接...")
    device_info = _enrich_device_info(mac, device_name)
    config.add_paired_device(mac, alias=device_name, name=device_name,
                             is_audio=device_info.get("is_audio", False),
                             rssi=device_info.get("rssi", ""))
    time.sleep(0.5)

    # 配对成功后自动连接
    connected = False
    try:
        connect_device(mac)  # 使用带互斥锁的连接方法
        connected = True
    except Exception as e:
        logger.warning(f"[配对入口] {mac} 配对后自动连接失败: {e}")

    if connected:
        logger.info(f"[配对入口] {mac} 配对并连接成功")
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
        pw_check = run_command(f"{platform_paths.CMD_PW_DUMP} 2>/dev/null | grep -c '{mac_us}'")
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
    mac = mac.upper()
    logger.info(f"[连接入口] connect_device({mac})")
    if _is_manual_power_off():
        logger.warning(f"[连接入口] {mac} 蓝牙电源已关闭")
        raise InvalidParamError("蓝牙电源已关闭，请先开启电源")
    with _connecting_lock:
        if mac not in _connecting_devices_lock:
            _connecting_devices_lock[mac] = threading.Lock()
        lock = _connecting_devices_lock[mac]
    try:
        with lock:
            _ensure_bluetoothd()
            ensure_controller_up()
            if not _power_on_adapter():
                logger.error(f"[连接入口] {mac} 蓝牙控制器无法上电")
                raise CommandError("蓝牙控制器无法上电")
            time.sleep(0.5)

            result = _connect_device_interactive(mac)

            logger.info(f"蓝牙设备 {mac} 连接成功")
            device_path = _find_device_path(mac)
            if device_path:
                try:
                    _set_property(BLUEZ_IFACE_DEVICE, device_path, 'Trusted', dbus.Boolean(True))
                except dbus.exceptions.DBusException:
                    pass
            threading.Thread(target=_trust_and_activate_audio, args=(mac,), daemon=True).start()
            return result or {'data': f'设备 {mac} 连接成功', 'device_name': mac}
    finally:
        # 不在 finally 中删除锁条目，避免竞态条件
        # 仅在字典过大时批量清理未被持有的锁
        if len(_connecting_devices_lock) > 100:
            with _connecting_lock:
                stale = [m for m, l in _connecting_devices_lock.items() if not l.locked()]
                for m in stale:
                    _connecting_devices_lock.pop(m, None)


# 断开蓝牙设备
def disconnect_device(mac):
    device_path = _find_device_path(mac)
    if not device_path:
        raise DeviceNotFoundError(f"设备 {mac} 未找到")
    # 在尝试断开前，先标记为手动断开，确保即使 Disconnect 失败也不会触发自动重连
    try:
        rm = _get_reconnect_manager()
        if rm:
            rm.mark_manual_disconnect(mac)
    except Exception:
        pass
    try:
        device = dbus.Interface(_get_object(device_path), BLUEZ_IFACE_DEVICE)
        device.Disconnect()
        return f"设备 {mac} 已断开"
    except dbus.exceptions.DBusException as e:
        error_msg = str(e)
        if 'not connected' in error_msg.lower():
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
        existing_info = config.get_cached_paired_devices().get(mac.upper(), {})
        config.add_paired_device(mac, alias=alias, name=alias, is_audio=existing_info.get('is_audio', False))  # 保留已有的 is_audio
        return f"别名已设为 {alias}"
    except dbus.exceptions.DBusException as e:
        raise CommandError(str(e)[:200])


# 设置蓝牙设备的信任状态
def set_device_trusted(mac, trusted=True):
    device_path = _find_device_path(mac)
    if not device_path:
        raise DeviceNotFoundError(f"设备 {mac} 未找到")
    try:
        _set_property(BLUEZ_IFACE_DEVICE, device_path, 'Trusted', dbus.Boolean(trusted))
        return f"设备 {mac} 已{'信任' if trusted else '取消信任'}"
    except dbus.exceptions.DBusException as e:
        raise CommandError(str(e)[:200])


# 设置蓝牙设备的阻塞状态
def set_device_blocked(mac, blocked=True):
    device_path = _find_device_path(mac)
    if not device_path:
        raise DeviceNotFoundError(f"设备 {mac} 未找到")
    try:
        _set_property(BLUEZ_IFACE_DEVICE, device_path, 'Blocked', dbus.Boolean(blocked))
        return f"设备 {mac} 已{'阻塞' if blocked else '取消阻塞'}"
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


# 设置蓝牙适配器的可配对模式
def set_pairable(enabled):
    _ensure_bluetoothd()
    controllers = get_all_controllers()
    if not controllers:
        raise DeviceNotFoundError("未检测到蓝牙控制器")
    success = 0
    for ctrl in controllers:
        adapter_path = _find_adapter_path_for_controller(ctrl["name"])
        if adapter_path:
            try:
                _set_property(BLUEZ_IFACE_ADAPTER, adapter_path, 'Pairable', dbus.Boolean(enabled))
                success += 1
            except dbus.exceptions.DBusException:
                pass
    if success > 0:
        return f"可配对已{'开启' if enabled else '关闭'} ({success}/{len(controllers)})"
    raise CommandError("可配对设置失败")


# 设置蓝牙适配器的可发现超时（秒），0 表示永不超时
def set_discoverable_timeout(timeout):
    timeout = max(0, int(timeout))
    _ensure_bluetoothd()
    controllers = get_all_controllers()
    if not controllers:
        raise DeviceNotFoundError("未检测到蓝牙控制器")
    success = 0
    for ctrl in controllers:
        adapter_path = _find_adapter_path_for_controller(ctrl["name"])
        if adapter_path:
            try:
                _set_property(BLUEZ_IFACE_ADAPTER, adapter_path, 'DiscoverableTimeout', dbus.UInt32(timeout))
                success += 1
            except dbus.exceptions.DBusException:
                pass
    if success > 0:
        return f"可发现超时已设为 {timeout} 秒 ({success}/{len(controllers)})"
    raise CommandError("可发现超时设置失败")


# 显式导入 bt_audio_profiles 的公开函数，供 routes/bluetooth.py 通过 bluetooth_manager 调用
from bt_audio_profiles import (
    get_bluetooth_audio_sources,
    get_bluetooth_audio_profiles,
    switch_bluetooth_profile,
    enable_bluetooth_microphone,
    disable_bluetooth_microphone,
)

# 显式导入 bluetooth_agent 的公开函数，供本模块内部调用
# 放在文件末尾避免循环导入（bluetooth_agent 依赖本模块的常量和工具函数）
from bluetooth_agent import (
    ensure_agent,
    release_agent,
    _pair_device_interactive,
    _connect_device_interactive,
)
