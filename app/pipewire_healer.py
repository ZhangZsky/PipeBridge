import re
import time
import logging

from utils import (run_command, pw_dump, is_real_sink,
                   start_pw_service, stop_pw_service)
import dependency_checker
import platform_paths

logger = logging.getLogger('MediaHub')


def _diagnose_no_sinks(pw_data):
    # 当没有 Audio/Sink 时进行深度诊断，返回诊断信息字典
    diag = {'has_device': False, 'has_alsa_card': False, 'wp_errors': []}

    # 检查 pw-dump 中是否有 Device 对象
    devices = [obj for obj in pw_data if isinstance(obj, dict)
               and obj.get('type') == 'PipeWire:Interface:Device']
    diag['has_device'] = len(devices) > 0
    if devices:
        for dev in devices:
            props = dev.get('info', {}).get('props', {})
            logger.debug(f"  Device: id={dev.get('id')}, name={props.get('device.name', '?')}, "
                         f"nick={props.get('device.nick', '?')}")
    else:
        logger.info("pw-dump 中无 PipeWire:Interface:Device 对象，WirePlumber 未发现任何 ALSA 声卡")

    # 检查 ALSA 声卡是否存在
    aplay_result = run_command(f"{platform_paths.CMD_APLAY} -l 2>/dev/null", timeout=3)
    if aplay_result['success'] and aplay_result['stdout'].strip():
        diag['has_alsa_card'] = True
        if 'hdmi' in aplay_result['stdout'].lower():
            diag['has_hdmi'] = True

    # root 下使用系统级 journalctl
    wp_log_cmd = "journalctl -u wireplumber --no-pager -n 50 2>/dev/null | "
    wp_log = run_command(
        wp_log_cmd +
        "grep -i 'error\\|fail\\|alsa\\|spa\\|device\\|profile' | tail -15",
        timeout=5)
    if wp_log['success'] and wp_log['stdout'].strip():
        diag['wp_errors'] = wp_log['stdout'].strip().split('\n')
        logger.info(f"WirePlumber 相关日志:\n{wp_log['stdout'].strip()}")

    return diag


# 检查 PipeWire 是否运行，未运行则尝试启动，返回是否成功
def _check_pw_running():
    if dependency_checker.check_pipewire_running():
        return True
    logger.info("PipeWire 未运行，尝试启动...")
    start_pw_service('pipewire')
    time.sleep(1)
    start_pw_service('pipewire-pulse')
    time.sleep(0.5)
    if dependency_checker.check_pipewire_running():
        return True
    logger.error("PipeWire 启动失败")
    return False


# 检查 WirePlumber 是否运行，未运行则尝试启动，返回是否成功
def _check_wireplumber_running():
    if dependency_checker.check_wireplumber_running():
        return True
    logger.info("WirePlumber 未运行，尝试启动...")
    wp_ok = start_pw_service('wireplumber')
    time.sleep(2)
    if not wp_ok and not dependency_checker.check_wireplumber_running():
        logger.info("WirePlumber 首次启动失败，重试...")
        start_pw_service('wireplumber')
        time.sleep(2)
    if not dependency_checker.check_wireplumber_running():
        logger.warning("WirePlumber 启动失败，蓝牙音频可能不可用")
        return False
    return True


# 检查是否有音频 sink，无则尝试修复，返回 (pw_data, real_sinks)
def _ensure_sinks_exist(pw_data):
    real_sinks = [obj for obj in pw_data if is_real_sink(obj)]

    if real_sinks:
        return pw_data, real_sinks

    # 检查是否有蓝牙 Sink
    bt_sink_names = set()
    for obj in pw_data:
        if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
            continue
        props = obj.get('info', {}).get('props', {})
        if props.get('media.class', '') in ('Audio/Sink', 'Audio/Sink/Virtual'):
            node_name = props.get('node.name', '').lower()
            if 'bluez' in node_name:
                bt_sink_names.add(props.get('node.name', ''))

    if bt_sink_names:
        logger.info(f"无通用 Sink 但有蓝牙 Sink ({bt_sink_names})，跳过破坏性修复")
        return pw_data, real_sinks

    # 有蓝牙设备已连接但 pw-dump 暂无 Sink，可能是刷新间隙
    # 延迟导入避免循环依赖
    from audio_manager import _has_connected_bluetooth
    if _has_connected_bluetooth():
        logger.info("有蓝牙设备已连接但 pw-dump 暂无 Sink，可能是刷新间隙，跳过破坏性修复")
        return pw_data, real_sinks

    # 4a: 卸载蜂鸣器内核模块
    from audio_manager import _ensure_pcspkr_module
    _ensure_pcspkr_module()

    # 4b: 部署/更新 IEC958 规则
    from audio_manager import _deploy_wp_iec958_rule
    _deploy_wp_iec958_rule()

    # 4c: 重载 WirePlumber 以发现新设备
    logger.info("重载 WirePlumber 以发现新设备...")
    run_command(f"{platform_paths.CMD_WPCTL} reload 2>/dev/null", timeout=5)
    time.sleep(3)

    # 4d: 重载后激活 profile 为 Off 的设备
    if _activate_inactive_profiles():
        time.sleep(2)

    pw_data = pw_dump()
    real_sinks = [obj for obj in pw_data if is_real_sink(obj)]

    # 4e: 重载+激活无效，尝试重启 WirePlumber
    if not real_sinks:
        logger.info("WirePlumber 重载无效，尝试重启 WirePlumber...")
        stop_pw_service('wireplumber')
        time.sleep(1)
        start_pw_service('wireplumber')
        time.sleep(3)
        _activate_inactive_profiles()
        time.sleep(1)

        pw_data = pw_dump()
        real_sinks = [obj for obj in pw_data if is_real_sink(obj)]

    # 4f: 诊断并尝试清除缓存
    pw_data, real_sinks = _diagnose_and_fix_no_sink(pw_data, real_sinks)

    return pw_data, real_sinks


# 诊断无 sink 原因并尝试修复，返回 (pw_data, real_sinks)
def _diagnose_and_fix_no_sink(pw_data, real_sinks):
    if real_sinks:
        return pw_data, real_sinks

    diag = _diagnose_no_sinks(pw_data)

    if not diag['has_alsa_card']:
        return pw_data, real_sinks

    # 有 ALSA 声卡但 WirePlumber 仍无 Device，清除状态缓存重试
    devices = [obj for obj in pw_data if isinstance(obj, dict)
               and obj.get('type') == 'PipeWire:Interface:Device']
    if devices:
        return pw_data, real_sinks

    logger.info("WirePlumber 仍无 Device，尝试清除状态缓存并重启...")
    from utils import _get_pw_env
    pw_env = _get_pw_env()
    xdg = pw_env.get('XDG_RUNTIME_DIR', '')
    # root 下直接清除 /root 下的缓存
    run_command(f"rm -rf /root/{platform_paths.WP_STATE_DIR} 2>/dev/null", timeout=3)
    if xdg:
        uid = xdg.replace('/run/user/', '')
        if uid.isdigit():
            home = run_command(f"getent passwd {uid} 2>/dev/null | cut -d: -f6", timeout=3)
            if home['success'] and home['stdout']:
                home_dir = home['stdout'].strip()
                run_command(f"rm -rf {home_dir}/{platform_paths.WP_STATE_DIR} 2>/dev/null", timeout=3)
    stop_pw_service('wireplumber')
    time.sleep(1)
    start_pw_service('wireplumber')
    time.sleep(4)

    _activate_inactive_profiles()
    time.sleep(1)

    pw_data = pw_dump()
    real_sinks = [obj for obj in pw_data if is_real_sink(obj)]

    # 最终修复尝试
    if not real_sinks:
        logger.info("ALSA 有声卡但 PipeWire 仍无 Sink，最终修复尝试...")
        from audio_manager import _ensure_pcspkr_module
        _ensure_pcspkr_module()
        run_command(f"{platform_paths.CMD_WPCTL} reload 2>/dev/null", timeout=5)
        time.sleep(3)
        _activate_inactive_profiles()
        time.sleep(1)
        pw_data = pw_dump()
        real_sinks = [obj for obj in pw_data if is_real_sink(obj)]

    return pw_data, real_sinks


def _ensure_audio_pipewire():
    # 检查 PipeWire 是否运行且具有音频能力，无 sink 时尝试修复 WirePlumber

    # 步骤1: 确保 PipeWire 进程运行
    if not _check_pw_running():
        return False

    # 步骤2: 确保 WirePlumber 运行
    _check_wireplumber_running()

    # 步骤3: 检查 pw-dump 是否有数据
    pw_data = pw_dump()
    if not pw_data:
        logger.info("PipeWire 运行中但 pw-dump 无数据")
        return False

    # 步骤4: 检查是否有真实 Audio/Sink，无则修复
    pw_data, real_sinks = _ensure_sinks_exist(pw_data)

    if real_sinks:
        logger.info(f"PipeWire 就绪，发现 {len(real_sinks)} 个 Audio/Sink")
        return True

    # 步骤5: 仍无 sink，但 pw-dump 有数据 → PW 运行但设备未注册
    audio_nodes = [obj for obj in pw_data if isinstance(obj, dict)
                   and obj.get('type') == 'PipeWire:Interface:Node'
                   and obj.get('info', {}).get('props', {}).get('media.class', '').startswith('Audio/')]
    if audio_nodes:
        logger.info(f"PipeWire 有 {len(audio_nodes)} 个音频节点但无 Sink，尝试通过 wpctl 补充")
        return True

    logger.info("PipeWire 运行中但无音频节点")
    return False


def _activate_inactive_profiles():
    # 检查 WirePlumber 中 profile 为 Off 的设备，尝试激活合适的 profile
    activated = False

    status_result = run_command(f"{platform_paths.CMD_WPCTL} status 2>/dev/null", timeout=5)
    if not status_result['success'] or not status_result['stdout']:
        return activated

    # 延迟导入避免循环依赖
    from audio_manager import _try_activate_profile

    in_devices = False
    current_device_id = None
    current_device_name = ''
    current_profile = ''

    for line in status_result['stdout'].splitlines():
        stripped = line.strip()

        if 'Devices' in stripped and ('├─' in stripped or '└─' in stripped):
            in_devices = True
            continue

        if in_devices:
            dev_match = re.match(r'(\d+)\.\s+(.+?)\s+\[(.+?)\]', stripped)
            if dev_match:
                if current_device_id and current_profile.lower() == 'off':
                    activated |= _try_activate_profile(current_device_id, current_device_name)

                current_device_id = int(dev_match.group(1))
                current_device_name = dev_match.group(3)
                current_profile = ''
                continue

            profile_match = re.search(r'Profile:\s*(.+)', stripped)
            if profile_match:
                current_profile = profile_match.group(1).strip().lstrip('*').strip()
                continue

            if stripped.startswith('Clients') or (not stripped and current_device_id is not None):
                in_devices = False

    if current_device_id and current_profile.lower() == 'off':
        activated |= _try_activate_profile(current_device_id, current_device_name)

    return activated
