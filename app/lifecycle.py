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

    # 音频栈启动与蓝牙配置部署放在同一后台线程内【顺序执行】：
    # 先把 PipeWire/WirePlumber 拉起来，再部署蓝牙配置。
    # 若拆成两个并发线程，deploy_bluez_config 会在 PW 尚未就绪时就判定"重启 WirePlumber"，
    # 与 setup_pipewire 抢着操作 PW 栈，导致 WirePlumber 刚启动就被重启一次（见启动日志竞争）。
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
    try:
        audio_manager.restore_default_device()
    except Exception as e:
        logger.warning(f"恢复默认设备失败: {e}")
    try:
        audio_manager.auto_set_defaults()
    except Exception as e:
        logger.warning(f"自动设置默认设备失败: {e}")
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
        # 主动初始化自动重连管理器，使其 DBus PropertiesChanged 信号监听常驻，
        # 蓝牙状态变化即可实时推送 SSE，取代后端高频轮询
        bluetooth_manager._get_reconnect_manager()
        logger.info("蓝牙状态信号监听已就绪")
    except Exception as e:
        logger.warning(f"初始化蓝牙状态信号监听失败: {e}")
    _start_bluetooth_keepalive_timer()

def _start_bluetooth_keepalive_timer():
    def _keepalive_loop():
        while not _keepalive_stop_event.is_set():
            _keepalive_stop_event.wait(timeout=60)
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
