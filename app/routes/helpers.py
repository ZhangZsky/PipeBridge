import re
from fastapi.responses import JSONResponse
from exceptions import InvalidParamError

MAC_PATTERN = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$')

def _as_bool(val):
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        return val.lower() in ('true', '1', 'yes')
    return bool(val)

def _json(result, **extra):
    # 统一响应契约：始终产出 {success, data}。
    #   - 业务不含 success 的返回值 → success=True, data=原值
    #   - 业务含 success(如 fix_obex_agent) → success 提到顶层, 其余入 data
    #   - extra(路由补充字段,如 needs_pin/connected/device_name) 合并进 data
    # 前端统一从 result.data 读取业务数据; 不再向顶层平铺字段。
    if isinstance(result, dict) and 'success' in result:
        payload = dict(result)
        success = payload.pop('success')
        data = payload
    else:
        success = True
        data = result

    if extra:
        if isinstance(data, dict):
            merged = dict(data)
            merged.update(extra)
            data = merged
        elif data is None:
            data = dict(extra)
        else:
            data = {'value': data, **extra}

    return JSONResponse(content={'success': success, 'data': data})

def _validate_mac(mac):
    if not mac or not MAC_PATTERN.match(mac):
        raise InvalidParamError("需要有效的 MAC 地址")

def require_param(data, key, msg=None, allow_empty=False):
    # 取必填参数。默认拦截 None 与空字符串(等价于旧样板的 `not val`/`is None`);
    # allow_empty=True 时仅拦 None(用于允许空串的场景)。
    val = data.get(key)
    missing = (val is None) if allow_empty else (val is None or val == '')
    if missing:
        raise InvalidParamError(msg or f"{key} 参数必填")
    return val

def get_int(data, key, lo=None, hi=None, required=True, msg=None):
    # 取整数参数,可选必填校验、类型转换与 [lo, hi] 范围钳制
    val = data.get(key)
    if val is None:
        if required:
            raise InvalidParamError(msg or f"{key} 参数必填")
        return None
    try:
        val = int(val)
    except (ValueError, TypeError):
        raise InvalidParamError(msg or f"{key} 必须为有效整数")
    if lo is not None:
        val = max(lo, val)
    if hi is not None:
        val = min(hi, val)
    return val

def get_float(data, key, lo=None, hi=None, required=True, msg=None):
    # 取浮点参数,可选必填校验、类型转换与 [lo, hi] 范围钳制
    val = data.get(key)
    if val is None:
        if required:
            raise InvalidParamError(msg or f"{key} 参数必填")
        return None
    try:
        val = float(val)
    except (ValueError, TypeError):
        raise InvalidParamError(msg or f"{key} 必须为有效数字")
    if lo is not None:
        val = max(lo, val)
    if hi is not None:
        val = min(hi, val)
    return val
