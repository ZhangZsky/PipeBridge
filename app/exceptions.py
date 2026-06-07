"""MediaHub 统一异常体系"""


class MediaHubError(Exception):
    """异常基类，所有业务异常的父类"""
    code = 'INTERNAL_ERROR'

    # 初始化异常消息和错误码
    def __init__(self, message='', code=None):
        self.message = message
        if code:
            self.code = code
        super().__init__(message)


class DeviceNotFoundError(MediaHubError):
    """设备未找到"""
    code = 'DEVICE_NOT_FOUND'


class CommandError(MediaHubError):
    """命令执行失败"""
    code = 'COMMAND_ERROR'

    # 初始化命令错误
    def __init__(self, message='', command='', code=None):
        self.command = command
        super().__init__(message, code)


class ConfigError(MediaHubError):
    """配置错误"""
    code = 'CONFIG_ERROR'


class InvalidParamError(MediaHubError):
    """无效参数"""
    code = 'INVALID_PARAM'
