import re
import os
import time
import logging
import threading
import shlex
from utils import (run_command, pw_dump, find_pw_node, get_node_id_by_name, get_node_name_by_id,
                   get_default_sink_name, get_default_source_name, _parse_wpctl_default,
                   find_audio_sinks, find_audio_sources,
                   get_prop_with_fallback, find_device_props, parse_edid_monitor_name,
                   pw_dump_invalidate, _get_pw_env)
from audio_helpers import _extract_node_audio_info, volume_controller
import config
import platform_paths
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError
from system_manager import WPConfigManager, check_pipewire_running, check_wireplumber_running

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
    if 'bluez' in device_api or 'bluez' in name_lower:
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



logger = logging.getLogger('PipeBridge')

_wpc = WPConfigManager()

_scan_lock = threading.Lock()


def _has_connected_bluetooth():
    try:
        import bluetooth_manager as _bt_mod
        return bool(_bt_mod.check_bluetooth_connections())
    except Exception:
        return False


# 仅检查 PipeWire 是否运行，不触发 healer 或任何修复操作
def _check_pw_running_only():
    if not check_pipewire_running():
        return False
    wp_was_running = check_wireplumber_running()
    if not wp_was_running:
        time.sleep(1)
    pw_data = pw_dump()
    return bool(pw_data)


def _get_connected_hdmi_info():

    connected_hdmi = []
    drm_path = platform_paths.SYS_DRM
    if not os.path.exists(drm_path):
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
    return connected_hdmi


def _get_wpctl_device_id(device_name):
    pw_data = pw_dump()
    node = find_pw_node(pw_data, name=device_name)
    if node is None:
        node = find_pw_node(pw_data, property_filters={'node.description': device_name})
    return node.get('id') if node else None


# 从节点名推导 WirePlumber 设备 ID（wpctl set-route/set-profile 需要设备 ID 而非节点 ID）
def _get_wpctl_route_device_id(device_name):
    """通过 pw-dump 节点的 device.id 属性获取关联的 Device 对象 ID"""
    pw_data = pw_dump()
    node = find_pw_node(pw_data, name=device_name)
    if node is None:
        node = find_pw_node(pw_data, property_filters={'node.description': device_name})
    if node is None:
        return None
    return node.get('info', {}).get('props', {}).get('device.id')


def _try_activate_profile(device_id, device_name):
    """尝试为指定 WirePlumber 设备激活合适的 profile。基于设备名推断类型，不再依赖 ALSA 工具。"""
    activated = False
    device_lower = device_name.lower()

    profiles_result = run_command(f"{platform_paths.CMD_WPCTL} inspect {device_id} 2>/dev/null", timeout=5)
    available_profiles = []

    if profiles_result['success'] and profiles_result['stdout']:
        current_profile_index = 0
        for line in profiles_result['stdout'].splitlines():
            # 从 EnumDeviceProfile (N) 行中提取索引号 N
            index_match = re.search(r'EnumDeviceProfile\s*\((\d+)\)', line)
            if index_match:
                current_profile_index = int(index_match.group(1))
            name_match = re.search(r'Name:\s*"([^"]+)"', line)
            if name_match:
                profile_name = name_match.group(1)
                available_profiles.append((profile_name, current_profile_index))
            if 'Available: no' in line and available_profiles:
                available_profiles.pop()

    logger.info(f"设备 {device_name} 可用 profiles: {available_profiles}")

    # 基于设备名推断目标 profile 类型
    target_profile_names = []
    if 'hdmi' in device_lower:
        target_profile_names = ['hdmi-stereo-extra3', 'hdmi-stereo-extra2',
                                'hdmi-stereo-extra1', 'hdmi-stereo',
                                'pro-output-3', 'pro-output-2', 'pro-output-1']
    elif 'pcsp' in device_lower or 'pcspkr' in device_lower:
        target_profile_names = ['analog-stereo', 'iec958-stereo']
    elif 'iec958' in device_lower or 'spdif' in device_lower:
        target_profile_names = ['iec958-stereo']
    else:
        target_profile_names = [
            'analog-stereo',
            'analog-surround-71', 'analog-surround-51', 'analog-surround-40',
            'iec958-stereo', 'hdmi-stereo',
            'pro-output-1',
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
            'profiles': audio_info['profiles'],
            'active_profile': audio_info['active_profile'],
            'channel_count': audio_info['channel_count'],
            'balance': audio_info['balance'],
            'extended': audio_info['extended'],
        })
        logger.info(f"[PW] {name}: type={audio_type}, vol={audio_info['volume']}%, muted={audio_info['muted']}, rate={audio_info['sample_rate']}, fmt='{audio_info['sample_format']}', ch={audio_info['channel_count']}, ports={len(audio_info['ports'])}, active_port='{audio_info['active_port']}'")

    # 扫描音频输入（Source）设备并合并
    source_devices = _scan_audio_sources(pw_data)
    devices.extend(source_devices)

    # 蜂鸣器设备保留显示但标记为 beeper 类型，set_default_device 会拒绝将其设为默认输出
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
            'profiles': audio_info['profiles'],
            'active_profile': audio_info['active_profile'],
            'channel_count': audio_info['channel_count'],
            'balance': 0.0,
            'extended': audio_info['extended'],
        })
        logger.info(f"[PW Source] {name}: type={audio_type}, rate={audio_info['sample_rate']}, fmt='{audio_info['sample_format']}', ch={audio_info['channel_count']}")

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

    # 判断设备角色，选择搜索 Sinks 或 Sources 区域
    pw_data = pw_dump()
    section = 'Sinks'
    for obj in pw_data:
        if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
            continue
        props = obj.get('info', {}).get('props', {})
        if props.get('node.name') == device_name:
            if 'Source' in props.get('media.class', ''):
                section = 'Sources'
            break

    # wpctl set-route 需要 WirePlumber 设备 ID（Devices 部分），不是节点 ID（Sinks/Sources 部分）
    route_device_id = _get_wpctl_route_device_id(device_name)
    if route_device_id is None:
        raise DeviceNotFoundError(f'未找到设备 {device_name} 的 WirePlumber 设备 ID')

    result = run_command(f"{platform_paths.CMD_WPCTL} set-route {route_device_id} {shlex.quote(route_name)}", timeout=5)
    if result['success']:
        return {'message': f'端口已切换到 {route_name}', 'route': route_name}
    raise CommandError(f'切换端口失败: {result.get("stderr", "")}')


def set_profile(device_name, profile_name):
    if not device_name or not profile_name:
        raise InvalidParamError('设备名和 Profile 名不能为空')

    # wpctl set-profile 需要 WirePlumber 设备 ID（与 set-route 相同）
    wp_device_id = _get_wpctl_route_device_id(device_name)
    if wp_device_id is None:
        # fallback: 从 pw-dump 查找 PipeWire:Interface:Device 对象的 ID
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

    # 先查找 profile 对应的索引，wpctl set-profile 需要数字索引而非名称
    profiles_result = get_profiles(device_name)
    profiles = profiles_result.get('profiles', [])
    target_index = None
    for p in profiles:
        if p.get('name') == profile_name or p.get('description') == profile_name:
            target_index = p.get('index')
            break
    if target_index is None:
        # 未找到匹配，尝试直接用名称（某些 wpctl 版本支持）
        target_index = profile_name
    result = run_command(f"{platform_paths.CMD_WPCTL} set-profile {wp_device_id} {shlex.quote(str(target_index))}", timeout=5)
    if result['success']:
        time.sleep(1)
        pw_dump_invalidate()  # 清除缓存，确保后续读取最新数据
        return {'message': f'Profile 已切换到 {profile_name}', 'profile': profile_name}
    raise CommandError(f'切换 Profile 失败: {result.get("stderr", "")}')


def get_profiles(device_name):
    pw_data = pw_dump()

    # 优先通过 Node 的 device.id 精确匹配 Device 对象（与 _extract_node_audio_info 一致）
    device_id = None
    for obj in pw_data:
        if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
            continue
        props = obj.get('info', {}).get('props', {})
        if props.get('node.name') == device_name:
            device_id = props.get('device.id')
            break

    # 回退：名称子串匹配
    card_id = device_name.replace('alsa_output.', '').replace('alsa_card.', '')

    for obj in pw_data:
        if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Device':
            continue

        # 精确匹配优先
        if device_id is not None and obj.get('id') != device_id:
            continue

        # 无 device.id 时回退到名称子串匹配
        if device_id is None:
            dev_props = obj.get('info', {}).get('props', {})
            dev_name = dev_props.get('device.name', '').lower()
            dev_nick = dev_props.get('device.nick', '').lower()
            dev_alias = dev_props.get('device.alias', '').lower()
            if not (card_id.lower() in dev_name or card_id.lower() in dev_nick
                    or card_id.lower() in dev_alias):
                continue

        params = obj.get('info', {}).get('params', {})
        if not isinstance(params, dict):
            params = {}

        profiles = []
        active_profile = ''
        enum_profiles = params.get('EnumProfile', [])
        if isinstance(enum_profiles, dict):
            enum_profiles = [enum_profiles]
        current_profiles = params.get('Profile', [])
        if isinstance(current_profiles, dict):
            current_profiles = [current_profiles]

        for ep in enum_profiles:
            if isinstance(ep, dict):
                p_name = ep.get('name', '')
                p_desc = ep.get('description', p_name)
                profiles.append({'name': p_name, 'description': p_desc, 'priority': ep.get('priority', 0), 'index': ep.get('index')})

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
        # pw_dump 偶尔返回空导致 0 个设备时，使用上次缓存数据
        if not result.get('devices'):
            cached_devices = config.get_audio_devices()
            if cached_devices:
                logger.info("pw-dump 返回空结果，使用缓存音频设备数据")
                default_sink = config.get_default_sink()
                default_source = config.get_default_source()
                for dev in cached_devices:
                    name = dev.get('name', '')
                    if dev.get('role') == 'source':
                        dev['is_default'] = (name == default_source)
                    else:
                        dev['is_default'] = (name == default_sink)
                return {'devices': cached_devices, 'default': default_sink, 'default_source': default_source, 'cached': True}
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
        # 回退：从缓存设备列表中查找
        cached = config.get_audio_devices()
        for d in cached:
            if d.get('name') == device_name:
                return d
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
    audio_type = _classify_audio_type(name, friendly_name, props, device_props_detail, hdmi_monitor_names,
                                      'source' if 'Source' in props.get('media.class', '') else 'sink')

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

    # 蜂鸣器禁止设为默认输出（仅静音使用，不作为默认音频设备）
    if _is_pcspkr(device_name):
        raise InvalidParamError('蜂鸣器设备不可设为默认输出')

    # 判断设备角色：Source 还是 Sink
    is_source = False
    pw_data = pw_dump()
    node = find_pw_node(pw_data, name=device_name)
    if node:
        media_class = node.get('info', {}).get('props', {}).get('media.class', '')
        if 'Source' in media_class:
            is_source = True

    node_id = _get_wpctl_device_id(device_name)
    if node_id is not None:
        result = run_command(f"{platform_paths.CMD_WPCTL} set-default {node_id}", timeout=5)
        if not result['success']:
            # 回退：pw-cli set-default（少数系统 wpctl 权限不足时有效）
            result = run_command(f"{platform_paths.CMD_PW_CLI} set-default {node_id}", timeout=5)
        if result['success']:
            if is_source:
                config.set_default_source(device_name)
            else:
                config.set_default_sink(device_name)
            return {'message': f'默认设备已设为: {device_name}', 'device': device_name, 'role': 'source' if is_source else 'sink'}
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


def _get_channels_from_pw(device_name):
    """从 pw-dump 中提取指定设备的声道信息"""
    channels = []
    try:
        pw_data = pw_dump()
        for obj in pw_data:
            if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
                continue
            if obj.get('info', {}).get('props', {}).get('node.name') == device_name:
                audio_info = _extract_node_audio_info(obj, pw_data)
                channels = audio_info.get('channels', [])
                break
    except Exception:
        pass
    return channels


def _resolve_device_name(device_name):
    """解析设备名，未指定时获取默认 Sink"""
    if device_name is not None and not _SAFE_DEVICE_PATTERN.match(device_name):
        raise InvalidParamError('无效的设备名')
    if not device_name:
        device_name = get_default_sink_name()
        if not device_name:
            raise DeviceNotFoundError('无法获取默认设备')
    return device_name


def _is_device_suspended(device_name):
    """检查设备是否被挂起（channelVolumes 为空，无法直接设置音量）"""
    try:
        props_params, _ = volume_controller._get_node_props(device_name)
        ch_vols = props_params.get('channelVolumes', [])
        return not ch_vols
    except Exception:
        return True


def _wake_device(device_name):
    """通过静音操作唤醒挂起的设备，使 channelVolumes 可写

    使用 wpctl set-mute 0 + pw-cli set-param 替代 speaker-test 正弦波，
    避免 20kHz 正弦波在蓝牙 A2DP 编码带宽限制下产生的低频噪声。
    """
    node_id = get_node_id_by_name(device_name)
    if node_id is None:
        logger.debug(f"唤醒设备失败：未找到节点 {device_name}")
        return
    # 取消静音触发 WirePlumber 与设备通信，唤醒挂起状态
    run_command(f"{platform_paths.CMD_WPCTL} set-mute {node_id} 0", timeout=3)
    time.sleep(0.3)
    # 设置默认 Props 参数，强制设备激活并恢复 channelVolumes
    run_command(
        f"{platform_paths.CMD_PW_CLI} set-param {node_id} Props '{{\"channelVolumes\":[1.0]}}'",
        timeout=3)
    pw_dump_invalidate()
    logger.info(f"设备已唤醒: {device_name}")


def set_volume(device_name=None, volume=50):
    device_name = _resolve_device_name(device_name)
    volume = max(0, min(100, volume))

    # 设备挂起时先唤醒（channelVolumes 为空会导致 wpctl set-volume 静默失败）
    if _is_device_suspended(device_name):
        logger.info(f"设备 {device_name} 处于挂起状态，先唤醒")
        _wake_device(device_name)

    vc_result = volume_controller.set_volume(device_name, volume)
    verified_vol = vc_result.get('volume', volume)

    channels = _get_channels_from_pw(device_name)
    for ch in channels:
        ch['volume'] = verified_vol
        ch['effective_volume'] = verified_vol
    return {'message': f'音量已设为 {verified_vol}%', 'verified_volume': verified_vol, 'channels': channels}


def set_mute(device_name=None, mute=True):
    device_name = _resolve_device_name(device_name)

    # 设备被挂起时先唤醒
    if _is_device_suspended(device_name):
        _wake_device(device_name)

    return volume_controller.set_mute(device_name, mute)


def _is_pcspkr(device_name):
    return device_name and ('pcspkr' in device_name.lower() or 'pcsp' in device_name.lower())


def get_balance(device_name=None):
    if device_name is not None and not _SAFE_DEVICE_PATTERN.match(device_name):
        raise InvalidParamError('无效的设备名')
    if not device_name:
        device_name = get_default_sink_name()
    if not device_name:
        raise DeviceNotFoundError('获取平衡信息失败')

    result = volume_controller.get_balance(device_name)
    return {**result, 'device': device_name}


def set_balance(device_name=None, balance=0.0):
    balance = max(-1.0, min(1.0, balance))
    device_name = _resolve_device_name(device_name)
    if not device_name:
        raise DeviceNotFoundError('设置平衡失败')

    cur_vol = get_volume(device_name)
    avg_vol = cur_vol.get('volume', 50)

    vc_result = volume_controller.set_balance(device_name, balance)
    actual_balance = vc_result.get('balance', balance)
    channels = _get_channels_from_pw(device_name)
    if channels and len(channels) >= 2:
        left = max(0, min(100, round(avg_vol * (1.0 - actual_balance))))
        right = max(0, min(100, round(avg_vol * (1.0 + actual_balance))))
        channels[0]['volume'] = left
        channels[0]['effective_volume'] = left
        channels[1]['volume'] = right
        channels[1]['effective_volume'] = right
    return {'message': f'平衡已设为 {actual_balance}', 'balance': actual_balance, 'channels': channels}


# 蜂鸣器内核模块管理
def _ensure_pcspkr_module():
    # snd_pcsp 注册 pcsp 声卡用于蜂鸣器设备显示，pcspkr 供 beep 命令发声
    # 两者均保留，通过 _mute_pcspkr_sinks 静音避免干扰默认设备
    # 确保蜂鸣器模块已加载
    run_command("modprobe snd_pcsp 2>/dev/null", timeout=3)
    run_command("modprobe pcspkr 2>/dev/null", timeout=3)


def _set_default_volumes():
    """服务启动时将所有音频设备音量设置为100%（覆盖 WirePlumber 默认的 40%）

    不同设备类型分类处理：
    - 蜂鸣器（beeper）：跳过，不调整音量（仅静音使用）
    - 蓝牙（bluetooth）：若 sink 已存在（服务启动前已连接），统一重置为 100%；
      若 sink 未出现（未连接或正在激活），由 activate_bluez_sink 在连接时处理
    - 其他类型（USB/HDMI/DisplayPort/内置/麦克风等）：统一设置为 100%（cubic 1.0）

    注意：本函数仅在服务启动时执行一次，不会覆盖运行中用户调整的音量。
    """
    try:
        result = scan_audio_devices()
        devices = result.get('devices', []) if isinstance(result, dict) else []
        if not devices:
            logger.debug("无音频设备，跳过默认音量设置")
            return

        set_count = 0
        for dev in devices:
            if not isinstance(dev, dict):
                continue
            # 蜂鸣器不调整音量（仅静音使用）
            if dev.get('audio_type') == 'beeper':
                continue
            # 未激活的设备没有 node_id，跳过
            if dev.get('needs_activate'):
                continue

            name = dev.get('name', '')
            # pw-dump 节点 ID 即 wpctl 节点 ID（两者是同一个 PipeWire 对象 ID）
            node_id = get_node_id_by_name(name)
            if node_id is None:
                node_id = dev.get('node_id')
            if node_id is None:
                logger.debug(f"跳过设备 {name}：无法获取节点 ID")
                continue

            try:
                # cubic volume 1.0 = 100% 线性音量，不会触发增益（>1.0 才是增益）
                run_command(
                    f"{platform_paths.CMD_WPCTL} set-volume {node_id} 1.0",
                    timeout=5)
                set_count += 1
            except Exception as e:
                logger.debug(f"设置设备 {name} 默认音量失败: {e}")

        pw_dump_invalidate()
        logger.info(f"已将 {set_count} 个设备默认音量设置为100%")
    except Exception as e:
        logger.warning(f"设置默认音量失败: {e}")


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
    raise CommandError('蜂鸣器不可用，请确保已安装 beep 命令 (apt-get install beep)')


_FALLBACK_SOUND = platform_paths.FALLBACK_SOUND


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

    # 构造带 PIPEWIRE_NODE 的环境，让 speaker-test 直接路由到目标设备
    play_env = _get_pw_env().copy()
    node_id = None
    if device_name:
        node_id = _get_wpctl_device_id(device_name)
        if node_id is not None:
            play_env['PIPEWIRE_NODE'] = str(node_id)
    logger.info(f"播放测试音: {device_name} 声道={label} ch={ch_count} node_id={node_id} speaker_num={speaker_num}")
    try:
        r = run_command(f"{platform_paths.CMD_SPEAKER_TEST} -c {ch_count} -t wav -l 1 -s {speaker_num} 2>/dev/null", timeout=10, env=play_env)
        logger.debug(f"speaker-test(wav) 结果: success={r['success']}, returncode={r.get('returncode')}, stdout={r.get('stdout','')[:200]}, stderr={r.get('stderr','')[:200]}")
        if not (r['success'] or 'Time' in r.get('stdout', '')):
            r = run_command(f"{platform_paths.CMD_SPEAKER_TEST} -c {ch_count} -t sine -f 1000 -l 1 -s {speaker_num} 2>/dev/null", timeout=10, env=play_env)
            logger.debug(f"speaker-test(sine) 结果: success={r['success']}, returncode={r.get('returncode')}, stdout={r.get('stdout','')[:200]}, stderr={r.get('stderr','')[:200]}")
    finally:
        try:
            set_volume(device_name, saved_pct)
            if saved_mute:
                set_mute(device_name, True)
            if saved_default and saved_default != device_name:
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
    # 构造带 PIPEWIRE_NODE 的环境，让 speaker-test 直接路由到目标设备（不依赖默认设备切换时序）
    play_env = _get_pw_env().copy()
    if device_name:
        node_id = _get_wpctl_device_id(device_name)
        if node_id is not None:
            play_env['PIPEWIRE_NODE'] = str(node_id)
    try:
        r = run_command(f"{platform_paths.CMD_SPEAKER_TEST} -c {ch_count} -t wav -l 1 2>/dev/null", timeout=15, env=play_env)
        if not (r['success'] or 'Time' in r.get('stdout', '')):
            r = run_command(f"{platform_paths.CMD_SPEAKER_TEST} -c {ch_count} -t sine -f 1000 -l 1 2>/dev/null", timeout=15, env=play_env)
    finally:
        try:
            set_volume(device_name, saved_pct)
            if saved_mute:
                set_mute(device_name, True)
            if saved_default and saved_default != device_name:
                set_default_device(saved_default)
        except Exception:
            pass

    if r['success'] or 'Time' in r.get('stdout', ''):
        return {'message': '测试音播放完成', 'method': 'speaker-test'}
    fallback = _FALLBACK_SOUND if os.path.exists(_FALLBACK_SOUND) else None
    if fallback:
        r = run_command(f"{platform_paths.CMD_PW_PLAY} {fallback} 2>/dev/null", timeout=10, env=play_env)
        if r['success']:
            return {'message': '测试音播放完成', 'method': 'pw-play'}
    raise CommandError(f'在设备 {device_name or "默认设备"} 上播放测试音失败')


# 恢复保存的默认设备
def restore_default_device():
    restored = False
    # 恢复默认 Sink
    saved_sink = config.get_default_sink()
    if saved_sink:
        node_id = _get_wpctl_device_id(saved_sink)
        if node_id is not None:
            result = run_command(f"{platform_paths.CMD_WPCTL} set-default {node_id}", timeout=5)
            if result['success']:
                restored = True
    # 恢复默认 Source
    saved_source = config.get_default_source()
    if saved_source:
        node_id = _get_wpctl_device_id(saved_source)
        if node_id is not None:
            result = run_command(f"{platform_paths.CMD_WPCTL} set-default {node_id}", timeout=5)
            if result['success']:
                restored = True
    return restored


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
        # 默认设备优先级：analog-stereo > HDMI > 其他（确保纯 HDMI 输出系统正确选 HDMI）
        preferred = None
        for d in sinks:
            name = d.get('name', '')
            if 'analog-stereo' in name:
                preferred = d
                break
        if not preferred:
            for d in sinks:
                if 'hdmi' in d.get('name', '').lower():
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
                # pw-dump 节点 ID 即 wpctl 节点 ID（同一个 PipeWire 对象 ID）
                node_id = obj.get('id')
                if node_id is not None:
                    run_command(f"{platform_paths.CMD_WPCTL} set-mute {node_id} 0", timeout=3)
                    # 蓝牙设备连接时统一重置音量为 100%（cubic 1.0），避免历史低音量或增益残留
                    run_command(f"{platform_paths.CMD_WPCTL} set-volume {node_id} 1.0", timeout=3)
                    logger.info(f"蓝牙设备 {node_name} 音量已重置为 100%")
                    result = run_command(f"{platform_paths.CMD_WPCTL} set-default {node_id}", timeout=5)
                    if result['success']:
                        config.set_default_sink(node_name)
                        logger.info(f"蓝牙音频 sink 已激活: {node_name} (id={node_id})")
                        return True
        if attempt < 2:
            time.sleep(2)
    logger.warning(f"蓝牙音频 sink 激活失败: {mac}")
    return False


# USB 声卡 & 音频路由封装

# 查找所有 USB 音频设备（Sink 和 Source），返回详细信息
def get_usb_audio_devices():
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

        is_default = (name == default_sink_name) if role == 'sink' else (name == default_source_name)

        devices.append({
            'name': name,
            'friendly_name': friendly_name,
            'role': role,
            'node_id': node_id,
            'volume': audio_info['volume'],
            'muted': audio_info['muted'],
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
