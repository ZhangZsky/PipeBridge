"""MediaBridge 统一异常体系"""


class MediaBridgeError(Exception):
    """异常基类，所有业务异常的父类"""
    code = 'INTERNAL_ERROR'

    # 初始化异常消息和错误码
    def __init__(self, message='', code=None):
        self.message = message
        if code:
            self.code = code
        super().__init__(message)


class DeviceNotFoundError(MediaBridgeError):
    """设备未找到"""
    code = 'DEVICE_NOT_FOUND'


class CommandError(MediaBridgeError):
    """命令执行失败"""
    code = 'COMMAND_ERROR'

    # 初始化命令错误
    def __init__(self, message='', command='', code=None):
        self.command = command
        super().__init__(message, code)


class ConfigError(MediaBridgeError):
    """配置错误"""
    code = 'CONFIG_ERROR'


class InvalidParamError(MediaBridgeError):
    """无效参数"""
    code = 'INVALID_PARAM'


class PairingNeedPinError(InvalidParamError):
    """需要PIN码"""
    code = 'PAIRING_NEED_PIN'

    def __init__(self, message='需要输入PIN码', device_name=None, **kwargs):
        self.device_name = device_name
        super().__init__(message, **kwargs)


class ProfileUnavailableError(InvalidParamError):
    """Profile不可用"""
    code = 'PROFILE_UNAVAILABLE'

    def __init__(self, message='蓝牙音频 profile 不可用', device_name=None, **kwargs):
        self.device_name = device_name
        super().__init__(message, **kwargs)
