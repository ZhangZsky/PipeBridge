"""统一音量控制器 —— 仅使用 pw-dump 读取 + wpctl 写入"""

import math
import json
import logging

from utils import (
    run_command, pw_dump,
    get_node_id_by_name,
    extract_pw_vol_params,
)
import platform_paths
from exceptions import DeviceNotFoundError, CommandError

logger = logging.getLogger('MediaHub')


class VolumeController:
    """统一音量控制器，仅使用 pw-dump 读取 + wpctl 写入，失败直接抛异常"""

    # PipeWire 立方音量曲线 → 线性值
    @staticmethod
    def _cubic_to_linear(vol):
        if vol <= 0:
            return 0.0
        return vol ** (1.0 / 3.0)

    # 线性值 → PipeWire 立方音量曲线
    @staticmethod
    def _linear_to_cubic(vol):
        if vol <= 0:
            return 0.0
        return vol ** 3

    # 获取设备的 Props params 和 node 对象，找不到则抛 DeviceNotFoundError
    @staticmethod
    def _get_node_props(device_name):
        pw_data = pw_dump()
        for obj in pw_data:
            if not isinstance(obj, dict) or obj.get('type') != 'PipeWire:Interface:Node':
                continue
            props = obj.get('info', {}).get('props', {})
            if props.get('node.name') == device_name:
                params = obj.get('info', {}).get('params', {})
                return extract_pw_vol_params(params if isinstance(params, dict) else {}), obj
        raise DeviceNotFoundError(f'设备不存在: {device_name}')

    def get_volume(self, device_name):
        """获取设备音量（仅 pw-dump）

        Returns:
            dict: {'volume': int, 'muted': bool, 'device': str}
        Raises:
            DeviceNotFoundError: 设备不存在
        """
        props_params, _ = self._get_node_props(device_name)
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

        return {'volume': 0, 'muted': False, 'device': device_name}

    def set_volume(self, device_name, volume):
        """设置设备音量（仅 wpctl）

        Args:
            device_name: 设备名
            volume: 0-100 整数
        Returns:
            dict: {'volume': int, 'device': str} 设置后的验证音量
        Raises:
            DeviceNotFoundError: 设备不存在
            CommandError: wpctl 命令失败
        """
        volume = max(0, min(100, int(volume)))
        vol_flat = volume / 100.0

        node_id = get_node_id_by_name(device_name)
        if node_id is None:
            raise DeviceNotFoundError(f'设备不存在: {device_name}')

        result = run_command(
            f"{platform_paths.CMD_WPCTL} set-volume {node_id} {vol_flat:.4f}", timeout=5)
        if not result['success']:
            raise CommandError(
                f"wpctl set-volume 失败: {result.get('stderr', '')[:200]}",
                command=platform_paths.CMD_WPCTL)

        # 验证
        verify = self.get_volume(device_name)
        logger.info(f"设置音量: {device_name} -> 目标{volume}% 实际{verify['volume']}%")
        return {'volume': verify['volume'], 'device': device_name}

    def set_mute(self, device_name, mute):
        """设置设备静音（仅 wpctl）

        Args:
            device_name: 设备名
            mute: True/False
        Returns:
            dict: {'muted': bool, 'device': str}
        Raises:
            DeviceNotFoundError: 设备不存在
            CommandError: wpctl 命令失败
        """
        mute_flag = '1' if mute else '0'
        node_id = get_node_id_by_name(device_name)
        if node_id is None:
            raise DeviceNotFoundError(f'设备不存在: {device_name}')

        result = run_command(
            f"{platform_paths.CMD_WPCTL} set-mute {node_id} {mute_flag}", timeout=5)
        if not result['success']:
            raise CommandError(
                f"wpctl set-mute 失败: {result.get('stderr', '')[:200]}",
                command=platform_paths.CMD_WPCTL)

        label = '静音' if mute else '取消静音'
        logger.info(f"{label}: {device_name} node={node_id}")
        return {'muted': mute, 'device': device_name}

    def get_balance(self, device_name):
        """获取设备左右声道平衡（pw-dump Props）

        Returns:
            dict: {'balance': float, 'left': float, 'right': float, 'stereo': bool}
        Raises:
            DeviceNotFoundError: 设备不存在
        """
        props_params, _ = self._get_node_props(device_name)
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
        """设置设备左右声道平衡（pw-cli）

        Args:
            device_name: 设备名
            balance: -1.0 ~ 1.0
        Returns:
            dict: {'balance': float, 'device': str}
        Raises:
            DeviceNotFoundError: 设备不存在
            CommandError: pw-cli 命令失败
        """
        balance = max(-1.0, min(1.0, float(balance)))
        props_params, node_obj = self._get_node_props(device_name)
        channel_volumes = props_params.get('channelVolumes', [])

        if not channel_volumes or len(channel_volumes) < 2:
            raise CommandError('该设备不是立体声设备', command='pw-dump')

        base_vol = (float(channel_volumes[0]) + float(channel_volumes[1])) / 2.0
        left = max(0.0, min(2.0, base_vol * (1.0 - balance)))
        right = max(0.0, min(2.0, base_vol * (1.0 + balance)))

        target_node_id = node_obj.get('id') if node_obj else None
        if target_node_id is None:
            raise DeviceNotFoundError(f'设备不存在: {device_name}')

        # 保留多声道设备的其他声道音量不变
        new_volumes = [round(left, 4), round(right, 4)]
        for i in range(2, len(channel_volumes)):
            new_volumes.append(float(channel_volumes[i]))
        props_json = json.dumps({"channelVolumes": new_volumes})
        result = run_command(
            f"{platform_paths.CMD_PW_CLI} set-param {target_node_id} Props '{props_json}'",
            timeout=5)
        if not result['success']:
            raise CommandError(
                f"pw-cli set-param 失败: {result.get('stderr', '')[:200]}",
                command=platform_paths.CMD_PW_CLI)

        logger.info(f"声道平衡: {device_name} -> {balance}")
        return {'balance': balance, 'device': device_name}


# 模块级单例
volume_controller = VolumeController()
