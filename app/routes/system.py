import io
import os
import zipfile
import logging
from collections import deque
from datetime import datetime
from fastapi import APIRouter, Body
from fastapi.responses import StreamingResponse
import bluetooth_manager
import system_manager
import route_manager
from exceptions import InvalidParamError, CommandError
from routes.helpers import _json, require_param
from utils import run_command
import platform_paths
from event_system import event_bus

logger = logging.getLogger('PipeBridge')

router = APIRouter(tags=["system"])

# 日志文件落地目录：与 cmd/main 保持一致，运行日志 app.log 与安装日志 install.log 均位于 TRIM_PKGVAR。
# 未注入环境变量时(如本地调试)回退到 app 目录，避免 KeyError。
def _log_dir():
    return os.environ.get('TRIM_PKGVAR') or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 日志类型 → (文件名, 导出时的目标文件名)
_LOG_FILES = {
    'runtime': ('app.log', 'runtime.log'),
    'install': ('install.log', 'installation.log'),
}

# 预览尾部行数上限，防止单次拉取过大导致前端卡顿；导出走全量不受此限制。
_MAX_PREVIEW_LINES = 5000

@router.get('/api/system/overview')
def system_overview():
    logger.debug("获取系统概览")
    return _json(system_manager.get_system_overview())

@router.get('/api/system/dependencies')
def system_dependencies():
    return _json(system_manager.get_all_status())

def _read_log_tail(path, max_lines):
    # 只读文件尾部 max_lines 行。用 deque(maxlen) 边读边淘汰，避免整文件载入内存。
    # 文件不存在/无权限时返回空，不抛错(前端首次装/未产生日志属正常态)。
    if not os.path.isfile(path):
        return [], 0
    total = 0
    tail = deque(maxlen=max_lines)
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                total += 1
                tail.append(line.rstrip('\n'))
    except OSError as e:
        logger.warning(f"读取日志失败 {path}: {e}")
        return [], 0
    return list(tail), total

@router.get('/api/system/logs')
def system_logs(type: str = 'runtime', lines: int = 500):
    # 日志预览：返回指定类型日志的尾部若干行供前端展示。
    #   type  : runtime(运行日志 app.log) | install(安装日志 install.log)
    #   lines : 尾部行数，1..5000，超限截断
    if type not in _LOG_FILES:
        raise InvalidParamError(f"不支持的日志类型: {type}，可选: {', '.join(_LOG_FILES.keys())}")
    try:
        n = max(1, min(int(lines), _MAX_PREVIEW_LINES))
    except (TypeError, ValueError):
        n = 500
    filename = _LOG_FILES[type][0]
    path = os.path.join(_log_dir(), filename)
    tail, total = _read_log_tail(path, n)
    return _json({
        'type': type,
        'file': filename,
        'lines': tail,       # 尾部行数组
        'returned': len(tail),
        'total': total,      # 文件总行数(用于前端提示"仅显示尾部")
        'exists': os.path.isfile(path),
    })

@router.get('/api/system/logs/export')
def system_logs_export():
    # 全量导出：把安装日志与运行日志各作为独立文件打进一个 zip 返回。
    # 全量读取原始文件(不受预览 lines 限制)；缺失的日志写占位说明，保证 zip 内两文件恒在。
    log_dir = _log_dir()
    buf = io.BytesIO()
    try:
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for _type, (src_name, out_name) in _LOG_FILES.items():
                src_path = os.path.join(log_dir, src_name)
                if os.path.isfile(src_path):
                    zf.write(src_path, arcname=out_name)
                else:
                    zf.writestr(out_name, f"# {out_name} 暂无内容(日志文件 {src_name} 尚未生成)\n")
    except OSError as e:
        logger.error(f"打包日志失败: {e}")
        raise CommandError(f"日志导出失败: {e}")
    buf.seek(0)
    fname = f"PipeBridge-logs-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    logger.info(f"导出全量日志: {fname}")
    return StreamingResponse(
        buf,
        media_type='application/zip',
        headers={'Content-Disposition': f'attachment; filename="{fname}"'},
    )

@router.post('/api/system/fix')
def system_fix():
    logger.info("一键修复系统依赖")
    result = system_manager.fix_all()
    pkg_ok = 'error' not in result.get('packages', {})
    pw_ok = 'error' not in result.get('pipewire', {})
    svc_ok = 'error' not in result.get('services', {})
    bt_audio_ok = 'error' not in result.get('bluetooth_audio', {})
    overall = pkg_ok and pw_ok and svc_ok and bt_audio_ok
    logger.info(f"修复完成: packages={pkg_ok}, pipewire={pw_ok}, services={svc_ok}, bluetooth_audio={bt_audio_ok}")
    if not overall:
        raise CommandError('部分修复失败')
    event_bus.publish('system.changed', {})
    return _json(result)

@router.post('/api/system/reconnect')
def system_reconnect(data: dict = Body(...)):
    enabled = require_param(data, 'enabled', "enabled field is required", allow_empty=True)
    bluetooth_manager.set_reconnect_enabled(bool(enabled))
    event_bus.publish('bluetooth.changed', {})
    return _json({"message": "ok"})

@router.get('/api/health')
def health_check():
    overview = system_manager.get_system_overview()
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

@router.get('/api/pipewire/links')
def pipewire_links():
    return _json(route_manager.get_all_links())

_CONTROLLABLE_SERVICES = {
    'bluetooth': '蓝牙服务',
    'dbus': 'D-Bus 系统消息总线',
}

def _require_service(data):
    # 取并校验 service 参数(必填 + 白名单),返回合法 service 名
    service = require_param(data, 'service', "service 参数必填")
    if service not in _CONTROLLABLE_SERVICES:
        raise InvalidParamError(f"不支持的服务: {service}，可选: {', '.join(_CONTROLLABLE_SERVICES.keys())}")
    return service

@router.post('/api/system/service/restart')
def system_service_restart(data: dict = Body(...)):
    service = _require_service(data)

    # 蓝牙服务重启前可选执行 USB 适配器硬件重置，解决适配器卡死时单纯 systemctl restart 无效的问题
    usb_reset_done = False
    if service == 'bluetooth' and data.get('usb_reset'):
        from bluetooth_manager import _try_usb_reset_adapter
        logger.info("重启蓝牙服务前执行 USB 适配器重置...")
        try:
            usb_reset_done = _try_usb_reset_adapter()
        except Exception as e:
            logger.warning(f"USB 适配器重置失败(继续重启服务): {e}")
        import time as _time
        _time.sleep(2)

    logger.info(f"重启服务: {service} (usb_reset={usb_reset_done})")
    result = run_command(f"{platform_paths.CMD_SYSTEMCTL} restart {service}", timeout=30)
    if not result['success']:
        raise CommandError(f"重启 {service} 失败: {result.get('stderr', '')[:200]}")
    msg = f"{_CONTROLLABLE_SERVICES[service]}已重启"
    if usb_reset_done:
        msg += "（含 USB 适配器重置）"
    event_bus.publish('system.changed', {})
    return _json({"message": msg, "usb_reset": usb_reset_done})

@router.post('/api/system/service/start')
def system_service_start(data: dict = Body(...)):
    service = _require_service(data)
    logger.info(f"启动服务: {service}")
    result = run_command(f"{platform_paths.CMD_SYSTEMCTL} start {service}", timeout=30)
    if not result['success']:
        raise CommandError(f"启动 {service} 失败: {result.get('stderr', '')[:200]}")
    event_bus.publish('system.changed', {})
    return _json({"message": f"{_CONTROLLABLE_SERVICES[service]}已启动"})

@router.post('/api/system/service/stop')
def system_service_stop(data: dict = Body(...)):
    service = _require_service(data)
    logger.info(f"停止服务: {service}")
    result = run_command(f"{platform_paths.CMD_SYSTEMCTL} stop {service}", timeout=30)
    if not result['success']:
        raise CommandError(f"停止 {service} 失败: {result.get('stderr', '')[:200]}")
    event_bus.publish('system.changed', {})
    return _json({"message": f"{_CONTROLLABLE_SERVICES[service]}已停止"})
