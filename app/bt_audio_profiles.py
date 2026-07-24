import time
import re
import logging

import dbus

import platform_paths
from utils import run_command, pw_dump, find_pw_node
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError, MediaBridgeError
from bluetooth_manager import (
    BLUEZ_IFACE_DEVICE,
    BLUEZ_SERVICE,
    DBUS_PROP_IFACE,
    _DEVICE_TYPE_UUIDS,
    _find_device_path,
    _get_property,
    _get_properties,
    _get_managed_objects,
    _get_object,
    _mac_from_path,
    _extract_bt_uuid_short,
)

logger = logging.getLogger('MediaBridge')

BLUEZ_IFACE_CARD = 'org.bluez.Card1'


# 获取蓝牙设备支持的 UUID 列表
def _get_device_uuids(mac):
    device_path = _find_device_path(mac)
    if not device_path:
        return []
    try:
        uuids = _get_property(BLUEZ_IFACE_DEVICE, device_path, 'UUIDs')
        return [str(u).upper() for u in uuids] if uuids else []
    except dbus.exceptions.DBusException:
        return []


# 判断 UUID 列表中是否包含音频输入相关 UUID (HFP/HSP)
def _has_audio_input_uuid(uuids):
    for u in uuids:
        short = _extract_bt_uuid_short(u)
        if short in ('1108', '111E', '111F'):
            return True
    return False


# 通过 D-Bus 查找设备的 org.bluez.Card1 路径
def _find_bluez_card_path(mac):
    dev_name = 'dev_' + mac.replace(':', '_').upper()
    try:
        for path, ifaces in _get_managed_objects().items():
            if BLUEZ_IFACE_CARD in ifaces and path.endswith('/' + dev_name):
                return path
    except dbus.exceptions.DBusException as e:
        logger.debug(f"查找 Card1 路径失败: {e}")
    return None


# 在 PipeWire 数据中查找与 MAC 对应的 Device 对象
def _find_pw_device_for_mac(mac, pw_data=None):
    if pw_data is None:
        pw_data = pw_dump()
    mac_us = mac.replace(':', '_')
    # 优先通过 find_pw_node 的 device_name_contains 搜索
    obj = find_pw_node(pw_data, device_name_contains=mac_us,
                       object_type='PipeWire:Interface:Device')
    if obj:
        return obj
    # 回退：检查 device.nick 和 api.bluez5.address 等额外字段
    for obj in pw_data:
        if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Device':
            continue
        props = obj.get('info', {}).get('props', {})
        dev_nick = props.get('device.nick', '')
        bt_addr = str(props.get('api.bluez5.address', ''))
        if mac_us.lower() in dev_nick.lower() or mac.upper() in bt_addr.upper():
            return obj
    return None


# 从 PipeWire Device 对象提取 EnumProfile 信息
def _get_pw_device_profiles(pw_device):
    profiles = []
    params = pw_device.get('info', {}).get('params', {})
    enum_profiles = params.get('EnumProfile', [])
    for ep in enum_profiles:
        if not isinstance(ep, dict):
            continue
        name = ep.get('name', '')
        desc = ep.get('description', name)
        priority = ep.get('priority', 0)
        available = ep.get('available', False)
        index = ep.get('index', -1)
        profiles.append({
            'name': name,
            'description': desc,
            'priority': priority,
            'available': available,
            'index': index,
        })
    return profiles


# 获取 PipeWire Device 当前激活的 profile 名称
def _get_pw_device_active_profile(pw_device):
    params = pw_device.get('info', {}).get('params', {})
    profiles = params.get('Profile', [])
    for p in profiles:
        if not isinstance(p, dict):
            continue
        # save=True 表示当前激活的 profile
        if p.get('save', False):
            return p.get('name', '')
    # 回退：从 props 获取
    props = pw_device.get('info', {}).get('props', {})
    return props.get('device.profile', '')


# 获取所有蓝牙音频输入源（HFP/HSP 麦克风、带麦音箱等）
def get_bluetooth_audio_sources():
    try:
        sources = []
        seen_macs = set()
        pw_data = pw_dump()

        # 1. 从 BlueZ D-Bus 查找已连接的音频设备
        try:
            for path, ifaces in _get_managed_objects().items():
                if BLUEZ_IFACE_DEVICE not in ifaces:
                    continue
                props = ifaces[BLUEZ_IFACE_DEVICE]
                if not props.get('Connected', False):
                    continue
                uuids = [str(u).upper() for u in (props.get('UUIDs') or [])]
                # 检查是否支持音频输入 (HFP/HSP) 或音频输出 (A2DP)
                has_audio = any(
                    _extract_bt_uuid_short(u) in _DEVICE_TYPE_UUIDS
                    for u in uuids
                )
                has_input = _has_audio_input_uuid(uuids)
                if not has_audio and not has_input:
                    continue

                mac = _mac_from_path(path)
                name = str(props.get('Alias', '') or props.get('Name', mac))
                seen_macs.add(mac)

                # 查找 PipeWire Source 节点
                mac_us = mac.replace(':', '_')
                source_name = ''
                source_node_id = None
                for obj in pw_data:
                    if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
                        continue
                    obj_props = obj.get('info', {}).get('props', {})
                    mc = obj_props.get('media.class', '')
                    if mc not in ('Audio/Source', 'Audio/Source/Virtual'):
                        continue
                    node_name = obj_props.get('node.name', '')
                    if 'bluez' in node_name.lower() and mac_us.lower() in node_name.lower():
                        source_name = node_name
                        source_node_id = obj.get('id')
                        break

                # 获取 profile 信息
                profiles = []
                active_profile = ''
                card_path = _find_bluez_card_path(mac)
                if card_path:
                    try:
                        card_props = _get_properties(BLUEZ_IFACE_CARD, card_path)
                        active_profile = str(card_props.get('ActiveProfile', ''))
                        for p in (card_props.get('Profiles', []) or []):
                            if isinstance(p, dbus.Dictionary):
                                profiles.append({
                                    'name': str(p.get('Name', '')),
                                    'description': str(p.get('Description', '')),
                                })
                            elif isinstance(p, str):
                                profiles.append({'name': p, 'description': p})
                    except dbus.exceptions.DBusException:
                        pass

                # 如果 Card1 没有 profile，尝试 PipeWire Device
                if not profiles:
                    pw_dev = _find_pw_device_for_mac(mac, pw_data)
                    if pw_dev:
                        profiles = _get_pw_device_profiles(pw_dev)
                        if not active_profile:
                            active_profile = _get_pw_device_active_profile(pw_dev)

                # 从 UUID 推断 profile
                if not profiles:
                    if has_input:
                        profiles.append({'name': 'hfp_hf', 'description': 'HFP Hands-Free'})
                        profiles.append({'name': 'hsp_hs', 'description': 'HSP Headset'})
                    profiles.append({'name': 'a2dp_sink', 'description': 'A2DP Sink'})

                sources.append({
                    'mac': mac,
                    'name': name,
                    'connected': bool(props.get('Connected', False)),
                    'source_name': source_name,
                    'source_node_id': source_node_id,
                    'profiles': profiles,
                    'active_profile': active_profile,
                })
        except dbus.exceptions.DBusException as e:
            logger.debug(f"D-Bus 查找蓝牙音频源失败: {e}")

        # 2. 从 PipeWire Source 节点中补充 bluez 源（可能 BlueZ 已断开但 PW 仍有残留）
        for obj in pw_data:
            if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
                continue
            obj_props = obj.get('info', {}).get('props', {})
            mc = obj_props.get('media.class', '')
            if mc not in ('Audio/Source', 'Audio/Source/Virtual'):
                continue
            node_name = obj_props.get('node.name', '')
            if 'bluez' not in node_name.lower():
                continue
            # 从节点名提取 MAC
            mac_match = re.search(r'([0-9A-Fa-f]{2}[_:]){5}[0-9A-Fa-f]{2}', node_name)
            if not mac_match:
                continue
            mac = mac_match.group(0).replace('_', ':').upper()
            if mac in seen_macs:
                continue
            seen_macs.add(mac)

            # 尝试获取设备名
            dev_name = obj_props.get('node.nick', '') or obj_props.get('device.description', '') or mac
            pw_dev = _find_pw_device_for_mac(mac, pw_data)
            profiles = []
            active_profile = ''
            if pw_dev:
                profiles = _get_pw_device_profiles(pw_dev)
                active_profile = _get_pw_device_active_profile(pw_dev)

            sources.append({
                'mac': mac,
                'name': dev_name,
                'connected': True,
                'source_name': node_name,
                'source_node_id': obj.get('id'),
                'profiles': profiles,
                'active_profile': active_profile,
            })

        return sources
    except Exception as e:
        logger.error(f"获取蓝牙音频源失败: {e}")
        raise CommandError(str(e)[:200])


# 切换蓝牙设备的音频 profile（如 A2DP ↔ HFP）
def switch_bluetooth_profile(mac, profile_name):
    mac = mac.upper()
    try:
        # 查找目标 profile 的 index（方式2和方式3共用）
        target_index = None
        pw_data = pw_dump()
        pw_dev = _find_pw_device_for_mac(mac, pw_data)

        if pw_dev:
            profiles = _get_pw_device_profiles(pw_dev)
            for p in profiles:
                if p['name'] == profile_name:
                    target_index = p.get('index')
                    break

        # 方式1: 通过 D-Bus Card1 接口切换
        card_path = _find_bluez_card_path(mac)
        if card_path:
            try:
                card_obj = _get_object(card_path)
                card_iface = dbus.Interface(card_obj, BLUEZ_IFACE_CARD)
                card_iface.SetProfile(dbus.String(profile_name))
                time.sleep(1)
                # 验证切换结果
                try:
                    new_profile = str(_get_property(BLUEZ_IFACE_CARD, card_path, 'ActiveProfile'))
                    if new_profile == profile_name:
                        return f'已切换到 {profile_name}'
                except dbus.exceptions.DBusException:
                    pass
                return f'已发送切换 {profile_name} 请求'
            except dbus.exceptions.DBusException as e:
                err = str(e)
                if 'NotSupported' in err or 'Not Available' in err:
                    logger.warning(f"Card1 不支持 profile {profile_name}: {err}")
                else:
                    logger.debug(f"Card1 切换 profile 失败: {e}")

        # 方式2: 通过 wpctl 切换
        if pw_dev and target_index is not None:
            dev_id = pw_dev.get('id')
            try:
                dev_id = int(dev_id)
            except (TypeError, ValueError):
                raise InvalidParamError('无效的设备ID或Profile索引')
            try:
                target_index = int(target_index)
            except (TypeError, ValueError):
                raise InvalidParamError('无效的设备ID或Profile索引')
            result = run_command(f"{platform_paths.CMD_WPCTL} set-profile {dev_id} {target_index} 2>/dev/null", timeout=5)
            if result['success']:
                time.sleep(1)
                return f'已通过 wpctl 切换到 {profile_name}'
            logger.debug(f"wpctl 切换失败: {result.get('stderr', '')}")

            # 方式3: 通过 pw-cli 切换
            safe_index = target_index
            result = run_command(
                f"{platform_paths.CMD_PW_CLI} set-param {dev_id} Profile '{{ \"index\": {safe_index}, \"save\": true }}' 2>/dev/null",
                timeout=5
            )
            if result['success']:
                time.sleep(1)
                return f'已通过 pw-cli 切换到 {profile_name}'
            logger.debug(f"pw-cli 切换失败: {result.get('stderr', '')}")

        if target_index is None:
            raise InvalidParamError(f'未找到 profile: {profile_name}')

        raise CommandError(f'无法切换设备 {mac} 的 profile，未找到 Card1 或 PipeWire Device')
    except MediaBridgeError:
        raise
    except Exception as e:
        logger.error(f"切换蓝牙 profile 失败: {e}")
        raise CommandError(str(e)[:200])


# 获取蓝牙设备的可用音频 profile 列表
def get_bluetooth_audio_profiles(mac):
    mac = mac.upper()
    try:
        profiles = []
        active_profile = ''

        # 1. 通过 D-Bus Card1 接口获取
        card_path = _find_bluez_card_path(mac)
        if card_path:
            try:
                card_props = _get_properties(BLUEZ_IFACE_CARD, card_path)
                active_profile = str(card_props.get('ActiveProfile', ''))
                for p in (card_props.get('Profiles', []) or []):
                    if isinstance(p, dbus.Dictionary):
                        profiles.append({
                            'name': str(p.get('Name', '')),
                            'description': str(p.get('Description', '')),
                            'available': True,
                        })
                    elif isinstance(p, str):
                        profiles.append({'name': p, 'description': p, 'available': True})
            except dbus.exceptions.DBusException as e:
                logger.debug(f"Card1 获取 profile 失败: {e}")

        # 2. 通过 PipeWire Device EnumProfile 获取
        pw_data = pw_dump()
        pw_dev = _find_pw_device_for_mac(mac, pw_data)
        if pw_dev:
            pw_profiles = _get_pw_device_profiles(pw_dev)
            if not active_profile:
                active_profile = _get_pw_device_active_profile(pw_dev)
            # 合并：以 PW 的可用性信息为准
            existing_names = {p['name'] for p in profiles}
            for pp in pw_profiles:
                existing = next((p for p in profiles if p['name'] == pp['name']), None)
                if existing:
                    existing['available'] = pp.get('available', True)
                    if pp.get('description') and pp['description'] != pp['name']:
                        existing['description'] = pp['description']
                elif pp['name'] not in existing_names:
                    profiles.append({
                        'name': pp['name'],
                        'description': pp.get('description', pp['name']),
                        'available': pp.get('available', True),
                    })
                    existing_names.add(pp['name'])

        # 3. 根据 UUID 推断
        if not profiles:
            uuids = _get_device_uuids(mac)
            has_hfp = any(_extract_bt_uuid_short(u) in ('111E', '111F') for u in uuids)
            has_hsp = any(_extract_bt_uuid_short(u) == '1108' for u in uuids)
            has_a2dp = any(_extract_bt_uuid_short(u) in ('110B', '110A', '110D') for u in uuids)
            if has_hfp:
                profiles.append({'name': 'hfp_hf', 'description': 'HFP Hands-Free (含麦克风)', 'available': True})
                profiles.append({'name': 'hfp_ag', 'description': 'HFP Audio Gateway', 'available': True})
            if has_hsp:
                profiles.append({'name': 'hsp_hs', 'description': 'HSP Headset (含麦克风)', 'available': True})
                profiles.append({'name': 'hsp_ag', 'description': 'HSP Audio Gateway', 'available': True})
            if has_a2dp:
                profiles.append({'name': 'a2dp_sink', 'description': 'A2DP Sink (高质量播放)', 'available': True})
                profiles.append({'name': 'a2dp_source', 'description': 'A2DP Source', 'available': True})

        # 标记当前活跃项（保留 off 选项，让用户看到当前实际状态）
        for p in profiles:
            p['active'] = (p['name'] == active_profile)

        return profiles
    except Exception as e:
        logger.error(f"获取蓝牙音频 profile 失败: {e}")
        raise CommandError(str(e)[:200])


# 启用蓝牙麦克风：切换到 HFP/HSP profile 并等待 Source 节点出现
def enable_bluetooth_microphone(mac):
    mac = mac.upper()
    # 检查设备是否已连接
    device_path = _find_device_path(mac)
    if not device_path:
        raise DeviceNotFoundError(f'设备 {mac} 未找到，请先连接')
    try:
        connected = _get_property(BLUEZ_IFACE_DEVICE, device_path, 'Connected')
        if not connected:
            raise DeviceNotFoundError(f'设备 {mac} 未连接，请先连接')
    except dbus.exceptions.DBusException as e:
        raise DeviceNotFoundError(f'设备 {mac} 状态未知，请重新连接')

    # 检查当前 profile
    card_path = _find_bluez_card_path(mac)
    current_profile = ''
    if card_path:
        try:
            current_profile = str(_get_property(BLUEZ_IFACE_CARD, card_path, 'ActiveProfile'))
        except dbus.exceptions.DBusException:
            pass

    # 如果已经是 HFP/HSP，直接查找 Source 节点
    need_switch = True
    if current_profile and any(kw in current_profile.lower() for kw in ('hfp', 'hsp')):
        need_switch = False
    else:
        # 获取可用 profile，优先 HFP，其次 HSP
        available_profiles = get_bluetooth_audio_profiles(mac)
        target_profile = None
        for pref in ('hfp_hf', 'hfp_ag', 'hsp_hs', 'hsp_ag'):
            match = next((p for p in available_profiles if p['name'] == pref and p.get('available', True)), None)
            if match:
                target_profile = pref
                break

        if not target_profile:
            raise InvalidParamError('设备不支持 HFP/HSP profile，无法使用麦克风')

        # 切换 profile
        try:
            switch_bluetooth_profile(mac, target_profile)
        except MediaBridgeError as e:
            raise CommandError(f'切换到 {target_profile} 失败: {e.message}')

    # 等待 PipeWire Source 节点出现（最多 8 秒）
    # 仅在切换了 profile 时等待，已处于正确 profile 时直接查找
    if need_switch:
        mac_us = mac.replace(':', '_')
        source_info = None
        for _ in range(16):
            time.sleep(0.5)
            pw_data = pw_dump()
            for obj in pw_data:
                if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
                    continue
                obj_props = obj.get('info', {}).get('props', {})
                mc = obj_props.get('media.class', '')
                if mc not in ('Audio/Source', 'Audio/Source/Virtual'):
                    continue
                node_name = obj_props.get('node.name', '')
                if 'bluez' in node_name.lower() and mac_us.lower() in node_name.lower():
                    source_info = {
                        'source_name': node_name,
                        'source_node_id': obj.get('id'),
                    }
                    break
            if source_info:
                break
    else:
        # 已在正确 profile，直接查找 Source 节点
        mac_us = mac.replace(':', '_')
        source_info = None
        pw_data = pw_dump()
        for obj in pw_data:
            if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
                continue
            obj_props = obj.get('info', {}).get('props', {})
            mc = obj_props.get('media.class', '')
            if mc not in ('Audio/Source', 'Audio/Source/Virtual'):
                continue
            node_name = obj_props.get('node.name', '')
            if 'bluez' in node_name.lower() and mac_us.lower() in node_name.lower():
                source_info = {
                    'source_name': node_name,
                    'source_node_id': obj.get('id'),
                }
                break

    if not source_info:
        raise CommandError(f'已切换 profile 但未检测到 PipeWire Source 节点，请稍后重试')

    return {
        'data': f'蓝牙麦克风已启用',
        'mac': mac,
        'source_name': source_info['source_name'],
        'source_node_id': source_info['source_node_id'],
    }


# 禁用蓝牙麦克风：切换回 A2DP profile 以获得更好的音频质量
def disable_bluetooth_microphone(mac):
    mac = mac.upper()
    # 检查设备是否已连接
    device_path = _find_device_path(mac)
    if not device_path:
        raise DeviceNotFoundError(f'设备 {mac} 未找到')

    # 获取可用 profile，优先 A2DP Sink
    available_profiles = get_bluetooth_audio_profiles(mac)
    target_profile = None
    for pref in ('a2dp_sink', 'a2dp_source'):
        match = next((p for p in available_profiles if p['name'] == pref and p.get('available', True)), None)
        if match:
            target_profile = pref
            break

    if not target_profile:
        raise InvalidParamError('设备不支持 A2DP profile，无法切换回高质量音频')

    # 检查当前 profile
    card_path = _find_bluez_card_path(mac)
    current_profile = ''
    if card_path:
        try:
            current_profile = str(_get_property(BLUEZ_IFACE_CARD, card_path, 'ActiveProfile'))
        except dbus.exceptions.DBusException:
            pass

    # 如果已经是 A2DP，无需切换
    if current_profile and 'a2dp' in current_profile.lower():
        return f'设备已在 A2DP profile ({current_profile})，无需切换'

    # 切换到 A2DP
    try:
        switch_bluetooth_profile(mac, target_profile)
    except MediaBridgeError as e:
        raise CommandError(f'切换到 A2DP 失败: {e.message}')

    return f'已切换回 A2DP 高质量音频模式'
