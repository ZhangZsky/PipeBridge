import re
import math
import json
import logging

from utils import (
    run_command, pw_dump, pw_dump_invalidate,
    extract_pw_vol_params, extract_pw_enumformat, extract_pw_routes,
    get_prop_with_fallback, find_device_props,
)
import platform_paths
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError

logger = logging.getLogger('PipeBridge')

class VolumeController:
    @staticmethod
    def _cubic_to_linear(vol):
        if vol <= 0:
            return 0.0
        return vol ** (1.0 / 3.0)

    @staticmethod
    def _linear_to_cubic(vol):
        if vol <= 0:
            return 0.0
        return vol ** 3

    @staticmethod
    def _is_bluez_device(device_name):
        # 蓝牙 A2DP 设备(bluez_output.*) 启用了 bluez5.enable-hw-volume，
        # 其 channelVolumes 走 AVRCP 绝对音量，PipeWire 内部为【线性】刻度，
        # 不能再套用 cubic(立方)映射，否则写入100%回读40%(双重错误映射)。
        return isinstance(device_name, str) and device_name.lower().startswith('bluez_')

    def _pct_to_raw(self, device_name, pct):
        # 百分比(0-100) -> PipeWire channelVolume 原始值
        linear = max(0.0, min(1.0, pct / 100.0))
        if self._is_bluez_device(device_name):
            return linear
        return self._linear_to_cubic(linear)

    def _raw_to_pct(self, device_name, raw):
        # PipeWire channelVolume 原始值 -> 百分比(0-100)
        raw = float(raw)
        if self._is_bluez_device(device_name):
            linear = max(0.0, raw)
        else:
            linear = self._cubic_to_linear(raw)
        return min(round(linear * 100), 100)

    def _raw_to_linear(self, device_name, raw):
        # channelVolume 原始值 -> 线性值（蓝牙原始值即线性，普通设备需开立方）
        raw = float(raw)
        if self._is_bluez_device(device_name):
            return max(0.0, raw)
        return self._cubic_to_linear(raw)

    def _linear_to_raw(self, device_name, linear):
        # 线性值 -> channelVolume 原始值（蓝牙直通，普通设备取立方）
        if self._is_bluez_device(device_name):
            return max(0.0, linear)
        return self._linear_to_cubic(linear)


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

    @staticmethod
    def _wpctl_get_volume(node_id):
        # 通过 wpctl get-volume 读取音量。wpctl 走 mixer-api(cubic scale)，
        # 与 wpctl set-volume 同源，返回值即“用户感知线性刻度”(0.56 = 56%)。
        #
        # 关键：对 alsa 硬件设备(如 HDMI)，真实音量存于 Device 的 Route 参数
        # (Spa:Pod:Object:Param:Route.props.channelVolumes)，而 pw-dump 中
        # Node 的 Props.channelVolumes 恒为透传值 1.0。因此直接读 Node Props
        # 会永远得到 100%。wpctl get-volume 经 WirePlumber 聚合，能取到 Route
        # 层的真实音量，是唯一与 set-volume 一致的回读来源。
        #
        # 返回 0-100 百分比整数；失败返回 None（由调用方回退到 Node Props）。
        if node_id is None:
            return None
        result = run_command(
            f"{platform_paths.CMD_WPCTL} get-volume {node_id}",
            timeout=5)
        if not result['success']:
            return None
        m = re.search(r'Volume:\s*([0-9]*\.?[0-9]+)', result.get('stdout', ''))
        if not m:
            return None
        try:
            linear = float(m.group(1))
        except (ValueError, TypeError):
            return None
        return min(round(max(0.0, linear) * 100), 100)

    def get_volume(self, device_name):
        props_params, node_obj = self._get_node_props(device_name)

        # 普通(alsa 硬件)设备：真实音量存于 Device Route，Node Props 恒为
        # 透传 1.0(100%)，必须经 wpctl get-volume 读取真实值。
        # 蓝牙设备：Node Props 已是可信线性音量，且 AVRCP 往返有延迟，
        # 直接读 Node Props 更稳定，故不走 wpctl。
        if not self._is_bluez_device(device_name):
            node_id = node_obj.get('id') if node_obj else None
            wpctl_pct = self._wpctl_get_volume(node_id)
            if wpctl_pct is not None:
                return {
                    'volume': wpctl_pct,
                    'muted': bool(props_params.get('mute', False)),
                    'device': device_name,
                }

        ch_vols = props_params.get('channelVolumes', [])

        if ch_vols and isinstance(ch_vols, list):
            valid = [float(cv) for cv in ch_vols
                     if isinstance(cv, (int, float))]
            if valid:
                # 按设备类型换算：蓝牙线性直通，普通设备 cubic
                avg_raw = sum(valid) / len(valid)
                vol_percent = self._raw_to_pct(device_name, avg_raw)
                return {
                    'volume': vol_percent,
                    'muted': bool(props_params.get('mute', False)),
                    'device': device_name,
                }

        raw_vol = props_params.get('volume', 0.0)
        if isinstance(raw_vol, (int, float)) and raw_vol >= 0:
            vol_percent = self._raw_to_pct(device_name, float(raw_vol))
            return {
                'volume': vol_percent,
                'muted': bool(props_params.get('mute', False)),
                'device': device_name,
            }

        return {'volume': 0, 'muted': False, 'device': device_name}

    def _get_channel_count_from_node(self, node_obj):
        info = node_obj.get('info', {}) if node_obj else {}
        props = info.get('props', {})
        try:
            ch = int(props.get('audio.channels', 0))
            if 1 <= ch <= 32:
                return ch
        except (ValueError, TypeError):
            pass
        params = info.get('params', {})
        if isinstance(params, dict):
            enum_format = extract_pw_enumformat(params)
            if enum_format:
                first = enum_format[0] if isinstance(enum_format[0], dict) else {}
                ch = first.get('channels', 0)
                if isinstance(ch, (int, float)) and 1 <= ch <= 32:
                    return int(ch)
        return 2

    def set_volume(self, device_name, volume):
        volume = max(0, min(100, int(volume)))
        props_params, node_obj = self._get_node_props(device_name)
        target_node_id = node_obj.get('id') if node_obj else None
        if target_node_id is None:
            raise DeviceNotFoundError(f'设备不存在: {device_name}')

        # 音量原始值换算：蓝牙(hw-volume)线性直通，普通设备 cubic。
        # 蓝牙 A2DP 走 AVRCP 绝对音量，PipeWire channelVolume 为线性刻度，
        # 若沿用 cubic 会出现写100%回读40%的错误映射。
        vol_raw = self._pct_to_raw(device_name, volume)
        is_bluez = self._is_bluez_device(device_name)

        # 使用 wpctl set-volume 而非 pw-cli set-param
        # wpctl 通过 WirePlumber API 设置音量，WirePlumber 会保存状态
        # 设备从挂起恢复时（如蓝牙 A2DP Transport 重建）会正确恢复用户设置的音量
        # pw-cli set-param 绕过 WirePlumber，设备恢复时音量会被旧状态覆盖
        result = run_command(
            f"{platform_paths.CMD_WPCTL} set-volume {target_node_id} {vol_raw:.6f}",
            timeout=5)
        if not result['success']:
            raise CommandError(
                f"wpctl set-volume 失败: {result.get('stderr', '')[:200]}",
                command=platform_paths.CMD_WPCTL)

        pw_dump_invalidate()

        # 蓝牙设备：AVRCP 往返有延迟，设置后立即回读常是旧值(如40%)，
        # 且部分设备仅支持有限音量档位。此处信任写入的目标值，
        # 由 pw-mon 实时推送做后续二次校准，避免 UI 立刻跳回旧值。
        if is_bluez:
            logger.info(f"设置音量: {device_name} -> 目标{volume}% (蓝牙, 信任写入值)")
            return {'volume': volume, 'device': device_name}

           # 普通设备：真实音量存于 Device Route，pw-dump 的 Node Props.channelVolumes
        # 恒为透传 1.0，就近吸附/回读若读 Node Props 会永远得到 100%。
        # 必须用 wpctl get-volume(与 set-volume 同源，走 mixer-api cubic scale)
        # 回读真实音量。wpctl 返回“用户线性刻度”(0.56=56%)，其百分比可直接与
        # 目标 volume 就近比较，容差 ±2% 吸收浮点/档位误差。
        wpctl_pct = self._wpctl_get_volume(target_node_id)
        if wpctl_pct is not None:
            if abs(wpctl_pct - volume) <= 2:
                logger.info(f"设置音量: {device_name} -> 目标{volume}% (就近吸附)")
                return {'volume': volume, 'device': device_name}
            logger.info(f"设置音量: {device_name} -> 目标{volume}% 实际{wpctl_pct}%")
            return {'volume': wpctl_pct, 'device': device_name}

        # wpctl 回读失败时兜底：读 Node Props(纯软件/虚拟 sink 该值可信)
        verify = self.get_volume(device_name)
        logger.info(f"设置音量: {device_name} -> 目标{volume}% 实际{verify['volume']}%")
        return {'volume': verify['volume'], 'device': device_name}

    def set_mute(self, device_name, mute):
        props_params, node_obj = self._get_node_props(device_name)
        target_node_id = node_obj.get('id') if node_obj else None
        if target_node_id is None:
            raise DeviceNotFoundError(f'设备不存在: {device_name}')

        channel_volumes = props_params.get('channelVolumes', [])
        if channel_volumes:
            props_data = {"mute": bool(mute), "channelVolumes": [float(cv) for cv in channel_volumes]}
        else:
            ch_count = self._get_channel_count_from_node(node_obj)
            props_data = {"mute": bool(mute), "channelVolumes": [1.0] * ch_count}

        props_json = json.dumps(props_data)
        result = run_command(
            f"{platform_paths.CMD_PW_CLI} set-param {target_node_id} Props '{props_json}'",
            timeout=5)
        if not result['success']:
            raise CommandError(
                f"pw-cli set-param 失败: {result.get('stderr', '')[:200]}",
                command=platform_paths.CMD_PW_CLI)
        pw_dump_invalidate()

        label = '静音' if mute else '取消静音'
        logger.info(f"{label}: {device_name} node={target_node_id}")
        return {'muted': mute, 'device': device_name}

    def get_balance(self, device_name):
        props_params, _ = self._get_node_props(device_name)
        channel_volumes = props_params.get('channelVolumes', [])

        if not channel_volumes or len(channel_volumes) < 2:
            return {'balance': 0.0, 'left': 0.0, 'right': 0.0, 'stereo': False}

        left_linear = self._raw_to_linear(device_name, float(channel_volumes[0]))
        right_linear = self._raw_to_linear(device_name, float(channel_volumes[1]))
        total = left_linear + right_linear
        balance = max(-1.0, min(1.0, (right_linear - left_linear) / total)) if total > 0 else 0.0

        return {
            'balance': round(balance, 3),
            'left': round(left_linear, 4),
            'right': round(right_linear, 4),
            'stereo': True,
        }

    def set_balance(self, device_name, balance):
        balance = max(-1.0, min(1.0, float(balance)))
        props_params, node_obj = self._get_node_props(device_name)
        channel_volumes = props_params.get('channelVolumes', [])

        if not channel_volumes:
            raise CommandError('设备处于挂起状态，无法设置平衡', command='pw-dump')
        if len(channel_volumes) < 2:
            raise CommandError('该设备不是立体声设备，无法设置平衡', command='pw-dump')

        left_linear = self._raw_to_linear(device_name, float(channel_volumes[0]))
        right_linear = self._raw_to_linear(device_name, float(channel_volumes[1]))
        base_vol_linear = (left_linear + right_linear) / 2.0

        new_left_linear = max(0.0, min(2.0, base_vol_linear * (1.0 - balance)))
        new_right_linear = max(0.0, min(2.0, base_vol_linear * (1.0 + balance)))

        left = self._linear_to_raw(device_name, new_left_linear)
        right = self._linear_to_raw(device_name, new_right_linear)

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
        volume = max(0, min(100, int(volume)))
        props_params, node_obj = self._get_node_props(device_name)
        channel_volumes = props_params.get('channelVolumes', [])

        channel_index = int(channel_index)
        if channel_index < 0 or channel_index >= len(channel_volumes):
            raise InvalidParamError(f'声道索引 {channel_index} 超出范围（共 {len(channel_volumes)} 个声道）')

        new_volumes = [float(cv) for cv in channel_volumes]
        new_volumes[channel_index] = self._linear_to_raw(device_name, volume / 100.0)

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

volume_controller = VolumeController()

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
    _dev_name = props.get('node.name', '')

    if props_params:
        channel_volumes = props_params.get('channelVolumes', [])
        if channel_volumes and isinstance(channel_volumes, list):
            for i, cv in enumerate(channel_volumes):
                if isinstance(cv, (int, float)):
                    pos_name = channel_positions[i] if i < len(channel_positions) else f'CH{i}'
                    ch_label = _CHANNEL_POS_MAP.get(pos_name, pos_name)
                    linear_cv = _vc._raw_to_linear(_dev_name, float(cv))
                    channels.append({'channel': ch_label, 'position': pos_name,
                                     'volume': min(round(linear_cv * 100), 100),
                                     'effective_volume': min(round(linear_cv * 100), 100)})
        valid_ch_vols = [_vc._raw_to_linear(_dev_name, float(cv))
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
        vol_flat = 0.0
        vol_percent = 0

    if len(channels) >= 2 and (channels[0]['volume'] + channels[1]['volume']) > 0:
        balance = round((channels[1]['volume'] - channels[0]['volume'])
                        / (channels[0]['volume'] + channels[1]['volume']), 3)

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

    ports, active_port = extract_pw_routes(params)

    if not ports and not active_port:
        device_id_prop = props.get('device.id')
        if device_id_prop is not None:
            for dev_obj in pw_data:
                if dev_obj.get('type') == 'PipeWire:Interface:Device' and dev_obj.get('id') == device_id_prop:
                    dev_params = dev_obj.get('info', {}).get('params', {})
                    if isinstance(dev_params, dict):
                        ports, active_port = extract_pw_routes(dev_params)
                    break

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
