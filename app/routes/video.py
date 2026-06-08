import logging
from fastapi import APIRouter, Body
import video_manager
import route_manager
from exceptions import InvalidParamError
from routes.helpers import _json

logger = logging.getLogger('MediaHub')

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
        raise InvalidParamError('Device name is required')
    logger.debug(f"设置默认视频设备: {device}")
    return _json(video_manager.set_default_video_device(device))


@router.post('/test')
def video_play_test(data: dict = Body(...)):
    device = data.get('device')
    logger.debug(f"视频测试: {device or '默认'}")
    return _json(video_manager.play_test_video(device))


@router.get('/streams')
def video_streams():
    """列出所有活跃视频流"""
    return _json(route_manager.get_video_streams())


@router.post('/route/stream')
def video_route_stream(data: dict = Body(...)):
    """将视频流路由到指定输出"""
    stream_id = data.get('stream_id')
    target_device = data.get('target_device')
    if stream_id is None or not target_device:
        raise InvalidParamError("stream_id and target_device are required")
    logger.debug(f"路由视频流: {stream_id} -> {target_device}")
    return _json(route_manager.route_video_stream(stream_id, target_device))


@router.delete('/route/stream')
def video_unlink_stream(data: dict = Body(...)):
    """断开视频流路由"""
    stream_id = data.get('stream_id')
    link_id = data.get('link_id')
    if stream_id is None:
        raise InvalidParamError("stream_id is required")
    return _json(video_manager.unlink_video_stream(stream_id, link_id))


@router.post('/display-output')
def video_set_display_output(data: dict = Body(...)):
    """配置DRM显示输出"""
    connector = data.get('connector')
    if not connector:
        raise InvalidParamError("connector is required")
    return _json(video_manager.set_display_output(connector, data.get('resolution'), data.get('refresh_rate')))
