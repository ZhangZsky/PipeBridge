import re
from fastapi.responses import JSONResponse
from exceptions import InvalidParamError

MAC_PATTERN = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$')


def _as_bool(val):
    """将各种类型安全地转为布尔值，避免 bool("false") 为 True"""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        return val.lower() in ('true', '1', 'yes')
    return bool(val)


def _json(result, **extra):
    if isinstance(result, dict) and 'success' in result:
        content = dict(result)  # 复制，避免修改原始对象
    else:
        content = {'success': True, 'data': result}
    content.update(extra)
    return JSONResponse(content=content)


def _validate_mac(mac):
    if not mac or not MAC_PATTERN.match(mac):
        raise InvalidParamError("需要有效的 MAC 地址")
