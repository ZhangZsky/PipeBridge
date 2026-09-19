import subprocess
import os
import json
import re
import shlex
import signal
import logging
import threading
import time

logger = logging.getLogger('PipeBridge')

_pw_env_logged = False
_pw_env_cache = None
_pw_uid_cache = None
_pw_user_cache = None

def _get_pw_user():
    """返回 PipeWire 的目标运行用户名。

    0.32 行为还原：PipeWire/WirePlumber 以 root 运行，socket 位于 /run/user/0，
    其他应用(root 运行的程序等)可经该路径发现声卡。
    "仅运行单用户"的约束不在于用户是谁，而在于 pgrep/pkill 始终按目标 UID 过滤：
    全系统只允许存在这一份 PW 实例，桌面用户等其他用户的 PW 进程既不会被误检，
    也不会被误杀（避免双实例冲突或守护失效）。
    """
    global _pw_user_cache
    if _pw_user_cache is not None:
        return _pw_user_cache
    _pw_user_cache = 'root'
    return 'root'

def _get_pw_uid():
    """返回 PipeWire 目标用户的 UID(root 恒为 0)。

    调用方以 `pgrep/pkill -u <uid>` 过滤目标用户的 PW 进程——uid 恒为 0 表示
    全系统仅 root 这一份实例，桌面用户等实例不会被误检/误杀。
    保留 None 返回契约(调用方已有判空分支)，当前实现不会返回 None。
    """
    global _pw_uid_cache
    if _pw_uid_cache is None:
        _pw_uid_cache = 0
    return _pw_uid_cache

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
    # PW 以 root 运行(0.32 行为), XDG 运行时目录为 /run/user/0,
    # pipewire-0 / pulse 等 socket 落在这里, 其他应用才能经此发现声卡。
    xdg_dir = f'/run/user/{_get_pw_uid()}'

    if not env.get('XDG_RUNTIME_DIR'):
        try:
            os.makedirs(xdg_dir, exist_ok=True)
            os.chmod(xdg_dir, 0o700)
        except OSError as e:
            logger.debug(f"创建 XDG 目录 {xdg_dir}: {e}")
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

def _read_log_tail(path, limit=500):
    # 读取日志文件尾部 limit 字符,不存在或读取失败返回空串
    if not os.path.exists(path):
        return ''
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read().strip()[-limit:]
    except OSError:
        return ''

def _stop_legacy_user_pw(service_name):
    # 升级兼容: 0.33 曾以 pipebridge 用户运行 PW, 升级到 root 模型后可能残留旧实例,
    # 与 root 实例争抢 ALSA 设备/造成双实例。启动前清掉遗留用户(非 root)的同名进程。
    legacy_user = (os.environ.get('TRIM_USERNAME') or '').strip() or 'pipebridge'
    try:
        legacy = subprocess.run(
            f"id -u {shlex.quote(legacy_user)}", shell=True, capture_output=True, text=True, timeout=5
        )
        if legacy.returncode != 0 or not legacy.stdout.strip().isdigit():
            return
        legacy_uid = int(legacy.stdout.strip())
        if legacy_uid == 0:
            return
        pg = run_command(f"pgrep -u {legacy_uid} -x {shlex.quote(service_name)} 2>/dev/null")
        if pg['stdout'].strip():
            logger.warning(f"发现旧版 {legacy_user} 用户的 {service_name} 残留进程({pg['stdout'].strip()})，清理中...")
            run_command(f"pkill -u {legacy_uid} -x {shlex.quote(service_name)} 2>/dev/null")
            time.sleep(1)
    except Exception as e:
        logger.debug(f"清理旧版用户 PW 残留失败: {e}")

def start_pw_service(service_name):
    pw_env = _get_pw_env()
    log_file = f"/tmp/{service_name}-0.log"

    cmd_check = run_command(f"command -v {service_name} 2>/dev/null")
    if not cmd_check['stdout'].strip():
        logger.error(f"{service_name} 命令不存在，请运行 install_init 安装系统依赖")
        return False

    # PW 以 root 运行(0.32 行为), socket 落在 /run/user/0。
    # pgrep/pkill 始终按目标 UID(-u 0)过滤: 全系统仅 root 这一份实例,
    # 桌面用户等其他用户的 PW 进程不会被误检为"已运行", 也不会被误杀。
    pw_uid = _get_pw_uid()
    if pw_uid is None:
        logger.error(f"无法解析 PipeWire 目标用户 UID，跳过启动 {service_name}")
        return False
    pg_result = run_command(f"pgrep -u {pw_uid} -x {shlex.quote(service_name)} 2>/dev/null")
    if pg_result['stdout'].strip():
        if service_name == 'pipewire' and not _pw_socket_exists():
            logger.warning(f"{service_name} 进程存在但 socket 缺失，重启进程...")
            run_command(f"pkill -u {pw_uid} -x {shlex.quote(service_name)} 2>/dev/null")
            time.sleep(1)
        else:
            return True

    _stop_legacy_user_pw(service_name)

    logger.debug(f"启动 {service_name} (用户: {_get_pw_user()})...")
    start_env = pw_env.copy()
    if service_name == 'wireplumber':
        start_env['WIREPLUMBER_DEBUG'] = '2'
    run_command(
        f"nohup {shlex.quote(service_name)} >{shlex.quote(log_file)} 2>&1 &",
        timeout=5, env=start_env
    )
    time.sleep(2 if service_name == 'pipewire' else 1)
    pg_result = run_command(f"pgrep -u {pw_uid} -x {shlex.quote(service_name)} 2>/dev/null")
    started = bool(pg_result['stdout'].strip())
    if not started:
        diag = _read_log_tail(log_file, 500)
        logger.warning(f"{service_name} 启动后未检测到进程，可能启动失败。日志: {diag[:300] if diag else '(空)'}")
    elif service_name == 'pipewire':
        if _pw_socket_exists():
            logger.info("pipewire 启动成功，socket 已创建")
        else:
            logger.warning("pipewire 进程存在但 socket 未创建，可能初始化卡住")
            if os.path.exists(log_file):
                diag = _read_log_tail(log_file, 500)
                logger.warning(f"pipewire 启动日志: {diag[:400] if diag else '(空)'}")
    return started

def stop_pw_service(service_name):
    # 仅清理目标用户(root)的 PW 进程; 桌面用户等其他用户实例不受影响
    pw_uid = _get_pw_uid()
    if pw_uid is None:
        logger.error(f"无法解析 PipeWire 目标用户 UID，跳过停止 {service_name}")
        return False
    run_command(f"pkill -u {pw_uid} -x {shlex.quote(service_name)} 2>/dev/null")
    time.sleep(0.5)
    return True

# 安全策略：run_command 使用 shell=True 以支持管道、重定向等合法 shell 语法
# （如 "pgrep -u 1000 -x pipewire 2>/dev/null"、"systemctl is-active bluetooth 2>/dev/null"）。
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
        # start_new_session=True 使子进程成为新进程组组长，
        # 超时时可用 os.killpg 回收整个进程组（含 shell 派生的孙子进程），
        # 否则 subprocess.run 超时仅杀 shell 自身，管道/后台子进程会泄漏。
        proc = subprocess.Popen(
            _validate_command(cmd),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            env=cmd_env,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return {
                "success": proc.returncode == 0,
                "stdout": (stdout or "").strip(),
                "stderr": (stderr or "").strip(),
                "returncode": proc.returncode
            }
        except subprocess.TimeoutExpired:
            # 回收整个进程组：先 SIGTERM，短暂等待后仍存活则 SIGKILL
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                try:
                    proc.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
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

def get_node_name_by_id(node_id):
    # node_id 可能是字符串(来自 wpctl 解析)或整数,统一转 int 以匹配 pw-dump 的 obj['id'](整数)。
    try:
        node_id = int(node_id)
    except (TypeError, ValueError):
        return ''
    pw_data = pw_dump()
    obj = find_pw_node(pw_data, node_id=node_id)
    return obj.get('info', {}).get('props', {}).get('node.name', '') if obj else ''

def _parse_wpctl_default_id():
    # 从 wpctl status 提取默认 sink/source 的节点 id(带 * 行的第一段数字),
    # 供 _get_default_node_name 反查 node.name。返回 ('', '') 表示未取到。
    result = run_command("wpctl status 2>/dev/null", timeout=5)
    if not result['success'] or not result['stdout']:
        return '', ''
    default_sink_id = ''
    default_source_id = ''
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
            m = re.search(r'\*\s+(\d+)\.', stripped)
            if m:
                if section == 'sink':
                    default_sink_id = m.group(1)
                elif section == 'source':
                    default_source_id = m.group(1)
    return default_sink_id, default_source_id

def _get_default_node_name(kind):
    # 获取系统当前默认音频节点名(kind: 'sink' 或 'source')——纯运行时读取,不涉及 PipeBridge 持久化。
    # 优先 pw-metadata: 它返回真正的 node.name(如 alsa_output.pci-xxxx),可与设备列表 name 精确匹配。
    # 关键:wpctl set-default 写入的是默认 metadata 命名空间(metadata id 0),而非 settings 命名空间;
    #       故必须读默认命名空间(不带 -n settings)。key='default.audio.{kind}' 才是生效值,
    #       key='default.configured.audio.{kind}' 是期望配置值,需排除避免拿到未生效项。
    # wpctl status 带 * 行显示的是 description(友好名),无法与 node.name 匹配,故仅作最后兜底且需换算。
    result = run_command(
        f"pw-metadata 0 2>/dev/null | grep \"'default.audio.{kind}'\"", timeout=5)
    if result['success'] and result['stdout']:
        for line in result['stdout'].splitlines():
            if f"default.configured.audio.{kind}" in line:
                continue
            if f"default.audio.{kind}" not in line:
                continue
            m = re.search(r'\"name\"\s*:\s*\"([^\"]+)\"', line)
            if not m:
                m = re.search(r'node:name:([^\"\s]+)', line)
            if m:
                return m.group(1)
    # 兜底:wpctl 拿到的是默认设备 id,用 id 反查 node.name
    sink_id, source_id = _parse_wpctl_default_id()
    node_id = sink_id if kind == 'sink' else source_id
    if node_id:
        name = get_node_name_by_id(node_id)
        if name:
            return name
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

def _avail_bool(value):
    # pw-dump 中 available 值形态不一(pod 序列化为 'yes'/'no'/'unknown' 字符串或布尔)。
    # 统一归一化为布尔：仅明确为 no/False 才算不可用，'unknown'(未探测)视为可用。
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() != 'no'

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
            # 可用性透传给前端：不可用端口(如未接显示器的 HDMI 口)在下拉中禁用，
            # 避免 wpctl set-route 被拒绝导致"端口切换失败"
            'available': _avail_bool(er.get('available')),
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
                content = _read_log_tail(log_file, 800)
                if content:
                    logger.warning(f"PipeWire 日志诊断 ({log_file}): {content}")
                    break

        for log_file in ['/tmp/wireplumber-0.log', '/tmp/wireplumber.log']:
            if os.path.exists(log_file):
                content = _read_log_tail(log_file, 800)
                if content:
                    logger.warning(f"WirePlumber 日志诊断 ({log_file}): {content}")
                    break
    except Exception as e:
        logger.warning(f"PipeWire 诊断异常: {e}")

def pw_dump():
    global _pw_dump_cache, _pw_dump_cache_time
    now = time.time()
    with _pw_dump_lock:
        if _pw_dump_cache is not None and (now - _pw_dump_cache_time) < _PW_DUMP_CACHE_TTL:
            return _pw_dump_cache

    result = run_command("pw-dump 2>/dev/null", timeout=3)
    # 瞬时失败(超时/PipeWire 忙)会导致设备列表突然清空 —— 表现为"重新打开界面声卡消失"。
    # 命令失败或输出为空时先做一次快速重试,给 PipeWire 一个稳定窗口,尽量避免误判为"无设备"。
    if not result['success'] or not (result.get('stdout') or '').strip():
        time.sleep(0.3)
        retry = run_command("pw-dump 2>/dev/null", timeout=3)
        if retry['success'] and (retry.get('stdout') or '').strip():
            result = retry
    if not result['success']:
        logger.info(f"pw-dump 执行失败: returncode={result.get('returncode', '?')}, stderr='{result.get('stderr', '')[:200]}'")
        _diagnose_pw_failure()
        # 不做长负缓存:失败仅短暂缓存,使下次请求能立即重试而非在 9s 内持续返回空致设备消失
        with _pw_dump_lock:
            _pw_dump_cache = []
            _pw_dump_cache_time = now - _PW_DUMP_CACHE_TTL + 0.3
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
            # 归一化为布尔：原值可能是 'yes'/'no' 字符串，此前 `is False` 类判断全部失效
            'available': _avail_bool(ep.get('available')),
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

def parse_edid_dtd_modes(edid_data):
    # 解析 EDID 的 DTD(Detailed Timing Descriptor)生成带刷新率的显示模式列表。
    # 背景: sysfs /sys/class/drm/*/modes 每行只有 "WxH"(同一分辨率的不同刷新率模式
    # 会产生重复行)且不含刷新率，导致前端"支持格式"重复显示分辨率、"刷新率"下拉恒为空。
    # EDID DTD 是内核态之外唯一可靠携带完整时序的来源。
    # 返回 ["WxH@Hz", ...](如 "1920x1080@60Hz"/"1280x720@59.94Hz")，按 EDID 顺序
    # (首个 DTD 即 preferred mode)，同模式去重；数据非法时返回空列表。
    if not edid_data or len(edid_data) < 128 or edid_data[0] != 0x00 or edid_data[1] != 0xFF:
        return []

    def _dtd_to_mode(dtd):
        # DTD 前 8 字节: [0:2] pixel clock(10kHz LE, 0=非时序描述符) [2:8] H/V active/blank
        # (active 与 blank 各 9 位: 低 8 位 + 相邻高位半字节拼合)
        pclk_khz = dtd[0] | (dtd[1] << 8)
        if pclk_khz == 0:
            return ''
        hactive = dtd[2] | ((dtd[4] & 0xF0) << 4)
        hblank = dtd[3] | ((dtd[4] & 0x0F) << 8)
        vactive = dtd[5] | ((dtd[7] & 0xF0) << 4)
        vblank = dtd[6] | ((dtd[7] & 0x0F) << 8)
        htotal = hactive + hblank
        vtotal = vactive + vblank
        if hactive <= 0 or vactive <= 0 or htotal <= 0 or vtotal <= 0:
            return ''
        hz = pclk_khz * 10000 / (htotal * vtotal)
        if hz <= 1 or hz > 1000:
            return ''
        hz_text = f'{hz:.2f}'.rstrip('0').rstrip('.')
        return f'{hactive}x{vactive}@{hz_text}Hz'

    modes = []
    seen = set()

    def _collect(dtds):
        for dtd in dtds:
            mode = _dtd_to_mode(dtd)
            if mode and mode not in seen:
                seen.add(mode)
                modes.append(mode)

    # base block: 偏移 54..125 的 4 个 18 字节描述符(pixel clock 为 0 的是名称/范围等非时序项)
    _collect([edid_data[i:i + 18] for i in range(54, 126, 18)])
    # CTA-861 扩展块: byte[0]==0x02，byte[2] 为 DTD 区偏移(0 = 无 DTD)
    ext_count = edid_data[126]
    for k in range(1, min(ext_count, (len(edid_data) // 128) - 1) + 1):
        block = edid_data[k * 128:(k + 1) * 128]
        if len(block) < 128 or block[0] != 0x02:
            continue
        dtd_offset = block[2]
        if 4 <= dtd_offset < 128 - 17:
            _collect([block[j:j + 18] for j in range(dtd_offset, 128 - 17, 18)])
    return modes

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
