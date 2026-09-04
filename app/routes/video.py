import logging
from fastapi import APIRouter, Body
import video_manager
import route_manager
from routes.helpers import _json, require_param
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

@router.get('/streams')
def video_streams():
    return _json(route_manager.get_video_streams())

@router.post('/route/stream')
def video_route_stream(data: dict = Body(...)):
    stream_id = require_param(data, 'stream_id', "stream_id 和 target_device 参数必填", allow_empty=True)
    target_device = require_param(data, 'target_device', "stream_id 和 target_device 参数必填")
    logger.debug(f"路由视频流: {stream_id} -> {target_device}")
    result = route_manager.route_video_stream(stream_id, target_device)
    event_bus.publish('video.changed', {})
    return _json(result)

@router.post('/default')
def video_set_default(data: dict = Body(...)):
    # 运行时将设备设为系统默认视频 sink(仅 pw-metadata 写,不持久化);DRM 显示器不支持
    device = require_param(data, 'device', "设备名必填")
    logger.debug(f"设为默认视频设备: {device}")
    result = video_manager.set_default_video(device)
    event_bus.publish('video.changed', {})
    return _json(result)

@router.delete('/route/stream')
def video_unlink_stream(data: dict = Body(...)):
    stream_id = require_param(data, 'stream_id', "stream_id 参数必填", allow_empty=True)
    link_id = data.get('link_id')
    result = route_manager.unlink_stream(stream_id, link_id)
    event_bus.publish('video.changed', {})
    return _json(result)

@router.post('/display-output')
def video_set_display_output(data: dict = Body(...)):
    connector = require_param(data, 'connector', "connector 参数必填")
    result = video_manager.set_display_output(connector, data.get('resolution'), data.get('refresh_rate'))
    event_bus.publish('video.changed', {})
    return _json(result)

@router.post('/display-layout')
def video_set_display_layout(data: dict = Body(...)):
    output = require_param(data, 'output', "output 参数必填（目标连接器）")
    relation = require_param(data, 'relation', "relation 参数必填（布局关系）")
    relative_to = data.get('relative_to')
    logger.debug(f"设置显示布局: {output} {relation} {relative_to or ''}")
    result = video_manager.set_display_layout(output, relation, relative_to)
    event_bus.publish('video.changed', {})
    return _json(result)

@router.post('/display-rotation')
def video_set_display_rotation(data: dict = Body(...)):
    output = require_param(data, 'output', "output 参数必填")
    rotation = require_param(data, 'rotation', "rotation 参数必填")
    result = video_manager.set_display_rotation(output, rotation)
    event_bus.publish('video.changed', {})
    return _json(result)

@router.post('/display-scale')
def video_set_display_scale(data: dict = Body(...)):
    output = require_param(data, 'output', "output 参数必填")
    scale = require_param(data, 'scale', "scale 参数必填", allow_empty=True)
    result = video_manager.set_display_scale(output, scale)
    event_bus.publish('video.changed', {})
    return _json(result)
