import re
import logging

from utils import run_command, get_prop_with_fallback
import platform_paths

logger = logging.getLogger('MediaHub')


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
    from node_info_extractor import _build_extended_props

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
