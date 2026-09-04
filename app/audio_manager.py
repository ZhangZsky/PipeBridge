# 音频设备管理模块 基于 PipeWire/WirePlumber 提供扫描/详情/默认设备管理/音量静音声道控制/Profile 端口切换/播放测试及蓝牙 USB 激活 核心职责含设备分类(USB/蓝牙/HDMI/DP)/唤醒挂起检测/播放测试串行化防并发覆盖
import re
import os
import time
import json
import logging
import threading
import shlex
import subprocess
import contextlib
from utils import (run_command, pw_dump, find_pw_node,
                   get_default_sink_name, get_default_source_name,
                   find_audio_sinks, find_audio_sources,
                   get_prop_with_fallback, find_device_props, parse_edid_monitor_name,
                   pw_dump_invalidate, _get_pw_env, extract_pw_vol_params,
                   iter_pw_devices, find_pw_device_by_id, find_pw_device_by_card_id,
                   get_device_enum_profiles, get_device_active_profile,
                   extract_pw_routes)
from audio_helpers import _extract_node_audio_info, volume_controller
import config
import platform_paths
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError
from system_manager import check_pipewire_running, check_wireplumber_running

_SAFE_DEVICE_PATTERN = re.compile(r'^[a-zA-Z0-9_.@:\[\]\/-]+$')

def _classify_audio_type(name, friendly_name='', props=None, device_props=None,
                         hdmi_monitor_names=None, role='sink'):
    # 根据节点名/友好名/属性判定音频设备类型 依次匹配 USB/蓝牙/HDMI/DP 未命中按角色(sink/source)回落到麦克风/线路输入/内置 返回类型字符串
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

_scan_lock = threading.Lock()
# 播放测试串行锁：防止连点导致多个测试并发互相覆盖保存的音量/默认设备
_play_test_lock = threading.Lock()
# 播放测试头/尾截断修复参数(秒):
# WARMUP 让 speaker-test 播放前设备节点从 suspend 唤醒并预建立 ALSA->PipeWire link, 消除开头被吞
# DRAIN 让 speaker-test 退出后 PipeWire/DMA 缓冲中的尾音排空再切换路由, 消除结尾被切
_PLAY_TEST_WARMUP_SEC = 0.35
_PLAY_TEST_DRAIN_SEC = 0.45
# 蓝牙 A2DP sink 从 suspend 唤醒 + Transport 重建耗时远超普通设备(常 1~3s),
# 固定 0.35s WARMUP 不够会导致开头几秒听不到。对蓝牙设备改用自适应预热:
# 先主动触发唤醒再轮询节点直到就绪(channelVolumes 非空)或达上限。
_PLAY_TEST_BT_WARMUP_MAX_SEC = 3.0
_PLAY_TEST_BT_DRAIN_SEC = 0.8
_PLAY_TEST_POLL_INTERVAL_SEC = 0.15

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

def _find_node_by_name_or_desc(pw_data, device_name):
    # 匹配链: node.name 精确 → node.description 精确 → node.nick 精确 →
    # 蓝牙 MAC 模糊(界面可能传 alias/description 而非 node.name)。
    if not device_name:
        return None
    node = find_pw_node(pw_data, name=device_name)
    if node is not None:
        return node
    node = find_pw_node(pw_data, property_filters={'node.description': device_name})
    if node is not None:
        return node
    node = find_pw_node(pw_data, property_filters={'node.nick': device_name})
    if node is not None:
        return node
    mac_underscore = device_name.replace(':', '_').upper()
    for obj in pw_data:
        if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
            continue
        node_name = obj.get('info', {}).get('props', {}).get('node.name', '')
        if not node_name:
            continue
        if mac_underscore and mac_underscore in node_name.upper():
            return obj
    return None

def _get_wpctl_device_id(device_name):
    # 根据设备名查找 PipeWire 节点 ID
    node = _find_node_by_name_or_desc(pw_dump(), device_name)
    return node.get('id') if node else None

def _get_wpctl_route_device_id(device_name):
    # 根据设备名查找其所属 WirePlumber Device 的 ID(用于 set-route/set-profile)
    node = _find_node_by_name_or_desc(pw_dump(), device_name)
    if node is None:
        return None
    return node.get('info', {}).get('props', {}).get('device.id')

def _try_activate_profile(device_id, device_name):
    # 按设备类型预设的 Profile 优先级尝试激活 依据设备名(hdmi/iec958)选目标 Profile 列表 先精确后模糊 无命中回退首个可用 返回 bool
    activated = False
    device_lower = device_name.lower()

    pw_data = pw_dump()
    available_profiles = []
    dev_obj = find_pw_device_by_id(pw_data, device_id)
    if dev_obj:
        for ep in get_device_enum_profiles(dev_obj):
            if ep.get('available') is False:
                continue
            available_profiles.append((ep.get('name', ''), ep.get('index', 0)))

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

    def _apply(target_index, target_desc):
        # 执行 set-profile 并记录日志,成功返回 True
        result = run_command(f"{platform_paths.CMD_WPCTL} set-profile {device_id} {target_index}", timeout=5)
        if result['success']:
            logger.info(f"已激活设备 {device_name} 的 profile: {target_desc} (index={target_index})")
            return True
        return False

    for target_name in target_profile_names:
        # 先精确匹配,再模糊(子串)匹配;命中即尝试激活
        for match_exact in (True, False):
            for avail_name, avail_index in available_profiles:
                if avail_name.lower() == 'off':
                    continue
                hit = (target_name == avail_name) if match_exact else \
                      (target_name in avail_name or avail_name in target_name)
                if hit:
                    activated = _apply(avail_index, avail_name)
                    break
            if activated:
                break
        if activated:
            break

    if not activated and available_profiles:
        # 无预设命中时回退首个可用(非 off)profile
        for avail_name, avail_index in available_profiles:
            if avail_name.lower() != 'off' and _apply(avail_index, avail_name):
                activated = True
                break

    return activated

def _scan_audio_devices():
    pw_data = pw_dump()
    sinks = find_audio_sinks(pw_data)
    default_sink_name = get_default_sink_name()
    default_source_name = get_default_source_name()
    # 注:不再用 _parse_wpctl_default() 兜底,它返回的是 description(友好名)而非 node.name,
    # 会污染匹配导致默认标签失效。get_default_*_name 内部已含 pw-metadata + wpctl-id 反查两级兜底。
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

    for obj in iter_pw_devices(pw_data):
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

def _resolve_route_index(device_id, route_name):
    # 将端口(route)名字解析为其在设备 EnumRoute 中的 index(int) 及适用的 device index 列表 未找到返回 (None, [])
    dev_obj = find_pw_device_by_id(pw_dump(), device_id)
    if not dev_obj:
        return None, []
    params = dev_obj.get('info', {}).get('params', {})
    enum_routes = params.get('EnumRoute', [])
    if isinstance(enum_routes, dict):
        enum_routes = [enum_routes]
    for er in enum_routes:
        if not isinstance(er, dict):
            continue
        if er.get('direction', '') != 'Output':
            continue
        if er.get('name', '') == route_name:
            idx = er.get('index')
            devices = er.get('devices', []) or []
            if isinstance(idx, int):
                return idx, devices
    return None, []

def set_route(device_name, route_name):
    # 切换设备端口(route) 参数 device_name/route_name 返回 dict(message/route) 空参抛 InvalidParamError 无 Device ID 抛 DeviceNotFoundError 失败抛 CommandError
    if not device_name or not route_name:
        raise InvalidParamError('设备名和端口名不能为空')

    route_device_id = _get_wpctl_route_device_id(device_name)
    if route_device_id is None:
        raise DeviceNotFoundError(f'未找到设备 {device_name} 的 WirePlumber 设备 ID')

    # wpctl set-route 需要 route 的数字 index,而非名字;直接传名字会导致底层
    # 报 "Property 'card.profile.device' not found"(无法把名字映射到当前 profile 的 device)
    route_index, _ = _resolve_route_index(route_device_id, route_name)
    if route_index is None:
        raise DeviceNotFoundError(
            f'设备 {device_name} 在当前 Profile 下无可用端口 {route_name},请先切换到匹配的 Profile')

    result = run_command(
        f"{platform_paths.CMD_WPCTL} set-route {route_device_id} {route_index}", timeout=5)
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
        dev_obj = find_pw_device_by_card_id(pw_data, card_id)
        if dev_obj:
            wp_device_id = dev_obj.get('id')

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
    node = find_pw_node(pw_data, name=device_name)
    if node:
        device_id = node.get('info', {}).get('props', {}).get('device.id')

    if device_id is not None:
        dev_obj = find_pw_device_by_id(pw_data, device_id)
    else:
        card_id = device_name.replace('alsa_output.', '').replace('alsa_card.', '')
        dev_obj = find_pw_device_by_card_id(pw_data, card_id)

    if dev_obj is None:
        raise DeviceNotFoundError(f'未找到设备 {device_name}')

    profiles = [
        {'name': p['name'], 'description': p['description'],
         'priority': p['priority'], 'index': p['index']}
        for p in get_device_enum_profiles(dev_obj)
    ]
    active_profile = get_device_active_profile(dev_obj)
    return {'profiles': profiles, 'active_profile': active_profile}

def get_audio_devices():
    if not _check_pw_running_only():
        # PipeWire 不可用时不返回过期配置缓存 直接返回空列表 待 PipeWire 恢复后重新请求即可拿到实时数据
        logger.warning("PipeWire 不可用，无法获取音频设备")
        return {'devices': [], 'default': '', 'default_source': '', 'cached': False}
    with _scan_lock:
        result = _scan_audio_devices()
        if not result.get('devices'):
            logger.info("pw-dump 返回空结果，无可用音频设备")
        # default/default_source 仅为运行时展示(系统当前实际默认),不再持久化。
        return result

def scan_audio_devices():
    # 扫描音频设备 返回 dict{devices/default/default_source}(default 仅运行时展示)
    if not _check_pw_running_only():
        logger.warning("PipeWire 不可用，无法扫描音频设备")
        return {'devices': [], 'default': '', 'default_source': ''}
    # 蜂鸣器拦截已在应用启动时一次性完成(app.py lifespan → block_pcspkr_via_override:
    # 卸载驱动 + 写 driver_override=none 物理拦截)。driver_override 是设备属性,进程存活
    # 期内持续生效,不随驱动增删或音频扫描丢失,故此处无需重复拦截。
    with _scan_lock:
        result = _scan_audio_devices()
        return result

def get_audio_device_detail(device_name):
    # 获取单个音频设备完整详情(音量/声道/采样率/端口/Profile) 参数 device_name 返回 dict 未找到抛 DeviceNotFoundError
    pw_data = pw_dump()
    node = find_pw_node(pw_data, name=device_name)
    if not node:
        # 设备未找到直接抛错，不做配置文件 fallback 缓存
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

def restore_device_volume(device_name):
    # 设备重连/重建时恢复其保存的音量。config 有该设备音量记忆且设备当前在线才恢复。
    # 返回 True 表示已恢复;无记忆/设备不在线/失败返回 False。
    if not device_name:
        return False
    saved = config.get_device_volume(device_name)
    if saved is None:
        return False
    try:
        node_id = _get_wpctl_device_id(device_name)
        if node_id is None:
            return False
        volume_controller.set_volume(device_name, saved)
        logger.info(f"已恢复设备音量: {device_name} -> {saved}%")
        return True
    except Exception:
        logger.exception(f"恢复设备音量失败: {device_name}")
        return False

def set_default_audio(device_name):
    # 运行时将指定音频设备设为当前系统默认(仅 wpctl set-default 即时生效,不写 config、不启动恢复)。
    # 成功返回 dict(message/device),失败抛 DeviceNotFoundError/CommandError。
    if not device_name or not _SAFE_DEVICE_PATTERN.match(device_name):
        raise InvalidParamError('无效的设备名')
    node_id = _get_wpctl_device_id(device_name)
    if node_id is None:
        raise DeviceNotFoundError(f'未找到设备: {device_name}')
    result = run_command(f"{platform_paths.CMD_WPCTL} set-default {node_id}", timeout=5)
    if not result or not result.get('success'):
        raise CommandError('设置默认设备失败')
    logger.info(f"已设为默认音频设备(运行时): {device_name} (id={node_id})")
    return {'message': f'已将 {device_name} 设为默认', 'device': device_name}

def get_volume(device_name=None):
    # 获取设备音量 device_name 为 None 时用默认 sink 返回 dict(volume/muted/device) 非法抛 InvalidParamError 无默认抛 DeviceNotFoundError 失败抛 CommandError
    device_name = _resolve_device_name(device_name)

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
    except Exception as e:
        logger.debug(f"提取声道信息失败: {e}")
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


def _warmup_before_play_test(device_name, node_id=None, play_env=None):
    # 播放测试前预热建链, 消除开头被吞。
    # 关键认知: 被动 sleep/轮询无法让节点提前唤醒 —— PipeWire 节点只有在有音频流连入时才会从
    #           suspend 唤醒并(对蓝牙)建立 A2DP Transport。因此正式 speaker-test 一启动就发声时,
    #           链路(尤其蓝牙 A2DP Transport, 建立需 1~3s)尚未就绪, 开头采样被丢弃 -> 头部截断。
    # 正确做法: 预热阶段主动播放一段静音流(pw-play /dev/zero 绑定到目标节点)强制建链并唤醒设备,
    #           轮询节点就绪(channelVolumes 非空)后停掉静音流, 主流程随即播正式音 -> 开头不再被吞。
    # 普通设备唤醒快, 固定短等待即可; 蓝牙走主动静音预热 + 自适应轮询。
    if not device_name:
        time.sleep(_PLAY_TEST_WARMUP_SEC)
        return
    is_bt = 'bluez' in device_name.lower()
    if not is_bt:
        time.sleep(_PLAY_TEST_WARMUP_SEC)
        return

    # 蓝牙: 后台启动静音预热流强制建链唤醒。pw-play 播 /dev/zero(raw s16 双声道)产生静音,
    # 绑定 PIPEWIRE_NODE 到目标节点, 触发 A2DP Transport 建立。
    warmup_proc = None
    try:
        warmup_env = (play_env or _get_pw_env()).copy()
        if node_id is not None:
            warmup_env['PIPEWIRE_NODE'] = str(node_id)
        warmup_proc = subprocess.Popen(
            [platform_paths.CMD_PW_PLAY,
             '--format', 's16', '--rate', '48000', '--channels', '2',
             '/dev/zero'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=warmup_env,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug(f"启动静音预热流失败(退回被动等待): {e}")
        warmup_proc = None

    deadline = time.time() + _PLAY_TEST_BT_WARMUP_MAX_SEC
    # 先给一个基础等待, 让静音流开始建链
    time.sleep(_PLAY_TEST_WARMUP_SEC)
    ready = False
    while time.time() < deadline:
        if not _is_device_suspended(device_name):
            logger.debug(f"蓝牙设备 {device_name} 已就绪(静音预热建链完成)")
            # 就绪后再稍等让 A2DP Transport 稳定
            time.sleep(_PLAY_TEST_POLL_INTERVAL_SEC)
            ready = True
            break
        time.sleep(_PLAY_TEST_POLL_INTERVAL_SEC)
    if not ready:
        logger.debug(f"蓝牙设备 {device_name} 预热达上限 {_PLAY_TEST_BT_WARMUP_MAX_SEC}s 仍未就绪, 继续播放")

    # 停掉静音预热流, 让位给正式测试音。此时链路已热, A2DP Transport 已建立, 主流程立即播不被吞。
    if warmup_proc is not None:
        try:
            warmup_proc.terminate()
            try:
                warmup_proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                warmup_proc.kill()
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug(f"停止静音预热流失败: {e}")


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


@contextlib.contextmanager
def _play_test_targeting(device_name):
    # 播放测试的公共定向模板(play_test_channel / play_test_sound 共用):
    # 定向机制(对齐历史已验证做法): speaker-test 忽略 PIPEWIRE_NODE, 声音恒落默认 sink。故:
    #   临时把目标设备设为默认 -> yield 出 play_env 供调用方执行 speaker-test -> finally 排空缓冲并恢复原默认。
    # 既定向到被点击设备, 又在测试结束后还原用户默认设备(不影响默认)。play_env 已按目标绑定 PIPEWIRE_NODE。
    play_env = _get_pw_env().copy()
    node_id = None
    # 记录原默认 sink, 测试结束后恢复, 保证"不影响默认设备"
    saved_default = get_default_sink_name()
    if device_name:
        node_id = _get_wpctl_device_id(device_name)
        if node_id is not None:
            play_env['PIPEWIRE_NODE'] = str(node_id)
        # 临时把目标设备设为默认, 让 speaker-test 的声音落到被点击设备上
        if saved_default != device_name:
            try:
                set_default_audio(device_name)
            except Exception as e:
                logger.debug(f"临时切换默认设备失败(继续播放): {e}")
    # 头部截断修复: 切换默认设备/绑定 PIPEWIRE_NODE 后设备节点可能仍处 suspend, 建链需时间,
    # 蓝牙 A2DP 唤醒较慢, 用主动静音预热流建链唤醒后再播, 避免开头(尤其蓝牙前几秒)被吞
    _warmup_before_play_test(device_name, node_id, play_env)
    try:
        yield play_env
    finally:
        # 尾部截断修复: 播完立即退出会销毁 stream 丢弃仍在 PipeWire/DMA 缓冲中的尾音,
        # 恢复默认设备(切换路由)前等待缓冲排空,避免尾部被切; 蓝牙缓冲更深, 排空时间更长
        _drain = _PLAY_TEST_BT_DRAIN_SEC if (device_name and 'bluez' in device_name.lower()) else _PLAY_TEST_DRAIN_SEC
        time.sleep(_drain)
        # 恢复原默认设备, 保证测试"不影响默认"
        if device_name and saved_default and saved_default != device_name:
            try:
                set_default_audio(saved_default)
            except Exception as e:
                logger.debug(f"恢复默认设备失败: {e}")


def play_test_channel(device_name, position):
    # 串行化播放测试防连点并发 不读取/恢复/调整任何音量 用设备当前音量 音量仅在用户拖动或点击音量条时改变
    # 定向到被点击设备并精确到声道位置(speaker-test -s <声道号>), 结束后还原用户默认设备(不影响默认)。
    with _play_test_lock:
        pos_upper = position.upper()
        label = _POS_LABEL.get(pos_upper, pos_upper)
        speaker_num = _POS_TO_SPEAKER_NUM.get(pos_upper)

        if not speaker_num:
            raise InvalidParamError(f'未知声道位置: {position}')

        ch_count = _get_device_channel_count(device_name)
        if ch_count < 1:
            ch_count = 2

        logger.info(f"播放测试音: {device_name} 声道={label} ch={ch_count} speaker_num={speaker_num}")
        with _play_test_targeting(device_name) as play_env:
            r = run_command(f"{platform_paths.CMD_SPEAKER_TEST} -c {ch_count} -t wav -l 1 -s {speaker_num} 2>/dev/null", timeout=10, env=play_env)
            logger.debug(f"speaker-test(wav) 结果: success={r['success']}, returncode={r.get('returncode')}, stdout={r.get('stdout','')[:200]}, stderr={r.get('stderr','')[:200]}")
            if not (r['success'] or 'Time' in r.get('stdout', '')):
                r = run_command(f"{platform_paths.CMD_SPEAKER_TEST} -c {ch_count} -t sine -f 1000 -l 1 -s {speaker_num} 2>/dev/null", timeout=10, env=play_env)
            logger.debug(f"speaker-test(sine) 结果: success={r['success']}, returncode={r.get('returncode')}, stdout={r.get('stdout','')[:200]}, stderr={r.get('stderr','')[:200]}")

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
    except Exception as e:
        logger.debug(f"获取设备声道数失败: {e}")
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
    # 串行化播放测试防连点并发 不读取/恢复/调整任何音量 用设备当前音量 音量仅在用户拖动或点击音量条时改变
    # 定向到被点击设备后播放整体测试音, 结束后还原用户默认设备(用户无感知)。
    with _play_test_lock:
        ch_count = _get_device_channel_count(device_name)
        with _play_test_targeting(device_name) as play_env:
            r = run_command(f"{platform_paths.CMD_SPEAKER_TEST} -c {ch_count} -t wav -l 1 2>/dev/null", timeout=15, env=play_env)
            if not (r['success'] or 'Time' in r.get('stdout', '')):
                r = run_command(f"{platform_paths.CMD_SPEAKER_TEST} -c {ch_count} -t sine -f 1000 -l 1 2>/dev/null", timeout=15, env=play_env)

    if r['success'] or 'Time' in r.get('stdout', ''):
        return {'message': '测试音播放完成', 'method': 'speaker-test'}
    raise CommandError(f'在设备 {device_name or "默认设备"} 上播放测试音失败')

def _get_device_active_profile(device_id):
    # 读取指定 WirePlumber Device 当前激活的 profile 名(委托 utils.get_device_active_profile)
    dev_obj = find_pw_device_by_id(pw_dump(), device_id)
    return get_device_active_profile(dev_obj) if dev_obj else ''

def _switch_bluez_to_a2dp(device_id, node_name):
    # 将蓝牙 card 的 profile 切到 A2DP 以启用 AVRCP 控制通道(HFP 下无 AVRCP，音箱按键无处转发)
    # 枚举该 Device 的 EnumProfile，优先匹配含 a2dp-sink/a2dp 的可用 profile，避开 headset-head-unit/handsfree
    dev_obj = find_pw_device_by_id(pw_dump(), device_id)

    # 先检查当前 active profile：若设备已在 A2DP 上则直接返回，跳过 set-profile。
    # 无条件 set-profile 会导致 A2DP Transport 断开重建，音箱播放断开/连接提示音，
    # 且连接质量较差的设备可能触发反复断连循环(尤其多设备场景)。
    current_active = get_device_active_profile(dev_obj) if dev_obj else ''
    if current_active and current_active.lower().startswith('a2dp'):
        logger.debug(f"蓝牙设备 {node_name} 当前已在 A2DP profile({current_active})，跳过切换")
        return True

    available_profiles = []
    if dev_obj:
        for ep in get_device_enum_profiles(dev_obj):
            if ep.get('available') is False:
                continue
            available_profiles.append((ep.get('name', ''), ep.get('index', 0)))

    logger.info(f"蓝牙设备 {node_name} (device_id={device_id}) 可用 profiles: {available_profiles}")

    # 优先级：a2dp-sink 精确/前缀 > 含 a2dp(排除 headset/handsfree/hfp/hsp/head-unit)
    target = None
    for avail_name, avail_index in available_profiles:
        low = avail_name.lower()
        if low == 'a2dp-sink' or low.startswith('a2dp-sink') or low.startswith('a2dp_sink'):
            target = (avail_name, avail_index)
            break
    if target is None:
        for avail_name, avail_index in available_profiles:
            low = avail_name.lower()
            if 'a2dp' in low and not any(k in low for k in ('headset', 'handsfree', 'hfp', 'hsp', 'head-unit')):
                target = (avail_name, avail_index)
                break

    if target is None:
        logger.warning(f"蓝牙设备 {node_name} 未找到可用 A2DP profile，跳过切换(可用: {available_profiles})")
        return False

    target_name, target_index = target

    # wpctl set-profile 报成功但 WirePlumber 自动策略可能在 Transport 建立时又把设备拉回 HFP，
    # 因此切换后必须重新读取 active profile 校验是否真的生效，未生效则重试(最多 3 次)。
    for attempt in range(3):
        result = run_command(f"{platform_paths.CMD_WPCTL} set-profile {device_id} {target_index}", timeout=5)
        if not result['success']:
            logger.warning(f"蓝牙设备 {node_name} set-profile 命令失败(第{attempt+1}次): {result.get('stderr', '')}")
            time.sleep(1)
            continue
        # 等待 A2DP Transport 重建后校验 active profile
        time.sleep(2)
        active = _get_device_active_profile(device_id)
        if active and active.lower().startswith('a2dp'):
            logger.info(f"已将蓝牙设备 {node_name} 切换到 A2DP profile: {target_name} (active={active}, index={target_index})")
            return True
        logger.warning(f"蓝牙设备 {node_name} set-profile 后 active profile 仍为 '{active}'(期望 a2dp)，第{attempt+1}次重试")

    logger.error(f"蓝牙设备 {node_name} 多次切换后仍未运行在 A2DP，AVRCP 控制通道可能无法建立，音箱按键将无效")
    return False

def _wait_bluez_node_stereo(normalized_mac, mac, max_wait=8):
    # A2DP profile 切换触发 Transport 重建，旧 Node(HFP/HSP 单声道)被销毁后
    # 新 Node(A2DP 立体声)需要时间重建。此函数轮询 pw_dump 直到该 MAC 的
    # Audio/Sink Node 的 EnumFormat.channels >= 2，或超时放弃。
    # 目的：确保前端读到的声道数是 A2DP 的 2(立体声)而非 HFP/HSP 的 1(单声道)。
    for _ in range(max_wait):
        time.sleep(1)
        pw_data = pw_dump()
        if not pw_data:
            continue
        for obj in pw_data:
            if not isinstance(obj, dict):
                continue
            if obj.get('type') != 'PipeWire:Interface:Node':
                continue
            props = obj.get('info', {}).get('props', {})
            if props.get('media.class', '') not in ('Audio/Sink', 'Audio/Sink/Virtual'):
                continue
            node_name = props.get('node.name', '')
            if normalized_mac not in node_name and mac.upper() not in node_name:
                continue
            # 读 Node EnumFormat 的 channels
            audio_info = _extract_node_audio_info(obj, pw_data)
            ch_count = audio_info.get('channel_count', 0)
            if ch_count >= 2:
                logger.info(f"蓝牙设备 {node_name} A2DP Node 已就绪(channels={ch_count})")
                return True
            logger.debug(f"蓝牙设备 {node_name} 等待 A2DP Node 就绪(当前 channels={ch_count})")
    logger.warning(f"蓝牙设备 {mac} 等待 A2DP 立体声 Node 超时({max_wait}s)，声道信息可能暂不准确")
    return False

def activate_bluez_sink(mac):
    # 连接成功后确保蓝牙 sink 运行在 A2DP profile(启用 AVRCP 控制通道)。
    # 不设默认设备：默认设备完全由用户手动掌控(参见默认设备手动策略)。
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
                    # 把蓝牙 card 切到 A2DP 以启用 AVRCP(否则 HFP 下音箱按键无 AVRCP 控制通道)
                    # _switch_bluez_to_a2dp 内部已带"已在 A2DP 则跳过"守卫，避免无谓 Transport 重建
                    device_id = props.get('device.id')
                    if device_id is not None:
                        switched = _switch_bluez_to_a2dp(device_id, node_name)
                        if not switched:
                            # headset-roles 配置未生效：设备只有 HFP/HSP profile 无 A2DP。
                            # 尝试重新部署 WirePlumber 配置并重启，使 headset-roles=[] 生效。
                            logger.warning(f"蓝牙设备 {node_name} 无 A2DP profile，尝试重新部署 WirePlumber 配置...")
                            try:
                                from system_manager import ensure_wireplumber_bluez_config
                                ensure_wireplumber_bluez_config()
                            except Exception as e:
                                logger.error(f"重新部署 WirePlumber 配置失败: {e}")
                            # 部署后重试：等待 WirePlumber 重启加载新配置
                            time.sleep(4)
                            continue  # 跳到外层 for attempt 重试
                    # A2DP profile 切换会触发 Transport 重建：旧 Node(HFP/HSP, channels=1)被销毁，
                    # 新 Node(A2DP, channels=2)被创建。若不等待新 Node 就绪，前端 pw_dump
                    # 可能读到旧 HFP Node 的 EnumFormat(channel_count=1)，导致设备被错误
                    # 识别为单声道——多设备场景下控制器资源紧张时尤为常见。
                    _wait_bluez_node_stereo(normalized_mac, mac)
                    logger.info(f"蓝牙音频 sink 已激活(A2DP): {node_name} (id={node_id})")
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


# ============ 多设备同时播放(combine-sink 聚合) ============
# 用 libpipewire-module-combine-stream 创建一个虚拟聚合 sink,把音频同时复制到多个已连接音箱。
# 设计要点:
# - 虚拟 sink 固定 node.name = _COMBINE_SINK_NAME,幂等:重复创建先销毁旧的再建新的。
# - slave 目标用各音箱的 node.name(蓝牙为 bluez_output.*),通过 stream.rules match 精确匹配。
# - 创建成功后把虚拟 sink 设为默认输出,音频即同放到所有选中音箱。
# - module 由 pw-cli -m load-module 加载;-m 保持 pw-cli 常驻持有 module,后台运行,销毁靠 kill 该进程。
_COMBINE_SINK_NAME = 'pipebridge_combine'
_COMBINE_SINK_DESC = 'PipeBridge 多设备同时播放'
# 记录当前 combine-stream 后台 pw-cli 进程(持有 module),None 表示未启用
_combine_proc = None
_combine_lock = threading.Lock()
# 记录当前参与合并的成员 node.name 列表(用于状态查询)
_combine_members = []


def _resolve_sink_node_names(device_names):
    # 校验并把入参设备名解析为存在的 Audio/Sink node.name 列表;过滤掉不存在或非 sink 的项
    pw_data = pw_dump()
    valid = []
    for dn in device_names:
        if not isinstance(dn, str) or not _SAFE_DEVICE_PATTERN.match(dn):
            raise InvalidParamError(f'无效的设备名: {dn}')
        node = find_pw_node(pw_data, name=dn)
        if not node:
            continue
        media_class = node.get('info', {}).get('props', {}).get('media.class', '')
        if 'Sink' in media_class:
            valid.append(dn)
    return valid


def _kill_combine_proc():
    # 结束持有 combine module 的后台 pw-cli 进程,module 随进程退出而卸载
    global _combine_proc, _combine_members
    proc = _combine_proc
    if proc is not None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        except Exception as e:
            logger.debug(f"结束 combine 进程失败: {e}")
    _combine_proc = None
    _combine_members = []


def create_combine_sink(device_names, set_default=True):
    # 把多个已连接音箱合并为一个虚拟聚合 sink 实现同时播放。device_names: list[str] 各设备 node.name。
    # 幂等:已存在旧的 combine 先销毁再重建。返回 dict(message/sink/members)。
    if not isinstance(device_names, (list, tuple)) or len(device_names) < 2:
        raise InvalidParamError('至少需要选择 2 个设备才能合并同时播放')

    with _combine_lock:
        members = _resolve_sink_node_names(device_names)
        if len(members) < 2:
            raise DeviceNotFoundError('可用的输出设备不足 2 个(设备可能已断开)')

        # 先清理旧的聚合 sink,保证幂等
        _kill_combine_proc()

        # 构造 libpipewire-module-combine-stream 参数:
        # - combine.mode=sink 创建一个可播放的虚拟 sink
        # - stream.rules 用 node.name matches 精确匹配各成员 sink,create-stream 到每个目标
        match_rules = []
        for m in members:
            match_rules.append({
                'matches': [{'node.name': m}],
                'actions': {'create-stream': {}},
            })
        module_args = {
            'combine.mode': 'sink',
            'node.name': _COMBINE_SINK_NAME,
            'node.description': _COMBINE_SINK_DESC,
            'combine.latency-compensate': False,
            'combine.props': {
                'audio.position': ['FL', 'FR'],
            },
            'stream.props': {},
            'stream.rules': match_rules,
        }
        args_json = json.dumps(module_args, ensure_ascii=False)
        # pw-cli -m 保持常驻持有 module;放后台,句柄留存用于销毁
        cmd = [platform_paths.CMD_PW_CLI, '-m', 'load-module',
               'libpipewire-module-combine-stream', args_json]
        try:
            global _combine_proc, _combine_members
            _combine_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_get_pw_env(),
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.error(f"加载 combine-stream 模块失败: {e}")
            raise CommandError(f'创建聚合输出失败: {e}')

        # 轮询等待虚拟 sink 在 PipeWire 中注册
        registered = False
        for _ in range(10):
            time.sleep(0.5)
            if _combine_proc.poll() is not None:
                _combine_proc = None
                _combine_members = []
                raise CommandError('聚合输出模块加载后立即退出,请检查 PipeWire 版本是否支持 combine-stream')
            if find_pw_node(pw_dump(), name=_COMBINE_SINK_NAME):
                registered = True
                break
        if not registered:
            _kill_combine_proc()
            raise CommandError('聚合输出 sink 未能注册')

        _combine_members = members
        logger.info(f"聚合输出 sink 已创建: {_COMBINE_SINK_NAME} 成员={members}")

        if set_default:
            # 合并播放需将虚拟聚合 sink 设为当前默认输出(运行时行为),否则声音不走聚合设备。
            # 仅 wpctl 运行时切换,不做 config 持久化。
            node_id = _get_wpctl_device_id(_COMBINE_SINK_NAME)
            if node_id is not None:
                run_command(f"{platform_paths.CMD_WPCTL} set-default {node_id}", timeout=5)

        return {
            'message': f'已合并 {len(members)} 个设备同时播放',
            'sink': _COMBINE_SINK_NAME,
            'members': members,
        }


def destroy_combine_sink():
    # 销毁聚合输出 sink,恢复单设备输出。返回 dict(message)。
    with _combine_lock:
        active = _combine_proc is not None
        _kill_combine_proc()
        if active:
            logger.info("聚合输出 sink 已销毁")
            return {'message': '已关闭多设备同时播放'}
        return {'message': '当前未启用多设备同时播放'}


def get_combine_sink_status():
    # 查询聚合输出当前状态。返回 dict(enabled/sink/members)。
    with _combine_lock:
        enabled = _combine_proc is not None and _combine_proc.poll() is None
        if not enabled:
            if _combine_proc is not None and _combine_proc.poll() is not None:
                _kill_combine_proc()
            return {'enabled': False, 'sink': _COMBINE_SINK_NAME, 'members': []}
        return {'enabled': True, 'sink': _COMBINE_SINK_NAME, 'members': list(_combine_members)}


# ============================================================================
# 按应用/播放流路由到指定音箱(per-stream routing)
# ----------------------------------------------------------------------------
# 本机每个播放程序在 PipeWire 里是一个 media.class=Stream/Output/Audio 节点。
# 通过 pw-metadata 给该流节点写 target.object=目标 sink 的 node.name,即可让
# 该流单独走指定音箱,绕开默认 sink(与默认 sink/combine-sink 互不影响)。
# 流不播放时对应节点不存在,故列表是动态的,需前端轮询/SSE 刷新。
# ============================================================================

def list_playback_streams():
    # 枚举当前所有播放流(Stream/Output/Audio),返回 list[dict]。
    # 每项含 id / name(应用名) / target(当前钉住的目标 sink node.name,空表示跟随默认)。
    pw_data = pw_dump()
    streams = []
    # 建 node_id -> node.name 映射,用于把 target.object 的数字 id 反解成名字
    id_to_name = {}
    for obj in pw_data:
        if isinstance(obj, dict) and obj.get('type') == 'PipeWire:Interface:Node':
            props = obj.get('info', {}).get('props', {})
            nm = props.get('node.name')
            if nm:
                id_to_name[obj.get('id')] = nm
    for obj in pw_data:
        if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
            continue
        props = obj.get('info', {}).get('props', {})
        if props.get('media.class') != 'Stream/Output/Audio':
            continue
        # 过滤掉 combine-stream 自身派生的内部流,避免误显示
        node_name = props.get('node.name', '') or ''
        if node_name == _COMBINE_SINK_NAME or node_name.startswith(_COMBINE_SINK_NAME):
            continue
        app_name = (props.get('application.name')
                    or props.get('media.name')
                    or props.get('node.description')
                    or node_name
                    or '未知应用')
        target = props.get('target.object', '')
        # target.object 可能是数字 node id,反解为名字便于前端匹配
        if target and str(target).isdigit():
            target = id_to_name.get(int(target), target)
        streams.append({
            'id': obj.get('id'),
            'name': app_name,
            'node_name': node_name,
            'target': target or '',
        })
    return streams


def route_stream_to_sink(stream_id, sink_name):
    # 把指定播放流(stream_id)钉到指定音箱(sink_name)。
    # sink_name 为空/None 表示清除路由,恢复跟随默认 sink。
    try:
        sid = int(stream_id)
    except (TypeError, ValueError):
        raise InvalidParamError("stream_id 无效")

    pw_data = pw_dump()
    # 校验流节点存在且确为播放流
    stream = find_pw_node(pw_data, node_id=sid)
    if stream is None:
        raise DeviceNotFoundError("找不到该播放流,可能已停止播放")
    if stream.get('info', {}).get('props', {}).get('media.class') != 'Stream/Output/Audio':
        raise InvalidParamError("目标节点不是播放流")

    env = _get_pw_env()

    if not sink_name:
        # 清除 target.object/target.node,恢复默认路由
        # run_command 内部 shell=True,命令须为字符串(sid 为 int 安全)
        run_command(f"pw-metadata {sid} target.object", timeout=5, env=env)
        run_command(f"pw-metadata {sid} target.node", timeout=5, env=env)
        logger.info(f"已清除流 {sid} 的路由,恢复默认输出")
        return {'message': '已恢复默认输出', 'stream_id': sid, 'target': ''}

    if not _SAFE_DEVICE_PATTERN.match(sink_name):
        raise InvalidParamError("音箱名包含非法字符")
    # 校验目标 sink 存在
    sink = find_pw_node(pw_data, name=sink_name)
    if sink is None:
        raise DeviceNotFoundError(f"找不到目标音箱: {sink_name}")

    # 用 pw-metadata 给该流写 target.object=sink node.name(字符串型)
    # shell=True 命令须为字符串,sink_name 已过 _SAFE_DEVICE_PATTERN,再用 shlex.quote 双保险
    result = run_command(
        f"pw-metadata {sid} target.object {shlex.quote(sink_name)} Spa:String",
        timeout=5, env=env)
    if not result.get('success'):
        raise CommandError(f"路由流失败: {result.get('stderr', '')}")
    logger.info(f"已将流 {sid} 路由到音箱 {sink_name}")
    return {'message': f'已输出到 {sink_name}', 'stream_id': sid, 'target': sink_name}
