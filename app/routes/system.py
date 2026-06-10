import logging
from fastapi import APIRouter, Body
import bluetooth_manager
import dependency_checker
import route_manager
from exceptions import InvalidParamError, CommandError
from routes.helpers import _json

logger = logging.getLogger('MediaHub')

router = APIRouter(tags=["system"])


@router.get('/api/system/overview')
def system_overview():
    logger.debug("获取系统概览")
    return _json(dependency_checker.get_system_overview())


@router.get('/api/system/dependencies')
def system_dependencies():
    return _json(dependency_checker.get_all_status())


@router.post('/api/system/fix')
def system_fix():
    logger.info("一键修复系统依赖")
    result = dependency_checker.fix_all()
    pkg_ok = 'error' not in result.get('packages', {})
    pw_ok = 'error' not in result.get('pipewire', {})
    svc_ok = 'error' not in result.get('services', {})
    bt_audio_ok = 'error' not in result.get('bluetooth_audio', {})
    overall = pkg_ok and pw_ok and svc_ok and bt_audio_ok
    logger.info(f"修复完成: packages={pkg_ok}, pipewire={pw_ok}, services={svc_ok}, bluetooth_audio={bt_audio_ok}")
    if not overall:
        raise CommandError('部分修复失败')
    return _json(result)


@router.post('/api/system/reconnect')
def system_reconnect(data: dict = Body(...)):
    enabled = data.get('enabled')
    if enabled is None:
        raise InvalidParamError("enabled field is required")
    bluetooth_manager.set_reconnect_enabled(bool(enabled))
    return _json({"message": "ok"})


@router.get('/api/health')
def health_check():
    overview = dependency_checker.get_system_overview()
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
    """获取所有PipeWire链接"""
    return _json(route_manager.get_all_links())
