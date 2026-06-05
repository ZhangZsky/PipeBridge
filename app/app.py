import os
import sys
import re
import time
import logging
import signal
import atexit
import threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn
import bluetooth_manager
import audio_manager
import video_manager
import dependency_checker
import route_manager
from utils import run_command
from exceptions import MediaHubError, InvalidParamError

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'DEBUG').upper()
MAC_PATTERN = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$')

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.WARNING),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('MediaHub')
logging.getLogger('uvicorn.access').disabled = True

app = FastAPI(title="MediaHub")


@app.exception_handler(MediaHubError)
async def mediahub_error_handler(request, exc):
    return JSONResponse(
        status_code=200,
        content={'success': False, 'error': exc.message, 'code': exc.code}
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.startswith('/api/'):
            return response
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response


app.add_middleware(NoCacheMiddleware)

web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
app.mount("/css", StaticFiles(directory=os.path.join(web_dir, 'css')), name="css")
app.mount("/js", StaticFiles(directory=os.path.join(web_dir, 'js')), name="js")
app.mount("/images", StaticFiles(directory=os.path.join(web_dir, 'images')), name="images")


def _json(result, **extra):
    """将结果转为 JSONResponse，成功时直接返回数据"""
    if isinstance(result, dict) and 'success' in result:
        content = result
    else:
        content = {'success': True, 'data': result}
    content.update(extra)
    return JSONResponse(content=content)


def _validate_mac(mac):
    if not mac or not MAC_PATTERN.match(mac):
        raise InvalidParamError("Valid MAC address is required")


@app.get('/')
async def index():
    return FileResponse(os.path.join(web_dir, 'index.html'))


@app.get('/config')
async def serve_web_config():
    config_path = os.path.join(web_dir, 'config')
    if os.path.exists(config_path):
        return FileResponse(config_path)
    raise InvalidParamError("Config file not found")


@app.get('/favicon.ico')
async def serve_favicon():
    favicon_path = os.path.join(web_dir, 'favicon.ico')
    if os.path.exists(favicon_path):
        return FileResponse(favicon_path)
    icon_path = os.path.join(web_dir, 'images', 'icon_64.png')
    if os.path.exists(icon_path):
        return FileResponse(icon_path)
    raise InvalidParamError("Not found")


@app.get('/api/bluetooth/status')
async def bluetooth_status():
    logger.debug("获取蓝牙状态")
    result = bluetooth_manager.get_bluetooth_status()
    logger.debug(f"蓝牙状态: {result.get('status', 'unknown')}")
    return _json(result)


@app.post('/api/bluetooth/install')
async def bluetooth_install():
    logger.debug("安装蓝牙驱动")
    result = bluetooth_manager.install_bluetooth_driver()
    logger.debug(f"蓝牙驱动安装: {result}")
    return _json(result)


@app.get('/api/bluetooth/scan')
async def bluetooth_scan():
    logger.debug("扫描蓝牙设备")
    result = bluetooth_manager.scan_devices()
    device_count = len(result) if result else 0
    logger.debug(f"扫描完成，发现 {device_count} 个设备")
    return _json(result)


@app.get('/api/bluetooth/devices')
async def bluetooth_devices():
    return _json(bluetooth_manager.get_paired_devices())


@app.post('/api/bluetooth/pair')
async def bluetooth_pair(data: dict = Body(...)):
    mac = data.get('mac')
    _validate_mac(mac)
    logger.debug(f"配对蓝牙设备: {mac}")
    try:
        result = bluetooth_manager.pair_device(mac, pin=data.get('pin'))
    except InvalidParamError as e:
        if getattr(e, 'needs_pin', False):
            device_name = getattr(e, 'device_name', None) or mac
            logger.debug(f"设备 {mac} 需要PIN码")
            return _json({"data": None}, needs_pin=True, device_name=device_name)
        raise
    logger.debug(f"配对结果: 成功, 连接: {result.get('connected', False)}")
    return _json(result, connected=result.get("connected", False), device_name=result.get("device_name", mac))


@app.post('/api/bluetooth/connect')
async def bluetooth_connect(data: dict = Body(...)):
    mac = data.get('mac')
    _validate_mac(mac)
    logger.debug(f"连接蓝牙设备: {mac}")
    result = bluetooth_manager.connect_device(mac)
    logger.debug(f"连接结果: 成功")
    return _json(result)


@app.post('/api/bluetooth/disconnect')
async def bluetooth_disconnect(data: dict = Body(...)):
    mac = data.get('mac')
    _validate_mac(mac)
    logger.debug(f"断开蓝牙设备: {mac}")
    return _json(bluetooth_manager.disconnect_device(mac))


@app.post('/api/bluetooth/remove')
async def bluetooth_remove(data: dict = Body(...)):
    mac = data.get('mac')
    _validate_mac(mac)
    logger.debug(f"删除蓝牙设备: {mac}")
    return _json(bluetooth_manager.remove_device(mac))


@app.post('/api/bluetooth/power')
async def bluetooth_power(data: dict = Body(...)):
    power = data.get('power')
    if power is None:
        raise InvalidParamError("Power state is required")
    return _json(bluetooth_manager.set_power(bool(power)))


@app.post('/api/bluetooth/discoverable')
async def bluetooth_discoverable(data: dict = Body(...)):
    discoverable = data.get('discoverable')
    if discoverable is None:
        raise InvalidParamError("Discoverable state is required")
    return _json(bluetooth_manager.set_discoverable(bool(discoverable)))


@app.post('/api/bluetooth/alias')
async def bluetooth_alias(data: dict = Body(...)):
    mac, alias = data.get('mac'), data.get('alias')
    if not mac or not alias:
        raise InvalidParamError("MAC 地址和别名不能为空")
    _validate_mac(mac)
    return _json(bluetooth_manager.set_device_alias(mac, alias))


@app.get('/api/audio/devices')
async def audio_devices():
    result = audio_manager.get_audio_devices()
    return _json(result)


@app.post('/api/audio/scan')
async def audio_scan():
    logger.debug("扫描音频设备")
    result = audio_manager.scan_audio_devices()
    device_count = len(result.get("devices", []))
    logger.debug(f"扫描完成，发现 {device_count} 个音频设备")
    return _json(result)


@app.get('/api/audio/device/{device_name}')
async def audio_device_detail(device_name: str):
    logger.debug(f"获取音频设备详情: {device_name}")
    return _json(audio_manager.get_audio_device_detail(device_name))


@app.post('/api/audio/default')
async def audio_default(data: dict = Body(...)):
    device = data.get('device')
    if not device:
        raise InvalidParamError("Device name is required")
    logger.debug(f"设置默认音频设备: {device}")
    return _json(audio_manager.set_default_device(device))


@app.get('/api/audio/volume')
async def audio_get_volume(device: str = Query(default=None)):
    return _json(audio_manager.get_volume(device))


@app.post('/api/audio/volume')
async def audio_set_volume(data: dict = Body(...)):
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


@app.post('/api/audio/test')
async def audio_play_test(data: dict = Body(...)):
    device = data.get('device')
    logger.debug(f"播放测试音: {device or '默认设备'}")
    return _json(audio_manager.play_test_sound(device))


@app.post('/api/audio/test-channel')
async def audio_play_test_channel(data: dict = Body(...)):
    device = data.get('device')
    position = data.get('position', '')
    logger.debug(f"播放声道测试音: {device or '默认设备'} 声道={position}")
    return _json(audio_manager.play_test_channel(device, position))


@app.post('/api/audio/mute')
async def audio_set_mute(data: dict = Body(...)):
    mute = data.get('mute')
    if mute is None:
        raise InvalidParamError("Mute state is required")
    return _json(audio_manager.set_mute(data.get('device'), bool(mute)))


@app.get('/api/audio/balance')
async def audio_get_balance(device: str = Query('', description='设备名')):
    return _json(audio_manager.get_balance(device if device else None))


@app.post('/api/audio/balance')
async def audio_set_balance(data: dict = Body(...)):
    balance = data.get('balance')
    if balance is None:
        raise InvalidParamError("Balance value is required")
    try:
        balance = float(balance)
    except (ValueError, TypeError):
        raise InvalidParamError("Balance must be a valid number")
    return _json(audio_manager.set_balance(data.get('device'), balance))


@app.get('/api/video/devices')
async def video_devices():
    logger.debug("获取视频设备列表")
    return _json(video_manager.get_video_devices())


@app.post('/api/video/scan')
async def video_scan():
    logger.debug("强制扫描视频设备")
    return _json(video_manager.scan_video_devices(force=True))


@app.post('/api/audio/activate')
async def audio_activate_device(data: dict = Body(...)):
    device = data.get('device')
    if not device:
        raise InvalidParamError("Device name is required")
    logger.debug(f"激活音频设备: {device}")
    return _json(audio_manager.activate_audio_device(device))


@app.post('/api/audio/route')
async def audio_set_route(data: dict = Body(...)):
    device = data.get('device')
    route = data.get('route')
    if not device or not route:
        raise InvalidParamError("Device and route are required")
    logger.debug(f"切换端口: {device} -> {route}")
    return _json(audio_manager.set_route(device, route))


@app.post('/api/audio/profile')
async def audio_set_profile(data: dict = Body(...)):
    device = data.get('device')
    profile = data.get('profile')
    if not device or not profile:
        raise InvalidParamError("Device and profile are required")
    logger.debug(f"切换 Profile: {device} -> {profile}")
    return _json(audio_manager.set_profile(device, profile))


@app.get('/api/audio/profiles/{device_name}')
async def audio_get_profiles(device_name: str):
    logger.debug(f"获取 Profile 列表: {device_name}")
    return _json(audio_manager.get_profiles(device_name))


@app.get('/api/video/device/{device_name}')
async def video_device_detail(device_name: str):
    logger.debug(f"获取视频设备详情: {device_name}")
    return _json(video_manager.get_video_device_detail(device_name))


@app.post('/api/video/default')
async def video_set_default(data: dict = Body(...)):
    device = data.get('device')
    if not device:
        raise InvalidParamError('Device name is required')
    logger.debug(f"设置默认视频设备: {device}")
    return _json(video_manager.set_default_video_device(device))


@app.post('/api/video/test')
async def video_play_test(data: dict = Body(...)):
    device = data.get('device')
    logger.debug(f"视频测试: {device or '默认'}")
    return _json(video_manager.play_test_video(device))


@app.post('/api/bluetooth/keep-alive')
async def bluetooth_keep_alive():
    bluetooth_manager.keep_bluetooth_alive()
    connected = bluetooth_manager.check_bluetooth_connections()
    return _json({"connected": connected})


@app.get('/api/system/overview')
async def system_overview():
    logger.debug("获取系统概览")
    return _json(dependency_checker.get_system_overview())


@app.get('/api/system/dependencies')
async def system_dependencies():
    overview = dependency_checker.get_system_overview()
    deps_data = overview.get('dependencies', {})
    return _json(deps_data)


@app.post('/api/system/fix')
async def system_fix():
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


@app.get('/api/health')
async def health_check():
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


@app.get('/api/bluetooth/reconnect/status')
async def bluetooth_reconnect_status():
    try:
        return _json(bluetooth_manager.get_reconnect_status())
    except Exception:
        return _json({"monitoring": False, "reconnecting_devices": [], "manual_disconnects": []})


@app.post('/api/system/reconnect')
async def system_reconnect(data: dict = Body(...)):
    enabled = data.get('enabled')
    if enabled is None:
        raise InvalidParamError("enabled field is required")
    bluetooth_manager.set_reconnect_enabled(bool(enabled))
    return _json({"message": "ok"})


def _startup_self_heal():
    # 启动时自检和修复：确保 PipeWire、蓝牙服务运行
    start_time = time.time()
    logger.info("启动自检和修复...")

    # 检查并启动 PipeWire 音频服务
    if not dependency_checker.check_pipewire_running():
        logger.info("音频服务未运行，尝试启动 PipeWire...")
        threading.Thread(target=_async_pipewire_setup, daemon=True).start()

    # 检查并启动蓝牙服务
    try:
        bt = run_command("systemctl is-active bluetooth 2>/dev/null")
        if 'active' not in bt.get('stdout', ''):
            logger.info("蓝牙服务未运行，尝试启动...")
            run_command("systemctl start bluetooth 2>/dev/null")
            time.sleep(1)
        else:
            logger.info("蓝牙服务已运行")
    except Exception as e:
        logger.warning(f"检查蓝牙服务失败: {e}")

    # 注册蓝牙 Agent 以处理入站配对请求
    try:
        bluetooth_manager.ensure_agent()
    except Exception as e:
        logger.warning(f"持久 Agent 注册失败: {e}")

    # 后台执行耗时初始化任务
    threading.Thread(target=_async_startup_tasks, daemon=True).start()

    logger.info(f"启动自检完成，耗时 {time.time() - start_time:.2f}s（后台任务继续）")


def _async_pipewire_setup():
    try:
        dependency_checker.setup_pipewire()
        logger.info("PipeWire 启动成功")
    except Exception as e:
        logger.warning(f"PipeWire 启动失败: {e}")


def _async_startup_tasks():
    try:
        bluetooth_manager.ensure_wireplumber_bluez_config()
    except Exception as e:
        logger.warning(f"WirePlumber 蓝牙配置检查失败: {e}")
    try:
        audio_manager.restore_default_device()
    except Exception as e:
        logger.warning(f"恢复默认设备失败: {e}")
    try:
        audio_manager.auto_set_defaults()
    except Exception as e:
        logger.warning(f"自动设置默认设备失败: {e}")
    try:
        bluetooth_manager.keep_bluetooth_alive()
    except Exception as e:
        logger.warning(f"蓝牙保活失败: {e}")
    _start_bluetooth_keepalive_timer()


def _start_bluetooth_keepalive_timer():
    def _keepalive_loop():
        while True:
            time.sleep(60)
            try:
                bluetooth_manager.keep_bluetooth_alive()
            except Exception as e:
                logger.debug(f"蓝牙周期保活失败: {e}")
    t = threading.Thread(target=_keepalive_loop, daemon=True)
    t.start()
    logger.debug("蓝牙周期保活已启动 (间隔 60s)")


def _cleanup():
    logger.info("正在清理资源...")
    try:
        rm = bluetooth_manager._auto_reconnect_manager
        if rm is not None:
            rm.stop()
    except Exception:
        pass
    try:
        bluetooth_manager.release_agent()
    except Exception:
        pass
    logger.info("资源清理完成")


def _signal_handler(signum, frame):
    logger.info(f"收到信号 {signum}，正在关闭...")
    _cleanup()
    sys.exit(0)


@app.get('/api/audio/streams')
async def audio_streams():
    """列出所有活跃音频流"""
    import route_manager
    result = route_manager.get_audio_streams()
    return _json(result)

@app.post('/api/audio/route/stream')
async def audio_route_stream(data: dict = Body(...)):
    """将音频流路由到指定设备"""
    stream_id = data.get('stream_id')
    target_device = data.get('target_device')
    if stream_id is None or not target_device:
        raise InvalidParamError("stream_id and target_device are required")
    logger.debug(f"路由音频流: {stream_id} -> {target_device}")
    import route_manager
    return _json(route_manager.route_audio_stream(stream_id, target_device))

@app.delete('/api/audio/route/stream')
async def audio_unlink_stream(data: dict = Body(...)):
    """断开音频流路由"""
    stream_id = data.get('stream_id')
    link_id = data.get('link_id')
    if stream_id is None:
        raise InvalidParamError("stream_id is required")
    import route_manager
    return _json(route_manager.unlink_stream(stream_id, link_id))

@app.get('/api/audio/routing')
async def audio_routing_status():
    """获取音频路由状态概览"""
    import audio_manager as am
    return _json(am.get_audio_routing_status())

@app.get('/api/audio/usb-devices')
async def audio_usb_devices():
    """获取USB音频设备列表"""
    import audio_manager as am
    return _json(am.get_usb_audio_devices())

@app.get('/api/video/streams')
async def video_streams():
    """列出所有活跃视频流"""
    import route_manager
    return _json(route_manager.get_video_streams())

@app.post('/api/video/route/stream')
async def video_route_stream(data: dict = Body(...)):
    """将视频流路由到指定输出"""
    stream_id = data.get('stream_id')
    target_device = data.get('target_device')
    if stream_id is None or not target_device:
        raise InvalidParamError("stream_id and target_device are required")
    logger.debug(f"路由视频流: {stream_id} -> {target_device}")
    import route_manager
    return _json(route_manager.route_video_stream(stream_id, target_device))

@app.delete('/api/video/route/stream')
async def video_unlink_stream(data: dict = Body(...)):
    """断开视频流路由"""
    stream_id = data.get('stream_id')
    link_id = data.get('link_id')
    if stream_id is None:
        raise InvalidParamError("stream_id is required")
    import video_manager
    return _json(video_manager.unlink_video_stream(stream_id, link_id))

@app.post('/api/video/display-output')
async def video_set_display_output(data: dict = Body(...)):
    """配置DRM显示输出"""
    connector = data.get('connector')
    if not connector:
        raise InvalidParamError("connector is required")
    import video_manager
    return _json(video_manager.set_display_output(connector, data.get('resolution'), data.get('refresh_rate')))

@app.get('/api/bluetooth/audio-sources')
async def bluetooth_audio_sources():
    """列出蓝牙音频输入设备"""
    return _json(bluetooth_manager.get_bluetooth_audio_sources())

@app.get('/api/bluetooth/audio-profiles/{mac}')
async def bluetooth_audio_profiles(mac: str):
    """获取蓝牙设备音频Profile列表"""
    _validate_mac(mac)
    return _json(bluetooth_manager.get_bluetooth_audio_profiles(mac))

@app.post('/api/bluetooth/audio-profile/switch')
async def bluetooth_switch_profile(data: dict = Body(...)):
    """切换蓝牙设备音频Profile"""
    mac = data.get('mac')
    profile = data.get('profile')
    if not mac or not profile:
        raise InvalidParamError("MAC and profile are required")
    _validate_mac(mac)
    logger.debug(f"切换蓝牙Profile: {mac} -> {profile}")
    return _json(bluetooth_manager.switch_bluetooth_profile(mac, profile))

@app.post('/api/bluetooth/microphone/enable')
async def bluetooth_enable_microphone(data: dict = Body(...)):
    """启用蓝牙麦克风"""
    mac = data.get('mac')
    if not mac:
        raise InvalidParamError("MAC is required")
    _validate_mac(mac)
    logger.debug(f"启用蓝牙麦克风: {mac}")
    return _json(bluetooth_manager.enable_bluetooth_microphone(mac))

@app.post('/api/bluetooth/microphone/disable')
async def bluetooth_disable_microphone(data: dict = Body(...)):
    """禁用蓝牙麦克风（切回A2DP）"""
    mac = data.get('mac')
    if not mac:
        raise InvalidParamError("MAC is required")
    _validate_mac(mac)
    logger.debug(f"禁用蓝牙麦克风: {mac}")
    return _json(bluetooth_manager.disable_bluetooth_microphone(mac))

@app.post('/api/bluetooth/audio-source/route')
async def bluetooth_route_source(data: dict = Body(...)):
    """将蓝牙音频输入路由到指定应用"""
    source_name = data.get('source_name')
    target_app = data.get('target_app')
    if not source_name or not target_app:
        raise InvalidParamError("source_name and target_app are required")
    import route_manager
    return _json(route_manager.route_bluetooth_source(source_name, target_app))

@app.get('/api/pipewire/links')
async def pipewire_links():
    """获取所有PipeWire链接"""
    import route_manager
    return _json(route_manager.get_all_links())


if __name__ == '__main__':
    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    logger.info("MediaHub 服务启动")
    _startup_self_heal()
    server_port = int(os.environ.get('TRIM_SERVICE_PORT', '33001'))
    logger.info(f"FastAPI 服务监听 0.0.0.0:{server_port}")
    uvicorn.run(app, host='0.0.0.0', port=server_port, log_level='warning', access_log=False)
