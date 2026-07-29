"""系统管理器 —— 依赖检查 + WirePlumber 配置管理

整合 dependency_checker 和 wp_config_manager：
- 依赖检查: 检查系统包、服务、命令是否就绪，提供系统综合概览和一键修复
- WirePlumber 配置: 统一管理 WP 规则部署，包括蓝牙配置、IEC958 规则、防挂起规则等
"""

import os
import re
import time
import logging
from concurrent.futures import ThreadPoolExecutor

from utils import run_command, start_pw_service, stop_pw_service, _get_pw_env, _pw_socket_exists
import platform_paths
from exceptions import CommandError, PipeBridgeError, ConfigError

logger = logging.getLogger('PipeBridge')

_overview_executor = ThreadPoolExecutor(max_workers=5)  # 复用线程池


# ============================================================================
# 依赖检查
# ============================================================================

DEPENDENCIES = {
    'packages': [
        {'name': 'pipewire', 'desc': 'PipeWire 音频服务', 'critical': True, 'type': 'audio-core'},
        {'name': 'pipewire-pulse', 'desc': 'PipeWire PulseAudio 兼容层', 'critical': True, 'type': 'audio-core'},
        {'name': 'wireplumber', 'desc': 'WirePlumber 会话管理', 'critical': True, 'type': 'audio-core'},
        {'name': 'libspa-0.2-bluetooth', 'desc': 'PipeWire 蓝牙支持', 'critical': True, 'type': 'bluetooth'},
        {'name': 'alsa-utils', 'desc': 'speaker-test 测试工具（非 ALSA 操作）', 'critical': False, 'type': 'audio-core'},
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
        {'name': 'speaker-test', 'desc': '声道测试工具（通过 pipewire-alsa）', 'critical': False, 'type': 'audio-core'},
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

# 检查 SPA 蓝牙插件 .so 文件是否实际存在
def check_spa_bluetooth_plugin():
    result = run_command("dpkg -L libspa-0.2-bluetooth 2>/dev/null | grep -E '\\.so$' | head -1", timeout=5)
    if result['success'] and result['stdout'].strip():
        so_file = result['stdout'].strip()
        return os.path.exists(so_file)
    return False


# 检查 BlueZ MediaEndpoint1 是否已注册（WirePlumber 蓝牙音频就绪）
def check_bluetooth_audio_ready():
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


# 获取所有依赖项状态（包、服务、命令）
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
    except PipeBridgeError as e:
        results['packages'] = {'error': str(e)}

    try:
        results['pipewire'] = setup_pipewire()
    except PipeBridgeError as e:
        results['pipewire'] = {'error': str(e)}

    try:
        results['services'] = start_missing_services()
    except PipeBridgeError as e:
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


# ============================================================================
# WirePlumber 配置管理
# ============================================================================

# 诊断蓝牙音频端点未注册的原因，输出关键信息帮助定位
def _diagnose_bluetooth_audio_failure():
    # 1. 读取 WirePlumber 日志文件（nohup 重定向的 stderr/stdout）
    try:
        log_file = '/tmp/wireplumber-0.log'
        if os.path.exists(log_file):
            with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                log_content = f.read().strip()
            if log_content:
                logger.warning(f"WirePlumber 日志末尾: ...{log_content[-800:]}")
            else:
                logger.warning("WirePlumber 日志文件为空（可能需要 WIREPLUMBER_DEBUG=2 获取详细日志）")
        else:
            logger.warning("WirePlumber 日志文件不存在: /tmp/wireplumber-0.log")
    except OSError as e:
        logger.warning(f"读取 WirePlumber 日志失败: {e}")

    # 2. 检查 SPA 蓝牙插件 .so 文件（WirePlumber 加载蓝牙模块的前提）
    spa_result = run_command("dpkg -L libspa-0.2-bluetooth 2>/dev/null | grep -E '\\.so$' | head -5")
    if spa_result['success'] and spa_result['stdout'].strip():
        logger.info(f"SPA 蓝牙插件文件: {spa_result['stdout'].strip()[:200]}")
    else:
        logger.error("libspa-0.2-bluetooth 包未安装或 .so 文件缺失，WirePlumber 无法加载蓝牙模块")

    # 3. 检查 BlueZ 适配器是否存在且上电（无适配器则 Media1 接口不暴露）
    hci_result = run_command("hciconfig 2>/dev/null", timeout=3)
    if hci_result['success'] and hci_result['stdout'].strip():
        logger.info(f"蓝牙适配器状态: {hci_result['stdout'].strip()[:300]}")
    else:
        logger.error("未检测到蓝牙适配器（hciconfig 无输出），BlueZ 可能未暴露 Media1 接口")

    # 4. 检查 WirePlumber 是否加载了蓝牙组件
    wp_status = run_command("wpctl status 2>/dev/null", timeout=5)
    if wp_status['success'] and wp_status['stdout']:
        status_lower = wp_status['stdout'].lower()
        if 'bluez' in status_lower or 'bluetooth' in status_lower:
            logger.info("WirePlumber 状态中包含蓝牙组件引用")
        else:
            logger.warning("WirePlumber 状态中未发现蓝牙组件，蓝牙模块可能未加载（检查配置文件或 libspa-0.2-bluetooth）")
    else:
        logger.warning("wpctl status 执行失败，无法确认 WirePlumber 组件加载状态")

    # 5. 检查 WirePlumber 版本（0.5+ 使用 SPA-JSON 配置，0.4.x 格式不同）
    wp_ver = run_command("wireplumber --version 2>/dev/null | head -1", timeout=3)
    if wp_ver['success'] and wp_ver['stdout'].strip():
        logger.info(f"WirePlumber 版本: {wp_ver['stdout'].strip()}")
    else:
        logger.warning("无法获取 WirePlumber 版本")


class WPConfigManager:
    """WirePlumber 配置统一管理器，负责查找配置目录、部署规则、清理旧配置"""

    # 查找所有 WirePlumber 配置目录（系统级 + 用户级）
    def find_config_dirs(self):
        dirs = []

        # 系统级配置目录（root 可写，对所有用户生效）
        dirs.append(platform_paths.WP_SYSTEM_CONF_DIR)

        # 用户级配置：通过 _get_pw_env() 获取正确的 XDG_RUNTIME_DIR
        pw_env = _get_pw_env()
        xdg = pw_env.get('XDG_RUNTIME_DIR', '')
        if xdg:
            uid = xdg.replace('/run/user/', '')
            if uid.isdigit():
                home = run_command(
                    f"getent passwd {uid} 2>/dev/null | cut -d: -f6", timeout=3
                )
                if home['success'] and home['stdout']:
                    home_dir = home['stdout'].strip()
                    dirs.append(f"{home_dir}/{platform_paths.WP_USER_CONF_SUBDIR}")

        # 回退：尝试常见 uid（1000、1001）
        for uid_str in ['1000', '1001']:
            h = run_command(
                f"getent passwd {uid_str} 2>/dev/null | cut -d: -f6", timeout=3
            )
            if h['success'] and h['stdout']:
                home_dir = h['stdout'].strip()
                d = f"{home_dir}/{platform_paths.WP_USER_CONF_SUBDIR}"
                if d not in dirs:
                    dirs.append(d)
                break

        return dirs

    # 部署 WirePlumber 规则到所有配置目录
    def deploy_rule(self, rule_name, content):
        config_dirs = self.find_config_dirs()
        logger.info(f"WirePlumber 配置目录候选: {config_dirs}")
        results = {}

        for wp_dir in config_dirs:
            rule_file = f"{wp_dir}/{rule_name}.conf"

            # 检查规则是否已存在且内容一致（跳过无变化的部署）
            if os.path.exists(rule_file):
                try:
                    with open(rule_file, 'r', encoding='utf-8') as f:
                        existing = f.read()
                    if existing == content:
                        logger.debug(f"WirePlumber 规则已存在且内容一致: {rule_file}")
                        results[wp_dir] = True
                        continue
                    else:
                        logger.info(f"WirePlumber 规则内容需要更新: {rule_file}")
                except OSError:
                    pass

            # 创建配置目录并写入规则文件
            os.makedirs(wp_dir, exist_ok=True)
            try:
                with open(rule_file, 'w', encoding='utf-8') as f:
                    f.write(content)
                logger.info(f"已部署 WirePlumber 规则: {rule_file}")
                results[wp_dir] = True
            except OSError as e:
                logger.warning(f"部署规则失败: {rule_file}, {e}")
                results[wp_dir] = False

        return results

    # 重启 WirePlumber 使新部署的 monitor.alsa.rules 等规则生效
    def restart_wireplumber(self):
        """重启 WirePlumber 会话管理器，令新写入的规则文件真正加载生效。

        deploy_rule 只写文件不重载，WirePlumber 仅在启动时读取 conf.d 规则，
        因此部署降权/防挂起等规则后必须重启 WirePlumber 才会生效。
        重启会短暂中断音频路由（启动阶段可接受）。
        """
        logger.info("重启 WirePlumber 以加载新规则...")
        stop_pw_service('wireplumber')
        time.sleep(0.5)
        started = start_pw_service('wireplumber')
        if started:
            logger.info("WirePlumber 已重启，新规则生效")
        else:
            logger.warning("WirePlumber 重启后未检测到进程，规则可能未生效")
        return started

    # 清理旧版 WirePlumber 配置文件
    def cleanup_legacy(self, patterns):
        for pattern in patterns:
            if os.path.exists(pattern):
                try:
                    os.remove(pattern)
                    logger.info(f"已删除旧配置: {pattern}")
                except OSError as e:
                    logger.debug(f"删除旧配置失败: {pattern}, {e}")

    # 部署 IEC958 数字音频规则
    def deploy_iec958_rule(self, need_iec958=None):
        """自动检测是否需要 IEC958 规则（基于 PipeWire 设备 profile 信息）"""
        if need_iec958 is None:
            need_iec958 = False
            try:
                from utils import pw_dump
                pw_data = pw_dump()
                for obj in pw_data:
                    if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Device':
                        continue
                    params = obj.get('info', {}).get('params', {})
                    if not isinstance(params, dict):
                        continue
                    enum_profiles = params.get('EnumProfile', [])
                    if isinstance(enum_profiles, dict):
                        enum_profiles = [enum_profiles]
                    profile_names = [ep.get('name', '') for ep in enum_profiles if isinstance(ep, dict)]
                    has_iec958 = any('iec958' in pn.lower() for pn in profile_names)
                    has_analog = any('analog' in pn.lower() for pn in profile_names)
                    has_hdmi = any('hdmi' in pn.lower() for pn in profile_names)
                    # 只有 IEC958 输出（无 HDMI、无 analog）的声卡才需要此规则
                    if has_iec958 and not has_analog and not has_hdmi:
                        dev_name = obj.get('info', {}).get('props', {}).get('device.name', '')
                        need_iec958 = True
                        logger.debug(f"声卡 {dev_name} 只有 IEC958 输出，需要 IEC958 规则")
                        break
            except Exception as e:
                logger.debug(f"IEC958 检测失败: {e}")

        # IEC958 规则内容
        if need_iec958:
            content = """# PipeBridge: 启用 IEC958 数字音频设备
# WirePlumber 默认只为有模拟输出的声卡创建 Sink
# 此规则让只有 IEC958 (S/PDIF) 输出的声卡也能被识别
# 注意：仅对无模拟/HDMI输出的声卡生效
monitor.alsa.rules = [
  {
    matches = [
      { "device.name" = "~alsa_card.*" }
      { "device.profile-names" = "~.*iec958.*" }
    ]
    actions = {
      update-props = {
        device.profile = "iec958-stereo"
      }
    }
  }
]
"""
        else:
            # 不需要 IEC958 规则，写入空规则避免影响 HDMI 声卡
            content = """# PipeBridge: IEC958 规则（当前系统不需要，已禁用）
# 当系统只有 IEC958 输出的声卡时，此规则会被自动激活
"""

        result = self.deploy_rule(
            rule_name='51-pipebridge-iec958',
            content=content,
        )

        return result

    # 移除 PC Speaker 黑名单规则，让蜂鸣器设备正常注册
    def deploy_pcspkr_blacklist(self):
        conf_dir = platform_paths.WP_SYSTEM_CONF_DIR
        conf_file = os.path.join(conf_dir, "52-pipebridge-pcspkr-blacklist.conf")

        removed = False
        if os.path.exists(conf_file):
            try:
                os.remove(conf_file)
                removed = True
                logger.info(f"已移除蜂鸣器黑名单规则: {conf_file}")
            except OSError as e:
                logger.warning(f"移除蜂鸣器黑名单规则失败: {e}")

        return {"removed": removed, "path": conf_file}

    # 部署蜂鸣器降权规则，从 WirePlumber 内核层杜绝 pcsp 蜂鸣器被选为默认输出
    def deploy_pcspkr_deprioritize_rule(self):
        """将 pcsp/pcspkr 蜂鸣器 sink 的会话优先级降到最低。

        防止蓝牙/USB 声卡消失后 WirePlumber 原生 fallback 自动把蜂鸣器
        选为默认输出，避免系统提示音路由到 PC Speaker 导致长响。
        规则序号 53 高于 50-no-suspend，确保覆盖其对蜂鸣器的音量重置。
        """
        content = """# PipeBridge: 蜂鸣器(pcsp/pcspkr)降权规则
# 将蜂鸣器 sink 会话优先级降到最低(0)，避免默认设备 fallback 选中蜂鸣器
# 序号 53 > 50-no-suspend，覆盖其对蜂鸣器的音量重置，防止 PC Speaker 长响
monitor.alsa.rules = [
  {
    matches = [
      { "node.name" = "~alsa_output.*pcsp.*" }
      { "node.name" = "~alsa_output.*pcspkr.*" }
      { "alsa.card_name" = "~.*pcsp.*" }
    ]
    actions = {
      update-props = {
        priority.session = 0,
        priority.driver = 0,
        node.dont-reconnect = true,
        node.autoconnect = false,
      }
    }
  }
]
"""
        result = self.deploy_rule(
            rule_name='53-pipebridge-pcspkr-deprioritize',
            content=content,
        )
        return result

    # 黑名单并卸载 pcspkr 内核模块，从根源杜绝主板蜂鸣器长响
    def blacklist_and_unload_pcspkr(self):
        """禁用 pcspkr（主板 PC Speaker 的 input/evdev SND_BELL 通路）。

        长响根因：pcspkr 模块驱动主板蜂鸣器走的是 input/evdev bell 通路，
        完全不经过 PipeWire。蓝牙/USB 声卡断开时，ALSA/内核产生的 bell 事件
        直达 pcspkr 导致主板蜂鸣器长响，PipeWire 层的静音/降权对此无效。

        解决方式：
        1. 写 modprobe 黑名单，阻止 pcspkr 开机/热插拔时自动加载；
        2. 立即卸载已加载的 pcspkr 模块；
        3. snd_pcsp（声卡形式，仅用于设备显示且已被 PipeWire 静音）保留不动。
        """
        conf_file = "/etc/modprobe.d/pipebridge-pcspkr-blacklist.conf"
        content = (
            "# PipeBridge: 禁用主板蜂鸣器(pcspkr)\n"
            "# pcspkr 走 input/evdev SND_BELL 通路，不经过 PipeWire，\n"
            "# 蓝牙/USB 声卡断开时会被 bell 事件触发导致长响，故彻底黑名单。\n"
            "blacklist pcspkr\n"
            "install pcspkr /bin/true\n"
        )
        written = False
        try:
            with open(conf_file, 'w') as f:
                f.write(content)
            written = True
            logger.info(f"已写入 pcspkr 黑名单: {conf_file}")
        except OSError as e:
            logger.warning(f"写入 pcspkr 黑名单失败: {e}")

        # 立即卸载已加载的 pcspkr 模块（-f 忽略未加载的情况）
        run_command("rmmod pcspkr 2>/dev/null || modprobe -r pcspkr 2>/dev/null", timeout=5)
        # 关闭当前终端/控制台的 bell，减少 bell 事件源
        run_command("setterm -blength 0 2>/dev/null; setterm -bfreq 0 2>/dev/null", timeout=3)

        # 卸载后校验：确认 pcspkr 确实已从内核移除，否则告警
        check = run_command("lsmod 2>/dev/null | grep -w pcspkr", timeout=3)
        unloaded = not check['stdout'].strip()
        if unloaded:
            logger.info("pcspkr 内核模块已卸载并加入黑名单")
        else:
            logger.warning(
                "pcspkr 仍处于加载状态，卸载失败（可能被其他模块占用），"
                "主板蜂鸣器仍有长响风险，请检查 lsmod"
            )

        return {"written": written, "path": conf_file, "unloaded": unloaded}

    # 部署防挂起规则，防止设备空闲后被 WirePlumber 挂起导致无法设置音量
    def deploy_no_suspend_rule(self):
        content = """# PipeBridge: 防止音频设备空闲挂起并设置默认音量
# 设备挂起后 channelVolumes 参数会丢失，导致无法设置音量
# 设置 suspend-timeout 为 0 表示永不挂起
# channelVolumes = 1.0 对应 100% 音量（覆盖 WirePlumber 默认的 40%）
monitor.alsa.rules = [
  {
    matches = [
      { "node.name" = "~alsa_output.*" }
    ]
    actions = {
      update-props = {
        session.suspend-timeout-seconds = 0,
        channelVolumes = [ 1.0 ]
      }
    }
  }
]
"""
        result = self.deploy_rule(
            rule_name='50-pipebridge-no-suspend',
            content=content,
        )
        return result

    # 部署 WirePlumber 蓝牙音频配置
    def deploy_bluez_config(self):
        conf_dir = platform_paths.WP_SYSTEM_CONF_DIR
        conf_file = os.path.join(conf_dir, "51-pipebridge-bluez.conf")

        # 蓝牙配置内容（WirePlumber 0.5+ 默认 profile 已含 hardware.bluetooth = required，无需显式启用）
        bluez_conf_content = (
            "# PipeBridge: 蓝牙音频配置 (WirePlumber 0.5+ SPA-JSON)\n"
            "wireplumber.profiles = {\n"
            "  main = {\n"
            "    monitor.bluez.seat-monitoring = disabled\n"
            "  }\n"
            "}\n"
            "\n"
            "monitor.bluez.properties = {\n"
            "    bluez5.enable-sbc-xq = true\n"
            "    bluez5.enable-msbc = true\n"
            "    bluez5.enable-hw-volume = true\n"
            "    bluez5.headset-roles = [ hsp_hs hsp_ag hfp_hf hfp_ag ]\n"
            "}\n"
        )

        # 检查现有配置是否正确
        if os.path.exists(conf_file):
            try:
                with open(conf_file, 'r') as f:
                    content = f.read()
                if 'monitor.bluez.properties' in content and 'seat-monitoring' in content and 'monitor.bluez = enabled' not in content:
                    # 配置文件内容正确，但需验证 WirePlumber 是否实际加载
                    # 配置存在但 WirePlumber 未运行/未加载时，MediaEndpoint1 不会注册
                    try:
                        import bluetooth_manager as _bt_mod
                        if _bt_mod.check_bluetooth_audio_ready():
                            logger.debug("WirePlumber 蓝牙配置已存在且已生效，跳过部署")
                            return
                        logger.warning("WirePlumber 蓝牙配置文件存在但 MediaEndpoint1 未注册，需重启 WirePlumber 使配置生效")
                    except ImportError:
                        logger.warning("无法检查蓝牙音频就绪状态，假设配置已生效")
                        return
            except OSError:
                pass

        # 创建配置目录并写入配置
        os.makedirs(conf_dir, exist_ok=True)
        try:
            with open(conf_file, 'w') as f:
                f.write(bluez_conf_content)
            logger.info(f"WirePlumber 蓝牙配置已创建: {conf_file}")

            # 重启 WirePlumber 使配置生效
            # 先确保 PipeWire socket 存在（WirePlumber 依赖 PipeWire，socket 缺失则启动必失败）
            if not _pw_socket_exists():
                logger.info("PipeWire socket 缺失，先启动 PipeWire...")
                start_pw_service('pipewire')
                for _ in range(10):
                    if _pw_socket_exists():
                        break
                    time.sleep(0.5)
            if not _pw_socket_exists():
                logger.error("PipeWire socket 仍未就绪，跳过 WirePlumber 重启")
                return {"deployed": True, "path": conf_file, "restart_skipped": True}

            # 检查是否有活跃音频流，避免断开正在播放的音频
            # 但当 MediaEndpoint1 未注册时（蓝牙音频不工作），即使有活跃链接也必须重启
            active_streams = run_command(f"{platform_paths.CMD_PW_CLI} list-objects 2>/dev/null | grep -c 'type.*Link'", timeout=3)
            stream_count = int(active_streams['stdout'].strip()) if active_streams['success'] and active_streams['stdout'].strip().isdigit() else 0
            bt_endpoint_ready = False
            try:
                import bluetooth_manager as _bt_mod
                bt_endpoint_ready = _bt_mod.check_bluetooth_audio_ready()
            except Exception:
                pass
            if stream_count > 0 and bt_endpoint_ready:
                logger.debug(f"检测到 {stream_count} 个活跃音频链接且蓝牙音频已就绪，跳过 WirePlumber 重启")
                return {"deployed": True, "path": conf_file, "restart_skipped": True}
            if stream_count > 0 and not bt_endpoint_ready:
                logger.warning(f"检测到 {stream_count} 个活跃音频链接，但蓝牙音频端点未注册，仍重启 WirePlumber 使蓝牙配置生效")
            stop_pw_service('wireplumber')
            time.sleep(1)
            start_pw_service('wireplumber')
            time.sleep(3)

            # 检查蓝牙音频是否就绪（等待最多 8 秒，WirePlumber 蓝牙模块初始化需要时间）
            bt_ready = False
            try:
                import bluetooth_manager as _bt_mod
                for _ in range(16):
                    if _bt_mod.check_bluetooth_audio_ready():
                        bt_ready = True
                        break
                    time.sleep(0.5)
                if bt_ready:
                    logger.info("WirePlumber 重启后蓝牙音频就绪")
                else:
                    logger.warning("WirePlumber 重启后蓝牙音频仍未就绪，开始诊断...")
                    _diagnose_bluetooth_audio_failure()
            except ImportError:
                logger.warning("无法检查蓝牙音频就绪状态（bluetooth_manager 不可导入）")

        except OSError as e:
            logger.warning(f"WirePlumber 蓝牙配置创建失败: {e}")
            raise ConfigError(f"WirePlumber 蓝牙配置创建失败: {e}")


# 模块级单例
_wp_config_manager = WPConfigManager()
