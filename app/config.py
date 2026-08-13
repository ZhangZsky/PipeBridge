import json
import logging
import os
import threading
import time

logger = logging.getLogger('PipeBridge')

CONFIG_FILE = 'pipebridge.conf'

# 运行时数据（设备/扫描/概览）不持久化，每次请求从 PipeWire/BlueZ 实时获取；配置文件仅存跨重启的用户设置
_LEGACY_RUNTIME_KEYS = ('last_scan', 'audio_devices', 'video_devices', 'system_overview', 'paired_devices', 'reconnect_blacklist')

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
        'default_sink': '',
        'default_source': '',
        'device_aliases': {},
        'auto_reconnect': True,
        'default_video_sink': '',
        'bt_power_enabled': True,
    }

def _migrate_legacy_keys(cfg):
    # 移除旧版本遗留的临时运行时字段，避免配置文件携带过期缓存
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
    # 原子写入配置文件
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

def _set_default_endpoint(key, role, name):
    config_set(key, name)

def _get_default_endpoint(key, role):
    return config_get(key, '')

def set_default_sink(sink_name):
    _set_default_endpoint('default_sink', 'sink', sink_name)

def get_default_sink():
    return _get_default_endpoint('default_sink', 'sink')

def set_default_source(source_name):
    _set_default_endpoint('default_source', 'source', source_name)

def get_default_source():
    return _get_default_endpoint('default_source', 'source')

def set_default_video_sink(sink_name):
    config_set('default_video_sink', sink_name)

def get_default_video_sink():
    return config_get('default_video_sink', '')

def set_bt_power_enabled(enabled: bool):
    config_set('bt_power_enabled', enabled)

def get_bt_power_enabled() -> bool:
    return config_get('bt_power_enabled', True)

def set_auto_reconnect(enabled: bool):
    config_set('auto_reconnect', bool(enabled))

def get_auto_reconnect() -> bool:
    return bool(config_get('auto_reconnect', True))

def get_device_aliases() -> dict:
    # 返回全部设备别名映射（键为大写 MAC），供列表补充自定义名称
    aliases = config_get('device_aliases', {})
    return aliases if isinstance(aliases, dict) else {}

def get_device_alias(mac: str) -> str:
    if not mac:
        return ''
    return get_device_aliases().get(mac.upper(), '')

def set_device_alias(mac: str, alias: str):
    # 持久化设备别名（键统一大写 MAC，与读取端一致）
    mac_key = mac.upper()
    def _update(cfg):
        aliases = cfg.get('device_aliases')
        if not isinstance(aliases, dict):
            aliases = {}
        aliases[mac_key] = alias
        cfg['device_aliases'] = aliases
    _atomic_update(_update)

def remove_device_alias(mac: str):
    # 删除设备时清理其持久化别名，避免遗留脏数据
    mac_key = mac.upper()
    def _update(cfg):
        aliases = cfg.get('device_aliases')
        if isinstance(aliases, dict) and mac_key in aliases:
            del aliases[mac_key]
            cfg['device_aliases'] = aliases
    _atomic_update(_update)
