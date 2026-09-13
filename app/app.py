import os
import sys
import logging
import threading
from contextlib import asynccontextmanager
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
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
    # 版本埋点：fastapi < 0.93 时 FastAPI(lifespan=) 会被 **extra 静默吞掉，
    # 导致本函数从不执行。留下版本记录便于日后一眼判断运行环境能力边界。
    try:
        import fastapi as _fastapi_mod
        import starlette as _starlette_mod
        logger.info(
            "lifespan startup 进入（fastapi=%s starlette=%s uvicorn=%s）",
            getattr(_fastapi_mod, '__version__', '?'),
            getattr(_starlette_mod, '__version__', '?'),
            getattr(uvicorn, '__version__', '?'),
        )
    except Exception:
        logger.info("lifespan startup 进入（版本信息获取失败）")

    # 以下每个子步骤独立兜底：uvicorn 以 lifespan='on' 运行时，
    # 本函数抛出异常会导致整个进程启动失败，必须逐项隔离。
    try:
        event_bus.set_loop(asyncio.get_running_loop())
    except Exception:
        logger.exception("初始化事件总线事件循环失败")
    try:
        event_detector.start()
    except Exception:
        logger.exception("启动事件检测器失败")
    try:
        pw_mon_listener.start()
    except Exception:
        logger.exception("启动 pw-mon 监听器失败")
    # 确保 Socket 文件权限可控。uvicorn 0.17.6 绑定 UDS 时默认 chmod 0o666，
    # 意味着本机任意用户都能直连业务端口、绕过 fnOS 网关鉴权，故收窄为 0o660
    # （仅属主 root 与同组可读写）。
    # ⚠ 若 fnOS 网关进程既非 root 也不在该 socket 属组，收窄会导致前端 502；
    # 此时可用环境变量 PIPEBRIDGE_SOCKET_MODE=666 临时放开，再排查网关身份。
    try:
        if os.path.exists(GATEWAY_SOCKET):
            _mode_raw = os.environ.get('PIPEBRIDGE_SOCKET_MODE', '660')
            try:
                _mode = int(_mode_raw, 8)
            except ValueError:
                logger.warning("PIPEBRIDGE_SOCKET_MODE=%s 非法八进制，回退 0o660", _mode_raw)
                _mode = 0o660
            _before = oct(os.stat(GATEWAY_SOCKET).st_mode & 0o777)
            os.chmod(GATEWAY_SOCKET, _mode)
            logger.info("网关 Socket 权限 %s -> %s: %s", _before, oct(_mode), GATEWAY_SOCKET)
    except OSError:
        logger.exception("设置网关 Socket 权限失败")
    try:
        from system_manager import WPConfigManager
        wpc = WPConfigManager()
        # 每个子步骤独立兜底：任一步失败不得影响其余步骤与服务启动
        for _step_name, _step in (
            ("部署 no-suspend 规则", wpc.deploy_no_suspend_rule),
            # 蜂鸣器禁用采用唯一方案：清理历史遗留规则(WirePlumber 屏蔽规则 + 旧 modprobe.d 黑名单)
            ("清理历史蜂鸣器屏蔽规则", wpc.cleanup_pcspkr_block_rules),
            # 蜂鸣器禁用唯一方案：driver_override 物理拦截，阻止驱动绑定 platform-pcspkr，
            # 即使 snd_pcsp 被加载也不注册声卡，PipeWire 无 sink 可路由 → 无声。
            # 主拦截已前移到 __main__（早于 PipeWire 拉起），此处为幂等二次兜底。
            ("蜂鸣器 driver_override 拦截(二次兜底)", wpc.block_pcspkr_via_override),
            # no_suspend 规则写入 + 清理旧屏蔽规则后需重启 WirePlumber 使其生效
            ("重启 WirePlumber 使规则生效", wpc.restart_wireplumber),
        ):
            try:
                _step()
            except Exception:
                logger.exception("启动音频初始化子步骤失败: %s", _step_name)
    except Exception:
        # 记录完整堆栈，避免初始化步骤静默失败难以排查
        logger.exception("启动时音频初始化失败")
    # 注意：uvicorn 0.17.6 不支持 lifespan state，此处只能裸 yield，不可 yield dict
    yield
    logger.info("FastAPI shutdown，清理资源...")
    for _name, _fn in (
        ("停止 pw-mon 监听器", pw_mon_listener.stop),
        ("停止事件检测器", event_detector.stop),
        ("置位保活停止标志", _keepalive_stop_event.set),
    ):
        try:
            _fn()
        except Exception:
            logger.exception("关闭子步骤失败: %s", _name)

# 业务应用：所有路由以 / 或 /api 开头。
# 请求进入前会先经父应用的 path 规范化中间件（见文件末尾），
# 因此无论网关保留前缀、剥离前缀，还是反代到任意路径，请求都能命中内部 /... 路由。
app = FastAPI(title="PipeBridge")

# ★ 关键：FastAPI(lifespan=...) 形参自 0.93.0 才引入。
# 本项目依赖由系统 apt 提供（Debian 12 → fastapi 0.92.0），0.92 的 __init__ 把未知
# 关键字塞进 **extra 静默丢弃，导致 lifespan 从未被调用（事件总线/热插拔检测/
# pw-dump 监听/蜂鸣器拦截/Socket 权限收窄 全部失效且无任何告警）。
# starlette 0.26.1 的 Router 原生支持 lifespan_context，直接注入即可，无需升级依赖。
_lifespan_bound = False
try:
    if hasattr(app.router, 'lifespan_context'):
        app.router.lifespan_context = lifespan
        _lifespan_bound = True
except Exception:
    logger.exception("注入 lifespan_context 失败，将回退到 on_event 钩子")

if not _lifespan_bound:
    # 兜底：极老/极新的 starlette 无 lifespan_context 时，用事件钩子手工驱动同一个上下文管理器
    _lifespan_cm = {}

    @app.on_event("startup")
    async def _lifespan_startup_fallback():
        cm = lifespan(app)
        _lifespan_cm['cm'] = cm
        await cm.__aenter__()

    @app.on_event("shutdown")
    async def _lifespan_shutdown_fallback():
        cm = _lifespan_cm.pop('cm', None)
        if cm is not None:
            await cm.__aexit__(None, None, None)

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

def _resolve_base_href(request):
    # 推导前端资源基址目录：优先用归一化前的原始外层 path(兼容 Lucky/网关
    # 反代到任意子路径),回退到当前请求 path。始终返回以 / 结尾的目录,
    # 使 index.html 内相对资源(css/js/images)与 /api 都基于该目录解析。
    original = ''
    try:
        original = (request.scope.get('state') or {}).get('gateway_original_path', '')
    except Exception:
        original = ''
    path = original or request.url.path or '/'
    if path.endswith('/'):
        directory = path
    elif '.' in path.rsplit('/', 1)[-1]:
        # 末段是带扩展名的文件(如 index.html),取其所在目录
        directory = path[:path.rfind('/') + 1] or '/'
    else:
        # 末段是无尾斜杠的目录(如网关 /app/PipeBridge),补足尾斜杠
        directory = path + '/'
    return directory or '/'


@app.get('/')
def index(request: Request):
    # 后端注入确定的 <base>,不再依赖前端 JS 猜测页面目录。
    # 这样无论飞牛自带网关还是 Lucky 反代到任意子路径,css/js/images 与
    # /api 都能稳定基于正确前缀解析,避免资源 404 导致白屏。
    base_href = _resolve_base_href(request)
    try:
        with open(os.path.join(web_dir, 'index.html'), 'r', encoding='utf-8') as f:
            html = f.read()
    except OSError:
        logger.exception("读取 index.html 失败")
        return JSONResponse(status_code=500, content={'success': False, 'error': 'index not found'})
    if '<base ' not in html:
        inject = f'<base href="{base_href}">'
        html = html.replace('<head>', '<head>\n    ' + inject, 1)
    return HTMLResponse(content=html)

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
            original_path = scope.get('path', '')
            new_path = _normalize_gateway_path(original_path)
            if new_path != original_path:
                scope = dict(scope)
                scope['path'] = new_path
                scope['raw_path'] = new_path.encode('utf-8')
                # 保留归一化前的原始外层 path，供 index 路由推导 <base>：
                # Lucky/网关可能把应用反代到任意外层前缀，前端相对资源必须基于
                # 该前缀解析，否则 css/js 落到上级目录 404。
                scope['state'] = dict(scope.get('state') or {})
                scope['state']['gateway_original_path'] = original_path
        await self.app(scope, receive, send)


# 父级 ASGI 可调用对象：包裹业务 app，承担网关 path 归一化。
# lifespan 由业务 app 自身持有（纯 ASGI 中间件会透传 lifespan 事件）。
root_app = GatewayPathMiddleware(app)


if __name__ == '__main__':
    lifecycle.register_signal_handlers()
    logger.info("PipeBridge 服务启动")

    # ★ 蜂鸣器拦截必须最早执行：startup_self_heal() 会拉起 PipeWire/WirePlumber，
    # 一旦 pcspkr 已注册声卡，PipeWire 会枚举出 pcspkr sink 并可能抢占默认输出。
    # driver_override 写在声卡枚举之前，才能真正做到"先卸载、再阻止绑定"。
    # 该 sysfs 值不持久，每次启动都需重设；lifespan 内保留一次幂等兜底。
    try:
        from system_manager import WPConfigManager as _WPC
        _WPC().block_pcspkr_via_override()
    except Exception:
        logger.exception("启动前蜂鸣器拦截失败（将在 lifespan 中重试）")

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
        # lifespan='auto'（默认）会在协议不被支持时静默降级，历史上正是它掩盖了
        # lifespan 从未执行的事实；显式 'on' 让任何 lifespan 故障立即暴露。
        # 前置条件：lifespan 内部各子步骤已逐项 try/except，不会因单点失败导致进程退出。
        lifespan='on',
        # 'warning' 会抬高 uvicorn.error 阈值，吞掉 "Application startup complete."
        # 等关键启动证据，改为 info 以便排障（access_log 仍关闭，不会刷屏）。
        log_level='info',
        access_log=False,
        # 适当延长 keep-alive，减少 SSE 长连接场景下客户端骤断触发的
        # h11 SEND_BODY/ConnectionClosed 协议竞态告警频率。
        timeout_keep_alive=75,
    )
