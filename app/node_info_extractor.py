import math
import logging

from utils import (extract_pw_vol_params, extract_pw_enumformat, extract_pw_routes,
                   get_prop_with_fallback, find_device_props)

logger = logging.getLogger('MediaHub')


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

    # 懒导入避免循环依赖：_get_wpctl_id_for_node / _get_wpctl_volume 定义在 audio_manager
    from volume_controller import volume_controller as _vc

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
            import audio_manager as _am
            wp_section = 'Sources' if 'Source' in props.get('media.class', '') else 'Sinks'
            wpctl_id = _am._get_wpctl_id_for_node(node_name, wp_section)
            if wpctl_id is not None:
                vol_info = _am._get_wpctl_volume(wpctl_id)
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
        import audio_manager as _am
        wp_section = 'Sources' if 'Source' in props.get('media.class', '') else 'Sinks'
        wpctl_id = _am._get_wpctl_id_for_node(node_name, wp_section)
        if wpctl_id is not None:
            vol_info = _am._get_wpctl_volume(wpctl_id)
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
