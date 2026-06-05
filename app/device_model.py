"""统一设备模型数据类"""

from dataclasses import dataclass, field, fields


@dataclass
class DeviceInfo:
    """设备信息基类"""
    name: str = ''
    friendly_name: str = ''
    node_id: int = None
    device_type: str = ''
    role: str = ''
    is_default: bool = False
    extended: dict = None

    def to_dict(self):
        """转换为字典，跳过 None 值"""
        result = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if value is not None:
                result[f.name] = value
        return result


@dataclass
class AudioDeviceInfo(DeviceInfo):
    """音频设备信息"""
    volume: int = 0
    volume_flat: float = 0.0
    volume_db: float = 0.0
    muted: bool = False
    channels: list = None
    sample_rate: int = 0
    sample_format: str = ''
    channel_count: int = 0
    balance: float = 0.0
    ports: list = None
    active_port: str = ''
    card_index: int = None
    monitor_source: str = ''
    audio_type: str = ''
    driver: str = ''
    state: str = ''
    needs_activate: bool = False

    def to_dict(self):
        """转换为字典，兼容 audio_manager API 格式"""
        return {
            'name': self.name,
            'friendly_name': self.friendly_name,
            'driver': self.driver,
            'state': self.state,
            'is_default': self.is_default,
            'audio_type': self.audio_type,
            'role': self.role,
            'node_id': self.node_id,
            'volume': self.volume,
            'volume_flat': self.volume_flat,
            'volume_db': self.volume_db,
            'muted': self.muted,
            'channels': self.channels if self.channels is not None else [],
            'sample_rate': self.sample_rate,
            'sample_format': self.sample_format,
            'card_index': self.card_index,
            'monitor_source': self.monitor_source,
            'ports': self.ports if self.ports is not None else [],
            'active_port': self.active_port,
            'channel_count': self.channel_count,
            'balance': self.balance,
            'extended': self.extended if self.extended is not None else {},
            'needs_activate': self.needs_activate,
        }


@dataclass
class BluetoothDeviceInfo(DeviceInfo):
    """蓝牙设备信息"""
    mac: str = ''
    paired: bool = False
    trusted: bool = False
    connected: bool = False
    blocked: bool = False
    alias: str = ''
    icon: str = ''
    vendor: str = ''
    battery: str = ''
    is_audio: bool = False
    profiles: list = None
    active_profile: str = ''
    device_class: str = ''
    rssi: str = ''
    address_type: str = ''
    services_resolved: bool = False

    def to_dict(self):
        """转换为字典，兼容 bluetooth_manager API 格式"""
        result = {
            'mac': self.mac,
            'name': self.name,
            'connected': self.connected,
            'type': self.device_type,
            'paired': self.paired,
            'trusted': self.trusted,
            'blocked': self.blocked,
            'alias': self.alias,
            'icon': self.icon,
            'vendor': self.vendor,
            'battery': self.battery,
            'is_audio': self.is_audio,
            'device_class': self.device_class,
            'rssi': self.rssi,
            'address_type': self.address_type,
            'services_resolved': self.services_resolved,
        }
        # 跳过空字符串的可选字段
        optional_str_fields = {
            'alias', 'icon', 'vendor', 'battery', 'device_class',
            'rssi', 'address_type',
        }
        cleaned = {}
        for k, v in result.items():
            if k in optional_str_fields and v == '':
                continue
            cleaned[k] = v
        return cleaned


@dataclass
class VideoDeviceInfo(DeviceInfo):
    """视频设备信息"""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    pixel_format: str = ''
    formats: list = None
    video_type: str = ''
    media_class: str = ''

    def to_dict(self):
        """转换为字典，兼容 video_manager API 格式"""
        result = {
            'name': self.name,
            'friendly_name': self.friendly_name,
            'node_id': self.node_id,
            'video_type': self.video_type,
            'role': self.role,
            'media_class': self.media_class,
            'width': self.width,
            'height': self.height,
            'fps': self.fps,
            'pixel_format': self.pixel_format,
            'formats': self.formats if self.formats is not None else [],
            'is_default': self.is_default,
            'extended': self.extended if self.extended is not None else {},
        }
        # 跳过 None 值和空字符串的可选字段
        cleaned = {}
        for k, v in result.items():
            if v is None:
                continue
            if isinstance(v, str) and v == '' and k not in ('name', 'friendly_name', 'video_type', 'role', 'media_class'):
                continue
            cleaned[k] = v
        return cleaned
