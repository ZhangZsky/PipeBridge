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

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.WARNING),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('PipeBridge')
logging.getLogger('uvicorn.access').disabled = True

def _resolve_service_port():
    # 解析服务端口，非法值回退默认 33001，作为 CORS 与 uvicorn 的唯一端口来源
    try:
        port = int(os.environ.get('TRIM_SERVICE_PORT', '33001'))
        if 1 <= port <= 65535:
            return port
    except (ValueError, TypeError):
        pass
    return 33001

SERVICE_PORT = _resolve_service_port()

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
        # 蜂鸣器降权：保留设备可显示/测试，但禁止 WirePlumber 将其选为默认输出以防长响
        wpc.deploy_pcspkr_deprioritize_rule()
        # 规则文件写入后需重启 WirePlumber 才能使降权/防挂起规则生效
        wpc.restart_wireplumber()
        # 加载 snd_pcsp 声卡模块，仅用于蜂鸣器设备显示与播放测试
        from audio_manager import _ensure_pcspkr_module
        _ensure_pcspkr_module()
    except Exception:
        # 记录完整堆栈，避免初始化步骤静默失败难以排查
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
    # 允许任意来源跨域，不使用 Cookie 凭证故通配来源符合 CORS 规范
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
    logger.info(f"FastAPI 服务监听 0.0.0.0:{SERVICE_PORT}")
    uvicorn.run(app, host='0.0.0.0', port=SERVICE_PORT, log_level='warning', access_log=False)
