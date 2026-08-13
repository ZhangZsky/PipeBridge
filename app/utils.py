import subprocess
import os
import json
import re
import logging
import threading
import time
import config

logger = logging.getLogger('PipeBridge')

_pw_env_logged = False
_pw_env_cache = None

def _get_pw_env():
    global _pw_env_logged, _pw_env_cache
    if _pw_env_cache is not None:
        dbus_addr = _pw_env_cache.get('DBUS_SESSION_BUS_ADDRESS', '')
        if dbus_addr.startswith('unix:path='):
            socket_path = dbus_addr[len('unix:path='):]
            if not os.path.exists(socket_path):
                _pw_env_cache = None
        return _pw_env_cache

    env = os.environ.copy()
    xdg_dir = f'/run/user/{os.getuid()}'

    if not env.get('XDG_RUNTIME_DIR'):
        os.makedirs(xdg_dir, exist_ok=True)
        env['XDG_RUNTIME_DIR'] = xdg_dir

    if not env.get('DBUS_SESSION_BUS_ADDRESS'):
        dbus_path = os.path.join(env['XDG_RUNTIME_DIR'], 'bus')
        if os.path.exists(dbus_path):
            env['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path={dbus_path}'
        else:
            try:
                result = subprocess.run(
                    "dbus-launch --sh-syntax 2>/dev/null",
                    shell=True, capture_output=True, text=True, timeout=5,
                    env=env
                )
                if result.returncode == 0 and result.stdout:
                    m = re.search(r'DBUS_SESSION_BUS_ADDRESS=([^;\s]+)', result.stdout)
                    if m:
                        env['DBUS_SESSION_BUS_ADDRESS'] = m.group(1)
                        if not _pw_env_logged:
                            logger.debug(f"dbus-launch 获取 D-Bus 地址: {m.group(1)}")
            except Exception as e:
                logger.debug(f"dbus-launch 获取 D-Bus 地址失败: {e}")

            if not env.get('DBUS_SESSION_BUS_ADDRESS'):
                try:
                    subprocess.run(
                        f"dbus-daemon --session --address=unix:path={dbus_path} --fork 2>/dev/null",
                        shell=True, capture_output=True, text=True, timeout=5,
                        env=env
                    )
                    if os.path.exists(dbus_path):
                        env['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path={dbus_path}'
                        if not _pw_env_logged:
                            logger.info(f"已启动 D-Bus 会话总线: {dbus_path}")
                except Exception as e:
                    if not _pw_env_logged:
                        logger.debug(f"dbus-daemon 启动失败: {e}")

    sys_bus_path = '/var/run/dbus/system_bus_socket'
    if not env.get('DBUS_SYSTEM_BUS_ADDRESS') and os.path.exists(sys_bus_path):
        env['DBUS_SYSTEM_BUS_ADDRESS'] = f'unix:path={sys_bus_path}'

    if not _pw_env_logged:
        _pw_env_logged = True
        logger.debug(f"PW 环境: XDG={env.get('XDG_RUNTIME_DIR')}, DBUS_SESSION={env.get('DBUS_SESSION_BUS_ADDRESS')}, DBUS_SYSTEM={env.get('DBUS_SYSTEM_BUS_ADDRESS')}")

    # 把推断/新建出的会话与运行时目录写回本进程 os.environ，使 D-Bus 连接与 run_command 子进程(obexd/systemctl --user)挂在同一条会话总线上，否则 OBEX Agent 注册到不同总线导致手机推送被 obexd 以 Forbidden 拒绝
    for _k in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'DBUS_SYSTEM_BUS_ADDRESS'):
        _v = env.get(_k)
        if _v:
            os.environ[_k] = _v

    _pw_env_cache = env
    return env

def _pw_socket_exists():
    pw_env = _get_pw_env()
    xdg = pw_env.get('XDG_RUNTIME_DIR', '')
    if not xdg:
        return False
    return os.path.exists(f"{xdg}/pipewire-0")

def start_pw_service(service_name):
    pw_env = _get_pw_env()
    log_file = f"/tmp/{service_name}-0.log"

    cmd_check = run_command(f"command -v {service_name} 2>/dev/null")
    if not cmd_check['stdout'].strip():
        logger.error(f"{service_name} 命令不存在，请运行 install_init 安装系统依赖")
        return False

    pg_result = run_command(f"pgrep -x {service_name} 2>/dev/null")
    if pg_result['stdout'].strip():
        if service_name == 'pipewire' and not _pw_socket_exists():
            logger.warning(f"{service_name} 进程存在但 socket 缺失，重启进程...")
            run_command(f"pkill -x {service_name} 2>/dev/null")
            time.sleep(1)
        else:
            return True

    logger.debug(f"启动 {service_name}...")
    start_env = pw_env.copy()
    if service_name == 'wireplumber':
        start_env['WIREPLUMBER_DEBUG'] = '2'
    run_command(f"nohup {service_name} >{log_file} 2>&1 &", timeout=5, env=start_env)
    time.sleep(2 if service_name == 'pipewire' else 1)
    pg_result = run_command(f"pgrep -x {service_name} 2>/dev/null")
    started = bool(pg_result['stdout'].strip())
    if not started:
        diag = ''
        try:
            if os.path.exists(log_file):
                with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                    diag = f.read().strip()[-500:]
        except OSError:
            pass
        logger.warning(f"{service_name} 启动后未检测到进程，可能启动失败。日志: {diag[:300] if diag else '(空)'}")
    elif service_name == 'pipewire':
        if _pw_socket_exists():
            logger.info("pipewire 启动成功，socket 已创建")
        else:
            logger.warning("pipewire 进程存在但 socket 未创建，可能初始化卡住")
            try:
                if os.path.exists(log_file):
                    with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                        diag = f.read().strip()[-500:]
                    logger.warning(f"pipewire 启动日志: {diag[:400] if diag else '(空)'}")
            except OSError:
                pass
    return started

def stop_pw_service(service_name):
    run_command(f"pkill -x {service_name} 2>/dev/null")
    time.sleep(0.5)
    return True

# 安全策略：run_command 使用 shell=True 以支持管道、重定向等合法 shell 语法
# （如 "pgrep -x pipewire 2>/dev/null"、"systemctl is-active bluetooth 2>/dev/null"）。
# 命令注入防护由调用方负责：所有动态参数必须使用 shlex.quote() 转义。
# 不使用正则拦截，因为合法运维命令本身包含 |、>、$ 等字符，正则会误杀。
def _validate_command(cmd):
    # shell=True 下 |、;、$、&、<> 均为合法 shell 语法，不做拦截。
    # 仅拦截换行符（防止多行命令注入，换行符在单行运维命令中无合法用途）。
    if '\n' in cmd or '\r' in cmd:
        raise ValueError(f"命令包含换行符，可能存在注入风险: {cmd[:100]}")
    return cmd

def run_command(cmd, timeout=30, env=None):
    try:
        cmd_env = env if env is not None else _get_pw_env()
        result = subprocess.run(
            _validate_command(cmd),
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=cmd_env
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "stdout": "", "stderr": "Command timeout", "returncode": -1}
    except (subprocess.SubprocessError, OSError, PermissionError) as e:
        logger.warning(f"命令执行系统错误: {e}")
        return {"success": False, "stdout": "", "stderr": str(e), "returncode": -1}
    except Exception as e:
        logger.error(f"命令执行内部错误（编程缺陷）: {type(e).__name__}: {e}")
        return {"success": False, "stdout": "", "stderr": f"Internal error: {type(e).__name__}: {e}", "returncode": -1}

def find_pw_node(pw_data, name=None, media_class=None, node_id=None, property_filters=None,
                 device_name=None, device_name_contains=None, object_type=None):
    target_type = object_type or 'PipeWire:Interface:Node'
    for obj in pw_data:
        if not isinstance(obj, dict) or obj.get('type') != target_type:
            continue
        props = obj.get('info', {}).get('props', {})
        if name is not None and props.get('node.name') != name:
            continue
        if media_class is not None and props.get('media.class') != media_class:
            continue
        if node_id is not None and obj.get('id') != node_id:
            continue
        if property_filters:
            if not all(props.get(k) == v for k, v in property_filters.items()):
                continue
        if device_name is not None and props.get('device.name') != device_name:
            continue
        if device_name_contains is not None:
            dev_name_val = props.get('device.name', '')
            if device_name_contains.lower() not in dev_name_val.lower():
                continue
        return obj
    return None

def get_node_id_by_name(name):
    pw_data = pw_dump()
    obj = find_pw_node(pw_data, name=name)
    return obj.get('id') if obj else None

def get_node_name_by_id(node_id):
    pw_data = pw_dump()
    obj = find_pw_node(pw_data, node_id=node_id)
    return obj.get('info', {}).get('props', {}).get('node.name', '') if obj else ''

def _parse_wpctl_default():
    result = run_command("wpctl status 2>/dev/null", timeout=5)
    if not result['success'] or not result['stdout']:
        return '', ''
    default_sink = ''
    default_source = ''
    section = ''
    for line in result['stdout'].splitlines():
        stripped = line.strip()
        if 'Sinks:' in stripped:
            section = 'sink'
            continue
        elif 'Sources:' in stripped:
            section = 'source'
            continue
        elif 'Clients:' in stripped:
            section = ''
            continue
        if '*' in stripped:
            m = re.search(r'\*\s+(\d+)\.\s+(\S+)', stripped)
            if m:
                if section == 'sink':
                    default_sink = m.group(2)
                elif section == 'source':
                    default_source = m.group(2)
    return default_sink, default_source

def _get_default_node_name(kind):
    # 获取默认音频节点名(kind: 'sink' 或 'source')
    # 依次尝试：配置文件保存值 → wpctl 默认 → pw-metadata
    getter = config.get_default_sink if kind == 'sink' else config.get_default_source
    saved = getter()
    if saved:
        return saved
    sink, source = _parse_wpctl_default()
    parsed = sink if kind == 'sink' else source
    if parsed:
        return parsed
    result = run_command(
        f"pw-metadata -n settings 2>/dev/null | grep 'default.audio.{kind}'", timeout=5)
    if result['success'] and result['stdout']:
        m = re.search(r'"Spa:Json:node:name:([^"]+)"', result['stdout'])
        if m:
            return m.group(1)
    return ''

def get_default_sink_name():
    return _get_default_node_name('sink')

def get_default_source_name():
    return _get_default_node_name('source')

def extract_pw_vol_params(params):
    props_params = params.get('Props', {})
    if isinstance(props_params, list) and len(props_params) > 0 and isinstance(props_params[0], dict):
        props_params = props_params[0]
    return props_params if isinstance(props_params, dict) else {}

def extract_pw_enumformat(params):
    ef = params.get('EnumFormat', [])
    if isinstance(ef, list):
        return ef
    if isinstance(ef, dict):
        return [ef]
    return []

def extract_pw_routes(params):
    ports = []
    active_port = ''

    enum_routes = params.get('EnumRoute', [])
    if isinstance(enum_routes, dict):
        enum_routes = [enum_routes]

    routes = params.get('Route', [])
    if isinstance(routes, dict):
        routes = [routes]

    for er in enum_routes:
        if not isinstance(er, dict):
            continue
        direction = er.get('direction', '')
        if direction != 'Output':
            continue
        port_name = er.get('name', '')
        port_desc = (er.get('description', '') or port_name).replace(' / ', '/')
        if not port_name:
            continue
        ports.append({
            'name': port_name,
            'description': port_desc,
            'priority': er.get('priority', 0),
            'devices': er.get('devices', []),
        })

    for r in routes:
        if not isinstance(r, dict):
            continue
        direction = r.get('direction', '')
        if direction != 'Output':
            continue
        active_port = r.get('name', '')
        break

    return ports, active_port

def is_real_sink(obj):
    if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
        return False
    props = obj.get('info', {}).get('props', {})
    if props.get('media.class', '') not in ('Audio/Sink', 'Audio/Sink/Virtual'):
        return False
    name = props.get('node.name', '').lower()
    desc = props.get('node.description', '')
    return ('auto_null' not in name and 'null-sink' not in name
            and 'dummy' not in name and 'Dummy' not in desc)

def is_real_audio_source(obj):
    if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
        return False
    props = obj.get('info', {}).get('props', {})
    mc = props.get('media.class', '')
    if mc not in ('Audio/Source', 'Audio/Source/Virtual'):
        return False
    name = props.get('node.name', '').lower()
    return ('auto_null' not in name and 'null' not in name
            and 'dummy' not in name)

def find_audio_sinks(pw_data=None):
    if pw_data is None:
        pw_data = pw_dump()
    return [obj for obj in pw_data if is_real_sink(obj)]

def find_audio_sources(pw_data=None):
    if pw_data is None:
        pw_data = pw_dump()
    return [obj for obj in pw_data if is_real_audio_source(obj)]

_pw_dump_cache = None
_pw_dump_cache_time = 0
_pw_dump_lock = threading.Lock()
_PW_DUMP_CACHE_TTL = 1.0

def pw_dump_invalidate():
    global _pw_dump_cache, _pw_dump_cache_time
    with _pw_dump_lock:
        _pw_dump_cache = None
        _pw_dump_cache_time = 0

_last_pw_diag_time = 0

def _diagnose_pw_failure():
    global _last_pw_diag_time
    now = time.time()
    if now - _last_pw_diag_time < 30:
        return
    _last_pw_diag_time = now

    try:
        pw_proc = run_command("pgrep -ax pipewire 2>/dev/null", timeout=2)
        pw_alive = bool(pw_proc['stdout'].strip())
        logger.warning(f"PipeWire 诊断: 进程存活={pw_alive}, 进程详情={pw_proc['stdout'][:200] if pw_alive else '(无)'}")

        wp_proc = run_command("pgrep -ax wireplumber 2>/dev/null", timeout=2)
        wp_alive = bool(wp_proc['stdout'].strip())
        logger.warning(f"WirePlumber 诊断: 进程存活={wp_alive}, 进程详情={wp_proc['stdout'][:200] if wp_alive else '(无)'}")

        pw_env = _get_pw_env()
        xdg = pw_env.get('XDG_RUNTIME_DIR', '')
        if xdg:
            socket_path = f"{xdg}/pipewire-0"
            if os.path.exists(socket_path):
                import stat
                st = os.stat(socket_path)
                perms = stat.filemode(st.st_mode)
                logger.warning(f"PipeWire socket 诊断: 路径={socket_path}, 权限={perms}, uid={st.st_uid}, gid={st.st_gid}")
            else:
                logger.warning(f"PipeWire socket 诊断: socket 不存在 ({socket_path})")

        for log_file in ['/tmp/pipewire-0.log', '/tmp/pipewire.log']:
            if os.path.exists(log_file):
                try:
                    with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read().strip()
                    if content:
                        logger.warning(f"PipeWire 日志诊断 ({log_file}): {content[-800:]}")
                        break
                except OSError:
                    pass

        for log_file in ['/tmp/wireplumber-0.log', '/tmp/wireplumber.log']:
            if os.path.exists(log_file):
                try:
                    with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read().strip()
                    if content:
                        logger.warning(f"WirePlumber 日志诊断 ({log_file}): {content[-800:]}")
                        break
                except OSError:
                    pass
    except Exception as e:
        logger.warning(f"PipeWire 诊断异常: {e}")

def pw_dump():
    global _pw_dump_cache, _pw_dump_cache_time
    now = time.time()
    with _pw_dump_lock:
        if _pw_dump_cache is not None and (now - _pw_dump_cache_time) < _PW_DUMP_CACHE_TTL:
            return _pw_dump_cache

    result = run_command("pw-dump 2>/dev/null", timeout=3)
    if not result['success']:
        logger.info(f"pw-dump 执行失败: returncode={result.get('returncode', '?')}, stderr='{result.get('stderr', '')[:200]}'")
        _diagnose_pw_failure()
        with _pw_dump_lock:
            _pw_dump_cache = []
            _pw_dump_cache_time = now + 9
        return []
    if not result['stdout'] or not result['stdout'].strip():
        logger.info("pw-dump 无输出（PipeWire 可能未配置音频）")
        with _pw_dump_lock:
            _pw_dump_cache = []
            _pw_dump_cache_time = now
        return []
    try:
        data = json.loads(result['stdout'])
        if not isinstance(data, list):
            logger.info(f"pw-dump 返回非列表类型: {type(data).__name__}")
            with _pw_dump_lock:
                _pw_dump_cache = []
                _pw_dump_cache_time = now
            return []
        logger.debug(f"pw-dump 返回 {len(data)} 个对象")
        with _pw_dump_lock:
            _pw_dump_cache = data
            _pw_dump_cache_time = now
        return data
    except (json.JSONDecodeError, ValueError) as e:
        logger.info(f"pw-dump JSON 解析失败: {e}, 原始输出前200字符: '{result['stdout'][:200]}'")
        with _pw_dump_lock:
            _pw_dump_cache = []
            _pw_dump_cache_time = now
        return []

def get_prop_with_fallback(primary_props, fallback_props, key, default=''):
    val = primary_props.get(key, '')
    if not val and fallback_props:
        val = fallback_props.get(key, '')
    return val if val else default

def find_device_props(pw_data, device_id):
    for obj in pw_data:
        if obj.get('type') == 'PipeWire:Interface:Device' and obj.get('id') == device_id:
            return obj.get('info', {}).get('props', {})
    return {}

def iter_pw_devices(pw_data):
    # 遍历所有 PipeWire:Interface:Device 对象(统一类型/字典校验)
    for obj in pw_data:
        if isinstance(obj, dict) and obj.get('type') == 'PipeWire:Interface:Device':
            yield obj

def find_pw_device_by_id(pw_data, device_id):
    for obj in iter_pw_devices(pw_data):
        if obj.get('id') == device_id:
            return obj
    return None

def find_pw_device_by_card_id(pw_data, card_id):
    # 按 card_id 在 device.name/nick/alias 中模糊匹配 Device 对象
    card_low = card_id.lower()
    for obj in iter_pw_devices(pw_data):
        dev_props = obj.get('info', {}).get('props', {})
        if (card_low in dev_props.get('device.name', '').lower()
                or card_low in dev_props.get('device.nick', '').lower()
                or card_low in dev_props.get('device.alias', '').lower()):
            return obj
    return None

def _normalize_pw_list(value):
    # PipeWire 参数可能是单 dict 或 list，统一为 list
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return value
    return []

def get_device_enum_profiles(pw_device):
    # 解析 Device 的 EnumProfile 为统一结构列表(name/description/priority/available/index)
    params = pw_device.get('info', {}).get('params', {})
    if not isinstance(params, dict):
        return []
    profiles = []
    for ep in _normalize_pw_list(params.get('EnumProfile', [])):
        if not isinstance(ep, dict):
            continue
        name = ep.get('name', '')
        profiles.append({
            'name': name,
            'description': ep.get('description', name),
            'priority': ep.get('priority', 0),
            'available': ep.get('available', True),
            'index': ep.get('index'),
        })
    return profiles

def get_device_active_profile(pw_device):
    # 读取 Device 当前激活 profile 名。优先 save=true 项(临时切换时 save 常为 false 会漏),
    # 否则取首个(当前激活项),再回退 device.profile 属性
    params = pw_device.get('info', {}).get('params', {})
    active_name = ''
    if isinstance(params, dict):
        for p in _normalize_pw_list(params.get('Profile', [])):
            if not isinstance(p, dict):
                continue
            name = p.get('name', '')
            if p.get('save', False):
                return name
            if not active_name and name:
                active_name = name
    if active_name:
        return active_name
    return pw_device.get('info', {}).get('props', {}).get('device.profile', '')

def parse_edid_monitor_name(edid_data):
    if not edid_data or len(edid_data) < 72:
        return ''
    for i in range(54, min(126, len(edid_data) - 17), 18):
        if edid_data[i] == 0x00 and edid_data[i+1] == 0x00 and edid_data[i+2] == 0x00:
            if edid_data[i+3] == 0xfc:
                name_bytes = edid_data[i+5:i+18]
                return name_bytes.rstrip(b'\x00\x0a').decode('latin-1', errors='ignore').strip()
    return ''

def parse_edid_physical_size(edid_data):
    if not edid_data or len(edid_data) < 73:
        return 0, 0
    width_mm = edid_data[21]
    height_mm = edid_data[22]
    return width_mm, height_mm

def parse_edid_vendor(edid_data):
    if not edid_data or len(edid_data) < 12:
        return ''
    b0 = edid_data[8]
    b1 = edid_data[9]
    c1 = chr(((b0 >> 2) & 0x1F) + 64)
    c2 = chr((((b0 & 0x03) << 3) | ((b1 >> 5) & 0x07)) + 64)
    c3 = chr((b1 & 0x1F) + 64)
    vendor = (c1 + c2 + c3).strip()
    return vendor if vendor.isalpha() and len(vendor) == 3 else ''

def parse_edid_product_id(edid_data):
    if not edid_data or len(edid_data) < 12:
        return 0
    return edid_data[10] | (edid_data[11] << 8)

def _find_pw_links(pw_data):
    return [obj for obj in pw_data
            if isinstance(obj, dict)
            and obj.get('type') == 'PipeWire:Interface:Link']

def _find_pw_ports(pw_data):
    return [obj for obj in pw_data
            if isinstance(obj, dict)
            and obj.get('type') == 'PipeWire:Interface:Port']

def _get_ports_for_node(pw_data, node_id, direction=None):
    ports = []
    for obj in _find_pw_ports(pw_data):
        info = obj.get('info', {})
        props = info.get('props', {})
        if info.get('node-id') == node_id:
            port_dir = props.get('port.direction', '')
            if direction is None or port_dir == direction:
                ports.append(obj)
    return ports

def _build_link_info(link_obj, pw_data):
    info = link_obj.get('info', {})
    link_id = link_obj.get('id')
    output_port = info.get('output-port-id')
    input_port = info.get('input-port-id')

    output_node_id = None
    output_node_name = ''
    input_node_id = None
    input_node_name = ''

    for port_obj in _find_pw_ports(pw_data):
        port_info = port_obj.get('info', {})
        port_id = port_obj.get('id')
        if port_id == output_port:
            output_node_id = port_info.get('node-id')
        elif port_id == input_port:
            input_node_id = port_info.get('node-id')
        if output_node_id is not None and input_node_id is not None:
            break

    if output_node_id is not None:
        node = find_pw_node(pw_data, node_id=output_node_id)
        if node:
            output_node_name = node.get('info', {}).get('props', {}).get('node.name', '')

    if input_node_id is not None:
        node = find_pw_node(pw_data, node_id=input_node_id)
        if node:
            input_node_name = node.get('info', {}).get('props', {}).get('node.name', '')

    return {
        'link_id': link_id,
        'output_port': output_port,
        'input_port': input_port,
        'output_node_id': output_node_id,
        'output_node_name': output_node_name,
        'input_node_id': input_node_id,
        'input_node_name': input_node_name,
    }
