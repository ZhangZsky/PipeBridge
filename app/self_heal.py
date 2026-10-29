"""PipeBridge 自愈加固模块（0.40）

解决两类"系统重刷/重启即失效"的现场问题：

1. 蓝牙固件缺失：RTL8821CU（0bda:c820）需要 rtl_bt/rtl8821c_fw.bin，
   飞牛镜像只带 rtl8821cs_*（另一颗芯片）。系统升级重刷 rootfs 后固件丢失，
   hci0 不再上线，蓝牙音频全链路失效。此处从应用资产目录回填并重载 btusb。
2. Logind 仲裁：headless NAS 无本地 seat，logind 报 seat=offline；
   WirePlumber 0.4.x 的 bluez.lua 在 logind 可用时仅按 seat 激活监控，
   导致蓝牙 audio monitor 永不创建（pw-dump 无 api.bluez5.* 节点）。
   写入 bluetooth.lua.d/60-pipebridge-no-logind.lua 置 with-logind=false。
"""

import os
import shutil
import logging
import subprocess
import time

logger = logging.getLogger('PipeBridge')

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(APP_DIR, 'assets')
FW_SRC_DIR = os.path.join(ASSETS_DIR, 'firmware')
FW_DST_DIR = '/lib/firmware/rtl_bt'
FW_FILES = ('rtl8821c_fw.bin', 'rtl8821c_config.bin')

LUA_SRC = os.path.join(ASSETS_DIR, '60-pipebridge-no-logind.lua')
LUA_DST_DIR = '/etc/wireplumber/bluetooth.lua.d'
LUA_DST = os.path.join(LUA_DST_DIR, '60-pipebridge-no-logind.lua')

HCI_SYSFS = '/sys/class/bluetooth/hci0'

# 每进程只尝试一次 btusb 重载，避免反复弹跳
_reload_attempted = False


def _run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except Exception as e:
        return -1, str(e)


def _wp_is_legacy():
    """WirePlumber < 0.5 视为 legacy（bluez.lua 走 bluetooth.lua.d 配置）。"""
    try:
        from system_manager import get_wireplumber_version
        ver = get_wireplumber_version()
    except Exception:
        return True
    return ver is None or (ver[0] == 0 and ver[1] < 5)


def ensure_logind_bypass():
    """部署/校正 logind 旁路 lua。返回是否发生变更。"""
    if not _wp_is_legacy():
        # 0.5+ 由 wireplumber.conf.d 的 monitor.bluez.seat-monitoring=disabled 覆盖
        if os.path.exists(LUA_DST):
            try:
                os.remove(LUA_DST)
                logger.info('WirePlumber 0.5+ 环境，已移除 0.4 专用 logind 旁路 lua')
            except OSError as e:
                logger.warning(f'移除历史 logind 旁路 lua 失败: {e}')
        return False

    try:
        with open(LUA_SRC, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError as e:
        logger.warning(f'自愈资产缺失，跳过 logind 旁路部署: {LUA_SRC} ({e})')
        return False

    if os.path.exists(LUA_DST):
        try:
            with open(LUA_DST, 'r', encoding='utf-8') as f:
                if f.read() == content:
                    logger.debug('logind 旁路配置已存在且一致')
                    return False
        except OSError:
            pass

    try:
        os.makedirs(LUA_DST_DIR, exist_ok=True)
        with open(LUA_DST, 'w', encoding='utf-8') as f:
            f.write(content)
        logger.info(f'已部署 logind 旁路配置: {LUA_DST}')
        return True
    except OSError as e:
        logger.warning(f'部署 logind 旁路配置失败: {LUA_DST} ({e})')
        return False


def _hci_present():
    return os.path.isdir(HCI_SYSFS)


def ensure_bt_firmware():
    """固件缺失/尺寸不符时回填。返回本次回填的文件名列表。"""
    copied = []
    for name in FW_FILES:
        src = os.path.join(FW_SRC_DIR, name)
        dst = os.path.join(FW_DST_DIR, name)
        if not os.path.exists(src):
            logger.debug(f'资产中无 {name}，跳过')
            continue
        try:
            if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
                continue
            os.makedirs(FW_DST_DIR, exist_ok=True)
            shutil.copyfile(src, dst)
            os.chmod(dst, 0o644)
            copied.append(name)
            logger.info(f'已回填蓝牙固件: {dst}')
        except OSError as e:
            logger.warning(f'回填蓝牙固件失败 {name}: {e}')
    return copied


def ensure_bt_controller():
    """控制器不在线且有固件时，重载 btusb 触发重新 probe + 固件加载。"""
    global _reload_attempted
    if _hci_present():
        return False
    if _reload_attempted:
        return False
    if not os.path.exists(os.path.join(FW_DST_DIR, 'rtl8821c_fw.bin')):
        logger.warning('蓝牙固件仍缺失，无法重载驱动（控制器不会上线）')
        return False

    _reload_attempted = True
    logger.info('未检测到蓝牙控制器，重载 btusb 以重新加载固件...')
    _run(['modprobe', '-r', 'btusb'], timeout=15)
    time.sleep(0.5)
    rc, out = _run(['modprobe', 'btusb'], timeout=15)
    if rc != 0:
        logger.warning(f'modprobe btusb 失败: {out.strip()}')
        return False
    for _ in range(6):
        if _hci_present():
            logger.info('蓝牙控制器已重新上线')
            return True
        time.sleep(0.5)
    logger.warning('重载 btusb 后仍未见 hci0，请检查硬件/固件')
    return False


def run_preflight():
    """启动前自愈：固件 → 控制器 → logind 旁路。异常不外抛。"""
    result = {'firmware': [], 'controller': False, 'logind_bypass': False}
    try:
        result['firmware'] = ensure_bt_firmware()
    except Exception as e:
        logger.warning(f'固件自愈失败: {e}')
    try:
        result['controller'] = ensure_bt_controller()
    except Exception as e:
        logger.warning(f'控制器自愈失败: {e}')
    try:
        result['logind_bypass'] = ensure_logind_bypass()
    except Exception as e:
        logger.warning(f'logind 旁路自愈失败: {e}')
    if result['firmware'] or result['controller'] or result['logind_bypass']:
        logger.info(f'自愈加固已执行: {result}')
    else:
        logger.debug('自愈加固检查通过，无需变更')
    return result
