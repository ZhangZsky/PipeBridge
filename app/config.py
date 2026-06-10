import copy
import json
import logging
import os
import threading
import time

logger = logging.getLogger('MediaHub')

CONFIG_FILE = 'mediahub.conf'

_lock = threading.Lock()
_config_cache = None
_cache_time = 0
_CACHE_TTL = 10


def _get_config_dir():
    # 生产环境：fnOS 框架下 $TRIM_PKGETC 指向 @appconf 存储卷
    pkgetc = os.environ.get('TRIM_PKGETC', '')
    if pkgetc and os.path.isdir(pkgetc):
        return pkgetc
    # 开发/测试环境回退
    config_dir = os.path.join(os.path.expanduser('~'), '.config', 'mediahub')
    os.makedirs(config_dir, exist_ok=True)
    return config_dir


# 返回配置文件路径
def _config_path():
    return os.path.join(_get_config_dir(), CONFIG_FILE)


# 返回默认配置
def _default_config():
    return {
        'paired_devices': {},
        'default_sink': '',
        'default_source': '',
        'device_aliases': {},
        'last_scan': [],
        'audio_devices': [],
        'auto_reconnect': True,
        'reconnect_blacklist': [],
        'default_video_sink': '',
        'video_devices': [],
        'system_overview': {},
        'bt_power_enabled': True,
    }


def load_config():
    # 加载配置，TTL 内返回缓存副本
    global _config_cache, _cache_time
    with _lock:
        now = time.time()
        if _config_cache is not None and now - _cache_time < _CACHE_TTL:
            return copy.deepcopy(_config_cache)

        path = _config_path()
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                defaults = _default_config()
                for key in defaults:
                    if key not in cfg:
                        cfg[key] = defaults[key]
                _config_cache = cfg
                _cache_time = time.time()
                return copy.deepcopy(cfg)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning('加载配置文件失败，将使用默认配置: %s: %s', path, e)
        defaults = _default_config()
        _config_cache = defaults
        _cache_time = time.time()
        return copy.deepcopy(defaults)


def _atomic_update(updater):
    # 原子更新配置：读取文件→执行 updater→写临时文件→原子替换，并清除缓存
    global _config_cache, _cache_time
    with _lock:
        path = _config_path()
        cfg = {}
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning('读取配置文件失败，将使用空配置: %s: %s', path, e)
                cfg = {}
        defaults = _default_config()
        for key in defaults:
            if key not in cfg:
                cfg[key] = defaults[key]
        updater(cfg)
        cfg_dir = os.path.dirname(path)
        os.makedirs(cfg_dir, exist_ok=True)
        try:
            tmp_path = path + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, path)
            _config_cache = None
            _cache_time = 0
            return True
        except IOError:
            # 清理临时文件
            try:
                os.unlink(path + '.tmp')
            except OSError as e:
                logger.warning('清理临时配置文件失败: %s.tmp: %s', path, e)
            return False


def config_get(key, default=None):
    """通用配置读取"""
    cfg = load_config()
    return cfg.get(key, default)


def config_set(key, value):
    """通用配置写入"""
    def _update(cfg):
        cfg[key] = value
    _atomic_update(_update)


# 添加已配对设备
def add_paired_device(mac, alias='', name='', is_audio=False, rssi=''):
    def _update(cfg):
        existing = cfg['paired_devices'].get(mac.upper(), {})
        cfg['paired_devices'][mac.upper()] = {
            'alias': alias or name or mac,
            'name': name or alias or mac,
            'mac': mac.upper(),
            'is_audio': is_audio,
            'rssi': rssi or existing.get('rssi', '')
        }
        if alias:
            cfg['device_aliases'][mac.upper()] = alias
    _atomic_update(_update)


# 移除已配对设备
def remove_paired_device(mac):
    def _update(cfg):
        mac_upper = mac.upper()
        cfg['paired_devices'].pop(mac_upper, None)
        cfg['device_aliases'].pop(mac_upper, None)
    _atomic_update(_update)


# 获取缓存的配对设备
def get_cached_paired_devices():
    cfg = load_config()
    return cfg.get('paired_devices', {})


# 更新缓存中设备的 RSSI 值
def update_device_rssi(mac, rssi):
    mac_upper = mac.upper()
    def _update(cfg):
        if mac_upper in cfg['paired_devices']:
            cfg['paired_devices'][mac_upper]['rssi'] = rssi
    _atomic_update(_update)


# 保存默认输出设备
def set_default_sink(sink_name):
    config_set('default_sink', sink_name)


# 读取默认输出设备
def get_default_sink():
    return config_get('default_sink', '')


# 保存默认输入设备
def set_default_source(source_name):
    config_set('default_source', source_name)


# 读取默认输入设备
def get_default_source():
    return config_get('default_source', '')


# 保存最近扫描结果
def set_last_scan(devices):
    config_set('last_scan', devices[:50])


# 读取最近扫描结果
def get_last_scan():
    return config_get('last_scan', [])


# 保存音频设备缓存
def set_audio_devices(devices):
    config_set('audio_devices', devices[:50])


# 读取音频设备缓存
def get_audio_devices():
    return config_get('audio_devices', [])


# 保存自动重连开关
def set_auto_reconnect(enabled: bool):
    config_set('auto_reconnect', enabled)


# 读取自动重连开关
def get_auto_reconnect() -> bool:
    return config_get('auto_reconnect', True)


# 添加重连黑名单
def add_reconnect_blacklist(mac: str):
    def _update(cfg):
        mac_upper = mac.upper()
        if mac_upper not in cfg['reconnect_blacklist']:
            cfg['reconnect_blacklist'].append(mac_upper)
    _atomic_update(_update)


# 移除重连黑名单
def remove_reconnect_blacklist(mac: str):
    def _update(cfg):
        mac_upper = mac.upper()
        if mac_upper in cfg['reconnect_blacklist']:
            cfg['reconnect_blacklist'].remove(mac_upper)
    _atomic_update(_update)


# 检查是否在重连黑名单
def is_reconnect_blacklisted(mac: str) -> bool:
    cfg = load_config()
    return mac.upper() in cfg['reconnect_blacklist']


# 保存默认视频设备
def set_default_video_sink(sink_name):
    config_set('default_video_sink', sink_name)


# 读取默认视频设备
def get_default_video_sink():
    return config_get('default_video_sink', '')


# 保存视频设备缓存
def set_video_devices(devices):
    config_set('video_devices', devices[:50])


# 读取视频设备缓存
def get_video_devices():
    return config_get('video_devices', [])


# 保存系统概览缓存
def set_system_overview(overview):
    config_set('system_overview', overview)


# 读取系统概览缓存
def get_system_overview():
    return config_get('system_overview', {})


# 保存蓝牙电源状态
def set_bt_power_enabled(enabled: bool):
    config_set('bt_power_enabled', enabled)


# 读取蓝牙电源状态
def get_bt_power_enabled() -> bool:
    return config_get('bt_power_enabled', True)
