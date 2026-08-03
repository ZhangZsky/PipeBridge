import json
import logging
import os
import threading
import time

logger = logging.getLogger('PipeBridge')

CONFIG_FILE = 'pipebridge.conf'

# 临时运行时数据（设备列表/扫描结果/系统概览）不再持久化到配置文件。
# 这些数据每次请求都从 PipeWire/BlueZ 实时获取，刷新页面即为新数据。
# 配置文件只保留需要跨重启持久化的用户设置类数据。
_LEGACY_RUNTIME_KEYS = ('last_scan', 'audio_devices', 'video_devices', 'system_overview')

_lock = threading.Lock()
_config_cache = None
_config_cache_time = 0
_CONFIG_CACHE_TTL = 1.0

def _get_config_dir():
    pkgetc = os.environ.get('TRIM_PKGETC', '')
    if pkgetc and os.path.isdir(pkgetc):
        return pkgetc
    config_dir = os.path.join(os.path.expanduser('~'), '.config', 'pipebridge')
    os.makedirs(config_dir, exist_ok=True)
    return config_dir

def _config_path():
    return os.path.join(_get_config_dir(), CONFIG_FILE)

def _default_config():
    # 仅保留需要持久化的用户设置类数据
    return {
        'paired_devices': {},
        'default_sink': '',
        'default_source': '',
        'device_aliases': {},
        'auto_reconnect': True,
        'reconnect_blacklist': [],
        'default_video_sink': '',
        'bt_power_enabled': True,
    }

def _migrate_legacy_keys(cfg):
    """移除旧版本遗留的临时运行时数据字段，避免配置文件继续携带过期缓存。"""
    changed = False
    for key in _LEGACY_RUNTIME_KEYS:
        if key in cfg:
            cfg.pop(key, None)
            changed = True
    return changed

def _write_default_config(path):
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
                    cfg = _write_default_config(path)
                else:
                    cfg = json.loads(raw)
                defaults = _default_config()
                for key in defaults:
                    if key not in cfg:
                        cfg[key] = defaults[key]
                # 迁移清理旧版本遗留的临时数据字段
                if _migrate_legacy_keys(cfg):
                    _save_config(path, cfg)
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

def _save_config(path, cfg):
    """原子写入配置文件。"""
    cfg_dir = os.path.dirname(path)
    os.makedirs(cfg_dir, exist_ok=True)
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)

def _atomic_update(updater):
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
        # 写入前再次清理遗留字段
        _migrate_legacy_keys(cfg)
        updater(cfg)
        try:
            _save_config(path, cfg)
            _config_cache = cfg
            _config_cache_time = time.time()
            return True
        except IOError:
            try:
                os.unlink(path + '.tmp')
            except OSError as e:
                logger.warning('清理临时配置文件失败: %s.tmp: %s', path, e)
            return False

def config_get(key, default=None):
    cfg = load_config()
    return cfg.get(key, default)

def config_set(key, value):
    def _update(cfg):
        cfg[key] = value
    _atomic_update(_update)

def add_paired_device(mac, alias='', name='', is_audio=False, rssi=''):
    # 仅持久化身份识别所需的最小字段：mac / name / alias。
    # is_audio、rssi 等运行时属性每次从 BlueZ 实时获取，不再写入配置文件。
    # 保留 is_audio/rssi 形参仅为兼容既有调用点，不参与持久化。
    def _update(cfg):
        cfg['paired_devices'][mac.upper()] = {
            'mac': mac.upper(),
            'name': name or alias or mac,
            'alias': alias or name or mac,
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

def _is_pcspkr_name(device_name):
    return device_name and ('pcspkr' in device_name.lower() or 'pcsp' in device_name.lower())

def set_default_sink(sink_name):
    if _is_pcspkr_name(sink_name):
        logger.warning(f"拒绝保存蜂鸣器设备为默认 sink: {sink_name}")
        sink_name = ''
    config_set('default_sink', sink_name)

def get_default_sink():
    saved = config_get('default_sink', '')
    if _is_pcspkr_name(saved):
        logger.warning(f"检测到配置文件中保存了蜂鸣器作为默认 sink，清理: {saved}")
        config_set('default_sink', '')
        return ''
    return saved

def set_default_source(source_name):
    if _is_pcspkr_name(source_name):
        logger.warning(f"拒绝保存蜂鸣器设备为默认 source: {source_name}")
        source_name = ''
    config_set('default_source', source_name)

def get_default_source():
    saved = config_get('default_source', '')
    if _is_pcspkr_name(saved):
        logger.warning(f"检测到配置文件中保存了蜂鸣器作为默认 source，清理: {saved}")
        config_set('default_source', '')
        return ''
    return saved

def is_reconnect_blacklisted(mac: str) -> bool:
    cfg = load_config()
    return mac.upper() in cfg['reconnect_blacklist']

def set_default_video_sink(sink_name):
    config_set('default_video_sink', sink_name)

def get_default_video_sink():
    return config_get('default_video_sink', '')

def set_bt_power_enabled(enabled: bool):
    config_set('bt_power_enabled', enabled)

def get_bt_power_enabled() -> bool:
    return config_get('bt_power_enabled', True)
