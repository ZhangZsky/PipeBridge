import logging
from fastapi import APIRouter, Body
import bluetooth_manager
import system_manager
import route_manager
from exceptions import InvalidParamError, CommandError
from routes.helpers import _json
from utils import run_command
import platform_paths
from event_system import event_bus

logger = logging.getLogger('PipeBridge')

router = APIRouter(tags=["system"])

@router.get('/api/system/overview')
def system_overview():
    logger.debug("获取系统概览")
    return _json(system_manager.get_system_overview())

@router.get('/api/system/dependencies')
def system_dependencies():
    return _json(system_manager.get_all_status())

@router.post('/api/system/fix')
def system_fix():
    logger.info("一键修复系统依赖")
    result = system_manager.fix_all()
    pkg_ok = 'error' not in result.get('packages', {})
    pw_ok = 'error' not in result.get('pipewire', {})
    svc_ok = 'error' not in result.get('services', {})
    bt_audio_ok = 'error' not in result.get('bluetooth_audio', {})
    overall = pkg_ok and pw_ok and svc_ok and bt_audio_ok
    logger.info(f"修复完成: packages={pkg_ok}, pipewire={pw_ok}, services={svc_ok}, bluetooth_audio={bt_audio_ok}")
    if not overall:
        raise CommandError('部分修复失败')
    event_bus.publish('system.changed', {})
    return _json(result)

@router.post('/api/system/reconnect')
def system_reconnect(data: dict = Body(...)):
    enabled = data.get('enabled')
    if enabled is None:
        raise InvalidParamError("enabled field is required")
    bluetooth_manager.set_reconnect_enabled(bool(enabled))
    event_bus.publish('bluetooth.changed', {})
    return _json({"message": "ok"})

@router.get('/api/health')
def health_check():
    overview = system_manager.get_system_overview()
    health = {
        'pipewire': overview.get('pipewire', False),
        'pipewire_pulse': overview.get('pipewire_pulse', False),
        'wireplumber': overview.get('wireplumber', False),
        'dbus': overview.get('dbus', False),
        'bluetooth_service': overview.get('bluetooth_service', False),
        'bluetooth_audio_ready': overview.get('bluetooth_audio_ready', False),
        'spa_bluetooth_plugin': overview.get('spa_bluetooth_plugin', False),
        'dependencies_ok': overview.get('dependencies', {}).get('all_ok', False),
    }
    all_ok = all(health.values())
    return _json({'healthy': all_ok, 'checks': health})

@router.get('/api/pipewire/links')
def pipewire_links():
    return _json(route_manager.get_all_links())

_CONTROLLABLE_SERVICES = {
    'bluetooth': '蓝牙服务',
    'dbus': 'D-Bus 系统消息总线',
}

@router.post('/api/system/service/restart')
def system_service_restart(data: dict = Body(...)):
    service = data.get('service')
    if not service:
        raise InvalidParamError("service 参数必填")
    if service not in _CONTROLLABLE_SERVICES:
        raise InvalidParamError(f"不支持的服务: {service}，可选: {', '.join(_CONTROLLABLE_SERVICES.keys())}")

    # 蓝牙服务重启前可选执行 USB 适配器硬件重置，解决适配器卡死时单纯 systemctl restart 无效的问题
    usb_reset_done = False
    if service == 'bluetooth' and data.get('usb_reset'):
        from bluetooth_manager import _try_usb_reset_adapter
        logger.info("重启蓝牙服务前执行 USB 适配器重置...")
        try:
            usb_reset_done = _try_usb_reset_adapter()
        except Exception as e:
            logger.warning(f"USB 适配器重置失败(继续重启服务): {e}")
        import time as _time
        _time.sleep(2)

    logger.info(f"重启服务: {service} (usb_reset={usb_reset_done})")
    result = run_command(f"{platform_paths.CMD_SYSTEMCTL} restart {service}", timeout=30)
    if not result['success']:
        raise CommandError(f"重启 {service} 失败: {result.get('stderr', '')[:200]}")
    msg = f"{_CONTROLLABLE_SERVICES[service]}已重启"
    if usb_reset_done:
        msg += "（含 USB 适配器重置）"
    event_bus.publish('system.changed', {})
    return _json({"message": msg, "usb_reset": usb_reset_done})

@router.post('/api/system/service/start')
def system_service_start(data: dict = Body(...)):
    service = data.get('service')
    if not service:
        raise InvalidParamError("service 参数必填")
    if service not in _CONTROLLABLE_SERVICES:
        raise InvalidParamError(f"不支持的服务: {service}，可选: {', '.join(_CONTROLLABLE_SERVICES.keys())}")
    logger.info(f"启动服务: {service}")
    result = run_command(f"{platform_paths.CMD_SYSTEMCTL} start {service}", timeout=30)
    if not result['success']:
        raise CommandError(f"启动 {service} 失败: {result.get('stderr', '')[:200]}")
    event_bus.publish('system.changed', {})
    return _json({"message": f"{_CONTROLLABLE_SERVICES[service]}已启动"})

@router.post('/api/system/service/stop')
def system_service_stop(data: dict = Body(...)):
    service = data.get('service')
    if not service:
        raise InvalidParamError("service 参数必填")
    if service not in _CONTROLLABLE_SERVICES:
        raise InvalidParamError(f"不支持的服务: {service}，可选: {', '.join(_CONTROLLABLE_SERVICES.keys())}")
    logger.info(f"停止服务: {service}")
    result = run_command(f"{platform_paths.CMD_SYSTEMCTL} stop {service}", timeout=30)
    if not result['success']:
        raise CommandError(f"停止 {service} 失败: {result.get('stderr', '')[:200]}")
    event_bus.publish('system.changed', {})
    return _json({"message": f"{_CONTROLLABLE_SERVICES[service]}已停止"})
