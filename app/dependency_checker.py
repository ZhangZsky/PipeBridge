import os
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from utils import run_command, start_pw_service, _pw_socket_exists
import config
from exceptions import CommandError, MediaBridgeError

logger = logging.getLogger('MediaBridge')

_overview_executor = ThreadPoolExecutor(max_workers=5)  # 复用线程池

DEPENDENCIES = {
    'packages': [
        {'name': 'pipewire', 'desc': 'PipeWire 音频服务', 'critical': True, 'type': 'audio-core'},
        {'name': 'pipewire-pulse', 'desc': 'PipeWire PulseAudio 兼容层', 'critical': True, 'type': 'audio-core'},
        {'name': 'wireplumber', 'desc': 'WirePlumber 会话管理', 'critical': True, 'type': 'audio-core'},
        {'name': 'alsa-utils', 'desc': 'ALSA 工具(aplay/arecord)', 'critical': False, 'type': 'audio-core'},
        {'name': 'libspa-0.2-bluetooth', 'desc': 'PipeWire 蓝牙支持', 'critical': True, 'type': 'bluetooth'},
        {'name': 'bluez', 'desc': '蓝牙协议栈', 'critical': True, 'type': 'bluetooth'},
        {'name': 'python3-dbus', 'desc': 'Python D-Bus 支持', 'critical': True, 'type': 'python'},
        {'name': 'python3-gi', 'desc': 'PyGObject (GLib bindings)', 'critical': True, 'type': 'python'},
        {'name': 'python3-fastapi', 'desc': 'FastAPI Web框架', 'critical': True, 'type': 'python'},
        {'name': 'python3-uvicorn', 'desc': 'Uvicorn ASGI服务器', 'critical': True, 'type': 'python'},
    ],
    'services': [
        {'name': 'bluetooth', 'desc': '蓝牙服务', 'critical': True, 'type': 'bluetooth'},
        {'name': 'dbus', 'desc': 'D-Bus 系统消息总线', 'critical': True, 'type': 'system'},
    ],
    'commands': [
        {'name': 'bluetoothctl', 'desc': '蓝牙控制(配对移除)', 'critical': False, 'type': 'bluetooth'},
        {'name': 'wpctl', 'desc': 'WirePlumber 控制', 'critical': True, 'type': 'audio-core'},
        {'name': 'pw-dump', 'desc': 'PipeWire 信息查询', 'critical': True, 'type': 'audio-core'},
        {'name': 'pw-metadata', 'desc': 'PipeWire 默认设备查询', 'critical': True, 'type': 'audio-core'},
        {'name': 'pw-cli', 'desc': 'PipeWire 节点参数控制', 'critical': True, 'type': 'audio-core'},
        {'name': 'pw-play', 'desc': 'PipeWire 音频播放', 'critical': True, 'type': 'audio-core'},
        {'name': 'pactl', 'desc': 'PulseAudio 兼容控制', 'critical': True, 'type': 'audio-core'},
    ]
}


# 检查 systemd 服务是否运行（root 下用 pgrep 检查用户级服务，systemctl 检查系统级服务）
def _check_service_running(service_name, user=False):
    if user:
        # root 下 systemctl --user 不可用，直接用 pgrep
        pg_result = run_command(f"pgrep -x {service_name} 2>/dev/null")
        return bool(pg_result['stdout'].strip())
    # 系统级服务用 systemctl
    result = run_command(f"systemctl is-active {service_name} 2>/dev/null")
    return result['stdout'].strip() == 'active'


# 检查 dpkg 包是否已安装
def check_package_installed(pkg_name):
    result = run_command(f"dpkg -s {pkg_name} 2>/dev/null | grep -c '^Status: install ok installed'")
    return result['stdout'].strip() == '1' if result['stdout'] else False

# 检查系统级 systemd 服务是否 active
def check_service_active(service_name):
    result = run_command(f"systemctl is-active {service_name} 2>/dev/null")
    return result['stdout'].strip() == 'active'

# 检查命令是否在 PATH 中
def check_command_exists(cmd):
    result = run_command(f"which {cmd} 2>/dev/null")
    return bool(result['stdout'].strip())

# 检查 PipeWire 是否运行
def check_pipewire_running():
    return _check_service_running('pipewire', user=True)

def check_wireplumber_running():
    return _check_service_running('wireplumber', user=True)

# 检查 pipewire-pulse 是否运行
def check_pipewire_pulse_running():
    return _check_service_running('pipewire-pulse', user=True)

# 安装并启动 PipeWire + WirePlumber，已运行则跳过
def setup_pipewire():
    if check_pipewire_running() and check_wireplumber_running():
        return {'message': '已运行'}

    logger.debug("PipeWire/WirePlumber 未运行，开始配置...")

    # 运行时不执行 apt-get install，包安装由 install_init 负责
    # 此处仅检测命令是否存在，缺失时给出明确错误提示
    if not check_command_exists("pipewire"):
        logger.error("pipewire 命令不存在，请运行 install_init 安装系统依赖")
        raise CommandError('PipeWire 未安装，请运行 install_init 安装系统依赖')
    if not check_command_exists("wireplumber"):
        logger.error("wireplumber 命令不存在，请运行 install_init 安装系统依赖")
        raise CommandError('WirePlumber 未安装，请运行 install_init 安装系统依赖')

    # 使用统一的 start_pw_service 启动（兼容 root 和普通用户）
    start_pw_service('pipewire')
    # 等待 PipeWire socket 创建（最多 5 秒），socket 就绪后 WirePlumber 才能连接
    for _ in range(10):
        if _pw_socket_exists():
            break
        time.sleep(0.5)
    else:
        logger.warning("PipeWire socket 未创建，后续服务可能启动失败")

    start_pw_service('pipewire-pulse')
    time.sleep(0.5)
    start_pw_service('wireplumber')
    time.sleep(1)

    if not check_pipewire_running() or not _pw_socket_exists():
        logger.error("PipeWire 启动失败（进程或 socket 异常）")
        raise CommandError('PipeWire 启动失败')

    logger.debug("PipeWire 服务启动成功")
    return {'message': '已启动'}

# 获取所有依赖项状态（包、服务、命令），带防重入缓存
def check_spa_bluetooth_plugin():
    """检查 SPA 蓝牙插件 .so 文件是否实际存在"""
    result = run_command("dpkg -L libspa-0.2-bluetooth 2>/dev/null | grep -E '\\.so$' | head -1", timeout=5)
    if result['success'] and result['stdout'].strip():
        so_file = result['stdout'].strip()
        return os.path.exists(so_file)
    return False


def check_bluetooth_audio_ready():
    """检查 BlueZ MediaEndpoint1 是否已注册（WirePlumber 蓝牙音频就绪）"""
    try:
        import bluetooth_manager
        return bluetooth_manager.check_bluetooth_audio_ready()
    except Exception:
        return False


def _safe_check_bluetooth_audio(timeout=5):
    # 带 5 秒超时保护，防止 D-Bus 调用阻塞系统概览
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(check_bluetooth_audio_ready)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            logger.warning("蓝牙音频就绪检查超时，跳过")
            return False


def get_all_status():
    logger.debug("获取所有依赖状态...")
    status = {
        'packages': [],
        'services': [],
        'commands': [],
        'pipewire': {},
        'wireplumber': {},
        'pipewire_pulse': {},
        'spa_bluetooth_plugin': False,
        'bluetooth_audio_ready': False,
        'all_ok': True,
        'critical_missing': []
    }

    for pkg in DEPENDENCIES['packages']:
        installed = check_package_installed(pkg['name'])
        status['packages'].append({
            'name': pkg['name'],
            'desc': pkg['desc'],
            'installed': installed,
            'critical': pkg['critical'],
            'type': pkg['type']
        })
        if not installed and pkg['critical']:
            status['critical_missing'].append(pkg['name'])
            status['all_ok'] = False

    pw_running = check_pipewire_running()
    status['pipewire'] = {
        'running': pw_running,
        'desc': 'PipeWire 服务'
    }
    if not pw_running:
        status['all_ok'] = False

    # WirePlumber 服务运行状态（蓝牙音频 profile 注册的关键服务）
    wp_running = check_wireplumber_running()
    status['wireplumber'] = {
        'running': wp_running,
        'desc': 'WirePlumber 会话管理'
    }
    if not wp_running:
        status['critical_missing'].append('wireplumber(service)')
        status['all_ok'] = False

    # pipewire-pulse 服务运行状态
    pwp_running = check_pipewire_pulse_running()
    status['pipewire_pulse'] = {
        'running': pwp_running,
        'desc': 'PipeWire PulseAudio 兼容层'
    }
    if not pwp_running:
        status['critical_missing'].append('pipewire-pulse(service)')
        status['all_ok'] = False

    for svc in DEPENDENCIES['services']:
        active = check_service_active(svc['name'])
        status['services'].append({
            'name': svc['name'],
            'desc': svc['desc'],
            'active': active,
            'critical': svc['critical'],
            'type': svc['type']
        })
        if not active and svc['critical']:
            status['critical_missing'].append(svc['name'])
            status['all_ok'] = False

    for cmd in DEPENDENCIES['commands']:
        exists = check_command_exists(cmd['name'])
        status['commands'].append({
            'name': cmd['name'],
            'desc': cmd['desc'],
            'exists': exists,
            'critical': cmd['critical'],
            'type': cmd['type']
        })
        if not exists and cmd['critical']:
            status['critical_missing'].append(cmd['name'])
            status['all_ok'] = False

    # SPA 蓝牙插件 .so 文件实际存在性（包已安装不代表 .so 可用）
    spa_ok = check_spa_bluetooth_plugin()
    status['spa_bluetooth_plugin'] = spa_ok
    if not spa_ok:
        status['critical_missing'].append('spa-bluetooth-plugin(.so)')
        status['all_ok'] = False

    # 蓝牙音频端点就绪状态（仅作信息展示，不影响 all_ok）
    # 原因: 端点注册依赖蓝牙设备已连接，无设备连接时端点未注册是正常现象，不应视为故障
    bt_audio_ready = _safe_check_bluetooth_audio(timeout=5)
    status['bluetooth_audio_ready'] = bt_audio_ready

    return status

# 安装缺失的关键包
def install_missing_packages():
    missing = [pkg['name'] for pkg in DEPENDENCIES['packages']
               if pkg['critical'] and not check_package_installed(pkg['name'])]

    if not missing:
        return {'message': '已安装'}

    result = run_command(f"apt-get install -y -qq {' '.join(missing)}", timeout=120)

    if result['success']:
        return {'message': f'已安装 {len(missing)} 个包'}

    raise CommandError('安装失败')

# 启动未运行的系统服务
def start_missing_services():
    errors = []
    for svc in DEPENDENCIES['services']:
        if not check_service_active(svc['name']):
            result = run_command(f"systemctl start {svc['name']} 2>/dev/null")
            if not result['success']:
                errors.append(svc['name'])

    if errors:
        raise CommandError(f'启动失败: {", ".join(errors)}')

    return {'message': '已运行'}


# 获取系统综合概览
def get_system_overview():
    return _build_overview()


# 并行构建系统概览
def _build_overview():
    import audio_manager
    import video_manager
    import bluetooth_manager
    from concurrent.futures import as_completed

    pipewire_running = check_pipewire_running()
    wireplumber_running = check_wireplumber_running()
    bluetooth_active = check_service_active('bluetooth')
    pipewire_pulse_running = check_pipewire_pulse_running()
    dbus_running = check_service_active('dbus')

    # 并行获取各子系统数据，避免串行等待
    audio_devices_list = []
    audio_default = ''
    video_devices_list = []
    video_default = ''
    deps_status = {}
    reconnect_status = {'monitoring': False, 'reconnecting_devices': [], 'manual_disconnects': []}
    bt_connected = 0

    def _fetch_audio():
        result = audio_manager.get_audio_devices()
        devices = result.get('devices', [])
        default = result.get('default', '')
        return devices, default

    # 并行获取视频设备
    def _fetch_video():
        result = video_manager.get_video_devices()
        devices = result.get('devices', [])
        default = result.get('default', '')
        return devices, default

    # 并行获取依赖状态
    def _fetch_deps():
        return get_all_status()

    # 并行获取重连状态
    def _fetch_reconnect():
        try:
            return bluetooth_manager.get_reconnect_status()
        except Exception:
            return {'monitoring': False, 'reconnecting_devices': [], 'manual_disconnects': []}

    # 并行获取蓝牙连接数（带超时保护）
    def _fetch_bt_connected():
        try:
            bt_paired = bluetooth_manager.get_paired_devices()
            if isinstance(bt_paired, list):
                return sum(1 for d in bt_paired if d.get('connected'))
        except Exception:
            pass
        return 0

    futures = {
        _overview_executor.submit(_fetch_audio): 'audio',
        _overview_executor.submit(_fetch_video): 'video',
        _overview_executor.submit(_fetch_deps): 'deps',
        _overview_executor.submit(_fetch_reconnect): 'reconnect',
        _overview_executor.submit(_fetch_bt_connected): 'bt_connected',
    }
    try:
        for future in as_completed(futures, timeout=8):
            key = futures[future]
            try:
                if key == 'audio':
                    audio_devices_list, audio_default = future.result(timeout=5)
                elif key == 'video':
                    video_devices_list, video_default = future.result(timeout=5)
                elif key == 'deps':
                    deps_status = future.result(timeout=5)
                elif key == 'reconnect':
                    reconnect_status = future.result(timeout=5)
                elif key == 'bt_connected':
                    bt_connected = future.result(timeout=5)
            except Exception as e:
                logger.warning(f"获取 {key} 数据失败: {e}")
    except Exception as e:
        # as_completed 超时或其他异常
        logger.warning(f"系统概览并行获取异常: {e}")

    # 判断核心服务是否全部正常
    all_ok = pipewire_running and wireplumber_running and pipewire_pulse_running and bluetooth_active and deps_status.get('all_ok', False)

    # bluetooth_audio_ready 优先使用 deps_status 的结果（避免重复调用 D-Bus 浪费超时）
    # deps_status 获取失败时 fallback 到 _safe_check_bluetooth_audio()
    bt_audio_ready = deps_status.get('bluetooth_audio_ready')
    if bt_audio_ready is None:
        bt_audio_ready = _safe_check_bluetooth_audio()

    # 蓝牙硬件检测
    bt_hardware_detected = False
    try:
        usb_devices = bluetooth_manager.check_bluetooth_hardware()
        controllers = bluetooth_manager.get_all_controllers()
        bt_hardware_detected = bool(usb_devices) or bool(controllers)
    except Exception:
        pass

    overview = {
        'pipewire': pipewire_running,
        'wireplumber': wireplumber_running,
        'pipewire_pulse': pipewire_pulse_running,
        'dbus': dbus_running,
        'bluetooth_service': bluetooth_active,
        'bluetooth_audio_ready': bt_audio_ready,
        'bluetooth_hardware': bt_hardware_detected,
        'spa_bluetooth_plugin': check_spa_bluetooth_plugin(),
        'audio_devices': {
            'count': len(audio_devices_list),
            'default': audio_default,
        },
        'video_devices': {
            'count': len(video_devices_list),
            'default': video_default,
        },
        'bluetooth_connected': bt_connected,
        'dependencies': deps_status,
        'auto_reconnect': reconnect_status,
        'all_ok': all_ok,
    }

    return overview


# 一键修复：安装缺失包、启动 PipeWire、启动服务
def fix_all():
    results = {}
    try:
        results['packages'] = install_missing_packages()
    except MediaBridgeError as e:
        results['packages'] = {'error': str(e)}

    try:
        results['pipewire'] = setup_pipewire()
    except MediaBridgeError as e:
        results['pipewire'] = {'error': str(e)}

    try:
        results['services'] = start_missing_services()
    except MediaBridgeError as e:
        results['services'] = {'error': str(e)}

    try:
        import bluetooth_manager
        if not check_bluetooth_audio_ready():
            logger.info("蓝牙音频未就绪，尝试修复...")
            bluetooth_manager.ensure_wireplumber_bluez_config()
            if check_wireplumber_running():
                start_pw_service('wireplumber')
                time.sleep(1)
        results['bluetooth_audio'] = {'ready': check_bluetooth_audio_ready()}
    except Exception as e:
        logger.warning(f"蓝牙音频修复失败: {e}")
        results['bluetooth_audio'] = {'error': str(e)}
    results['status'] = get_all_status()
    return results