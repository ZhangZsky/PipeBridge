import logging
from fastapi import APIRouter, Body
import bluetooth_manager
import route_manager
from exceptions import InvalidParamError, PairingNeedPinError
from routes.helpers import _json, _validate_mac, _as_bool
from pipewire_healer import invalidate_pw_ok_cache

logger = logging.getLogger('MediaHub')

router = APIRouter(prefix="/api/bluetooth", tags=["bluetooth"])


@router.get('/status')
def bluetooth_status():
    logger.debug("获取蓝牙状态")
    result = bluetooth_manager.get_bluetooth_status()
    logger.debug(f"蓝牙状态: {result.get('status', 'unknown')}")
    return _json(result)


@router.post('/install')
def bluetooth_install():
    logger.debug("安装蓝牙驱动")
    result = bluetooth_manager.install_bluetooth_driver()
    logger.debug(f"蓝牙驱动安装: {result}")
    return _json(result)


@router.get('/scan')
def bluetooth_scan():
    logger.debug("扫描蓝牙设备")
    result = bluetooth_manager.scan_devices()
    device_count = len(result) if result else 0
    logger.debug(f"扫描完成，发现 {device_count} 个设备")
    return _json(result)


@router.get('/devices')
def bluetooth_devices():
    return _json(bluetooth_manager.get_paired_devices())


@router.post('/pair')
def bluetooth_pair(data: dict = Body(...)):
    mac = data.get('mac')
    _validate_mac(mac)
    logger.debug(f"配对蓝牙设备: {mac}")
    try:
        result = bluetooth_manager.pair_device(mac, pin=data.get('pin'))
    except PairingNeedPinError as e:
        device_name = e.device_name or mac
        logger.debug(f"设备 {mac} 需要PIN码")
        return _json({'success': False, 'data': None}, needs_pin=True, device_name=device_name)
    logger.debug(f"配对结果: 成功, 连接: {result.get('connected', False)}")
    return _json(result, connected=result.get("connected", False), device_name=result.get("device_name", mac))


@router.post('/connect')
def bluetooth_connect(data: dict = Body(...)):
    mac = data.get('mac')
    _validate_mac(mac)
    logger.debug(f"连接蓝牙设备: {mac}")
    result = bluetooth_manager.connect_device(mac)
    # 蓝牙连接可能改变音频设备拓扑，清除 PipeWire 缓存
    invalidate_pw_ok_cache()
    logger.debug(f"连接结果: 成功")
    return _json(result)


@router.post('/disconnect')
def bluetooth_disconnect(data: dict = Body(...)):
    mac = data.get('mac')
    _validate_mac(mac)
    logger.debug(f"断开蓝牙设备: {mac}")
    result = bluetooth_manager.disconnect_device(mac)
    # 蓝牙断开可能改变音频设备拓扑，清除 PipeWire 缓存
    invalidate_pw_ok_cache()
    return _json(result)


@router.post('/remove')
def bluetooth_remove(data: dict = Body(...)):
    mac = data.get('mac')
    _validate_mac(mac)
    logger.debug(f"删除蓝牙设备: {mac}")
    return _json(bluetooth_manager.remove_device(mac))


@router.post('/power')
def bluetooth_power(data: dict = Body(...)):
    power = data.get('power')
    if power is None:
        raise InvalidParamError("电源状态参数必填")
    return _json(bluetooth_manager.set_power(_as_bool(power)))


@router.post('/discoverable')
def bluetooth_discoverable(data: dict = Body(...)):
    discoverable = data.get('discoverable')
    if discoverable is None:
        raise InvalidParamError("可发现状态参数必填")
    return _json(bluetooth_manager.set_discoverable(_as_bool(discoverable)))


@router.post('/alias')
def bluetooth_alias(data: dict = Body(...)):
    mac, alias = data.get('mac'), data.get('alias')
    if not mac or not alias:
        raise InvalidParamError("MAC 地址和别名不能为空")
    _validate_mac(mac)
    return _json(bluetooth_manager.set_device_alias(mac, alias))


@router.post('/keep-alive')
def bluetooth_keep_alive():
    bluetooth_manager.keep_bluetooth_alive()
    connected = bluetooth_manager.check_bluetooth_connections()
    return _json({"connected": connected})


@router.get('/reconnect/status')
def bluetooth_reconnect_status():
    try:
        return _json(bluetooth_manager.get_reconnect_status())
    except Exception as e:
        logger.warning(f"获取重连状态失败: {e}")
        return _json({"monitoring": False, "reconnecting_devices": [], "manual_disconnects": []})


@router.get('/audio-sources')
def bluetooth_audio_sources():
    """列出蓝牙音频输入设备"""
    return _json(bluetooth_manager.get_bluetooth_audio_sources())


@router.get('/audio-profiles/{mac}')
def bluetooth_audio_profiles(mac: str):
    """获取蓝牙设备音频Profile列表"""
    _validate_mac(mac)
    return _json(bluetooth_manager.get_bluetooth_audio_profiles(mac))


@router.post('/audio-profile/switch')
def bluetooth_switch_profile(data: dict = Body(...)):
    """切换蓝牙设备音频Profile"""
    mac = data.get('mac')
    profile = data.get('profile')
    if not mac or not profile:
        raise InvalidParamError("MAC 地址和 Profile 参数必填")
    _validate_mac(mac)
    logger.debug(f"切换蓝牙Profile: {mac} -> {profile}")
    return _json(bluetooth_manager.switch_bluetooth_profile(mac, profile))


@router.post('/microphone/enable')
def bluetooth_enable_microphone(data: dict = Body(...)):
    """启用蓝牙麦克风"""
    mac = data.get('mac')
    if not mac:
        raise InvalidParamError("MAC 地址必填")
    _validate_mac(mac)
    logger.debug(f"启用蓝牙麦克风: {mac}")
    return _json(bluetooth_manager.enable_bluetooth_microphone(mac))


@router.post('/microphone/disable')
def bluetooth_disable_microphone(data: dict = Body(...)):
    """禁用蓝牙麦克风（切回A2DP）"""
    mac = data.get('mac')
    if not mac:
        raise InvalidParamError("MAC 地址必填")
    _validate_mac(mac)
    logger.debug(f"禁用蓝牙麦克风: {mac}")
    return _json(bluetooth_manager.disable_bluetooth_microphone(mac))


@router.post('/audio-source/route')
def bluetooth_route_source(data: dict = Body(...)):
    """将蓝牙音频输入路由到指定应用"""
    source_name = data.get('source_name')
    target_app = data.get('target_app')
    if not source_name or not target_app:
        raise InvalidParamError("source_name 和 target_app 参数必填")
    return _json(route_manager.route_bluetooth_source(source_name, target_app))
