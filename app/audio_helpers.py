"""音频辅助模块 —— 音量控制 + 节点信息提取

所有音频操作统一使用 PipeWire/WirePlumber：
- VolumeController: 读取 pw-dump + 写入 wpctl
- 节点信息提取: 从 PipeWire 节点对象提取统一的音频信息
- 不再使用 ALSA/PulseAudio 工具，设备发现完全依赖 PipeWire
"""

import re
import math
import json
import time
import logging

from utils import (
    run_command, pw_dump, pw_dump_invalidate,
    extract_pw_vol_params, extract_pw_enumformat, extract_pw_routes,
    get_prop_with_fallback, find_device_props,
    get_node_id_by_name,
)
import platform_paths
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError

logger = logging.getLogger('PipeBridge')


# ============================================================================
# 音量控制器
# ============================================================================

class VolumeController:
    """统一音量控制器，使用 pw-dump 读取 + wpctl 写入"""

    @staticmethod
    def _cubic_to_linear(vol):
        """PipeWire 立方音量曲线 → 线性值"""
        if vol <= 0:
            return 0.0
        return vol ** (1.0 / 3.0)

    @staticmethod
    def _linear_to_cubic(vol):
        """线性值 → PipeWire 立方音量曲线"""
        if vol <= 0:
            return 0.0
        return vol ** 3

    @staticmethod
    def _get_node_props(device_name):
        """获取设备的 Props params 和 node 对象"""
        pw_data = pw_dump()
        for obj in pw_data:
            if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
                continue
            props = obj.get('info', {}).get('props', {})
            if props.get('node.name') == device_name:
                params = obj.get('info', {}).get('params', {})
                return extract_pw_vol_params(params if isinstance(params, dict) else {}), obj
        raise DeviceNotFoundError(f'设备不存在: {device_name}')

    def get_volume(self, device_name):
        """获取设备音量（pw-dump 读取 channelVolumes）"""
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

        raw_vol = props_params.get('volume', 0.0)
        if isinstance(raw_vol, (int, float)) and raw_vol >= 0:
            vol_percent = min(round(self._cubic_to_linear(float(raw_vol)) * 100), 100)
            return {
                'volume': vol_percent,
                'muted': bool(props_params.get('mute', False)),
                'device': device_name,
            }

        return {'volume': 0, 'muted': False, 'device': device_name}

    def set_volume(self, device_name, volume):
        """设置设备音量（wpctl set-volume）"""
        volume = max(0, min(100, int(volume)))
        wpctl_node_id = get_node_id_by_name(device_name)
        if wpctl_node_id is None:
            raise DeviceNotFoundError(f'设备不存在: {device_name}')

        vol_cubic = self._linear_to_cubic(volume / 100.0)
        result = run_command(
            f"{platform_paths.CMD_WPCTL} set-volume {wpctl_node_id} {vol_cubic:.4f}",
            timeout=5)
        if not result['success']:
            raise CommandError(
                f"wpctl set-volume 失败: {result.get('stderr', '')[:200]}",
                command=platform_paths.CMD_WPCTL)

        pw_dump_invalidate()
        time.sleep(0.15)
        verify = self.get_volume(device_name)
        logger.info(f"设置音量: {device_name} -> 目标{volume}% 实际{verify['volume']}%")
        return {'volume': verify['volume'], 'device': device_name}

    def set_mute(self, device_name, mute):
        """设置设备静音（wpctl set-mute）"""
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
        pw_dump_invalidate()

        label = '静音' if mute else '取消静音'
        logger.info(f"{label}: {device_name} node={node_id}")
        return {'muted': mute, 'device': device_name}

    def get_balance(self, device_name):
        """获取设备左右声道平衡（在线性空间计算）"""
        props_params, _ = self._get_node_props(device_name)
        channel_volumes = props_params.get('channelVolumes', [])

        if not channel_volumes or len(channel_volumes) < 2:
            return {'balance': 0.0, 'left': 0.0, 'right': 0.0, 'stereo': False}

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

    def set_balance(self, device_name, balance):
        """设置设备左右声道平衡（pw-cli set-param）"""
        balance = max(-1.0, min(1.0, float(balance)))
        props_params, node_obj = self._get_node_props(device_name)
        channel_volumes = props_params.get('channelVolumes', [])

        if not channel_volumes or len(channel_volumes) < 2:
            raise CommandError('该设备不是立体声设备', command='pw-dump')

        left_linear = self._cubic_to_linear(float(channel_volumes[0]))
        right_linear = self._cubic_to_linear(float(channel_volumes[1]))
        base_vol_linear = (left_linear + right_linear) / 2.0

        new_left_linear = max(0.0, min(2.0, base_vol_linear * (1.0 - balance)))
        new_right_linear = max(0.0, min(2.0, base_vol_linear * (1.0 + balance)))

        left = self._linear_to_cubic(new_left_linear)
        right = self._linear_to_cubic(new_right_linear)

        target_node_id = node_obj.get('id') if node_obj else None
        if target_node_id is None:
            raise DeviceNotFoundError(f'设备不存在: {device_name}')

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
        pw_dump_invalidate()

        logger.info(f"声道平衡: {device_name} -> {balance}")
        return {'balance': balance, 'device': device_name}

    def set_channel_volume(self, device_name, channel_index, volume):
        """设置设备指定声道的音量（pw-cli set-param）"""
        volume = max(0, min(100, int(volume)))
        props_params, node_obj = self._get_node_props(device_name)
        channel_volumes = props_params.get('channelVolumes', [])

        channel_index = int(channel_index)
        if channel_index < 0 or channel_index >= len(channel_volumes):
            raise InvalidParamError(f'声道索引 {channel_index} 超出范围（共 {len(channel_volumes)} 个声道）')

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

_STANDARD_SAMPLE_RATES = {
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


def _extract_node_audio_info(obj, pw_data):
    """从 PipeWire 节点对象提取统一的音频信息"""
    info = obj.get('info', {})
    params = info.get('params', {})
    props = info.get('props', {})

    if not isinstance(params, dict):
        params = {}

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
            raw_vol = props_params.get('volume', 0.0)
            vol_flat = float(raw_vol) if isinstance(raw_vol, (int, float)) and raw_vol >= 0 else 0.0
            vol_percent = min(round(vol_flat * 100), 100)
            if vol_flat > 0:
                vol_db = round(20 * math.log10(vol_flat), 2)
            muted = bool(props_params.get('mute', False))
    else:
        # Props params 为空（设备挂起或未初始化），音量不可读
        vol_flat = 0.0
        vol_percent = 0

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