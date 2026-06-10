import subprocess
import os
import json
import re
import logging
import threading
import time
import config

logger = logging.getLogger('MediaHub')


_pw_env_logged = False
_pw_env_cache = None

def _get_pw_env():
    global _pw_env_logged, _pw_env_cache
    if _pw_env_cache is not None:
        # D-Bus socket 可能已失效（如会话总线重启），检查后自动失效
        dbus_addr = _pw_env_cache.get('DBUS_SESSION_BUS_ADDRESS', '')
        if dbus_addr.startswith('unix:path='):
            socket_path = dbus_addr[len('unix:path='):]
            if not os.path.exists(socket_path):
                _pw_env_cache = None
        return _pw_env_cache

    env = os.environ.copy()
    xdg_dir = '/run/user/0'

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
            except Exception:
                pass

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

    _pw_env_cache = env
    return env


def start_pw_service(service_name):
    # root 环境：直接启动进程
    pw_env = _get_pw_env()
    # 先检查是否已在运行
    pg_result = run_command(f"pgrep -x {service_name} 2>/dev/null")
    if pg_result['stdout'].strip():
        return True
    # 启动进程
    logger.debug(f"启动 {service_name}...")
    log_file = f"/tmp/{service_name}-0.log"
    run_command(f"nohup {service_name} >{log_file} 2>&1 &", timeout=5, env=pw_env)
    time.sleep(1)
    pg_result = run_command(f"pgrep -x {service_name} 2>/dev/null")
    started = bool(pg_result['stdout'].strip())
    if not started:
        logger.warning(f"{service_name} 启动后未检测到进程，可能启动失败")
    return started


def stop_pw_service(service_name):
    # root 环境：直接 kill 进程
    run_command(f"pkill -x {service_name} 2>/dev/null")
    time.sleep(0.5)
    return True


# 命令注入风险字符检测 — 仅检测反引号（命令替换）
# 内部命令使用 &&、||、|、;、>、$()、${} 等是合法的，防护依赖 shlex.quote() 转义用户输入
_SHELL_INJECTION_PATTERN = re.compile(r'`')

# 校验命令字符串，拒绝明显的注入模式
def _validate_command(cmd):
    if _SHELL_INJECTION_PATTERN.search(cmd):
        raise ValueError(f"命令包含注入风险字符，可能存在命令注入: {cmd[:100]}")
    return cmd


def run_command(cmd, timeout=30, env=None):
    # 执行 shell 命令，返回 {success, stdout, stderr, returncode}
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
    """在 pw_dump 数据中查找 PipeWire 节点或设备

    Args:
        pw_data: pw_dump() 返回的数据
        name: 按 node.name 精确匹配
        media_class: 按 media.class 精确匹配
        node_id: 按节点 ID 匹配
        property_filters: dict，按 props 中的键值对匹配
        device_name: 按 device.name 属性精确匹配
        device_name_contains: 按 device.name 属性包含匹配（大小写不敏感）
        object_type: 按对象类型匹配，默认 'PipeWire:Interface:Node'；
                     设为 'PipeWire:Interface:Device' 可搜索设备对象
    """
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
    # 通过名称获取节点 ID
    pw_data = pw_dump()
    obj = find_pw_node(pw_data, name=name)
    return obj.get('id') if obj else None


def get_node_name_by_id(node_id):
    # 通过 ID 获取节点名称
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


# 获取默认输出设备名
def get_default_sink_name():
    saved = config.get_default_sink()
    if saved:
        return saved
    sink, _ = _parse_wpctl_default()
    if sink:
        return sink
    result = run_command("pw-metadata -n settings 2>/dev/null | grep 'default.audio.sink'", timeout=5)
    if result['success'] and result['stdout']:
        m = re.search(r'"Spa:Json:node:name:([^"]+)"', result['stdout'])
        if m:
            return m.group(1)
    return ''


def get_default_source_name():
    saved = config.get_default_source()
    if saved:
        return saved
    _, source = _parse_wpctl_default()
    if source:
        return source
    result = run_command("pw-metadata -n settings 2>/dev/null | grep 'default.audio.source'", timeout=5)
    if result['success'] and result['stdout']:
        m = re.search(r'"Spa:Json:node:name:([^"]+)"', result['stdout'])
        if m:
            return m.group(1)
    return ''


def extract_pw_vol_params(params):
    # 从 pw-dump params 中提取 Props（处理 list/dict 两种格式）
    props_params = params.get('Props', {})
    if isinstance(props_params, list) and len(props_params) > 0 and isinstance(props_params[0], dict):
        props_params = props_params[0]
    return props_params if isinstance(props_params, dict) else {}


def extract_pw_enumformat(params):
    # 从 pw-dump params 提取 EnumFormat 列表
    ef = params.get('EnumFormat', [])
    if isinstance(ef, list):
        return ef
    if isinstance(ef, dict):
        return [ef]
    return []


def extract_pw_routes(params):
    # 从 pw-dump 的 params 中解析 EnumRoute 和 Route 信息，获取端口列表和活动端口
    ports = []
    active_port = ''

    enum_routes = params.get('EnumRoute', [])
    if isinstance(enum_routes, dict):
        enum_routes = [enum_routes]

    routes = params.get('Route', [])
    if isinstance(routes, dict):
        routes = [routes]

    # 从 EnumRoute 构建端口列表
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

    # 从 Route 获取当前活动端口
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
    # 判断 pw-dump 节点是否为真实 Audio/Sink（排除虚拟/空设备）
    if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
        return False
    props = obj.get('info', {}).get('props', {})
    if props.get('media.class', '') not in ('Audio/Sink', 'Audio/Sink/Virtual'):
        return False
    name = props.get('node.name', '').lower()
    desc = props.get('node.description', '')
    # 排除虚拟空设备
    return ('auto_null' not in name and 'null-sink' not in name
            and 'dummy' not in name and 'Dummy' not in desc)


def is_real_audio_source(obj):
    # 判断是否为真实的音频来源（输入设备）
    if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
        return False
    props = obj.get('info', {}).get('props', {})
    mc = props.get('media.class', '')
    if mc not in ('Audio/Source', 'Audio/Source/Virtual'):
        return False
    name = props.get('node.name', '').lower()
    # 排除虚拟空设备
    return ('auto_null' not in name and 'null' not in name
            and 'dummy' not in name)


def find_audio_sinks(pw_data=None):
    # 从 pw-dump 数据中提取真实 Audio/Sink 节点
    if pw_data is None:
        pw_data = pw_dump()
    return [obj for obj in pw_data if is_real_sink(obj)]


def find_audio_sources(pw_data=None):
    # 从 pw-dump 数据中提取真实 Audio/Source 节点
    if pw_data is None:
        pw_data = pw_dump()
    return [obj for obj in pw_data if is_real_audio_source(obj)]


class ScanCache:
    def __init__(self, cooldown=15):
        self.cooldown = cooldown
        self._last_time = 0
        self._last_result = None
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            now = time.time()
            if self._last_result is not None and (now - self._last_time) < self.cooldown:
                return self._last_result
            return None

    def set(self, result):
        with self._lock:
            self._last_time = time.time()
            self._last_result = result

    def invalidate(self):
        with self._lock:
            self._last_time = 0
            self._last_result = None


# pw_dump 短时缓存（5秒TTL，避免同一请求内多次调用 pw-dump）
_pw_dump_cache = ScanCache(cooldown=5)


def pw_dump(force_refresh=False):
    # 执行 pw-dump 并返回 JSON 数据，失败时返回空列表
    # 使用 5 秒短时缓存，避免同一请求内多次启动 pw-dump 进程
    if force_refresh:
        _pw_dump_cache.invalidate()

    cached = _pw_dump_cache.get()
    if cached is not None:
        return cached

    pw_env = _get_pw_env()
    xdg = pw_env.get('XDG_RUNTIME_DIR', '(未设置)')
    dbus = pw_env.get('DBUS_SESSION_BUS_ADDRESS', '(未设置)')
    pw_socket = os.path.exists(f"{xdg}/pipewire-0") if xdg != '(未设置)' else False
    logger.debug(f"PW 环境: XDG_RUNTIME_DIR={xdg}, DBUS={dbus}, pw_socket={pw_socket}")

    result = run_command("pw-dump 2>/dev/null", timeout=5)
    if not result['success']:
        logger.info(f"pw-dump 执行失败: returncode={result.get('returncode', '?')}, stderr='{result.get('stderr', '')[:200]}'")
        return []
    if not result['stdout'] or not result['stdout'].strip():
        logger.info("pw-dump 无输出（PipeWire 可能未配置音频）")
        return []
    try:
        data = json.loads(result['stdout'])
        if not isinstance(data, list):
            logger.info(f"pw-dump 返回非列表类型: {type(data).__name__}")
            return []
        logger.debug(f"pw-dump 返回 {len(data)} 个对象")
        _pw_dump_cache.set(data)
        return data
    except (json.JSONDecodeError, ValueError) as e:
        logger.info(f"pw-dump JSON 解析失败: {e}, 原始输出前200字符: '{result['stdout'][:200]}'")
        return []


def get_prop_with_fallback(primary_props, fallback_props, key, default=''):
    val = primary_props.get(key, '')
    if not val and fallback_props:
        val = fallback_props.get(key, '')
    return val if val else default


# 按 device ID 查找设备属性
def find_device_props(pw_data, device_id):
    for obj in pw_data:
        if obj.get('type') == 'PipeWire:Interface:Device' and obj.get('id') == device_id:
            return obj.get('info', {}).get('props', {})
    return {}


# 解析 EDID 显示器名称
def parse_edid_monitor_name(edid_data):
    if not edid_data or len(edid_data) < 108:
        return ''
    for i in range(54, min(108, len(edid_data) - 1), 18):
        if edid_data[i] == 0x00 and edid_data[i+1] == 0x00 and edid_data[i+2] == 0x00:
            if edid_data[i+3] == 0xfc:
                name_bytes = edid_data[i+5:i+18]
                return name_bytes.rstrip(b'\x00\x0a').decode('latin-1', errors='ignore').strip()
    return ''


# 解析 EDID 物理尺寸
def parse_edid_physical_size(edid_data):
    if not edid_data or len(edid_data) < 73:
        return 0, 0
    width_mm = edid_data[21]
    height_mm = edid_data[22]
    return width_mm, height_mm


# 从 pw-dump 数据中提取所有 Link 对象
def _find_pw_links(pw_data):
    return [obj for obj in pw_data
            if isinstance(obj, dict)
            and obj.get('type') == 'PipeWire:Interface:Link']


# 从 pw-dump 数据中提取所有 Port 对象
def _find_pw_ports(pw_data):
    return [obj for obj in pw_data
            if isinstance(obj, dict)
            and obj.get('type') == 'PipeWire:Interface:Port']


# 获取指定节点的端口列表，direction 可选 'output' / 'input'
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


# 从 Link 对象构建链接详情
def _build_link_info(link_obj, pw_data):
    info = link_obj.get('info', {})
    props = info.get('props', {})
    link_id = link_obj.get('id')
    output_port = info.get('output-port-id')
    input_port = info.get('input-port-id')

    # 查找输出端口所属节点
    output_node_id = None
    output_node_name = ''
    input_node_id = None
    input_node_name = ''

    for port_obj in _find_pw_ports(pw_data):
        port_info = port_obj.get('info', {})
        port_id = port_obj.get('id')
        if port_id == output_port:
            output_node_id = port_info.get('node-id')
        if port_id == input_port:
            input_node_id = port_info.get('node-id')

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
