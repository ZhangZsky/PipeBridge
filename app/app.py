import os
import sys
import logging
import threading
from contextlib import asynccontextmanager
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn
from exceptions import PipeBridgeError
import lifecycle
from routes.bluetooth import router as bluetooth_router
from routes.audio import router as audio_router
from routes.video import router as video_router
from routes.system import router as system_router
from routes.events import router as events_router
from event_system import event_bus, event_detector
from pw_mon_listener import pw_mon_listener

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'DEBUG').upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.WARNING),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('PipeBridge')
logging.getLogger('uvicorn.access').disabled = True

SERVICE_PORT = int(os.environ.get('TRIM_SERVICE_PORT', '33001'))

_keepalive_stop_event = threading.Event()
lifecycle.setup(_keepalive_stop_event)

@asynccontextmanager
async def lifespan(app):
    import asyncio
    event_bus.set_loop(asyncio.get_running_loop())
    event_detector.start()
    pw_mon_listener.start()
    try:
        from system_manager import WPConfigManager
        wpc = WPConfigManager()
        wpc.deploy_no_suspend_rule()
        # 部署蜂鸣器降权规则：保留蜂鸣器设备（可显示/播放测试），
        # 但绝不让 WirePlumber fallback 把它选为默认输出，防止 PC Speaker 长响
        wpc.deploy_pcspkr_deprioritize_rule()
        # 规则文件写入后 WirePlumber 不会自动重载，必须重启才能让
        # monitor.alsa.rules（降权/防挂起）真正生效
        wpc.restart_wireplumber()
        # 加载 snd_pcsp 声卡模块（仅用于蜂鸣器设备显示与播放测试，运行时被静音）
        from audio_manager import _ensure_pcspkr_module, _set_default_volumes, _mute_pcspkr_sinks
        _ensure_pcspkr_module()
        # WirePlumber 刚重启，等待其完成 ALSA 节点枚举。
        # 若等待不足，_set_default_volumes 会因节点未就绪而漏设，导致设备停留在 WirePlumber 默认 40% 音量。
        import time as _time
        _time.sleep(2.0)
        # 将所有设备默认音量设置为100%（覆盖 WirePlumber 默认的 40%）
        _set_default_volumes()
        # 再次延迟后静音蜂鸣器，确保节点已就绪避免静音落空
        _time.sleep(0.5)
        _mute_pcspkr_sinks()
        # 二次校准：部分设备（尤其是蓝牙/USB）可能在首次设置时仍未就绪，
        # 延迟后再次设置确保音量生效
        _time.sleep(1.0)
        _set_default_volumes()
        _mute_pcspkr_sinks()
    except Exception:
        # 记录完整堆栈，避免蜂鸣器降权等初始化步骤静默失败难以排查
        logger.exception("启动时蜂鸣器降权/音频初始化失败")
    yield
    logger.info("FastAPI shutdown，清理资源...")
    pw_mon_listener.stop()
    event_detector.stop()
    _keepalive_stop_event.set()

app = FastAPI(title="PipeBridge", lifespan=lifespan)

@app.exception_handler(PipeBridgeError)
async def pipebridge_error_handler(request, exc):
    return JSONResponse(
        status_code=200,
        content={'success': False, 'error': exc.message, 'code': exc.code}
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
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

app.include_router(bluetooth_router)
app.include_router(audio_router)
app.include_router(video_router)
app.include_router(system_router)
app.include_router(events_router)

web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
app.mount("/css", StaticFiles(directory=os.path.join(web_dir, 'css')), name="css")
app.mount("/js", StaticFiles(directory=os.path.join(web_dir, 'js')), name="js")
app.mount("/images", StaticFiles(directory=os.path.join(web_dir, 'images')), name="images")

@app.get('/')
def index():
    return FileResponse(os.path.join(web_dir, 'index.html'))

@app.get('/config')
def serve_web_config():
    config_path = os.path.join(web_dir, 'config')
    if os.path.exists(config_path):
        return FileResponse(config_path)
    return JSONResponse(status_code=404, content={'success': False, 'error': 'Config file not found'})

@app.get('/favicon.ico')
def serve_favicon():
    favicon_path = os.path.join(web_dir, 'favicon.ico')
    if os.path.exists(favicon_path):
        return FileResponse(favicon_path)
    icon_path = os.path.join(web_dir, 'images', 'icon_64.png')
    if os.path.exists(icon_path):
        return FileResponse(icon_path)
    return JSONResponse(status_code=404, content={'success': False, 'error': 'Not found'})

if __name__ == '__main__':
    lifecycle.register_signal_handlers()
    logger.info("PipeBridge 服务启动")
    lifecycle.startup_self_heal()
    try:
        server_port = int(os.environ.get('TRIM_SERVICE_PORT', '33001'))
        assert 1 <= server_port <= 65535
    except (ValueError, AssertionError):
        server_port = 33001
    logger.info(f"FastAPI 服务监听 0.0.0.0:{server_port}")
    uvicorn.run(app, host='0.0.0.0', port=server_port, log_level='warning', access_log=False)
