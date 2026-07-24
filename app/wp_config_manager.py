# WirePlumber 统一配置管理器，整合分散的 WP 规则部署逻辑，仅支持 0.5+ SPA-JSON 格式

import os
import re
import time
import logging

from utils import run_command, start_pw_service, stop_pw_service, _get_pw_env, _pw_socket_exists
import platform_paths
from exceptions import ConfigError

logger = logging.getLogger('MediaHub')


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
        # 自动检测是否需要 IEC958 规则
        if need_iec958 is None:
            need_iec958 = False
            aplay_result = run_command(f"{platform_paths.CMD_APLAY} -l 2>/dev/null", timeout=3)
            if aplay_result['success'] and aplay_result['stdout']:
                for line in aplay_result['stdout'].strip().split('\n'):
                    if not line.startswith('  ') and 'card' in line.lower():
                        card_match = re.search(r'card \d+: (.+?) \[', line)
                        if card_match:
                            card_name = card_match.group(1)
                            line_lower = line.lower()
                            # 只有 IEC958 输出（无 HDMI、无 analog）的声卡才需要此规则
                            if ('iec958' in line_lower
                                    and 'hdmi' not in line_lower
                                    and 'analog' not in line_lower):
                                need_iec958 = True
                                logger.debug(f"声卡 {card_name} 只有 IEC958 输出，需要 IEC958 规则")
                            elif 'hdmi' in line_lower or 'analog' in line_lower:
                                logger.debug(f"声卡 {card_name} 有 HDMI/模拟输出，不需要 IEC958 规则")

        # IEC958 规则内容
        if need_iec958:
            content = """# MediaHub: 启用 IEC958 数字音频设备
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
            content = """# MediaHub: IEC958 规则（当前系统不需要，已禁用）
# 当系统只有 IEC958 输出的声卡时，此规则会被自动激活
"""

        result = self.deploy_rule(
            rule_name='51-mediahub-iec958',
            content=content,
        )

        return result

    # 移除 PC Speaker 黑名单规则，让蜂鸣器设备正常注册
    def deploy_pcspkr_blacklist(self):
        conf_dir = platform_paths.WP_SYSTEM_CONF_DIR
        conf_file = os.path.join(conf_dir, "52-mediahub-pcspkr-blacklist.conf")

        removed = False
        if os.path.exists(conf_file):
            try:
                os.remove(conf_file)
                removed = True
                logger.info(f"已移除蜂鸣器黑名单规则: {conf_file}")
            except OSError as e:
                logger.warning(f"移除蜂鸣器黑名单规则失败: {e}")

        return {"removed": removed, "path": conf_file}

    # 部署 WirePlumber 蓝牙音频配置
    def deploy_bluez_config(self):
        conf_dir = platform_paths.WP_SYSTEM_CONF_DIR
        conf_file = os.path.join(conf_dir, "51-mediahub-bluez.conf")

        # 蓝牙配置内容（WirePlumber 0.5+ 默认 profile 已含 hardware.bluetooth = required，无需显式启用）
        bluez_conf_content = (
            "# MediaHub: 蓝牙音频配置 (WirePlumber 0.5+ SPA-JSON)\n"
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
