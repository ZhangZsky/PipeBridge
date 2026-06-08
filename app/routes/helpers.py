import re
from fastapi.responses import JSONResponse
from exceptions import InvalidParamError

MAC_PATTERN = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$')


def _json(result, **extra):
    if isinstance(result, dict) and 'success' in result:
        content = result
    else:
        content = {'success': True, 'data': result}
    content.update(extra)
    return JSONResponse(content=content)


def _validate_mac(mac):
    if not mac or not MAC_PATTERN.match(mac):
        raise InvalidParamError("需要有效的 MAC 地址")
