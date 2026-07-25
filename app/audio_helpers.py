"""音频辅助模块 —— 音量控制 + 节点信息提取 + ALSA 回退 + PipeWire 修复

整合 volume_controller、node_info_extractor、alsa_fallback、pipewire_healer：
- VolumeController: 统一音量控制器，使用 pw-dump 读取 + wpctl 写入
- 节点信息提取: 从 PipeWire 节点对象提取统一的音频信息
- ALSA 回退: 通过 aplay/arecord 发现 PipeWire 未创建 Sink 的 ALSA 设备
- PipeWire 修复: PipeWire/WirePlumber 健康检查与恢复
"""

import re
import math
import json
import time
import logging

from utils import (
    run_command, pw_dump, pw_dump_invalidate,
    is_real_sink, start_pw_service, stop_pw_service,
    extract_pw_vol_params, extract_pw_enumformat, extract_pw_routes,
    get_prop_with_fallback, find_device_props,
    get_node_id_by_name,
)
import platform_paths
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError

logger = logging.getLogger('MediaBridge')


# ============================================================================
# 音量控制器
# ============================================================================

class VolumeController:
    """统一音量控制器，仅使用 pw-dump 读取 + wpctl 写入，失败直接抛异常"""

    # PipeWire 立方音量曲线 → 线性值
    @staticmethod
    def _cubic_to_linear(vol):
        if vol <= 0:
            return 0.0
        return vol ** (1.0 / 3.0)

    # 线性值 → PipeWire 立方音量曲线
    @staticmethod
    def _linear_to_cubic(vol):
        if vol <= 0:
            return 0.0
        return vol ** 3

    # 获取设备的 Props params 和 node 对象，找不到则抛 DeviceNotFoundError
    @staticmethod
    def _get_node_props(device_name):
        pw_data = pw_dump()
        for obj in pw_data:
            if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
                continue
            props = obj.get('info', {}).get('props', {})
            if props.get('node.name') == device_name:
                params = obj.get('info', {}).get('params', {})
                return extract_pw_vol_params(params if isinstance(params, dict) else {}), obj
        raise DeviceNotFoundError(f'设备不存在: {device_name}')

    # 获取设备音量（仅 pw-dump）
    def get_volume(self, device_name):
        props_params, _ = self._get_node_props(device_name)
        ch_vols = props_params.get('channelVolumes', [])

        if ch_vols and isinstance(ch_vols, list):
            valid = [self._cubic_to_linear(float(cv)) for cv in ch_vols
                     if isinstance(cv, (int, float))]
            if valid:
                vol_percent = min(round((sum(valid) / len(valid)) * 100), 100)
                return {
                    'volume': vol_percent,
                    'muted': bool(props_params.get('mute', False)),
                    'device': device_name,
                }

        # 回退到 volume 字段
        raw_vol = props_params.get('volume', 0.0)
        if isinstance(raw_vol, (int, float)) and raw_vol >= 0:
            vol_percent = min(round(self._cubic_to_linear(float(raw_vol)) * 100), 100)
            return {
                'volume': vol_percent,
                'muted': bool(props_params.get('mute', False)),
                'device': device_name,
            }

        return {'volume': 0, 'muted': False, 'device': device_name}

    # 设置设备音量（pw-cli set-param，统一使用 cubic 音量曲线）
    def set_volume(self, device_name, volume):
        volume = max(0, min(100, int(volume)))
        props_params, node_obj = self._get_node_props(device_name)
        channel_volumes = props_params.get('channelVolumes', [])

        target_node_id = node_obj.get('id') if node_obj else None
        if target_node_id is None:
            raise DeviceNotFoundError(f'设备不存在: {device_name}')

        # 优先使用 wpctl set-volume（WirePlumber 原生命令，会被正确处理和存储）
        # wpctl set-volume 接受 cubic 音量（与 PipeWire 内部 channelVolumes 一致）
        wpctl_node_id = get_node_id_by_name(device_name)
        vol_cubic = self._linear_to_cubic(volume / 100.0)
        if wpctl_node_id is not None:
            result = run_command(
                f"{platform_paths.CMD_WPCTL} set-volume {wpctl_node_id} {vol_cubic:.4f}",
                timeout=5)
            if not result['success']:
                raise CommandError(
                    f"wpctl set-volume 失败: {result.get('stderr', '')[:200]}",
                    command=platform_paths.CMD_WPCTL)
        else:
            # fallback: pw-cli set-param（可能被 WirePlumber 覆盖，仅在没有 wpctl ID 时使用）
            vol_linear = volume / 100.0
            if channel_volumes and len(channel_volumes) > 1:
                current_linears = [self._cubic_to_linear(float(cv)) for cv in channel_volumes]
                avg_current = sum(current_linears) / len(current_linears)
                if avg_current > 0:
                    new_volumes = [self._linear_to_cubic(cl / avg_current * vol_linear) for cl in current_linears]
                else:
                    new_volumes = [vol_cubic] * len(channel_volumes)
            else:
                new_volumes = [vol_cubic] * max(1, len(channel_volumes))
            props_json = json.dumps({"channelVolumes": new_volumes})
            result = run_command(
                f"{platform_paths.CMD_PW_CLI} set-param {target_node_id} Props '{props_json}'",
                timeout=5)
            if not result['success']:
                raise CommandError(
                    f"pw-cli set-param 失败: {result.get('stderr', '')[:200]}",
                    command=platform_paths.CMD_PW_CLI)
        pw_dump_invalidate()  # 清除缓存，确保后续验证读取最新数据

        # 短暂延迟后验证，确保 pw-dump 读取到新值
        time.sleep(0.15)
        verify = self.get_volume(device_name)
        logger.info(f"设置音量: {device_name} -> 目标{volume}% 实际{verify['volume']}%")
        return {'volume': verify['volume'], 'device': device_name}

    # 设置设备静音（仅 wpctl）
    def set_mute(self, device_name, mute):
        mute_flag = '1' if mute else '0'
        node_id = get_node_id_by_name(device_name)
        if node_id is None:
            raise DeviceNotFoundError(f'设备不存在: {device_name}')

        result = run_command(
            f"{platform_paths.CMD_WPCTL} set-mute {node_id} {mute_flag}", timeout=5)
        if not result['success']:
            raise CommandError(
                f"wpctl set-mute 失败: {result.get('stderr', '')[:200]}",
                command=platform_paths.CMD_WPCTL)
        pw_dump_invalidate()  # 清除缓存，确保后续读取最新数据

        label = '静音' if mute else '取消静音'
        logger.info(f"{label}: {device_name} node={node_id}")
        return {'muted': mute, 'device': device_name}

    # 获取设备左右声道平衡（pw-dump Props，在线性空间计算）
    def get_balance(self, device_name):
        props_params, _ = self._get_node_props(device_name)
        channel_volumes = props_params.get('channelVolumes', [])

        if not channel_volumes or len(channel_volumes) < 2:
            return {'balance': 0.0, 'left': 0.0, 'right': 0.0, 'stereo': False}

        # 在线性空间计算平衡，与 set_balance 保持一致
        left_linear = self._cubic_to_linear(float(channel_volumes[0]))
        right_linear = self._cubic_to_linear(float(channel_volumes[1]))
        total = left_linear + right_linear
        balance = max(-1.0, min(1.0, (right_linear - left_linear) / total)) if total > 0 else 0.0

        return {
            'balance': round(balance, 3),
            'left': round(left_linear, 4),
            'right': round(right_linear, 4),
            'stereo': True,
        }

    # 设置设备左右声道平衡（pw-cli）
    def set_balance(self, device_name, balance):
        balance = max(-1.0, min(1.0, float(balance)))
        props_params, node_obj = self._get_node_props(device_name)
        channel_volumes = props_params.get('channelVolumes', [])

        if not channel_volumes or len(channel_volumes) < 2:
            raise CommandError('该设备不是立体声设备', command='pw-dump')

        # 将 cubic volume 转为线性值后再计算平衡，避免感知偏差
        left_linear = self._cubic_to_linear(float(channel_volumes[0]))
        right_linear = self._cubic_to_linear(float(channel_volumes[1]))
        base_vol_linear = (left_linear + right_linear) / 2.0

        # 在线性空间中计算平衡
        new_left_linear = max(0.0, min(2.0, base_vol_linear * (1.0 - balance)))
        new_right_linear = max(0.0, min(2.0, base_vol_linear * (1.0 + balance)))

        # 转回 cubic volume
        left = self._linear_to_cubic(new_left_linear)
        right = self._linear_to_cubic(new_right_linear)

        target_node_id = node_obj.get('id') if node_obj else None
        if target_node_id is None:
            raise DeviceNotFoundError(f'设备不存在: {device_name}')

        # 保留多声道设备的其他声道音量不变
        new_volumes = [round(left, 6), round(right, 6)]
        for i in range(2, len(channel_volumes)):
            new_volumes.append(float(channel_volumes[i]))
        props_json = json.dumps({"channelVolumes": new_volumes})
        result = run_command(
            f"{platform_paths.CMD_PW_CLI} set-param {target_node_id} Props '{props_json}'",
            timeout=5)
        if not result['success']:
            raise CommandError(
                f"pw-cli set-param 失败: {result.get('stderr', '')[:200]}",
                command=platform_paths.CMD_PW_CLI)
        pw_dump_invalidate()  # 清除缓存，确保后续读取最新数据

        logger.info(f"声道平衡: {device_name} -> {balance}")
        return {'balance': balance, 'device': device_name}

    # 设置设备指定声道的音量（pw-cli）
    def set_channel_volume(self, device_name, channel_index, volume):
        volume = max(0, min(100, int(volume)))
        props_params, node_obj = self._get_node_props(device_name)
        channel_volumes = props_params.get('channelVolumes', [])

        channel_index = int(channel_index)
        if channel_index < 0 or channel_index >= len(channel_volumes):
            raise InvalidParamError(f'声道索引 {channel_index} 超出范围（共 {len(channel_volumes)} 个声道）')

        # 将百分比转为 PipeWire cubic volume
        new_volumes = [float(cv) for cv in channel_volumes]
        new_volumes[channel_index] = self._linear_to_cubic(volume / 100.0)

        target_node_id = node_obj.get('id') if node_obj else None
        if target_node_id is None:
            raise DeviceNotFoundError(f'设备不存在: {device_name}')

        props_json = json.dumps({"channelVolumes": new_volumes})
        result = run_command(
            f"{platform_paths.CMD_PW_CLI} set-param {target_node_id} Props '{props_json}'",
            timeout=5)
        if not result['success']:
            raise CommandError(
                f"pw-cli set-param 失败: {result.get('stderr', '')[:200]}",
                command=platform_paths.CMD_PW_CLI)

        logger.info(f"声道音量: {device_name} CH{channel_index} -> {volume}%")
        return {'device': device_name, 'channel': channel_index, 'volume': volume}


# 模块级单例
volume_controller = VolumeController()


# ============================================================================
# 节点信息提取
# ============================================================================

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


# 构建设备扩展属性字典
def _build_extended_props(props, device_props):
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


# 从 wpctl 回退获取音量信息（当 pw-dump Props 为空时使用）
def _get_vol_from_wpctl(node_name, media_class):
    # 懒导入避免循环依赖：audio_manager 依赖本模块间接依赖回自身
    import audio_manager as _am
    wp_section = 'Sources' if 'Source' in (media_class or '') else 'Sinks'
    wpctl_id = _am._get_wpctl_id_for_node(node_name, wp_section)
    if wpctl_id is None:
        return None
    vol_info = _am._get_wpctl_volume(wpctl_id)
    vol_percent = vol_info['volume']
    vol_flat = min(vol_percent / 100.0, 1.0)
    vol_db = round(20 * math.log10(vol_flat), 2) if vol_flat > 0 else 0.0
    return {'vol_percent': min(vol_percent, 100), 'vol_flat': vol_flat, 'vol_db': vol_db, 'muted': vol_info['muted']}


# 从 PipeWire 节点对象提取统一的音频信息
def _extract_node_audio_info(obj, pw_data):
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
    media_class = props.get('media.class', '')

    # volume_controller 是本模块的内部单例，直接使用
    _vc = volume_controller

    if props_params:
        channel_volumes = props_params.get('channelVolumes', [])
        if channel_volumes and isinstance(channel_volumes, list):
            for i, cv in enumerate(channel_volumes):
                if isinstance(cv, (int, float)):
                    pos_name = channel_positions[i] if i < len(channel_positions) else f'CH{i}'
                    ch_label = _CHANNEL_POS_MAP.get(pos_name, pos_name)
                    linear_cv = _vc._cubic_to_linear(float(cv))
                    channels.append({'channel': ch_label, 'position': pos_name,
                                     'volume': min(round(linear_cv * 100), 100),
                                     'effective_volume': min(round(linear_cv * 100), 100)})
        valid_ch_vols = [_vc._cubic_to_linear(float(cv))
                         for cv in channel_volumes if isinstance(cv, (int, float))]
        if valid_ch_vols:
            vol_flat = sum(valid_ch_vols) / len(valid_ch_vols)
            vol_percent = min(round(vol_flat * 100), 100)
            if vol_flat > 0:
                vol_db = round(20 * math.log10(vol_flat), 2)
            muted = bool(props_params.get('mute', False))
        else:
            # 回退到 wpctl 获取音量
            wp_info = _get_vol_from_wpctl(node_name, media_class)
            if wp_info:
                vol_percent = wp_info['vol_percent']
                muted = wp_info['muted']
                vol_flat = wp_info['vol_flat']
                vol_db = wp_info['vol_db']
            else:
                raw_vol = props_params.get('volume', 0.0)
                vol_flat = float(raw_vol) if isinstance(raw_vol, (int, float)) and raw_vol >= 0 else 0.0
                vol_percent = min(round(vol_flat * 100), 100)
                if vol_flat > 0:
                    vol_db = round(20 * math.log10(vol_flat), 2)
                muted = bool(props_params.get('mute', False))
    else:
        # Props 参数为空，回退到 wpctl
        wp_info = _get_vol_from_wpctl(node_name, media_class)
        if wp_info:
            vol_percent = wp_info['vol_percent']
            muted = wp_info['muted']
            vol_flat = wp_info['vol_flat']
            vol_db = wp_info['vol_db']

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


# ============================================================================
# ALSA 回退设备发现
# ============================================================================

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


def _supplement_alsa_devices(pw_data, pw_sinks, devices, default_sink_name, skip_activate=False):
    # 通过 aplay -l 发现 PipeWire 未创建 Sink 的 ALSA 设备，按声卡去重后补充
    # skip_activate=True 时跳过 profile 激活，避免扫描时破坏已有音频连接
    from audio_manager import _try_activate_profile, _get_wpctl_id_for_node, _get_wpctl_volume

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
                if dev_id is not None and not skip_activate:
                    _try_activate_profile(dev_id, ad.get('card_id', ''))
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
            'driver': get_prop_with_fallback(pw_dev_props, {}, 'alsa.driver') or 'ALSA/PipeWire',
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


# ============================================================================
# PipeWire/WirePlumber 修复
# ============================================================================

# 从 system_manager 导入依赖检查函数
from system_manager import check_pipewire_running, check_wireplumber_running


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
    if check_pipewire_running():
        return True
    logger.info("PipeWire 未运行，尝试启动...")
    start_pw_service('pipewire')
    time.sleep(1)
    start_pw_service('pipewire-pulse')
    time.sleep(0.5)
    if check_pipewire_running():
        return True
    logger.error("PipeWire 启动失败")
    return False


# 检查 WirePlumber 是否运行，未运行则尝试启动，返回是否成功
def _check_wireplumber_running():
    if check_wireplumber_running():
        return True
    logger.info("WirePlumber 未运行，尝试启动...")
    wp_ok = start_pw_service('wireplumber')
    time.sleep(2)
    if not wp_ok and not check_wireplumber_running():
        logger.info("WirePlumber 首次启动失败，重试...")
        start_pw_service('wireplumber')
        time.sleep(2)
    if not check_wireplumber_running():
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

    # 4a: 确保蜂鸣器内核模块已加载
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
