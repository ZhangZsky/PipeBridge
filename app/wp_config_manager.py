# WirePlumber 统一配置管理器
# 整合 audio_manager 和 bluetooth_manager 中分散的 WP 规则部署逻辑
# 支持 WirePlumber 0.5+ (SPA-JSON) 和 0.4 (Lua) 两种格式

import os
import re
import time
import logging

from utils import run_command, start_pw_service, stop_pw_service, _get_pw_env, _is_root
import platform_paths

logger = logging.getLogger('MediaHub')


class WPConfigManager:
    """WirePlumber 配置统一管理器，负责查找配置目录、部署规则、清理旧配置"""

    def find_config_dirs(self):
        """查找所有 WirePlumber 配置目录（系统级 + 用户级）

        返回列表，包含 wireplumber.conf.d 和 main.lua.d 两种格式的目录路径。
        优先系统级，再通过 XDG_RUNTIME_DIR 解析用户级，最后回退常见 uid。
        """
        dirs = []

        # 系统级配置目录（root 可写，对所有用户生效）
        dirs.append(platform_paths.WP_SYSTEM_CONF_DIR)
        dirs.append(platform_paths.WP_SYSTEM_LUA_DIR)

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
                    dirs.append(f"{home_dir}/{platform_paths.WP_USER_LUA_SUBDIR}")

        # 回退：尝试常见 uid（1000、1001）
        for uid_str in ['1000', '1001']:
            h = run_command(
                f"getent passwd {uid_str} 2>/dev/null | cut -d: -f6", timeout=3
            )
            if h['success'] and h['stdout']:
                home_dir = h['stdout'].strip()
                for sub in [platform_paths.WP_USER_CONF_SUBDIR, platform_paths.WP_USER_LUA_SUBDIR]:
                    d = f"{home_dir}/{sub}"
                    if d not in dirs:
                        dirs.append(d)
                break

        return dirs

    def deploy_rule(self, rule_name, content_conf, content_lua=None):
        """部署 WirePlumber 规则到所有配置目录

        Args:
            rule_name: 规则文件名（不含扩展名），如 '51-mediahub-iec958'
            content_conf: SPA-JSON 内容（WirePlumber 0.5+，写入 wireplumber.conf.d）
            content_lua: Lua 内容（WirePlumber 0.4，写入 main.lua.d），可选

        Returns:
            dict: {目录路径: 是否部署成功}
        """
        config_dirs = self.find_config_dirs()
        logger.info(f"WirePlumber 配置目录候选: {config_dirs}")
        results = {}

        for wp_dir in config_dirs:
            is_lua = wp_dir.endswith('main.lua.d')
            is_conf = wp_dir.endswith('wireplumber.conf.d')

            if is_conf:
                # WirePlumber 0.5+ SPA-JSON 格式
                rule_file = f"{wp_dir}/{rule_name}.conf"
                rule_content = content_conf
            elif is_lua and content_lua is not None:
                # WirePlumber 0.4 Lua 格式
                rule_file = f"{wp_dir}/{rule_name}.lua"
                rule_content = content_lua
            else:
                # lua 目录但未提供 lua 内容，跳过
                continue

            # 检查规则是否已存在且内容一致（跳过无变化的部署）
            check = run_command(f"test -f '{rule_file}' 2>/dev/null", timeout=2)
            if check['success']:
                existing = run_command(f"cat '{rule_file}' 2>/dev/null", timeout=3)
                if existing['success'] and existing['stdout'] == rule_content:
                    logger.debug(f"WirePlumber 规则已存在且内容一致: {rule_file}")
                    results[wp_dir] = True
                    continue
                else:
                    logger.info(f"WirePlumber 规则内容需要更新: {rule_file}")

            # 创建配置目录并写入规则文件
            run_command(f"mkdir -p '{wp_dir}' 2>/dev/null", timeout=3)
            write_result = run_command(
                f"cat > '{rule_file}' << 'MEDIAHUB_WP_EOF'\n{rule_content}MEDIAHUB_WP_EOF",
                timeout=5
            )
            if write_result['success']:
                logger.info(f"已部署 WirePlumber 规则: {rule_file}")
                results[wp_dir] = True
            else:
                logger.warning(
                    f"部署规则失败: {rule_file}, stderr='{write_result.get('stderr', '')[:200]}'"
                )
                results[wp_dir] = False

        return results

    def cleanup_legacy(self, patterns):
        """清理旧版 WirePlumber 配置文件

        Args:
            patterns: 要删除的文件路径列表或 glob 模式列表
        """
        for pattern in patterns:
            # 直接删除指定文件
            if os.path.exists(pattern):
                try:
                    os.remove(pattern)
                    logger.info(f"已删除旧配置: {pattern}")
                except OSError as e:
                    logger.debug(f"删除旧配置失败: {pattern}, {e}")

        # 扫描 main.lua.d 目录，删除文件名包含 'mediahub' 且以 .lua 结尾的文件
        lua_dirs = [
            platform_paths.WP_SYSTEM_LUA_DIR,
            f"/root/{platform_paths.WP_USER_LUA_SUBDIR}",
        ]
        # 也从 find_config_dirs 获取的用户级 lua 目录
        for wp_dir in self.find_config_dirs():
            if wp_dir.endswith('main.lua.d') and wp_dir not in lua_dirs:
                lua_dirs.append(wp_dir)

        for lua_dir in lua_dirs:
            if os.path.isdir(lua_dir):
                try:
                    for f in os.listdir(lua_dir):
                        if 'mediahub' in f.lower() and f.endswith('.lua'):
                            fp = os.path.join(lua_dir, f)
                            try:
                                os.remove(fp)
                                logger.info(f"已删除旧 Lua 配置: {fp}")
                            except OSError as e:
                                logger.debug(f"删除旧 Lua 配置失败: {fp}, {e}")
                except OSError:
                    pass

    def deploy_iec958_rule(self, need_iec958=None):
        """部署 IEC958 数字音频规则

        检查是否有声卡只有 IEC958 输出（无 HDMI、无模拟），
        为这些声卡部署 WirePlumber 规则使其被识别为 Sink。

        Args:
            need_iec958: 是否需要 IEC958 规则，None 时自动检测

        Returns:
            dict: deploy_rule 的返回结果
        """
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
            content_conf = """# MediaHub: 启用 IEC958 数字音频设备
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
            content_lua = """-- MediaHub: 启用 IEC958 数字音频设备
-- WirePlumber 默认只为有模拟输出的声卡创建 Sink
-- 此规则让只有 IEC958 (S/PDIF) 输出的声卡也能被识别
-- 注意：仅对无模拟/HDMI输出的声卡生效
rule = {
  matches = {
    {
      { "device.name", "matches", "alsa_card.*" },
    },
  },
  apply_properties = {
    ["device.profile"] = "iec958-stereo",
  },
}
table.insert(alsa_monitor.rules, rule)
"""
        else:
            # 不需要 IEC958 规则，写入空规则避免影响 HDMI 声卡
            content_conf = """# MediaHub: IEC958 规则（当前系统不需要，已禁用）
# 当系统只有 IEC958 输出的声卡时，此规则会被自动激活
"""
            content_lua = """-- MediaHub: IEC958 规则（当前系统不需要，已禁用）
-- 当系统只有 IEC958 输出的声卡时，此规则会被自动激活
"""

        result = self.deploy_rule(
            rule_name='51-mediahub-iec958',
            content_conf=content_conf,
            content_lua=content_lua,
        )

        # 清理旧版 Lua 配置
        self.cleanup_legacy([
            f"{platform_paths.WP_SYSTEM_LUA_DIR}/51-mediahub-iec958.lua",
            f"/root/{platform_paths.WP_USER_LUA_SUBDIR}/51-mediahub-iec958.lua",
        ])

        return result

    def deploy_pcspkr_blacklist(self):
        """部署 PC Speaker 黑名单规则，禁用蜂鸣器设备

        Returns:
            dict: deploy_rule 的返回结果
        """
        content_conf = """monitor.alsa.rules = [
  {
    matches = [
      { "device.name" = "~alsa_card.pcsp" }
      { "node.name" = "~alsa_output.pcsp.*" }
    ]
    actions = {
      update-props = {
        device.disabled = true
        node.disabled = true
      }
    }
  }
]
"""
        content_lua = """rule = {
  matches = {
    { "device.name", "matches", "alsa_card.pcsp" },
    { "node.name", "matches", "alsa_output.pcsp.*" }
  },
  apply_properties = {
    ["device.disabled"] = true,
    ["node.disabled"] = true
  }
}
table.insert(alsa_monitor.rules, rule)
"""
        result = self.deploy_rule(
            rule_name='52-mediahub-pcspkr-blacklist',
            content_conf=content_conf,
            content_lua=content_lua,
        )

        # 清理旧版 Lua 配置
        self.cleanup_legacy([
            f"{platform_paths.WP_SYSTEM_LUA_DIR}/52-mediahub-pcspkr-blacklist.lua",
            f"/root/{platform_paths.WP_USER_LUA_SUBDIR}/52-mediahub-pcspkr-blacklist.lua",
        ])

        return result

    def deploy_bluez_config(self):
        """部署 WirePlumber 蓝牙音频配置

        配置内容：SBC-XQ、mSBC、硬件音量、耳机角色
        同时禁用 seat-monitoring（root 无 logind 会话）
        部署后重启 WirePlumber 使配置生效

        Returns:
            bool: 配置是否成功部署
        """
        conf_dir = platform_paths.WP_SYSTEM_CONF_DIR
        conf_file = os.path.join(conf_dir, "51-mediahub-bluez.conf")

        # 清理所有旧版 Lua 配置文件（WP 0.5 不支持 Lua）
        self.cleanup_legacy([
            f"{platform_paths.WP_SYSTEM_LUA_DIR}/51-mediahub-bluez.lua",
            f"/root/{platform_paths.WP_USER_LUA_SUBDIR}/51-mediahub-iec958.lua",
            f"/root/{platform_paths.WP_USER_LUA_SUBDIR}/52-mediahub-pcspkr-blacklist.lua",
        ])

        # 蓝牙配置内容
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
                if 'monitor.bluez.properties' in content and 'seat-monitoring' in content:
                    logger.debug("WirePlumber 蓝牙配置已存在且正确，跳过部署")
                    return True
            except OSError:
                pass

        # 创建配置目录并写入配置
        os.makedirs(conf_dir, exist_ok=True)
        try:
            with open(conf_file, 'w') as f:
                f.write(bluez_conf_content)
            logger.info(f"WirePlumber 蓝牙配置已创建: {conf_file}")

            # 重启 WirePlumber 使配置生效
            stop_pw_service('wireplumber')
            time.sleep(1)
            start_pw_service('wireplumber')
            time.sleep(3)

            # 检查蓝牙音频是否就绪
            try:
                import bluetooth_manager as _bt_mod
                if _bt_mod.check_bluetooth_audio_ready():
                    logger.info("WirePlumber 重启后蓝牙音频就绪")
                else:
                    logger.warning("WirePlumber 重启后蓝牙音频仍未就绪")
            except ImportError:
                logger.warning("无法检查蓝牙音频就绪状态（bluetooth_manager 不可导入）")

            return True
        except OSError as e:
            logger.warning(f"WirePlumber 蓝牙配置创建失败: {e}")
            return False


# 模块级单例
_wp_config_manager = WPConfigManager()
