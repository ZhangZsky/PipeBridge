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
