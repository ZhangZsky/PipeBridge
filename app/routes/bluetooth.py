import logging
from fastapi import APIRouter, Body, UploadFile, File
import bluetooth_manager
import obex_manager
from exceptions import InvalidParamError, PairingNeedPinError, DeviceNotFoundError
from routes.helpers import _json, _validate_mac, _as_bool
from event_bus import event_bus

logger = logging.getLogger('MediaBridge')

router = APIRouter(prefix="/api/bluetooth", tags=["bluetooth"])


@router.get('/status')
def bluetooth_status():
    logger.debug("获取蓝牙状态")
    result = bluetooth_manager.get_bluetooth_status()
    logger.debug(f"蓝牙状态: {result.get('status', 'unknown')}")
    return _json(result)


@router.post('/install')
def bluetooth_install():
    logger.debug("安装蓝牙驱动")
    result = bluetooth_manager.install_bluetooth_driver()
    logger.debug(f"蓝牙驱动安装: {result}")
    return _json(result)


@router.get('/scan')
def bluetooth_scan():
    logger.debug("扫描蓝牙设备")
    result = bluetooth_manager.scan_devices()
    device_count = len(result) if result else 0
    logger.debug(f"扫描完成，发现 {device_count} 个设备")
    return _json(result)


@router.get('/devices')
def bluetooth_devices():
    return _json(bluetooth_manager.get_paired_devices())


# 获取单个蓝牙设备详情
@router.get('/devices/{mac}')
def bluetooth_device_detail(mac: str):
    mac = _validate_mac(mac)
    devices = bluetooth_manager.get_paired_devices()
    for dev in devices:
        if dev.get('mac', '').upper() == mac.upper():
            return _json(dev)
    raise DeviceNotFoundError(f'蓝牙设备 {mac} 未找到')


@router.post('/pair')
def bluetooth_pair(data: dict = Body(...)):
    mac = data.get('mac')
    _validate_mac(mac)
    logger.debug(f"配对蓝牙设备: {mac}")
    try:
        result = bluetooth_manager.pair_device(mac, pin=data.get('pin'))
    except PairingNeedPinError as e:
        device_name = e.device_name or mac
        logger.debug(f"设备 {mac} 需要PIN码")
        return _json({'success': False, 'data': None}, needs_pin=True, device_name=device_name)
    logger.debug(f"配对结果: 成功, 连接: {result.get('connected', False)}")
    event_bus.publish('bluetooth.changed', {})  # 操作成功后推送 SSE 事件
    return _json(result, connected=result.get("connected", False), device_name=result.get("device_name", mac))


@router.post('/connect')
def bluetooth_connect(data: dict = Body(...)):
    mac = data.get('mac')
    _validate_mac(mac)
    logger.debug(f"连接蓝牙设备: {mac}")
    result = bluetooth_manager.connect_device(mac)
    logger.debug(f"连接结果: 成功")
    event_bus.publish('bluetooth.changed', {})  # 操作成功后推送 SSE 事件
    return _json(result)


@router.post('/disconnect')
def bluetooth_disconnect(data: dict = Body(...)):
    mac = data.get('mac')
    _validate_mac(mac)
    logger.debug(f"断开蓝牙设备: {mac}")
    result = bluetooth_manager.disconnect_device(mac)
    event_bus.publish('bluetooth.changed', {})  # 操作成功后推送 SSE 事件
    return _json(result)


@router.post('/remove')
def bluetooth_remove(data: dict = Body(...)):
    mac = data.get('mac')
    _validate_mac(mac)
    logger.debug(f"删除蓝牙设备: {mac}")
    result = bluetooth_manager.remove_device(mac)
    event_bus.publish('bluetooth.changed', {})  # 操作成功后推送 SSE 事件
    return _json(result)


@router.post('/power')
def bluetooth_power(data: dict = Body(...)):
    power = data.get('power')
    if power is None:
        raise InvalidParamError("电源状态参数必填")
    result = bluetooth_manager.set_power(_as_bool(power))
    event_bus.publish('bluetooth.changed', {})  # 操作成功后推送 SSE 事件
    return _json(result)


@router.post('/discoverable')
def bluetooth_discoverable(data: dict = Body(...)):
    discoverable = data.get('discoverable')
    if discoverable is None:
        raise InvalidParamError("可发现状态参数必填")
    result = bluetooth_manager.set_discoverable(_as_bool(discoverable))
    event_bus.publish('bluetooth.changed', {})  # 操作成功后推送 SSE 事件
    return _json(result)


@router.post('/pairable')
# 设置蓝牙可配对模式
def bluetooth_pairable(data: dict = Body(...)):
    pairable = data.get('pairable')
    if pairable is None:
        raise InvalidParamError("可配对状态参数必填")
    result = bluetooth_manager.set_pairable(_as_bool(pairable))
    event_bus.publish('bluetooth.changed', {})  # 操作成功后推送 SSE 事件
    return _json(result)


@router.post('/discoverable-timeout')
# 设置蓝牙可发现超时
def bluetooth_discoverable_timeout(data: dict = Body(...)):
    timeout = data.get('timeout')
    if timeout is None:
        raise InvalidParamError("超时参数必填")
    try:
        timeout = int(timeout)
    except (ValueError, TypeError):
        raise InvalidParamError("超时必须为整数")
    return _json(bluetooth_manager.set_discoverable_timeout(timeout))


@router.post('/alias')
def bluetooth_alias(data: dict = Body(...)):
    mac, alias = data.get('mac'), data.get('alias')
    if not mac or not alias:
        raise InvalidParamError("MAC 地址和别名不能为空")
    _validate_mac(mac)
    result = bluetooth_manager.set_device_alias(mac, alias)
    event_bus.publish('bluetooth.changed', {})  # 操作成功后推送 SSE 事件
    return _json(result)


@router.post('/trust')
# 设置蓝牙设备信任状态
def bluetooth_trust(data: dict = Body(...)):
    mac = data.get('mac')
    trusted = data.get('trusted')
    if not mac:
        raise InvalidParamError("MAC 地址必填")
    if trusted is None:
        raise InvalidParamError("信任状态参数必填")
    _validate_mac(mac)
    logger.debug(f"设置设备信任: {mac} -> {trusted}")
    result = bluetooth_manager.set_device_trusted(mac, _as_bool(trusted))
    event_bus.publish('bluetooth.changed', {})  # 操作成功后推送 SSE 事件
    return _json(result)


@router.post('/block')
# 设置蓝牙设备阻塞状态
def bluetooth_block(data: dict = Body(...)):
    mac = data.get('mac')
    blocked = data.get('blocked')
    if not mac:
        raise InvalidParamError("MAC 地址必填")
    if blocked is None:
        raise InvalidParamError("阻塞状态参数必填")
    _validate_mac(mac)
    logger.debug(f"设置设备阻塞: {mac} -> {blocked}")
    result = bluetooth_manager.set_device_blocked(mac, _as_bool(blocked))
    event_bus.publish('bluetooth.changed', {})  # 操作成功后推送 SSE 事件
    return _json(result)


@router.post('/keep-alive')
def bluetooth_keep_alive():
    bluetooth_manager.keep_bluetooth_alive()
    connected = bluetooth_manager.check_bluetooth_connections()
    return _json({"connected": connected})


@router.get('/reconnect/status')
def bluetooth_reconnect_status():
    try:
        return _json(bluetooth_manager.get_reconnect_status())
    except Exception as e:
        logger.warning(f"获取重连状态失败: {e}")
        return _json({"monitoring": False, "reconnecting_devices": [], "manual_disconnects": []})


@router.post('/reconnect')
def bluetooth_reconnect(data: dict = Body(...)):
    # 设置自动重连开关
    enabled = data.get('enabled')
    if enabled is None:
        raise InvalidParamError("enabled 参数必填")
    enabled = _as_bool(enabled)
    logger.debug(f"设置自动重连: {enabled}")
    bluetooth_manager.set_reconnect_enabled(enabled)
    return _json(bluetooth_manager.get_reconnect_status())


@router.get('/audio-sources')
# 列出蓝牙音频输入设备
def bluetooth_audio_sources():
    return _json(bluetooth_manager.get_bluetooth_audio_sources())


@router.get('/audio-profiles/{mac}')
# 获取蓝牙设备音频Profile列表
def bluetooth_audio_profiles(mac: str):
    _validate_mac(mac)
    return _json(bluetooth_manager.get_bluetooth_audio_profiles(mac))


@router.post('/audio-profile/switch')
# 切换蓝牙设备音频Profile
def bluetooth_switch_profile(data: dict = Body(...)):
    mac = data.get('mac')
    profile = data.get('profile')
    if not mac or not profile:
        raise InvalidParamError("MAC 地址和 Profile 参数必填")
    _validate_mac(mac)
    logger.debug(f"切换蓝牙Profile: {mac} -> {profile}")
    result = bluetooth_manager.switch_bluetooth_profile(mac, profile)
    event_bus.publish('bluetooth.changed', {})  # 操作成功后推送 SSE 事件
    return _json(result)


@router.post('/microphone/enable')
# 启用蓝牙麦克风
def bluetooth_enable_microphone(data: dict = Body(...)):
    mac = data.get('mac')
    if not mac:
        raise InvalidParamError("MAC 地址必填")
    _validate_mac(mac)
    logger.debug(f"启用蓝牙麦克风: {mac}")
    result = bluetooth_manager.enable_bluetooth_microphone(mac)
    event_bus.publish('bluetooth.changed', {})  # 操作成功后推送 SSE 事件
    return _json(result)


@router.post('/microphone/disable')
# 禁用蓝牙麦克风（切回A2DP）
def bluetooth_disable_microphone(data: dict = Body(...)):
    mac = data.get('mac')
    if not mac:
        raise InvalidParamError("MAC 地址必填")
    _validate_mac(mac)
    logger.debug(f"禁用蓝牙麦克风: {mac}")
    result = bluetooth_manager.disable_bluetooth_microphone(mac)
    event_bus.publish('bluetooth.changed', {})  # 操作成功后推送 SSE 事件
    return _json(result)


# ==================== 文件传输 ====================

@router.post('/file/send')
# 上传并发送文件到蓝牙设备
async def bluetooth_file_send(mac: str = '', file: UploadFile = File(...)):
    if not mac:
        raise InvalidParamError("MAC 地址必填")
    _validate_mac(mac)
    logger.debug(f"发送文件到蓝牙设备: {mac}, 文件: {file.filename}")
    file_path = obex_manager.save_upload_file(file)
    result = obex_manager.send_file(mac, file_path, file.filename)
    return _json(result)


@router.get('/file/transfers')
def bluetooth_file_transfers():
    return _json(obex_manager.get_transfers())


@router.post('/file/cancel')
# 取消文件传输
def bluetooth_file_cancel(data: dict = Body(...)):
    transfer_id = data.get('transfer_id')
    if not transfer_id:
        raise InvalidParamError("传输 ID 必填")
    return _json(obex_manager.cancel_transfer(transfer_id))


@router.post('/file/clear')
# 清除已完成的传输记录
def bluetooth_file_clear():
    return _json(obex_manager.clear_transfers())


@router.post('/file/receive/start')
# 启动 OBEX 接收服务
def bluetooth_file_receive_start():
    return _json(obex_manager.start_obex_server())


@router.post('/file/receive/stop')
# 停止 OBEX 接收服务
def bluetooth_file_receive_stop():
    return _json(obex_manager.stop_obex_server())


@router.get('/file/receive/status')
def bluetooth_file_receive_status():
    return _json({'running': obex_manager.is_obex_server_running()})


@router.get('/file/received')
# 获取已接收文件列表
def bluetooth_file_received():
    return _json(obex_manager.get_received_files())
