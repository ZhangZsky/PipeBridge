import logging
from fastapi import APIRouter, Body
import video_manager
import route_manager
from exceptions import InvalidParamError
from routes.helpers import _json

logger = logging.getLogger('PipeBridge')

router = APIRouter(prefix="/api/video", tags=["video"])

@router.get('/devices')
def video_devices():
    logger.debug("获取视频设备列表")
    return _json(video_manager.get_video_devices())

@router.post('/scan')
def video_scan():
    logger.debug("强制扫描视频设备")
    return _json(video_manager.scan_video_devices(force=True))

@router.get('/device/{device_name}')
def video_device_detail(device_name: str):
    logger.debug(f"获取视频设备详情: {device_name}")
    return _json(video_manager.get_video_device_detail(device_name))

@router.post('/default')
def video_set_default(data: dict = Body(...)):
    device = data.get('device')
    if not device:
        raise InvalidParamError('设备名必填')
    logger.debug(f"设置默认视频设备: {device}")
    return _json(video_manager.set_default_video_device(device))

@router.post('/test')
def video_play_test(data: dict = Body(...)):
    device = data.get('device')
    logger.debug(f"视频测试: {device or '默认'}")
    return _json(video_manager.get_video_test_status(device))

@router.get('/streams')
def video_streams():
    return _json(route_manager.get_video_streams())

@router.post('/route/stream')
def video_route_stream(data: dict = Body(...)):
    stream_id = data.get('stream_id')
    target_device = data.get('target_device')
    if stream_id is None or not target_device:
        raise InvalidParamError("stream_id 和 target_device 参数必填")
    logger.debug(f"路由视频流: {stream_id} -> {target_device}")
    return _json(route_manager.route_video_stream(stream_id, target_device))

@router.delete('/route/stream')
def video_unlink_stream(data: dict = Body(...)):
    stream_id = data.get('stream_id')
    link_id = data.get('link_id')
    if stream_id is None:
        raise InvalidParamError("stream_id 参数必填")
    return _json(route_manager.unlink_stream(stream_id, link_id))

@router.post('/display-output')
def video_set_display_output(data: dict = Body(...)):
    connector = data.get('connector')
    if not connector:
        raise InvalidParamError("connector 参数必填")
    return _json(video_manager.set_display_output(connector, data.get('resolution'), data.get('refresh_rate')))

@router.post('/display-layout')
def video_set_display_layout(data: dict = Body(...)):
    output = data.get('output')
    relation = data.get('relation')
    relative_to = data.get('relative_to')
    if not output:
        raise InvalidParamError("output 参数必填（目标连接器）")
    if not relation:
        raise InvalidParamError("relation 参数必填（布局关系）")
    logger.debug(f"设置显示布局: {output} {relation} {relative_to or ''}")
    return _json(video_manager.set_display_layout(output, relation, relative_to))

@router.get('/v4l2-controls/{device_name}')
def video_v4l2_controls(device_name: str):
    return _json(video_manager.get_v4l2_controls(device_name))

@router.post('/v4l2-control')
def video_set_v4l2_control(data: dict = Body(...)):
    device_name = data.get('device')
    control_name = data.get('control')
    value = data.get('value')
    if not device_name:
        raise InvalidParamError("device 参数必填")
    if not control_name:
        raise InvalidParamError("control 参数必填")
    if value is None:
        raise InvalidParamError("value 参数必填")
    logger.debug(f"设置 V4L2 参数: {device_name} {control_name}={value}")
    return _json(video_manager.set_v4l2_control(device_name, control_name, value))

@router.get('/v4l2-formats/{device_name}')
def video_v4l2_formats(device_name: str):
    return _json(video_manager.get_v4l2_formats(device_name))

@router.post('/v4l2-format')
def video_set_v4l2_format(data: dict = Body(...)):
    device_name = data.get('device')
    if not device_name:
        raise InvalidParamError("device 参数必填")
    width = data.get('width')
    height = data.get('height')
    pixel_format = data.get('pixel_format')
    if not width and not height and not pixel_format:
        raise InvalidParamError("至少需要指定分辨率或像素格式")
    logger.debug(f"设置 V4L2 格式: {device_name} {width}x{height} {pixel_format}")
    return _json(video_manager.set_v4l2_format(device_name, width, height, pixel_format))

@router.post('/v4l2-framerate')
def video_set_v4l2_framerate(data: dict = Body(...)):
    device_name = data.get('device')
    fps = data.get('fps')
    if not device_name:
        raise InvalidParamError("device 参数必填")
    if fps is None:
        raise InvalidParamError("fps 参数必填")
    logger.debug(f"设置 V4L2 帧率: {device_name} -> {fps}fps")
    return _json(video_manager.set_v4l2_framerate(device_name, fps))

@router.post('/display-rotation')
def video_set_display_rotation(data: dict = Body(...)):
    output = data.get('output')
    rotation = data.get('rotation')
    if not output:
        raise InvalidParamError("output 参数必填")
    if not rotation:
        raise InvalidParamError("rotation 参数必填")
    return _json(video_manager.set_display_rotation(output, rotation))

@router.post('/display-scale')
def video_set_display_scale(data: dict = Body(...)):
    output = data.get('output')
    scale = data.get('scale')
    if not output:
        raise InvalidParamError("output 参数必填")
    if scale is None:
        raise InvalidParamError("scale 参数必填")
    return _json(video_manager.set_display_scale(output, scale))
