import shlex
import logging
import os
import re
import json
from utils import (run_command, pw_dump, find_pw_node, get_prop_with_fallback,
                   find_device_props, parse_edid_monitor_name, parse_edid_physical_size,
                   parse_edid_vendor, parse_edid_product_id, parse_edid_dtd_modes,
                   _get_pw_env)
import platform_paths
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError

logger = logging.getLogger('PipeBridge')

def _xrandr_env():
    # xrandr 需要 X11 DISPLAY,而 run_command 默认注入的 _get_pw_env 只有 PipeWire/D-Bus
    # 会话环境——应用进程内未设置 DISPLAY 时 xrandr 一律 "Can't open display",
    # 表现为分辨率/刷新率/布局/旋转/缩放切换全部失败(真机实测: NAS 有 X 在 :0)。
    # 自动探测 /tmp/.X11-unix/X* 第一个 socket 作为 DISPLAY(:N)。
    env = _get_pw_env()
    if env.get('DISPLAY'):
        return env
    try:
        for name in sorted(os.listdir('/tmp/.X11-unix')):
            if name.startswith('X') and name[1:].isdigit():
                env['DISPLAY'] = f':{name[1:]}'
                break
    except OSError:
        pass
    return env

_XRANDR_ENV = None

def _get_xrandr_env():
    global _XRANDR_ENV
    if _XRANDR_ENV is None:
        _XRANDR_ENV = _xrandr_env()
    return _XRANDR_ENV

def _xrandr_query():
    # 查询当前 RandR 状态(输出列表/模式)，失败返回 None
    result = run_command(f"{platform_paths.CMD_XRANDR} --current 2>/dev/null", timeout=5, env=_get_xrandr_env())
    if result['success'] and result['stdout']:
        return result['stdout']
    return None

_XRANDR_HEADER_RE = re.compile(
    r'^(\S+)\s+(connected|disconnected)(?:\s+(?:primary\s+)?(\d+)x(\d+)\+\d+\+\d+)?')

def _xrandr_output_names(xrandr_text):
    names = []
    for line in xrandr_text.splitlines():
        m = _XRANDR_HEADER_RE.match(line)
        if m:
            names.append(m.group(1))
    return names

def _resolve_xrandr_output(target_connector, xrandr_text):
    # 内核 DRM 连接器名(card1-HDMI-A-1)与 X RandR 输出名可能不一致——
    # modesetting 驱动把 HDMI-A-1 报告为 HDMI-1，旧 intel/radeon 驱动用 HDMI1 风格。
    # 真机实测 xrandr 对未知输出名仅打 warning 且退出码 0，直接拼接会"假成功"。
    # 因此必须先取 xrandr 实际输出列表，按候选名(原名/去编码器字母/去连字符)匹配。
    if not xrandr_text:
        return None
    outputs = _xrandr_output_names(xrandr_text)
    if not outputs:
        return None
    base = re.sub(r'^card\d+-', '', target_connector)
    candidates = [base]
    stripped = re.sub(r'-([A-Za-z])-(\d+)$', r'-\2', base)
    if stripped != base:
        candidates.append(stripped)
    compact = stripped.replace('-', '')
    if compact != stripped:
        candidates.append(compact)
    lower_map = {o.lower(): o for o in outputs}
    for cand in candidates:
        hit = lower_map.get(cand.lower())
        if hit:
            return hit
    return None

def _parse_xrandr_output_section(xrandr_text, output_name):
    # 从 xrandr --current 文本解析指定输出的当前模式：带 * 的行即当前模式
    # (星号可能在任一刷新率列)。w/h 兜底取头行几何值。返回 (w, h, fps)。
    if not xrandr_text or not output_name:
        return 0, 0, 0.0
    in_section = False
    geom_w = geom_h = 0
    for line in xrandr_text.splitlines():
        m = _XRANDR_HEADER_RE.match(line)
        if m:
            in_section = (m.group(1) == output_name)
            if in_section and m.group(3):
                geom_w, geom_h = int(m.group(3)), int(m.group(4))
            continue
        if in_section and (line.startswith('   ') or line.startswith('\t')):
            mm = re.match(r'^\s*(\d+)x(\d+)\s+(.+)$', line)
            if mm and '*' in mm.group(3):
                fps_m = re.search(r'([\d.]+)\s*\*', mm.group(3))
                fps = float(fps_m.group(1)) if fps_m else 0.0
                return int(mm.group(1)), int(mm.group(2)), fps
    return geom_w, geom_h, 0.0

# 仅枚举 Video/Sink（显示输出），不含 Video/Source 输入源
_VIDEO_MEDIA_CLASSES = (
    'Video/Sink', 'Video/Sink/Virtual',
)

def _classify_video_type(name, props=None, device_props=None):
    if props is None:
        props = {}
    if device_props is None:
        device_props = {}
    name_lower = name.lower()

    device_api = get_prop_with_fallback(props, device_props, 'device.api', '').lower()

    if 'bluez' in device_api:
        return 'other'

    if 'hdmi' in name_lower:
        return 'hdmi'
    if 'displayport' in name_lower or 'dp-' in name_lower:
        return 'displayport'
    if 'vga' in name_lower:
        return 'vga'
    if 'display' in name_lower or 'monitor' in name_lower:
        return 'display'
    if 'virtual' in name_lower:
        return 'virtual'
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

def _read_sysfs_value(path):
    # 读取 sysfs 单值文件(如 dpms/enabled),不存在或读取失败时返回空串
    if not os.path.exists(path):
        return ''
    try:
        with open(path, 'r') as f:
            return f.read().strip()
    except (OSError, IOError) as e:
        logger.debug(f"读取失败: {e}")
        return ''

def _drm_modes_with_refresh(connector_dir, edid_data):
    # DRM 模式列表(带刷新率)：sysfs modes 每行只有 "WxH"(同一分辨率的不同刷新率模式
    # 会产生重复行)且不含刷新率，前端"支持格式"因此重复显示分辨率、"刷新率"下拉恒为空。
    # 优先用 EDID DTD 解析出的 "WxH@Hz" 模式(首个 DTD 即 preferred mode)，
    # 再补充 sysfs 中 DTD 未覆盖的分辨率(仅 "WxH"，此类模式无刷新率信息)。
    result = []
    seen = set()
    for mode in parse_edid_dtd_modes(edid_data or b''):
        if mode not in seen:
            seen.add(mode)
            result.append(mode)
    covered = {m.split('@')[0] for m in result}
    modes_result = run_command(f"cat {connector_dir}/modes 2>/dev/null", timeout=2)
    if modes_result['success'] and modes_result['stdout']:
        for line in modes_result['stdout'].splitlines():
            res = line.strip()
            if res and res not in covered:
                covered.add(res)
                result.append(res)
    return result

def _expand_drm_device_info(dd):
    connector_name = dd.get('name', '').replace('drm_', '', 1)
    connector_dir = f"/sys/class/drm/{connector_name}"

    edid_monitor_name = ''
    edid_physical_size = ''
    edid_vendor = ''
    edid_product_id = 0
    edid_data = b''
    edid_path = f"{connector_dir}/edid"
    if os.path.exists(edid_path):
        try:
            with open(edid_path, 'rb') as f:
                edid_data = f.read()
            if edid_data:
                edid_vendor = parse_edid_vendor(edid_data)
                edid_product_id = parse_edid_product_id(edid_data)
                if len(edid_data) >= 128:
                    width_mm, height_mm = parse_edid_physical_size(edid_data)
                    if width_mm > 0 and height_mm > 0:
                        edid_physical_size = f"{width_mm}x{height_mm} mm"
                edid_monitor_name = parse_edid_monitor_name(edid_data)
                if not edid_monitor_name and edid_vendor:
                    edid_monitor_name = f"{edid_vendor}(0x{edid_product_id:04X})" if edid_product_id else edid_vendor
                logger.debug(f"DRM {connector_name} EDID {len(edid_data)}B: name='{edid_monitor_name}' vendor='{edid_vendor}' product=0x{edid_product_id:04X} size='{edid_physical_size}'")
        except (OSError, IOError) as e:
            logger.debug(f"EDID 读取失败: {e}")

    modes = _drm_modes_with_refresh(connector_dir, edid_data)

    dpms_status = _read_sysfs_value(f"{connector_dir}/dpms")

    card_part = connector_name.split('-', 1)
    conn_type_part = card_part[1] if len(card_part) >= 2 else connector_name
    conn_type_parts = conn_type_part.split('-')
    conn_type = conn_type_parts[0].lower()
    conn_index = conn_type_parts[1] if len(conn_type_parts) >= 2 else '0'

    drm_enabled = _read_sysfs_value(f"{connector_dir}/enabled")

    dd['formats'] = modes
    dd['extended'] = {
        'edid_monitor_name': edid_monitor_name,
        'edid_physical_size': edid_physical_size,
        'edid_vendor': edid_vendor,
        'edid_product_id': edid_product_id,
        'connector': connector_name,
        'connector_type': conn_type,
        'connector_index': conn_index,
        'dpms_status': dpms_status,
        'drm_status': dd.get('drm_status', 'connected'),
        'drm_enabled': drm_enabled,
    }

    if edid_monitor_name and 'HDMI -' not in dd.get('friendly_name', ''):
        dd['friendly_name'] = f"{dd.get('friendly_name', '').upper()} - {edid_monitor_name}"
    return dd

def scan_video_devices(force=False):
    # 每次实时扫描，不读取配置文件缓存；force 参数保留向后兼容，不影响扫描行为
    pw_data = pw_dump()
    nodes = _find_video_nodes(pw_data)

    devices = []
    for n in nodes:
        dev = _get_node_info(n)
        props = n.get('info', {}).get('props', {})
        device_id_prop = props.get('device.id')
        device_props = find_device_props(pw_data, device_id_prop) if device_id_prop is not None else {}

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

    default_name = _get_runtime_default_video()
    for dev in devices:
        dev['is_default'] = (dev.get('name') == default_name)

    result = {'devices': devices, 'default': default_name}

    logger.debug(f"扫描视频设备完成: {len(devices)} 个 (PipeWire: {len(nodes)}, DRM: {len(drm_devices)})")
    return result

def get_video_devices():
    return scan_video_devices()

def _get_drm_displays():
    devices = []
    # 整个扫描只查一次 xrandr --current，供所有连接器解析当前模式
    xr_text = _xrandr_query()
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
            edid_data = b''
            try:
                with open(edid_path, 'rb') as f:
                    edid_data = f.read()
                monitor_name = parse_edid_monitor_name(edid_data)
                if not monitor_name and edid_data:
                    vendor = parse_edid_vendor(edid_data)
                    product_id = parse_edid_product_id(edid_data)
                    if vendor:
                        monitor_name = f"{vendor}(0x{product_id:04X})" if product_id else vendor
                    logger.debug(f"DRM {connector} EDID {len(edid_data)}B: monitor_name='{monitor_name}' vendor='{vendor}' product_id=0x{product_id:04X}")
            except (IOError, OSError) as e:
                logger.debug(f"DRM {connector} EDID 读取失败: {e}")
                monitor_name = ''
            if monitor_name:
                friendly_name = f"{connector_upper} - {monitor_name}"

            disp_w = 0
            disp_h = 0
            disp_fps = 0
            disp_pixel_format = ''

            # 1. 获取支持的模式列表（仅用于展示支持格式，不作为当前模式）；
            #    DTD 优先带刷新率，sysfs 仅补充 EDID 未覆盖的分辨率
            resos = _drm_modes_with_refresh(connector_dir, edid_data)

            # 2. 从 xrandr 获取当前模式：内核连接器名需先匹配成 X 输出名
            #    （如 HDMI-A-1 → HDMI-1），否则永远匹配不到、拿不到真实当前模式
            xr_output = _resolve_xrandr_output(connector, xr_text)
            if xr_output:
                disp_w, disp_h, disp_fps = _parse_xrandr_output_section(xr_text, xr_output)

            # 3. xrandr 不可用时，从 DRM state 文件获取当前刷新率
            if disp_fps == 0:
                debug_path = None
                try:
                    dri_dirs = sorted(os.listdir('/sys/kernel/debug/dri/'))
                except (OSError, IOError):
                    dri_dirs = []  # debugfs 未挂载/无权限：跳过刷新率获取，不影响设备枚举
                for dri_dir in dri_dirs:
                    state_file = f"/sys/kernel/debug/dri/{dri_dir}/state"
                    if os.path.exists(state_file):
                        debug_path = state_file
                        break
                if debug_path:
                    state_result = run_command(f"cat {debug_path} 2>/dev/null", timeout=3)
                    if state_result['success'] and state_result['stdout']:
                        found_connector = False
                        for st_line in state_result['stdout'].splitlines():
                            # state 文件中连接器名是 HDMI-A-1，不是 card0-HDMI-A-1
                            if conn_type_part in st_line:
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

            # 4. 都获取不到时，从 modes 列表取第一个作为 fallback
            if disp_w == 0 and resos:
                for m_line in resos:
                    if 'x' in m_line:
                        mode_base = m_line.split('@')[0] if '@' in m_line else m_line
                        size_parts = mode_base.split('x')
                        try:
                            disp_w = int(size_parts[0])
                            dh = size_parts[1].split()[0] if ' ' in size_parts[1] else size_parts[1]
                            disp_h = int(dh)
                        except (ValueError, IndexError):
                            pass
                        break

            if disp_fps == 0 and resos:
                for m_line in resos:
                    if '@' in m_line:
                        hz_match = re.search(r'@([\d.]+)Hz?', m_line, re.IGNORECASE)
                        if hz_match:
                            try:
                                disp_fps = float(hz_match.group(1))
                            except ValueError:
                                pass
                            break

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

def get_video_device_detail(device_name):
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

            pw_extra = {}
            for key in ('device.api', 'device.bus', 'device.bus-path', 'device.bus-id',
                        'device.form-factor', 'device.icon-name', 'device.string',
                    'factory.name', 'client.id',
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

    drm_devices = _get_drm_displays()
    for dd in drm_devices:
        if dd.get('name') == device_name:
            connector_name = device_name.replace('drm_', '', 1)
            connector_dir = f"/sys/class/drm/{connector_name}"

            edid_monitor_name = ''
            edid_path = f"{connector_dir}/edid"
            try:
                with open(edid_path, 'rb') as f:
                    edid_data = f.read()
                edid_monitor_name = parse_edid_monitor_name(edid_data)
            except (IOError, OSError):
                edid_monitor_name = ''

            modes = []
            modes_result = run_command(f"cat {connector_dir}/modes 2>/dev/null", timeout=3)
            if modes_result['success'] and modes_result['stdout']:
                for m_line in modes_result['stdout'].splitlines():
                    m_line = m_line.strip()
                    if m_line:
                        modes.append(m_line)

            drm_status = 'unknown'
            status_result = run_command(f"cat {connector_dir}/status 2>/dev/null", timeout=3)
            if status_result['success'] and status_result['stdout']:
                drm_status = status_result['stdout'].strip()

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

    raise DeviceNotFoundError(f'设备 {device_name} 未找到')

def set_display_output(target_connector, resolution=None, refresh_rate=None):
    if not target_connector:
        raise InvalidParamError('target_connector 不能为空')

    connector_dir = os.path.join(platform_paths.SYS_DRM, target_connector)
    if not os.path.exists(connector_dir):
        raise DeviceNotFoundError(f'连接器 {target_connector} 不存在')

    status_path = f"{connector_dir}/status"
    if os.path.exists(status_path):
        try:
            with open(status_path, 'r') as f:
                status = f.read().strip()
            if status != 'connected':
                raise DeviceNotFoundError(f'连接器 {target_connector} 状态为 {status}，未连接')
        except (IOError, OSError):
            pass

    xr_text = _xrandr_query()
    xrandr_output = _resolve_xrandr_output(target_connector, xr_text)
    if not xrandr_output:
        raise CommandError(
            f'xrandr 中找不到输出 {target_connector}（内核连接器名无法匹配到 X 输出名）')

    cmd_parts = [platform_paths.CMD_XRANDR, '--output', xrandr_output]

    if resolution:
        if not re.match(r'^\d+x\d+$', resolution):
            raise InvalidParamError(f'分辨率格式无效: {resolution}，应为 WxH')
        if refresh_rate:
            try:
                float(refresh_rate)
            except (ValueError, TypeError):
                raise InvalidParamError(f'刷新率格式无效: {refresh_rate}')
        cmd_parts.extend(['--mode', resolution])
    else:
        cmd_parts.append('--auto')

    if refresh_rate and resolution:
        cmd_parts.extend(['--rate', str(refresh_rate)])

    cmd_str = ' '.join(shlex.quote(p) for p in cmd_parts)
    logger.debug(f"执行显示配置命令: {cmd_str}")

    result = run_command(cmd_str, timeout=10, env=_get_xrandr_env())
    if not result['success']:
        raise CommandError(
            f"显示输出配置失败：xrandr 不可用或配置无效（connector={target_connector}, "
            f"resolution={resolution}）: {result.get('stderr', '')[:200]}")

    # 校验实际生效：xrandr 对某些无效参数只打 warning 且退出码 0，会造成"假成功"
    if resolution:
        cur_w, cur_h, _cur_fps = _parse_xrandr_output_section(_xrandr_query(), xrandr_output)
        if cur_w and cur_h and f'{cur_w}x{cur_h}' != resolution:
            raise CommandError(
                f'分辨率切换未生效: 请求 {resolution}，当前仍为 {cur_w}x{cur_h}')

    logger.info(f"显示输出 {target_connector} (xrandr: {xrandr_output}) 已配置: "
                f"resolution={resolution}, refresh_rate={refresh_rate}")
    return {
        'connector': target_connector,
        'xrandr_output': xrandr_output,
        'resolution': resolution,
        'refresh_rate': refresh_rate,
        'method': 'xrandr',
        'message': f'已配置 {target_connector}',
    }

def set_display_layout(output, relation, relative_to=None):
    valid_relations = ('left-of', 'right-of', 'above', 'below', 'same-as', 'primary')
    if relation not in valid_relations:
        raise InvalidParamError(f'布局关系无效: {relation}，可选: {", ".join(valid_relations)}')
    if not output:
        raise InvalidParamError('output 参数必填')
    if relation != 'primary' and not relative_to:
        raise InvalidParamError(f'{relation} 关系需要指定 relative_to 参数')

    xr_text = _xrandr_query()
    xrandr_output = _resolve_xrandr_output(output, xr_text)
    if not xrandr_output:
        raise CommandError(f'xrandr 中找不到输出 {output}')
    cmd_parts = [platform_paths.CMD_XRANDR, '--output', xrandr_output]

    if relation == 'primary':
        cmd_parts.extend(['--primary'])
    else:
        xrandr_relative = _resolve_xrandr_output(relative_to, xr_text)
        if not xrandr_relative:
            raise CommandError(f'xrandr 中找不到参考输出 {relative_to}')
        cmd_parts.extend([f'--{relation}', xrandr_relative])

    cmd_str = ' '.join(shlex.quote(p) for p in cmd_parts)
    result = run_command(cmd_str, timeout=10, env=_get_xrandr_env())
    if not result['success']:
        raise CommandError(f'xrandr 布局设置失败: {result.get("stderr", "")[:200]}')

    logger.info(f"显示布局: {output} {relation} {relative_to or ''}")
    return {
        'output': output,
        'relation': relation,
        'relative_to': relative_to,
        'message': f'{output} 已设为 {relation} {relative_to or "主显示器"}',
    }

def set_display_rotation(output, rotation):
    valid_rotations = ('normal', 'left', 'right', 'inverted')
    if rotation not in valid_rotations:
        raise InvalidParamError(f'旋转方向无效: {rotation}，可选: {", ".join(valid_rotations)}')
    if not output:
        raise InvalidParamError('output 参数必填')

    xrandr_output = _resolve_xrandr_output(output, _xrandr_query())
    if not xrandr_output:
        raise CommandError(f'xrandr 中找不到输出 {output}')

    result = run_command(
        f"{platform_paths.CMD_XRANDR} --output {shlex.quote(xrandr_output)} --rotate {rotation}",
        timeout=10, env=_get_xrandr_env())
    if not result['success']:
        raise CommandError(f'xrandr 旋转设置失败: {result.get("stderr", "")[:200]}')

    logger.info(f"显示旋转: {output} -> {rotation}")
    return {'output': output, 'rotation': rotation, 'message': f'{output} 已旋转为 {rotation}'}

def set_display_scale(output, scale):
    if not output:
        raise InvalidParamError('output 参数必填')
    try:
        scale = float(scale)
    except (ValueError, TypeError):
        raise InvalidParamError(f'缩放比例无效: {scale}')
    if scale < 0.1 or scale > 4.0:
        raise InvalidParamError('缩放比例范围: 0.1 ~ 4.0')

    xrandr_output = _resolve_xrandr_output(output, _xrandr_query())
    if not xrandr_output:
        raise CommandError(f'xrandr 中找不到输出 {output}')

    result = run_command(
        f"{platform_paths.CMD_XRANDR} --output {shlex.quote(xrandr_output)} --scale {scale}x{scale}",
        timeout=10, env=_get_xrandr_env())
    if not result['success']:
        raise CommandError(f'xrandr 缩放设置失败: {result.get("stderr", "")[:200]}')

    logger.info(f"显示缩放: {output} -> {scale}x{scale}")
    return {'output': output, 'scale': scale, 'message': f'{output} 缩放已设为 {scale}x{scale}'}

def _get_runtime_default_video():
    # 纯运行时读取系统当前默认视频 sink(不涉及 PipeBridge 自身持久化),仅用于 is_default 展示
    result = run_command("pw-metadata -n settings 2>/dev/null | grep 'default.video.sink'", timeout=5)
    if result['success'] and result['stdout']:
        match = re.search(r"value:\s*[\"']([^\"']+)[\"']", result['stdout'])
        if match:
            return match.group(1)
    return ''

_SAFE_VIDEO_PATTERN = re.compile(r'^[\w.\-: ]+$')

def set_default_video(device_name):
    # 运行时将指定视频设备设为系统默认视频 sink(仅 pw-metadata 写 default.video.sink,不持久化到 config)。
    # 仅对 PipeWire 视频节点有效;DRM 显示器(HDMI 等)非 PipeWire sink,抛 InvalidParamError 提示不支持。
    # 成功返回 dict(message/device),失败抛 DeviceNotFoundError/CommandError。
    if not device_name or not _SAFE_VIDEO_PATTERN.match(device_name):
        raise InvalidParamError('无效的设备名')
    pw_data = pw_dump()
    node = find_pw_node(pw_data, name=device_name)
    if node is None:
        node = find_pw_node(pw_data, property_filters={'node.description': device_name})
    if node is None:
        # 未找到对应 PipeWire 节点,通常是 DRM 显示器,不支持设为默认视频 sink
        raise InvalidParamError('该设备为显示输出(DRM),不支持设为默认视频 sink;请使用显示输出设置')
    node_name = node.get('info', {}).get('props', {}).get('node.name', device_name)
    cmd = f"pw-metadata -n settings 0 default.video.sink {shlex.quote(json.dumps({'name': node_name}))}"
    result = run_command(cmd, timeout=5)
    if not result or not result.get('success'):
        raise CommandError('设置默认视频设备失败')
    logger.info(f"已设为默认视频设备(运行时): {device_name} (node={node_name})")
    return {'message': f'已将 {device_name} 设为默认视频', 'device': device_name}
