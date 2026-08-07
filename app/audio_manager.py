# 音频设备管理模块 基于 PipeWire/WirePlumber 提供扫描/详情/默认设备管理/音量静音声道控制/Profile 端口切换/播放测试及蓝牙 USB 激活 核心职责含设备分类(USB/蓝牙/HDMI/DP/蜂鸣器)/唤醒挂起检测/蜂鸣器防护(仅 snd_pcsp/降权防 fallback/禁设默认)/播放测试串行化防并发覆盖
import re
import os
import time
import logging
import threading
import shlex
from utils import (run_command, pw_dump, find_pw_node,
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
    # 根据节点名/友好名/属性判定音频设备类型 依次匹配 USB/蓝牙/蜂鸣器/HDMI/DP 未命中按角色(sink/source)回落到麦克风/线路输入/内置 返回类型字符串
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

    if 'pcspkr' in name_lower or 'pcsp' in name_lower:
        return 'beeper'

    if 'hdmi' in name_lower or 'hdmi' in friendly_upper or 'display audio' in name_lower:
        return 'hdmi'
    card_name = get_prop_with_fallback(props, device_props, 'alsa.card_name', '').lower()
    if 'hdmi' in card_name:
        return 'hdmi'
    friendly_lower = (friendly_name or '').lower()
    if ('displayport' in name_lower or 'display-port' in name_lower
            or 'displayport' in card_name or 'display-port' in card_name
            or 'displayport' in friendly_lower or 'display-port' in friendly_lower):
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
# 播放测试串行锁：防止连点导致多个测试并发互相覆盖保存的音量/默认设备
_play_test_lock = threading.Lock()

def _has_connected_bluetooth():
    # 检测当前是否存在已连接的蓝牙设备
    try:
        import bluetooth_manager as _bt_mod
        return bool(_bt_mod.check_bluetooth_connections())
    except Exception:
        return False

def _check_pw_running_only():
    # 检查 PipeWire 是否可用(运行且 pw-dump 有数据) 返回 bool
    if not check_pipewire_running():
        return False
    wp_was_running = check_wireplumber_running()
    if not wp_was_running:
        time.sleep(1)
    pw_data = pw_dump()
    return bool(pw_data)

def _get_connected_hdmi_info():
    # 读取 sysfs DRM 子系统获取已连接 HDMI 接口及显示器名 解析 /sys/class/drm 下各 HDMI 卡 status 与 EDID 提取显示器名辅助识别 HDMI 音频设备 返回 list[dict](drm_entry/monitor_name)
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
    # 根据设备名查找 PipeWire 节点 ID
    pw_data = pw_dump()
    node = find_pw_node(pw_data, name=device_name)
    if node is None:
        node = find_pw_node(pw_data, property_filters={'node.description': device_name})
    return node.get('id') if node else None

def _get_wpctl_route_device_id(device_name):
    # 根据设备名查找其所属 WirePlumber Device 的 ID(用于 set-route/set-profile)
    pw_data = pw_dump()
    node = find_pw_node(pw_data, name=device_name)
    if node is None:
        node = find_pw_node(pw_data, property_filters={'node.description': device_name})
    if node is None:
        return None
    return node.get('info', {}).get('props', {}).get('device.id')

def _try_activate_profile(device_id, device_name):
    # 按设备类型预设的 Profile 优先级尝试激活 依据设备名(hdmi/pcsp/iec958)选目标 Profile 列表 先精确后模糊 无命中回退首个可用 返回 bool
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
        if not default_sink_name and not _is_pcspkr(wp_sink):
            default_sink_name = wp_sink
        if not default_source_name and not _is_pcspkr(wp_source):
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
    # 扫描所有音频 Source(输入设备)并返回结构化列表 pw_data 为 None 时内部重新获取 返回 list[dict]
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
    # 激活指定音频设备(选择合适 Profile) device_name 支持 alsa_output./alsa_card. 前缀 返回 dict(message/device) 空名抛 InvalidParamError 未找到抛 DeviceNotFoundError
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
    # 切换设备端口(route) 参数 device_name/route_name 返回 dict(message/route) 空参抛 InvalidParamError 无 Device ID 抛 DeviceNotFoundError 失败抛 CommandError
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
    # 切换设备 Profile 参数 device_name/profile_name(未匹配按索引尝试) 返回 dict(message/profile) 空参抛 InvalidParamError 无 Device ID 抛 DeviceNotFoundError 失败抛 CommandError
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
        # PipeWire 不可用时不返回过期配置缓存 直接返回空列表 待 PipeWire 恢复后重新请求即可拿到实时数据
        logger.warning("PipeWire 不可用，无法获取音频设备")
        return {'devices': [], 'default': '', 'default_source': '', 'cached': False}
    with _scan_lock:
        result = _scan_audio_devices()
        if not result.get('devices'):
            logger.info("pw-dump 返回空结果，无可用音频设备")
        # 默认设备是用户设置需持久化 设备列表是运行时数据不持久化 保存前过滤蜂鸣器避免 WirePlumber 临时 fallback 污染配置
        default_val = result.get('default', '')
        if _is_pcspkr(default_val):
            default_val = ''
        default_source_val = result.get('default_source', '')
        if _is_pcspkr(default_source_val):
            default_source_val = ''
        config.set_default_sink(default_val)
        config.set_default_source(default_source_val)
        return result

def scan_audio_devices():
    # 扫描音频设备并持久化默认设备配置(过滤蜂鸣器后保存) 返回 dict{devices/default/default_source}
    if not _check_pw_running_only():
        logger.warning("PipeWire 不可用，无法扫描音频设备")
        return {'devices': [], 'default': '', 'default_source': ''}
    with _scan_lock:
        result = _scan_audio_devices()
        # 保存前过滤蜂鸣器，避免 WirePlumber 临时 fallback 污染配置
        default_val = result.get('default', '')
        if _is_pcspkr(default_val):
            default_val = ''
        default_source_val = result.get('default_source', '')
        if _is_pcspkr(default_source_val):
            default_source_val = ''
        config.set_default_sink(default_val)
        config.set_default_source(default_source_val)
        return result

def get_audio_device_detail(device_name):
    # 获取单个音频设备完整详情(音量/声道/采样率/端口/Profile) 参数 device_name 返回 dict 未找到抛 DeviceNotFoundError
    pw_data = pw_dump()
    node = find_pw_node(pw_data, name=device_name)
    if not node:
        # 不再从配置文件缓存 fallback，设备未找到直接抛错
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
    # 将指定设备设为默认输出/输入 蜂鸣器禁止设默认 自动识别 sink/source 角色 优先 wpctl 失败回退 pw-cli 参数 device_name 返回 dict(message/device/role) 非法或蜂鸣器抛 InvalidParamError 失败抛 CommandError
    if not _SAFE_DEVICE_PATTERN.match(device_name):
        raise InvalidParamError('无效的设备名')

    # 蜂鸣器禁止设为默认输出（仅供播放测试，不作为默认音频设备）
    if _is_pcspkr(device_name):
        raise InvalidParamError('蜂鸣器设备不可设为默认输出')

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
    # 获取设备音量 device_name 为 None 时用默认 sink 返回 dict(volume/muted/device) 非法抛 InvalidParamError 无默认抛 DeviceNotFoundError 失败抛 CommandError
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
    # 从 pw-dump 读取设备的声道列表(含位置与音量)
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
    # 解析设备名 校验合法性 为空时回退默认 sink 非法抛 InvalidParamError 无默认抛 DeviceNotFoundError
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


def set_volume(device_name=None, volume=50):
    device_name = _resolve_device_name(device_name)
    volume = max(0, min(100, volume))

    vc_result = volume_controller.set_volume(device_name, volume)
    verified_vol = vc_result.get('volume', volume)

    channels = _get_channels_from_pw(device_name)
    for ch in channels:
        ch['volume'] = verified_vol
        ch['effective_volume'] = verified_vol
    return {'message': f'音量已设为 {verified_vol}%', 'verified_volume': verified_vol, 'channels': channels}

def set_mute(device_name=None, mute=True):
    device_name = _resolve_device_name(device_name)

    return volume_controller.set_mute(device_name, mute)

def set_channel_volume(device_name=None, channel_index=0, volume=50):
    device_name = _resolve_device_name(device_name)

    return volume_controller.set_channel_volume(device_name, channel_index, volume)

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

    vc_result = volume_controller.set_balance(device_name, balance)
    actual_balance = vc_result.get('balance', balance)
    channels = _get_channels_from_pw(device_name)
    return {'message': f'平衡已设为 {actual_balance}', 'balance': actual_balance, 'channels': channels}

# 蜂鸣器内核模块管理
def _ensure_pcspkr_module():
    # snd_pcsp 注册 pcsp 声卡用于蜂鸣器显示与播放测试 不加载 pcspkr(走 input/evdev SND_BELL 通路不经 PipeWire 会被 bell 事件触发致主板蜂鸣器长响) 仅加载 snd_pcsp 并配降权/拒设默认/运行时静音确保不被 fallback 选中
    run_command("modprobe snd_pcsp 2>/dev/null", timeout=3)




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

def _play_pcspkr(device_name=None, freq=1000):
    # 优先使用 beep 命令直接驱动蜂鸣器，失败则回退到 pw-play / speaker-test
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


def play_test_channel(device_name, position):
    if _is_pcspkr(device_name):
        return _play_pcspkr(device_name=device_name, freq=1000)

    # 串行化播放测试防连点并发 不读取/恢复/调整任何音量 用设备当前音量 音量仅在用户拖动或点击音量条时改变
    with _play_test_lock:
        saved_default = get_default_sink_name()
        if device_name:
            set_default_device(device_name)

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


def get_peak_levels():
    # 采集各音频节点的平均电平(近似峰值) 返回 list[dict(node_id/name/media_class/volume)] volume 为 0-100 整数
    pw_data = pw_dump()
    if not pw_data:
        return []
    peaks = []
    for obj in pw_data:
        if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
            continue
        info = obj.get('info', {})
        props = info.get('props', {})
        media_class = props.get('media.class', '')
        if media_class not in ('Audio/Playback', 'Audio/Record', 'Audio/Sink', 'Audio/Source'):
            continue
        props_param = extract_pw_vol_params(info.get('params', {}))
        ch_vols = props_param.get('channelVolumes', [])
        if not ch_vols:
            continue
        dev_name = props.get('node.name', '')
        valid = [volume_controller._raw_to_linear(dev_name, float(cv))
                 for cv in ch_vols if isinstance(cv, (int, float))]
        if not valid:
            continue
        avg_vol = sum(valid) / len(valid)
        peaks.append({
            'node_id': obj.get('id'),
            'name': dev_name,
            'media_class': media_class,
            'volume': min(round(avg_vol * 100), 100),
        })
    return peaks

def play_test_sound(device_name=None):
    if _is_pcspkr(device_name):
        return _play_pcspkr(device_name=device_name, freq=1000)

    # 串行化播放测试防连点并发 不读取/恢复/调整任何音量 用设备当前音量 音量仅在用户拖动或点击音量条时改变
    with _play_test_lock:
        saved_default = get_default_sink_name()
        if device_name:
            set_default_device(device_name)

        ch_count = _get_device_channel_count(device_name)
        play_env = _get_pw_env().copy()
        node_id = None
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
    if saved_sink and not _is_pcspkr(saved_sink):
        node_id = _get_wpctl_device_id(saved_sink)
        if node_id is not None:
            result = run_command(f"{platform_paths.CMD_WPCTL} set-default {node_id}", timeout=5)
            if result['success']:
                restored = True
    saved_source = config.get_default_source()
    if saved_source and not _is_pcspkr(saved_source):
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

    sinks = [d for d in devices if d.get('role') != 'source' and not _is_pcspkr(d.get('name', ''))]
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
