import time
import os
import re
import threading
import logging

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop

from utils import run_command, start_pw_service, stop_pw_service, pw_dump, find_pw_node
import config
from auto_reconnect import AutoReconnectManager
import audio_manager
import dependency_checker
import platform_paths
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError, MediaHubError
from wp_config_manager import WPConfigManager

logger = logging.getLogger('MediaHub')

BLUEZ_SERVICE = 'org.bluez'
BLUEZ_IFACE_ADAPTER = 'org.bluez.Adapter1'
BLUEZ_IFACE_DEVICE = 'org.bluez.Device1'
BLUEZ_IFACE_AGENT_MANAGER = 'org.bluez.AgentManager1'
BLUEZ_IFACE_AGENT = 'org.bluez.Agent1'
DBUS_PROP_IFACE = 'org.freedesktop.DBus.Properties'
BLUEZ_IFACE_BATTERY = 'org.bluez.Battery1'

_DEVICE_TYPE_UUIDS = {
    '1108': 'audio-headset', '111E': 'audio-headset', '111F': 'audio-headset',
    '110B': 'audio-headphones', '110A': 'audio-headphones',
    '110C': 'audio-headphones', '110D': 'audio-headphones', '110E': 'audio-headphones',
    '1124': 'input-keyboard', '1125': 'input-keyboard', '1126': 'input-mouse', '1120': 'input-keyboard',
    '1203': 'audio-speakers', '1104': 'phone', '1105': 'phone',
}

_BT_APPEARANCE = {
    0x0400: '通用音频', 0x0401: '可穿戴耳机', 0x0402: '手持耳机',
    0x0403: '耳机', 0x0404: '便携音箱', 0x0405: '书架音箱',
    0x0406: '广播音箱', 0x0407: 'Soundbar', 0x0408: '有源音箱',
    0x0409: '智能音箱', 0x040A: '扩展低音',
    0x0340: '通用遥控', 0x0341: '遥控', 0x0342: '游戏手柄',
    0x0343: '电视遥控', 0x0344: '传感器遥控',
    0x0180: '通用键盘', 0x0181: '键盘', 0x0182: '小键盘',
    0x0190: '通用鼠标', 0x0191: '鼠标', 0x0192: '轨迹球',
    0x03C0: '通用手表', 0x03C1: '手表', 0x03C2: '怀表',
    0x07C0: '通用显示器', 0x07C1: '显示器',
    0x0700: '通用电话', 0x0701: '手机', 0x0702: '无绳电话',
    0x0540: '心率传感器', 0x0580: '血压计', 0x0900: '通用标签',
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
}

_bus = None
_bus_lock = threading.Lock()
_auto_reconnect_manager = None
_reconnect_lock = threading.Lock()
_connecting_devices_lock = {}
_connecting_devices_lock_lock = threading.Lock()
_wpc = WPConfigManager()


def _extract_bt_uuid_short(uuid_str):
    s = str(uuid_str).upper().replace('-', '')
    if len(s) == 32 and s[8:] == '00001000800000805F9B34FB':
        return s[4:8]
    if len(s) >= 8:
        return s.lstrip('0')[:4] or '0'
    return s


def _guess_type_from_uuids(uuids):
    priority = ['input-keyboard', 'input-mouse', 'audio-headset', 'audio-headphones', 'audio-speakers', 'phone']
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


def _get_managed_objects():
    bus = _get_system_bus()
    if bus is None:
        return {}
    return _get_object('/').GetManagedObjects(dbus_interface='org.freedesktop.DBus.ObjectManager')


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
    dev_name = 'dev_' + mac.replace(':', '_').upper()
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


def _ensure_bluetoothd():
    status = run_command(f"{platform_paths.CMD_SYSTEMCTL} is-active bluetooth 2>/dev/null")
    if "active" not in status["stdout"]:
        run_command(f"{platform_paths.CMD_SYSTEMCTL} start bluetooth 2>/dev/null")
        time.sleep(1)
    return True


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


def _get_reconnect_manager():
    global _auto_reconnect_manager
    with _reconnect_lock:
        if _auto_reconnect_manager is None:
            _auto_reconnect_manager = AutoReconnectManager(
                bus=_get_system_bus(),
                activate_sink_callback=audio_manager._activate_bluez_sink
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


class _BluezAgent(dbus.service.Object):
    def __init__(self, bus, path, pin=None):
        dbus.service.Object.__init__(self, bus, path)
        self._pin = pin

    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='', out_signature='')
    def Release(self):
        pass

    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='o', out_signature='s')
    def RequestPinCode(self, device):
        if self._pin:
            return self._pin
        raise dbus.DBusException('org.bluez.Error.Rejected: No PIN available')

    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='os', out_signature='')
    def DisplayPinCode(self, device, pin_code):
        pass

    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='o', out_signature='u')
    def RequestPasskey(self, device):
        if self._pin and self._pin.isdigit():
            return dbus.UInt32(int(self._pin))
        raise dbus.DBusException('org.bluez.Error.Rejected: No passkey available')

    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='ouq', out_signature='')
    def DisplayPasskey(self, device, passkey, entered):
        pass

    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='ou', out_signature='')
    def RequestConfirmation(self, device, passkey):
        if self._pin:
            return
        raise dbus.DBusException('org.bluez.Error.Rejected: 需要用户确认')

    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='os', out_signature='')
    def AuthorizeService(self, device, uuid):
        pass

    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='', out_signature='')
    def Cancel(self):
        pass


class _PersistentAgent(dbus.service.Object):
    def __init__(self, bus, path):
        dbus.service.Object.__init__(self, bus, path)

    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='', out_signature='')
    def Release(self):
        pass

    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='o', out_signature='s')
    def RequestPinCode(self, device):
        return '0000'

    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='o', out_signature='u')
    def RequestPasskey(self, device):
        return dbus.UInt32(0)

    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='os', out_signature='')
    def DisplayPinCode(self, device, pin_code):
        pass

    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='ouq', out_signature='')
    def DisplayPasskey(self, device, passkey, entered):
        pass

    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='ou', out_signature='')
    def RequestConfirmation(self, device, passkey):
        pass

    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='os', out_signature='')
    def AuthorizeService(self, device, uuid):
        pass

    @dbus.service.method(BLUEZ_IFACE_AGENT, in_signature='', out_signature='')
    def Cancel(self):
        pass


_agent_manager = None
_agent_lock = threading.Lock()
_agent_registered = False


def ensure_agent():
    global _agent_manager, _agent_registered
    with _agent_lock:
        if _agent_registered:
            return True
        try:
            bus = _get_system_bus()
            if bus is None:
                return False
            agent_obj = _PersistentAgent(bus, '/mediahub/agent')
            agent_mgr = dbus.Interface(
                bus.get_object(BLUEZ_SERVICE, '/org/bluez'),
                BLUEZ_IFACE_AGENT_MANAGER
            )
            agent_mgr.RegisterAgent('/mediahub/agent', 'NoInputNoOutput')
            agent_mgr.RequestDefaultAgent('/mediahub/agent')
            _agent_manager = agent_obj
            _agent_registered = True
            logger.info("持久蓝牙 Agent 已注册，可处理入站连接")
            return True
        except dbus.exceptions.DBusException as e:
            logger.warning(f"注册持久 Agent 失败: {e}")
            return False


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


class BluetoothAgent:
    def __init__(self):
        self._bus = _get_system_bus()
        self._agent_path = '/test/agent'
        self._agent = None
        self._pin = None

    def _register_agent(self):
        agent_manager = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, '/org/bluez'),
            BLUEZ_IFACE_AGENT_MANAGER
        )
        capability = 'KeyboardDisplay' if self._pin else 'NoInputNoOutput'
        agent_manager.RegisterAgent(self._agent_path, capability)
        agent_manager.RequestDefaultAgent(self._agent_path)

    def _unregister_agent(self):
        try:
            agent_manager = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, '/org/bluez'),
                BLUEZ_IFACE_AGENT_MANAGER
            )
            agent_manager.UnregisterAgent(self._agent_path)
        except dbus.exceptions.DBusException:
            pass

    def _get_device_interface(self, mac):
        device_path = _find_device_path(mac)
        if not device_path:
            raise dbus.exceptions.DBusException(f'设备 {mac} 未找到')
        return dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, device_path),
            BLUEZ_IFACE_DEVICE
        )

    def pair(self, mac, pin=None, timeout=45):
        self._pin = pin
        self._agent = _BluezAgent(self._bus, self._agent_path, pin=pin)

        try:
            self._register_agent()
        except dbus.exceptions.DBusException as e:
            self._cleanup()
            raise CommandError(f'蓝牙代理注册失败: {str(e)[:200]}')

        try:
            device = self._get_device_interface(mac)

            # 已配对设备先移除旧记录
            paired = _get_property(BLUEZ_IFACE_DEVICE, device.object_path, 'Paired')
            if paired:
                run_command(f"{platform_paths.CMD_BLUETOOTHCTL} remove {mac} 2>/dev/null", timeout=10)
                time.sleep(1)
                device = self._get_device_interface(mac)

            # 执行配对
            device.Pair()

            # 等待配对完成
            pair_start = time.time()
            while time.time() - pair_start < timeout:
                time.sleep(0.5)
                try:
                    paired = _get_property(BLUEZ_IFACE_DEVICE, device.object_path, 'Paired')
                    if paired:
                        try:
                            _set_property(BLUEZ_IFACE_DEVICE, device.object_path, 'Trusted', dbus.Boolean(True))
                        except dbus.exceptions.DBusException:
                            pass
                        alias = ''
                        try:
                            alias = _get_property(BLUEZ_IFACE_DEVICE, device.object_path, 'Alias')
                        except dbus.exceptions.DBusException:
                            pass
                        self._cleanup()
                        return {
                            'data': f'设备 {alias or mac} 配对成功',
                            'output': '',
                            'device_name': alias or mac
                        }
                except dbus.exceptions.DBusException:
                    pass

            self._cleanup()
            raise CommandError('配对超时，请确认设备处于可配对模式')

        except dbus.exceptions.DBusException as e:
            error_msg = str(e)
            needs_pin = 'AuthenticationFailed' in error_msg or 'AuthenticationRejected' in error_msg
            if needs_pin and not pin:
                self._cleanup()
                alias = mac
                try:
                    alias = _get_property(BLUEZ_IFACE_DEVICE, _find_device_path(mac), 'Alias')
                except Exception:
                    pass
                exc = InvalidParamError('需要输入PIN码')
                exc.needs_pin = True
                exc.device_name = alias
                raise exc
            self._cleanup()
            alias = mac
            try:
                device_path = _find_device_path(mac)
                if device_path:
                    alias = _get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias')
            except Exception:
                pass
            raise CommandError(error_msg[:200] or '配对失败')

    def connect(self, mac, timeout=20):
        try:
            device = self._get_device_interface(mac)
            device.Connect()

            # 等待连接完成
            conn_start = time.time()
            while time.time() - conn_start < timeout:
                time.sleep(0.5)
                try:
                    connected = _get_property(BLUEZ_IFACE_DEVICE, device.object_path, 'Connected')
                    if connected:
                        alias = mac
                        try:
                            alias = str(_get_property(BLUEZ_IFACE_DEVICE, device.object_path, 'Alias'))
                        except Exception:
                            pass
                        return {
                            'data': f'设备 {alias} 连接成功',
                            'output': '', 'device_name': alias
                        }
                except dbus.exceptions.DBusException:
                    pass

            raise CommandError('连接超时')

        except dbus.exceptions.DBusException as e:
            error_msg = str(e)
            error_name = getattr(e, 'get_dbus_name', lambda: '')() or ''

            # 已连接
            if 'already' in error_msg.lower() and 'connected' in error_msg.lower():
                alias = mac
                try:
                    device_path = _find_device_path(mac)
                    if device_path:
                        alias = str(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias'))
                except Exception:
                    pass
                return {'data': f'设备 {alias} 已连接', 'output': '', 'device_name': alias}

            # profile 不可用
            if 'profile-unavailable' in error_msg or 'br-connection-profile' in error_msg:
                exc = CommandError('蓝牙音频 profile 不可用，请检查 WirePlumber 和 libspa-0.2-bluetooth')
                exc.profile_unavailable = True
                exc.device_name = mac
                raise exc

            raise CommandError(error_msg[:200] or '连接失败')

    def _cleanup(self):
        self._unregister_agent()
        if self._agent is not None:
            try:
                self._agent.remove_from_connection()
            except Exception:
                pass
            self._agent = None


def _quick_discover_device(mac):
    """快速扫描使设备出现在 BlueZ managed objects 中"""
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

    release_agent()
    try:
        agent = BluetoothAgent()
        return agent.connect(mac, timeout=20)
    finally:
        ensure_agent()


def _pair_device_interactive(mac, pin=None):
    release_agent()
    try:
        agent = BluetoothAgent()
        return agent.pair(mac, pin=pin, timeout=45)
    finally:
        ensure_agent()


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

    # 解析 hciconfig
    hci_result = run_command(f"{platform_paths.CMD_HCICONFIG} -a {controller_name} 2>/dev/null")
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
        hci_info = run_command(f"{platform_paths.CMD_HCITOOL} info {controller_name} 2>/dev/null")
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
    controller_details = [get_controller_details(c["name"]) for c in controllers]

    result = run_command(f"{platform_paths.CMD_SYSTEMCTL} is-active bluetooth 2>/dev/null || echo inactive")
    service_active = "active" in result["stdout"]
    any_powered = any(c.get("powered", False) for c in controller_details)

    # 服务运行但未上电时自动上电
    if service_active and controller_details and not any_powered and not _is_manual_power_off():
        logger.info("蓝牙服务运行中但适配器未上电，自动上电...")
        _power_on_adapter()
        controller_details = [get_controller_details(c["name"]) for c in controllers]
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
    for dev in connected:
        mac_us = dev['mac'].replace(':', '_')
        sink_check = run_command(f"pactl list sinks short 2>/dev/null", timeout=5)
        if mac_us not in (sink_check.get('stdout') or ''):
            pw_check = run_command(f"pw-dump 2>/dev/null | grep -c '{mac_us}'", timeout=5)
            if not pw_check['stdout'] or pw_check['stdout'].strip() == '0':
                with _activating_devices_lock:
                    if dev['mac'] in _activating_devices:
                        continue
                    _activating_devices.add(dev['mac'])
                threading.Thread(target=_activate_audio, args=(dev['mac'],), daemon=True).start()


def _activate_audio(mac):
    """激活蓝牙设备的音频 sink"""
    try:
        _trust_and_activate_audio(mac)
    finally:
        with _activating_devices_lock:
            _activating_devices.discard(mac)


def install_bluetooth_driver():
    pkgs = ["bluez", "bluez-tools", "libspa-0.2-bluetooth", "pipewire", "pipewire-pulse", "wireplumber"]
    missing = []
    for pkg in pkgs:
        check = run_command(f"dpkg -s {pkg} 2>/dev/null | grep -c '^Status: install ok installed'")
        if not check["stdout"] or "0" in check["stdout"]:
            missing.append(pkg)
    if missing:
        result = run_command(f"apt-get update -qq && apt-get install -y -qq {' '.join(missing)}", timeout=180)
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

    _ensure_bluetoothd()
    adapter_paths = _find_all_adapter_paths()
    if not adapter_paths:
        return []

    # 上电所有适配器
    for adapter_path in adapter_paths:
        try:
            _set_property(BLUEZ_IFACE_ADAPTER, adapter_path, 'Powered', dbus.Boolean(True))
            _set_property(BLUEZ_IFACE_ADAPTER, adapter_path, 'Discoverable', dbus.Boolean(True))
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

    # 执行扫描
    for adapter_path in adapter_paths:
        try:
            adapter = dbus.Interface(_get_object(adapter_path), BLUEZ_IFACE_ADAPTER)
            adapter.StartDiscovery()
            time.sleep(8)
            adapter.StopDiscovery()
        except dbus.exceptions.DBusException as e:
            logger.debug(f"扫描适配器失败: {e}")

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
        if props.get('RSSI'):
            device_info["rssi"] = str(props['RSSI']) + " dBm"
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
                    if _extract_bt_uuid_short(u) in {k for k, v in _DEVICE_TYPE_UUIDS.items() if v in ('audio-headset', 'audio-headphones', 'audio-speakers')}:
                        device_info["is_audio"] = True
                        break
        if props.get('Modalias'):
            device_info["modalias"] = str(props['Modalias']).strip()
        if props.get('Name') and (not device_info.get("name") or device_info.get("name") == "Unknown"):
            device_info["name"] = str(props['Name']).strip()
        if props.get('Appearance'):
            appearance_val = int(props['Appearance'])
            device_info["appearance"] = _BT_APPEARANCE.get(appearance_val, f'0x{appearance_val:04X}')
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

    try:
        _pair_device_interactive(mac, pin=pin)
    except InvalidParamError as e:
        if not pin and getattr(e, 'needs_pin', False):
            exc = InvalidParamError('需要PIN码')
            exc.needs_pin = True
            exc.device_name = getattr(e, 'device_name', None) or device_name
            raise exc
        raise

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


def _trust_and_activate_audio(mac):
    """信任设备并激活音频 sink"""
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

    audio_manager._activate_bluez_sink(mac)
    try:
        _get_reconnect_manager()
    except Exception:
        pass


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

            _connect_device_interactive(mac)

            logger.debug(f"连接结果: 成功")
            device_path = _find_device_path(mac)
            if device_path:
                try:
                    _set_property(BLUEZ_IFACE_DEVICE, device_path, 'Trusted', dbus.Boolean(True))
                except dbus.exceptions.DBusException:
                    pass
            threading.Thread(target=_trust_and_activate_audio, args=(mac,), daemon=True).start()
        finally:
            _connecting_devices_lock.pop(mac, None)


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
        raise CommandError(error_msg[:200] or f"断开设备 {mac} 失败")


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
            raise CommandError(str(e)[:200])
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
        config.add_paired_device(mac, alias=alias, name=alias, is_audio=None)
        return f"别名已设为 {alias}"
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


# ── 蓝牙音频输入 (Source) 管理函数 ──────────────────────────────

BLUEZ_IFACE_CARD = 'org.bluez.Card1'

_AUDIO_UUIDS = {
    'HFP': ('0000111E-0000-1000-8000-00805F9B34FB', '111E', '111F'),
    'HSP': ('00001108-0000-1000-8000-00805F9B34FB', '1108'),
    'A2DP': ('0000110B-0000-1000-8000-00805F9B34FB', '110B', '110D', '110A'),
}

_PROFILE_UUID_MAP = {
    'hfp_hf': 'HFP', 'hfp_ag': 'HFP',
    'hsp_hs': 'HSP', 'hsp_ag': 'HSP',
    'a2dp_sink': 'A2DP', 'a2dp_source': 'A2DP',
}


def _get_device_uuids(mac):
    """获取蓝牙设备支持的 UUID 列表"""
    device_path = _find_device_path(mac)
    if not device_path:
        return []
    try:
        uuids = _get_property(BLUEZ_IFACE_DEVICE, device_path, 'UUIDs')
        return [str(u).upper() for u in uuids] if uuids else []
    except dbus.exceptions.DBusException:
        return []


def _has_audio_input_uuid(uuids):
    """判断 UUID 列表中是否包含音频输入相关 UUID (HFP/HSP)"""
    for u in uuids:
        short = _extract_bt_uuid_short(u)
        if short in ('1108', '111E', '111F'):
            return True
        if '1111E' in u or '11108' in u or '1111F' in u:
            return True
    return False


def _find_bluez_card_path(mac):
    """通过 D-Bus 查找设备的 org.bluez.Card1 路径"""
    dev_name = 'dev_' + mac.replace(':', '_').upper()
    try:
        for path, ifaces in _get_managed_objects().items():
            if BLUEZ_IFACE_CARD in ifaces and path.endswith('/' + dev_name):
                return path
    except dbus.exceptions.DBusException as e:
        logger.debug(f"查找 Card1 路径失败: {e}")
    return None


def _find_pw_device_for_mac(mac, pw_data=None):
    """在 PipeWire 数据中查找与 MAC 对应的 Device 对象"""
    if pw_data is None:
        pw_data = pw_dump()
    mac_us = mac.replace(':', '_')
    for obj in pw_data:
        if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Device':
            continue
        props = obj.get('info', {}).get('props', {})
        device_name = props.get('device.name', '') or props.get('device.nick', '') or props.get('api.bluez5.address', '')
        if mac_us.lower() in device_name.lower() or mac.upper() in str(props.get('api.bluez5.address', '')).upper():
            return obj
    return None


def _get_pw_device_profiles(pw_device):
    """从 PipeWire Device 对象提取 EnumProfile 信息"""
    profiles = []
    params = pw_device.get('info', {}).get('params', {})
    enum_profiles = params.get('EnumProfile', [])
    for ep in enum_profiles:
        if not isinstance(ep, dict):
            continue
        name = ep.get('name', '')
        desc = ep.get('description', name)
        priority = ep.get('priority', 0)
        available = ep.get('available', False)
        index = ep.get('index', -1)
        profiles.append({
            'name': name,
            'description': desc,
            'priority': priority,
            'available': available,
            'index': index,
        })
    return profiles


def _get_pw_device_active_profile(pw_device):
    """获取 PipeWire Device 当前激活的 profile 名称"""
    params = pw_device.get('info', {}).get('params', {})
    profiles = params.get('Profile', [])
    for p in profiles:
        if not isinstance(p, dict):
            continue
        if p.get('save', False) or p.get('index', -1) >= 0:
            return p.get('name', '')
    # 回退：从 props 获取
    props = pw_device.get('info', {}).get('props', {})
    return props.get('device.profile', '')


def get_bluetooth_audio_sources():
    """获取所有蓝牙音频输入源 (HFP/HSP 麦克风、带麦音箱等)"""
    try:
        sources = []
        seen_macs = set()

        # 1. 从 BlueZ D-Bus 查找已连接的音频设备
        try:
            for path, ifaces in _get_managed_objects().items():
                if BLUEZ_IFACE_DEVICE not in ifaces:
                    continue
                props = ifaces[BLUEZ_IFACE_DEVICE]
                if not props.get('Connected', False):
                    continue
                uuids = [str(u).upper() for u in (props.get('UUIDs') or [])]
                # 检查是否支持音频输入 (HFP/HSP) 或音频输出 (A2DP)
                has_audio = any(
                    _extract_bt_uuid_short(u) in _DEVICE_TYPE_UUIDS
                    for u in uuids
                )
                has_input = _has_audio_input_uuid(uuids)
                if not has_audio and not has_input:
                    continue

                mac = _mac_from_path(path)
                name = str(props.get('Alias', '') or props.get('Name', mac))
                seen_macs.add(mac)

                # 查找 PipeWire Source 节点
                mac_us = mac.replace(':', '_')
                pw_data = pw_dump()
                source_name = ''
                source_node_id = None
                for obj in pw_data:
                    if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
                        continue
                    obj_props = obj.get('info', {}).get('props', {})
                    mc = obj_props.get('media.class', '')
                    if mc not in ('Audio/Source', 'Audio/Source/Virtual'):
                        continue
                    node_name = obj_props.get('node.name', '')
                    if 'bluez' in node_name.lower() and mac_us.lower() in node_name.lower():
                        source_name = node_name
                        source_node_id = obj.get('id')
                        break

                # 获取 profile 信息
                profiles = []
                active_profile = ''
                card_path = _find_bluez_card_path(mac)
                if card_path:
                    try:
                        card_props = _get_properties(BLUEZ_IFACE_CARD, card_path)
                        active_profile = str(card_props.get('ActiveProfile', ''))
                        for p in (card_props.get('Profiles', []) or []):
                            if isinstance(p, dbus.Dictionary):
                                profiles.append({
                                    'name': str(p.get('Name', '')),
                                    'description': str(p.get('Description', '')),
                                })
                            elif isinstance(p, str):
                                profiles.append({'name': p, 'description': p})
                    except dbus.exceptions.DBusException:
                        pass

                # 如果 Card1 没有 profile，尝试 PipeWire Device
                if not profiles:
                    pw_dev = _find_pw_device_for_mac(mac, pw_data)
                    if pw_dev:
                        profiles = _get_pw_device_profiles(pw_dev)
                        if not active_profile:
                            active_profile = _get_pw_device_active_profile(pw_dev)

                # 从 UUID 推断 profile
                if not profiles:
                    if has_input:
                        profiles.append({'name': 'hfp_hf', 'description': 'HFP Hands-Free'})
                        profiles.append({'name': 'hsp_hs', 'description': 'HSP Headset'})
                    profiles.append({'name': 'a2dp_sink', 'description': 'A2DP Sink'})

                sources.append({
                    'mac': mac,
                    'name': name,
                    'connected': bool(props.get('Connected', False)),
                    'source_name': source_name,
                    'source_node_id': source_node_id,
                    'profiles': profiles,
                    'active_profile': active_profile,
                })
        except dbus.exceptions.DBusException as e:
            logger.debug(f"D-Bus 查找蓝牙音频源失败: {e}")

        # 2. 从 PipeWire Source 节点中补充 bluez 源（可能 BlueZ 已断开但 PW 仍有残留）
        pw_data = pw_dump()
        for obj in pw_data:
            if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
                continue
            obj_props = obj.get('info', {}).get('props', {})
            mc = obj_props.get('media.class', '')
            if mc not in ('Audio/Source', 'Audio/Source/Virtual'):
                continue
            node_name = obj_props.get('node.name', '')
            if 'bluez' not in node_name.lower():
                continue
            # 从节点名提取 MAC
            mac_match = re.search(r'([0-9A-Fa-f]{2}[_:]){5}[0-9A-Fa-f]{2}', node_name)
            if not mac_match:
                continue
            mac = mac_match.group(0).replace('_', ':').upper()
            if mac in seen_macs:
                continue
            seen_macs.add(mac)

            # 尝试获取设备名
            dev_name = obj_props.get('node.nick', '') or obj_props.get('device.description', '') or mac
            pw_dev = _find_pw_device_for_mac(mac, pw_data)
            profiles = []
            active_profile = ''
            if pw_dev:
                profiles = _get_pw_device_profiles(pw_dev)
                active_profile = _get_pw_device_active_profile(pw_dev)

            sources.append({
                'mac': mac,
                'name': dev_name,
                'connected': True,
                'source_name': node_name,
                'source_node_id': obj.get('id'),
                'profiles': profiles,
                'active_profile': active_profile,
            })

        return sources
    except Exception as e:
        logger.error(f"获取蓝牙音频源失败: {e}")
        raise CommandError(str(e)[:200])


def switch_bluetooth_profile(mac, profile_name):
    """切换蓝牙设备的音频 profile (如 A2DP ↔ HFP)"""
    mac = mac.upper()
    try:
        # 方式1: 通过 D-Bus Card1 接口切换
        card_path = _find_bluez_card_path(mac)
        if card_path:
            try:
                card_obj = _get_object(card_path)
                card_iface = dbus.Interface(card_obj, BLUEZ_IFACE_CARD)
                card_iface.SetProfile(dbus.String(profile_name))
                time.sleep(1)
                # 验证切换结果
                try:
                    new_profile = str(_get_property(BLUEZ_IFACE_CARD, card_path, 'ActiveProfile'))
                    if new_profile == profile_name:
                        return f'已切换到 {profile_name}'
                except dbus.exceptions.DBusException:
                    pass
                return f'已发送切换 {profile_name} 请求'
            except dbus.exceptions.DBusException as e:
                err = str(e)
                if 'NotSupported' in err or 'Not Available' in err:
                    logger.warning(f"Card1 不支持 profile {profile_name}: {err}")
                else:
                    logger.debug(f"Card1 切换 profile 失败: {e}")

        # 方式2: 通过 wp-cli / wpctl 切换
        pw_data = pw_dump()
        pw_dev = _find_pw_device_for_mac(mac, pw_data)
        if pw_dev:
            dev_id = pw_dev.get('id')
            # 查找目标 profile 的 index
            target_index = None
            profiles = _get_pw_device_profiles(pw_dev)
            for p in profiles:
                if p['name'] == profile_name:
                    target_index = p.get('index')
                    break
            if target_index is not None:
                result = run_command(f"wpctl set-profile {dev_id} {target_index} 2>/dev/null", timeout=5)
                if result['success']:
                    time.sleep(1)
                    return f'已通过 wpctl 切换到 {profile_name}'
                logger.debug(f"wpctl 切换失败: {result.get('stderr', '')}")

        # 方式3: 通过 pw-cli 切换
        if pw_dev:
            dev_id = pw_dev.get('id')
            result = run_command(
                f"pw-cli set-param {dev_id} Profile '{{ \"index\": {target_index or 0}, \"save\": true }}' 2>/dev/null",
                timeout=5
            )
            if result['success']:
                time.sleep(1)
                return f'已通过 pw-cli 切换到 {profile_name}'

        raise CommandError(f'无法切换设备 {mac} 的 profile，未找到 Card1 或 PipeWire Device')
    except MediaHubError:
        raise
    except Exception as e:
        logger.error(f"切换蓝牙 profile 失败: {e}")
        raise CommandError(str(e)[:200])


def get_bluetooth_audio_profiles(mac):
    """获取蓝牙设备的可用音频 profile 列表"""
    mac = mac.upper()
    try:
        profiles = []

        # 1. 通过 D-Bus Card1 接口获取
        card_path = _find_bluez_card_path(mac)
        if card_path:
            try:
                card_props = _get_properties(BLUEZ_IFACE_CARD, card_path)
                for p in (card_props.get('Profiles', []) or []):
                    if isinstance(p, dbus.Dictionary):
                        profiles.append({
                            'name': str(p.get('Name', '')),
                            'description': str(p.get('Description', '')),
                            'available': True,
                        })
                    elif isinstance(p, str):
                        profiles.append({'name': p, 'description': p, 'available': True})
            except dbus.exceptions.DBusException as e:
                logger.debug(f"Card1 获取 profile 失败: {e}")

        # 2. 通过 PipeWire Device EnumProfile 获取
        pw_data = pw_dump()
        pw_dev = _find_pw_device_for_mac(mac, pw_data)
        if pw_dev:
            pw_profiles = _get_pw_device_profiles(pw_dev)
            # 合并：以 PW 的可用性信息为准
            pw_profile_names = {p['name'] for p in pw_profiles}
            for pp in pw_profiles:
                existing = next((p for p in profiles if p['name'] == pp['name']), None)
                if existing:
                    existing['available'] = pp.get('available', True)
                    if pp.get('description') and pp['description'] != pp['name']:
                        existing['description'] = pp['description']
                else:
                    profiles.append({
                        'name': pp['name'],
                        'description': pp.get('description', pp['name']),
                        'available': pp.get('available', True),
                    })
            # 补充 PW 有但 Card1 没有的 profile
            for pp in pw_profiles:
                if pp['name'] not in pw_profile_names:
                    profiles.append({
                        'name': pp['name'],
                        'description': pp.get('description', pp['name']),
                        'available': pp.get('available', True),
                    })

        # 3. 根据 UUID 推断
        if not profiles:
            uuids = _get_device_uuids(mac)
            has_hfp = any('1111E' in u or '1111F' in u or _extract_bt_uuid_short(u) in ('111E', '111F') for u in uuids)
            has_hsp = any('11108' in u or _extract_bt_uuid_short(u) == '1108' for u in uuids)
            has_a2dp = any('1110B' in u or '1110A' in u or '1110D' in u or _extract_bt_uuid_short(u) in ('110B', '110A', '110D') for u in uuids)
            if has_hfp:
                profiles.append({'name': 'hfp_hf', 'description': 'HFP Hands-Free (含麦克风)', 'available': True})
                profiles.append({'name': 'hfp_ag', 'description': 'HFP Audio Gateway', 'available': True})
            if has_hsp:
                profiles.append({'name': 'hsp_hs', 'description': 'HSP Headset (含麦克风)', 'available': True})
                profiles.append({'name': 'hsp_ag', 'description': 'HSP Audio Gateway', 'available': True})
            if has_a2dp:
                profiles.append({'name': 'a2dp_sink', 'description': 'A2DP Sink (高质量播放)', 'available': True})
                profiles.append({'name': 'a2dp_source', 'description': 'A2DP Source', 'available': True})

        return profiles
    except Exception as e:
        logger.error(f"获取蓝牙音频 profile 失败: {e}")
        raise CommandError(str(e)[:200])


def enable_bluetooth_microphone(mac):
    """启用蓝牙麦克风：切换到 HFP/HSP profile 并等待 Source 节点出现"""
    mac = mac.upper()
    # 检查设备是否已连接
    device_path = _find_device_path(mac)
    if not device_path:
        raise DeviceNotFoundError(f'设备 {mac} 未找到，请先连接')
    try:
        connected = _get_property(BLUEZ_IFACE_DEVICE, device_path, 'Connected')
        if not connected:
            raise DeviceNotFoundError(f'设备 {mac} 未连接，请先连接')
    except dbus.exceptions.DBusException:
        pass

    # 检查当前 profile
    card_path = _find_bluez_card_path(mac)
    current_profile = ''
    if card_path:
        try:
            current_profile = str(_get_property(BLUEZ_IFACE_CARD, card_path, 'ActiveProfile'))
        except dbus.exceptions.DBusException:
            pass

    # 如果已经是 HFP/HSP，直接查找 Source 节点
    if current_profile and any(kw in current_profile.lower() for kw in ('hfp', 'hsp')):
        pass  # 已在正确 profile，跳过切换
    else:
        # 获取可用 profile，优先 HFP，其次 HSP
        available_profiles = get_bluetooth_audio_profiles(mac)
        target_profile = None
        for pref in ('hfp_hf', 'hfp_ag', 'hsp_hs', 'hsp_ag'):
            match = next((p for p in available_profiles if p['name'] == pref and p.get('available', True)), None)
            if match:
                target_profile = pref
                break

        if not target_profile:
            raise InvalidParamError('设备不支持 HFP/HSP profile，无法使用麦克风')

        # 切换 profile
        try:
            switch_bluetooth_profile(mac, target_profile)
        except MediaHubError as e:
            raise CommandError(f'切换到 {target_profile} 失败: {e.message}')

    # 等待 PipeWire Source 节点出现（最多 8 秒）
    mac_us = mac.replace(':', '_')
    source_info = None
    for _ in range(16):
        time.sleep(0.5)
        pw_data = pw_dump()
        for obj in pw_data:
            if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
                continue
            obj_props = obj.get('info', {}).get('props', {})
            mc = obj_props.get('media.class', '')
            if mc not in ('Audio/Source', 'Audio/Source/Virtual'):
                continue
            node_name = obj_props.get('node.name', '')
            if 'bluez' in node_name.lower() and mac_us.lower() in node_name.lower():
                source_info = {
                    'source_name': node_name,
                    'source_node_id': obj.get('id'),
                }
                break
        if source_info:
            break

    if not source_info:
        raise CommandError(f'已切换 profile 但未检测到 PipeWire Source 节点，请稍后重试')

    return {
        'data': f'蓝牙麦克风已启用',
        'mac': mac,
        'source_name': source_info['source_name'],
        'source_node_id': source_info['source_node_id'],
    }


def disable_bluetooth_microphone(mac):
    """禁用蓝牙麦克风：切换回 A2DP profile 以获得更好的音频质量"""
    mac = mac.upper()
    # 检查设备是否已连接
    device_path = _find_device_path(mac)
    if not device_path:
        raise DeviceNotFoundError(f'设备 {mac} 未找到')

    # 获取可用 profile，优先 A2DP Sink
    available_profiles = get_bluetooth_audio_profiles(mac)
    target_profile = None
    for pref in ('a2dp_sink', 'a2dp_source'):
        match = next((p for p in available_profiles if p['name'] == pref and p.get('available', True)), None)
        if match:
            target_profile = pref
            break

    if not target_profile:
        raise InvalidParamError('设备不支持 A2DP profile，无法切换回高质量音频')

    # 检查当前 profile
    card_path = _find_bluez_card_path(mac)
    current_profile = ''
    if card_path:
        try:
            current_profile = str(_get_property(BLUEZ_IFACE_CARD, card_path, 'ActiveProfile'))
        except dbus.exceptions.DBusException:
            pass

    # 如果已经是 A2DP，无需切换
    if current_profile and 'a2dp' in current_profile.lower():
        return f'设备已在 A2DP profile ({current_profile})，无需切换'

    # 切换到 A2DP
    try:
        switch_bluetooth_profile(mac, target_profile)
    except MediaHubError as e:
        raise CommandError(f'切换到 A2DP 失败: {e.message}')

    return f'已切换回 A2DP 高质量音频模式'
