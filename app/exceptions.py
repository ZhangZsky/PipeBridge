class PipeBridgeError(Exception):
    code = 'INTERNAL_ERROR'

    def __init__(self, message='', code=None):
        self.message = message
        if code:
            self.code = code
        super().__init__(message)

class DeviceNotFoundError(PipeBridgeError):
    code = 'DEVICE_NOT_FOUND'

class CommandError(PipeBridgeError):
    code = 'COMMAND_ERROR'

    def __init__(self, message='', command='', code=None):
        self.command = command
        super().__init__(message, code)

class ConfigError(PipeBridgeError):
    code = 'CONFIG_ERROR'

class InvalidParamError(PipeBridgeError):
    code = 'INVALID_PARAM'

class PairingNeedPinError(InvalidParamError):
    code = 'PAIRING_NEED_PIN'

    def __init__(self, message='需要输入PIN码', device_name=None, **kwargs):
        self.device_name = device_name
        super().__init__(message, **kwargs)

class ProfileUnavailableError(InvalidParamError):
    code = 'PROFILE_UNAVAILABLE'

    def __init__(self, message='蓝牙音频 profile 不可用', device_name=None, **kwargs):
        self.device_name = device_name
        super().__init__(message, **kwargs)
