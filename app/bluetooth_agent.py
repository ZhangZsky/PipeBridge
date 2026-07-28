"""蓝牙配对 Agent —— D-Bus Agent 实现 + 配对/连接交互流程

从 bluetooth_manager 拆分而来，包含：
- GLib 主循环管理（D-Bus Agent 方法调用依赖）
- 持久 Agent 实现（自动接受 SSP 配对、支持临时 PIN）
- Agent 注册/注销
- 配对/连接交互流程（dbus-send 调用 Pair/Connect）

依赖 bluetooth_manager 提供的常量和 D-Bus 工具函数。
"""

import re
import time
import shlex
import logging
import threading

import dbus
import dbus.service

from utils import run_command, pw_dump, start_pw_service, _pw_socket_exists
import platform_paths
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError, PairingNeedPinError, ProfileUnavailableError

logger = logging.getLogger('PipeBridge')

# 音频就绪预检缓存（避免并发连接时重复检查 WirePlumber）
_audio_ready_cache = {'ready': False, 'detail': '', 'time': 0}
_AUDIO_READY_CACHE_TTL = 5  # 秒


def _cached_ensure_bluetooth_audio_ready():
    """带 TTL 缓存的蓝牙音频就绪检查，避免并发连接时重复检查"""
    now = time.time()
    if now - _audio_ready_cache['time'] < _AUDIO_READY_CACHE_TTL:
        return _audio_ready_cache['ready'], _audio_ready_cache['detail']

    from bluetooth_manager import check_bluetooth_audio_ready
    ready = check_bluetooth_audio_ready()
    detail = 'WirePlumber 蓝牙音频模块已就绪' if ready else 'WirePlumber 蓝牙音频端点未注册'
    _audio_ready_cache['ready'] = ready
    _audio_ready_cache['detail'] = detail
    _audio_ready_cache['time'] = now
    return ready, detail


# ============================================================================
# GLib 主循环管理
# ============================================================================

# GLib 主循环（后台线程运行，用于派发 D-Bus Agent 方法调用）
_glib_loop = None
_glib_loop_thread = None


# 确保 GLib 主循环在后台线程中运行，D-Bus Agent 方法调用依赖它
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


# ============================================================================
# 蓝牙 Agent 实现
# ============================================================================

# 蓝牙 Agent 基类
class _BaseBluezAgent(dbus.service.Object):
    # 初始化 Agent
    def __init__(self, bus, path):
        dbus.service.Object.__init__(self, bus, path)

    # Agent 释放回调
    @dbus.service.method('org.bluez.Agent1', in_signature='', out_signature='')
    def Release(self):
        logger.debug("Agent Release 被调用")

    # 显示 PIN 码回调
    @dbus.service.method('org.bluez.Agent1', in_signature='os', out_signature='')
    def DisplayPinCode(self, device, pin_code):
        logger.debug(f"Agent DisplayPinCode: device={device}, pin={pin_code}")

    # 显示 Passkey 回调
    @dbus.service.method('org.bluez.Agent1', in_signature='ouq', out_signature='')
    def DisplayPasskey(self, device, passkey, entered):
        logger.debug(f"Agent DisplayPasskey: device={device}, passkey={passkey}, entered={entered}")

    # 授权服务回调（自动允许，确保 A2DP/HFP 等配对后服务可用）
    @dbus.service.method('org.bluez.Agent1', in_signature='os', out_signature='')
    def AuthorizeService(self, device, uuid):
        logger.debug(f"Agent AuthorizeService: device={device}, uuid={uuid} (自动授权)")

    # 取消配对回调
    @dbus.service.method('org.bluez.Agent1', in_signature='', out_signature='')
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
    @dbus.service.method('org.bluez.Agent1', in_signature='o', out_signature='s')
    def RequestPinCode(self, device):
        pin = self._pairing_pin or '0000'
        logger.debug(f"持久Agent RequestPinCode: device={device}, pin={'***' if self._pairing_pin else '0000'}")
        return pin

    # 请求数字 Passkey：优先使用用户 PIN，否则返回 0
    @dbus.service.method('org.bluez.Agent1', in_signature='o', out_signature='u')
    def RequestPasskey(self, device):
        key = int(self._pairing_pin) if self._pairing_pin and self._pairing_pin.isdigit() else 0
        logger.debug(f"持久Agent RequestPasskey: device={device}, key={'***' if self._pairing_pin else '0'}")
        return dbus.UInt32(key)

    # 自动确认 SSP 配对
    @dbus.service.method('org.bluez.Agent1', in_signature='ou', out_signature='')
    def RequestConfirmation(self, device, passkey):
        logger.debug(f"持久Agent RequestConfirmation: device={device}, passkey={passkey} (自动确认)")


# ============================================================================
# Agent 注册/注销
# ============================================================================

_agent_manager = None
_agent_lock = threading.Lock()
_agent_registered = False


# 注册持久蓝牙 Agent（确保 GLib 主循环运行后再注册）
def ensure_agent():
    global _agent_manager, _agent_registered
    # 延迟导入 bluetooth_manager 中的 D-Bus 工具函数
    from bluetooth_manager import _get_system_bus, BLUEZ_SERVICE, BLUEZ_IFACE_AGENT_MANAGER

    with _agent_lock:
        # 健康检查：已注册时验证 Agent 是否仍为默认 Agent
        if _agent_registered and _agent_manager is not None:
            try:
                # 验证 BlueZ 仍可访问 Agent 路径
                bus = _get_system_bus()
                if bus is not None:
                    # 尝试获取 AgentManager，失败说明 BlueZ 已重启
                    agent_manager_obj = bus.get_object('org.bluez', '/org/bluez')
                    agent_manager_obj.Introspect(dbus_interface='org.freedesktop.DBus.Introspectable')
                    return True
            except dbus.exceptions.DBusException:
                logger.warning("BlueZ 服务可能已重启，Agent 失效，重新注册...")
                _agent_registered = False
                _agent_manager = None
        
        # 重试注册逻辑（最多 3 次）
        max_retries = 3
        for attempt in range(max_retries):
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
                # 尝试清理残留状态
                if _agent_manager is not None:
                    try:
                        _agent_manager.remove_from_connection()
                    except Exception:
                        pass
                    _agent_manager = None
                
                # 如果是 UnknownMethod 错误，说明 BlueZ 刚重启，等待后重试
                if 'UnknownMethod' in error_msg and attempt < max_retries - 1:
                    logger.info("BlueZ 服务可能正在重启，等待 2 秒后重试...")
                    time.sleep(2)
                    continue
                else:
                    # 其他错误或最后一次尝试失败
                    break
        
        logger.error(f"注册持久 Agent 失败，已重试 {max_retries} 次")
        return False


# 注销持久蓝牙 Agent
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
        except dbus.exceptions.DBusException:
            pass
        try:
            _agent_manager.remove_from_connection()
        except Exception:
            pass
        _agent_manager = None
        _agent_registered = False
        logger.info("持久蓝牙 Agent 已注销")


# ============================================================================
# 配对/连接交互流程
# ============================================================================

# 通过 dbus-send 调用 Pair()，持久 Agent 处理认证回调
def _dbus_pair_device(mac, pin=None, timeout=30):
    from bluetooth_manager import (
        _find_device_path, _get_property, _set_property,
        BLUEZ_IFACE_DEVICE, _translate_pairing_error,
    )

    logger.info(f"[配对] 开始配对 {mac}, PIN={'有' if pin else '无'}, 超时={timeout}s")

    # 确保 Agent 已注册且为默认
    if not ensure_agent():
        logger.error(f"[配对] {mac} Agent 未注册，无法配对")
        raise CommandError('蓝牙 Agent 未注册，无法配对')

    device_path = _find_device_path(mac)
    if not device_path:
        logger.error(f"[配对] {mac} 未在 D-Bus managed objects 中找到")
        raise DeviceNotFoundError(f'设备 {mac} 未找到，请先扫描')

    # 读取配对前的设备状态
    pre_paired = False
    pre_connected = False
    pre_alias = mac
    try:
        pre_paired = bool(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Paired'))
        pre_connected = bool(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Connected'))
        pre_alias = str(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias')) or mac
    except dbus.exceptions.DBusException:
        pass
    logger.info(f"[配对] {mac} 配对前状态: alias={pre_alias}, paired={pre_paired}, connected={pre_connected}")

    # 设置临时 PIN 码（Agent 回调会读取）
    with _agent_lock:
        if _agent_manager is not None:
            _agent_manager.set_pairing_pin(pin)
            logger.info(f"[配对] {mac} PIN 码已设置: {'****' if pin else '无PIN'}")
        else:
            logger.error(f"[配对] {mac} Agent Manager 不可用")
            raise CommandError('蓝牙 Agent 不可用')

    try:
        # 使用 dbus-send 调用 Pair()，独立进程不影响 GLib 主循环
        # dbus-send 不会注册自己的 agent，持久 Agent 始终处理认证回调
        cmd = f"dbus-send --system --print-reply --dest=org.bluez {shlex.quote(device_path)} {BLUEZ_IFACE_DEVICE}.Pair"
        logger.info(f"[配对] {mac} 执行 dbus-send Pair, device_path={device_path}")
        result = run_command(cmd, timeout=timeout)
        output = result.get('stdout', '') + result.get('stderr', '')
        logger.info(f"[配对] {mac} dbus-send 返回: success={result.get('success')}, returncode={result.get('returncode')}, output={output[:500]}")

        # dbus-send 成功返回表示配对成功
        if result.get('success', False) or 'method return' in output:
            logger.info(f"[配对] {mac} 配对成功 (dbus-send method return)")
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
            m = re.search(r'Error\.(\w+)', output)
            if m:
                error_name = m.group(1)

        # 已配对
        if 'AlreadyExists' in output or error_name == 'AlreadyExists':
            logger.info(f"[配对] {mac} 设备已配对 (AlreadyExists)")
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
            logger.warning(f"[配对] {mac} 认证失败 (AuthenticationFailed), PIN提供={'是' if pin else '否'}")
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
            logger.info(f"[配对] {mac} 配对正在进行中 (InProgress)")
            raise CommandError('配对正在进行中，请稍后重试')

        # 其他错误 - 先检查设备是否实际已配对
        logger.warning(f"[配对] {mac} 未识别的 dbus-send 结果, error_name={error_name}, 尝试检查实际配对状态...")
        try:
            actually_paired = bool(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Paired'))
            if actually_paired:
                logger.info(f"[配对] {mac} dbus-send 报错但实际已配对，视为成功")
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
        logger.error(f"[配对] {mac} 配对失败: {error_msg}, 原始输出: {output[:300]}")
        raise CommandError(error_msg)

    except (PairingNeedPinError, CommandError):
        raise
    except Exception as e:
        logger.error(f"[配对] {mac} 配对异常: {e}", exc_info=True)
        raise CommandError(f'配对失败: {str(e)[:200]}')

    finally:
        # 清除临时 PIN
        with _agent_lock:
            if _agent_manager is not None:
                _agent_manager.clear_pairing_pin()


# 快速扫描使设备出现在 BlueZ managed objects 中
def _quick_discover_device(mac):
    from bluetooth_manager import _find_all_adapter_paths, _get_object, _find_device_path, BLUEZ_IFACE_ADAPTER

    adapters = _find_all_adapter_paths()
    if not adapters:
        return False
    try:
        adapter = dbus.Interface(_get_object(adapters[0]), BLUEZ_IFACE_ADAPTER)
        adapter.StartDiscovery()
        try:
            # 等待设备出现（最多8秒）
            for _ in range(16):
                time.sleep(0.5)
                if _find_device_path(mac):
                    return True
            return False
        finally:
            try:
                adapter.StopDiscovery()
            except dbus.exceptions.DBusException:
                pass
    except dbus.exceptions.DBusException as e:
        logger.debug(f"快速扫描失败: {e}")
    return False


def _connect_device_interactive(mac):
    from bluetooth_manager import (
        _find_device_path, _get_property, _get_system_bus,
        _translate_connection_error, BLUEZ_IFACE_DEVICE, BLUEZ_SERVICE,
    )

    logger.info(f"[连接] 开始连接 {mac}")

    # 设备未发现时先快速扫描
    if not _find_device_path(mac):
        logger.info(f"[连接] {mac} 尚未发现，自动快速扫描...")
        found = _quick_discover_device(mac)
        if not found:
            logger.error(f"[连接] {mac} 快速扫描后仍未发现")
            raise DeviceNotFoundError(f'设备 {mac} 未找到，请先扫描')
    else:
        logger.info(f"[连接] {mac} 已在 D-Bus 中发现")

    # 连接不需要自定义 Agent，使用持久 Agent 即可
    # 连接前预检蓝牙音频环境，使用缓存避免并发连接时重复检查
    audio_ready, audio_detail = _cached_ensure_bluetooth_audio_ready()
    if not audio_ready:
        logger.error(f"[连接] {mac} 蓝牙音频预检失败: {audio_detail}")
        raise ProfileUnavailableError(
            f'蓝牙音频环境未就绪，无法连接。诊断: {audio_detail}。请检查 WirePlumber 服务和 libspa-0.2-bluetooth 包',
            device_name=mac
        )
    logger.info(f"[连接] {mac} 蓝牙音频预检通过: {audio_detail}")

    try:
        bus = _get_system_bus()
        device_path = _find_device_path(mac)
        if not device_path:
            raise DeviceNotFoundError(f'设备 {mac} 未找到')
        device = dbus.Interface(bus.get_object(BLUEZ_SERVICE, device_path), BLUEZ_IFACE_DEVICE)

        # 读取连接前的设备属性
        pre_props = {}
        try:
            pre_props['Paired'] = bool(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Paired'))
            pre_props['Connected'] = bool(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Connected'))
            pre_props['Alias'] = str(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias'))
            pre_props['UUIDs'] = list(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'UUIDs') or [])
            logger.info(f"[连接] {mac} 连接前属性: {pre_props}")
        except dbus.exceptions.DBusException as e:
            logger.warning(f"[连接] {mac} 读取连接前属性失败: {e}")

        # 使用线程执行 Connect，避免阻塞
        connect_result = [None]
        connect_error = [None]
        def _do_connect():
            try:
                logger.info(f"[连接] {mac} 调用 device.Connect()...")
                device.Connect()
                connect_result[0] = True
                logger.info(f"[连接] {mac} device.Connect() 返回成功")
            except Exception as e:
                connect_error[0] = e
                logger.warning(f"[连接] {mac} device.Connect() 抛出异常: {e}")
        t = threading.Thread(target=_do_connect, daemon=True)
        t.start()
        t.join(timeout=15)  # 15秒超时
        if t.is_alive():
            # 超时，连接仍在进行中，不阻塞，继续轮询等待结果
            logger.info(f"[连接] {mac} Connect 调用超时(15s)，继续轮询等待连接结果")
        elif connect_error[0]:
            raise connect_error[0]

        # 等待连接完成
        conn_start = time.time()
        while time.time() - conn_start < 20:
            time.sleep(0.5)
            # 子线程已抛出异常时立即抛出，避免丢失真实失败原因
            if connect_error[0] is not None:
                logger.warning(f"[连接] {mac} 子线程 Connect 异常: {connect_error[0]}")
                raise connect_error[0]
            try:
                connected = _get_property(BLUEZ_IFACE_DEVICE, device_path, 'Connected')
                if connected:
                    alias = mac
                    try:
                        alias = str(_get_property(BLUEZ_IFACE_DEVICE, device_path, 'Alias'))
                    except Exception:
                        pass
                    logger.info(f"[连接] {mac} 连接成功, alias={alias}, 耗时={time.time()-conn_start:.1f}s")
                    return {'data': f'设备 {alias} 连接成功', 'output': '', 'device_name': alias}
            except dbus.exceptions.DBusException:
                pass

        logger.error(f"[连接] {mac} 连接超时 (20s)")
        raise CommandError('连接超时')

    except dbus.exceptions.DBusException as e:
        error_msg = str(e)
        logger.warning(f"[连接] {mac} D-Bus 异常: {error_msg}")

        # 已连接
        if 'already' in error_msg.lower() and 'connected' in error_msg.lower():
            logger.info(f"[连接] {mac} 设备已连接 (already connected)")
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
            # 预检已通过但 BlueZ 仍拒绝，可能是设备 profile 不匹配或 WirePlumber 端点不完整
            wp_recheck = run_command("pgrep -x wireplumber 2>/dev/null")
            wp_running = bool(wp_recheck['success'] and wp_recheck['stdout'].strip())
            endpoint_ok = check_bluetooth_audio_ready()
            diag = f"WirePlumber运行={'是' if wp_running else '否'}, MediaEndpoint1={'已注册' if endpoint_ok else '未注册'}"
            logger.error(f"[连接] {mac} profile-unavailable: {diag}, 完整错误: {error_msg}")
            raise ProfileUnavailableError(
                f'蓝牙音频 profile 不可用。诊断: {diag}。预检已通过但 BlueZ 仍拒绝，可能是设备 profile 不匹配或 WirePlumber 端点不完整，建议重启 WirePlumber 或重新部署蓝牙配置',
                device_name=mac
            )
        logger.error(f"[连接] {mac} 连接失败: {_translate_connection_error(error_msg)}, 原始错误: {error_msg}")
        raise CommandError(_translate_connection_error(error_msg))


# 交互式配对设备（使用 D-Bus Pair()，不再释放/重建 Agent）
def _pair_device_interactive(mac, pin=None):
    from bluetooth_manager import _pairing_lock
    with _pairing_lock:  # 串行化配对，保证 PIN 不被并发操作覆盖
        return _dbus_pair_device(mac, pin=pin)
