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
import dependency_checker
from utils import run_command

logger = logging.getLogger('MediaHub')

# 模块级引用，由 setup() 初始化
_keepalive_stop_event = None


def setup(keepalive_stop_event):
    """初始化 lifecycle 模块，传入共享的 keepalive_stop_event"""
    global _keepalive_stop_event
    _keepalive_stop_event = keepalive_stop_event


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


# 后台启动 PipeWire
def _async_pipewire_setup():
    try:
        dependency_checker.setup_pipewire()
        logger.info("PipeWire 启动成功")
    except Exception as e:
        logger.warning(f"PipeWire 启动失败: {e}")


# 后台启动初始化任务
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


# 启动蓝牙周期保活
def _start_bluetooth_keepalive_timer():
    # 保活循环(60s间隔)
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


# 退出时清理资源
def _cleanup():
    logger.info("正在清理资源...")
    _keepalive_stop_event.set()
    try:
        rm = bluetooth_manager._auto_reconnect_manager
        if rm is not None:
            rm.stop()
    except Exception as e:
        logger.debug(f"停止自动重连管理器失败: {e}")
    try:
        bluetooth_manager.release_agent()
    except Exception as e:
        logger.debug(f"释放蓝牙 Agent 失败: {e}")
    logger.info("资源清理完成")


# 信号处理触发清理退出
def _signal_handler(signum, frame):
    logger.info(f"收到信号 {signum}，正在关闭...")
    _cleanup()
    sys.exit(0)


def register_signal_handlers():
    """注册退出信号处理和 atexit 钩子"""
    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
