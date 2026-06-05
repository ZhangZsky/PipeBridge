import copy
import json
import os
import threading
import time

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


def _config_path():
    return os.path.join(_get_config_dir(), CONFIG_FILE)


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
            except (json.JSONDecodeError, IOError):
                pass
        defaults = _default_config()
        _config_cache = defaults
        _cache_time = time.time()
        return copy.deepcopy(defaults)


def _atomic_update(updater):
    # 原子更新配置：读取文件→执行 updater→写回文件，并清除缓存
    global _config_cache, _cache_time
    with _lock:
        path = _config_path()
        cfg = {}
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
            except (json.JSONDecodeError, IOError):
                cfg = {}
        defaults = _default_config()
        for key in defaults:
            if key not in cfg:
                cfg[key] = defaults[key]
        updater(cfg)
        cfg_dir = os.path.dirname(path)
        os.makedirs(cfg_dir, exist_ok=True)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            _config_cache = None
            _cache_time = 0
            return True
        except IOError:
            return False


def add_paired_device(mac, alias='', name='', is_audio=False):
    def _update(cfg):
        cfg['paired_devices'][mac.upper()] = {
            'alias': alias or name or mac,
            'name': name or alias or mac,
            'mac': mac.upper(),
            'is_audio': is_audio
        }
        if alias:
            cfg['device_aliases'][mac.upper()] = alias
    _atomic_update(_update)


def remove_paired_device(mac):
    def _update(cfg):
        mac_upper = mac.upper()
        cfg['paired_devices'].pop(mac_upper, None)
        cfg['device_aliases'].pop(mac_upper, None)
    _atomic_update(_update)


def get_cached_paired_devices():
    cfg = load_config()
    return cfg.get('paired_devices', {})


def set_default_sink(sink_name):
    def _update(cfg):
        cfg['default_sink'] = sink_name
    _atomic_update(_update)


def get_default_sink():
    cfg = load_config()
    return cfg.get('default_sink', '')


def set_default_source(source_name):
    def _update(cfg):
        cfg['default_source'] = source_name
    _atomic_update(_update)


def get_default_source():
    cfg = load_config()
    return cfg.get('default_source', '')


def set_last_scan(devices):
    def _update(cfg):
        cfg['last_scan'] = devices[:50]
    _atomic_update(_update)


def get_last_scan():
    cfg = load_config()
    return cfg.get('last_scan', [])


def set_audio_devices(devices):
    def _update(cfg):
        cfg['audio_devices'] = devices[:50]
    _atomic_update(_update)


def get_audio_devices():
    cfg = load_config()
    return cfg.get('audio_devices', [])


def set_auto_reconnect(enabled: bool):
    def _update(cfg):
        cfg['auto_reconnect'] = enabled
    _atomic_update(_update)


def get_auto_reconnect() -> bool:
    cfg = load_config()
    return cfg.get('auto_reconnect', True)


def add_reconnect_blacklist(mac: str):
    def _update(cfg):
        mac_upper = mac.upper()
        if mac_upper not in cfg['reconnect_blacklist']:
            cfg['reconnect_blacklist'].append(mac_upper)
    _atomic_update(_update)


def remove_reconnect_blacklist(mac: str):
    def _update(cfg):
        mac_upper = mac.upper()
        if mac_upper in cfg['reconnect_blacklist']:
            cfg['reconnect_blacklist'].remove(mac_upper)
    _atomic_update(_update)


def is_reconnect_blacklisted(mac: str) -> bool:
    cfg = load_config()
    return mac.upper() in cfg['reconnect_blacklist']


def set_default_video_sink(sink_name):
    def _update(cfg):
        cfg['default_video_sink'] = sink_name
    _atomic_update(_update)


def get_default_video_sink():
    cfg = load_config()
    return cfg.get('default_video_sink', '')


def set_video_devices(devices):
    def _update(cfg):
        cfg['video_devices'] = devices[:50]
    _atomic_update(_update)


def get_video_devices():
    cfg = load_config()
    return cfg.get('video_devices', [])


def set_system_overview(overview):
    def _update(cfg):
        cfg['system_overview'] = overview
    _atomic_update(_update)


def get_system_overview():
    cfg = load_config()
    return cfg.get('system_overview', {})


def set_bt_power_enabled(enabled: bool):
    def _update(cfg):
        cfg['bt_power_enabled'] = enabled
    _atomic_update(_update)


def get_bt_power_enabled() -> bool:
    cfg = load_config()
    return cfg.get('bt_power_enabled', True)
