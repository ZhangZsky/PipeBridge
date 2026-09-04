import os
import sys
import time
import logging
import signal
import atexit
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bluetooth_manager
import audio_manager
import system_manager
import platform_paths
from utils import run_command, _pw_socket_exists

logger = logging.getLogger('PipeBridge')

_keepalive_stop_event = None
_cleanup_done = False

def setup(keepalive_stop_event):
    global _keepalive_stop_event
    _keepalive_stop_event = keepalive_stop_event

def startup_self_heal():
    start_time = time.time()
    logger.info("启动自检和修复...")

    # dbus 缺失降级提示:此时所有蓝牙相关自检会经 D-Bus 门卫抛异常并被下方 try/except 兜底,
    # 应用主体(音频/视频/Web/依赖检测面板)仍正常启动;此处集中给出一条清晰告警便于排障。
    if not getattr(bluetooth_manager, 'HAS_DBUS', True):
        logger.error("检测到 python3-dbus 不可用,蓝牙功能已禁用;应用其余功能不受影响,请安装 python3-dbus 后重启")

    if not system_manager.check_pipewire_running() or not _pw_socket_exists():
        logger.info("音频服务未运行或 socket 缺失，尝试启动 PipeWire...")
        need_pw_setup = True
    else:
        need_pw_setup = False

    try:
        bt = run_command(f"{platform_paths.CMD_SYSTEMCTL} is-active bluetooth 2>/dev/null")
        if 'active' not in bt.get('stdout', ''):
            logger.info("蓝牙服务未运行，尝试启动...")
            run_command(f"{platform_paths.CMD_SYSTEMCTL} start bluetooth 2>/dev/null")
            time.sleep(1)
        else:
            logger.info("蓝牙服务已运行")
    except Exception as e:
        logger.warning(f"检查蓝牙服务失败: {e}")

    try:
        bluetooth_manager.ensure_agent()
    except Exception as e:
        logger.warning(f"持久 Agent 注册失败: {e}")

    # 音频栈启动与蓝牙配置部署置于同一后台线程内顺序执行：先拉起 PipeWire/WirePlumber 再部署蓝牙配置，避免拆成并发线程时两者争抢 PW 栈导致 WirePlumber 刚启动即被重启
    threading.Thread(target=_async_audio_startup, args=(need_pw_setup,), daemon=True).start()

    logger.info(f"启动自检完成，耗时 {time.time() - start_time:.2f}s（后台任务继续）")

def _async_audio_startup(need_pw_setup):
    if need_pw_setup:
        try:
            system_manager.setup_pipewire()
            logger.info("PipeWire 启动成功")
        except Exception as e:
            logger.warning(f"PipeWire 启动失败: {e}")
    _async_startup_tasks()

def _async_startup_tasks():
    try:
        bluetooth_manager.ensure_wireplumber_bluez_config()
    except Exception as e:
        logger.warning(f"WirePlumber 蓝牙配置检查失败: {e}")
    # 已彻底移除"默认设备"设置/恢复功能:默认设备完全交由系统(WirePlumber)与用户手动掌控,
    # PipeBridge 不再保存/恢复默认设备,仅保存并在设备重连时恢复各设备音量(见 event_system)。
    try:
        if not bluetooth_manager._power_on_adapter():
            logger.warning("蓝牙适配器上电失败，可能需要手动检查硬件")
        else:
            logger.info("蓝牙适配器已上电")
    except Exception as e:
        logger.warning(f"蓝牙适配器上电失败: {e}")
    try:
        bluetooth_manager.keep_bluetooth_alive()
    except Exception as e:
        logger.warning(f"蓝牙保活失败: {e}")
    try:
        # 主动初始化自动重连管理器，使其 DBus PropertiesChanged 监听常驻，蓝牙状态变化实时推送 SSE，取代高频轮询
        bluetooth_manager._get_reconnect_manager()
        logger.info("蓝牙状态信号监听已就绪")
    except Exception as e:
        logger.warning(f"初始化蓝牙状态信号监听失败: {e}")
    try:
        # 启动时预拉起 obexd 并注册 OBEX Agent（会话总线），使入站文件推送无需手动"修复"即可被自动授权接受
        import bluetooth_extras
        if bluetooth_extras._ensure_obex_service():
            logger.info("OBEX 接收授权服务已就绪")
        else:
            logger.warning("OBEX接收授权服务未就绪，入站文件推送可能被拒绝")
    except Exception as e:
        logger.warning(f"初始化 OBEX 接收授权服务失败: {e}")
    try:
        # BlueZ 服务就绪、适配器已上电后启动 AVRCP 媒体键桥接：
        # 注册 Target MediaPlayer1 + 监听对端 MediaPlayer1/Transport 信号，产出 mediakey SSE 事件
        import avrcp_bridge
        avrcp_bridge.start()
    except Exception as e:
        logger.warning(f"启动 AVRCP 媒体键桥接失败(降级，不影响其它功能): {e}")
    _start_bluetooth_keepalive_timer()

def _guard_pw_services():
    # 进程守护:PipeWire/WirePlumber 崩溃后主动拉起。start_pw_service 幂等——
    # 进程已存在则直接返回,仅缺失时才启动,不会重复拉起或干扰正常运行。
    try:
        from utils import start_pw_service
        for svc in ('pipewire', 'wireplumber'):
            pg = run_command(f"pgrep -x {svc} 2>/dev/null")
            if not pg['stdout'].strip():
                logger.warning(f"{svc} 进程缺失,守护拉起...")
                start_pw_service(svc)
    except Exception as e:
        logger.debug(f"PipeWire 服务守护检查失败: {e}")

def _start_bluetooth_keepalive_timer():
    def _keepalive_loop():
        while not _keepalive_stop_event.is_set():
            _keepalive_stop_event.wait(timeout=60)
            if _keepalive_stop_event.is_set():
                break
            _guard_pw_services()
            try:
                bluetooth_manager.keep_bluetooth_alive()
            except Exception as e:
                logger.debug(f"蓝牙周期保活失败: {e}")
    t = threading.Thread(target=_keepalive_loop, daemon=True)
    t.start()
    logger.debug("蓝牙周期保活已启动 (间隔 60s)")

def _cleanup():
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True
    logger.info("正在清理资源...")
    if _keepalive_stop_event is not None:
        _keepalive_stop_event.set()
    try:
        from event_system import event_detector
        event_detector.stop()
    except Exception as e:
        logger.debug(f"停止事件检测器失败: {e}")
    try:
        from pw_mon_listener import pw_mon_listener
        pw_mon_listener.stop()
    except Exception as e:
        logger.debug(f"停止 pw-mon 监听器失败: {e}")
    try:
        rm = bluetooth_manager._get_reconnect_manager()
        if rm is not None:
            rm.stop()
    except Exception as e:
        logger.debug(f"停止自动重连管理器失败: {e}")
    try:
        import avrcp_bridge
        avrcp_bridge.stop()
    except Exception as e:
        logger.debug(f"停止 AVRCP 媒体键桥接失败: {e}")
    try:
        bluetooth_manager.release_agent()
    except Exception as e:
        logger.debug(f"释放蓝牙 Agent 失败: {e}")
    try:
        import bluetooth_extras
        if bluetooth_extras.is_obex_server_running():
            bluetooth_extras.stop_obex_server()
    except Exception as e:
        logger.debug(f"停止 OBEX 接收服务失败: {e}")
    try:
        system_manager._overview_executor.shutdown(wait=False)
    except Exception as e:
        logger.debug(f"关闭概览线程池失败: {e}")
    logger.info("资源清理完成")

def _signal_handler(signum, frame):
    logger.info(f"收到信号 {signum}，正在关闭...")
    _cleanup()
    sys.exit(0)

def register_signal_handlers():
    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
