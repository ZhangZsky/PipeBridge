import json
import logging
import os
import threading
import time

logger = logging.getLogger('MediaBridge')

CONFIG_FILE = 'mediabridge.conf'

# 设备列表最大保存数量（扫描结果、音频/视频设备缓存）
MAX_CACHED_DEVICES = 50

_lock = threading.Lock()
# 配置读取缓存（TTL 1 秒），减少 event_detector 周期轮询时的文件 IO
_config_cache = None
_config_cache_time = 0
_CONFIG_CACHE_TTL = 1.0


def _get_config_dir():
    # 生产环境：fnOS 框架下 $TRIM_PKGETC 指向 @appconf 存储卷
    pkgetc = os.environ.get('TRIM_PKGETC', '')
    if pkgetc and os.path.isdir(pkgetc):
        return pkgetc
    # 开发/测试环境回退（root 下为 /root/.config/mediabridge）
    config_dir = os.path.join(os.path.expanduser('~'), '.config', 'mediabridge')
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


def _write_default_config(path):
    # 配置文件缺失或损坏时，写入默认配置避免反复告警
    try:
        cfg_dir = os.path.dirname(path)
        os.makedirs(cfg_dir, exist_ok=True)
        defaults = _default_config()
        tmp_path = path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(defaults, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
        logger.info('已重建默认配置文件: %s', path)
        return defaults
    except OSError as e:
        logger.warning('重建默认配置文件失败: %s: %s', path, e)
        return _default_config()


def load_config():
    # 带 1 秒 TTL 缓存的配置读取，减少频繁文件 IO
    global _config_cache, _config_cache_time
    now = time.time()
    if _config_cache is not None and (now - _config_cache_time) < _CONFIG_CACHE_TTL:
        return _config_cache
    with _lock:
        path = _config_path()
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    raw = f.read()
                if not raw.strip():
                    # 空文件，重建默认配置
                    cfg = _write_default_config(path)
                else:
                    cfg = json.loads(raw)
                defaults = _default_config()
                for key in defaults:
                    if key not in cfg:
                        cfg[key] = defaults[key]
                _config_cache = cfg
                _config_cache_time = now
                return cfg
            except (json.JSONDecodeError, IOError) as e:
                logger.warning('配置文件损坏，重建默认配置: %s: %s', path, e)
                cfg = _write_default_config(path)
                _config_cache = cfg
                _config_cache_time = now
                return cfg
        cfg = _write_default_config(path)
        _config_cache = cfg
        _config_cache_time = now
        return cfg


def _atomic_update(updater):
    # 原子更新配置：读取文件→执行 updater→写临时文件→原子替换
    global _config_cache, _config_cache_time
    with _lock:
        path = _config_path()
        cfg = {}
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    raw = f.read()
                if raw.strip():
                    cfg = json.loads(raw)
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
            # 写入成功后更新缓存
            _config_cache = cfg
            _config_cache_time = time.time()
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
    config_set('last_scan', devices[:MAX_CACHED_DEVICES])


# 保存音频设备缓存
def set_audio_devices(devices):
    config_set('audio_devices', devices[:MAX_CACHED_DEVICES])


# 读取音频设备缓存
def get_audio_devices():
    return config_get('audio_devices', [])


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
    config_set('video_devices', devices[:MAX_CACHED_DEVICES])


# 读取视频设备缓存
def get_video_devices():
    return config_get('video_devices', [])


# 保存蓝牙电源状态
def set_bt_power_enabled(enabled: bool):
    config_set('bt_power_enabled', enabled)


# 读取蓝牙电源状态
def get_bt_power_enabled() -> bool:
    return config_get('bt_power_enabled', True)
