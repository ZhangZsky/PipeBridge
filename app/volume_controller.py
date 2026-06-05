"""
统一音量控制器 —— 封装 wpctl → pw-dump → pactl 回退逻辑

将 audio_manager.py 中分散的音量获取/设置/静音/平衡操作
统一到 VolumeController 类中，支持方法缓存以跳过已知失败路径。
"""

import re
import math
import shlex
import logging
import threading

from utils import (
    run_command, pw_dump, find_pw_node,
    get_node_id_by_name, get_node_name_by_id,
    get_default_sink_name, get_default_source_name,
    extract_pw_vol_params,
)
import platform_paths

logger = logging.getLogger('MediaHub')

# 安全设备名正则（与 audio_manager 保持一致）
_SAFE_DEVICE_PATTERN = re.compile(r'^[a-zA-Z0-9_.@:\[\]\/-]+$')


class VolumeController:
    """统一音量控制器，封装 wpctl / pw-dump / pactl 回退逻辑"""

    def __init__(self):
        # 方法缓存：{device_name: 'wpctl' | 'pw_dump' | 'pactl'}
        # 记录上次成功的方法，下次优先使用
        self._method_cache = {}
        self._cache_lock = threading.Lock()

    # ── 公共方法 ──────────────────────────────────────────────────

    def get_volume(self, device_name):
        """获取设备音量

        返回 {'volume': int, 'muted': bool, 'device': str} 或 None
        尝试顺序：缓存方法 → wpctl → pw-dump Props → pactl
        """
        if not device_name or not _SAFE_DEVICE_PATTERN.match(device_name):
            return None

        # 优先使用缓存的方法
        cached = self._get_cached_method(device_name)
        if cached == 'wpctl':
            result = self._try_get_volume_wpctl(device_name)
            if result is not None:
                return result
        elif cached == 'pw_dump':
            result = self._try_get_volume_pw_dump(device_name)
            if result is not None:
                return result
        elif cached == 'pactl':
            result = self._try_get_volume_pactl(device_name)
            if result is not None:
                return result

        # 按优先级逐一尝试
        # 1. wpctl
        result = self._try_get_volume_wpctl(device_name)
        if result is not None:
            self._set_cached_method(device_name, 'wpctl')
            return result

        # 2. pw-dump Props（channelVolumes + cubic_to_linear）
        result = self._try_get_volume_pw_dump(device_name)
        if result is not None:
            self._set_cached_method(device_name, 'pw_dump')
            return result

        # 3. pactl
        result = self._try_get_volume_pactl(device_name)
        if result is not None:
            self._set_cached_method(device_name, 'pactl')
            return result

        logger.warning(f"获取音量失败: {device_name}，所有方法均不可用")
        return None

    def set_volume(self, device_name, volume):
        """设置设备音量

        volume: 0-100 整数
        返回 {'success': bool, 'data': str, 'verified_volume': int}
        """
        if not device_name or not _SAFE_DEVICE_PATTERN.match(device_name):
            return {'success': False, 'data': '无效的设备名', 'verified_volume': -1}

        volume = max(0, min(100, int(volume)))
        vol_flat = volume / 100.0
        is_source = self._is_source_device(device_name)

        # 优先使用缓存的方法
        cached = self._get_cached_method(device_name)

        # 1. wpctl
        if cached != 'pactl':  # 缓存不是 pactl 时优先尝试 wpctl
            node_id = self._resolve_node_id(device_name)
            if node_id is not None:
                result = run_command(f"{platform_paths.CMD_WPCTL} set-volume {node_id} {vol_flat:.4f}", timeout=5)
                if result['success']:
                    self._set_cached_method(device_name, 'wpctl')
                    return self._verify_volume(device_name, volume)
                logger.warning(f"wpctl set-volume 失败: {result.get('stderr', '')}")

        # 2. pactl 回退
        pulse_vol = int(vol_flat * 65536)
        if is_source:
            pa_result = run_command(
                f"{platform_paths.CMD_PACTL} set-source-volume {shlex.quote(device_name)} {pulse_vol}", timeout=5)
        else:
            pa_result = run_command(
                f"{platform_paths.CMD_PACTL} set-sink-volume {shlex.quote(device_name)} {pulse_vol}", timeout=5)

        if pa_result['success']:
            self._set_cached_method(device_name, 'pactl')
            return self._verify_volume(device_name, volume)

        # 如果缓存是 pactl 但失败了，再尝试 wpctl
        if cached == 'pactl':
            node_id = self._resolve_node_id(device_name)
            if node_id is not None:
                result = run_command(f"{platform_paths.CMD_WPCTL} set-volume {node_id} {vol_flat:.4f}", timeout=5)
                if result['success']:
                    self._set_cached_method(device_name, 'wpctl')
                    return self._verify_volume(device_name, volume)

        logger.error(f"设置音量失败: device={device_name}, volume={volume}")
        return {'success': False, 'data': '设置音量失败', 'verified_volume': -1}

    def set_mute(self, device_name, mute):
        """设置设备静音

        mute: True/False
        返回 {'success': bool, 'data': str}
        """
        if not device_name or not _SAFE_DEVICE_PATTERN.match(device_name):
            return {'success': False, 'data': '无效的设备名'}

        is_source = self._is_source_device(device_name)
        mute_flag = '1' if mute else '0'
        mute_label = '静音' if mute else '取消静音'

        # 1. wpctl
        node_id = self._resolve_node_id(device_name)
        if node_id is not None:
            result = run_command(f"{platform_paths.CMD_WPCTL} set-mute {node_id} {mute_flag}", timeout=5)
            if result['success']:
                self._set_cached_method(device_name, 'wpctl')
                logger.info(f"{mute_label}(wpctl): {device_name} node={node_id}")
                return {'success': True, 'data': f'已{mute_label}'}
            logger.warning(f"wpctl set-mute 失败: {result.get('stderr', '')}")

        # 2. pactl 回退
        if is_source:
            pa_result = run_command(
                f"{platform_paths.CMD_PACTL} set-source-mute {shlex.quote(device_name)} {mute_flag}", timeout=5)
        else:
            pa_result = run_command(
                f"{platform_paths.CMD_PACTL} set-sink-mute {shlex.quote(device_name)} {mute_flag}", timeout=5)

        if pa_result['success']:
            self._set_cached_method(device_name, 'pactl')
            logger.info(f"{mute_label}(pactl): {device_name}")
            return {'success': True, 'data': f'已{mute_label}'}

        return {'success': False, 'data': '设置静音失败'}

    def get_balance(self, device_name):
        """获取设备左右声道平衡

        返回 {'balance': float, 'left': float, 'right': float, 'stereo': bool}
        """
        if not device_name or not _SAFE_DEVICE_PATTERN.match(device_name):
            return {'balance': 0.0, 'left': 0.0, 'right': 0.0, 'stereo': False}

        # 从 pw-dump Props 获取声道音量
        props_params, _ = self._get_node_props_params(device_name)
        channel_volumes = props_params.get('channelVolumes', [])

        if not channel_volumes or len(channel_volumes) < 2:
            return {'balance': 0.0, 'left': 0.0, 'right': 0.0, 'stereo': False}

        left, right = float(channel_volumes[0]), float(channel_volumes[1])
        total = left + right
        balance = max(-1.0, min(1.0, (right - left) / total)) if total > 0 else 0.0

        return {
            'balance': round(balance, 3),
            'left': round(left, 4),
            'right': round(right, 4),
            'stereo': True,
        }

    def set_balance(self, device_name, balance):
        """设置设备左右声道平衡

        balance: -1.0 ~ 1.0（负值偏左，正值偏右）
        返回 {'success': bool, 'data': str, 'balance': float}
        """
        balance = max(-1.0, min(1.0, float(balance)))

        if not device_name or not _SAFE_DEVICE_PATTERN.match(device_name):
            return {'success': False, 'data': '无效的设备名', 'balance': balance}

        # 从 pw-dump 获取当前声道音量
        props_params, node_obj = self._get_node_props_params(device_name)
        channel_volumes = props_params.get('channelVolumes', [])

        if not channel_volumes or len(channel_volumes) < 2:
            return {'success': False, 'data': '该设备不是立体声设备', 'balance': balance}

        # 根据平衡值调整左右声道
        base_vol = (float(channel_volumes[0]) + float(channel_volumes[1])) / 2.0
        left = max(0.0, min(2.0, base_vol * (1.0 - balance)))
        right = max(0.0, min(2.0, base_vol * (1.0 + balance)))

        target_node_id = node_obj.get('id') if node_obj else None
        if target_node_id is not None:
            run_command(
                f"{platform_paths.CMD_PW_CLI} set-param {target_node_id} Props "
                f"'{{ \"channelVolumes\": [ {left:.4f}, {right:.4f} ] }}'",
                timeout=5)

        # 验证设置结果
        actual = self.get_balance(device_name)
        actual_balance = actual.get('balance', balance) if actual.get('stereo') else balance
        logger.info(f"声道平衡: {device_name} -> 目标{balance} 实际{actual_balance}")

        return {
            'success': True,
            'data': f'平衡已设为 {actual_balance}',
            'balance': actual_balance,
        }

    # ── 私有方法 ──────────────────────────────────────────────────

    def _resolve_node_id(self, device_name):
        """从 wpctl status 输出中解析设备对应的 node ID

        先尝试通过 pw-dump 获取，再解析 wpctl status
        """
        # 方法1：通过 pw-dump 直接获取 node ID
        pw_data = pw_dump()
        for obj in pw_data:
            if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
                continue
            props = obj.get('info', {}).get('props', {})
            media_class = props.get('media.class', '')
            if media_class not in ('Audio/Sink', 'Audio/Sink/Virtual',
                                   'Audio/Source', 'Audio/Source/Virtual'):
                continue
            node_name = props.get('node.name', '')
            node_desc = props.get('node.description', '')
            if node_name == device_name or node_desc == device_name:
                return obj.get('id')

        # 方法2：解析 wpctl status
        is_source = self._is_source_device(device_name)
        if is_source:
            return self._get_wpctl_id_for_source(device_name)
        else:
            return self._get_wpctl_id_for_sink(device_name)

    @staticmethod
    def _cubic_to_linear(vol):
        """PipeWire 立方音量曲线 → 线性值"""
        if vol <= 0:
            return 0.0
        return vol ** (1.0 / 3.0)

    @staticmethod
    def _linear_to_cubic(vol):
        """线性值 → PipeWire 立方音量曲线"""
        if vol <= 0:
            return 0.0
        return vol ** 3

    @staticmethod
    def _is_source_device(device_name):
        """检查设备是否为音频输入（Audio/Source）"""
        if not device_name:
            return False
        pw_data = pw_dump()
        for obj in pw_data:
            if not isinstance(obj, dict):
                continue
            if obj.get('type') != 'PipeWire:Interface:Node':
                continue
            props = obj.get('info', {}).get('props', {})
            node_name = props.get('node.name', '')
            if node_name == device_name:
                return props.get('media.class', '') in ('Audio/Source', 'Audio/Source/Virtual')
        return False

    def _get_wpctl_volume(self, node_id):
        """通过 wpctl 获取已知 node ID 的音量"""
        result = run_command(f"{platform_paths.CMD_WPCTL} get-volume {node_id} 2>/dev/null", timeout=3)
        vol_percent = 0
        muted = False
        if result['success'] and result['stdout']:
            vol_str = result['stdout'].strip()
            m = re.search(r'Volume:\s*([\d.]+)', vol_str)
            if m:
                vol_flat = float(m.group(1))
                vol_percent = min(round(vol_flat * 100), 100)
            if 'MUTED' in vol_str.upper():
                muted = True
        return {'volume': vol_percent, 'muted': muted}

    # ── 音量获取回退方法 ──────────────────────────────────────────

    def _try_get_volume_wpctl(self, device_name):
        """尝试通过 wpctl 获取音量"""
        node_id = self._resolve_node_id(device_name)
        if node_id is None:
            return None

        vol_info = self._get_wpctl_volume(node_id)
        vol_percent = vol_info['volume']
        muted = vol_info['muted']

        # wpctl 返回 0 且非静音时，可能是解析失败，尝试 pw-dump 补充
        if vol_percent > 0 or muted:
            return {'volume': vol_percent, 'muted': muted, 'device': device_name}

        # 补充：用 pw-dump Props 验证
        props_params, _ = self._get_node_props_params(device_name)
        ch_vols = props_params.get('channelVolumes', [])
        if ch_vols and isinstance(ch_vols, list):
            valid = [self._cubic_to_linear(float(cv)) for cv in ch_vols
                     if isinstance(cv, (int, float))]
            if valid:
                pw_vol = min(round((sum(valid) / len(valid)) * 100), 100)
                if pw_vol > 0:
                    return {
                        'volume': pw_vol,
                        'muted': bool(props_params.get('mute', False)),
                        'device': device_name,
                    }

        return {'volume': 0, 'muted': muted, 'device': device_name}

    def _try_get_volume_pw_dump(self, device_name):
        """尝试通过 pw-dump Props 获取音量（channelVolumes + cubic_to_linear）"""
        props_params, _ = self._get_node_props_params(device_name)
        if not props_params:
            return None

        ch_vols = props_params.get('channelVolumes', [])
        if ch_vols and isinstance(ch_vols, list):
            valid = [self._cubic_to_linear(float(cv)) for cv in ch_vols
                     if isinstance(cv, (int, float))]
            if valid:
                vol_percent = min(round((sum(valid) / len(valid)) * 100), 100)
                return {
                    'volume': vol_percent,
                    'muted': bool(props_params.get('mute', False)),
                    'device': device_name,
                }

        # 回退到 volume 字段
        raw_vol = props_params.get('volume', 0.0)
        if isinstance(raw_vol, (int, float)) and raw_vol >= 0:
            vol_percent = min(round(self._cubic_to_linear(float(raw_vol)) * 100), 100)
            return {
                'volume': vol_percent,
                'muted': bool(props_params.get('mute', False)),
                'device': device_name,
            }

        return None

    def _try_get_volume_pactl(self, device_name):
        """尝试通过 pactl 获取音量"""
        is_source = self._is_source_device(device_name)
        if is_source:
            result = run_command(
                f"{platform_paths.CMD_PACTL} get-source-volume {shlex.quote(device_name)} 2>/dev/null", timeout=5)
        else:
            result = run_command(
                f"{platform_paths.CMD_PACTL} get-sink-volume {shlex.quote(device_name)} 2>/dev/null", timeout=5)

        if not result['success'] or not result['stdout']:
            return None

        # 解析 pactl 输出中的音量百分比
        m = re.search(r'(\d+)%', result['stdout'])
        if m:
            vol_percent = int(m.group(1))
            # 检查静音状态
            muted = 'Mute: yes' in result['stdout']
            return {'volume': vol_percent, 'muted': muted, 'device': device_name}

        return None

    # ── wpctl status 解析 ─────────────────────────────────────────

    @staticmethod
    def _get_wpctl_id_for_sink(name):
        """从 wpctl status 中解析 Sink 设备的 node ID"""
        result = run_command(f"{platform_paths.CMD_WPCTL} status 2>/dev/null", timeout=5)
        if not result['success'] or not result['stdout']:
            return None
        in_sinks = False
        for line in result['stdout'].splitlines():
            stripped = line.strip()
            if stripped.startswith('Sinks') or stripped.startswith('├─ Sinks') or stripped.startswith('└─ Sinks'):
                in_sinks = True
                continue
            if in_sinks:
                if stripped.startswith('├─') or stripped.startswith('└─') or stripped.startswith('│'):
                    if stripped.endswith(']') or stripped.endswith('*'):
                        parts = stripped.split()
                        for p in parts:
                            p_clean = p.strip('*')
                            if p_clean.isdigit():
                                node_id = int(p_clean)
                                resolved = get_node_name_by_id(node_id)
                                if resolved == name:
                                    return node_id
                if not stripped or stripped.startswith('Clients') or stripped.startswith('├─ Clients') or stripped.startswith('└─ Clients'):
                    break
        return None

    @staticmethod
    def _get_wpctl_id_for_source(name):
        """从 wpctl status 中解析 Source 设备的 node ID"""
        result = run_command(f"{platform_paths.CMD_WPCTL} status 2>/dev/null", timeout=5)
        if not result['success'] or not result['stdout']:
            return None
        in_sources = False
        for line in result['stdout'].splitlines():
            stripped = line.strip()
            if stripped.startswith('Sources') or stripped.startswith('├─ Sources') or stripped.startswith('└─ Sources'):
                in_sources = True
                continue
            if in_sources:
                if stripped.startswith('├─') or stripped.startswith('└─') or stripped.startswith('│'):
                    if stripped.endswith(']') or stripped.endswith('*'):
                        parts = stripped.split()
                        for p in parts:
                            p_clean = p.strip('*')
                            if p_clean.isdigit():
                                node_id = int(p_clean)
                                resolved = get_node_name_by_id(node_id)
                                if resolved == name:
                                    return node_id
                if not stripped or stripped.startswith('Clients') or stripped.startswith('├─ Clients') or stripped.startswith('└─ Clients'):
                    break
        return None

    # ── pw-dump Props 提取 ────────────────────────────────────────

    @staticmethod
    def _get_node_props_params(device_name):
        """获取指定设备的 Props params，返回 (props_params, node_obj)"""
        pw_data = pw_dump()
        for obj in pw_data:
            if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
                continue
            props = obj.get('info', {}).get('props', {})
            if props.get('node.name') == device_name:
                params = obj.get('info', {}).get('params', {})
                return extract_pw_vol_params(params if isinstance(params, dict) else {}), obj
        return {}, None

    # ── 方法缓存 ──────────────────────────────────────────────────

    def _get_cached_method(self, device_name):
        """获取设备上次成功的音量控制方法"""
        with self._cache_lock:
            return self._method_cache.get(device_name)

    def _set_cached_method(self, device_name, method):
        """记录设备成功的音量控制方法"""
        with self._cache_lock:
            self._method_cache[device_name] = method

    # ── 音量验证 ──────────────────────────────────────────────────

    def _verify_volume(self, device_name, target_volume):
        """设置音量后验证实际值"""
        verify = self.get_volume(device_name)
        actual_pct = -1
        if verify is not None:
            actual_pct = verify.get('volume', -1)
        logger.info(f"设置音量: {device_name} -> 目标{target_volume}% 实际{actual_pct}%")
        return {
            'success': True,
            'data': f'音量已设为 {actual_pct}%',
            'verified_volume': actual_pct,
        }


# ── 模块级单例 ────────────────────────────────────────────────────

_volume_controller = VolumeController()


def get_volume(device_name=None):
    """获取设备音量（模块级便捷函数）"""
    if not device_name:
        device_name = get_default_sink_name()
        if not device_name:
            return {'success': False, 'error': '无法获取默认设备'}
    result = _volume_controller.get_volume(device_name)
    if result is not None:
        return {'success': True, 'data': result}
    return {'success': False, 'error': '获取音量失败'}


def set_volume(device_name=None, volume=50):
    """设置设备音量（模块级便捷函数）"""
    if not device_name:
        device_name = get_default_sink_name()
        if not device_name:
            return {'success': False, 'error': '无法获取默认设备'}
    return _volume_controller.set_volume(device_name, volume)


def set_mute(device_name=None, mute=True):
    """设置设备静音（模块级便捷函数）"""
    if not device_name:
        device_name = get_default_sink_name()
        if not device_name:
            return {'success': False, 'error': '无法获取默认设备'}
    return _volume_controller.set_mute(device_name, mute)


def get_balance(device_name=None):
    """获取设备声道平衡（模块级便捷函数）"""
    if not device_name:
        device_name = get_default_sink_name()
    if not device_name:
        return {'success': False, 'data': None, 'error': '获取平衡信息失败'}
    result = _volume_controller.get_balance(device_name)
    return {'success': True, 'data': {**result, 'device': device_name}}


def set_balance(device_name=None, balance=0.0):
    """设置设备声道平衡（模块级便捷函数）"""
    if not device_name:
        device_name = get_default_sink_name()
    if not device_name:
        return {'success': False, 'error': '设置平衡失败'}
    return _volume_controller.set_balance(device_name, balance)
