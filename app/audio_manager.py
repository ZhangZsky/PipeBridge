import re
import os
import json
import time
import logging
import threading
import shlex
from utils import (run_command, pw_dump, find_pw_node, get_node_id_by_name,
                   get_default_sink_name, get_default_source_name, _parse_wpctl_default,
                   find_audio_sinks, find_audio_sources,
                   get_prop_with_fallback, find_device_props, parse_edid_monitor_name,
                   pw_dump_invalidate, _get_pw_env, extract_pw_vol_params)
from audio_helpers import _extract_node_audio_info, volume_controller
import config
import platform_paths
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError
from system_manager import WPConfigManager, check_pipewire_running, check_wireplumber_running

_SAFE_DEVICE_PATTERN = re.compile(r'^[a-zA-Z0-9_.@:\[\]\/-]+$')

def _classify_audio_type(name, friendly_name='', props=None, device_props=None,
                         hdmi_monitor_names=None, role='sink'):
    if props is None:
        props = {}
    if device_props is None:
        device_props = {}
    name_lower = name.lower()
    friendly_upper = (friendly_name or '').upper()

    bus = get_prop_with_fallback(props, device_props, 'device.bus', '').lower()
    device_api = get_prop_with_fallback(props, device_props, 'device.api', '').lower()

    if bus == 'usb' or device_api == 'usb':
        return 'usb'
    if 'bluez' in device_api or 'bluez' in name_lower:
        return 'bluetooth'

    if 'hdmi' in name_lower or 'hdmi' in friendly_upper or 'display audio' in name_lower:
        return 'hdmi'
    card_name = get_prop_with_fallback(props, device_props, 'alsa.card_name', '').lower()
    if 'hdmi' in card_name:
        return 'hdmi'
    if 'dp' in name_lower and 'displayport' in (card_name or name_lower):
        return 'displayport'
    if hdmi_monitor_names:
        for mn in hdmi_monitor_names:
            if mn and mn in friendly_upper:
                return 'hdmi'

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

def _get_wpctl_route_device_id(device_name):
    pw_data = pw_dump()
    node = find_pw_node(pw_data, name=device_name)
    if node is None:
        node = find_pw_node(pw_data, property_filters={'node.description': device_name})
    if node is None:
        return None
    return node.get('info', {}).get('props', {}).get('device.id')

def _try_activate_profile(device_id, device_name):
    activated = False
    device_lower = device_name.lower()

    pw_data = pw_dump()
    available_profiles = []
    for obj in pw_data:
        if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Device':
            continue
        if obj.get('id') != device_id:
            continue
        params = obj.get('info', {}).get('params', {})
        if not isinstance(params, dict):
            break
        enum_profiles = params.get('EnumProfile', [])
        if isinstance(enum_profiles, dict):
            enum_profiles = [enum_profiles]
        for ep in enum_profiles:
            if not isinstance(ep, dict):
                continue
            p_name = ep.get('name', '')
            p_index = ep.get('index', 0)
            if ep.get('available', True) is False:
                continue
            available_profiles.append((p_name, p_index))
        break

    logger.info(f"设备 {device_name} 可用 profiles: {available_profiles}")

    target_profile_names = []
    if 'hdmi' in device_lower:
        target_profile_names = ['hdmi-stereo-extra3', 'hdmi-stereo-extra2',
                                'hdmi-stereo-extra1', 'hdmi-stereo',
                                'pro-output-3', 'pro-output-2', 'pro-output-1']
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

    source_devices = _scan_audio_sources(pw_data)
    devices.extend(source_devices)

    logger.info(f"音频设备总计: {len(devices)} 个 (Sink: {len(devices) - len(source_devices)}, Source: {len(source_devices)})")
    return {'devices': devices, 'default': default_sink_name, 'default_source': default_source_name}

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

    card_id = device_name
    for prefix in ('alsa_output.', 'alsa_card.'):
        if card_id.startswith(prefix):
            card_id = card_id[len(prefix):]
            break

    pw_data = pw_dump()

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

    raise DeviceNotFoundError(f'未找到设备 {device_name}，无法激活')

def set_route(device_name, route_name):
    if not device_name or not route_name:
        raise InvalidParamError('设备名和端口名不能为空')

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

    wp_device_id = _get_wpctl_route_device_id(device_name)
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

    profiles_result = get_profiles(device_name)
    profiles = profiles_result.get('profiles', [])
    target_index = None
    for p in profiles:
        if p.get('name') == profile_name or p.get('description') == profile_name:
            target_index = p.get('index')
            break
    if target_index is None:
        target_index = profile_name
    result = run_command(f"{platform_paths.CMD_WPCTL} set-profile {wp_device_id} {shlex.quote(str(target_index))}", timeout=5)
    if result['success']:
        time.sleep(1)
        pw_dump_invalidate()
        return {'message': f'Profile 已切换到 {profile_name}', 'profile': profile_name}
    raise CommandError(f'切换 Profile 失败: {result.get("stderr", "")}')

def get_profiles(device_name):
    pw_data = pw_dump()

    device_id = None
    for obj in pw_data:
        if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
            continue
        props = obj.get('info', {}).get('props', {})
        if props.get('node.name') == device_name:
            device_id = props.get('device.id')
            break

    card_id = device_name.replace('alsa_output.', '').replace('alsa_card.', '')

    for obj in pw_data:
        if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Device':
            continue

        if device_id is not None and obj.get('id') != device_id:
            continue

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
    pw_data = pw_dump()
    node = find_pw_node(pw_data, name=device_name)
    if not node:
        cached = config.get_audio_devices()
        for d in cached:
            if d.get('name') == device_name:
                return d
        raise DeviceNotFoundError(f'设备 {device_name} 未找到')

    info = node.get('info', {})
    props = info.get('props', {})
    node_id = node.get('id')

    name = props.get('node.name', '')
    friendly_name = props.get('node.description', '') or props.get('node.nick', '') or name
    driver = props.get('node.driver', '') or 'pipewire'

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
    if device_name is not None and not _SAFE_DEVICE_PATTERN.match(device_name):
        raise InvalidParamError('无效的设备名')
    if not device_name:
        device_name = get_default_sink_name()
        if not device_name:
            raise DeviceNotFoundError('无法获取默认设备')
    return device_name

def _is_device_suspended(device_name):
    try:
        props_params, _ = volume_controller._get_node_props(device_name)
        ch_vols = props_params.get('channelVolumes', [])
        return not ch_vols
    except Exception:
        return True

def _wake_device(device_name):
    node_id = get_node_id_by_name(device_name)
    if node_id is None:
        logger.debug(f"唤醒设备失败：未找到节点 {device_name}")
        return

    pw_data = pw_dump()
    node = find_pw_node(pw_data, name=device_name)
    ch_count = 2
    if node:
        info = node.get('info', {})
        props = info.get('props', {})
        try:
            audio_ch = int(props.get('audio.channels', 0))
            if 1 <= audio_ch <= 32:
                ch_count = audio_ch
        except (ValueError, TypeError):
            pass

    init_volumes = [1.0] * ch_count
    props_json = json.dumps({"mute": False, "channelVolumes": init_volumes})
    run_command(
        f"{platform_paths.CMD_PW_CLI} set-param {node_id} Props '{props_json}'",
        timeout=3)

    if 'bluez' in device_name.lower():
        time.sleep(1.0)
    else:
        time.sleep(0.5)

    pw_dump_invalidate()
    logger.info(f"设备已唤醒: {device_name} (声道数={ch_count})")

def set_volume(device_name=None, volume=50):
    device_name = _resolve_device_name(device_name)
    volume = max(0, min(100, volume))

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

    if _is_device_suspended(device_name):
        _wake_device(device_name)

    return volume_controller.set_mute(device_name, mute)

def set_channel_volume(device_name=None, channel_index=0, volume=50):
    device_name = _resolve_device_name(device_name)

    if _is_device_suspended(device_name):
        logger.info(f"设备 {device_name} 处于挂起状态，先唤醒")
        _wake_device(device_name)

    return volume_controller.set_channel_volume(device_name, channel_index, volume)

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

    if _is_device_suspended(device_name):
        logger.info(f"设备 {device_name} 处于挂起状态，先唤醒")
        _wake_device(device_name)

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

def _set_default_volumes():
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
            if dev.get('needs_activate'):
                continue

            name = dev.get('name', '')
            node_id = get_node_id_by_name(name)
            if node_id is None:
                node_id = dev.get('node_id')
            if node_id is None:
                logger.debug(f"跳过设备 {name}：无法获取节点 ID")
                continue

            try:
                ch_count = int(dev.get('channel_count', 0))
                if not ch_count:
                    node = find_pw_node(pw_dump(), name=name)
                    if node:
                        try:
                            ch_count = int(node.get('info', {}).get('props', {}).get('audio.channels', 0))
                        except (ValueError, TypeError):
                            pass
                if not ch_count:
                    ch_count = 2
                init_volumes = [1.0] * ch_count
                props_json = json.dumps({"channelVolumes": init_volumes})
                run_command(
                    f"{platform_paths.CMD_PW_CLI} set-param {node_id} Props '{props_json}'",
                    timeout=5)
                set_count += 1
            except Exception as e:
                logger.debug(f"设置设备 {name} 默认音量失败: {e}")

        pw_dump_invalidate()
        logger.info(f"已将 {set_count} 个设备默认音量设置为100%")
    except Exception as e:
        logger.warning(f"设置默认音量失败: {e}")

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

def play_test_channel(device_name, position):
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
    saved_default = get_default_sink_name()
    if device_name:
        set_default_device(device_name)

    saved_vol = get_volume(device_name)
    saved_pct = saved_vol.get('volume', 50)
    saved_mute = saved_vol.get('muted', False)

    ch_count = _get_device_channel_count(device_name)
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

def restore_default_device():
    restored = False
    saved_sink = config.get_default_sink()
    if saved_sink:
        node_id = _get_wpctl_device_id(saved_sink)
        if node_id is not None:
            result = run_command(f"{platform_paths.CMD_WPCTL} set-default {node_id}", timeout=5)
            if result['success']:
                restored = True
    saved_source = config.get_default_source()
    if saved_source:
        node_id = _get_wpctl_device_id(saved_source)
        if node_id is not None:
            result = run_command(f"{platform_paths.CMD_WPCTL} set-default {node_id}", timeout=5)
            if result['success']:
                restored = True
    return restored

def auto_set_defaults():
    devices_result = get_audio_devices()
    devices = devices_result.get('devices', [])
    if not devices:
        return

    sinks = [d for d in devices if d.get('role') != 'source']
    sources = [d for d in devices if d.get('role') == 'source']

    current_default = config.get_default_sink()

    if not current_default and sinks:
        preferred = None
        for d in sinks:
            name = d.get('name', '')
            if 'bluez' in name.lower():
                preferred = d
                break
        if not preferred:
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

def activate_bluez_sink(mac, set_default=True):
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
                    node_params = obj.get('info', {}).get('params', {})
                    if isinstance(node_params, dict):
                        ch_vols = extract_pw_vol_params(node_params).get('channelVolumes', [1.0])
                    else:
                        ch_vols = [1.0]
                    init_volumes = [1.0] * max(len(ch_vols), 2)
                    props_json = json.dumps({"mute": False, "channelVolumes": init_volumes})
                    run_command(
                        f"{platform_paths.CMD_PW_CLI} set-param {node_id} Props '{props_json}'",
                        timeout=3)
                    logger.info(f"蓝牙设备 {node_name} 音量已重置为 100%")
                    if set_default:
                        result = run_command(f"{platform_paths.CMD_WPCTL} set-default {node_id}", timeout=5)
                        if result['success']:
                            config.set_default_sink(node_name)
                    logger.info(f"蓝牙音频 sink 已激活: {node_name} (id={node_id}, 设为默认={set_default})")
                    return True
        if attempt < 2:
            time.sleep(2)
    logger.warning(f"蓝牙音频 sink 激活失败: {mac}")
    return False

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
