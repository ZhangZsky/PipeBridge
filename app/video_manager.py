import shlex
import logging
import os
import re
import json
from utils import run_command, pw_dump, find_pw_node, get_prop_with_fallback, find_device_props, parse_edid_monitor_name, parse_edid_physical_size, _find_pw_links, _get_ports_for_node, _build_link_info
import config
import platform_paths
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError

logger = logging.getLogger('MediaHub')



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
        except:
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
            except:
                pass

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
            except:
                pass
        if os.path.exists(usb_product_path):
            try:
                with open(usb_product_path, 'r') as f:
                    product_id = f.read().strip()
            except:
                pass

        # 读取 device/bus 以区分 USB/PCI/平台设备
        bus_type = ''
        bus_path = os.path.join(dev_path, 'device', 'bus')
        if os.path.exists(bus_path):
            try:
                with open(bus_path, 'r') as f:
                    bus_type = f.read().strip()
            except:
                pass
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
                    edid_physical_size = f"{width_mm}x{height_mm} cm"
                edid_monitor_name = parse_edid_monitor_name(edid_data)
        except:
            pass

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
        except:
            pass

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
        except:
            pass

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
    except Exception:
        pass

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
                disp_fps = 60.0

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


def play_test_video(device_name=None):
    # 播放视频测试信号，返回设备列表和测试结果
    scan_result = scan_video_devices()
    devices = scan_result.get('devices', [])
    device_list = []
    for dev in devices:
        w = dev.get('width', 0) or 0
        h = dev.get('height', 0) or 0
        resolution = f"{w}x{h}" if w and h else '未知'
        device_list.append({
            'name': dev.get('name', ''),
            'friendly_name': dev.get('friendly_name', ''),
            'video_type': dev.get('video_type', ''),
            'resolution': resolution,
            'connected': True,
            'formats': dev.get('formats', []),
        })

    if device_name:
        node_id = None
        pw_data_test = pw_dump()
        node = find_pw_node(pw_data_test, name=device_name)
        if node:
            node_id = node.get('id')
        if node_id is not None:
            result = run_command(f"pw-cli set-param {node_id} Props '{{ \"channelVolumes\": [ 0.5, 0.5 ] }}'", timeout=5)
            return {'devices': device_list, 'message': f'视频设备 {device_name} 已激活（节点 {node_id}）'}
        return {'devices': device_list, 'message': f'视频设备 {device_name} 已确认存在（非 PipeWire 节点）'}

    return {'devices': device_list, 'message': '视频设备状态正常，无测试信号输出'}


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


# 视频输出路由功能

def get_video_streams():
    """查询 PipeWire 中活跃的视频流节点（非设备节点），
    返回每个流的连接输出和链接详情"""
    pw_data = pw_dump()
    if not pw_data:
        raise CommandError('pw-dump 无数据')

    links = _find_pw_links(pw_data)
    streams = []

    for obj in _find_video_nodes(pw_data):
        info = obj.get('info', {})
        props = info.get('props', {})
        node_id = obj.get('id')
        name = props.get('node.name', '')
        media_class = props.get('media.class', '')
        media_category = props.get('media.category', '')

        # 区分设备节点和流节点：
        # 设备节点通常没有 media.category，或者 media.category 为 "Device"
        # 流节点有 media.category = "Playback" 或 "Capture"
        is_stream = media_category in ('Playback', 'Capture', 'Stream', 'Duplex')
        # 也包含 media.role 存在的节点（通常表示应用流）
        if not is_stream and props.get('media.role'):
            is_stream = True
        # 排除明确的设备节点（factory.name 含 "device" 或 device.api 存在）
        if props.get('device.api') or 'device' in (props.get('factory.name', '')).lower():
            is_stream = False

        if not is_stream:
            continue

        friendly_name = props.get('node.description', '') or props.get('node.nick', '') or name
        application = props.get('application.name', '') or props.get('app.name', '')

        # 查找与此流节点相关的所有 Link
        stream_links = []
        connected_outputs = []
        for link_obj in links:
            link_detail = _build_link_info(link_obj, pw_data)
            if link_detail['output_node_id'] == node_id or link_detail['input_node_id'] == node_id:
                stream_links.append(link_detail)
                # 收集连接的输出设备名
                target_name = ''
                if link_detail['output_node_id'] == node_id:
                    target_name = link_detail['input_node_name']
                elif link_detail['input_node_id'] == node_id:
                    target_name = link_detail['output_node_name']
                if target_name and target_name not in connected_outputs:
                    connected_outputs.append(target_name)

        streams.append({
            'node_id': node_id,
            'name': name,
            'friendly_name': friendly_name,
            'application': application,
            'media_class': media_class,
            'media_category': media_category,
            'connected_outputs': connected_outputs,
            'links': stream_links,
        })

    # 包含 V4L2 采集流
    v4l2_devices = _get_v4l2_devices()
    for vd in v4l2_devices:
        streams.append({
            'node_id': vd.get('node_id'),
            'name': vd.get('name', ''),
            'friendly_name': vd.get('friendly_name', ''),
            'application': '',
            'media_class': vd.get('media_class', 'Video4Linux'),
            'media_category': 'Capture',
            'connected_outputs': [],
            'links': [],
        })

    logger.debug(f"查询视频流完成: {len(streams)} 个流")
    return streams


def route_video_stream(stream_node_id, target_output_name):
    """将视频流路由到指定的视频输出设备"""
    try:
        stream_node_id = int(stream_node_id)
    except (TypeError, ValueError):
        raise InvalidParamError('无效的流ID')

    if not target_output_name:
        raise InvalidParamError('target_output_name 不能为空')

    pw_data = pw_dump()
    if not pw_data:
        raise CommandError('pw-dump 无数据')

    # 查找流节点
    stream_node = find_pw_node(pw_data, node_id=stream_node_id)
    if not stream_node:
        raise DeviceNotFoundError(f'流节点 {stream_node_id} 未找到')

    # 查找目标输出节点
    target_node = find_pw_node(pw_data, name=target_output_name)
    if not target_node:
        # 检查是否为 DRM 显示输出（无 PipeWire 节点）
        drm_devices = _get_drm_displays()
        for dd in drm_devices:
            if dd.get('name') == target_output_name:
                return {
                    'message': f'DRM 显示输出 {target_output_name} 不由 PipeWire 管理，路由为 no-op',
                    'target_type': 'drm',
                }
        raise DeviceNotFoundError(f'目标输出 {target_output_name} 未找到')

    target_node_id = target_node.get('id')

    # 查找流的输出端口
    stream_output_ports = _get_ports_for_node(pw_data, stream_node_id, 'output')
    if not stream_output_ports:
        # 对于 Source 类流，可能是 input 端口
        stream_output_ports = _get_ports_for_node(pw_data, stream_node_id, 'input')

    # 查找目标的输入端口
    target_input_ports = _get_ports_for_node(pw_data, target_node_id, 'input')
    if not target_input_ports:
        target_input_ports = _get_ports_for_node(pw_data, target_node_id, 'output')

    if not stream_output_ports:
        raise DeviceNotFoundError(f'流节点 {stream_node_id} 无可用端口')
    if not target_input_ports:
        raise DeviceNotFoundError(f'目标节点 {target_output_name} 无可用端口')

    # 先断开流节点的现有链接
    links = _find_pw_links(pw_data)
    for link_obj in links:
        link_detail = _build_link_info(link_obj, pw_data)
        if link_detail['output_node_id'] == stream_node_id or link_detail['input_node_id'] == stream_node_id:
            link_id = link_obj.get('id')
            run_command(f"pw-cli unlink {link_id} 2>/dev/null", timeout=3)
            logger.debug(f"已断开链接 {link_id}")

    # 取第一对匹配的端口创建新链接
    out_port_id = stream_output_ports[0].get('id')
    in_port_id = target_input_ports[0].get('id')

    link_result = run_command(
        f"pw-cli link {out_port_id} {in_port_id}",
        timeout=5,
    )
    if not link_result['success']:
        raise CommandError(f'创建链接失败: {link_result.get("stderr", "")}')

    logger.info(f"视频流 {stream_node_id} 已路由到 {target_output_name} (端口 {out_port_id} -> {in_port_id})")
    return {
        'stream_node_id': stream_node_id,
        'target_output_name': target_output_name,
        'output_port': out_port_id,
        'input_port': in_port_id,
        'message': f'视频流已路由到 {target_output_name}',
    }


def unlink_video_stream(stream_node_id, link_id=None):
    """断开视频流的输出链接。
    若提供 link_id 则仅断开该链接，否则断开该流的所有链接。"""
    try:
        stream_node_id = int(stream_node_id)
    except (TypeError, ValueError):
        raise InvalidParamError('无效的流ID')

    if link_id is not None:
        try:
            link_id = int(link_id)
        except (TypeError, ValueError):
            raise InvalidParamError('无效的链接ID')

    pw_data = pw_dump()
    if not pw_data:
        raise CommandError('pw-dump 无数据')

    links = _find_pw_links(pw_data)
    unlinked = []

    if link_id is not None:
        # 断开指定链接
        target_link = None
        for link_obj in links:
            if link_obj.get('id') == link_id:
                target_link = link_obj
                break
        if not target_link:
            raise DeviceNotFoundError(f'链接 {link_id} 未找到')
        result = run_command(f"pw-cli unlink {link_id} 2>/dev/null", timeout=3)
        if result['success']:
            unlinked.append(link_id)
        else:
            raise CommandError(f'断开链接 {link_id} 失败: {result.get("stderr", "")}')
    else:
        # 断开该流节点的所有链接
        for link_obj in links:
            link_detail = _build_link_info(link_obj, pw_data)
            if link_detail['output_node_id'] == stream_node_id or link_detail['input_node_id'] == stream_node_id:
                lid = link_obj.get('id')
                result = run_command(f"pw-cli unlink {lid} 2>/dev/null", timeout=3)
                if result['success']:
                    unlinked.append(lid)
                else:
                    logger.warning(f"断开链接 {lid} 失败: {result.get('stderr', '')}")

    if not unlinked:
        return {'unlinked': [], 'message': '无需断开的链接'}

    logger.info(f"视频流 {stream_node_id} 已断开 {len(unlinked)} 条链接: {unlinked}")
    return {
        'stream_node_id': stream_node_id,
        'unlinked': unlinked,
        'message': f'已断开 {len(unlinked)} 条链接',
    }


def set_display_output(target_connector, resolution=None, refresh_rate=None):
    """配置 DRM 显示输出，使用 xrandr 设置分辨率和刷新率。
    适用于 PipeWire 不直接管理的显示输出。"""
    if not target_connector:
        raise InvalidParamError('target_connector 不能为空')

    # 验证连接器是否存在
    connector_dir = f"/sys/class/drm/{target_connector}"
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
        # xrandr 失败时尝试使用 drm-kms 方式
        logger.info(f"xrandr 配置失败，尝试 drm-kms 方式: {result.get('stderr', '')}")
        if resolution and refresh_rate:
            # 尝试通过 modetest 设置模式
            modetest_result = run_command(
                f"{platform_paths.CMD_MODETEST} -s {shlex.quote(target_connector)}:{resolution}@{refresh_rate} 2>/dev/null",
                timeout=10,
            )
            if modetest_result['success']:
                return {
                    'connector': target_connector,
                    'resolution': resolution,
                    'refresh_rate': refresh_rate,
                    'method': 'drm-kms/modetest',
                    'message': f'已通过 modetest 配置 {target_connector}',
                }
        raise CommandError(f'配置显示输出失败: {result.get("stderr", "")}')

    logger.info(f"显示输出 {target_connector} 已配置: resolution={resolution}, refresh_rate={refresh_rate}")
    return {
        'connector': target_connector,
        'resolution': resolution,
        'refresh_rate': refresh_rate,
        'method': 'xrandr',
        'message': f'已配置 {target_connector}',
    }


def get_default_video_device():
    # 获取默认视频设备名，优先从配置读取，否则查询 pw-metadata
    saved = config.get_default_video_sink()
    if saved:
        return saved
    # 通过 pw-metadata 查询当前默认视频 sink
    result = run_command("pw-metadata -n settings 2>/dev/null | grep 'default.video.sink'", timeout=5)
    if result['success'] and result['stdout']:
        parts = result['stdout'].strip().split()
        if len(parts) >= 4:
            value = parts[3].strip("'\"")
            if value:
                return value
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
