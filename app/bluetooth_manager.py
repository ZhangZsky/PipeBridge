import os
import re
import time
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
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError, PairingNeedPinError

logger = logging.getLogger('PipeBridge')

BLUEZ_SERVICE = 'org.bluez'
BLUEZ_IFACE_ADAPTER = 'org.bluez.Adapter1'
BLUEZ_IFACE_DEVICE = 'org.bluez.Device1'
BLUEZ_IFACE_AGENT_MANAGER = 'org.bluez.AgentManager1'
BLUEZ_IFACE_AGENT = 'org.bluez.Agent1'
DBUS_PROP_IFACE = 'org.freedesktop.DBus.Properties'
BLUEZ_IFACE_BATTERY = 'org.bluez.Battery1'

_DEVICE_TYPE_UUIDS = {
    '110B': 'audio-headphones', '110A': 'audio-headphones',
    '110C': 'audio-headphones', '110D': 'audio-headphones', '110E': 'audio-headphones',
    '1203': 'audio-speakers',
    '1108': 'audio-headset', '111E': 'audio-headset', '111F': 'audio-headset',
    '1112': 'audio-headset',
    '1116': 'audio-video',
    '1124': 'input-keyboard', '1125': 'input-keyboard', '1126': 'input-mouse',
    '1120': 'input-keyboard', '1122': 'input-mouse', '1123': 'input-joystick',
    '1104': 'phone', '1105': 'phone', '1111': 'phone',
    '184E': 'le-audio', '184F': 'le-audio', '1850': 'le-audio',
}

_BT_APPEARANCE = {
    0x0400: '通用音频', 0x0401: '可穿戴耳机', 0x0402: '手持耳机',
    0x0403: '耳机', 0x0404: '便携音箱', 0x0405: '书架音箱',
    0x0406: '广播音箱', 0x0407: 'Soundbar', 0x0408: '有源音箱',
    0x0409: '智能音箱', 0x040A: '扩展低音',
    0x040B: 'Soundbar 前置', 0x040C: 'Soundbar 后置',
    0x0410: '助听器-左耳', 0x0411: '助听器-右耳', 0x0412: '助听器-双耳',
    0x0340: '通用遥控', 0x0341: '遥控', 0x0342: '游戏手柄',
    0x0343: '电视遥控', 0x0344: '传感器遥控',
    0x0180: '通用键盘', 0x0181: '键盘', 0x0182: '小键盘',
    0x0190: '通用鼠标', 0x0191: '鼠标', 0x0192: '轨迹球',
    0x03C0: '通用手表', 0x03C1: '手表', 0x03C2: '怀表',
    0x03C3: '智能手环', 0x03C4: '智能戒指',
    0x07C0: '通用显示器', 0x07C1: '显示器',
    0x0700: '通用电话', 0x0701: '手机', 0x0702: '无绳电话',
    0x0703: '智能手机',
    0x0540: '心率传感器', 0x0580: '血压计', 0x0900: '通用标签',
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
_connecting_lock = threading.Lock()
_wpc = WPConfigManager()
_pairing_lock = threading.Lock()

def _extract_bt_uuid_short(uuid_str):
    s = str(uuid_str).upper().replace('-', '')
    if len(s) == 32 and s[8:] == '00001000800000805F9B34FB':
        return s[4:8]
    if len(s) >= 8:
        return s[:4].lstrip('0') or '0'
    return s

_AUDIO_APPEARANCES = {
    0x0400, 0x0401, 0x0402, 0x0403, 0x0404, 0x0405, 0x0406, 0x0407,
    0x0408, 0x0409, 0x040A, 0x040B, 0x040C,
    0x0410, 0x0411, 0x0412,
    0x0941, 0x0942, 0x0943,
}

_AUDIO_UUID_SHORTS = {
    '1108', '110A', '110B', '110C', '110D', '110E',
    '1112', '1116', '111E', '111F', '1203',
    '184E', '184F', '1850',
}

_A2DP_SOURCE_UUID = '110A'
_A2DP_SINK_UUID = '110B'

_PHONE_APPEARANCE_RANGE = range(0x0700, 0x0710)

def _guess_type_from_uuids(uuids):
    priority = ['input-keyboard', 'input-mouse', 'audio-headset', 'audio-headphones',
                'audio-speakers', 'audio-video', 'le-audio', 'phone']
    matched = {_DEVICE_TYPE_UUIDS.get(_extract_bt_uuid_short(u)) for u in uuids}
    for t in priority:
        if t in matched:
            return t
    return None

def _is_manual_power_off():
    return not config.get_bt_power_enabled()

def _get_system_bus():
    global _bus
    with _bus_lock:
        if _bus is None:
            DBusGMainLoop(set_as_default=True)
            _bus = dbus.SystemBus()
            from bluetooth_agent import _ensure_glib_loop
            _ensure_glib_loop()
        return _bus

def _get_object(path):
    bus = _get_system_bus()
    if bus is None:
        raise dbus.exceptions.DBusException("无法连接到系统D-Bus")
    return bus.get_object(BLUEZ_SERVICE, path)

def _get_properties(interface, path):
    return _get_object(path).GetAll(interface, dbus_interface=DBUS_PROP_IFACE)

def _get_property(interface, path, prop_name):
    return _get_object(path).Get(interface, prop_name, dbus_interface=DBUS_PROP_IFACE)

def _set_property(interface, path, prop_name, value):
    return _get_object(path).Set(interface, prop_name, value, dbus_interface=DBUS_PROP_IFACE)

_mo_cache = None
_mo_cache_time = 0
_MO_CACHE_TTL = 2.0

def _get_managed_objects():
    global _mo_cache, _mo_cache_time
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

def _find_adapter_path():
    try:
        for path, ifaces in _get_managed_objects().items():
            if BLUEZ_IFACE_ADAPTER in ifaces:
                return path
    except dbus.exceptions.DBusException as e:
        logger.debug(f"查找适配器失败: {e}")
    return None

def _find_adapter_path_for_controller(ctrl_name):
    try:
        for path, ifaces in _get_managed_objects().items():
            if BLUEZ_IFACE_ADAPTER in ifaces and path.endswith(ctrl_name):
                return path
    except dbus.exceptions.DBusException:
        pass
    return None

def _find_all_adapter_paths():
    paths = []
    try:
        for path, ifaces in _get_managed_objects().items():
            if BLUEZ_IFACE_ADAPTER in ifaces:
                paths.append(path)
    except dbus.exceptions.DBusException as e:
        logger.debug(f"查找适配器失败: {e}")
    return paths

def _find_device_path(mac):
    dev_name = 'dev_' + mac.replace(':', '_').replace('-', '_').upper()
    try:
        for path, ifaces in _get_managed_objects().items():
            if BLUEZ_IFACE_DEVICE in ifaces and path.endswith('/' + dev_name):
                return path
    except dbus.exceptions.DBusException as e:
        logger.debug(f"查找设备路径失败: {e}")
    return None

def _mac_from_path(path):
    last = path.split('/')[-1]
    if last.startswith('dev_'):
        last = last[4:]
    return last.replace('_', ':').upper()

_bt_start_fail_time = 0
_BT_START_RETRY_INTERVAL = 60

def _ensure_bluetoothd():
    global _bt_start_fail_time
    status = run_command(f"{platform_paths.CMD_SYSTEMCTL} is-active bluetooth 2>/dev/null")
    if "active" not in status["stdout"]:
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

def _try_usb_reset_adapter():
    if _reset_bluetooth_usb_devices_sysfs():
        return True

    adapter = _find_adapter_path()
    if adapter:
        try:
            vendor_id = str(_get_property(BLUEZ_IFACE_ADAPTER, adapter, 'VendorID'))
            product_id = str(_get_property(BLUEZ_IFACE_ADAPTER, adapter, 'ProductID'))
            if vendor_id and product_id:
                vendor_hex = f"{int(vendor_id):04x}"
                product_hex = f"{int(product_id):04x}"
                if _reset_usb_device_by_match(vendor_hex, product_hex):
                    return True
        except dbus.exceptions.DBusException:
            pass

    if _reset_usb_device_by_match(keyword='bluetooth'):
        return True
    if _reset_usb_device_by_match(keyword='bt'):
        return True

    return False

def _reset_bluetooth_usb_devices_sysfs() -> bool:
    btusb_devices = _find_devices_by_btusb_driver()
    for dev_path in btusb_devices:
        if _reset_usb_device_by_sysfs_path(dev_path):
            return True

    base = '/sys/bus/usb/devices'
    try:
        dev_names = os.listdir(base)
    except OSError:
        return False

    for dev_name in dev_names:
        dev_path = os.path.join(base, dev_name)
        if _is_bluetooth_device_by_usb_class(dev_path):
            if _reset_usb_device_by_sysfs_path(dev_path):
                return True

    return False

def _find_devices_by_btusb_driver() -> list:
    driver_path = '/sys/bus/usb/drivers/btusb'
    devices = []
    try:
        entries = os.listdir(driver_path)
    except OSError:
        return devices

    for entry in entries:
        if entry in ('bind', 'unbind', 'module', 'uevent'):
            continue
        if ':' in entry:
            parent_name = entry.split(':')[0]
            parent_path = os.path.join('/sys/bus/usb/devices', parent_name)
            if os.path.exists(parent_path) and parent_path not in devices:
                devices.append(parent_path)
    return devices

def _is_bluetooth_device_by_usb_class(dev_path: str) -> bool:
    dev_class_file = os.path.join(dev_path, 'bDeviceClass')
    if os.path.exists(dev_class_file):
        try:
            with open(dev_class_file, 'r') as f:
                dev_class = f.read().strip()
            if dev_class.lower() == 'e0':
                return True
        except (IOError, OSError):
            pass

    try:
        entries = os.listdir(dev_path)
    except OSError:
        return False

    for entry in entries:
        if ':' not in entry:
            continue
        iface_path = os.path.join(dev_path, entry)
        iface_class_file = os.path.join(iface_path, 'bInterfaceClass')
        if not os.path.exists(iface_class_file):
            continue
        try:
            with open(iface_class_file, 'r') as f:
                iface_class = f.read().strip()
            if iface_class.lower() == 'e0':
                return True
        except (IOError, OSError):
            continue

    return False

def _reset_usb_device_by_sysfs_path(dev_path: str) -> bool:
    auth_file = os.path.join(dev_path, 'authorized')
    if not os.path.exists(auth_file):
        return False
    product_name = ''
    try:
        with open(os.path.join(dev_path, 'product'), 'r') as f:
            product_name = f.read().strip()
    except (IOError, OSError):
        pass
    run_command(f"echo 0 > {auth_file}", timeout=5)
    time.sleep(1)
    run_command(f"echo 1 > {auth_file}", timeout=5)
    time.sleep(2)
    logger.info(f"已通过 sysfs 复位蓝牙 USB 设备: {dev_path} ({product_name})")
    return True

def _deep_reset_bluetooth_adapter() -> bool:
    logger.warning("USB 复位无效，执行深层恢复：unbind + rmmod + USB 复位 + modprobe")

    btusb_interfaces = _find_btusb_interfaces()
    usb_device_path = None
    if btusb_interfaces:
        for iface in btusb_interfaces:
            parent_name = iface.split(':')[0]
            usb_device_path = os.path.join('/sys/bus/usb/devices', parent_name)
            logger.info(f"找到 btusb 接口: {iface}, 父设备: {usb_device_path}")

    run_command(f"{platform_paths.CMD_SYSTEMCTL} stop bluetooth", timeout=10)
    time.sleep(2)

    for iface in btusb_interfaces:
        unbind_result = run_command(f"echo '{iface}' > /sys/bus/usb/drivers/btusb/unbind", timeout=3)
        if unbind_result['success']:
            logger.info(f"已 unbind btusb 接口: {iface}")
        else:
            logger.warning(f"unbind btusb 接口失败: {iface}, stderr={unbind_result.get('stderr', '')}")
    time.sleep(1)

    rmmod_btusb = run_command("rmmod btusb", timeout=5)
    if rmmod_btusb['success']:
        logger.info("btusb 模块已卸载")
    else:
        logger.warning(f"rmmod btusb 失败: {rmmod_btusb.get('stderr', '').strip()}")
    rmmod_bt = run_command("rmmod bluetooth", timeout=5)
    if rmmod_bt['success']:
        logger.info("bluetooth 模块已卸载")
    else:
        logger.warning(f"rmmod bluetooth 失败: {rmmod_bt.get('stderr', '').strip()}")
    time.sleep(1)

    if usb_device_path and os.path.exists(usb_device_path):
        auth_file = os.path.join(usb_device_path, 'authorized')
        if os.path.exists(auth_file):
            run_command(f"echo 0 > {auth_file}", timeout=5)
            time.sleep(2)
            run_command(f"echo 1 > {auth_file}", timeout=5)
            time.sleep(2)
            logger.info(f"已执行 USB authorized 复位: {usb_device_path}")

    modprobe_bt = run_command("modprobe bluetooth", timeout=5)
    if modprobe_bt['success']:
        logger.info("bluetooth 模块已加载")
    else:
        logger.warning(f"modprobe bluetooth 失败: {modprobe_bt.get('stderr', '').strip()}")
    time.sleep(0.5)
    modprobe_btusb = run_command("modprobe btusb", timeout=5)
    if modprobe_btusb['success']:
        logger.info("btusb 模块已加载")
    else:
        logger.warning(f"modprobe btusb 失败: {modprobe_btusb.get('stderr', '').strip()}")

    if not _wait_for_hci0_ready(timeout=15):
        logger.warning("等待 hci0 初始化超时，继续尝试启动 bluetooth 服务")

    run_command(f"{platform_paths.CMD_SYSTEMCTL} start bluetooth", timeout=10)

    if not _wait_for_bluez_adapter(timeout=10):
        logger.warning("BlueZ 未识别适配器，重启 bluetooth 服务触发重新扫描")
        run_command(f"{platform_paths.CMD_SYSTEMCTL} restart bluetooth", timeout=10)
        _wait_for_bluez_adapter(timeout=8)

    adapter = _find_adapter_path()
    if adapter:
        try:
            _set_property(BLUEZ_IFACE_ADAPTER, adapter, 'Powered', dbus.Boolean(True))
            time.sleep(0.5)
            if _get_property(BLUEZ_IFACE_ADAPTER, adapter, 'Powered'):
                logger.info("深层恢复成功：btusb 模块重载后适配器已上电")
                return True
        except dbus.exceptions.DBusException as e:
            logger.warning(f"深层恢复后适配器上电失败: {e}")
    else:
        logger.warning("深层恢复后 BlueZ 仍未识别适配器")

    return False

def _find_btusb_interfaces() -> list:
    driver_path = '/sys/bus/usb/drivers/btusb'
    interfaces = []
    try:
        entries = os.listdir(driver_path)
    except OSError:
        return interfaces

    for entry in entries:
        if entry in ('bind', 'unbind', 'module', 'uevent'):
            continue
        if ':' in entry:
            interfaces.append(entry)
    return interfaces

def _wait_for_hci0_ready(timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = run_command(f"{platform_paths.CMD_HCICONFIG} hci0 2>/dev/null", timeout=2)
        if result['stdout'] and 'UP' in result['stdout']:
            logger.info("hci0 已就绪（UP 状态）")
            return True
        time.sleep(1)
    return False

def _wait_for_bluez_adapter(timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _find_adapter_path():
            logger.info("BlueZ 已识别适配器")
            return True
        time.sleep(1)
    return False

def _reset_usb_device_by_match(vendor_hex: str = '', product_hex: str = '', keyword: str = '') -> bool:
    result = run_command("lsusb 2>/dev/null", timeout=3)
    if not result['stdout']:
        return False

    for line in result['stdout'].split('\n'):
        line_lower = line.lower()
        matched = False
        if vendor_hex and product_hex:
            if vendor_hex.lower() in line_lower and product_hex.lower() in line_lower:
                matched = True
        elif keyword:
            if keyword in line_lower:
                matched = True
        if not matched:
            continue

        match = re.search(r'Bus\s+(\d+)\s+Device\s+(\d+):', line)
        if not match:
            continue
        bus, dev = match.group(1), match.group(2)
        find_result = run_command(
            f"find /sys/bus/usb/devices/ -maxdepth 1 -name '{bus}-{dev}*'",
            timeout=3
        )
        if not find_result['stdout']:
            continue
        dev_path = find_result['stdout'].strip().split('\n')[0]
        auth_file = f"{dev_path}/authorized"
        run_command(f"echo 0 > {auth_file}", timeout=5)
        time.sleep(1)
        run_command(f"echo 1 > {auth_file}", timeout=5)
        time.sleep(2)
        logger.info(f"已尝试 USB 蓝牙适配器复位: bus={bus} dev={dev} line={line.strip()}")
        return True
    return False

def _power_on_adapter():
    adapter = _find_adapter_path()
    if not adapter:
        logger.warning("适配器上电失败: 未找到适配器路径，尝试 USB 复位恢复...")
        if _try_usb_reset_adapter():
            time.sleep(3)
            adapter = _find_adapter_path()
            if adapter:
                try:
                    _set_property(BLUEZ_IFACE_ADAPTER, adapter, 'Powered', dbus.Boolean(True))
                    time.sleep(0.5)
                    if _get_property(BLUEZ_IFACE_ADAPTER, adapter, 'Powered'):
                        logger.info("USB 复位后适配器成功识别并上电")
                        return True
                except dbus.exceptions.DBusException as e:
                    logger.warning(f"USB 复位后适配器上电失败: {e}")
            else:
                logger.warning("USB 复位后 BlueZ 仍未识别适配器")
        return _deep_reset_bluetooth_adapter()
    try:
        _set_property(BLUEZ_IFACE_ADAPTER, adapter, 'Powered', dbus.Boolean(True))
        time.sleep(0.5)
        if _get_property(BLUEZ_IFACE_ADAPTER, adapter, 'Powered'):
            return True
    except dbus.exceptions.DBusException as e:
        logger.warning(f"适配器上电失败: {e}")

    try:
        rfkill = run_command("rfkill list 2>/dev/null", timeout=3)
        if rfkill['stdout']:
            logger.warning(f"rfkill 状态: {rfkill['stdout'][:300]}")
        hciconfig = run_command(f"{platform_paths.CMD_HCICONFIG} -a 2>/dev/null", timeout=3)
        if hciconfig['stdout']:
            logger.warning(f"hciconfig 状态: {hciconfig['stdout'][:300]}")
        dmesg = run_command("dmesg 2>/dev/null | grep -iE 'bluetooth|firmware|btusb|hci' | tail -10", timeout=3)
        if dmesg['stdout']:
            logger.warning(f"内核蓝牙日志: {dmesg['stdout'][:500]}")
    except Exception:
        pass

    logger.info("尝试通过 USB 复位恢复蓝牙适配器...")
    if _try_usb_reset_adapter():
        time.sleep(2)
        try:
            _set_property(BLUEZ_IFACE_ADAPTER, adapter, 'Powered', dbus.Boolean(True))
            time.sleep(0.5)
            if _get_property(BLUEZ_IFACE_ADAPTER, adapter, 'Powered'):
                logger.info("USB 复位后适配器成功上电")
                return True
        except dbus.exceptions.DBusException:
            pass
        logger.warning("USB 复位后适配器仍无法上电")
    else:
        logger.warning("USB 复位失败，无法恢复蓝牙适配器")

    logger.info("USB 复位无法恢复，执行深层恢复...")
    return _deep_reset_bluetooth_adapter()

def _get_reconnect_manager():
    global _auto_reconnect_manager
    with _reconnect_lock:
        if _auto_reconnect_manager is None:
            _auto_reconnect_manager = AutoReconnectManager(
                bus=_get_system_bus()
            )
            _auto_reconnect_manager.start()
        return _auto_reconnect_manager

def set_reconnect_enabled(enabled):
    _get_reconnect_manager().set_enabled(enabled)

def get_reconnect_status():
    try:
        return _get_reconnect_manager().get_status()
    except Exception:
        return {'monitoring': False, 'reconnecting_devices': [], 'manual_disconnects': []}

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

def get_controller_details(controller_name):
    details = {
        "name": controller_name, "status": "DOWN", "mac": "", "type": "",
        "features": "", "packet_types": "", "link_policy": "", "link_mode": "",
        "hci_version": "", "manufacturer": "", "bus": "", "powered": False
    }

    hci_result = run_command(f"{platform_paths.CMD_HCICONFIG} -a {shlex.quote(controller_name)} 2>/dev/null")
    if hci_result['stdout']:
        _parse_hciconfig(hci_result['stdout'], details)

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

    if not details['type']:
        sysfs_type = run_command(f"cat {platform_paths.SYSFS_BLUETOOTH}/{controller_name}/type 2>/dev/null")
        if sysfs_type['stdout']:
            type_map = {'1': 'BR/EDR', '2': 'AMP', '3': 'LE'}
            details['type'] = type_map.get(sysfs_type['stdout'].strip(), sysfs_type['stdout'].strip())

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

def get_bluetooth_status():
    _ensure_bluetoothd()
    usb_devices = check_bluetooth_hardware()
    controllers = get_all_controllers()
    _details_cache = {}
    def _get_details(name):
        if name not in _details_cache:
            _details_cache[name] = get_controller_details(name)
        return _details_cache[name]

    controller_details = [_get_details(c["name"]) for c in controllers]

    result = run_command(f"{platform_paths.CMD_SYSTEMCTL} is-active bluetooth 2>/dev/null || echo inactive")
    service_active = "active" in result["stdout"]
    any_powered = any(c.get("powered", False) for c in controller_details)

    if service_active and controller_details and not any_powered and not _is_manual_power_off():
        logger.info("蓝牙服务运行中但适配器未上电，自动上电...")
        _power_on_adapter()
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
    try:
        for path, ifaces in _get_managed_objects().items():
            if 'org.bluez.MediaEndpoint1' in ifaces:
                return True
    except dbus.exceptions.DBusException:
        pass
    return False

def _ensure_bluetooth_audio_ready():
    if check_bluetooth_audio_ready():
        logger.info("[音频预检] 蓝牙音频环境已就绪 (MediaEndpoint1 已注册)")
        return True, '已就绪'

    logger.warning("[音频预检] 蓝牙音频环境未就绪，尝试自动修复...")

    pw_check = run_command("pgrep -x pipewire 2>/dev/null")
    pw_running = bool(pw_check['success'] and pw_check['stdout'].strip())
    if not pw_running or not _pw_socket_exists():
        logger.info("[音频预检] PipeWire 未运行或 socket 缺失，启动 PipeWire...")
        start_pw_service('pipewire')
        for _ in range(10):
            if _pw_socket_exists():
                break
            time.sleep(0.5)

    if not _pw_socket_exists():
        detail = f"PipeWire运行={'是' if pw_running else '否'}, PipeWire socket缺失, MediaEndpoint1未注册"
        logger.error(f"[音频预检] PipeWire socket 未就绪: {detail}")
        return False, detail

    wp_check = run_command("pgrep -x wireplumber 2>/dev/null")
    if not (wp_check['success'] and wp_check['stdout'].strip()):
        logger.info("[音频预检] WirePlumber 未运行，尝试启动...")
        start_pw_service('wireplumber')
        time.sleep(2)

    try:
        ensure_wireplumber_bluez_config()
    except Exception as e:
        logger.warning(f"[音频预检] 部署 WirePlumber 蓝牙配置失败: {e}")

    logger.info("[音频预检] 等待 MediaEndpoint1 注册...")
    for _ in range(16):
        if check_bluetooth_audio_ready():
            logger.info("[音频预检] 修复成功，MediaEndpoint1 已注册")
            return True, '修复后已就绪'
        time.sleep(0.5)

    logger.error("[音频预检] 修复失败，8秒后 MediaEndpoint1 仍未注册")
    wp_recheck = run_command("pgrep -x wireplumber 2>/dev/null")
    wp_running = bool(wp_recheck['success'] and wp_recheck['stdout'].strip())

    spa_result = run_command("dpkg -L libspa-0.2-bluetooth 2>/dev/null | grep -E '\\.so$' | head -1")
    spa_ok = bool(spa_result['success'] and spa_result['stdout'].strip())

    detail = f"PipeWire socket={'存在' if _pw_socket_exists() else '缺失'}, WirePlumber运行={'是' if wp_running else '否'}, SPA插件={'存在' if spa_ok else '缺失'}, MediaEndpoint1未注册"
    logger.error(f"[音频预检] 诊断: {detail}")
    return False, detail

def ensure_wireplumber_bluez_config():
    return _wpc.deploy_bluez_config()

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

_activating_devices = {}
_activating_devices_lock = threading.Lock()
_ACTIVATING_TIMEOUT = 30

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
    pw_data = pw_dump()
    for dev in connected:
        mac_us = dev['mac'].replace(':', '_')
        has_sink = any(
            isinstance(obj, dict) and obj.get('type') == 'PipeWire:Interface:Node'
            and 'bluez' in obj.get('info', {}).get('props', {}).get('node.name', '').lower()
            and mac_us.lower() in obj.get('info', {}).get('props', {}).get('node.name', '').lower()
            for obj in pw_data
        )
        if not has_sink:
            with _activating_devices_lock:
                if dev['mac'] in _activating_devices:
                    if time.time() - _activating_devices[dev['mac']] > _ACTIVATING_TIMEOUT:
                        _activating_devices.pop(dev['mac'], None)
                    else:
                        continue
                _activating_devices[dev['mac']] = time.time()
            threading.Thread(target=_activate_audio, args=(dev['mac'],), daemon=True).start()

def _activate_audio(mac):
    try:
        _trust_and_activate_audio(mac)
    finally:
        with _activating_devices_lock:
            _activating_devices.pop(mac, None)

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

def scan_devices():
    if _is_manual_power_off():
        raise InvalidParamError("蓝牙电源已关闭，请先开启电源")

    with _connecting_lock:
        active = [m for m, l in _connecting_devices_lock.items() if l.locked()]
    if active:
        raise InvalidParamError(f"有设备正在连接中 ({', '.join(active)})，请稍后扫描")

    _ensure_bluetoothd()
    adapter_paths = _find_all_adapter_paths()
    if not adapter_paths:
        return []

    for adapter_path in adapter_paths:
        try:
            _set_property(BLUEZ_IFACE_ADAPTER, adapter_path, 'Powered', dbus.Boolean(True))
        except dbus.exceptions.DBusException as e:
            logger.debug(f"设置适配器属性失败: {e}")
    time.sleep(0.5)

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
        for adapter_path in adapter_paths:
            adapter = None
            discovery_started = False
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

    cached = config.get_cached_paired_devices()
    for d in all_devices:
        mac = d["mac"].upper()
        if mac in cached:
            d["alias"] = cached[mac].get("alias", "")
            if d.get("name") == "Unknown" or not d.get("name"):
                d["name"] = cached[mac].get("alias") or cached[mac].get("name", d.get("name", "Unknown"))
            if d.get("rssi") is None:
                cached_rssi = cached[mac].get("rssi", "")
                if cached_rssi:
                    d["rssi"] = cached_rssi

    config.set_last_scan(all_devices)
    return all_devices

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
        if device_info.get('connected'):
            rssi_val = get_connected_rssi(mac)
            if rssi_val:
                device_info["rssi"] = rssi_val
            elif props.get('RSSI') is not None:
                device_info["rssi"] = str(props['RSSI']) + " dBm"
            else:
                try:
                    rssi_dbus = _get_property(BLUEZ_IFACE_DEVICE, device_path, 'RSSI')
                    if rssi_dbus is not None:
                        device_info["rssi"] = str(rssi_dbus) + " dBm"
                except dbus.exceptions.DBusException:
                    pass
        else:
            if props.get('RSSI') is not None:
                device_info["rssi"] = str(props['RSSI']) + " dBm"
            else:
                try:
                    rssi_dbus = _get_property(BLUEZ_IFACE_DEVICE, device_path, 'RSSI')
                    if rssi_dbus is not None:
                        device_info["rssi"] = str(rssi_dbus) + " dBm"
                except dbus.exceptions.DBusException:
                    pass
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

def get_connected_rssi(mac):
    
    try:
        out = run_command(f"{platform_paths.CMD_HCITOOL} rssi {mac} 2>/dev/null", timeout=3)
        out_text = out.get('stdout', '') + out.get('stderr', '')
        if 'RSSI return value' in out_text:
            m = re.search(r'-?\d+', out_text.split('RSSI return value')[-1])
            if m:
                return m.group() + " dBm"
    except Exception:
        pass
    return None

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

    device_name = mac
    device_path = _find_device_path(mac)
    if device_path:
        try:
            alias = _get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias')
            if alias:
                device_name = alias
        except dbus.exceptions.DBusException:
            pass

    if device_path:
        try:
            already_paired = bool(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Paired'))
            already_connected = bool(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Connected'))
            logger.info(f"[配对入口] {mac} 状态检查: paired={already_paired}, connected={already_connected}")
            if already_paired and already_connected:
                logger.info(f"[配对入口] {mac} 已配对且已连接，直接返回")
                device_info = _enrich_device_info(mac, device_name)
                return {
                    "data": f"设备 {device_name} 已配对并已连接",
                    "connected": True, "device_name": device_name
                }
            if already_paired:
                logger.info(f"[配对入口] {mac} 已配对但未连接，尝试连接...")
                connected = False
                try:
                    connect_device(mac)
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
                    logger.info(f"[配对入口] {mac} 已配对但连接失败，删除旧记录重新配对")
                    try:
                        adapter_path = _find_adapter_path()
                        if adapter_path:
                            adapter = dbus.Interface(_get_object(adapter_path), BLUEZ_IFACE_ADAPTER)
                            adapter.RemoveDevice(device_path)
                    except dbus.exceptions.DBusException:
                        try:
                            run_command(f"{platform_paths.CMD_BLUETOOTHCTL} remove {shlex.quote(mac)} 2>/dev/null", timeout=10)
                        except Exception:
                            pass
                    time.sleep(1)
        except dbus.exceptions.DBusException:
            pass

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

    logger.info(f"[配对入口] {mac} 配对成功，保存配置并尝试自动连接...")
    device_info = _enrich_device_info(mac, device_name)
    config.add_paired_device(mac, alias=device_name, name=device_name,
                             is_audio=device_info.get("is_audio", False),
                             rssi=device_info.get("rssi", ""))
    time.sleep(0.5)

    connected = False
    try:
        connect_device(mac)
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

def _trust_and_activate_audio(mac, is_auto_reconnect=False):
    device_path = _find_device_path(mac)
    if device_path:
        try:
            if not _get_property(BLUEZ_IFACE_DEVICE, device_path, 'Connected'):
                logger.info(f"设备 {mac} 已断开，跳过音频激活")
                return
            _set_property(BLUEZ_IFACE_DEVICE, device_path, 'Trusted', dbus.Boolean(True))
        except dbus.exceptions.DBusException:
            pass

    mac_us = mac.replace(':', '_')
    for _ in range(10):
        time.sleep(1)
        pw_check = run_command(f"{platform_paths.CMD_PW_DUMP} 2>/dev/null | grep -c '{mac_us}'")
        if pw_check["stdout"] and pw_check["stdout"].strip() != "0":
            break

    import audio_manager
    audio_manager.activate_bluez_sink(mac, set_default=not is_auto_reconnect)
    try:
        _get_reconnect_manager()
    except Exception:
        pass

def _get_max_connections():
    
    adapter = _find_adapter_path()
    if not adapter:
        return 7
    try:
        max_conn = _get_property(BLUEZ_IFACE_ADAPTER, adapter, 'SupportedMaxConnections')
        if max_conn and isinstance(max_conn, (int, float)):
            return int(max_conn)
    except dbus.exceptions.DBusException:
        pass
    return 7

def _count_connected_devices():
    count = 0
    try:
        for path, ifaces in _get_managed_objects().items():
            if BLUEZ_IFACE_DEVICE in ifaces:
                props = ifaces[BLUEZ_IFACE_DEVICE]
                if props.get('Connected', False):
                    count += 1
    except dbus.exceptions.DBusException:
        pass
    return count

def connect_device(mac, is_auto_reconnect=False):
    mac = mac.upper()
    logger.info(f"[连接入口] connect_device({mac}, auto_reconnect={is_auto_reconnect})")
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

            device_path = _find_device_path(mac)
            already_connected = False
            if device_path:
                try:
                    already_connected = bool(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Connected'))
                except dbus.exceptions.DBusException:
                    pass
            if not already_connected:
                max_conn = _get_max_connections()
                current = _count_connected_devices()
                if current >= max_conn:
                    logger.warning(f"[连接入口] {mac} 连接数已达上限: {current}/{max_conn}")
                    raise CommandError(f"蓝牙适配器连接数已达上限 ({max_conn})，请断开其他设备后重试")

            result = _connect_device_interactive(mac)

            logger.info(f"蓝牙设备 {mac} 连接成功")
            device_path = _find_device_path(mac)
            if device_path:
                try:
                    _set_property(BLUEZ_IFACE_DEVICE, device_path, 'Trusted', dbus.Boolean(True))
                except dbus.exceptions.DBusException:
                    pass
            threading.Thread(target=_trust_and_activate_audio, args=(mac, is_auto_reconnect), daemon=True).start()
            return result or {'data': f'设备 {mac} 连接成功', 'device_name': mac}
    finally:
        with _connecting_lock:
            stale = [m for m, l in _connecting_devices_lock.items() if not l.locked()]
            for m in stale:
                _connecting_devices_lock.pop(m, None)

def disconnect_device(mac):
    device_path = _find_device_path(mac)
    if not device_path:
        raise DeviceNotFoundError(f"设备 {mac} 未找到")
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
        config.add_paired_device(mac, alias=alias, name=alias, is_audio=existing_info.get('is_audio', False))
        return f"别名已设为 {alias}"
    except dbus.exceptions.DBusException as e:
        raise CommandError(str(e)[:200])

def set_device_trusted(mac, trusted=True):
    device_path = _find_device_path(mac)
    if not device_path:
        raise DeviceNotFoundError(f"设备 {mac} 未找到")
    try:
        _set_property(BLUEZ_IFACE_DEVICE, device_path, 'Trusted', dbus.Boolean(trusted))
        return f"设备 {mac} 已{'信任' if trusted else '取消信任'}"
    except dbus.exceptions.DBusException as e:
        raise CommandError(str(e)[:200])

def set_device_blocked(mac, blocked=True):
    device_path = _find_device_path(mac)
    if not device_path:
        raise DeviceNotFoundError(f"设备 {mac} 未找到")
    try:
        _set_property(BLUEZ_IFACE_DEVICE, device_path, 'Blocked', dbus.Boolean(blocked))
        return f"设备 {mac} 已{'阻塞' if blocked else '取消阻塞'}"
    except dbus.exceptions.DBusException as e:
        raise CommandError(str(e)[:200])

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

from bt_audio_profiles import (
    get_bluetooth_audio_sources,
    get_bluetooth_audio_profiles,
    switch_bluetooth_profile,
    enable_bluetooth_microphone,
    disable_bluetooth_microphone,
)

from bluetooth_agent import (
    ensure_agent,
    release_agent,
    _pair_device_interactive,
    _connect_device_interactive,
)

__all__ = [
    'get_bluetooth_audio_sources', 'get_bluetooth_audio_profiles',
    'switch_bluetooth_profile', 'enable_bluetooth_microphone',
    'disable_bluetooth_microphone', 'ensure_agent', 'release_agent',
]
