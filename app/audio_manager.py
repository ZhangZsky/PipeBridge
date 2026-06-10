import re
import os
import time
import logging
import threading
import shlex
from utils import run_command, pw_dump, find_pw_node, get_node_id_by_name, get_node_name_by_id, get_default_sink_name, get_default_source_name, _parse_wpctl_default, extract_pw_vol_params, find_audio_sinks, find_audio_sources, get_prop_with_fallback, find_device_props, parse_edid_monitor_name, parse_edid_physical_size
from node_info_extractor import _extract_node_audio_info, _build_extended_props
import config
import platform_paths
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError
from volume_controller import volume_controller
from wp_config_manager import WPConfigManager
from pipewire_healer import (_check_pw_running, _check_wireplumber_running,
                              _ensure_sinks_exist, _diagnose_and_fix_no_sink,
                              _ensure_audio_pipewire, _activate_inactive_profiles,
                              _diagnose_no_sinks)
from alsa_fallback import _get_alsa_devices, _supplement_alsa_devices, _get_alsa_capture_devices

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



logger = logging.getLogger('MediaHub')

_wpc = WPConfigManager()

_scan_lock = threading.Lock()


def _has_connected_bluetooth():
    try:
        import bluetooth_manager as _bt_mod
        return bool(_bt_mod.check_bluetooth_connections())
    except Exception:
        return False


def _check_pw_running_only():
    """仅检查 PipeWire 是否运行，不触发 healer 或任何修复操作。
    用于扫描场景，避免重载/重启 WirePlumber 破坏蓝牙音频连接。"""
    from pipewire_healer import _check_pw_running, _check_wireplumber_running
    if not _check_pw_running():
        return False
    wp_was_running = _check_wireplumber_running()
    if not wp_was_running:
        # WirePlumber 刚启动时需要等待设备枚举完成
        time.sleep(1)
    # 刷新 pw_dump 缓存，确保获取最新设备数据
    pw_data = pw_dump(force_refresh=True)
    return bool(pw_data)


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
        # 优先精确匹配
        for avail_name, avail_index in available_profiles:
            if avail_name.lower() == 'off':
                continue
            if target_name == avail_name:
                result = run_command(f"{platform_paths.CMD_WPCTL} set-profile {device_id} {avail_index}", timeout=5)
                if result['success']:
                    logger.info(f"已激活设备 {device_name} 的 profile: {avail_name} (index={avail_index})")
                    activated = True
                break
        if activated:
            break
        # 回退到子串匹配
        for avail_name, avail_index in available_profiles:
            if avail_name.lower() == 'off':
                continue
            if target_name in avail_name or avail_name in target_name:
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
    pw_data = pw_dump(force_refresh=True)
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
    # 扫描时跳过 profile 激活，避免破坏已有音频连接
    devices = _supplement_alsa_devices(pw_data, sinks, devices, default_sink_name, skip_activate=True)

    # 扫描音频输入（Source）设备并合并
    source_devices = _scan_audio_sources(pw_data)
    devices.extend(source_devices)

    logger.info(f"音频设备总计: {len(devices)} 个 (Sink: {len(devices) - len(source_devices)}, Source: {len(source_devices)})")
    return {'devices': devices, 'default': default_sink_name, 'default_source': default_source_name}


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
    card_id = device_name
    for prefix in ('alsa_output.', 'alsa_card.'):
        if card_id.startswith(prefix):
            card_id = card_id[len(prefix):]
            break

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
    # 不调用 _ensure_audio_pipewire（含 healer），避免重载/重启 WirePlumber 破坏蓝牙连接
    # 仅检查 PipeWire 是否运行
    if not _check_pw_running_only():
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
    with _scan_lock:
        result = _scan_audio_devices()
        config.set_audio_devices(result['devices'])
        config.set_default_sink(result.get('default', ''))
        config.set_default_source(result.get('default_source', ''))
        return result


def scan_audio_devices():
    # 扫描时不运行 healer（重载/重启 WirePlumber），避免破坏蓝牙音频连接
    # 仅检查 PipeWire 是否运行，不做任何修复操作
    if not _check_pw_running_only():
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
        for d in config.get_audio_devices() or []:
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
        for d in config.get_audio_devices() or []:
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
    # 确保 snd-pcsp 模块已加载（aplay -l 需要它来发现蜂鸣器设备）
    # 仅卸载 pcspkr（input 层驱动），保留 snd-pcsp（ALSA 驱动）
    run_command("modprobe -r pcspkr 2>/dev/null", timeout=3)
    run_command("modprobe snd-pcsp 2>/dev/null", timeout=3)


def _play_pcspkr(device_name=None, freq=1000):
    # 直接使用 beep 命令，不操作内核模块（模块由 _ensure_pcspkr_module 在初始化时管理）
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

    saved_default = get_default_sink_name()
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
    if saved_default and saved_default != device_name:
        try:
            set_default_device(saved_default)
        except Exception:
            pass

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

    saved_default = get_default_sink_name()
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
    if saved_default and saved_default != device_name:
        try:
            set_default_device(saved_default)
        except Exception:
            pass

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
        pw_data = pw_dump(force_refresh=True)
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
    """废弃: 请直接使用 route_manager.get_audio_streams()"""
    import route_manager
    return route_manager.get_audio_streams()


def route_audio_stream(stream_id, target_sink_name):
    """废弃: 请直接使用 route_manager.route_audio_stream()"""
    import route_manager
    return route_manager.route_audio_stream(stream_id, target_sink_name)


def unlink_audio_stream(link_id):
    """废弃: 请直接使用 route_manager.unlink_stream()"""
    import route_manager
    return route_manager.unlink_stream(link_id)


def get_audio_routing_status():
    """废弃: 请直接使用 route_manager"""
    import route_manager
    streams = route_manager.get_audio_streams()
    links = route_manager.get_all_links()
    return {'streams': streams, 'links': links}


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
