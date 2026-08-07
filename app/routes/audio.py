import logging
from fastapi import APIRouter, Body, Query
import audio_manager
from exceptions import InvalidParamError
from routes.helpers import _json, _as_bool

logger = logging.getLogger('PipeBridge')

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
        raise InvalidParamError("设备名必填")
    logger.debug(f"设置默认音频设备: {device}")
    return _json(audio_manager.set_default_device(device))

@router.get('/volume')
def audio_get_volume(device: str = Query(default=None)):
    return _json(audio_manager.get_volume(device))

@router.post('/volume')
def audio_set_volume(data: dict = Body(...)):
    volume = data.get('volume')
    if volume is None:
        raise InvalidParamError("音量参数必填")
    try:
        volume = int(volume)
    except (ValueError, TypeError):
        raise InvalidParamError("音量必须为有效数字")
    volume = max(0, min(100, volume))
    logger.debug(f"设置音量: {data.get('device')} -> {volume}%")
    return _json(audio_manager.set_volume(data.get('device'), volume))

@router.post('/volume/channel')
def audio_set_channel_volume(data: dict = Body(...)):
    device = data.get('device')
    channel_index = data.get('channel')
    volume = data.get('volume')
    if not device:
        raise InvalidParamError("device 参数必填")
    if channel_index is None:
        raise InvalidParamError("channel 参数必填（声道索引，从0开始）")
    if volume is None:
        raise InvalidParamError("volume 参数必填")
    try:
        channel_index = int(channel_index)
        volume = int(volume)
    except (ValueError, TypeError):
        raise InvalidParamError("channel 和 volume 必须为整数")
    volume = max(0, min(100, volume))
    logger.debug(f"设置声道音量: {device} CH{channel_index} -> {volume}%")
    return _json(audio_manager.set_channel_volume(device, channel_index, volume))

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
        raise InvalidParamError("静音状态参数必填")
    return _json(audio_manager.set_mute(data.get('device'), _as_bool(mute)))

@router.get('/balance')
def audio_get_balance(device: str = Query('', description='设备名')):
    return _json(audio_manager.get_balance(device if device else None))

@router.post('/balance')
def audio_set_balance(data: dict = Body(...)):
    balance = data.get('balance')
    if balance is None:
        raise InvalidParamError("平衡值参数必填")
    try:
        balance = float(balance)
    except (ValueError, TypeError):
        raise InvalidParamError("平衡值必须为有效数字")
    return _json(audio_manager.set_balance(data.get('device'), balance))

@router.post('/activate')
def audio_activate_device(data: dict = Body(...)):
    device = data.get('device')
    if not device:
        raise InvalidParamError("设备名必填")
    logger.debug(f"激活音频设备: {device}")
    return _json(audio_manager.activate_audio_device(device))

@router.post('/route')
def audio_set_route(data: dict = Body(...)):
    device = data.get('device')
    route = data.get('route')
    if not device or not route:
        raise InvalidParamError("设备和端口参数必填")
    logger.debug(f"切换端口: {device} -> {route}")
    return _json(audio_manager.set_route(device, route))

@router.post('/profile')
def audio_set_profile(data: dict = Body(...)):
    device = data.get('device')
    profile = data.get('profile')
    if not device or not profile:
        raise InvalidParamError("设备和 Profile 参数必填")
    logger.debug(f"切换 Profile: {device} -> {profile}")
    return _json(audio_manager.set_profile(device, profile))

@router.get('/profiles/{device_name}')
def audio_get_profiles(device_name: str):
    logger.debug(f"获取 Profile 列表: {device_name}")
    return _json(audio_manager.get_profiles(device_name))

@router.get('/usb-devices')
def audio_usb_devices():
    return _json(audio_manager.get_usb_audio_devices())

@router.get('/peak')
def audio_peak():
    return _json(audio_manager.get_peak_levels())
