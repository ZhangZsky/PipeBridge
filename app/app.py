import os
import sys
import logging
import threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn
from exceptions import MediaHubError, InvalidParamError
import lifecycle
from routes.bluetooth import router as bluetooth_router
from routes.audio import router as audio_router
from routes.video import router as video_router
from routes.system import router as system_router
from routes.events import router as events_router
from event_bus import event_bus
from event_detector import event_detector

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'DEBUG').upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.WARNING),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('MediaHub')
logging.getLogger('uvicorn.access').disabled = True

app = FastAPI(title="MediaHub")


@app.on_event("startup")
async def _on_startup():
    import asyncio
    event_bus.set_loop(asyncio.get_running_loop())
    event_detector.start()
    # 移除旧版蜂鸣器黑名单规则（如果存在），让蜂鸣器设备正常注册
    try:
        from wp_config_manager import WpConfigManager
        WpConfigManager().deploy_pcspkr_blacklist()
    except Exception:
        pass

_keepalive_stop_event = threading.Event()
lifecycle.setup(_keepalive_stop_event)


# 全局业务异常处理器
@app.exception_handler(MediaHubError)
async def mediahub_error_handler(request, exc):
    return JSONResponse(
        status_code=200,
        content={'success': False, 'error': exc.message, 'code': exc.code}
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"http://localhost:{int(os.environ.get('TRIM_SERVICE_PORT', '33001'))}", f"http://127.0.0.1:{int(os.environ.get('TRIM_SERVICE_PORT', '33001'))}"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 禁用前端缓存的中间件
class NoCacheMiddleware(BaseHTTPMiddleware):
    # 非 API 请求加 no-cache 头
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
    raise InvalidParamError("Config file not found")


# 返回 favicon
@app.get('/favicon.ico')
def serve_favicon():
    favicon_path = os.path.join(web_dir, 'favicon.ico')
    if os.path.exists(favicon_path):
        return FileResponse(favicon_path)
    icon_path = os.path.join(web_dir, 'images', 'icon_64.png')
    if os.path.exists(icon_path):
        return FileResponse(icon_path)
    raise InvalidParamError("Not found")


if __name__ == '__main__':
    lifecycle.register_signal_handlers()
    logger.info("MediaHub 服务启动")
    lifecycle._startup_self_heal()
    try:
        server_port = int(os.environ.get('TRIM_SERVICE_PORT', '33001'))
        assert 1 <= server_port <= 65535
    except (ValueError, AssertionError):
        server_port = 33001
    logger.info(f"FastAPI 服务监听 0.0.0.0:{server_port}")
    uvicorn.run(app, host='0.0.0.0', port=server_port, log_level='warning', access_log=False)
