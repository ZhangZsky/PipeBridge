"""后端变化检测线程 — 周期性轮询系统状态，变化时通过 EventBus 推送 SSE 事件"""

import time
import logging
import threading

from event_bus import event_bus

logger = logging.getLogger('MediaBridge')

# 各类检测的间隔（秒）
_CHECK_INTERVALS = {
    'audio': 2,
    'bluetooth': 3,
    'video': 5,
}


class EventDetector:
    """后台线程：周期性检测音频/蓝牙/视频设备变化，变化时发布 SSE 事件"""

    def __init__(self):
        self._thread = None
        self._running = False
        self._snapshots = {}
        self._no_bt_hardware = False  # 无蓝牙硬件标记，避免反复尝试
        self._bt_hw_check_done = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name='event-detector')
        self._thread.start()
        logger.info("事件检测器已启动")

    def stop(self):
        self._running = False

    def _run(self):
        last_check = {}
        while self._running:
            now = time.time()
            for check_type, interval in _CHECK_INTERVALS.items():
                if now - last_check.get(check_type, 0) >= interval:
                    try:
                        getattr(self, f'_check_{check_type}')()
                    except Exception as e:
                        logger.debug(f"事件检测 {check_type} 异常: {e}")
                    last_check[check_type] = now
            time.sleep(1)

    def _check_audio(self):
        from audio_manager import get_audio_devices
        result = get_audio_devices()
        devices = result.get('devices', [])
        snapshot = ';'.join(sorted(
            f"{d.get('name', '')}|{d.get('state', '')}|{d.get('is_default', '')}|"
            f"{d.get('volume', 0)}|{d.get('muted', '')}"
            for d in devices
        ))
        if snapshot != self._snapshots.get('audio'):
            self._snapshots['audio'] = snapshot
            event_bus.publish('audio.changed')

    def _check_bluetooth(self):
        from bluetooth_manager import get_paired_devices
        # 无硬件时跳过轮询，避免反复尝试启动服务
        if self._no_bt_hardware:
            return
        # 首次检测蓝牙硬件状态，无硬件则标记并跳过后续检查
        if not self._bt_hw_check_done:
            try:
                from bluetooth_manager import get_all_controllers, check_bluetooth_hardware
                controllers = get_all_controllers()
                usb_devices = check_bluetooth_hardware()
                if not controllers and not usb_devices:
                    self._no_bt_hardware = True
                    self._bt_hw_check_done = True
                    logger.info("未检测到蓝牙硬件，跳过蓝牙事件检测")
                    return
            except Exception:
                pass
            self._bt_hw_check_done = True
        devices = get_paired_devices()
        snapshot = ';'.join(sorted(
            f"{d.get('mac', '')}|{d.get('connected', '')}"
            for d in devices
        ))
        if snapshot != self._snapshots.get('bluetooth'):
            self._snapshots['bluetooth'] = snapshot
            event_bus.publish('bluetooth.changed')

    def _check_video(self):
        # 强制扫描以检测热插拔变化
        from video_manager import scan_video_devices
        result = scan_video_devices(force=True)
        devices = result.get('devices', [])
        snapshot = f"{len(devices)}|"
        for d in devices:
            snapshot += f"{d.get('name', '')}|{d.get('width', 0)}x{d.get('height', 0)}@{d.get('fps', 0)}|{d.get('is_default', '')};"
        if snapshot != self._snapshots.get('video'):
            self._snapshots['video'] = snapshot
            event_bus.publish('video.changed')


# 全局单例
event_detector = EventDetector()
