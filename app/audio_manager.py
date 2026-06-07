import re
import os
import time
import logging
import threading
import shlex
import math
from utils import run_command, pw_dump, find_pw_node, get_node_id_by_name, get_node_name_by_id, get_default_sink_name, get_default_source_name, _parse_wpctl_default, extract_pw_vol_params, extract_pw_enumformat, extract_pw_routes, is_real_sink, find_audio_sinks, find_audio_sources, start_pw_service, stop_pw_service, get_prop_with_fallback, find_device_props, parse_edid_monitor_name, parse_edid_physical_size
import config
import dependency_checker
import platform_paths
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError
from volume_controller import volume_controller
from wp_config_manager import WPConfigManager

_SAFE_DEVICE_PATTERN = re.compile(r'^[a-zA-Z0-9_.@:\[\]\/-]+$')


# 统一音频设备类型分类，替代各处重复的判断逻辑
# 优先级: device.bus > 名称关键词 > HDMI 检测 > 默认
def _classify_audio_type(name, friendly_name='', props=None, device_props=None,
                         hdmi_monitor_names=None, role='sink'):
    if props is None:
        props = {}
    if device_props is None:
        device_props = {}
    name_lower = name.lower()
    friendly_upper = (friendly_name or '').upper()

    # 1. 通过 device.bus 属性检测（最可靠）
    bus = get_prop_with_fallback(props, device_props, 'device.bus', '').lower()
    device_api = get_prop_with_fallback(props, device_props, 'device.api', '').lower()

    if bus == 'usb' or device_api == 'usb':
        return 'usb'
    if 'bluez' in device_api or 'bluez' in name_lower or 'bt' in name_lower:
        return 'bluetooth'

    # 2. 名称关键词
    if 'pcspkr' in name_lower or 'pcsp' in name_lower:
        return 'beeper'

    # 3. HDMI / DisplayPort 检测
    if 'hdmi' in name_lower or 'hdmi' in friendly_upper or 'display audio' in name_lower:
        return 'hdmi'
    card_name = get_prop_with_fallback(props, device_props, 'alsa.card_name', '').lower()
    if 'hdmi' in card_name:
        return 'hdmi'
    # DisplayPort 音频
    if 'dp' in name_lower and 'displayport' in (card_name or name_lower):
        return 'displayport'
    if hdmi_monitor_names:
        for mn in hdmi_monitor_names:
            if mn and mn in friendly_upper:
                return 'hdmi'

    # 4. Source 角色的特殊分类
    if role == 'source':
        if 'mic' in name_lower or 'microphone' in name_lower:
            return 'microphone'
        if 'line' in name_lower:
            return 'linein'
        return 'internal'

    return 'internal'


def _extract_node_audio_info(obj, pw_data):
    """从 PipeWire 节点对象提取统一的音频信息"""
    info = obj.get('info', {})
    params = info.get('params', {})
    props = info.get('props', {})

    if not isinstance(params, dict):
        params = {}

    # Volume
    vol_flat = 0.0
    vol_percent = 0
    vol_db = 0.0
    muted = False
    channels = []
    balance = 0.0

    props_params = extract_pw_vol_params(params)
    enum_format = extract_pw_enumformat(params)

    channel_positions = []
    if enum_format:
        first = enum_format[0] if isinstance(enum_format[0], dict) else {}
        pos = first.get('position', [])
        if isinstance(pos, list):
            channel_positions = [str(p).upper() for p in pos]

    node_name = props.get('node.name', '')

    if props_params:
        channel_volumes = props_params.get('channelVolumes', [])
        if channel_volumes and isinstance(channel_volumes, list):
            for i, cv in enumerate(channel_volumes):
                if isinstance(cv, (int, float)):
                    pos_name = channel_positions[i] if i < len(channel_positions) else f'CH{i}'
                    ch_label = _CHANNEL_POS_MAP.get(pos_name, pos_name)
                    linear_cv = volume_controller._cubic_to_linear(float(cv))
                    channels.append({'channel': ch_label, 'position': pos_name,
                                     'volume': min(round(linear_cv * 100), 100),
                                     'effective_volume': min(round(linear_cv * 100), 100)})
        valid_ch_vols = [volume_controller._cubic_to_linear(float(cv))
                         for cv in channel_volumes if isinstance(cv, (int, float))]
        if valid_ch_vols:
            vol_flat = sum(valid_ch_vols) / len(valid_ch_vols)
            vol_percent = min(round(vol_flat * 100), 100)
            if vol_flat > 0:
                vol_db = round(20 * math.log10(vol_flat), 2)
            muted = bool(props_params.get('mute', False))
        else:
            # 回退到 wpctl 获取音量
            wpctl_id = _get_wpctl_id_for_node(node_name, 'Sinks')
            if wpctl_id is not None:
                vol_info = _get_wpctl_volume(wpctl_id)
                vol_percent = vol_info['volume']
                muted = vol_info['muted']
                vol_flat = vol_percent / 100.0
                if vol_flat > 0:
                    vol_db = round(20 * math.log10(vol_flat), 2)
            else:
                raw_vol = props_params.get('volume', 0.0)
                vol_flat = float(raw_vol) if isinstance(raw_vol, (int, float)) and raw_vol >= 0 else 0.0
                vol_percent = min(round(vol_flat * 100), 100)
                if vol_flat > 0:
                    vol_db = round(20 * math.log10(vol_flat), 2)
                muted = bool(props_params.get('mute', False))
    else:
        wpctl_id = _get_wpctl_id_for_node(node_name, 'Sinks')
        if wpctl_id is not None:
            vol_info = _get_wpctl_volume(wpctl_id)
            vol_percent = vol_info['volume']
            muted = vol_info['muted']
            vol_flat = vol_percent / 100.0
            if vol_flat > 0:
                vol_db = round(20 * math.log10(vol_flat), 2)

    # Balance
    if len(channels) >= 2 and (channels[0]['volume'] + channels[1]['volume']) > 0:
        balance = round((channels[1]['volume'] - channels[0]['volume'])
                        / (channels[0]['volume'] + channels[1]['volume']), 3)

    # Sample rate / format / channel count
    sample_rate = 0
    sample_format = ''
    channel_count = 0

    if enum_format:
        first = enum_format[0] if isinstance(enum_format[0], dict) else {}
        rate = first.get('rate', 0)
        if isinstance(rate, dict):
            rate = rate.get('default', 0)
        if isinstance(rate, (int, float)) and rate in _STANDARD_SAMPLE_RATES:
            sample_rate = int(rate)
        elif isinstance(rate, (int, float)) and rate > 0:
            sample_rate = int(rate)
        fmt = first.get('format', '')
        if isinstance(fmt, list) and fmt:
            fmt = fmt[0].get('name', '') if isinstance(fmt[0], dict) else str(fmt[0])
        if isinstance(fmt, str) and fmt:
            sample_format = fmt
        ch = first.get('channels', 0)
        if isinstance(ch, (int, float)) and 1 <= ch <= 32:
            channel_count = int(ch)
    else:
        logger.debug(f"PW node '{node_name}': EnumFormat 为空")

    if not sample_rate:
        audio_rate = props.get('audio.rate', None)
        if audio_rate is not None:
            try:
                val = int(audio_rate)
                if val in _STANDARD_SAMPLE_RATES:
                    sample_rate = val
            except (ValueError, TypeError):
                pass
    if not sample_format:
        sample_format = str(props.get('audio.format', '')) or sample_format
    if not channel_count:
        try:
            channel_count = int(props.get('audio.channels', 0))
        except (ValueError, TypeError):
            pass
    if not channel_count and channels:
        channel_count = len(channels)

    # Routes / ports
    ports, active_port = extract_pw_routes(params)

    # Node 无端口时从关联的 Device 对象补充
    if not ports and not active_port:
        device_id_prop = props.get('device.id')
        if device_id_prop is not None:
            for dev_obj in pw_data:
                if dev_obj.get('type') == 'PipeWire:Interface:Device' and dev_obj.get('id') == device_id_prop:
                    dev_params = dev_obj.get('info', {}).get('params', {})
                    if isinstance(dev_params, dict):
                        ports, active_port = extract_pw_routes(dev_params)
                    break

    # Profiles from associated Device object
    profiles = []
    active_profile = ''
    device_id_prop = props.get('device.id')
    if device_id_prop is not None:
        for dev_obj in pw_data:
            if not isinstance(dev_obj, dict) or dev_obj.get('type') != 'PipeWire:Interface:Device':
                continue
            if dev_obj.get('id') == device_id_prop:
                dev_params = dev_obj.get('info', {}).get('params', {})
                if not isinstance(dev_params, dict):
                    dev_params = {}
                enum_profiles = dev_params.get('EnumProfile', [])
                if isinstance(enum_profiles, dict):
                    enum_profiles = [enum_profiles]
                for ep in enum_profiles:
                    if not isinstance(ep, dict):
                        continue
                    profiles.append({
                        'name': ep.get('name', ''),
                        'description': ep.get('description', ''),
                        'priority': ep.get('priority', 0),
                        'available': ep.get('available', True),
                    })
                active_profiles = dev_params.get('Profile', [])
                if isinstance(active_profiles, dict):
                    active_profiles = [active_profiles]
                for ap in active_profiles:
                    if isinstance(ap, dict):
                        active_profile = ap.get('name', '')
                        break
                break

    # Extended props
    device_props = {}
    if device_id_prop is not None:
        device_props = find_device_props(pw_data, device_id_prop)
    extended_props = _build_extended_props(props, device_props)

    return {
        'volume': vol_percent,
        'volume_flat': round(vol_flat, 4),
        'volume_db': vol_db,
        'muted': muted,
        'channels': channels,
        'channel_count': channel_count,
        'balance': balance,
        'sample_rate': sample_rate,
        'sample_format': sample_format,
        'ports': ports,
        'active_port': active_port,
        'profiles': profiles,
        'active_profile': active_profile,
        'extended': extended_props,
    }


def _build_extended_props(props, device_props):
    """构建设备扩展属性字典"""
    return {
        'alsa.card_name': get_prop_with_fallback(props, device_props, 'alsa.card_name'),
        'alsa.card_id': get_prop_with_fallback(props, device_props, 'alsa.card_id'),
        'device.api': get_prop_with_fallback(props, device_props, 'device.api'),
        'device.bus': get_prop_with_fallback(props, device_props, 'device.bus'),
        'device.bus_path': get_prop_with_fallback(props, device_props, 'device.bus-path'),
        'device.nick': get_prop_with_fallback(props, device_props, 'device.nick'),
        'device.icon_name': get_prop_with_fallback(props, device_props, 'device.icon-name'),
        'device.product.id': get_prop_with_fallback(props, device_props, 'device.product.id'),
        'device.vendor.id': get_prop_with_fallback(props, device_props, 'device.vendor.id'),
        'alsa.driver': get_prop_with_fallback(props, device_props, 'alsa.driver'),
        'alsa.pcm.card': get_prop_with_fallback(props, device_props, 'alsa.pcm.card'),
        'alsa.pcm.device': get_prop_with_fallback(props, device_props, 'alsa.pcm.device'),
        'alsa.card': get_prop_with_fallback(props, device_props, 'alsa.card'),
        'device.form_factor': get_prop_with_fallback(props, device_props, 'device.form-factor'),
        'device.string': get_prop_with_fallback(props, device_props, 'device.string'),
        'device.description': get_prop_with_fallback(props, device_props, 'device.description'),
        'media.name': get_prop_with_fallback(props, device_props, 'media.name'),
        'node.driver': get_prop_with_fallback(props, device_props, 'node.driver'),
    }


_STANDARD_SAMPLE_RATES = {  # 音频标准采样率集合
    8000, 11025, 12000, 16000, 22050, 24000, 32000,
    44100, 48000, 64000, 88200, 96000,
    176400, 192000, 352800, 384000,
}

_CHANNEL_POS_MAP = {
    'FL': 'FL', 'FR': 'FR', 'FC': 'FC', 'LFE': 'LFE',
    'BL': 'BL', 'BR': 'BR', 'FLC': 'FLC', 'FRC': 'FRC',
    'BC': 'BC', 'SL': 'SL', 'SR': 'SR', 'TC': 'TC',
    'TFL': 'TFL', 'TFC': 'TFC', 'TFR': 'TFR',
    'TBL': 'TBL', 'TBC': 'TBC', 'TBR': 'TBR',
    'MONO': 'Mono',
}

logger = logging.getLogger('MediaHub')

_wpc = WPConfigManager()

_pw_ok_cache = {}
_pw_ok_cache_lock = threading.Lock()
_scan_lock = threading.Lock()


def _has_connected_bluetooth():
    try:
        import bluetooth_manager as _bt_mod
        return bool(_bt_mod.check_bluetooth_connections())
    except ImportError:
        return False


def _get_wpctl_volume(node_id):
    result = run_command(f"{platform_paths.CMD_WPCTL} get-volume {node_id} 2>/dev/null", timeout=3)
    vol_percent = 0
    muted = False
    if result['success'] and result['stdout']:
        vol_str = result['stdout'].strip()
        m = re.search(r'Volume:\s*([\d.]+)', vol_str)
        if m:
            vol_flat = float(m.group(1))
            vol_percent = min(round(vol_flat * 100), 100)
        if 'MUTED' in vol_str.upper():
            muted = True
    return {'volume': vol_percent, 'muted': muted}


_hdmi_cache = {}
_HDMI_CACHE_TTL = 5


def _get_connected_hdmi_info():
    import time as _time
    now = _time.time()
    cache_key = '_hdmi_info'
    if cache_key in _hdmi_cache:
        cached_data, cached_time = _hdmi_cache[cache_key]
        if now - cached_time < _HDMI_CACHE_TTL:
            return cached_data

    connected_hdmi = []
    drm_path = platform_paths.SYS_DRM
    if not os.path.exists(drm_path):
        _hdmi_cache[cache_key] = (connected_hdmi, now)
        return connected_hdmi
    for entry in os.listdir(drm_path):
        status_path = os.path.join(drm_path, entry, 'status')
        if os.path.exists(status_path):
            try:
                with open(status_path, 'r') as f:
                    status = f.read().strip()
                if status == 'connected' and 'HDMI' in entry.upper():
                    edid_path = os.path.join(drm_path, entry, 'edid')
                    monitor_name = None
                    if os.path.exists(edid_path):
                        try:
                            with open(edid_path, 'rb') as f:
                                edid_data = f.read()
                            if len(edid_data) >= 128:
                                monitor_name = parse_edid_monitor_name(edid_data)
                        except Exception:
                            logger.debug(f"读取 EDID 数据失败: {edid_path}")
                    connected_hdmi.append({
                        'drm_entry': entry,
                        'monitor_name': monitor_name or 'HDMI 显示器'
                    })
            except Exception:
                logger.debug(f"读取 DRM 状态失败: {entry}")
    _hdmi_cache[cache_key] = (connected_hdmi, now)
    return connected_hdmi


def _get_wpctl_id_for_node(name, section):
    """从 wpctl status 中查找指定节点的 wpctl ID

    Args:
        name: 节点名
        section: 'Sinks' 或 'Sources'
    """
    result = run_command(f"{platform_paths.CMD_WPCTL} status 2>/dev/null", timeout=5)
    if not result['success'] or not result['stdout']:
        return None
    in_section = False
    for line in result['stdout'].splitlines():
        stripped = line.strip()
        if stripped.startswith(section) or stripped.startswith('├─ ' + section) or stripped.startswith('└─ ' + section):
            in_section = True
            continue
        if in_section:
            if stripped.startswith('├─') or stripped.startswith('└─') or stripped.startswith('│'):
                if stripped.endswith(']') or stripped.endswith('*'):
                    parts = stripped.split()
                    for p in parts:
                        p_clean = p.strip('*')
                        if p_clean.isdigit():
                            node_id = int(p_clean)
                            resolved = get_node_name_by_id(node_id)
                            if resolved == name:
                                return node_id
            if not stripped or stripped.startswith('Clients') or stripped.startswith('├─ Clients') or stripped.startswith('└─ Clients'):
                break
    return None


def _get_wpctl_id_for_sink(name):
    """从 wpctl status 的 Sinks 区域查找指定节点的 wpctl ID"""
    return _get_wpctl_id_for_node(name, 'Sinks')


def _get_wpctl_device_id(device_name):
    pw_data = pw_dump()
    node = find_pw_node(pw_data, name=device_name)
    if node is None:
        node = find_pw_node(pw_data, property_filters={'node.description': device_name})
    return node.get('id') if node else None


def _find_wp_config_dirs():
    return _wpc.find_config_dirs()



def _deploy_wp_iec958_rule():
    _wpc.deploy_iec958_rule()
    _wpc.deploy_pcspkr_blacklist()
    _check_alsa_spa_plugin()


def _check_alsa_spa_plugin():
    # 检查 ALSA SPA 插件是否可用，WirePlumber 依赖它发现声卡
    spa_check = run_command("find /usr/lib /usr/lib64 -name 'libspa-alsa.so' 2>/dev/null", timeout=5)
    if spa_check['success'] and spa_check['stdout'].strip():
        logger.info(f"ALSA SPA 插件: {spa_check['stdout'].strip()}")
    else:
        logger.warning("未找到 ALSA SPA 插件 (libspa-alsa.so)，WirePlumber 无法发现 ALSA 声卡")

    # 检查 ALSA 设备是否可见
    aplay_check = run_command(f"{platform_paths.CMD_APLAY} -l 2>/dev/null", timeout=3)
    if aplay_check['success'] and aplay_check['stdout'].strip():
        logger.info(f"ALSA 设备列表:\n{aplay_check['stdout'].strip()}")
    else:
        logger.info("aplay -l 无输出，可能未安装 alsa-utils 或无 ALSA 设备")

    # 检查 /proc/asound/cards
    cards_check = run_command(f"cat {platform_paths.PROC_ASOUND_CARDS} 2>/dev/null", timeout=3)
    if cards_check['success'] and cards_check['stdout'].strip():
        logger.info(f"ALSA 声卡:\n{cards_check['stdout'].strip()}")
    else:
        logger.info("/proc/asound/cards 无内容")

    # 检查 WirePlumber 日志中的 ALSA 相关错误
    # root 下使用系统级 journalctl
    wp_log = run_command("journalctl -u wireplumber --no-pager -n 30 2>/dev/null | grep -i 'alsa\\|spa\\|device\\|error\\|fail' | tail -10", timeout=5)
    if wp_log['success'] and wp_log['stdout'].strip():
        logger.info(f"WirePlumber ALSA 相关日志:\n{wp_log['stdout'].strip()}")


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


def _check_pw_running():
    """检查 PipeWire 是否运行，未运行则尝试启动，返回是否成功"""
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


def _check_wireplumber_running():
    """检查 WirePlumber 是否运行，未运行则尝试启动"""
    if dependency_checker.check_wireplumber_running():
        return
    logger.info("WirePlumber 未运行，尝试启动...")
    wp_ok = start_pw_service('wireplumber')
    time.sleep(2)
    if not wp_ok and not dependency_checker.check_wireplumber_running():
        logger.info("WirePlumber 首次启动失败，重试...")
        start_pw_service('wireplumber')
        time.sleep(2)
    if not dependency_checker.check_wireplumber_running():
        logger.warning("WirePlumber 启动失败，蓝牙音频可能不可用")


def _ensure_sinks_exist(pw_data):
    """检查是否有音频 sink，无则尝试修复，返回 (pw_data, real_sinks)"""
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
    if _has_connected_bluetooth():
        logger.info("有蓝牙设备已连接但 pw-dump 暂无 Sink，可能是刷新间隙，跳过破坏性修复")
        return pw_data, real_sinks

    # 4a: 卸载蜂鸣器内核模块
    _ensure_pcspkr_module()

    # 4b: 部署/更新 IEC958 规则
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


def _diagnose_and_fix_no_sink(pw_data, real_sinks):
    """诊断无 sink 原因并尝试修复，返回 (pw_data, real_sinks)"""
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
    now = time.time()
    with _pw_ok_cache_lock:
        if '_ts' in _pw_ok_cache and (now - _pw_ok_cache.get('_ts', 0)) < 60:
            return _pw_ok_cache.get('ok', False)

    # 步骤1: 确保 PipeWire 进程运行
    if not _check_pw_running():
        with _pw_ok_cache_lock:
            _pw_ok_cache.update({'_ts': now, 'ok': False})
        return False

    # 步骤2: 确保 WirePlumber 运行
    _check_wireplumber_running()

    # 步骤3: 检查 pw-dump 是否有数据
    pw_data = pw_dump()
    if not pw_data:
        logger.info("PipeWire 运行中但 pw-dump 无数据")
        with _pw_ok_cache_lock:
            _pw_ok_cache.update({'_ts': now, 'ok': False})
        return False

    # 步骤4: 检查是否有真实 Audio/Sink，无则修复
    pw_data, real_sinks = _ensure_sinks_exist(pw_data)

    if real_sinks:
        logger.info(f"PipeWire 就绪，发现 {len(real_sinks)} 个 Audio/Sink")
        with _pw_ok_cache_lock:
            _pw_ok_cache.update({'_ts': now, 'ok': True})
        return True

    # 步骤5: 仍无 sink，但 pw-dump 有数据 → PW 运行但设备未注册
    audio_nodes = [obj for obj in pw_data if isinstance(obj, dict)
                   and obj.get('type') == 'PipeWire:Interface:Node'
                   and obj.get('info', {}).get('props', {}).get('media.class', '').startswith('Audio/')]
    if audio_nodes:
        logger.info(f"PipeWire 有 {len(audio_nodes)} 个音频节点但无 Sink，尝试通过 wpctl 补充")
        with _pw_ok_cache_lock:
            _pw_ok_cache.update({'_ts': now, 'ok': True})
        return True

    logger.info("PipeWire 运行中但无音频节点")
    with _pw_ok_cache_lock:
        _pw_ok_cache.update({'_ts': now, 'ok': False})
    return False


def _get_node_props_params(device_name):
    # 获取指定设备的 Props params（用于音量/平衡查询），返回 (props_params, node_obj) 或 ({}, None)
    pw_data = pw_dump()
    for obj in pw_data:
        if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
            continue
        props = obj.get('info', {}).get('props', {})
        if props.get('node.name') == device_name:
            params = obj.get('info', {}).get('params', {})
            return extract_pw_vol_params(params if isinstance(params, dict) else {}), obj
    return {}, None


def _get_alsa_devices():
    # 通过 aplay -l 和 /proc/asound/cards 获取 ALSA 设备列表
    alsa_devices = []

    aplay_result = run_command(f"{platform_paths.CMD_APLAY} -l 2>/dev/null", timeout=3)
    if not aplay_result['success'] or not aplay_result['stdout'].strip():
        return alsa_devices

    current_card = None
    for line in aplay_result['stdout'].strip().split('\n'):
        card_match = re.match(r'^card (\d+): (.+?) \[(.+?)\], device (\d+): (.+?) \[(.+?)\]', line)
        if card_match:
            card_idx = int(card_match.group(1))
            card_id = card_match.group(2)
            card_name = card_match.group(3)
            device_idx = int(card_match.group(4))
            device_id = card_match.group(5)
            device_name = card_match.group(6)

            current_card = {
                'card_idx': card_idx,
                'card_id': card_id,
                'card_name': card_name,
                'device_idx': device_idx,
                'device_id': device_id,
                'device_name': device_name,
                'alsa_name': f'alsa_card.{card_id}',
            }

            device_lower = (device_name + ' ' + device_id + ' ' + card_name).lower()
            if 'hdmi' in device_lower:
                current_card['audio_type'] = 'hdmi'
            elif 'pcsp' in device_lower or 'pcspkr' in device_lower:
                current_card['audio_type'] = 'beeper'
            elif 'iec958' in device_lower or 'spdif' in device_lower:
                current_card['audio_type'] = 'iec958'
            else:
                current_card['audio_type'] = 'internal'

            alsa_devices.append(current_card)

    return alsa_devices


def _activate_inactive_profiles():
    # 检查 WirePlumber 中 profile 为 Off 的设备，尝试激活合适的 profile
    activated = False

    status_result = run_command(f"{platform_paths.CMD_WPCTL} status 2>/dev/null", timeout=5)
    if not status_result['success'] or not status_result['stdout']:
        return activated

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


def _try_activate_profile(device_id, device_name):
    # 尝试为指定 WirePlumber 设备激活合适的 profile
    activated = False
    device_lower = device_name.lower()

    profiles_result = run_command(f"{platform_paths.CMD_WPCTL} inspect {device_id} 2>/dev/null", timeout=5)
    available_profiles = []

    if profiles_result['success'] and profiles_result['stdout']:
        current_profile_index = 0
        for line in profiles_result['stdout'].splitlines():
            if 'EnumDeviceProfile' in line:
                current_profile_index += 1
            name_match = re.search(r'Name:\s*"([^"]+)"', line)
            if name_match:
                profile_name = name_match.group(1)
                available_profiles.append((profile_name, current_profile_index))
            if 'Available: no' in line and available_profiles:
                available_profiles.pop()

    logger.info(f"设备 {device_name} 可用 profiles: {available_profiles}")

    alsa_devices = _get_alsa_devices()
    target_profile_names = []

    for ad in alsa_devices:
        if ad['alsa_name'].lower() in device_lower or ad['card_id'].lower() in device_lower:
            if ad['audio_type'] == 'hdmi':
                target_profile_names = ['hdmi-stereo-extra3', 'hdmi-stereo-extra2',
                                        'hdmi-stereo-extra1', 'hdmi-stereo',
                                        'pro-output-3', 'pro-output-2', 'pro-output-1',
                                        'iec958-stereo']
            elif ad['audio_type'] == 'beeper':
                target_profile_names = ['analog-stereo', 'iec958-stereo']
            elif ad['audio_type'] == 'iec958':
                target_profile_names = ['iec958-stereo']
            else:
                # 内置声卡：优先立体声，其次多声道，最后 S/PDIF/HDMI
                target_profile_names = [
                    'analog-stereo',
                    'analog-surround-71', 'analog-surround-51', 'analog-surround-40',
                    'iec958-stereo', 'hdmi-stereo',
                    'pro-output-1',
                ]
            break

    if not target_profile_names:
        if 'hdmi' in device_lower:
            target_profile_names = ['hdmi-stereo-extra3', 'hdmi-stereo-extra2',
                                    'hdmi-stereo-extra1', 'hdmi-stereo',
                                    'pro-output-3', 'pro-output-2', 'pro-output-1']
        else:
            target_profile_names = [
                'analog-stereo',
                'analog-surround-71', 'analog-surround-51', 'analog-surround-40',
                'iec958-stereo', 'hdmi-stereo',
            ]

    for target_name in target_profile_names:
        for avail_name, avail_index in available_profiles:
            if target_name == avail_name or target_name in avail_name or avail_name in target_name:
                if avail_name.lower() == 'off':
                    continue
                result = run_command(f"{platform_paths.CMD_WPCTL} set-profile {device_id} {avail_index}", timeout=5)
                if result['success']:
                    logger.info(f"已激活设备 {device_name} 的 profile: {avail_name} (index={avail_index})")
                    activated = True
                    break
        if activated:
            break

    # 如果按名称匹配失败，激活第一个非 Off 的可用 profile
    if not activated and available_profiles:
        for avail_name, avail_index in available_profiles:
            if avail_name.lower() != 'off':
                result = run_command(f"{platform_paths.CMD_WPCTL} set-profile {device_id} {avail_index}", timeout=5)
                if result['success']:
                    logger.info(f"已激活设备 {device_name} 的 profile: {avail_name} (index={avail_index})")
                    activated = True
                    break

    return activated


def _scan_audio_devices():
    pw_data = pw_dump()
    sinks = find_audio_sinks(pw_data)
    default_sink_name = get_default_sink_name()
    default_source_name = get_default_source_name()
    if not default_sink_name or not default_source_name:
        wp_sink, wp_source = _parse_wpctl_default()
        if not default_sink_name:
            default_sink_name = wp_sink
        if not default_source_name:
            default_source_name = wp_source
    logger.info(f"默认设备检测: sink='{default_sink_name}', source='{default_source_name}'")

    # PipeWire 诊断：如果没有 sink，记录所有节点类型
    if not sinks:
        pw_types = {}
        for obj in pw_data:
            if isinstance(obj, dict):
                t = obj.get('type', 'unknown')
                pw_types[t] = pw_types.get(t, 0) + 1
        logger.info(f"PipeWire 无 Audio/Sink 节点，pw-dump 节点类型: {pw_types}")
    else:
        logger.info(f"PipeWire 发现 {len(sinks)} 个 Audio/Sink")

    connected_hdmi = _get_connected_hdmi_info()
    hdmi_monitor_names = [h.get('monitor_name', '').upper() for h in connected_hdmi if h.get('monitor_name')]

    devices = []

    for sink in sinks:
        info = sink.get('info', {})
        props = info.get('props', {})
        node_id = sink.get('id')

        name = props.get('node.name', '')
        friendly_name = props.get('node.description', '') or props.get('node.nick', '') or name

        if not name:
            continue

        device_id_prop = props.get('device.id')
        device_props = find_device_props(pw_data, device_id_prop) if device_id_prop is not None else {}

        audio_type = _classify_audio_type(name, friendly_name, props, device_props, hdmi_monitor_names, 'sink')

        is_default = (name == default_sink_name)
        if is_default:
            logger.info(f"设备 '{name}' 被标记为默认输出")

        audio_info = _extract_node_audio_info(sink, pw_data)
        alsa_card_index = audio_info['extended'].get('alsa.card')

        devices.append({
            'name': name,
            'friendly_name': friendly_name,
            'driver': 'pipewire',
            'state': '默认' if is_default else '可用',
            'is_default': is_default,
            'audio_type': audio_type,
            'role': 'sink',
            'node_id': node_id,
            'volume': audio_info['volume'],
            'volume_flat': audio_info['volume_flat'],
            'volume_db': audio_info['volume_db'],
            'muted': audio_info['muted'],
            'channels': audio_info['channels'],
            'sample_rate': audio_info['sample_rate'],
            'sample_format': audio_info['sample_format'],
            'card_index': int(alsa_card_index) if alsa_card_index and str(alsa_card_index).isdigit() else node_id,
            'monitor_source': get_prop_with_fallback(props, device_props, 'monitor.source.name'),
            'ports': audio_info['ports'],
            'active_port': audio_info['active_port'],
            'channel_count': audio_info['channel_count'],
            'balance': audio_info['balance'],
            'extended': audio_info['extended'],
        })
        logger.info(f"[PW] {name}: type={audio_type}, vol={audio_info['volume']}%, muted={audio_info['muted']}, rate={audio_info['sample_rate']}, fmt='{audio_info['sample_format']}', ch={audio_info['channel_count']}, ports={len(audio_info['ports'])}, active_port='{audio_info['active_port']}'")

    # ALSA 回退：补充 pw-dump 未覆盖的设备（蜂鸣器、HDMI 等 profile 为 Off 的设备）
    devices = _supplement_alsa_devices(pw_data, sinks, devices, default_sink_name)

    # 扫描音频输入（Source）设备并合并
    source_devices = _scan_audio_sources(pw_data)
    devices.extend(source_devices)

    logger.info(f"音频设备总计: {len(devices)} 个 (Sink: {len(devices) - len(source_devices)}, Source: {len(source_devices)})")
    return {'devices': devices, 'default': default_sink_name, 'default_source': default_source_name}


def _supplement_alsa_devices(pw_data, pw_sinks, devices, default_sink_name):
    # 通过 aplay -l 发现 PipeWire 未创建 Sink 的 ALSA 设备，按声卡去重后补充
    pw_sink_names = {d['name'].lower() for d in devices}
    alsa_devices = _get_alsa_devices()

    # 按 card_id 去重：同一声卡只保留一条记录，优先选 analog/internal 类型
    seen_cards = {}
    for ad in alsa_devices:
        cid = ad['card_id'].lower()
        if cid not in seen_cards:
            seen_cards[cid] = ad
        else:
            # 优先保留 internal/analog，其次 beeper，最后 hdmi
            priority = {'internal': 0, 'beeper': 1, 'iec958': 2, 'hdmi': 3}
            old_prio = priority.get(seen_cards[cid].get('audio_type', ''), 4)
            new_prio = priority.get(ad.get('audio_type', ''), 4)
            if new_prio < old_prio:
                seen_cards[cid] = ad

    pw_pcm_cards = set()
    pw_card_names = set()
    for d in devices:
        ext = d.get('extended', {})
        pcm_card = str(ext.get('alsa.pcm.card', '')).strip()
        card_name = str(ext.get('alsa.card_name', '')).strip().lower()
        if pcm_card:
            pw_pcm_cards.add(pcm_card)
        if card_name:
            pw_card_names.add(card_name)

    for ad in seen_cards.values():
        card_id_lower = ad['card_id'].lower()
        card_name_lower = ad['card_name'].lower()

        matched = str(ad['card_idx']) in pw_pcm_cards
        if not matched and card_name_lower in pw_card_names:
            matched = True
        if not matched:
            for pw_name in pw_sink_names:
                if (card_id_lower in pw_name
                        or card_id_lower.replace('_', '-') in pw_name
                        or card_name_lower.replace(' ', '_') in pw_name):
                    matched = True
                    break
        if matched:
            continue

        # 检查 pw-dump 中是否有对应的 Device 对象（profile 可能为 Off），同时提取 props
        has_pw_device = False
        pw_dev_props = {}
        for obj in pw_data:
            if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Device':
                continue
            dev_props = obj.get('info', {}).get('props', {})
            dev_name = dev_props.get('device.name', '').lower()
            dev_nick = dev_props.get('device.nick', '').lower()
            dev_alias = dev_props.get('device.alias', '').lower()
            if (card_id_lower in dev_name or card_id_lower in dev_nick
                    or card_id_lower in dev_alias
                    or card_name_lower.replace(' ', '_') in dev_name):
                has_pw_device = True
                pw_dev_props = dev_props
                dev_id = obj.get('id')
                if dev_id is not None:
                    _try_activate_profile(dev_id, ad.get('card_name', ''))
                break

        # 构建 ALSA 回退设备信息
        audio_type = ad.get('audio_type', 'internal')
        name = f"alsa_output.{ad['card_id']}"
        friendly_name = ad.get('card_name', ad.get('device_name', name))

        # 获取 wpctl 音量信息
        vol_percent = 0
        muted = False
        wpctl_id = _get_wpctl_id_for_node(name, 'Sinks')
        if wpctl_id is not None:
            vol_info = _get_wpctl_volume(wpctl_id)
            vol_percent = vol_info['volume']
            muted = vol_info['muted']

        device_info = {
            'name': name,
            'friendly_name': friendly_name,
            'driver': get_prop_with_fallback(pw_dev_props, None, 'alsa.driver') or 'ALSA/PipeWire',
            'state': '默认' if name == default_sink_name else '可用（未激活）',
            'is_default': name == default_sink_name,
            'audio_type': audio_type,
            'role': 'sink',
            'node_id': None,
            'volume': vol_percent,
            'volume_flat': round(vol_percent / 100, 4) if vol_percent else 0.0,
            'volume_db': 0.0,
            'muted': muted,
            'channels': [],
            'sample_rate': 0,
            'sample_format': '',
            'card_index': ad.get('card_idx'),
            'monitor_source': '',
            'ports': [],
            'active_port': '',
            'channel_count': 0,
            'balance': 0.0,
            'needs_activate': True,
            'extended': _build_extended_props(pw_dev_props, None),
        }
        devices.append(device_info)
        logger.info(f"[ALSA] {name}: type={audio_type}, vol={vol_percent}%, muted={muted}, "
                     f"pw_device={'yes' if has_pw_device else 'no'}")

    return devices


def _get_alsa_capture_devices():
    # 通过 arecord -l 获取 ALSA 捕获（输入）设备列表
    alsa_devices = []
    arecord_result = run_command(f"{platform_paths.CMD_ARECORD} -l 2>/dev/null", timeout=3)
    if not arecord_result['success'] or not arecord_result['stdout'].strip():
        return alsa_devices

    for line in arecord_result['stdout'].strip().split('\n'):
        card_match = re.match(r'^card (\d+): (.+?) \[(.+?)\], device (\d+): (.+?) \[(.+?)\]', line)
        if card_match:
            card_idx = int(card_match.group(1))
            card_id = card_match.group(2)
            card_name = card_match.group(3)
            device_idx = int(card_match.group(4))
            device_id = card_match.group(5)
            device_name = card_match.group(6)

            device_lower = (device_name + ' ' + device_id + ' ' + card_name).lower()
            if 'hdmi' in device_lower:
                audio_type = 'hdmi'
            elif 'mic' in device_lower or 'microphone' in device_lower:
                audio_type = 'microphone'
            elif 'line' in device_lower:
                audio_type = 'linein'
            elif 'pcsp' in device_lower or 'pcspkr' in device_lower:
                audio_type = 'beeper'
            elif 'iec958' in device_lower or 'spdif' in device_lower:
                audio_type = 'iec958'
            else:
                audio_type = 'internal'

            alsa_devices.append({
                'card_idx': card_idx,
                'card_id': card_id,
                'card_name': card_name,
                'device_idx': device_idx,
                'device_id': device_id,
                'device_name': device_name,
                'alsa_name': f'alsa_card.{card_id}',
                'audio_type': audio_type,
                'direction': 'capture',
            })
    return alsa_devices


# 扫描音频输入设备
def _scan_audio_sources(pw_data=None):
    if pw_data is None:
        pw_data = pw_dump()
    sources = find_audio_sources(pw_data)
    default_source_name = get_default_source_name()
    devices = []

    for src in sources:
        info = src.get('info', {})
        props = info.get('props', {})
        node_id = src.get('id')
        name = props.get('node.name', '')
        friendly_name = props.get('node.description', '') or props.get('node.nick', '') or name
        if not name:
            continue

        device_id_prop = props.get('device.id')
        device_props = find_device_props(pw_data, device_id_prop) if device_id_prop is not None else {}

        audio_type = _classify_audio_type(name, friendly_name, props, device_props, role='source')

        audio_info = _extract_node_audio_info(src, pw_data)
        alsa_card_index = audio_info['extended'].get('alsa.card')

        is_default_source = (name == default_source_name)
        devices.append({
            'name': name,
            'friendly_name': friendly_name,
            'driver': 'pipewire',
            'state': '默认' if is_default_source else '可用',
            'is_default': is_default_source,
            'audio_type': audio_type,
            'role': 'source',
            'node_id': node_id,
            'volume': audio_info['volume'],
            'volume_flat': audio_info['volume_flat'],
            'volume_db': audio_info['volume_db'],
            'muted': audio_info['muted'],
            'channels': audio_info['channels'],
            'sample_rate': audio_info['sample_rate'],
            'sample_format': audio_info['sample_format'],
            'card_index': int(alsa_card_index) if alsa_card_index and str(alsa_card_index).isdigit() else node_id,
            'monitor_source': name,
            'ports': audio_info['ports'],
            'active_port': audio_info['active_port'],
            'channel_count': audio_info['channel_count'],
            'balance': 0.0,
            'extended': audio_info['extended'],
        })
        logger.info(f"[PW Source] {name}: type={audio_type}, rate={audio_info['sample_rate']}, fmt='{audio_info['sample_format']}', ch={audio_info['channel_count']}")

    # ALSA 捕获设备回退（按 card_id 去重）
    alsa_capture = _get_alsa_capture_devices()
    seen_capture_cards = {}
    for ad in alsa_capture:
        cid = ad['card_id'].lower()
        if cid not in seen_capture_cards:
            seen_capture_cards[cid] = ad
        else:
            priority = {'microphone': 0, 'linein': 1, 'internal': 2, 'hdmi': 3, 'iec958': 4}
            old_prio = priority.get(seen_capture_cards[cid].get('audio_type', ''), 5)
            new_prio = priority.get(ad.get('audio_type', ''), 5)
            if new_prio < old_prio:
                seen_capture_cards[cid] = ad

    pw_source_names = {d['name'].lower() for d in devices}
    for ad in seen_capture_cards.values():
        card_id_lower = ad['card_id'].lower()
        matched = False
        for pw_name in pw_source_names:
            if card_id_lower in pw_name:
                matched = True
                break
        if matched:
            continue
        name = f"alsa_input.{ad['card_id']}"
        devices.append({
            'name': name,
            'friendly_name': ad.get('card_name', ad.get('device_name', name)),
            'driver': 'ALSA/PipeWire',
            'state': '可用（未激活）',
            'audio_type': ad.get('audio_type', 'internal'),
            'role': 'source',
            'node_id': None,
            'volume': 0,
            'volume_flat': 0.0,
            'volume_db': 0.0,
            'muted': False,
            'channels': [],
            'sample_rate': 0,
            'sample_format': '',
            'card_index': ad.get('card_idx'),
            'monitor_source': name,
            'ports': [],
            'active_port': '',
            'channel_count': 0,
            'balance': 0.0,
            'needs_activate': True,
        })
        logger.info(f"[ALSA Capture] {name}: type={ad.get('audio_type', 'internal')}")

    return devices


def activate_audio_device(device_name):
    if not device_name:
        raise InvalidParamError('设备名不能为空')

    # 从设备名提取 card_id（如 alsa_output.pch_hdmi → pch_hdmi）
    card_id = device_name.replace('alsa_output.', '').replace('alsa_card.', '')

    pw_data = pw_dump()

    # 查找 pw-dump 中对应的 Device 对象
    for obj in pw_data:
        if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Device':
            continue
        dev_props = obj.get('info', {}).get('props', {})
        dev_name = dev_props.get('device.name', '').lower()
        dev_nick = dev_props.get('device.nick', '').lower()
        dev_alias = dev_props.get('device.alias', '').lower()

        if (card_id.lower() in dev_name or card_id.lower() in dev_nick
                or card_id.lower() in dev_alias):
            dev_id = obj.get('id')
            if dev_id is not None:
                activated = _try_activate_profile(dev_id, card_id)
                if activated:
                    time.sleep(2)
                    return {'message': f'设备 {device_name} 已激活', 'device': device_name}

    # 未在 pw-dump 中找到，尝试通过 wpctl status 查找
    status_result = run_command(f"{platform_paths.CMD_WPCTL} status 2>/dev/null", timeout=5)
    if status_result['success'] and status_result['stdout']:
        for line in status_result['stdout'].splitlines():
            dev_match = re.match(r'\s*(\d+)\.\s+.+?\[(.+?)\]', line.strip())
            if dev_match:
                dev_id_str = dev_match.group(1)
                dev_name_str = dev_match.group(2).lower()
                if card_id.lower() in dev_name_str:
                    try:
                        dev_id_int = int(dev_id_str)
                        activated = _try_activate_profile(dev_id_int, card_id)
                        if activated:
                            time.sleep(2)
                            return {'message': f'设备 {device_name} 已激活', 'device': device_name}
                    except (ValueError, TypeError):
                        pass

    raise DeviceNotFoundError(f'未找到设备 {device_name}，无法激活')


# 切换音频输出端口
def set_route(device_name, route_name):
    if not device_name or not route_name:
        raise InvalidParamError('设备名和端口名不能为空')

    wpctl_id = _get_wpctl_id_for_node(device_name, 'Sinks')
    if wpctl_id is None:
        raise DeviceNotFoundError(f'未找到设备 {device_name} 的 wpctl ID')

    result = run_command(f"{platform_paths.CMD_WPCTL} set-route {wpctl_id} {shlex.quote(route_name)}", timeout=5)
    if result['success']:
        return {'message': f'端口已切换到 {route_name}', 'route': route_name}
    raise CommandError(f'切换端口失败: {result.get("stderr", "")}')


def set_profile(device_name, profile_name):
    if not device_name or not profile_name:
        raise InvalidParamError('设备名和 Profile 名不能为空')

    wp_device_id = _get_wpctl_device_id(device_name)
    if wp_device_id is None:
        pw_data = pw_dump()
        card_id = device_name.replace('alsa_output.', '').replace('alsa_card.', '')
        for obj in pw_data:
            if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Device':
                continue
            dev_props = obj.get('info', {}).get('props', {})
            dev_name = dev_props.get('device.name', '').lower()
            dev_nick = dev_props.get('device.nick', '').lower()
            if card_id.lower() in dev_name or card_id.lower() in dev_nick:
                wp_device_id = obj.get('id')
                break

    if wp_device_id is None:
        raise DeviceNotFoundError(f'未找到设备 {device_name} 的 Device ID')

    result = run_command(f"{platform_paths.CMD_WPCTL} set-profile {wp_device_id} {shlex.quote(profile_name)}", timeout=5)
    if result['success']:
        time.sleep(1)
        return {'message': f'Profile 已切换到 {profile_name}', 'profile': profile_name}
    raise CommandError(f'切换 Profile 失败: {result.get("stderr", "")}')


def get_profiles(device_name):
    pw_data = pw_dump()
    card_id = device_name.replace('alsa_output.', '').replace('alsa_card.', '')

    for obj in pw_data:
        if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Device':
            continue
        dev_props = obj.get('info', {}).get('props', {})
        dev_name = dev_props.get('device.name', '').lower()
        dev_nick = dev_props.get('device.nick', '').lower()
        dev_alias = dev_props.get('device.alias', '').lower()

        if (card_id.lower() in dev_name or card_id.lower() in dev_nick
                or card_id.lower() in dev_alias):
            params = obj.get('info', {}).get('params', {})
            if not isinstance(params, dict):
                params = {}

            profiles = []
            active_profile = ''
            enum_profiles = params.get('EnumProfile', [])
            current_profiles = params.get('Profile', [])

            for ep in enum_profiles:
                if isinstance(ep, dict):
                    p_name = ep.get('name', '')
                    p_desc = ep.get('description', p_name)
                    profiles.append({'name': p_name, 'description': p_desc, 'priority': ep.get('priority', 0)})

            for cp in current_profiles:
                if isinstance(cp, dict):
                    active_profile = cp.get('name', '')
                    break

            return {'profiles': profiles, 'active_profile': active_profile}

    raise DeviceNotFoundError(f'未找到设备 {device_name}')


def get_audio_devices():
    pipewire_ok = _ensure_audio_pipewire()
    if pipewire_ok:
        return scan_audio_devices()
    cached_devices = config.get_audio_devices()
    default_sink = config.get_default_sink()
    default_source = config.get_default_source()
    if cached_devices:
        for dev in cached_devices:
            name = dev.get('name', '')
            if dev.get('role') == 'source':
                dev['is_default'] = (name == default_source)
            else:
                dev['is_default'] = (name == default_sink)
        return {'devices': cached_devices, 'default': default_sink, 'default_source': default_source, 'cached': True}
    logger.warning("PipeWire 不可用，无法获取音频设备")
    return {'devices': [], 'default': '', 'default_source': '', 'cached': False}


def scan_audio_devices():
    if not _ensure_audio_pipewire():
        logger.warning("PipeWire 不可用，无法扫描音频设备")
        return {'devices': [], 'default': '', 'default_source': ''}
    with _scan_lock:
        result = _scan_audio_devices()
        config.set_audio_devices(result['devices'])
        config.set_default_sink(result.get('default', ''))
        config.set_default_source(result.get('default_source', ''))
        return result


def get_audio_device_detail(device_name):
    # 获取指定音频设备的详细信息
    pw_data = pw_dump()
    node = find_pw_node(pw_data, name=device_name)
    if not node:
        raise DeviceNotFoundError(f'设备 {device_name} 未找到')

    info = node.get('info', {})
    props = info.get('props', {})
    node_id = node.get('id')

    # 基本信息
    name = props.get('node.name', '')
    friendly_name = props.get('node.description', '') or props.get('node.nick', '') or name
    driver = props.get('node.driver', '') or 'pipewire'

    # 判断 audio_type
    device_id_prop = props.get('device.id')
    device_props_detail = find_device_props(pw_data, device_id_prop) if device_id_prop is not None else {}
    connected_hdmi = _get_connected_hdmi_info()
    hdmi_monitor_names = [h.get('monitor_name', '').upper() for h in connected_hdmi if h.get('monitor_name')]
    audio_type = _classify_audio_type(name, friendly_name, props, device_props_detail, hdmi_monitor_names, 'sink')

    audio_info = _extract_node_audio_info(node, pw_data)

    device_detail = {
        'name': name,
        'friendly_name': friendly_name,
        'driver': driver,
        'node_id': node_id,
        'audio_type': audio_type,
        'sample_rate': audio_info['sample_rate'],
        'sample_format': audio_info['sample_format'],
        'channel_count': audio_info['channel_count'],
        'channels': audio_info['channels'],
        'volume': audio_info['volume'],
        'volume_flat': audio_info['volume_flat'],
        'volume_db': audio_info['volume_db'],
        'muted': audio_info['muted'],
        'balance': audio_info['balance'],
        'ports': audio_info['ports'],
        'active_port': audio_info['active_port'],
        'profiles': audio_info['profiles'],
        'active_profile': audio_info['active_profile'],
        'extended_props': audio_info['extended'],
    }

    return device_detail


def set_default_device(device_name):
    if not _SAFE_DEVICE_PATTERN.match(device_name):
        raise InvalidParamError('无效的设备名')
    for get_id in [_get_wpctl_device_id, _get_wpctl_id_for_sink]:
        node_id = get_id(device_name)
        if node_id is not None:
            result = run_command(f"{platform_paths.CMD_WPCTL} set-default {node_id}", timeout=5)
            if result['success']:
                config.set_default_sink(device_name)
                return {'message': f'默认设备已设为: {device_name}', 'device': device_name}
    node_id = get_node_id_by_name(device_name)
    if node_id is not None:
        result = run_command(f"{platform_paths.CMD_PW_CLI} set-default {node_id}", timeout=5)
        if result['success']:
            config.set_default_sink(device_name)
            return {'message': f'默认设备已设为: {device_name}', 'device': device_name}
    result = run_command(f"{platform_paths.CMD_PW_METADATA} -n settings 0 'default.audio.sink' {shlex.quote(device_name)} 2>/dev/null", timeout=5)
    if result['success']:
        config.set_default_sink(device_name)
        return {'message': f'默认设备已设为: {device_name}', 'device': device_name}
    raise CommandError('设置默认设备失败')


def get_volume(device_name=None):
    if device_name is not None and not _SAFE_DEVICE_PATTERN.match(device_name):
        raise InvalidParamError('无效的设备名')
    if not device_name:
        device_name = get_default_sink_name()
        if not device_name:
            raise DeviceNotFoundError('无法获取默认设备')

    result = volume_controller.get_volume(device_name)
    if result is not None:
        return result
    raise CommandError('获取音量失败')


# 验证并返回实际音量
def _verify_and_return_volume(device_name, target_volume):
    verify = get_volume(device_name)
    actual_pct = verify.get('volume', -1)
    logger.info(f"设置音量: {device_name} -> 目标{target_volume}% 实际{actual_pct}%")
    channels = []
    try:
        for d in get_audio_devices().get('devices', []):
            if d.get('name') == device_name:
                for ch in d.get('channels', []):
                    channels.append({
                        'channel': ch.get('channel', ''),
                        'volume': actual_pct,
                        'effective_volume': actual_pct,
                    })
                break
    except Exception:
        pass
    return {'message': f'音量已设为 {actual_pct}%', 'verified_volume': actual_pct, 'channels': channels}


def set_volume(device_name=None, volume=50):
    if device_name is not None and not _SAFE_DEVICE_PATTERN.match(device_name):
        raise InvalidParamError('无效的设备名')
    volume = max(0, min(100, volume))
    if not device_name:
        device_name = get_default_sink_name()
        if not device_name:
            raise DeviceNotFoundError('无法获取默认设备')
    volume_controller.set_volume(device_name, volume)
    return _verify_and_return_volume(device_name, volume)


def set_mute(device_name=None, mute=True):
    if device_name is not None and not _SAFE_DEVICE_PATTERN.match(device_name):
        raise InvalidParamError('无效的设备名')
    if not device_name:
        device_name = get_default_sink_name()
        if not device_name:
            raise DeviceNotFoundError('无法获取默认设备')
    return volume_controller.set_mute(device_name, mute)


def _is_pcspkr(device_name):
    return device_name and ('pcspkr' in device_name.lower() or 'pcsp' in device_name.lower())


def get_balance(device_name=None):
    if not device_name:
        device_name = get_default_sink_name()
    if not device_name:
        raise DeviceNotFoundError('获取平衡信息失败')

    result = volume_controller.get_balance(device_name)
    return {**result, 'device': device_name}


def set_balance(device_name=None, balance=0.0):
    balance = max(-1.0, min(1.0, balance))
    if not device_name:
        device_name = get_default_sink_name()
    if not device_name:
        raise DeviceNotFoundError('设置平衡失败')

    vc_result = volume_controller.set_balance(device_name, balance)
    actual_balance = vc_result.get('balance', balance)
    channels = []
    try:
        for d in get_audio_devices().get('devices', []):
            if d.get('name') == device_name:
                for ch in d.get('channels', []):
                    channels.append({
                        'channel': ch.get('channel', ''),
                        'volume': ch.get('effective_volume', ch.get('volume', 0)),
                        'effective_volume': ch.get('effective_volume', ch.get('volume', 0)),
                    })
                break
    except Exception:
        pass
    return {'message': f'平衡已设为 {actual_balance}', 'balance': actual_balance, 'channels': channels}


# 卸载蜂鸣器内核模块
def _ensure_pcspkr_module():
    run_command("modprobe -r snd-pcsp 2>/dev/null", timeout=3)
    run_command("modprobe -r pcspkr 2>/dev/null", timeout=3)


def _play_pcspkr(device_name=None, freq=1000):
    lsmod = run_command("lsmod 2>/dev/null", timeout=3)
    if 'pcspkr' not in lsmod.get('stdout', ''):
        run_command("modprobe pcspkr 2>/dev/null", timeout=3)
    run_command("modprobe -r snd-pcsp 2>/dev/null", timeout=3)
    beep_result = run_command(f"{platform_paths.CMD_BEEP} -f {freq} -l 200 -d 100 -n -f {freq} -l 200 2>/dev/null", timeout=5)
    if beep_result['success'] or beep_result['returncode'] == 0:
        return {'message': '蜂鸣器测试完成', 'method': 'beep'}
    if device_name:
        test_sound = os.path.join(platform_paths.SOUNDS_DIR, 'Front_Center.wav')
        if not os.path.exists(test_sound):
            test_sound = platform_paths.FALLBACK_SOUND
        pw_result = run_command(f"{platform_paths.CMD_PW_PLAY} --volume=0.5 {test_sound} 2>/dev/null", timeout=10)
        if pw_result['success']:
            return {'message': '蜂鸣器测试音播放完成', 'method': 'pw-play'}
        st_result = run_command(f"{platform_paths.CMD_SPEAKER_TEST} -t sine -f {freq} -l 1 2>/dev/null", timeout=5)
        if st_result['success'] or 'Time' in st_result['stdout']:
            return {'message': f'蜂鸣器 {freq}Hz 测试音播放完成', 'method': 'speaker-test'}
    run_command("echo -e '\\a' 2>/dev/null", timeout=3)
    raise CommandError('蜂鸣器不可用，请确保已加载 pcspkr 内核模块 (modprobe pcspkr) 且已安装 beep')


_ALSA_SOUNDS_DIR = platform_paths.SOUNDS_DIR
_ALSA_CHANNEL_WAVS = [
    ('Front_Left.wav', '前左'), ('Front_Right.wav', '前右'),
    ('Front_Center.wav', '前中'),
    ('Rear_Left.wav', '后左'), ('Rear_Right.wav', '后右'),
    ('Rear_Center.wav', '后中'),
    ('Side_Left.wav', '侧左'), ('Side_Right.wav', '侧右'),
    ('Noise.wav', '低音/噪声'),
]
_FALLBACK_SOUND = platform_paths.FALLBACK_SOUND

_POS_TO_WAV = {
    'FL': 'Front_Left.wav', 'FR': 'Front_Right.wav',
    'FC': 'Front_Center.wav', 'LFE': 'Noise.wav',
    'BL': 'Rear_Left.wav', 'BR': 'Rear_Right.wav',
    'BC': 'Rear_Center.wav',
    'SL': 'Side_Left.wav', 'SR': 'Side_Right.wav',
    'FLC': 'Front_Left.wav', 'FRC': 'Front_Right.wav',
    'RL': 'Rear_Left.wav', 'RR': 'Rear_Right.wav',
    'RC': 'Rear_Center.wav',
    'MONO': 'Front_Center.wav',
}


_POS_TO_SPEAKER_NUM = {
    'FL': 1, 'FR': 2, 'FC': 3, 'LFE': 4,
    'BL': 5, 'BR': 6, 'BC': 7, 'SL': 8, 'SR': 9,
    'RL': 5, 'RR': 6, 'RC': 7,
    'FLC': 1, 'FRC': 2,
    'MONO': 1,
}

_POS_LABEL = {
    'FL': '前左', 'FR': '前右', 'FC': '前中', 'LFE': '低音',
    'BL': '后左', 'BR': '后右', 'BC': '后中', 'SL': '侧左', 'SR': '侧右',
    'RL': '后左', 'RR': '后右', 'RC': '后中',
    'FLC': '前中左', 'FRC': '前中右',
    'MONO': '单声道',
}


# 播放声道测试音
def play_test_channel(device_name, position):
    if _is_pcspkr(device_name):
        return _play_pcspkr(device_name=device_name, freq=1000)

    if device_name:
        set_default_device(device_name)

    saved_vol = get_volume(device_name)
    saved_pct = saved_vol.get('volume', 50)
    saved_mute = saved_vol.get('muted', False)

    pos_upper = position.upper()
    label = _POS_LABEL.get(pos_upper, pos_upper)
    speaker_num = _POS_TO_SPEAKER_NUM.get(pos_upper)

    if not speaker_num:
        raise InvalidParamError(f'未知声道位置: {position}')

    ch_count = _get_device_channel_count(device_name)
    if ch_count < 1:
        ch_count = 2

    r = run_command(f"{platform_paths.CMD_SPEAKER_TEST} -c {ch_count} -t wav -l 1 -s {speaker_num} 2>/dev/null", timeout=10)
    if not (r['success'] or 'Time' in r.get('stdout', '')):
        r = run_command(f"{platform_paths.CMD_SPEAKER_TEST} -c {ch_count} -t sine -f 1000 -l 1 -s {speaker_num} 2>/dev/null", timeout=10)

    set_volume(device_name, saved_pct)
    if saved_mute:
        set_mute(device_name, True)

    if r['success'] or 'Time' in r.get('stdout', ''):
        return {'message': f'{label} 声道测试完成', 'channel': label, 'method': 'speaker-test'}
    raise CommandError(f'{label} 声道测试失败')


# 获取设备声道数
def _get_device_channel_count(device_name):
    if not device_name:
        return 2
    try:
        for d in get_audio_devices().get('devices', []):
            if d.get('name') == device_name:
                return len(d.get('channels', [])) or 2
    except Exception:
        pass
    return 2


def play_test_sound(device_name=None):
    if _is_pcspkr(device_name):
        return _play_pcspkr(device_name=device_name, freq=1000)

    if device_name:
        set_default_device(device_name)

    saved_vol = get_volume(device_name)
    saved_pct = saved_vol.get('volume', 50)
    saved_mute = saved_vol.get('muted', False)

    ch_count = _get_device_channel_count(device_name)
    r = run_command(f"{platform_paths.CMD_SPEAKER_TEST} -c {ch_count} -t wav -l 1 2>/dev/null", timeout=15)
    if not (r['success'] or 'Time' in r.get('stdout', '')):
        r = run_command(f"{platform_paths.CMD_SPEAKER_TEST} -c {ch_count} -t sine -f 1000 -l 1 2>/dev/null", timeout=15)

    set_volume(device_name, saved_pct)
    if saved_mute:
        set_mute(device_name, True)

    if r['success'] or 'Time' in r.get('stdout', ''):
        return {'message': '测试音播放完成', 'method': 'speaker-test'}
    fallback = _FALLBACK_SOUND if os.path.exists(_FALLBACK_SOUND) else None
    if fallback:
        r = run_command(f"{platform_paths.CMD_PW_PLAY} {fallback} 2>/dev/null", timeout=10)
        if r['success']:
            return {'message': '测试音播放完成', 'method': 'pw-play'}
    raise CommandError(f'在设备 {device_name or "默认设备"} 上播放测试音失败')


# 恢复保存的默认设备
def restore_default_device():
    saved = config.get_default_sink()
    if saved:
        for get_id in [_get_wpctl_device_id, _get_wpctl_id_for_sink]:
            node_id = get_id(saved)
            if node_id is not None:
                result = run_command(f"{platform_paths.CMD_WPCTL} set-default {node_id}", timeout=5)
                if result['success']:
                    return True
    return False


def auto_set_defaults():
    # 首次启动时自动设置默认音频输出/输入
    devices_result = get_audio_devices()
    devices = devices_result.get('devices', [])
    if not devices:
        return

    sinks = [d for d in devices if d.get('role') != 'source' and not _is_pcspkr(d.get('name', ''))]
    sources = [d for d in devices if d.get('role') == 'source']

    current_default = config.get_default_sink()

    if not current_default and sinks:
        preferred = None
        for d in sinks:
            name = d.get('name', '')
            if 'analog-stereo' in name:
                preferred = d
                break
        if not preferred:
            for d in sinks:
                if 'hdmi' not in d.get('name', '').lower():
                    preferred = d
                    break
        target = preferred or sinks[0]
        set_default_device(target['name'])
        logger.info(f"自动设置默认音频输出: {target['name']}")

    default_source = config.get_default_source()
    if not default_source and sources:
        if len(sources) == 1:
            src = sources[0]
            node_id = _get_wpctl_device_id(src['name'])
            if node_id is not None:
                run_command(f"{platform_paths.CMD_WPCTL} set-default {node_id}", timeout=5)
                config.set_default_source(src['name'])
                logger.info(f"自动设置默认音频输入: {src['name']}")


# 静音所有蜂鸣器 Sink
def _mute_pcspkr_sinks():
    pw_data = pw_dump()
    muted = []
    for obj in pw_data:
        if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
            continue
        props = obj.get('info', {}).get('props', {})
        name = props.get('node.name', '').lower()
        if 'pcspkr' in name or 'pcsp' in name:
            node_id = obj.get('id')
            if node_id is not None:
                run_command(f"{platform_paths.CMD_WPCTL} set-mute {node_id} 1", timeout=3)
                muted.append(props.get('node.name', ''))
    if muted:
        logger.info(f"已静音蜂鸣器 sink: {muted}")
    return muted


# 激活蓝牙 Sink 并设默认
def activate_bluez_sink(mac):
    _mute_pcspkr_sinks()
    normalized_mac = mac.replace(':', '_')
    for attempt in range(3):
        pw_data = pw_dump()
        for obj in pw_data:
            if not isinstance(obj, dict):
                continue
            if obj.get('type') != 'PipeWire:Interface:Node':
                continue
            props = obj.get('info', {}).get('props', {})
            media_class = props.get('media.class', '')
            if media_class not in ('Audio/Sink', 'Audio/Sink/Virtual'):
                continue
            node_name = props.get('node.name', '')
            if normalized_mac in node_name or mac.upper() in node_name:
                node_id = obj.get('id')
                if node_id is not None:
                    run_command(f"{platform_paths.CMD_WPCTL} set-mute {node_id} 0", timeout=3)
                    vol_result = run_command(f"{platform_paths.CMD_WPCTL} get-volume {node_id} 2>/dev/null", timeout=3)
                    if vol_result['success'] and vol_result['stdout']:
                        m = re.search(r'Volume:\s*([\d.]+)', vol_result['stdout'])
                        if m and float(m.group(1)) < 0.1:
                            run_command(f"{platform_paths.CMD_WPCTL} set-volume {node_id} 0.5", timeout=3)
                            logger.info(f"蓝牙设备 {node_name} 音量过低，已调整为 50%")
                    result = run_command(f"{platform_paths.CMD_WPCTL} set-default {node_id}", timeout=5)
                    if result['success']:
                        config.set_default_sink(node_name)
                        logger.info(f"蓝牙音频 sink 已激活: {node_name} (id={node_id})")
                        return True
        pa_sinks = run_command(f"{platform_paths.CMD_PACTL} list sinks short 2>/dev/null | grep '{normalized_mac}'", timeout=5)
        if pa_sinks['stdout'].strip():
            for line in pa_sinks['stdout'].strip().split('\n'):
                parts = line.split('\t')
                if len(parts) >= 2:
                    sink_name = parts[1]
                    run_command(f"{platform_paths.CMD_PACTL} set-sink-mute {shlex.quote(sink_name)} 0", timeout=3)
                    set_result = run_command(f"{platform_paths.CMD_PACTL} set-default-sink {shlex.quote(sink_name)}", timeout=5)
                    if set_result['success']:
                        config.set_default_sink(sink_name)
                        logger.info(f"蓝牙音频 sink 已激活(pactl): {sink_name}")
                        return True
        if attempt < 2:
            time.sleep(2)
    logger.warning(f"蓝牙音频 sink 激活失败: {mac}")
    return False


# USB 声卡 & 音频路由封装

def get_usb_audio_devices():
    """查找所有 USB 音频设备（Sink 和 Source），返回详细信息"""
    pw_data = pw_dump()
    if not pw_data:
        raise CommandError('PipeWire 未运行或无数据')

    default_sink_name = get_default_sink_name()
    default_source_name = get_default_source_name()
    devices = []

    for obj in pw_data:
        if not isinstance(obj, dict):
            continue
        if obj.get('type') != 'PipeWire:Interface:Node':
            continue
        info = obj.get('info', {})
        props = info.get('props', {})
        media_class = props.get('media.class', '')
        if media_class not in ('Audio/Sink', 'Audio/Sink/Virtual',
                               'Audio/Source', 'Audio/Source/Virtual'):
            continue

        # 通过 node props 或 device props 检查 device.bus == "usb"
        device_id_prop = props.get('device.id')
        device_props = find_device_props(pw_data, device_id_prop) if device_id_prop is not None else {}
        bus = get_prop_with_fallback(props, device_props, 'device.bus', '')
        if bus.lower() != 'usb':
            continue

        node_id = obj.get('id')
        name = props.get('node.name', '')
        friendly_name = (props.get('node.description', '')
                         or props.get('node.nick', '')
                         or name)
        role = 'sink' if 'Sink' in media_class else 'source'

        audio_info = _extract_node_audio_info(obj, pw_data)

        # USB 设备使用 wpctl 获取音量（更可靠）
        vol_info = _get_wpctl_volume(node_id) if node_id is not None else {'volume': 0, 'muted': False}

        is_default = (name == default_sink_name) if role == 'sink' else (name == default_source_name)

        devices.append({
            'name': name,
            'friendly_name': friendly_name,
            'role': role,
            'node_id': node_id,
            'volume': vol_info['volume'],
            'muted': vol_info['muted'],
            'vendor_id': get_prop_with_fallback(props, device_props, 'device.vendor.id', ''),
            'product_id': get_prop_with_fallback(props, device_props, 'device.product.id', ''),
            'vendor_name': get_prop_with_fallback(props, device_props, 'device.vendor.name', ''),
            'product_name': get_prop_with_fallback(props, device_props, 'device.product.name', ''),
            'alsa_card_name': get_prop_with_fallback(props, device_props, 'alsa.card_name', ''),
            'is_default': is_default,
            'sample_rate': audio_info['sample_rate'],
            'sample_format': audio_info['sample_format'],
            'channel_count': audio_info['channel_count'],
        })

    return devices


def get_audio_streams():
    """查询所有活跃音频流，并补充连接 sink 的完整设备详情"""
    try:
        import route_manager
        result = route_manager.get_audio_streams()
        if not result.get('success'):
            return result

        streams = result.get('data', [])
        # 为每条流补充所连接 sink 的完整设备信息
        all_devices_result = get_audio_devices()
        devices_list = all_devices_result.get('devices', [])

        for stream in streams:
            sink_details = []
            for sink_name in stream.get('connected_sinks', []):
                for dev in devices_list:
                    if dev.get('name') == sink_name:
                        sink_details.append(dev)
                        break
            stream['sink_device_details'] = sink_details

        return streams

    except ImportError:
        logger.warning("route_manager 模块不可用")
        raise CommandError('route_manager 模块不可用')
    except Exception as e:
        logger.error(f"获取音频流失败: {e}")
        raise CommandError(str(e))


def route_audio_stream(stream_node_id, target_sink_name):
    """将音频流路由到指定 Sink，若该流为唯一活跃流则同时更新默认 Sink"""
    try:
        import route_manager
        result = route_manager.route_audio_stream(stream_node_id, target_sink_name)
        if not result.get('success'):
            return result

        # 检查是否为唯一活跃流，若是则更新默认 sink
        try:
            streams_result = route_manager.get_audio_streams()
            if streams_result.get('success'):
                active_streams = [s for s in streams_result.get('data', [])
                                  if s.get('media_class') == 'Audio/Playback'
                                  and s.get('connected_sinks')]
                if len(active_streams) == 1 and active_streams[0].get('node_id') == stream_node_id:
                    run_command(f"{platform_paths.CMD_WPCTL} set-default {result['data']['sink_node_id']}", timeout=5)
                    config.set_default_sink(target_sink_name)
                    logger.info(f"唯一活跃流路由后已更新默认 Sink: {target_sink_name}")
        except Exception as e:
            logger.warning(f"更新默认 Sink 失败: {e}")

        return result

    except ImportError:
        logger.warning("route_manager 模块不可用")
        raise CommandError('route_manager 模块不可用')
    except Exception as e:
        logger.error(f"路由音频流失败: {e}")
        raise CommandError(str(e))


def unlink_audio_stream(stream_node_id, link_id=None):
    """断开音频流的链接"""
    try:
        import route_manager
        return route_manager.unlink_stream(stream_node_id, link_id)
    except ImportError:
        logger.warning("route_manager 模块不可用")
        raise CommandError('route_manager 模块不可用')
    except Exception as e:
        logger.error(f"断开音频流链接失败: {e}")
        raise CommandError(str(e))


def get_audio_routing_status():
    """获取当前音频路由的综合视图：流、链接、默认设备、USB 设备"""
    try:
        import route_manager

        streams_result = route_manager.get_audio_streams()
        links_result = route_manager.get_all_links()
        usb_devices = get_usb_audio_devices()

        default_sink = get_default_sink_name()
        default_source = get_default_source_name()

        return {
            'streams': streams_result.get('data', []) if streams_result.get('success') else [],
            'links': links_result.get('data', []) if links_result.get('success') else [],
            'default_sink': default_sink,
            'default_source': default_source,
            'usb_devices': usb_devices,
        }

    except ImportError:
        logger.warning("route_manager 模块不可用")
        raise CommandError('route_manager 模块不可用')
    except Exception as e:
        logger.error(f"获取音频路由状态失败: {e}")
        raise CommandError(str(e))


def detect_usb_hotplug():
    """检测 USB 音频设备热插拔变化，与缓存状态比较后返回新增/移除列表"""
    try:
        current_devices = get_usb_audio_devices()
        current_names = {d['name'] for d in current_devices if d.get('name')}

        # 从 config 读取上次缓存的 USB 设备列表
        cfg = config.load_config()
        cached_usb = cfg.get('usb_audio_devices', [])
        cached_names = {d.get('name', '') for d in cached_usb if d.get('name')}

        added = [d for d in current_devices if d.get('name') and d['name'] not in cached_names]
        removed = [d for d in cached_usb if d.get('name') and d['name'] not in current_names]

        # 更新缓存
        def _update_usb(cfg_inner):
            cfg_inner['usb_audio_devices'] = current_devices
        config._atomic_update(_update_usb)

        if added:
            logger.info(f"USB 音频设备新增: {[d['name'] for d in added]}")
        if removed:
            logger.info(f"USB 音频设备移除: {[d['name'] for d in removed]}")

        return {
            'added': added,
            'removed': removed,
            'current': current_devices,
        }

    except Exception as e:
        logger.error(f"检测 USB 热插拔失败: {e}")
        raise CommandError(str(e))
