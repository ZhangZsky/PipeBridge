import shlex
import logging
import os
import re
import json
from utils import run_command, pw_dump, find_pw_node, get_prop_with_fallback, find_device_props, parse_edid_monitor_name, parse_edid_physical_size, _find_pw_links, _get_ports_for_node, _build_link_info
import config
import platform_paths
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError

logger = logging.getLogger('MediaBridge')



# 视频相关 media.class 集合（仅包含 PipeWire 实际存在的类型）
_VIDEO_MEDIA_CLASSES = (
    'Video/Sink', 'Video/Sink/Virtual',
    'Video/Source', 'Video/Source/Virtual',
    'Video/Processor', 'Video/Processor/Virtual',
)


# 视频设备类型分类，综合名称关键词和 device 属性
def _classify_video_type(name, props=None, device_props=None):
    if props is None:
        props = {}
    if device_props is None:
        device_props = {}
    name_lower = name.lower()

    # 1. 通过 device.api / device.bus 属性检测（最可靠）
    device_api = get_prop_with_fallback(props, device_props, 'device.api', '').lower()
    device_bus = get_prop_with_fallback(props, device_props, 'device.bus', '').lower()

    if device_bus == 'usb' or 'v4l2' in device_api:
        # USB 摄像头 vs USB 采集卡
        if 'hdmi' in name_lower or 'capture' in name_lower or 'display' in name_lower:
            return 'hdmi_capture'
        return 'camera'
    if 'bluez' in device_api:
        return 'other'

    # 2. 名称关键词
    if 'hdmi' in name_lower:
        return 'hdmi'
    if 'display' in name_lower or 'monitor' in name_lower:
        return 'display'
    if 'camera' in name_lower or 'cam' in name_lower:
        return 'camera'
    if 'screen' in name_lower or 'capture' in name_lower:
        return 'screen_capture'
    if 'v4l2' in name_lower:
        return 'v4l2'
    if 'loopback' in name_lower:
        return 'loopback'
    return 'other'


def _classify_role(media_class):
    if 'Source' in media_class:
        return 'source'
    if 'Sink' in media_class:
        return 'sink'
    if 'Processor' in media_class:
        return 'processor'
    return 'unknown'


def _parse_video_format(video_params):
    """解析 PipeWire 视频格式参数，返回 (width, height, fps, pixel_format)"""
    w = 0
    h = 0
    f = 0
    pf = ''
    if not isinstance(video_params, dict):
        return w, h, f, pf
    sz = video_params.get('size', {})
    if isinstance(sz, dict):
        w_val = sz.get('width', 0)
        h_val = sz.get('height', 0)
        if isinstance(w_val, (int, float)) and w_val > 0:
            w = int(w_val)
        if isinstance(h_val, (int, float)) and h_val > 0:
            h = int(h_val)
    fr = video_params.get('framerate', {})
    if isinstance(fr, dict):
        num = fr.get('num', 0)
        denom = fr.get('denom', 1)
        if isinstance(num, (int, float)) and isinstance(denom, (int, float)) and denom > 0:
            f = round(num / denom, 1)
        elif 'default' in fr and isinstance(fr['default'], dict):
            dn = fr['default']
            num = dn.get('num', 0)
            denom = dn.get('denom', 1)
            if isinstance(num, (int, float)) and isinstance(denom, (int, float)) and denom > 0:
                f = round(num / denom, 1)
    elif isinstance(fr, list) and len(fr) > 0:
        first_fr = fr[0] if isinstance(fr[0], dict) else {}
        num = first_fr.get('num', 0)
        denom = first_fr.get('denom', 1)
        if isinstance(num, (int, float)) and isinstance(denom, (int, float)) and denom > 0:
            f = round(num / denom, 1)
    fmt_val = video_params.get('format', '')
    if isinstance(fmt_val, str) and fmt_val:
        pf = fmt_val
    elif isinstance(fmt_val, list) and len(fmt_val) > 0:
        first_fmt = fmt_val[0]
        if isinstance(first_fmt, str) and first_fmt:
            pf = first_fmt
    return w, h, f, pf


def _find_video_nodes(pw_data):
    return [obj for obj in pw_data
            if isinstance(obj, dict)
            and obj.get('type') == 'PipeWire:Interface:Node'
            and obj.get('info', {}).get('props', {}).get('media.class', '') in _VIDEO_MEDIA_CLASSES]


def _get_node_info(obj):
    info = obj.get('info', {})
    props = info.get('props', {})
    params = info.get('params', {})

    node_id = obj.get('id')
    name = props.get('node.name', '')
    friendly_name = props.get('node.description', '') or props.get('node.nick', '') or name
    media_class = props.get('media.class', '')

    video_type = _classify_video_type(name, props)

    role = _classify_role(media_class)

    width = 0
    height = 0
    fps = 0
    pixel_format = ''
    formats = []

    enum_format = []
    if isinstance(params, dict):
        ef = params.get('EnumFormat', [])
        if isinstance(ef, list):
            enum_format = ef
        elif isinstance(ef, dict):
            enum_format = [ef]

    if not enum_format and node_id is not None:
        cli_result = run_command(f"pw-cli enum-params {node_id} EnumFormat 2>/dev/null", timeout=3)
        if cli_result['success'] and cli_result['stdout']:
            try:
                parsed = json.loads(cli_result['stdout'])
                if isinstance(parsed, list):
                    enum_format = parsed
                elif isinstance(parsed, dict):
                    enum_format = [parsed]
            except (json.JSONDecodeError, ValueError):
                for line in cli_result['stdout'].splitlines():
                    line = line.strip()
                    if line.startswith('{'):
                        try:
                            parsed = json.loads(line)
                            if isinstance(parsed, dict):
                                enum_format.append(parsed)
                        except (json.JSONDecodeError, ValueError):
                            continue

    if enum_format:
        logger.debug(f"EnumFormat raw for {name}: {enum_format[:1]}")
        first = enum_format[0] if isinstance(enum_format[0], dict) else {}
        video_params = first.get('Video', first)
        width, height, fps, pixel_format = _parse_video_format(video_params)
        logger.debug(f"Parsed for {name}: w={width} h={height} fps={fps} pf={pixel_format}")

        for ef_entry in enum_format:
            if isinstance(ef_entry, dict):
                vp = ef_entry.get('Video', ef_entry)
                if isinstance(vp, dict):
                    fmt_val = vp.get('format', '')
                    if isinstance(fmt_val, str) and fmt_val and fmt_val not in formats:
                        formats.append(fmt_val)
                    elif isinstance(fmt_val, list):
                        for fv in fmt_val:
                            if isinstance(fv, str) and fv and fv not in formats:
                                formats.append(fv)

    if width == 0 and fps == 0 and not pixel_format:
        fmt_param = []
        if isinstance(params, dict):
            fp = params.get('Format', [])
            if isinstance(fp, list):
                fmt_param = fp
            elif isinstance(fp, dict):
                fmt_param = [fp]

        if not fmt_param and node_id is not None:
            cli_result = run_command(f"pw-cli enum-params {node_id} Format 2>/dev/null", timeout=3)
            if cli_result['success'] and cli_result['stdout']:
                try:
                    parsed = json.loads(cli_result['stdout'])
                    if isinstance(parsed, list):
                        fmt_param = parsed
                    elif isinstance(parsed, dict):
                        fmt_param = [parsed]
                except (json.JSONDecodeError, ValueError):
                    pass

        if fmt_param:
            logger.debug(f"Format fallback raw for {name}: {fmt_param[:1]}")
            first = fmt_param[0] if isinstance(fmt_param[0], dict) else {}
            video_params = first.get('Video', first)
            w2, h2, f2, pf2 = _parse_video_format(video_params)
            logger.debug(f"Format fallback parsed for {name}: w={w2} h={h2} fps={f2} pf={pf2}")
            if w2 > 0:
                width = w2
            if h2 > 0:
                height = h2
            if f2 > 0:
                fps = f2
            if pf2:
                pixel_format = pf2

    return {
        'name': name,
        'friendly_name': friendly_name,
        'node_id': node_id,
        'video_type': video_type,
        'role': role,
        'media_class': media_class,
        'source': 'PipeWire',
        'width': width,
        'height': height,
        'fps': fps,
        'pixel_format': pixel_format,
        'formats': formats,
    }


def _get_v4l2_devices():
    # 通过 /sys/class/video4linux 检测 V4L2 设备（USB 摄像头、采集卡等）
    devices = []
    v4l2_path = platform_paths.SYS_VIDEO4LINUX
    if not os.path.exists(v4l2_path):
        return devices

    for entry in sorted(os.listdir(v4l2_path)):
        if not entry.startswith('video'):
            continue
        dev_path = os.path.join(v4l2_path, entry)
        name_path = os.path.join(dev_path, 'name')
        if not os.path.exists(name_path):
            continue
        try:
            with open(name_path, 'r') as f:
                dev_name = f.read().strip()
        except (OSError, IOError) as e:
            logger.debug(f"读取失败: {e}")
            continue
        if not dev_name:
            continue

        # 读取设备能力
        dev_caps = ''
        caps_desc = []
        caps_path = os.path.join(dev_path, 'device', 'capabilities')
        if os.path.exists(caps_path):
            try:
                with open(caps_path, 'r') as f:
                    caps_val = f.read().strip()
                    caps_int = int(caps_val, 16) if caps_val else 0
                    if caps_int & 0x00000001:
                        caps_desc.append('视频捕获')
                    if caps_int & 0x00000002:
                        caps_desc.append('视频输出')
                    if caps_int & 0x00000004:
                        caps_desc.append('视频叠加')
                    if caps_int & 0x00000008:
                        caps_desc.append('VBI捕获')
                    if caps_int & 0x00000010:
                        caps_desc.append('VBI输出')
                    if caps_int & 0x04000000:
                        caps_desc.append('流式传输')
                    if caps_int & 0x00000100:
                        caps_desc.append('调谐器')
                    if caps_int & 0x00000200:
                        caps_desc.append('音频')
                    dev_caps = ', '.join(caps_desc) if caps_desc else ''
            except (OSError, IOError) as e:
                logger.debug(f"读取失败: {e}")

        # 读取 USB 厂商/产品信息
        vendor_id = ''
        product_id = ''
        is_usb = False
        usb_vendor_path = os.path.join(dev_path, 'device', 'idVendor')
        usb_product_path = os.path.join(dev_path, 'device', 'idProduct')
        if os.path.exists(usb_vendor_path):
            try:
                with open(usb_vendor_path, 'r') as f:
                    vendor_id = f.read().strip()
                is_usb = True
            except (OSError, IOError) as e:
                logger.debug(f"读取失败: {e}")
        if os.path.exists(usb_product_path):
            try:
                with open(usb_product_path, 'r') as f:
                    product_id = f.read().strip()
            except (OSError, IOError) as e:
                logger.debug(f"读取失败: {e}")

        # 读取 device/bus 以区分 USB/PCI/平台设备
        bus_type = ''
        bus_path = os.path.join(dev_path, 'device', 'bus')
        if os.path.exists(bus_path):
            try:
                with open(bus_path, 'r') as f:
                    bus_type = f.read().strip()
            except (OSError, IOError) as e:
                logger.debug(f"读取失败: {e}")
        if not is_usb and bus_type == 'usb':
            is_usb = True

        video_type = 'camera'
        name_lower = dev_name.lower()
        if 'hdmi' in name_lower or 'display' in name_lower or 'capture' in name_lower:
            if 'hdmi' in name_lower:
                video_type = 'hdmi_capture'
            else:
                video_type = 'capture_card'
        elif 'loopback' in name_lower:
            video_type = 'loopback'
        elif 'virtual' in name_lower:
            video_type = 'virtual'

        friendly_name = dev_name
        if vendor_id and product_id:
            friendly_name = f"{dev_name} ({vendor_id}:{product_id})"

        v4l2_width = 0
        v4l2_height = 0
        v4l2_fps = 0
        v4l2_pixel_format = ''
        v4l2_formats = []

        dev_node = f"/dev/{entry}"
        fmt_result = run_command(f"{platform_paths.CMD_V4L2_CTL} --device={dev_node} --get-fmt-video 2>/dev/null", timeout=3)
        if fmt_result['success'] and fmt_result['stdout']:
            for line in fmt_result['stdout'].splitlines():
                if 'Width/Height' in line:
                    m = re.search(r'(\d+)/(\d+)', line)
                    if m:
                        v4l2_width = int(m.group(1))
                        v4l2_height = int(m.group(2))
                elif 'Pixel Format' in line:
                    m = re.search(r"'([^']+)'", line)
                    if m:
                        v4l2_pixel_format = m.group(1)

        parm_result = run_command(f"{platform_paths.CMD_V4L2_CTL} --device={dev_node} --get-parm 2>/dev/null", timeout=3)
        if parm_result['success'] and parm_result['stdout']:
            for line in parm_result['stdout'].splitlines():
                if 'Frames per second' in line or 'fps' in line.lower():
                    m = re.search(r'([\d.]+)', line)
                    if m:
                        v4l2_fps = float(m.group(1))

        enum_result = run_command(f"{platform_paths.CMD_V4L2_CTL} --device={dev_node} --list-formats 2>/dev/null", timeout=3)
        if enum_result['success'] and enum_result['stdout']:
            for line in enum_result['stdout'].splitlines():
                m = re.search(r"'([^']+)'", line)
                if m and m.group(1) not in v4l2_formats:
                    v4l2_formats.append(m.group(1))

        devices.append({
            'name': f"v4l2_{entry}",
            'friendly_name': friendly_name,
            'node_id': None,
            'video_type': video_type,
            'role': 'source',
            'media_class': 'Video/Source',
            'source': 'V4L2',
            'width': v4l2_width,
            'height': v4l2_height,
            'fps': v4l2_fps,
            'pixel_format': v4l2_pixel_format,
            'formats': v4l2_formats,
            'extended': {
                'v4l2_device': f"/dev/{entry}",
                'v4l2_name': dev_name,
                'v4l2_caps': dev_caps,
                'vendor_id': vendor_id,
                'product_id': product_id,
                'is_usb': is_usb,
                'bus_type': bus_type,
            },
        })

    return devices


def _expand_drm_device_info(dd):
    # 扩展 DRM 设备信息，添加更多硬件属性
    connector_name = dd.get('name', '').replace('drm_', '', 1)
    connector_dir = f"/sys/class/drm/{connector_name}"

    # EDID 解析：显示器名称、物理尺寸
    edid_monitor_name = ''
    edid_physical_size = ''
    edid_path = f"{connector_dir}/edid"
    if os.path.exists(edid_path):
        try:
            with open(edid_path, 'rb') as f:
                edid_data = f.read()
            if len(edid_data) >= 128:
                width_mm, height_mm = parse_edid_physical_size(edid_data)
                if width_mm > 0 and height_mm > 0:
                    edid_physical_size = f"{width_mm}x{height_mm} mm"
                edid_monitor_name = parse_edid_monitor_name(edid_data)
        except (OSError, IOError) as e:
            logger.debug(f"读取失败: {e}")

    # 分辨率列表
    modes = []
    modes_result = run_command(f"cat {connector_dir}/modes 2>/dev/null", timeout=2)
    if modes_result['success'] and modes_result['stdout']:
        modes = [m.strip() for m in modes_result['stdout'].splitlines() if m.strip()]

    # DPMS 状态
    dpms_status = ''
    dpms_path = f"{connector_dir}/dpms"
    if os.path.exists(dpms_path):
        try:
            with open(dpms_path, 'r') as f:
                dpms_status = f.read().strip()
        except (OSError, IOError) as e:
            logger.debug(f"读取失败: {e}")

    # 连接器类型和索引
    card_part = connector_name.split('-', 1)
    conn_type_part = card_part[1] if len(card_part) >= 2 else connector_name
    conn_type_parts = conn_type_part.split('-')
    conn_type = conn_type_parts[0].lower()
    conn_index = conn_type_parts[1] if len(conn_type_parts) >= 2 else '0'

    drm_enabled = ''
    enabled_path = f"{connector_dir}/enabled"
    if os.path.exists(enabled_path):
        try:
            with open(enabled_path, 'r') as f:
                drm_enabled = f.read().strip()
        except (OSError, IOError) as e:
            logger.debug(f"读取失败: {e}")

    dd['formats'] = modes
    dd['extended'] = {
        'edid_monitor_name': edid_monitor_name,
        'edid_physical_size': edid_physical_size,
        'connector': connector_name,
        'connector_type': conn_type,
        'connector_index': conn_index,
        'dpms_status': dpms_status,
        'drm_status': dd.get('drm_status', 'connected'),
        'drm_enabled': drm_enabled,
    }

    # 如果有 EDID 显示器名称，更新友好名
    if edid_monitor_name and 'HDMI -' not in dd.get('friendly_name', ''):
        dd['friendly_name'] = f"{dd.get('friendly_name', '').upper()} - {edid_monitor_name}"
    return dd


# 扫描所有视频设备
def scan_video_devices(force=False):
    # force=True 时跳过缓存，强制重新扫描
    if not force:
        cached = config.get_video_devices()
        if cached:
            default_name = get_default_video_device()
            for dev in cached:
                dev['is_default'] = (dev.get('name') == default_name)
            return {'devices': cached, 'default': default_name}

    pw_data = pw_dump()
    nodes = _find_video_nodes(pw_data)

    devices = []
    for n in nodes:
        dev = _get_node_info(n)
        props = n.get('info', {}).get('props', {})
        device_id_prop = props.get('device.id')
        device_props = find_device_props(pw_data, device_id_prop) if device_id_prop is not None else {}

        # 用 device_props 重新分类（更准确）
        dev['video_type'] = _classify_video_type(dev['name'], props, device_props)

        dev['extended'] = {
            'factory.name': get_prop_with_fallback(props, device_props, 'factory.name'),
            'device.api': get_prop_with_fallback(props, device_props, 'device.api'),
            'device.bus': get_prop_with_fallback(props, device_props, 'device.bus'),
            'device.bus_path': get_prop_with_fallback(props, device_props, 'device.bus-path'),
            'device.vendor.id': get_prop_with_fallback(props, device_props, 'device.vendor.id'),
            'device.product.id': get_prop_with_fallback(props, device_props, 'device.product.id'),
            'object.serial': get_prop_with_fallback(props, device_props, 'object.serial'),
            'priority.session': get_prop_with_fallback(props, device_props, 'priority.session'),
            'priority.driver': get_prop_with_fallback(props, device_props, 'priority.driver'),
            'device.form_factor': get_prop_with_fallback(props, device_props, 'device.form-factor'),
            'device.icon_name': get_prop_with_fallback(props, device_props, 'device.icon-name'),
            'device.description': get_prop_with_fallback(props, device_props, 'device.description'),
            'node.driver': get_prop_with_fallback(props, device_props, 'node.driver'),
        }
        devices.append(dev)

    drm_devices = _get_drm_displays()
    pw_names = {d.get('name') for d in devices}
    for dd in drm_devices:
        if dd.get('name') not in pw_names:
            dd = _expand_drm_device_info(dd)
            devices.append(dd)

    v4l2_devices = _get_v4l2_devices()
    all_names = {d.get('name') for d in devices}
    for vd in v4l2_devices:
        if vd.get('name') not in all_names:
            devices.append(vd)

    default_name = get_default_video_device()
    for dev in devices:
        dev['is_default'] = (dev.get('name') == default_name)

    result = {'devices': devices, 'default': default_name}

    try:
        config.set_video_devices(devices)
    except Exception as e:
        logger.warning(f"缓存视频设备失败: {e}")

    logger.debug(f"扫描视频设备完成: {len(devices)} 个 (PipeWire: {len(nodes)}, DRM: {len(drm_devices)})")
    return result


# 获取视频设备列表
def get_video_devices():
    return scan_video_devices()


def _get_drm_displays():
    # 从 /sys/class/drm 读取已连接的显示器信息
    devices = []
    result = run_command(
        f"for f in {platform_paths.SYS_DRM}/*/status; do "
        "  s=$(cat \"$f\" 2>/dev/null); "
        "  echo \"$f:$s\"; "
        "done 2>/dev/null",
        timeout=3
    )
    if not result['success'] or not result['stdout']:
        return devices
    for line in result['stdout'].splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(':', 1)
        if len(parts) != 2:
            continue
        status_path = parts[0]
        status = parts[1].strip()
        if status != 'connected':
            continue
        connector_dir = status_path.rsplit('/', 1)[0] if '/' in status_path else ''
        connector = status_path.split('/')[-2] if '/' in status_path else ''
        if not connector or connector == 'drm':
            continue
        try:
            card_part = connector.split('-', 1)
            if len(card_part) < 2:
                continue
            conn_type_part = card_part[1]
            conn_type = conn_type_part.split('-')[0].lower()

            if 'hdmi' in conn_type:
                video_type = 'hdmi'
            elif 'dp' in conn_type or 'displayport' in conn_type:
                video_type = 'displayport'
            elif 'edp' in conn_type or 'lvds' in conn_type or 'dsi' in conn_type:
                video_type = 'display'
            elif 'vga' in conn_type:
                video_type = 'vga'
            elif 'virtual' in conn_type:
                video_type = 'virtual'
            else:
                video_type = 'display'

            name = f"drm_{connector}"
            connector_upper = conn_type_part.upper()
            friendly_name = f"{connector_upper}"

            edid_path = f"{connector_dir}/edid"
            monitor_name = ''
            try:
                with open(edid_path, 'rb') as f:
                    edid_data = f.read()
                monitor_name = parse_edid_monitor_name(edid_data)
            except (IOError, OSError):
                monitor_name = ''
            if monitor_name:
                friendly_name = f"{connector_upper} - {monitor_name}"

            resos = []
            disp_w = 0
            disp_h = 0
            disp_fps = 0
            disp_pixel_format = ''
            modes_result = run_command(f"cat {connector_dir}/modes 2>/dev/null", timeout=3)
            if modes_result['success'] and modes_result['stdout']:
                for m_line in modes_result['stdout'].splitlines():
                    m_line = m_line.strip()
                    if m_line:
                        resos.append(m_line)
                        if disp_w == 0 and 'x' in m_line:
                            mode_base = m_line.split('@')[0] if '@' in m_line else m_line
                            size_parts = mode_base.split('x')
                            try:
                                disp_w = int(size_parts[0])
                                dh = size_parts[1].split()[0] if ' ' in size_parts[1] else size_parts[1]
                                disp_h = int(dh)
                            except (ValueError, IndexError):
                                pass
                        if disp_fps == 0 and '@' in m_line:
                            hz_match = re.search(r'@([\d.]+)Hz?', m_line, re.IGNORECASE)
                            if hz_match:
                                try:
                                    disp_fps = float(hz_match.group(1))
                                except ValueError:
                                    pass

            if disp_fps == 0:
                modetest_result = run_command(f"{platform_paths.CMD_MODETEST} -c 2>/dev/null", timeout=5)
                if modetest_result['success'] and modetest_result['stdout']:
                    in_connector = False
                    for mt_line in modetest_result['stdout'].splitlines():
                        if connector in mt_line:
                            in_connector = True
                            continue
                        if in_connector:
                            if mt_line.strip().startswith('#') or 'x' in mt_line:
                                hz_match = re.search(r'(\d+\.?\d*)\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+', mt_line)
                                if hz_match:
                                    try:
                                        disp_fps = float(hz_match.group(1))
                                    except ValueError:
                                        pass
                                    break
                                hz_simple = re.search(r'(\d+\.?\d*)\s*Hz', mt_line)
                                if hz_simple:
                                    try:
                                        disp_fps = float(hz_simple.group(1))
                                    except ValueError:
                                        pass
                                    break
                            elif mt_line.strip() and not mt_line.strip().startswith('#') and not mt_line.strip().startswith(' '):
                                in_connector = False

            if disp_fps == 0:
                debug_path = None
                for dri_dir in sorted(os.listdir('/sys/kernel/debug/dri/')):
                    state_file = f"/sys/kernel/debug/dri/{dri_dir}/state"
                    if os.path.exists(state_file):
                        debug_path = state_file
                        break
                if debug_path:
                    state_result = run_command(f"cat {debug_path} 2>/dev/null", timeout=3)
                    if state_result['success'] and state_result['stdout']:
                        found_connector = False
                        for st_line in state_result['stdout'].splitlines():
                            if connector in st_line:
                                found_connector = True
                            if found_connector:
                                if 'vrefresh' in st_line.lower() or 'refresh' in st_line.lower():
                                    v_match = re.search(r'(\d+)', st_line)
                                    if v_match:
                                        try:
                                            disp_fps = float(v_match.group(1))
                                        except ValueError:
                                            pass
                                        break

            if disp_fps == 0:
                xrandr_result = run_command(
                    f"{platform_paths.CMD_XRANDR} --current 2>/dev/null | grep -i '{conn_type_part}'",
                    timeout=3
                )
                if xrandr_result['success'] and xrandr_result['stdout']:
                    for xr_line in xrandr_result['stdout'].splitlines():
                        hz_match = re.search(r'([\d.]+)\s*\*', xr_line)
                        if hz_match:
                            try:
                                disp_fps = float(hz_match.group(1))
                            except ValueError:
                                pass
                            break

            if disp_fps == 0 and disp_w > 0:
                # 刷新率未知时返回 0，前端标注"未知"
                disp_fps = 0.0

            fmt_result = run_command(f"cat {connector_dir}/format 2>/dev/null", timeout=2)
            if fmt_result['success'] and fmt_result['stdout'] and fmt_result['stdout'].strip():
                disp_pixel_format = fmt_result['stdout'].strip()
            else:
                disp_pixel_format = 'RGB'

            logger.debug(f"DRM {connector}: w={disp_w} h={disp_h} fps={disp_fps} pf={disp_pixel_format}")

            devices.append({
                'name': name,
                'friendly_name': friendly_name,
                'node_id': None,
                'video_type': video_type,
                'role': 'sink',
                'media_class': 'Video/Sink',
                'source': 'DRM',
                'width': disp_w,
                'height': disp_h,
                'fps': disp_fps,
                'pixel_format': disp_pixel_format,
                'formats': resos,
            })
        except Exception:
            logger.warning(f"跳过无法读取的 DRM 设备: {connector}", exc_info=False)
            continue

    return devices


def get_video_test_status(device_name=None):
    # 返回视频设备测试状态（当前仅返回设备列表，未实现实际预览）
    scan_result = scan_video_devices()
    devices = scan_result.get('devices', [])
    return {'success': True, 'message': '视频设备检测完成', 'devices': devices}


def get_video_device_detail(device_name):
    # 获取单个视频设备的详细信息
    # 先在 PipeWire 节点中查找
    pw_data = pw_dump()
    nodes = _find_video_nodes(pw_data)
    for obj in nodes:
        props = obj.get('info', {}).get('props', {})
        if props.get('node.name', '') == device_name:
            info = obj.get('info', {})
            params = info.get('params', {})
            node_id = obj.get('id')
            name = props.get('node.name', '')
            friendly_name = props.get('node.description', '') or props.get('node.nick', '') or name
            media_class = props.get('media.class', '')

            video_type = _classify_video_type(name, props)
            role = _classify_role(media_class)

            # 视频参数
            width = 0
            height = 0
            fps = 0
            pixel_format = ''
            formats = []

            enum_format = []
            if isinstance(params, dict):
                ef = params.get('EnumFormat', [])
                if isinstance(ef, list):
                    enum_format = ef
                elif isinstance(ef, dict):
                    enum_format = [ef]

            if enum_format:
                first = enum_format[0] if isinstance(enum_format[0], dict) else {}
                video_params = first.get('Video', first)
                width, height, fps, pixel_format = _parse_video_format(video_params)

                for ef_entry in enum_format:
                    if isinstance(ef_entry, dict):
                        vp = ef_entry.get('Video', {})
                        if isinstance(vp, dict):
                            f = vp.get('format', '')
                            if isinstance(f, str) and f not in formats:
                                formats.append(f)

            # PipeWire 扩展属性
            pw_extra = {}
            for key in ('device.api', 'device.bus', 'device.bus-path', 'device.bus-id',
                        'device.form-factor', 'device.icon-name', 'device.string',
                        'api.v4l2.path', 'api.v4l2.cap.driver', 'api.v4l2.cap.card',
                        'api.v4l2.cap.bus_info', 'factory.name', 'client.id',
                        'object.serial', 'priority.session', 'priority.driver'):
                val = props.get(key)
                if val is not None:
                    pw_extra[key] = val

            detail = {
                'name': name,
                'friendly_name': friendly_name,
                'node_id': node_id,
                'video_type': video_type,
                'role': role,
                'media_class': media_class,
                'source': 'PipeWire',
                'width': width,
                'height': height,
                'fps': fps,
                'pixel_format': pixel_format,
                'formats': formats,
                'pipewire': pw_extra,
            }
            return detail

    # 在 DRM 设备中查找
    drm_devices = _get_drm_displays()
    for dd in drm_devices:
        if dd.get('name') == device_name:
            # 补充 DRM 详细信息
            connector_name = device_name.replace('drm_', '', 1)
            connector_dir = f"/sys/class/drm/{connector_name}"

            # 读取 EDID 中的显示器名称
            edid_monitor_name = ''
            edid_path = f"{connector_dir}/edid"
            try:
                with open(edid_path, 'rb') as f:
                    edid_data = f.read()
                edid_monitor_name = parse_edid_monitor_name(edid_data)
            except (IOError, OSError):
                edid_monitor_name = ''

            # 读取完整 modes 列表
            modes = []
            modes_result = run_command(f"cat {connector_dir}/modes 2>/dev/null", timeout=3)
            if modes_result['success'] and modes_result['stdout']:
                for m_line in modes_result['stdout'].splitlines():
                    m_line = m_line.strip()
                    if m_line:
                        modes.append(m_line)

            # 读取 DRM 连接状态
            drm_status = 'unknown'
            status_result = run_command(f"cat {connector_dir}/status 2>/dev/null", timeout=3)
            if status_result['success'] and status_result['stdout']:
                drm_status = status_result['stdout'].strip()

            # 解析 connector 类型
            card_part = connector_name.split('-', 1)
            conn_type_part = card_part[1] if len(card_part) >= 2 else connector_name
            connector_type = conn_type_part.split('-')[0].lower()

            detail = {
                'name': dd.get('name', ''),
                'friendly_name': dd.get('friendly_name', ''),
                'node_id': dd.get('node_id'),
                'video_type': dd.get('video_type', ''),
                'role': dd.get('role', 'sink'),
                'media_class': dd.get('media_class', 'Video/Sink'),
                'source': dd.get('source', 'DRM'),
                'width': dd.get('width', 0),
                'height': dd.get('height', 0),
                'fps': dd.get('fps', 0),
                'pixel_format': dd.get('pixel_format', ''),
                'formats': dd.get('formats', []),
                'drm': {
                    'connector': connector_name,
                    'connector_type': connector_type,
                    'edid_monitor_name': edid_monitor_name,
                    'modes': modes,
                    'drm_status': drm_status,
                },
            }
            return detail

    # 在 V4L2 设备中查找
    v4l2_devices = _get_v4l2_devices()
    for vd in v4l2_devices:
        if vd.get('name') == device_name:
            # 补充 V4L2 详细信息
            detail = {
                'name': vd.get('name', ''),
                'friendly_name': vd.get('friendly_name', ''),
                'node_id': vd.get('node_id'),
                'video_type': vd.get('video_type', ''),
                'role': vd.get('role', 'source'),
                'media_class': vd.get('media_class', 'Video/Source'),
                'source': vd.get('source', 'V4L2'),
                'width': vd.get('width', 0),
                'height': vd.get('height', 0),
                'fps': vd.get('fps', 0),
                'pixel_format': vd.get('pixel_format', ''),
                'formats': vd.get('formats', []),
                'v4l2': vd.get('extended', {}),
            }
            return detail

    # 都找不到
    raise DeviceNotFoundError(f'设备 {device_name} 未找到')


def set_display_output(target_connector, resolution=None, refresh_rate=None):
    """配置 DRM 显示输出，使用 xrandr 设置分辨率和刷新率。
    适用于 PipeWire 不直接管理的显示输出。"""
    if not target_connector:
        raise InvalidParamError('target_connector 不能为空')

    # 验证连接器是否存在
    connector_dir = os.path.join(platform_paths.SYS_DRM, target_connector)
    if not os.path.exists(connector_dir):
        raise DeviceNotFoundError(f'连接器 {target_connector} 不存在')

    # 检查连接状态
    status_path = f"{connector_dir}/status"
    if os.path.exists(status_path):
        try:
            with open(status_path, 'r') as f:
                status = f.read().strip()
            if status != 'connected':
                raise DeviceNotFoundError(f'连接器 {target_connector} 状态为 {status}，未连接')
        except (IOError, OSError):
            pass

    # 构建 xrandr 命令
    # 提取连接器名（去掉 card 前缀，如 card0-HDMI-A-1 -> HDMI-A-1）
    parts = target_connector.split('-', 1)
    xrandr_connector = parts[1] if len(parts) >= 2 else target_connector

    cmd_parts = [platform_paths.CMD_XRANDR, '--output', xrandr_connector]

    if resolution:
        # 验证分辨率格式
        if not re.match(r'^\d+x\d+$', resolution):
            raise InvalidParamError(f'分辨率格式无效: {resolution}，应为 WxH')
        mode_str = resolution
        if refresh_rate:
            try:
                float(refresh_rate)
                mode_str = f"{resolution}_{refresh_rate}"
            except (ValueError, TypeError):
                raise InvalidParamError(f'刷新率格式无效: {refresh_rate}')
        cmd_parts.extend(['--mode', mode_str])
    else:
        cmd_parts.append('--auto')

    if refresh_rate and resolution:
        cmd_parts.extend(['--rate', str(refresh_rate)])

    cmd_str = ' '.join(shlex.quote(p) for p in cmd_parts)
    logger.debug(f"执行显示配置命令: {cmd_str}")

    result = run_command(cmd_str, timeout=10)
    if not result['success']:
        # xrandr 失败后无有效回退，返回错误
        raise CommandError(f"显示输出配置失败：xrandr 不可用或配置无效（connector={target_connector}, resolution={resolution}）")

    logger.info(f"显示输出 {target_connector} 已配置: resolution={resolution}, refresh_rate={refresh_rate}")
    return {
        'connector': target_connector,
        'resolution': resolution,
        'refresh_rate': refresh_rate,
        'method': 'xrandr',
        'message': f'已配置 {target_connector}',
    }


def set_display_layout(output, relation, relative_to=None):
    """配置多显示器布局关系

    Args:
        output: 目标输出连接器（如 HDMI-A-1）
        relation: 布局关系，可选 left-of/right-of/above/below/same-as/primary
        relative_to: 相对目标连接器（same-as/left-of/right-of/above/below 时必填）
    Returns:
        dict: 布局配置结果
    """
    valid_relations = ('left-of', 'right-of', 'above', 'below', 'same-as', 'primary')
    if relation not in valid_relations:
        raise InvalidParamError(f'布局关系无效: {relation}，可选: {", ".join(valid_relations)}')
    if not output:
        raise InvalidParamError('output 参数必填')
    if relation != 'primary' and not relative_to:
        raise InvalidParamError(f'{relation} 关系需要指定 relative_to 参数')

    # 提取连接器名
    def _to_xrandr(conn):
        parts = conn.split('-', 1)
        return parts[1] if len(parts) >= 2 else conn

    xrandr_output = _to_xrandr(output)
    cmd_parts = [platform_paths.CMD_XRANDR, '--output', xrandr_output]

    if relation == 'primary':
        cmd_parts.extend(['--primary'])
    else:
        xrandr_relative = _to_xrandr(relative_to)
        cmd_parts.extend([f'--{relation}', xrandr_relative])

    cmd_str = ' '.join(shlex.quote(p) for p in cmd_parts)
    result = run_command(cmd_str, timeout=10)
    if not result['success']:
        raise CommandError(f'xrandr 布局设置失败: {result.get("stderr", "")[:200]}')

    logger.info(f"显示布局: {output} {relation} {relative_to or ''}")
    return {
        'output': output,
        'relation': relation,
        'relative_to': relative_to,
        'message': f'{output} 已设为 {relation} {relative_to or "主显示器"}',
    }


def get_v4l2_controls(device_name):
    """获取 V4L2 设备的可调参数列表

    Args:
        device_name: 设备名（如 v4l2_video0）
    Returns:
        list: 参数列表，每项含 name/type/min/max/step/default/value
    """
    dev_node = _v4l2_dev_node(device_name)
    if not dev_node:
        raise InvalidParamError(f'无效的 V4L2 设备: {device_name}')
    result = run_command(
        f"{platform_paths.CMD_V4L2_CTL} --device={dev_node} --list-ctrls 2>/dev/null",
        timeout=5)
    if not result['success']:
        return []
    controls = []
    for line in result['stdout'].splitlines():
        # 格式: brightness (int) : min=0 max=255 step=1 default=128 value=128
        m = re.match(r'\s*(\w+)\s+\((\w+)\)\s*:\s*(.*)', line)
        if not m:
            continue
        name, ctrl_type, rest = m.group(1), m.group(2), m.group(3)
        ctrl = {'name': name, 'type': ctrl_type}
        for prop in ('min', 'max', 'step', 'default', 'value'):
            pm = re.search(rf'{prop}=([^\s,]+)', rest)
            if pm:
                ctrl[prop] = int(pm.group(1)) if ctrl_type == 'int' else pm.group(1)
        if 'value' in ctrl:
            controls.append(ctrl)
    return controls


def set_v4l2_control(device_name, control_name, value):
    """设置 V4L2 设备参数

    Args:
        device_name: 设备名（如 v4l2_video0）
        control_name: 参数名（如 brightness）
        value: 参数值
    Returns:
        dict: 设置结果
    """
    dev_node = _v4l2_dev_node(device_name)
    if not dev_node:
        raise InvalidParamError(f'无效的 V4L2 设备: {device_name}')
    if not control_name:
        raise InvalidParamError('control_name 参数必填')
    if not re.match(r'^[a-zA-Z_]\w*$', control_name):
        raise InvalidParamError(f'无效的参数名: {control_name}')
    # 验证 value 为整数或浮点数，防止命令注入
    try:
        if '.' in str(value):
            safe_value = float(value)
        else:
            safe_value = int(value)
    except (ValueError, TypeError):
        raise InvalidParamError(f'无效的参数值: {value}，必须为数字')
    result = run_command(
        f"{platform_paths.CMD_V4L2_CTL} --device={dev_node} --set-ctrl={control_name}={safe_value} 2>/dev/null",
        timeout=5)
    if not result['success']:
        raise CommandError(f'设置参数失败: {result.get("stderr", "")[:200]}')
    return {'device': device_name, 'control': control_name, 'value': value}


def _v4l2_dev_node(device_name):
    """从设备名提取 /dev/videoN 路径"""
    if not device_name or not device_name.startswith('v4l2_'):
        return None
    entry = device_name[5:]  # v4l2_video0 -> video0
    if not re.match(r'^video\d+$', entry):
        return None
    return f"/dev/{entry}"


def get_v4l2_formats(device_name):
    """获取 V4L2 设备支持的分辨率和帧率列表

    Args:
        device_name: 设备名（如 v4l2_video0）
    Returns:
        dict: 包含支持的格式列表，每项含 pixel_format、分辨率、帧率
    """
    dev_node = _v4l2_dev_node(device_name)
    if not dev_node:
        raise InvalidParamError(f'无效的 V4L2 设备: {device_name}')

    # 获取支持的像素格式
    fmt_result = run_command(
        f"{platform_paths.CMD_V4L2_CTL} --device={dev_node} --list-formats 2>/dev/null",
        timeout=5)
    if not fmt_result['success']:
        return {'formats': []}

    formats = []
    for line in fmt_result['stdout'].splitlines():
        m = re.search(r'\[[^\]]*\]\s*:\s*\'([^\']+)\'\s*\(([^)]+)\)', line)
        if not m:
            continue
        pixel_format = m.group(1)
        description = m.group(2)

        # 获取该格式支持的分辨率
        sizes_result = run_command(
            f"{platform_paths.CMD_V4L2_CTL} --device={dev_node} --list-framesizes={pixel_format} 2>/dev/null",
            timeout=5)
        resolutions = []
        if sizes_result['success']:
            for sline in sizes_result['stdout'].splitlines():
                size_match = re.search(r'(\d+)x(\d+)', sline)
                if size_match:
                    w, h = int(size_match.group(1)), int(size_match.group(2))
                    # 获取该分辨率的帧率
                    intervals_result = run_command(
                        f"{platform_paths.CMD_V4L2_CTL} --device={dev_node} --list-frameintervals width={w},height={h},pixelformat={pixel_format} 2>/dev/null",
                        timeout=5)
                    frame_rates = []
                    if intervals_result['success']:
                        for iline in intervals_result['stdout'].splitlines():
                            fps_match = re.search(r'(\d+)/(\d+)', iline)
                            if fps_match:
                                num, den = int(fps_match.group(1)), int(fps_match.group(2))
                                if den > 0:
                                    frame_rates.append(round(num / den, 1))
                    resolutions.append({
                        'width': w,
                        'height': h,
                        'frame_rates': sorted(set(frame_rates), reverse=True),
                    })

        formats.append({
            'pixel_format': pixel_format,
            'description': description,
            'resolutions': resolutions,
        })

    return {'formats': formats}


def set_v4l2_format(device_name, width=None, height=None, pixel_format=None):
    """设置 V4L2 设备的视频格式（分辨率和像素格式）

    Args:
        device_name: 设备名（如 v4l2_video0）
        width: 宽度
        height: 高度
        pixel_format: 像素格式（如 YUYV, MJPEG）
    Returns:
        dict: 设置结果
    """
    dev_node = _v4l2_dev_node(device_name)
    if not dev_node:
        raise InvalidParamError(f'无效的 V4L2 设备: {device_name}')

    cmd_parts = [f"{platform_paths.CMD_V4L2_CTL} --device={dev_node}"]

    if width and height:
        if not re.match(r'^\d+$', str(width)) or not re.match(r'^\d+$', str(height)):
            raise InvalidParamError('分辨率必须为正整数')
        cmd_parts.append(f"--set-fmt-video=width={width},height={height}")

    if pixel_format:
        if not re.match(r'^[a-zA-Z0-9]+$', pixel_format):
            raise InvalidParamError(f'无效的像素格式: {pixel_format}')
        if width and height:
            cmd_parts[1] += f",pixelformat={pixel_format}"
        else:
            cmd_parts.append(f"--set-fmt-video=pixelformat={pixel_format}")

    if len(cmd_parts) == 1:
        raise InvalidParamError('至少需要指定分辨率或像素格式')

    result = run_command(' '.join(cmd_parts) + ' 2>/dev/null', timeout=5)
    if not result['success']:
        raise CommandError(f'设置视频格式失败: {result.get("stderr", "")[:200]}')

    # 读取设置后的格式验证
    verify = run_command(
        f"{platform_paths.CMD_V4L2_CTL} --device={dev_node} --get-fmt-video 2>/dev/null",
        timeout=5)
    current = {}
    if verify['success']:
        fmt_m = re.search(r'Pixel Format\s*:\s*\'([^\']+)\'', verify['stdout'])
        size_m = re.search(r'Size\s*:\s*(\d+)x(\d+)', verify['stdout'])
        if fmt_m:
            current['pixel_format'] = fmt_m.group(1)
        if size_m:
            current['width'] = int(size_m.group(1))
            current['height'] = int(size_m.group(2))

    logger.info(f"V4L2 格式设置: {device_name} -> {current}")
    return {'device': device_name, 'current': current}


def set_v4l2_framerate(device_name, fps):
    """设置 V4L2 设备的帧率

    Args:
        device_name: 设备名（如 v4l2_video0）
        fps: 帧率（如 30, 60）
    Returns:
        dict: 设置结果
    """
    dev_node = _v4l2_dev_node(device_name)
    if not dev_node:
        raise InvalidParamError(f'无效的 V4L2 设备: {device_name}')
    try:
        fps = int(fps)
        if fps <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise InvalidParamError(f'无效的帧率: {fps}，必须为正整数')

    result = run_command(
        f"{platform_paths.CMD_V4L2_CTL} --device={dev_node} --set-parm={fps} 2>/dev/null",
        timeout=5)
    if not result['success']:
        raise CommandError(f'设置帧率失败: {result.get("stderr", "")[:200]}')

    logger.info(f"V4L2 帧率设置: {device_name} -> {fps}fps")
    return {'device': device_name, 'fps': fps}


def set_display_rotation(output, rotation):
    """设置显示器旋转方向

    Args:
        output: 连接器名（如 HDMI-A-1）
        rotation: 旋转方向，可选 normal/left/right/inverted
    Returns:
        dict: 设置结果
    """
    valid_rotations = ('normal', 'left', 'right', 'inverted')
    if rotation not in valid_rotations:
        raise InvalidParamError(f'旋转方向无效: {rotation}，可选: {", ".join(valid_rotations)}')
    if not output:
        raise InvalidParamError('output 参数必填')

    parts = output.split('-', 1)
    xrandr_output = parts[1] if len(parts) >= 2 else output

    result = run_command(
        f"{platform_paths.CMD_XRANDR} --output {shlex.quote(xrandr_output)} --rotate {rotation}",
        timeout=10)
    if not result['success']:
        raise CommandError(f'xrandr 旋转设置失败: {result.get("stderr", "")[:200]}')

    logger.info(f"显示旋转: {output} -> {rotation}")
    return {'output': output, 'rotation': rotation, 'message': f'{output} 已旋转为 {rotation}'}


def set_display_scale(output, scale):
    """设置显示器缩放比例

    Args:
        output: 连接器名（如 HDMI-A-1）
        scale: 缩放比例（如 1.5, 2.0），范围 0.1-4.0
    Returns:
        dict: 设置结果
    """
    if not output:
        raise InvalidParamError('output 参数必填')
    try:
        scale = float(scale)
    except (ValueError, TypeError):
        raise InvalidParamError(f'缩放比例无效: {scale}')
    if scale < 0.1 or scale > 4.0:
        raise InvalidParamError('缩放比例范围: 0.1 ~ 4.0')

    parts = output.split('-', 1)
    xrandr_output = parts[1] if len(parts) >= 2 else output

    result = run_command(
        f"{platform_paths.CMD_XRANDR} --output {shlex.quote(xrandr_output)} --scale {scale}x{scale}",
        timeout=10)
    if not result['success']:
        raise CommandError(f'xrandr 缩放设置失败: {result.get("stderr", "")[:200]}')

    logger.info(f"显示缩放: {output} -> {scale}x{scale}")
    return {'output': output, 'scale': scale, 'message': f'{output} 缩放已设为 {scale}x{scale}'}


def get_default_video_device():
    # 获取默认视频设备名，优先从配置读取，否则查询 pw-metadata
    saved = config.get_default_video_sink()
    if saved:
        return saved
    # 通过 pw-metadata 查询当前默认视频 sink
    result = run_command("pw-metadata -n settings 2>/dev/null | grep 'default.video.sink'", timeout=5)
    if result['success'] and result['stdout']:
        match = re.search(r"value:\s*[\"']([^\"']+)[\"']", result['stdout'])
        if match:
            return match.group(1)
    return ''


def set_default_video_device(device_name):
    # 设置默认视频设备
    if not device_name:
        raise InvalidParamError('设备名不能为空')

    # 先尝试通过 PipeWire 节点查找
    pw_data = pw_dump()
    node = find_pw_node(pw_data, name=device_name)
    if node:
        node_id = node.get('id')
        if node_id is not None:
            result = run_command(f"wpctl set-default {node_id}", timeout=5)
            if result['success']:
                config.set_default_video_sink(device_name)
                return f'默认视频设备已设为: {device_name}'

    # 尝试通过 DRM 设备名匹配（DRM 设备无 node_id，仅持久化配置）
    drm_devices = _get_drm_displays()
    for dd in drm_devices:
        if dd.get('name') == device_name:
            config.set_default_video_sink(device_name)
            return f'默认视频设备已设为: {device_name}（DRM 设备，仅持久化配置）'

    raise DeviceNotFoundError(f'设备 {device_name} 未找到')
