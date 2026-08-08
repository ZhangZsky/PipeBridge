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


def _thread_excepthook(args):
    # 兜底捕获所有线程(含 daemon 后台线程)未处理异常，统一转 logger，
    # 避免默认钩子把裸堆栈直接打到 stderr 造成日志刷屏。
    if issubclass(args.exc_type, SystemExit):
        return
    thread_name = args.thread.name if args.thread else '未知线程'
    logger.error(
        "后台线程 %s 未捕获异常: %s",
        thread_name,
        args.exc_value,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


threading.excepthook = _thread_excepthook

# 统一网关前缀（须与 app/ui/config 的 gatewayPrefix 保持一致），飞牛 fnOS 通过该前缀转发请求
GATEWAY_PREFIX = os.environ.get('TRIM_GATEWAY_PREFIX', '/app/PipeBridge')

def _resolve_gateway_socket():
    # 统一网关模式下 uvicorn 监听的 Unix Socket 路径。
    # gatewaySocket 声明的文件名应位于已安装应用的 target 目录（TRIM_APPDEST）下。
    sock_name = os.environ.get('TRIM_GATEWAY_SOCKET', 'app.sock')
    app_dest = os.environ.get('TRIM_APPDEST', '') or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(app_dest, sock_name)

GATEWAY_SOCKET = _resolve_gateway_socket()

_keepalive_stop_event = threading.Event()
lifecycle.setup(_keepalive_stop_event)

@asynccontextmanager
async def lifespan(app):
    import asyncio
    event_bus.set_loop(asyncio.get_running_loop())
    event_detector.start()
    pw_mon_listener.start()
    # 确保 Socket 文件对 fnOS 网关可读写（uvicorn 创建后权限可能过严）
    try:
        if os.path.exists(GATEWAY_SOCKET):
            os.chmod(GATEWAY_SOCKET, 0o666)
    except OSError:
        logger.exception("设置网关 Socket 权限失败")
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

# 业务应用：所有路由以 / 或 /api 开头。
# 请求进入前会先经父应用的 path 规范化中间件（见文件末尾），
# 因此无论网关保留前缀、剥离前缀，还是反代到任意路径，请求都能命中内部 /... 路由。
app = FastAPI(title="PipeBridge", lifespan=lifespan)

# 业务异常 code -> HTTP 状态码映射。
# 未列出的 code(含 INTERNAL_ERROR/COMMAND_ERROR)默认 500，表示服务端未能完成操作；
# 客户端可修正的输入/状态类错误用 4xx，便于前端与网关按标准 HTTP 语义区分对待。
_ERROR_STATUS = {
    'DEVICE_NOT_FOUND': 404,
    'INVALID_PARAM': 400,
    'PAIRING_NEED_PIN': 400,
    'PROFILE_UNAVAILABLE': 400,
    'CONFIG_ERROR': 400,
}

@app.exception_handler(PipeBridgeError)
async def pipebridge_error_handler(request, exc):
    return JSONResponse(
        status_code=_ERROR_STATUS.get(exc.code, 500),
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

web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui')
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

# 已知内部路由锚点：请求 path 中出现这些片段即视为内部路径的起点。
# 用于将网关/反代附加的任意外层前缀归一化剥离。
_INTERNAL_ANCHORS = ('/api/', '/css/', '/js/', '/images/', '/config', '/favicon.ico')


def _normalize_gateway_path(path):
    # 将来自飞牛统一网关任意转发方式的请求 path 收敛到业务内部路由：
    #   1) 网关【保留】完整前缀   /app/PipeBridge[/...] → 剥离前缀
    #   2) 反向代理到【任意】子路径 /x/y/api/... → 从内部锚点起截取
    #   3) 网关【剥离】前缀 / 直连  /... → 原样透传
    if GATEWAY_PREFIX and GATEWAY_PREFIX != '/':
        if path == GATEWAY_PREFIX:
            return '/'
        if path.startswith(GATEWAY_PREFIX + '/'):
            return path[len(GATEWAY_PREFIX):] or '/'
    for anchor in _INTERNAL_ANCHORS:
        idx = path.find(anchor)
        if idx > 0:
            return path[idx:]
    return path


class GatewayPathMiddleware:
    # 纯 ASGI 中间件（非 BaseHTTPMiddleware），在请求进入业务 app 前重写 path。
    # 相比“把 app 同时挂载到前缀与根”的方案，这里能兼容网关反代到任意子路径，
    # 且不会触发 Starlette 挂载点无尾斜杠时的 307 重定向陷阱。
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get('type') == 'http':
            new_path = _normalize_gateway_path(scope.get('path', ''))
            if new_path != scope.get('path'):
                scope = dict(scope)
                scope['path'] = new_path
                scope['raw_path'] = new_path.encode('utf-8')
        await self.app(scope, receive, send)


# 父级 ASGI 可调用对象：包裹业务 app，承担网关 path 归一化。
# lifespan 由业务 app 自身持有（纯 ASGI 中间件会透传 lifespan 事件）。
root_app = GatewayPathMiddleware(app)


if __name__ == '__main__':
    lifecycle.register_signal_handlers()
    logger.info("PipeBridge 服务启动")
    lifecycle.startup_self_heal()

    # 清理可能残留的旧 Socket 文件，避免绑定失败
    try:
        if os.path.exists(GATEWAY_SOCKET):
            os.unlink(GATEWAY_SOCKET)
    except OSError:
        logger.exception(f"清理残留 Socket 失败: {GATEWAY_SOCKET}")
    logger.info(f"FastAPI 服务监听 Unix Socket: {GATEWAY_SOCKET}（统一网关模式，前缀 {GATEWAY_PREFIX}）")
    uvicorn.run(
        root_app,
        uds=GATEWAY_SOCKET,
        log_level='warning',
        access_log=False,
        # 适当延长 keep-alive，减少 SSE 长连接场景下客户端骤断触发的
        # h11 SEND_BODY/ConnectionClosed 协议竞态告警频率。
        timeout_keep_alive=75,
    )
