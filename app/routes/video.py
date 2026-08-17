import logging
from fastapi import APIRouter, Body
import video_manager
import route_manager
from exceptions import InvalidParamError
from routes.helpers import _json
from event_system import event_bus

logger = logging.getLogger('PipeBridge')

router = APIRouter(prefix="/api/video", tags=["video"])

@router.get('/devices')
def video_devices():
    logger.debug("获取视频设备列表")
    return _json(video_manager.get_video_devices())

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
    result = video_manager.set_default_video_device(device)
    event_bus.publish('video.changed', {})
    return _json(result)

@router.post('/default/clear')
def video_default_clear(data: dict = Body(default={})):
    logger.debug("取消默认视频设备")
    result = video_manager.clear_default_video_device()
    event_bus.publish('video.changed', {})
    return _json(result)

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
    result = route_manager.route_video_stream(stream_id, target_device)
    event_bus.publish('video.changed', {})
    return _json(result)

@router.delete('/route/stream')
def video_unlink_stream(data: dict = Body(...)):
    stream_id = data.get('stream_id')
    link_id = data.get('link_id')
    if stream_id is None:
        raise InvalidParamError("stream_id 参数必填")
    result = route_manager.unlink_stream(stream_id, link_id)
    event_bus.publish('video.changed', {})
    return _json(result)

@router.post('/display-output')
def video_set_display_output(data: dict = Body(...)):
    connector = data.get('connector')
    if not connector:
        raise InvalidParamError("connector 参数必填")
    result = video_manager.set_display_output(connector, data.get('resolution'), data.get('refresh_rate'))
    event_bus.publish('video.changed', {})
    return _json(result)

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
    result = video_manager.set_display_layout(output, relation, relative_to)
    event_bus.publish('video.changed', {})
    return _json(result)

@router.post('/display-rotation')
def video_set_display_rotation(data: dict = Body(...)):
    output = data.get('output')
    rotation = data.get('rotation')
    if not output:
        raise InvalidParamError("output 参数必填")
    if not rotation:
        raise InvalidParamError("rotation 参数必填")
    result = video_manager.set_display_rotation(output, rotation)
    event_bus.publish('video.changed', {})
    return _json(result)

@router.post('/display-scale')
def video_set_display_scale(data: dict = Body(...)):
    output = data.get('output')
    scale = data.get('scale')
    if not output:
        raise InvalidParamError("output 参数必填")
    if scale is None:
        raise InvalidParamError("scale 参数必填")
    result = video_manager.set_display_scale(output, scale)
    event_bus.publish('video.changed', {})
    return _json(result)
