import logging
from fastapi import APIRouter, Body, Query
import audio_manager
import route_manager
from exceptions import InvalidParamError
from routes.helpers import _json

logger = logging.getLogger('MediaHub')

router = APIRouter(prefix="/api/audio", tags=["audio"])


@router.get('/devices')
def audio_devices():
    result = audio_manager.get_audio_devices()
    return _json(result)


@router.post('/scan')
def audio_scan():
    logger.debug("扫描音频设备")
    result = audio_manager.scan_audio_devices()
    device_count = len(result.get("devices", []))
    logger.debug(f"扫描完成，发现 {device_count} 个音频设备")
    return _json(result)


@router.get('/device/{device_name}')
def audio_device_detail(device_name: str):
    logger.debug(f"获取音频设备详情: {device_name}")
    return _json(audio_manager.get_audio_device_detail(device_name))


@router.post('/default')
def audio_default(data: dict = Body(...)):
    device = data.get('device')
    if not device:
        raise InvalidParamError("Device name is required")
    logger.debug(f"设置默认音频设备: {device}")
    return _json(audio_manager.set_default_device(device))


@router.get('/volume')
def audio_get_volume(device: str = Query(default=None)):
    return _json(audio_manager.get_volume(device))


@router.post('/volume')
def audio_set_volume(data: dict = Body(...)):
    volume = data.get('volume')
    if volume is None:
        raise InvalidParamError("Volume is required")
    try:
        volume = int(volume)
    except (ValueError, TypeError):
        raise InvalidParamError("Volume must be a valid number")
    volume = max(0, min(100, volume))
    logger.debug(f"设置音量: {data.get('device')} -> {volume}%")
    return _json(audio_manager.set_volume(data.get('device'), volume))


@router.post('/test')
def audio_play_test(data: dict = Body(...)):
    device = data.get('device')
    logger.debug(f"播放测试音: {device or '默认设备'}")
    return _json(audio_manager.play_test_sound(device))


@router.post('/test-channel')
def audio_play_test_channel(data: dict = Body(...)):
    device = data.get('device')
    position = data.get('position', '')
    logger.debug(f"播放声道测试音: {device or '默认设备'} 声道={position}")
    return _json(audio_manager.play_test_channel(device, position))


@router.post('/mute')
def audio_set_mute(data: dict = Body(...)):
    mute = data.get('mute')
    if mute is None:
        raise InvalidParamError("Mute state is required")
    return _json(audio_manager.set_mute(data.get('device'), bool(mute)))


@router.get('/balance')
def audio_get_balance(device: str = Query('', description='设备名')):
    return _json(audio_manager.get_balance(device if device else None))


@router.post('/balance')
def audio_set_balance(data: dict = Body(...)):
    balance = data.get('balance')
    if balance is None:
        raise InvalidParamError("Balance value is required")
    try:
        balance = float(balance)
    except (ValueError, TypeError):
        raise InvalidParamError("Balance must be a valid number")
    return _json(audio_manager.set_balance(data.get('device'), balance))


@router.post('/activate')
def audio_activate_device(data: dict = Body(...)):
    device = data.get('device')
    if not device:
        raise InvalidParamError("Device name is required")
    logger.debug(f"激活音频设备: {device}")
    return _json(audio_manager.activate_audio_device(device))


@router.post('/route')
def audio_set_route(data: dict = Body(...)):
    device = data.get('device')
    route = data.get('route')
    if not device or not route:
        raise InvalidParamError("Device and route are required")
    logger.debug(f"切换端口: {device} -> {route}")
    return _json(audio_manager.set_route(device, route))


@router.post('/profile')
def audio_set_profile(data: dict = Body(...)):
    device = data.get('device')
    profile = data.get('profile')
    if not device or not profile:
        raise InvalidParamError("Device and profile are required")
    logger.debug(f"切换 Profile: {device} -> {profile}")
    return _json(audio_manager.set_profile(device, profile))


@router.get('/profiles/{device_name}')
def audio_get_profiles(device_name: str):
    logger.debug(f"获取 Profile 列表: {device_name}")
    return _json(audio_manager.get_profiles(device_name))


@router.get('/streams')
def audio_streams():
    """列出所有活跃音频流"""
    result = route_manager.get_audio_streams()
    return _json(result)


@router.post('/route/stream')
def audio_route_stream(data: dict = Body(...)):
    """将音频流路由到指定设备"""
    stream_id = data.get('stream_id')
    target_device = data.get('target_device')
    if stream_id is None or not target_device:
        raise InvalidParamError("stream_id and target_device are required")
    logger.debug(f"路由音频流: {stream_id} -> {target_device}")
    return _json(route_manager.route_audio_stream(stream_id, target_device))


@router.delete('/route/stream')
def audio_unlink_stream(data: dict = Body(...)):
    """断开音频流路由"""
    stream_id = data.get('stream_id')
    link_id = data.get('link_id')
    if stream_id is None:
        raise InvalidParamError("stream_id is required")
    return _json(route_manager.unlink_stream(stream_id, link_id))


@router.get('/routing')
def audio_routing_status():
    """获取音频路由状态概览"""
    return _json(audio_manager.get_audio_routing_status())


@router.get('/usb-devices')
def audio_usb_devices():
    """获取USB音频设备列表"""
    return _json(audio_manager.get_usb_audio_devices())
