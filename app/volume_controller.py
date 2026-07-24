"""统一音量控制器 —— 仅使用 pw-dump 读取 + wpctl 写入"""

import math
import json
import time
import logging

from utils import (
    run_command, pw_dump,
    get_node_id_by_name,
    extract_pw_vol_params, pw_dump_invalidate,
)
import platform_paths
from exceptions import DeviceNotFoundError, CommandError, InvalidParamError

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

    # 获取设备音量（仅 pw-dump）
    def get_volume(self, device_name):
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

    # 设置设备音量（pw-cli set-param，统一使用 cubic 音量曲线）
    def set_volume(self, device_name, volume):
        volume = max(0, min(100, int(volume)))
        props_params, node_obj = self._get_node_props(device_name)
        channel_volumes = props_params.get('channelVolumes', [])

        target_node_id = node_obj.get('id') if node_obj else None
        if target_node_id is None:
            raise DeviceNotFoundError(f'设备不存在: {device_name}')

        # 优先使用 wpctl set-volume（WirePlumber 原生命令，会被正确处理和存储）
        # wpctl set-volume 接受线性音量（0.0-1.0），会保留声道平衡比例
        wpctl_node_id = get_node_id_by_name(device_name)
        vol_linear = volume / 100.0
        if wpctl_node_id is not None:
            result = run_command(
                f"{platform_paths.CMD_WPCTL} set-volume {wpctl_node_id} {vol_linear:.4f}",
                timeout=5)
            if not result['success']:
                raise CommandError(
                    f"wpctl set-volume 失败: {result.get('stderr', '')[:200]}",
                    command=platform_paths.CMD_WPCTL)
        else:
            # fallback: pw-cli set-param（可能被 WirePlumber 覆盖，仅在没有 wpctl ID 时使用）
            vol_cubic = self._linear_to_cubic(vol_linear)
            if channel_volumes and len(channel_volumes) > 1:
                current_linears = [self._cubic_to_linear(float(cv)) for cv in channel_volumes]
                avg_current = sum(current_linears) / len(current_linears)
                if avg_current > 0:
                    new_volumes = [self._linear_to_cubic(cl / avg_current * vol_linear) for cl in current_linears]
                else:
                    new_volumes = [vol_cubic] * len(channel_volumes)
            else:
                new_volumes = [vol_cubic] * max(1, len(channel_volumes))
            props_json = json.dumps({"channelVolumes": new_volumes})
            result = run_command(
                f"{platform_paths.CMD_PW_CLI} set-param {target_node_id} Props '{props_json}'",
                timeout=5)
            if not result['success']:
                raise CommandError(
                    f"pw-cli set-param 失败: {result.get('stderr', '')[:200]}",
                    command=platform_paths.CMD_PW_CLI)
        pw_dump_invalidate()  # 清除缓存，确保后续验证读取最新数据

        # 短暂延迟后验证，确保 pw-dump 读取到新值
        time.sleep(0.15)
        verify = self.get_volume(device_name)
        logger.info(f"设置音量: {device_name} -> 目标{volume}% 实际{verify['volume']}%")
        return {'volume': verify['volume'], 'device': device_name}

    # 设置设备静音（仅 wpctl）
    def set_mute(self, device_name, mute):
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
        pw_dump_invalidate()  # 清除缓存，确保后续读取最新数据

        label = '静音' if mute else '取消静音'
        logger.info(f"{label}: {device_name} node={node_id}")
        return {'muted': mute, 'device': device_name}

    # 获取设备左右声道平衡（pw-dump Props，在线性空间计算）
    def get_balance(self, device_name):
        props_params, _ = self._get_node_props(device_name)
        channel_volumes = props_params.get('channelVolumes', [])

        if not channel_volumes or len(channel_volumes) < 2:
            return {'balance': 0.0, 'left': 0.0, 'right': 0.0, 'stereo': False}

        # 在线性空间计算平衡，与 set_balance 保持一致
        left_linear = self._cubic_to_linear(float(channel_volumes[0]))
        right_linear = self._cubic_to_linear(float(channel_volumes[1]))
        total = left_linear + right_linear
        balance = max(-1.0, min(1.0, (right_linear - left_linear) / total)) if total > 0 else 0.0

        return {
            'balance': round(balance, 3),
            'left': round(left_linear, 4),
            'right': round(right_linear, 4),
            'stereo': True,
        }

    # 设置设备左右声道平衡（pw-cli）
    def set_balance(self, device_name, balance):
        balance = max(-1.0, min(1.0, float(balance)))
        props_params, node_obj = self._get_node_props(device_name)
        channel_volumes = props_params.get('channelVolumes', [])

        if not channel_volumes or len(channel_volumes) < 2:
            raise CommandError('该设备不是立体声设备', command='pw-dump')

        # 将 cubic volume 转为线性值后再计算平衡，避免感知偏差
        left_linear = self._cubic_to_linear(float(channel_volumes[0]))
        right_linear = self._cubic_to_linear(float(channel_volumes[1]))
        base_vol_linear = (left_linear + right_linear) / 2.0

        # 在线性空间中计算平衡
        new_left_linear = max(0.0, min(2.0, base_vol_linear * (1.0 - balance)))
        new_right_linear = max(0.0, min(2.0, base_vol_linear * (1.0 + balance)))

        # 转回 cubic volume
        left = self._linear_to_cubic(new_left_linear)
        right = self._linear_to_cubic(new_right_linear)

        target_node_id = node_obj.get('id') if node_obj else None
        if target_node_id is None:
            raise DeviceNotFoundError(f'设备不存在: {device_name}')

        # 保留多声道设备的其他声道音量不变
        new_volumes = [round(left, 6), round(right, 6)]
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
        pw_dump_invalidate()  # 清除缓存，确保后续读取最新数据

        logger.info(f"声道平衡: {device_name} -> {balance}")
        return {'balance': balance, 'device': device_name}

    # 设置设备指定声道的音量（pw-cli）
    def set_channel_volume(self, device_name, channel_index, volume):
        volume = max(0, min(100, int(volume)))
        props_params, node_obj = self._get_node_props(device_name)
        channel_volumes = props_params.get('channelVolumes', [])

        channel_index = int(channel_index)
        if channel_index < 0 or channel_index >= len(channel_volumes):
            raise InvalidParamError(f'声道索引 {channel_index} 超出范围（共 {len(channel_volumes)} 个声道）')

        # 将百分比转为 PipeWire cubic volume
        new_volumes = [float(cv) for cv in channel_volumes]
        new_volumes[channel_index] = self._linear_to_cubic(volume / 100.0)

        target_node_id = node_obj.get('id') if node_obj else None
        if target_node_id is None:
            raise DeviceNotFoundError(f'设备不存在: {device_name}')

        props_json = json.dumps({"channelVolumes": new_volumes})
        result = run_command(
            f"{platform_paths.CMD_PW_CLI} set-param {target_node_id} Props '{props_json}'",
            timeout=5)
        if not result['success']:
            raise CommandError(
                f"pw-cli set-param 失败: {result.get('stderr', '')[:200]}",
                command=platform_paths.CMD_PW_CLI)

        logger.info(f"声道音量: {device_name} CH{channel_index} -> {volume}%")
        return {'device': device_name, 'channel': channel_index, 'volume': volume}


# 模块级单例
volume_controller = VolumeController()
