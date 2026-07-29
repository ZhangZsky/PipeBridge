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

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'DEBUG').upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.WARNING),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('PipeBridge')
logging.getLogger('uvicorn.access').disabled = True

# 服务端口
SERVICE_PORT = int(os.environ.get('TRIM_SERVICE_PORT', '33001'))

# 保活停止事件
_keepalive_stop_event = threading.Event()
lifecycle.setup(_keepalive_stop_event)


# FastAPI 生命周期管理
@asynccontextmanager
async def lifespan(app):
    import asyncio
    event_bus.set_loop(asyncio.get_running_loop())
    event_detector.start()
    try:
        from system_manager import WPConfigManager
        wpc = WPConfigManager()
        wpc.deploy_pcspkr_blacklist()
        wpc.deploy_no_suspend_rule()
        # 部署蜂鸣器降权规则，防止蓝牙/USB 声卡消失后蜂鸣器被 fallback 选为默认输出
        wpc.deploy_pcspkr_deprioritize_rule()
        # 黑名单并卸载 pcspkr（主板蜂鸣器 input/evdev 通路），从根源杜绝
        # 蓝牙/USB 声卡断开后 PC Speaker 长响（该通路不经过 PipeWire，静音无效）
        wpc.blacklist_and_unload_pcspkr()
        # 规则文件写入后 WirePlumber 不会自动重载，必须重启才能让
        # monitor.alsa.rules（降权/防挂起）真正生效
        wpc.restart_wireplumber()
        # 确保 snd_pcsp 声卡模块已加载（仅用于蜂鸣器设备显示，已被 PipeWire 静音）
        from audio_manager import _ensure_pcspkr_module, _set_default_volumes, _mute_pcspkr_sinks
        _ensure_pcspkr_module()
        # 将所有设备默认音量设置为100%（覆盖 WirePlumber 默认的 40%）
        _set_default_volumes()
        # 启动时即静音蜂鸣器 sink，避免其保持 100% 未静音导致 fallback 漏音
        # WirePlumber 刚重启，等待其重新枚举 ALSA 节点后再静音，避免节点未就绪静音落空
        import time as _time
        _time.sleep(1.5)
        _mute_pcspkr_sinks()
    except Exception:
        # 记录完整堆栈，避免蜂鸣器屏蔽等初始化步骤静默失败难以排查
        logger.exception("启动时蜂鸣器屏蔽/音频初始化失败")
    yield
    logger.info("FastAPI shutdown，清理资源...")
    event_detector.stop()
    _keepalive_stop_event.set()


app = FastAPI(title="PipeBridge", lifespan=lifespan)


# 全局业务异常处理器
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


# 禁用前端缓存的中间件
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

# 挂载路由
app.include_router(bluetooth_router)
app.include_router(audio_router)
app.include_router(video_router)
app.include_router(system_router)
app.include_router(events_router)

web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
app.mount("/css", StaticFiles(directory=os.path.join(web_dir, 'css')), name="css")
app.mount("/js", StaticFiles(directory=os.path.join(web_dir, 'js')), name="js")
app.mount("/images", StaticFiles(directory=os.path.join(web_dir, 'images')), name="images")


# 返回前端首页
@app.get('/')
def index():
    return FileResponse(os.path.join(web_dir, 'index.html'))


# 返回前端配置文件
@app.get('/config')
def serve_web_config():
    config_path = os.path.join(web_dir, 'config')
    if os.path.exists(config_path):
        return FileResponse(config_path)
    return JSONResponse(status_code=404, content={'success': False, 'error': 'Config file not found'})


# 返回 favicon
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
