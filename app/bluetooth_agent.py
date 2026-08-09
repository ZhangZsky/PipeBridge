import os
import re
import time
import shlex
import logging
import threading

import dbus
import dbus.service

from utils import run_command
from exceptions import DeviceNotFoundError, CommandError, PairingNeedPinError, ProfileUnavailableError

logger = logging.getLogger('PipeBridge')

_audio_ready_cache = {'ready': False, 'detail': '', 'time': 0}
_AUDIO_READY_CACHE_TTL = 5

def _cached_ensure_bluetooth_audio_ready():
    now = time.time()
    # 仅复用「成功」的缓存结果；失败结果不缓存，避免环境恢复后仍被拒绝
    if _audio_ready_cache['ready'] and now - _audio_ready_cache['time'] < _AUDIO_READY_CACHE_TTL:
        return _audio_ready_cache['ready'], _audio_ready_cache['detail']

    # 使用带自动修复能力的预检：会尝试启动 PipeWire/WirePlumber、部署蓝牙配置并等待 MediaEndpoint1 注册
    from bluetooth_manager import _ensure_bluetooth_audio_ready
    ready, detail = _ensure_bluetooth_audio_ready()

    if ready:
        _audio_ready_cache['ready'] = ready
        _audio_ready_cache['detail'] = detail
        _audio_ready_cache['time'] = now
    else:
        # 预检失败时清除缓存，确保下一次连接立即重新检测并再次尝试修复
        _audio_ready_cache['ready'] = False
        _audio_ready_cache['detail'] = detail
        _audio_ready_cache['time'] = 0
    return ready, detail

_glib_loop = None
_glib_loop_thread = None

def _ensure_glib_loop():
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

class _BaseBluezAgent(dbus.service.Object):
    def __init__(self, bus, path):
        dbus.service.Object.__init__(self, bus, path)

    @dbus.service.method('org.bluez.Agent1', in_signature='', out_signature='')
    def Release(self):
        logger.debug("Agent Release 被调用")

    @dbus.service.method('org.bluez.Agent1', in_signature='os', out_signature='')
    def DisplayPinCode(self, device, pin_code):
        logger.debug(f"Agent DisplayPinCode: device={device}, pin={pin_code}")

    @dbus.service.method('org.bluez.Agent1', in_signature='ouq', out_signature='')
    def DisplayPasskey(self, device, passkey, entered):
        logger.debug(f"Agent DisplayPasskey: device={device}, passkey={passkey}, entered={entered}")

    @dbus.service.method('org.bluez.Agent1', in_signature='os', out_signature='')
    def AuthorizeService(self, device, uuid):
        logger.debug(f"Agent AuthorizeService: device={device}, uuid={uuid} (自动授权)")

    @dbus.service.method('org.bluez.Agent1', in_signature='', out_signature='')
    def Cancel(self):
        logger.debug("Agent Cancel 被调用")

class _PersistentAgent(_BaseBluezAgent):
    def __init__(self, bus, path):
        super().__init__(bus, path)
        self._pairing_pin = None

    def set_pairing_pin(self, pin):
        self._pairing_pin = pin

    def clear_pairing_pin(self):
        self._pairing_pin = None

    @dbus.service.method('org.bluez.Agent1', in_signature='o', out_signature='s')
    def RequestPinCode(self, device):
        pin = self._pairing_pin or '0000'
        logger.debug(f"持久Agent RequestPinCode: device={device}, pin={'***' if self._pairing_pin else '0000'}")
        return pin

    @dbus.service.method('org.bluez.Agent1', in_signature='o', out_signature='u')
    def RequestPasskey(self, device):
        key = int(self._pairing_pin) if self._pairing_pin and self._pairing_pin.isdigit() else 0
        logger.debug(f"持久Agent RequestPasskey: device={device}, key={'***' if self._pairing_pin else '0'}")
        return dbus.UInt32(key)

    @dbus.service.method('org.bluez.Agent1', in_signature='ou', out_signature='')
    def RequestConfirmation(self, device, passkey):
        logger.debug(f"持久Agent RequestConfirmation: device={device}, passkey={passkey} (自动确认)")

# OBEX Agent(org.bluez.obex.Agent1)：手机通过 OPP 推送文件时 obexd 回调已注册 Agent 的 AuthorizePush 请求授权，无 Agent 则 obexd 以 0x43 Forbidden 拒绝收发；OBEX D-Bus 接口挂在用户会话总线，故必须注册到会话总线而非系统总线

OBEX_SERVICE = 'org.bluez.obex'
OBEX_AGENT_PATH = '/pipebridge/obex_agent'
OBEX_IFACE_AGENT = 'org.bluez.obex.Agent1'
OBEX_IFACE_AGENT_MANAGER = 'org.bluez.obex.AgentManager1'
OBEX_IFACE_TRANSFER = 'org.bluez.obex.Transfer1'


def _get_session_bus_address():
    # 推断当前用户会话总线地址(obexd/obex Agent 均挂在该总线上)：
    #   1. 环境变量 DBUS_SESSION_BUS_ADDRESS，但仅当其为 unix:path= 且 socket 真实存在时才采纳
    #      (root 或 systemd 环境下该变量常残留失效地址，直接用会连到不存在的总线导致 OBEX 静默失败)
    #   2. 按 XDG_RUNTIME_DIR/bus 或 /run/user/<uid>/bus 约定拼装并校验 socket 存在
    def _socket_ok(a):
        if not a:
            return False
        if a.startswith('unix:path='):
            return os.path.exists(a[len('unix:path='):])
        # 抽象命名空间(unix:abstract=)或其它形式无法用 path 校验，保守视为可用
        return True

    addr = os.environ.get('DBUS_SESSION_BUS_ADDRESS')
    if addr and _socket_ok(addr):
        return addr

    runtime_dir = os.environ.get('XDG_RUNTIME_DIR')
    if not runtime_dir:
        try:
            runtime_dir = f'/run/user/{os.getuid()}'
        except AttributeError:
            runtime_dir = None
    if runtime_dir:
        candidate = f'unix:path={runtime_dir}/bus'
        if _socket_ok(candidate):
            return candidate
    # 环境变量地址虽 socket 不存在，但无更好来源时仍返回它交由上层尝试(dbus-daemon 可能随后建好)
    if addr:
        return addr
    return None


_session_bus = None
_session_bus_lock = threading.Lock()


def get_session_bus():
    # 获取(并缓存)用户会话总线连接，失败返回 None
    global _session_bus
    with _session_bus_lock:
        if _session_bus is not None:
            return _session_bus
        try:
            from dbus.mainloop.glib import DBusGMainLoop
            DBusGMainLoop(set_as_default=True)
        except Exception as e:
            logger.debug(f"设置 DBusGMainLoop 失败: {e}")
        addr = _get_session_bus_address()
        if not addr:
            logger.warning("无法确定用户会话总线地址，OBEX Agent 可能无法注册")
            return None
        # 确保环境变量存在，subprocess 启动 obexd/obexctl 时能继承(强制写入，覆盖可能残留的失效地址)
        os.environ['DBUS_SESSION_BUS_ADDRESS'] = addr
        try:
            _session_bus = dbus.bus.BusConnection(addr)
            return _session_bus
        except dbus.exceptions.DBusException as e:
            logger.warning(f"连接用户会话总线失败: {e}")
            _session_bus = None
            return None


def obexd_service_available():
    # 检查 org.bluez.obex 是否已在会话总线上(obexd 就绪)：obexd 是 D-Bus 可激活服务无常驻进程，pgrep 不可靠，故通过访问总线上 org.bluez.obex 名称并 Introspect 判定，成功即可用，否则抛 ServiceUnknown/NameHasNoOwner 返回 False
    bus = get_session_bus()
    if bus is None:
        return False
    try:
        bus.get_object(OBEX_SERVICE, '/org/bluez/obex').Introspect(
            dbus_interface='org.freedesktop.DBus.Introspectable'
        )
        return True
    except dbus.exceptions.DBusException:
        return False


class _ObexAgent(dbus.service.Object):
    # org.bluez.obex.Agent1 实现：自动接受所有入站推送

    def __init__(self, bus, path):
        dbus.service.Object.__init__(self, bus, path)

    @dbus.service.method(OBEX_IFACE_AGENT, in_signature='', out_signature='')
    def Release(self):
        logger.debug("OBEX Agent Release 被调用")

    @dbus.service.method(OBEX_IFACE_AGENT, in_signature='o', out_signature='s')
    def AuthorizePush(self, transfer_path):
        # 收到推送请求：返回最终保存文件名(相对 RECEIVE_DIR)，返回空字符串表示沿用发送方原始文件名并接受推送
        name = ''
        try:
            bus = get_session_bus()
            if bus is not None:
                props = dbus.Interface(
                    bus.get_object(OBEX_SERVICE, transfer_path),
                    'org.freedesktop.DBus.Properties'
                )
                name = str(props.Get(OBEX_IFACE_TRANSFER, 'Name'))
        except dbus.exceptions.DBusException as e:
            logger.debug(f"OBEX AuthorizePush 读取传输属性失败(忽略): {e}")
        logger.debug(f"OBEX 收到推送并自动接受: name={name or '未知'}, transfer={transfer_path}")
        return ""

    @dbus.service.method(OBEX_IFACE_AGENT, in_signature='', out_signature='')
    def Cancel(self):
        logger.debug("OBEX Agent Cancel 被调用")


_obex_agent = None
_obex_agent_lock = threading.Lock()
_obex_agent_registered = False


def ensure_obex_agent():
    # 在用户会话总线上注册 OBEX Agent(幂等)，返回是否注册成功；需在 obexd 就绪后调用(org.bluez.obex 名称已在会话总线出现)
    global _obex_agent, _obex_agent_registered

    with _obex_agent_lock:
        _ensure_glib_loop()
        bus = get_session_bus()
        if bus is None:
            return False

        if _obex_agent_registered and _obex_agent is not None:
            # 校验 obexd 仍在会话总线上；否则需要重新注册
            try:
                bus.get_object(OBEX_SERVICE, '/org/bluez/obex').Introspect(
                    dbus_interface='org.freedesktop.DBus.Introspectable'
                )
                return True
            except dbus.exceptions.DBusException:
                logger.warning("obexd 可能已重启，OBEX Agent 失效，重新注册...")
                _obex_agent_registered = False

        max_retries = 3
        for attempt in range(max_retries):
            try:
                if _obex_agent is not None:
                    try:
                        _obex_agent.remove_from_connection()
                    except Exception as e:
                        logger.debug(f"注销旧 OBEX Agent 失败: {e}")
                    _obex_agent = None
                agent_obj = _ObexAgent(bus, OBEX_AGENT_PATH)
                mgr = dbus.Interface(
                    bus.get_object(OBEX_SERVICE, '/org/bluez/obex'),
                    OBEX_IFACE_AGENT_MANAGER
                )
                mgr.RegisterAgent(OBEX_AGENT_PATH)
                _obex_agent = agent_obj
                _obex_agent_registered = True
                logger.info("OBEX Agent 已注册，可自动接受入站文件推送")
                return True
            except dbus.exceptions.DBusException as e:
                error_msg = str(e)
                logger.warning(f"注册 OBEX Agent 失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                if _obex_agent is not None:
                    try:
                        _obex_agent.remove_from_connection()
                    except Exception as e:
                        logger.debug(f"注销旧 OBEX Agent 失败: {e}")
                    _obex_agent = None
                # AlreadyExists：说明已注册过（可能上次未清理），先注销再重试
                if 'AlreadyExists' in error_msg:
                    try:
                        mgr = dbus.Interface(
                            bus.get_object(OBEX_SERVICE, '/org/bluez/obex'),
                            OBEX_IFACE_AGENT_MANAGER
                        )
                        mgr.UnregisterAgent(OBEX_AGENT_PATH)
                    except dbus.exceptions.DBusException as e:
                        logger.debug(f"注销已存在的 OBEX Agent 失败: {e}")
                    time.sleep(0.3)
                    continue
                if ('ServiceUnknown' in error_msg or 'NameHasNoOwner' in error_msg) and attempt < max_retries - 1:
                    logger.debug("obexd 尚未就绪，等待 1 秒后重试注册 OBEX Agent...")
                    time.sleep(1)
                    continue
                break

        logger.error(f"注册 OBEX Agent 失败，已重试 {max_retries} 次")
        return False


def release_obex_agent():
    global _obex_agent, _obex_agent_registered
    with _obex_agent_lock:
        if _obex_agent is None:
            return
        bus = get_session_bus()
        if bus is not None:
            try:
                mgr = dbus.Interface(
                    bus.get_object(OBEX_SERVICE, '/org/bluez/obex'),
                    OBEX_IFACE_AGENT_MANAGER
                )
                mgr.UnregisterAgent(OBEX_AGENT_PATH)
            except dbus.exceptions.DBusException as e:
                logger.debug(f"注销 OBEX Agent 失败: {e}")
        try:
            _obex_agent.remove_from_connection()
        except Exception as e:
            logger.debug(f"移除 OBEX Agent 连接失败: {e}")
        _obex_agent = None
        _obex_agent_registered = False
        logger.info("OBEX Agent 已注销")


def is_obex_agent_ready():
    # 只读查询 OBEX Agent 是否已注册且 obexd 仍在会话总线上(不触发注册)
    with _obex_agent_lock:
        if not (_obex_agent_registered and _obex_agent is not None):
            return False
        bus = get_session_bus()
        if bus is None:
            return False
        try:
            bus.get_object(OBEX_SERVICE, '/org/bluez/obex').Introspect(
                dbus_interface='org.freedesktop.DBus.Introspectable'
            )
            return True
        except dbus.exceptions.DBusException:
            return False


_agent_manager = None
_agent_lock = threading.Lock()
_agent_registered = False

def ensure_agent():
    global _agent_manager, _agent_registered
    from bluetooth_manager import _get_system_bus, BLUEZ_SERVICE, BLUEZ_IFACE_AGENT_MANAGER

    with _agent_lock:
        if _agent_registered and _agent_manager is not None:
            try:
                bus = _get_system_bus()
                if bus is not None:
                    agent_manager_obj = bus.get_object('org.bluez', '/org/bluez')
                    agent_manager_obj.Introspect(dbus_interface='org.freedesktop.DBus.Introspectable')
                    return True
            except dbus.exceptions.DBusException:
                logger.warning("BlueZ 服务可能已重启，Agent 失效，重新注册...")
                _agent_registered = False
                _agent_manager = None
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                _ensure_glib_loop()
                bus = _get_system_bus()
                if bus is None:
                    return False
                if _agent_manager is not None:
                    try:
                        _agent_manager.remove_from_connection()
                    except Exception as e:
                        logger.debug(f"注销旧 Agent 失败: {e}")
                    _agent_manager = None
                agent_obj = _PersistentAgent(bus, '/pipebridge/agent')
                agent_mgr = dbus.Interface(
                    bus.get_object(BLUEZ_SERVICE, '/org/bluez'),
                    BLUEZ_IFACE_AGENT_MANAGER
                )
                agent_mgr.RegisterAgent('/pipebridge/agent', 'KeyboardDisplay')
                agent_mgr.RequestDefaultAgent('/pipebridge/agent')
                _agent_manager = agent_obj
                _agent_registered = True
                logger.info("持久蓝牙 Agent 已注册，可处理入站连接")
                return True
            except dbus.exceptions.DBusException as e:
                error_msg = str(e)
                logger.warning(f"注册持久 Agent 失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                if _agent_manager is not None:
                    try:
                        _agent_manager.remove_from_connection()
                    except Exception as e:
                        logger.debug(f"注销旧 Agent 失败: {e}")
                    _agent_manager = None
                
                if 'UnknownMethod' in error_msg and attempt < max_retries - 1:
                    logger.debug("BlueZ 服务可能正在重启，等待 2 秒后重试...")
                    time.sleep(2)
                    continue
                else:
                    break
        
        logger.error(f"注册持久 Agent 失败，已重试 {max_retries} 次")
        return False

def release_agent():
    global _agent_manager, _agent_registered
    from bluetooth_manager import _get_system_bus, BLUEZ_SERVICE, BLUEZ_IFACE_AGENT_MANAGER

    with _agent_lock:
        if _agent_manager is None:
            return
        try:
            bus = _get_system_bus()
            agent_mgr = dbus.Interface(
                bus.get_object(BLUEZ_SERVICE, '/org/bluez'),
                BLUEZ_IFACE_AGENT_MANAGER
            )
            agent_mgr.UnregisterAgent('/pipebridge/agent')
        except dbus.exceptions.DBusException as e:
            logger.debug(f"注销 Agent 失败: {e}")
        try:
            _agent_manager.remove_from_connection()
        except Exception as e:
            logger.debug(f"移除 Agent 连接失败: {e}")
        _agent_manager = None
        _agent_registered = False
        logger.info("持久蓝牙 Agent 已注销")

def _dbus_pair_device(mac, pin=None, timeout=30):
    from bluetooth_manager import (
        _find_device_path, _get_property, _set_property,
        BLUEZ_IFACE_DEVICE, _translate_pairing_error,
    )

    logger.debug(f"[配对] 开始配对 {mac}, PIN={'有' if pin else '无'}, 超时={timeout}s")

    if not ensure_agent():
        logger.error(f"[配对] {mac} Agent 未注册，无法配对")
        raise CommandError('蓝牙 Agent 未注册，无法配对')

    device_path = _find_device_path(mac)
    if not device_path:
        logger.error(f"[配对] {mac} 未在 D-Bus managed objects 中找到")
        raise DeviceNotFoundError(f'设备 {mac} 未找到，请先扫描')

    pre_paired = False
    pre_connected = False
    pre_alias = mac
    try:
        pre_paired = bool(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Paired'))
        pre_connected = bool(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Connected'))
        pre_alias = str(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias')) or mac
    except dbus.exceptions.DBusException as e:
        logger.debug(f"获取设备配对前状态失败: {e}")
    logger.debug(f"[配对] {mac} 配对前状态: alias={pre_alias}, paired={pre_paired}, connected={pre_connected}")

    with _agent_lock:
        if _agent_manager is not None:
            _agent_manager.set_pairing_pin(pin)
            logger.debug(f"[配对] {mac} PIN 码已设置: {'****' if pin else '无PIN'}")
        else:
            logger.error(f"[配对] {mac} Agent Manager 不可用")
            raise CommandError('蓝牙 Agent 不可用')

    try:
        cmd = f"dbus-send --system --print-reply --dest=org.bluez {shlex.quote(device_path)} {BLUEZ_IFACE_DEVICE}.Pair"
        logger.debug(f"[配对] {mac} 执行 dbus-send Pair, device_path={device_path}")
        result = run_command(cmd, timeout=timeout)
        output = result.get('stdout', '') + result.get('stderr', '')
        logger.debug(f"[配对] {mac} dbus-send 返回: success={result.get('success')}, returncode={result.get('returncode')}, output={output[:500]}")

        if result.get('success', False) or 'method return' in output:
            logger.debug(f"[配对] {mac} 配对成功 (dbus-send method return)")
            try:
                _set_property(BLUEZ_IFACE_DEVICE, device_path, 'Trusted', dbus.Boolean(True))
            except dbus.exceptions.DBusException as e:
                logger.debug(f"设置Trusted属性失败: {e}")
            alias = mac
            try:
                alias = str(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias')) or mac
            except dbus.exceptions.DBusException as e:
                logger.debug(f"获取设备别名失败: {e}")
            return {
                'data': f'设备 {alias} 配对成功',
                'output': '',
                'device_name': alias
            }

        error_name = ''
        if 'Error' in output:
            m = re.search(r'Error\.(\w+)', output)
            if m:
                error_name = m.group(1)

        if 'AlreadyExists' in output or error_name == 'AlreadyExists':
            logger.debug(f"[配对] {mac} 设备已配对 (AlreadyExists)")
            alias = mac
            try:
                alias = str(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias')) or mac
            except dbus.exceptions.DBusException as e:
                logger.debug(f"获取已配对设备别名失败: {e}")
            return {
                'data': f'设备 {alias} 已配对',
                'output': '',
                'device_name': alias
            }

        if 'AuthenticationFailed' in output or error_name == 'AuthenticationFailed':
            logger.warning(f"[配对] {mac} 认证失败 (AuthenticationFailed), PIN提供={'是' if pin else '否'}")
            alias = mac
            try:
                alias = str(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias')) or mac
            except dbus.exceptions.DBusException as e:
                logger.debug(f"获取认证失败设备别名失败: {e}")
            if not pin:
                raise PairingNeedPinError('需要输入PIN码', device_name=alias)
            raise CommandError('配对验证失败，PIN码可能不正确')

        if 'InProgress' in output or error_name == 'InProgress':
            logger.debug(f"[配对] {mac} 配对正在进行中 (InProgress)")
            raise CommandError('配对正在进行中，请稍后重试')

        logger.warning(f"[配对] {mac} 未识别的 dbus-send 结果, error_name={error_name}, 尝试检查实际配对状态...")
        try:
            actually_paired = bool(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Paired'))
            if actually_paired:
                logger.debug(f"[配对] {mac} dbus-send 报错但实际已配对，视为成功")
                alias = mac
                try:
                    alias = str(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias')) or mac
                except dbus.exceptions.DBusException as e:
                    logger.debug(f"获取设备别名失败: {e}")
                return {
                    'data': f'设备 {alias} 配对成功',
                    'output': '',
                    'device_name': alias
                }
        except dbus.exceptions.DBusException as e:
            logger.debug(f"检查实际配对状态失败: {e}")

        error_msg = _translate_pairing_error(output)
        logger.error(f"[配对] {mac} 配对失败: {error_msg}, 原始输出: {output[:300]}")
        raise CommandError(error_msg)

    except (PairingNeedPinError, CommandError):
        raise
    except Exception as e:
        logger.error(f"[配对] {mac} 配对异常: {e}", exc_info=True)
        raise CommandError(f'配对失败: {str(e)[:200]}')

    finally:
        with _agent_lock:
            if _agent_manager is not None:
                _agent_manager.clear_pairing_pin()

def _quick_discover_device(mac):
    # 连接前快速扫描：委托 bluetooth_manager 的公共发现逻辑，统一扫描/超时/清理行为
    from bluetooth_manager import _discover_device_until_found
    return _discover_device_until_found(mac, timeout=8.0)

def _connect_device_interactive(mac):
    from bluetooth_manager import (
        _find_device_path, _get_property, _get_system_bus,
        _translate_connection_error, BLUEZ_IFACE_DEVICE, BLUEZ_SERVICE,
    )

    logger.debug(f"[连接] 开始连接 {mac}")

    if not _find_device_path(mac):
        logger.debug(f"[连接] {mac} 尚未发现，自动快速扫描...")
        found = _quick_discover_device(mac)
        if not found:
            logger.error(f"[连接] {mac} 快速扫描后仍未发现")
            raise DeviceNotFoundError(f'设备 {mac} 未找到，请先扫描')
    else:
        logger.debug(f"[连接] {mac} 已在 D-Bus 中发现")

    audio_ready, audio_detail = _cached_ensure_bluetooth_audio_ready()
    if not audio_ready:
        logger.error(f"[连接] {mac} 蓝牙音频预检失败: {audio_detail}")
        raise ProfileUnavailableError(
            f'蓝牙音频环境未就绪，无法连接。诊断: {audio_detail}。请检查 WirePlumber 服务和 libspa-0.2-bluetooth 包',
            device_name=mac
        )
    logger.debug(f"[连接] {mac} 蓝牙音频预检通过: {audio_detail}")

    try:
        bus = _get_system_bus()
        device_path = _find_device_path(mac)
        if not device_path:
            raise DeviceNotFoundError(f'设备 {mac} 未找到')
        device = dbus.Interface(bus.get_object(BLUEZ_SERVICE, device_path), BLUEZ_IFACE_DEVICE)

        # 连接前门控：确保适配器已上电，避免在未上电适配器上 Connect 触发 NoReply 超时。
        # 去抖：Powered 属性在 BlueZ 忙碌/D-Bus 抖动时可能瞬时读到 False，若据此立即调用
        # _power_on_adapter，其失败兜底会触发 USB/深层复位导致蓝牙反复重启。故此处先多次重读
        # Powered，仅当持续为 False 时才轻量上电(set Powered=True，不触发复位链路)，仍失败
        # 才回退到完整 _power_on_adapter，把重枚举级复位限制在真正硬件异常场景。
        try:
            from bluetooth_manager import (
                _find_adapter_path, _get_property as _get_prop_mgr,
                _set_property as _set_prop_mgr, _power_on_adapter, BLUEZ_IFACE_ADAPTER,
            )

            def _read_powered():
                ap = _find_adapter_path()
                if not ap:
                    return None, False
                try:
                    return ap, bool(_get_prop_mgr(BLUEZ_IFACE_ADAPTER, ap, 'Powered'))
                except dbus.exceptions.DBusException:
                    return ap, False

            adapter_path, powered = _read_powered()
            # 去抖重读：最多 3 次，间隔 0.3s，任一读到 True 即视为已上电
            for _ in range(2):
                if powered or adapter_path is None:
                    break
                time.sleep(0.3)
                adapter_path, powered = _read_powered()

            if not powered:
                logger.debug(f"[连接] {mac} 适配器未上电，连接前先执行轻量上电...")
                # 优先轻量上电：仅设置 Powered=True，绝不触发 reset 链路
                if adapter_path is not None:
                    try:
                        _set_prop_mgr(BLUEZ_IFACE_ADAPTER, adapter_path, 'Powered', dbus.Boolean(True))
                        time.sleep(0.5)
                        _, powered = _read_powered()
                    except dbus.exceptions.DBusException as e:
                        logger.debug(f"[连接] {mac} 轻量上电失败(将回退完整上电): {e}")
                # 仅当轻量上电仍失败(真硬件异常/无适配器)才走完整 _power_on_adapter
                if not powered:
                    logger.debug(f"[连接] {mac} 轻量上电未生效，回退完整上电流程...")
                    if _power_on_adapter():
                            logger.debug(f"[连接] {mac} 适配器上电完成")
                    else:
                        logger.warning(f"[连接] {mac} 连接前上电失败，仍尝试继续连接")
                # 上电后设备路径可能刷新，重新解析
                refreshed = _find_device_path(mac)
                if refreshed:
                    device_path = refreshed
                    device = dbus.Interface(bus.get_object(BLUEZ_SERVICE, device_path), BLUEZ_IFACE_DEVICE)
        except Exception as e:
            logger.warning(f"[连接] {mac} 连接前 Powered 门控检查异常(忽略): {e}")

        pre_props = {}
        try:
            pre_props['Paired'] = bool(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Paired'))
            pre_props['Connected'] = bool(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Connected'))
            pre_props['Alias'] = str(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias'))
            pre_props['UUIDs'] = list(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'UUIDs') or [])
            logger.debug(f"[连接] {mac} 连接前属性: {pre_props}")
        except dbus.exceptions.DBusException as e:
            logger.warning(f"[连接] {mac} 读取连接前属性失败: {e}")

        connect_result = [None]
        connect_error = [None]
        def _do_connect():
            try:
                logger.debug(f"[连接] {mac} 调用 device.Connect()...")
                device.Connect()
                connect_result[0] = True
                logger.debug(f"[连接] {mac} device.Connect() 返回成功")
            except Exception as e:
                connect_error[0] = e
                logger.warning(f"[连接] {mac} device.Connect() 抛出异常: {e}")
        t = threading.Thread(target=_do_connect, daemon=True)
        t.start()
        t.join(timeout=15)
        if t.is_alive():
            logger.debug(f"[连接] {mac} Connect 调用超时(15s)，继续轮询等待连接结果")
        elif connect_error[0]:
            raise connect_error[0]

        conn_start = time.time()
        while time.time() - conn_start < 20:
            time.sleep(0.5)
            if connect_error[0] is not None:
                logger.warning(f"[连接] {mac} 子线程 Connect 异常: {connect_error[0]}")
                raise connect_error[0]
            try:
                connected = _get_property(BLUEZ_IFACE_DEVICE, device_path, 'Connected')
                if connected:
                    alias = mac
                    try:
                        alias = str(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias'))
                    except Exception as e:
                        logger.debug(f"获取连接成功设备别名失败: {e}")
                    logger.info(f"[连接] {mac} 连接成功, alias={alias}, 耗时={time.time()-conn_start:.1f}s")
                    return {'data': f'设备 {alias} 连接成功', 'output': '', 'device_name': alias}
            except dbus.exceptions.DBusException as e:
                logger.debug(f"检查连接状态失败: {e}")

        logger.error(f"[连接] {mac} 连接超时 (20s)")
        raise CommandError('连接超时')

    except dbus.exceptions.DBusException as e:
        error_msg = str(e)
        logger.warning(f"[连接] {mac} D-Bus 异常: {error_msg}")

        if 'already' in error_msg.lower() and 'connected' in error_msg.lower():
            logger.info(f"[连接] {mac} 设备已连接 (already connected)")
            alias = mac
            try:
                dp = _find_device_path(mac)
                if dp:
                    alias = str(_get_property(BLUEZ_IFACE_DEVICE, dp, 'Alias'))
            except Exception as e:
                logger.debug(f"获取已连接设备别名失败: {e}")
            return {'data': f'设备 {alias} 已连接', 'output': '', 'device_name': alias}
        if 'profile-unavailable' in error_msg or 'br-connection-profile' in error_msg:
            wp_recheck = run_command("pgrep -x wireplumber 2>/dev/null")
            wp_running = bool(wp_recheck['success'] and wp_recheck['stdout'].strip())
            from bluetooth_manager import check_bluetooth_audio_ready
            endpoint_ok = check_bluetooth_audio_ready()
            diag = f"WirePlumber运行={'是' if wp_running else '否'}, MediaEndpoint1={'已注册' if endpoint_ok else '未注册'}"
            logger.error(f"[连接] {mac} profile-unavailable: {diag}, 完整错误: {error_msg}")
            raise ProfileUnavailableError(
                f'蓝牙音频 profile 不可用。诊断: {diag}。预检已通过但 BlueZ 仍拒绝，可能是设备 profile 不匹配或 WirePlumber 端点不完整，建议重启 WirePlumber 或重新部署蓝牙配置',
                device_name=mac
            )
        logger.error(f"[连接] {mac} 连接失败: {_translate_connection_error(error_msg)}, 原始错误: {error_msg}")
        raise CommandError(_translate_connection_error(error_msg))

def _pair_device_interactive(mac, pin=None):
    from bluetooth_manager import _pairing_lock
    with _pairing_lock:
        return _dbus_pair_device(mac, pin=pin)
