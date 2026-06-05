"""统一设备模型数据类"""

from dataclasses import dataclass, field


@dataclass
class DeviceInfo:
    """设备信息基类"""
    name: str = ''
    friendly_name: str = ''
    node_id: int = None
    device_type: str = ''
    role: str = ''
    is_default: bool = False
    extended: dict = field(default_factory=dict)


@dataclass
class AudioDeviceInfo(DeviceInfo):
    """音频设备信息"""
    volume: int = 0
    volume_flat: float = 0.0
    volume_db: float = 0.0
    muted: bool = False
    channels: list = field(default_factory=list)
    sample_rate: int = 0
    sample_format: str = ''
    channel_count: int = 0
    balance: float = 0.0
    ports: list = field(default_factory=list)
    active_port: str = ''
    card_index: int = None
    monitor_source: str = ''
    audio_type: str = ''
    driver: str = ''
    state: str = ''
    needs_activate: bool = False


@dataclass
class BluetoothDeviceInfo(DeviceInfo):
    """蓝牙设备信息"""
    mac: str = ''
    type: str = ''  # 直接用 type，不再用 device_type 映射
    paired: bool = False
    trusted: bool = False
    connected: bool = False
    blocked: bool = False
    alias: str = ''
    icon: str = ''
    vendor: str = ''
    battery: str = ''
    is_audio: bool = False
    profiles: list = field(default_factory=list)
    active_profile: str = ''
    device_class: str = ''
    rssi: str = ''
    address_type: str = ''
    services_resolved: bool = False


@dataclass
class VideoDeviceInfo(DeviceInfo):
    """视频设备信息"""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    pixel_format: str = ''
    formats: list = field(default_factory=list)
    video_type: str = ''
    media_class: str = ''
