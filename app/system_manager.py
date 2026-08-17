# 系统依赖与 WirePlumber 配置管理 负责声明所需系统包/服务/命令清单(DEPENDENCIES) 检测安装运行状态并汇总(get_all_status/get_system_overview) 一键修复缺失依赖(install_missing_packages/start_missing_services/setup_pipewire/fix_all) 经 WPConfigManager 部署清理重启 WirePlumber 规则(防挂起/蜂鸣器屏蔽/IEC958/bluez) 规则文件写入 conf.d 仅写不触发重载 变更后须显式调用 restart_wireplumber 才生效

import os
import time
import logging
from concurrent.futures import ThreadPoolExecutor

from utils import run_command, start_pw_service, stop_pw_service, _get_pw_env, _pw_socket_exists
import platform_paths
from exceptions import CommandError, PipeBridgeError, ConfigError

logger = logging.getLogger('PipeBridge')

# 系统概览并行采集线程池：音频/视频/依赖/重连状态/蓝牙连接数同时拉取，避免串行阻塞 API
_overview_executor = ThreadPoolExecutor(max_workers=5)

# PipeBridge 运行所需依赖清单 critical=True 表示缺失会导致核心功能不可用 type 用于前端分组展示
DEPENDENCIES = {
    'packages': [
        {'name': 'pipewire', 'desc': 'PipeWire 音频服务', 'critical': True, 'type': 'audio-core'},
        {'name': 'pipewire-pulse', 'desc': 'PipeWire PulseAudio 兼容层', 'critical': True, 'type': 'audio-core'},
        {'name': 'pipewire-alsa', 'desc': 'PipeWire ALSA 桥接（speaker-test 依赖）', 'critical': False, 'type': 'audio-core'},
        {'name': 'wireplumber', 'desc': 'WirePlumber 会话管理', 'critical': True, 'type': 'audio-core'},
        {'name': 'libspa-0.2-bluetooth', 'desc': 'PipeWire 蓝牙支持', 'critical': True, 'type': 'bluetooth'},
        {'name': 'alsa-utils', 'desc': 'speaker-test 声道测试工具（非 ALSA 操作）', 'critical': False, 'type': 'audio-core'},
        {'name': 'bluez', 'desc': '蓝牙协议栈', 'critical': True, 'type': 'bluetooth'},
        {'name': 'bluez-tools', 'desc': '蓝牙 CLI 工具', 'critical': False, 'type': 'bluetooth'},
        {'name': 'bluez-firmware', 'desc': '蓝牙固件', 'critical': False, 'type': 'bluetooth'},
        {'name': 'python3-dbus', 'desc': 'Python D-Bus 支持', 'critical': True, 'type': 'python', 'subtype': 'bind'},
        {'name': 'python3-gi', 'desc': 'PyGObject (GLib bindings)', 'critical': True, 'type': 'python', 'subtype': 'bind'},
        {'name': 'python3-fastapi', 'desc': 'FastAPI Web框架', 'critical': True, 'type': 'python', 'subtype': 'web'},
        {'name': 'python3-uvicorn', 'desc': 'Uvicorn ASGI服务器', 'critical': True, 'type': 'python', 'subtype': 'web'},
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

def _check_service_running(service_name, user=False):
    # 检测服务是否运行 service_name 服务/进程名 user=True 用户级进程(pgrep 检测) False 系统级 systemd 服务(systemctl is-active) 返回是否运行中/active
    if user:
        pg_result = run_command(f"pgrep -x {service_name} 2>/dev/null")
        return bool(pg_result['stdout'].strip())
    result = run_command(f"systemctl is-active {service_name} 2>/dev/null")
    return result['stdout'].strip() == 'active'

def check_package_installed(pkg_name):
    # 检测 deb 包是否已安装(基于 dpkg -s 状态行)
    result = run_command(f"dpkg -s {pkg_name} 2>/dev/null | grep -c '^Status: install ok installed'")
    return result['stdout'].strip() == '1' if result['stdout'] else False

def check_service_active(service_name):
    # 检测系统级 systemd 服务是否处于 active 状态
    result = run_command(f"systemctl is-active {service_name} 2>/dev/null")
    return result['stdout'].strip() == 'active'

def check_command_exists(cmd):
    # 检测可执行命令是否存在于 PATH 中(通过 which)
    result = run_command(f"which {cmd} 2>/dev/null")
    return bool(result['stdout'].strip())

def check_pipewire_running():
    # 检测 pipewire 用户级进程是否在运行
    return _check_service_running('pipewire', user=True)

def check_wireplumber_running():
    # 检测 wireplumber 用户级进程是否在运行
    return _check_service_running('wireplumber', user=True)

def check_pipewire_pulse_running():
    # 检测 pipewire-pulse 用户级进程是否在运行
    return _check_service_running('pipewire-pulse', user=True)

def setup_pipewire():
    # 启动 PipeWire/pipewire-pulse/WirePlumber 用户级服务 顺序 pipewire(等 socket 就绪)->pipewire-pulse->wireplumber 已运行则直接返回 返回 dict(message) pipewire/wireplumber 未安装或启动后进程 socket 未就绪抛 CommandError
    if check_pipewire_running() and check_wireplumber_running():
        return {'message': '已运行'}

    logger.debug("PipeWire/WirePlumber 未运行，开始配置...")

    if not check_command_exists("pipewire"):
        logger.error("pipewire 命令不存在，请运行 install_init 安装系统依赖")
        raise CommandError('PipeWire 未安装，请运行 install_init 安装系统依赖')
    if not check_command_exists("wireplumber"):
        logger.error("wireplumber 命令不存在，请运行 install_init 安装系统依赖")
        raise CommandError('WirePlumber 未安装，请运行 install_init 安装系统依赖')

    start_pw_service('pipewire')
    # 等待 pipewire-0 socket 出现，最长 5s；socket 缺失会让后续 wireplumber 启动失败
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

def check_spa_bluetooth_plugin():
    # 检测 libspa-0.2-bluetooth 是否提供了 .so 插件文件(WirePlumber 加载蓝牙模块所需)
    result = run_command("dpkg -L libspa-0.2-bluetooth 2>/dev/null | grep -E '\\.so$' | head -1", timeout=5)
    if result['success'] and result['stdout'].strip():
        so_file = result['stdout'].strip()
        return os.path.exists(so_file)
    return False

def check_bluetooth_audio_ready():
    # 委托 bluetooth_manager 检测蓝牙音频端点是否已就绪 任何异常视为未就绪
    try:
        import bluetooth_manager
        return bluetooth_manager.check_bluetooth_audio_ready()
    except Exception:
        return False

def _safe_check_bluetooth_audio(timeout=5):
    # 带超时保护的蓝牙音频就绪检测 蓝牙 D-Bus 调用可能阻塞 单独线程+超时避免拖垮系统概览接口 超时返回 False 由调用方决定后续诊断
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(check_bluetooth_audio_ready)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            logger.warning("蓝牙音频就绪检查超时，跳过")
            return False

def get_all_status():
    # 汇总所有依赖(包/服务/命令/PipeWire 进程/蓝牙插件/蓝牙音频就绪)状态 返回字典含 packages/services/commands 列表 pipewire/wireplumber/pipewire_pulse 运行情况 spa_bluetooth_plugin bluetooth_audio_ready all_ok 总体标记及 critical_missing 缺失关键项清单
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

    wp_running = check_wireplumber_running()
    status['wireplumber'] = {
        'running': wp_running,
        'desc': 'WirePlumber 会话管理'
    }
    if not wp_running:
        # wireplumber 包缺失已由 packages 循环计入 critical_missing 此处仅在包已安装但进程未运行时补充标记 避免重复计入同名项
        wp_installed = check_package_installed('wireplumber')
        if wp_installed:
            status['critical_missing'].append('wireplumber(进程未运行)')
        status['all_ok'] = False

    pwp_running = check_pipewire_pulse_running()
    status['pipewire_pulse'] = {
        'running': pwp_running,
        'desc': 'PipeWire PulseAudio 兼容层'
    }
    if not pwp_running:
        # 同 wireplumber：包缺失已计入，此处仅标记“已安装但进程未运行”。
        pwp_installed = check_package_installed('pipewire-pulse')
        if pwp_installed:
            status['critical_missing'].append('pipewire-pulse(进程未运行)')
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

    spa_ok = check_spa_bluetooth_plugin()
    status['spa_bluetooth_plugin'] = spa_ok
    if not spa_ok:
        status['critical_missing'].append('spa-bluetooth-plugin(.so)')
        status['all_ok'] = False

    bt_audio_ready = _safe_check_bluetooth_audio(timeout=5)
    status['bluetooth_audio_ready'] = bt_audio_ready

    return status

def install_missing_packages():
    # apt-get 安装所有缺失的关键依赖包 返回 dict(message) 失败抛 CommandError
    missing = [pkg['name'] for pkg in DEPENDENCIES['packages']
               if pkg['critical'] and not check_package_installed(pkg['name'])]

    if not missing:
        return {'message': '已安装'}

    result = run_command(f"apt-get install -y -qq {' '.join(missing)}", timeout=120)

    if result['success']:
        return {'message': f'已安装 {len(missing)} 个包'}

    raise CommandError('安装失败')

def start_missing_services():
    # systemctl start 启动所有未运行的系统级依赖服务 返回 dict(message) 任一失败抛 CommandError 并列出失败服务名
    errors = []
    for svc in DEPENDENCIES['services']:
        if not check_service_active(svc['name']):
            result = run_command(f"systemctl start {svc['name']} 2>/dev/null")
            if not result['success']:
                errors.append(svc['name'])

    if errors:
        raise CommandError(f'启动失败: {", ".join(errors)}')

    return {'message': '已运行'}

def get_system_overview():
    # 返回系统概览(含服务状态/设备数量/依赖/重连状态等)供前端首页展示
    return _build_overview()

def _build_overview():
    # 并行采集音频/视频/依赖/蓝牙重连/蓝牙连接数并组装系统概览字典 各子任务经 _overview_executor 并行 单任务 5s 整体 8s 超时 任一失败仅记 warning 不影响其它字段
    import audio_manager
    import video_manager
    import bluetooth_manager
    from concurrent.futures import as_completed

    pipewire_running = check_pipewire_running()
    wireplumber_running = check_wireplumber_running()
    bluetooth_active = check_service_active('bluetooth')
    pipewire_pulse_running = check_pipewire_pulse_running()
    dbus_running = check_service_active('dbus')

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

    def _fetch_video():
        result = video_manager.get_video_devices()
        devices = result.get('devices', [])
        default = result.get('default', '')
        return devices, default

    def _fetch_deps():
        return get_all_status()

    def _fetch_reconnect():
        try:
            return bluetooth_manager.get_reconnect_status()
        except Exception:
            return {'monitoring': False, 'reconnecting_devices': [], 'manual_disconnects': []}

    def _fetch_bt_connected():
        try:
            bt_paired = bluetooth_manager.get_paired_devices()
            if isinstance(bt_paired, list):
                return sum(1 for d in bt_paired if d.get('connected'))
        except Exception as e:
            logger.debug(f"获取蓝牙已连接设备数失败: {e}")
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
        logger.warning(f"系统概览并行获取异常: {e}")

    all_ok = pipewire_running and wireplumber_running and pipewire_pulse_running and bluetooth_active and deps_status.get('all_ok', False)

    bt_audio_ready = deps_status.get('bluetooth_audio_ready')
    if bt_audio_ready is None:
        bt_audio_ready = _safe_check_bluetooth_audio()

    bt_hardware_detected = False
    try:
        usb_devices = bluetooth_manager.check_bluetooth_hardware()
        controllers = bluetooth_manager.get_all_controllers()
        bt_hardware_detected = bool(usb_devices) or bool(controllers)
    except Exception as e:
        logger.debug(f"检查蓝牙硬件失败: {e}")

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

def _diagnose_bluetooth_audio_failure():
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

    spa_result = run_command("dpkg -L libspa-0.2-bluetooth 2>/dev/null | grep -E '\\.so$' | head -5")
    if spa_result['success'] and spa_result['stdout'].strip():
        logger.info(f"SPA 蓝牙插件文件: {spa_result['stdout'].strip()[:200]}")
    else:
        logger.error("libspa-0.2-bluetooth 包未安装或 .so 文件缺失，WirePlumber 无法加载蓝牙模块")

    hci_result = run_command("hciconfig 2>/dev/null", timeout=3)
    if hci_result['success'] and hci_result['stdout'].strip():
        logger.info(f"蓝牙适配器状态: {hci_result['stdout'].strip()[:300]}")
    else:
        logger.error("未检测到蓝牙适配器（hciconfig 无输出），BlueZ 可能未暴露 Media1 接口")

    wp_status = run_command("wpctl status 2>/dev/null", timeout=5)
    if wp_status['success'] and wp_status['stdout']:
        status_lower = wp_status['stdout'].lower()
        if 'bluez' in status_lower or 'bluetooth' in status_lower:
            logger.info("WirePlumber 状态中包含蓝牙组件引用")
        else:
            logger.warning("WirePlumber 状态中未发现蓝牙组件，蓝牙模块可能未加载（检查配置文件或 libspa-0.2-bluetooth）")
    else:
        logger.warning("wpctl status 执行失败，无法确认 WirePlumber 组件加载状态")

    wp_ver = run_command("wireplumber --version 2>/dev/null | head -1", timeout=3)
    if wp_ver['success'] and wp_ver['stdout'].strip():
        logger.info(f"WirePlumber 版本: {wp_ver['stdout'].strip()}")
    else:
        logger.warning("无法获取 WirePlumber 版本")

class WPConfigManager:
    def find_config_dirs(self):
        dirs = []

        dirs.append(platform_paths.WP_SYSTEM_CONF_DIR)

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

    def deploy_rule(self, rule_name, content):
        config_dirs = self.find_config_dirs()
        logger.info(f"WirePlumber 配置目录候选: {config_dirs}")
        results = {}

        for wp_dir in config_dirs:
            rule_file = f"{wp_dir}/{rule_name}.conf"

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

    def cleanup_legacy(self, patterns):
        for pattern in patterns:
            if os.path.exists(pattern):
                try:
                    os.remove(pattern)
                    logger.info(f"已删除旧配置: {pattern}")
                except OSError as e:
                    logger.debug(f"删除旧配置失败: {pattern}, {e}")

    def deploy_iec958_rule(self, need_iec958=None):
        if need_iec958 is None:
            need_iec958 = False
            try:
                from utils import pw_dump, iter_pw_devices, get_device_enum_profiles
                pw_data = pw_dump()
                for obj in iter_pw_devices(pw_data):
                    profile_names = [ep.get('name', '') for ep in get_device_enum_profiles(obj)]
                    has_iec958 = any('iec958' in pn.lower() for pn in profile_names)
                    has_analog = any('analog' in pn.lower() for pn in profile_names)
                    has_hdmi = any('hdmi' in pn.lower() for pn in profile_names)
                    if has_iec958 and not has_analog and not has_hdmi:
                        dev_name = obj.get('info', {}).get('props', {}).get('device.name', '')
                        need_iec958 = True
                        logger.debug(f"声卡 {dev_name} 只有 IEC958 输出，需要 IEC958 规则")
                        break
            except Exception as e:
                logger.debug(f"IEC958 检测失败: {e}")

        if need_iec958:
            content = """# PipeBridge: 启用 IEC958 数字音频设备
# WirePlumber 默认只为有模拟输出的声卡创建 Sink 此规则让只有 IEC958 (S/PDIF) 输出的声卡也能被识别 注意：仅对无模拟/HDMI输出的声卡生效
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
            content = """# PipeBridge: IEC958 规则（当前系统不需要，已禁用）
# 当系统只有 IEC958 输出的声卡时，此规则会被自动激活
"""
        result = self.deploy_rule(
            rule_name='51-pipebridge-iec958',
            content=content,
        )

        return result

    def deploy_no_suspend_rule(self):
        content = """# PipeBridge: 防止音频设备空闲挂起
# 设备挂起后 channelVolumes 参数会丢失，导致无法设置音量 设置 suspend-timeout 为 0 表示永不挂起
# 不在此处设置 channelVolumes：WirePlumber 会在节点状态变化时重新应用 update-props，导致用户调整的音量被覆盖回默认值
# 音量仅在用户主动拖动/点击音量条时通过 set_volume 修改，不做任何自动初始化
monitor.alsa.rules = [
  {
    matches = [
      { "node.name" = "~alsa_output.*" }
    ]
    actions = {
      update-props = {
        session.suspend-timeout-seconds = 0
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

    # 重启 WirePlumber 使新部署的 monitor.alsa.rules 等规则生效
    def restart_wireplumber(self):
        # 重启 WirePlumber 会话管理器令新写入规则文件真正加载生效 deploy_rule 只写文件不重载 WirePlumber 仅启动时读 conf.d 规则 故部署降权/防挂起规则后须重启才生效 会短暂中断音频路由(启动阶段可接受)
        logger.info("重启 WirePlumber 以加载新规则...")
        stop_pw_service('wireplumber')
        time.sleep(0.5)
        started = start_pw_service('wireplumber')
        if started:
            logger.info("WirePlumber 已重启，新规则生效")
        else:
            logger.warning("WirePlumber 重启后未检测到进程，规则可能未生效")
        return started

    def cleanup_pcspkr_block_rules(self):
        # 蜂鸣器改为 driver_override 物理拦截方案(见 block_pcspkr_via_override),
        # 不再依赖 WirePlumber 屏蔽规则或 modprobe.d 黑名单。
        # 此处清理历史版本残留的两类文件,避免失效/冲突规则影响系统:
        #   1) WirePlumber 屏蔽规则(旧的音频层屏蔽方案)
        #   2) /etc/modprobe.d 黑名单(旧的模块卸载方案,install /bin/true 会阻止模块加载)
        removed = False
        conf_dir = platform_paths.WP_SYSTEM_CONF_DIR
        for old_name in (
            "52-pipebridge-pcspkr-block.conf",
            "53-pipebridge-pcspkr-block.conf",
            "53-pipebridge-pcspkr-deprioritize.conf",
        ):
            old_path = os.path.join(conf_dir, old_name)
            if os.path.exists(old_path):
                try:
                    os.remove(old_path)
                    logger.info(f"已移除历史蜂鸣器屏蔽规则: {old_path}")
                    removed = True
                except OSError as e:
                    logger.warning(f"移除历史蜂鸣器屏蔽规则失败: {e}")
        # 清理旧模块卸载方案写入的 modprobe.d 黑名单
        old_blacklist = '/etc/modprobe.d/pipebridge-beeper-blacklist.conf'
        if os.path.exists(old_blacklist):
            try:
                os.remove(old_blacklist)
                logger.info(f"已移除历史蜂鸣器模块黑名单: {old_blacklist}")
                removed = True
            except OSError as e:
                logger.warning(f"移除历史蜂鸣器模块黑名单失败: {e}")
        return removed

    def block_pcspkr_via_override(self):
        # 彻底禁用蜂鸣器——唯一方案: 平台设备 driver_override 物理拦截。
        #
        # 原理: PC-Speaker 是一个固定存在的平台设备(/sys/devices/platform/pcspkr)。
        #   向其 driver_override 写入 "none" 后,内核 driver core 会拒绝任何驱动
        #   (snd_pcsp / pcspkr)绑定到该设备。驱动即使被加载进内存,probe 也会被拒,
        #   因此永远不会注册出 ALSA 声卡(card1 [pcsp]),PipeWire 也就没有对应 sink,
        #   即使强制路由音频到蜂鸣器也无处可去,自然无声。
        #
        # 关键实测(Debian 12 / kernel 6.18 trim):
        #   - override 为空时 insmod snd_pcsp → 出现 card1 [pcsp]
        #   - 写 override=none 后再 insmod snd_pcsp → 模块进内存但 probe 被拒,
        #     /proc/asound/cards 只剩 card0,card1 不出现 —— 验证物理拦截生效。
        #
        # 相比卸载模块方案的优势: 不与模块热插拔/自动重载竞态; "即使系统注册过蜂鸣器
        #   也无声"—— 因为根本无法完成驱动绑定。
        #
        # 局限: sysfs 值不持久,重启失效。故本方法在每次服务启动时调用重设,
        #   并解绑已绑定的驱动 + 卸载已注册的声卡, 保证当前会话立即无声。
        # 所有失败仅记录 warning 不阻断启动流程。
        override_path = '/sys/devices/platform/pcspkr/driver_override'
        if not os.path.exists(override_path):
            logger.info("未发现 platform-pcspkr 设备,无需拦截蜂鸣器")
            return True

        # 1) 若 snd_pcsp 已绑定该设备并注册了声卡,先解绑再卸载,使 override 立即对现存声卡生效
        bound = run_command(
            "ls -l /sys/devices/platform/pcspkr/driver 2>/dev/null",
        )
        if bound['success'] and bound['stdout'].strip():
            # 通过 driver unbind 解除现有绑定
            run_command(
                "echo pcspkr > /sys/bus/platform/drivers/*/unbind 2>/dev/null; "
                "for d in /sys/bus/platform/drivers/*/pcspkr; do "
                "echo pcspkr > \"$(dirname $d)/unbind\" 2>/dev/null; done",
            )
            # 卸载已注册的声卡模块(force 应对 refcnt 恒为 1)
            for mod in ('snd_pcsp', 'pcspkr'):
                run_command(f"rmmod -f {mod} 2>/dev/null", timeout=5)
            logger.info("已解绑并卸载现存蜂鸣器驱动")

        # 2) 写 driver_override=none, 物理阻止任何驱动再次绑定
        r = run_command(f"echo none > {override_path} 2>&1", timeout=5)
        if r['success']:
            logger.info("已设置 pcspkr driver_override=none,蜂鸣器驱动将无法绑定")
            return True
        logger.warning(f"设置 pcspkr driver_override 失败: {r.get('stdout', '').strip()}")
        return False

    def deploy_bluez_config(self):
        conf_dir = platform_paths.WP_SYSTEM_CONF_DIR
        conf_file = os.path.join(conf_dir, "51-pipebridge-bluez.conf")

        bluez_conf_content = (
            "# PipeBridge: 蓝牙音频配置 (WirePlumber 0.5+ SPA-JSON)\n"
            "wireplumber.profiles = {\n"
            "  main = {\n"
            "    monitor.bluez.seat-monitoring = disabled\n"
            "  }\n"
            "}\n"
            "\n"
            "# 禁止蓝牙连接后自动成为默认输出：默认设备完全由用户手动掌控。\n"
            "# node.restore-default-targets=false 关闭 WirePlumber 自动选择/恢复默认目标;\n"
            "# bluetooth.autoswitch-to-headset-profile=false关闭 A2DP<->HFP 自动切换(与手动策略一致)。\n"
            "wireplumber.settings = {\n"
            "    node.restore-default-targets = false\n"
            "    bluetooth.autoswitch-to-headset-profile = false\n"
            "}\n"
            "\n"
            "monitor.bluez.properties = {\n"
            "    bluez5.enable-sbc-xq = true\n"
            "    bluez5.enable-msbc = true\n"
            "    bluez5.enable-hw-volume = true\n"
            "    bluez5.headset-roles = [ hsp_hs hsp_ag hfp_hf hfp_ag ]\n"
            "}\n"
            "\n"
            "# 降低蓝牙 A2DP sink 的会话优先级(默认 1010)，使其连接后不高于内置声卡而被自动选默认。\n"
            "monitor.bluez.rules = [\n"
            "    {\n"
            "        matches = [ { node.name = \"~bluez_output.*\" } ]\n"
            "        actions = {\n"
            "            update-props = {\n"
            "                priority.session = 500\n"
            "            }\n"
            "        }\n"
            "    }\n"
            "]\n"
        )

        if os.path.exists(conf_file):
            try:
                with open(conf_file, 'r') as f:
                    content = f.read()
                # 除基础项外，还须包含"禁止自动设默认"的新设置(node.restore-default-targets)，
                # 否则视为旧配置需重新部署，避免已装机器升级后漏掉关闭自动默认的配置。
                if ('monitor.bluez.properties' in content and 'seat-monitoring' in content
                        and 'monitor.bluez = enabled' not in content
                        and 'node.restore-default-targets = false' in content):
                    try:
                        import bluetooth_manager as _bt_mod
                        if _bt_mod.check_bluetooth_audio_ready():
                            logger.debug("WirePlumber 蓝牙配置已存在且已生效，跳过部署")
                            return
                        logger.warning("WirePlumber 蓝牙配置文件存在但 MediaEndpoint1 未注册，需重启 WirePlumber 使配置生效")
                    except ImportError:
                        logger.warning("无法检查蓝牙音频就绪状态，假设配置已生效")
                        return
            except OSError as e:
                logger.debug(f"读取WirePlumber配置文件失败: {e}")

        os.makedirs(conf_dir, exist_ok=True)
        try:
            with open(conf_file, 'w') as f:
                f.write(bluez_conf_content)
            logger.info(f"WirePlumber 蓝牙配置已创建: {conf_file}")

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

            active_streams = run_command(f"{platform_paths.CMD_PW_CLI} list-objects 2>/dev/null | grep -c 'type.*Link'", timeout=3)
            stream_count = int(active_streams['stdout'].strip()) if active_streams['success'] and active_streams['stdout'].strip().isdigit() else 0
            bt_endpoint_ready = False
            try:
                import bluetooth_manager as _bt_mod
                bt_endpoint_ready = _bt_mod.check_bluetooth_audio_ready()
            except Exception as e:
                logger.debug(f"检查蓝牙音频就绪状态失败: {e}")
            if stream_count > 0 and bt_endpoint_ready:
                logger.debug(f"检测到 {stream_count} 个活跃音频链接且蓝牙音频已就绪，跳过 WirePlumber 重启")
                return {"deployed": True, "path": conf_file, "restart_skipped": True}
            if stream_count > 0 and not bt_endpoint_ready:
                logger.warning(f"检测到 {stream_count} 个活跃音频链接，但蓝牙音频端点未注册，仍重启 WirePlumber 使蓝牙配置生效")
            # 首启动场景 WirePlumber 尚未运行时直接 start（首次加载即读到新配置）无需多余的 stop→sleep→start 仅当它已在运行时才需重启使新配置生效
            wp_running = run_command("pgrep -x wireplumber 2>/dev/null")
            wp_is_running = bool(wp_running['success'] and wp_running['stdout'].strip())
            if wp_is_running:
                stop_pw_service('wireplumber')
                time.sleep(1)
            start_pw_service('wireplumber')
            time.sleep(3)

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

_wp_config_manager = WPConfigManager()
