import logging
import os
from fastapi import APIRouter, Body, Query
from fastapi.responses import FileResponse
import audio_manager
from exceptions import InvalidParamError
from routes.helpers import _json, _as_bool, get_int, get_float, _validate_mac
from event_system import event_bus

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

@router.get('/volume')
def audio_get_volume(device: str = Query(default=None)):
    return _json(audio_manager.get_volume(device))

@router.post('/volume')
def audio_set_volume(data: dict = Body(...)):
    volume = get_int(data, 'volume', 0, 100, msg="音量必须为有效数字")
    logger.debug(f"设置音量: {data.get('device')} -> {volume}%")
    result = audio_manager.set_volume(data.get('device'), volume)
    event_bus.publish('audio.changed', {})
    return _json(result)

@router.post('/default')
def audio_set_default(data: dict = Body(...)):
    # 运行时将设备设为系统默认(仅 wpctl set-default 即时生效,不写 config、不启动恢复)
    device = data.get('device')
    if not device:
        raise InvalidParamError("设备名必填")
    logger.debug(f"设为默认音频设备: {device}")
    result = audio_manager.set_default_audio(device)
    event_bus.publish('audio.changed', {})
    return _json(result)

@router.post('/volume/channel')
def audio_set_channel_volume(data: dict = Body(...)):
    device = data.get('device')
    if not device:
        raise InvalidParamError("device 参数必填")
    channel_index = get_int(data, 'channel', msg="channel 参数必填（声道索引，从0开始）")
    volume = get_int(data, 'volume', 0, 100, msg="volume 必须为整数")
    logger.debug(f"设置声道音量: {device} CH{channel_index} -> {volume}%")
    result = audio_manager.set_channel_volume(device, channel_index, volume)
    event_bus.publish('audio.changed', {})
    return _json(result)

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
    result = audio_manager.set_mute(data.get('device'), _as_bool(mute))
    event_bus.publish('audio.changed', {})
    return _json(result)

@router.get('/balance')
def audio_get_balance(device: str = Query('', description='设备名')):
    return _json(audio_manager.get_balance(device if device else None))

@router.post('/balance')
def audio_set_balance(data: dict = Body(...)):
    balance = get_float(data, 'balance', msg="平衡值必须为有效数字")
    result = audio_manager.set_balance(data.get('device'), balance)
    event_bus.publish('audio.changed', {})
    return _json(result)

@router.post('/activate')
def audio_activate_device(data: dict = Body(...)):
    device = data.get('device')
    if not device:
        raise InvalidParamError("设备名必填")
    mac = data.get('mac')
    if mac:
        # 蓝牙设备已在 BlueZ 层连接但 PipeWire 未建立 bluez_output 节点：
        # 此时 device 只是 BlueZ alias，pw_dump 中查不到，必须按 MAC 重建 A2DP sink。
        _validate_mac(mac)
        logger.debug(f"激活蓝牙音频 sink: {device} ({mac})")
        result = audio_manager.activate_bluetooth_audio(mac, device)
    else:
        logger.debug(f"激活音频设备: {device}")
        result = audio_manager.activate_audio_device(device)
    event_bus.publish('audio.changed', {})
    return _json(result)

@router.post('/route')
def audio_set_route(data: dict = Body(...)):
    device = data.get('device')
    route = data.get('route')
    if not device or not route:
        raise InvalidParamError("设备和端口参数必填")
    logger.debug(f"切换端口: {device} -> {route}")
    result = audio_manager.set_route(device, route)
    event_bus.publish('audio.changed', {})
    return _json(result)

@router.post('/profile')
def audio_set_profile(data: dict = Body(...)):
    device = data.get('device')
    profile = data.get('profile')
    if not device or not profile:
        raise InvalidParamError("设备和 Profile 参数必填")
    logger.debug(f"切换 Profile: {device} -> {profile}")
    result = audio_manager.set_profile(device, profile)
    event_bus.publish('audio.changed', {})
    return _json(result)

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

@router.get('/combine')
def audio_combine_status():
    return _json(audio_manager.get_combine_sink_status())

@router.post('/combine')
def audio_combine_create(data: dict = Body(...)):
    devices = data.get('devices')
    if not isinstance(devices, list) or len(devices) < 2:
        raise InvalidParamError("请选择至少 2 个设备")
    logger.debug(f"创建多设备同时播放: {devices}")
    result = audio_manager.create_combine_sink(devices)
    event_bus.publish('audio.changed', {})
    return _json(result)

@router.delete('/combine')
def audio_combine_destroy():
    logger.debug("关闭多设备同时播放")
    result = audio_manager.destroy_combine_sink()
    event_bus.publish('audio.changed', {})
    return _json(result)

@router.get('/streams')
def audio_list_streams():
    # 列出当前所有播放流(应用),用于按流路由到不同音箱
    return _json(audio_manager.list_playback_streams())

@router.post('/streams/route')
def audio_route_stream(data: dict = Body(...)):
    stream_id = data.get('stream_id')
    if stream_id is None:
        raise InvalidParamError("stream_id 不能为空")
    sink = data.get('sink') or ''
    logger.debug(f"路由播放流 {stream_id} -> {sink or '默认输出'}")
    result = audio_manager.route_stream_to_sink(stream_id, sink)
    event_bus.publish('audio.changed', {})
    return _json(result)


@router.post('/record/start')
def audio_record_start(data: dict = Body(...)):
    device = data.get('device')
    if not device:
        raise InvalidParamError("device 参数必填")
    filename = data.get('filename')
    logger.debug(f"开始录音: {device} -> {filename or '自动命名'}")
    return _json(audio_manager.start_recording(device, filename))


@router.post('/record/stop')
def audio_record_stop():
    logger.debug("停止录音")
    result = audio_manager.stop_recording()
    event_bus.publish('audio.changed', {})
    return _json(result)


@router.get('/record/status')
def audio_record_status():
    return _json(audio_manager.get_recording_status())


@router.get('/record/files')
def audio_record_files():
    return _json({'files': audio_manager.list_recordings()})


@router.delete('/record/files/{filename}')
def audio_record_delete(filename: str):
    logger.debug(f"删除录音: {filename}")
    return _json(audio_manager.delete_recording(filename))


@router.get('/record/download/{filename}')
def audio_record_download(filename: str):
    path = audio_manager.recording_path(filename)
    if not os.path.exists(path):
        raise InvalidParamError(f"录音文件不存在: {filename}")
    return FileResponse(path, media_type='audio/wav', filename=filename)


@router.get('/clock')
def audio_get_clock():
    return _json(audio_manager.get_audio_clock())


@router.post('/clock')
def audio_set_clock(data: dict = Body(...)):
    rate = data.get('rate')
    quantum = data.get('quantum')
    persist = _as_bool(data.get('persist', False))
    try:
        if rate is not None:
            rate = int(rate)
        if quantum is not None:
            quantum = int(quantum)
    except (TypeError, ValueError):
        raise InvalidParamError("rate/quantum 必须为整数")
    logger.debug(f"设置音频时钟: rate={rate} quantum={quantum} persist={persist}")
    result = audio_manager.set_audio_clock(rate, quantum, persist)
    event_bus.publish('audio.changed', {})
    return _json(result)


@router.delete('/clock')
def audio_reset_clock():
    logger.debug("恢复默认音频时钟")
    result = audio_manager.reset_audio_clock()
    event_bus.publish('audio.changed', {})
    return _json(result)
