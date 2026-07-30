"""PipeWire 实时事件监听器。

通过长驻 `pw-mon` 子进程订阅 PipeWire 事件流，解析节点属性变化
（音量、静音、状态等），并通过 event_bus 实时推送带 payload 的
`audio.changed` 事件，前端可据此做精准增量更新，无需重新拉取全量列表。

设计要点：
1. 子进程通过 `pw-mon -m` 输出 JSON Lines，每行一个事件对象。
2. 仅关注与音频节点 Props 变化相关的 event/type 组合，避免无效推送。
3. 解析失败或子进程退出时自动重启，保证长驻。
4. 通过节流（DEBOUNCE_S）合并连续事件，避免前端被高频事件淹没。
5. 提供 stop() 用于服务关闭时优雅退出。
"""
import json
import logging
import subprocess
import threading
import time
from collections import defaultdict, deque

from utils import _get_pw_env

logger = logging.getLogger('PipeBridge')

# 事件节流：同一设备在 DEBOUNCE_S 内的多次变化合并为一次推送
DEBOUNCE_S = 0.08
# 子进程异常退出后的重启间隔
RESTART_DELAY_S = 1.0
# 缓冲区大小：单行 pw-mon 输出最大长度
_MAX_LINE_LEN = 65536

# 节点媒体类型白名单：只关心音频相关节点
_AUDIO_MEDIA_CLASSES = {'Audio/Sink', 'Audio/Source', 'Audio/Playback', 'Audio/Record'}


class _PwMonListener:
    def __init__(self):
        self._thread = None
        self._proc = None
        self._running = False
        # {node_id: last_pushed_payload_json} 用于变更检测
        self._last_payload = {}
        # {node_id: deque([timestamps])} 用于节流合并
        self._pending = defaultdict(deque)
        self._pending_lock = threading.Lock()
        self._flusher_started = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name='pw-mon-listener')
        self._thread.start()
        logger.info("pw-mon 实时监听器已启动")

    def stop(self):
        self._running = False
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception as e:
                logger.debug(f"停止 pw-mon 子进程失败: {e}")
        self._proc = None

    def _run(self):
        while self._running:
            try:
                self._consume_stream()
            except Exception as e:
                logger.warning(f"pw-mon 监听异常: {e}")
            if not self._running:
                break
            time.sleep(RESTART_DELAY_S)

    def _consume_stream(self):
        env = _get_pw_env()
        try:
            # -m 输出 JSON Lines（每行一个事件对象）
            self._proc = subprocess.Popen(
                ['pw-mon', '-m'],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                text=False,
                bufsize=0,
            )
        except FileNotFoundError:
            logger.error("未找到 pw-mon 命令，无法启动实时监听")
            self._running = False
            return
        except Exception as e:
            logger.error(f"启动 pw-mon 失败: {e}")
            return

        # 启动后台 flusher（合并节流事件）
        if not self._flusher_started:
            ft = threading.Thread(target=self._flush_loop, daemon=True, name='pw-mon-flusher')
            ft.start()
            self._flusher_started = True

        buf = b''
        while self._running:
            chunk = self._proc.stdout.read(4096)
            if not chunk:
                # 子进程结束
                break
            buf += chunk
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                if line and len(line) < _MAX_LINE_LEN:
                    self._handle_line(line)

    def _handle_line(self, raw_line):
        try:
            line = raw_line.decode('utf-8', errors='replace').strip()
        except Exception:
            return
        if not line or not line.startswith('{'):
            return
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(evt, dict):
            return

        # pw-mon -m 输出结构: {"type":"removed|changed|added","id":N,"obj":{
        #   "type":"PipeWire:Interface:Node","info":{"props":{...},"params":{"Props":[...],"EnumFormat":[...]}}}}
        evt_type = evt.get('type')
        if evt_type not in ('changed', 'added', 'removed'):
            return
        obj = evt.get('obj')
        if not isinstance(obj, dict):
            return
        if obj.get('type') != 'PipeWire:Interface:Node':
            return
        node_id = evt.get('id')
        if node_id is None:
            return

        info = obj.get('info', {}) or {}
        props = info.get('props', {}) or {}
        media_class = props.get('media.class', '')
        node_name = props.get('node.name', '')

        # removed 事件：直接通知前端该节点消失
        if evt_type == 'removed':
            self._last_payload.pop(node_id, None)
            self._schedule_push(node_id, node_name, {'removed': True})
            return

        # 仅关注音频节点
        if media_class not in _AUDIO_MEDIA_CLASSES:
            return

        payload = self._extract_payload(info, props, node_id, node_name)
        if payload is None:
            return

        # 与上次推送的 payload 对比，无变化则跳过
        payload_json = json.dumps(payload, sort_keys=True)
        if self._last_payload.get(node_id) == payload_json:
            return
        self._last_payload[node_id] = payload_json
        self._schedule_push(node_id, node_name, payload)

    def _extract_payload(self, info, props, node_id, node_name):
        """从节点 info/props 提取音量相关字段，返回 payload dict 或 None。"""
        params = info.get('params', {}) or {}
        if not isinstance(params, dict):
            params = {}

        # Props 参数中包含 channelVolumes/mute
        props_param_list = params.get('Props', [])
        if isinstance(props_param_list, dict):
            props_param_list = [props_param_list]

        channel_volumes = None
        mute = None
        for pp in props_param_list:
            if not isinstance(pp, dict):
                continue
            cv = pp.get('channelVolumes')
            if cv is not None:
                channel_volumes = cv
            if 'mute' in pp:
                mute = bool(pp['mute'])

        # 如果没有任何音量相关信息，跳过
        if channel_volumes is None and mute is None:
            return None

        # 计算平均音量百分比。
        # 蓝牙(bluez_)启用 hw-volume 时 channelVolumes 为线性刻度，直接使用；
        # 普通设备 channelVolumes 为 cubic 刻度，需开立方还原为线性感知值。
        is_bluez = isinstance(node_name, str) and node_name.startswith('bluez_')
        volume_percent = None
        channels = []
        if channel_volumes and isinstance(channel_volumes, list):
            valid = []
            for cv in channel_volumes:
                try:
                    v = float(cv)
                    if v <= 0:
                        linear = 0.0
                    elif is_bluez:
                        linear = v
                    else:
                        linear = v ** (1.0 / 3.0)
                    valid.append(linear)
                    channels.append(min(round(linear * 100), 100))
                except (TypeError, ValueError):
                    continue
            if valid:
                avg = sum(valid) / len(valid)
                volume_percent = min(round(avg * 100), 100)

        payload = {
            'node_id': node_id,
            'name': node_name,
            'media_class': props.get('media.class', ''),
        }
        if volume_percent is not None:
            payload['volume'] = volume_percent
            payload['channels'] = channels
        if mute is not None:
            payload['muted'] = mute
        return payload

    def _schedule_push(self, node_id, node_name, payload):
        """将事件加入待推送队列，由 flusher 节流合并后批量推送。"""
        with self._pending_lock:
            q = self._pending[node_id]
            q.append((time.time(), node_name, payload))
            # 仅保留最近一条（合并多次变化）
            if len(q) > 1:
                # 保留最早时间戳 + 最新 payload，用于计算节流时机
                first_ts = q[0][0]
                q.clear()
                q.append((first_ts, node_name, payload))

    def _flush_loop(self):
        while self._running:
            time.sleep(DEBOUNCE_S)
            self._flush()

    def _flush(self):
        # 收集所有到期的事件
        now = time.time()
        ready = []
        with self._pending_lock:
            for node_id, q in list(self._pending.items()):
                if not q:
                    continue
                ts, node_name, payload = q[0]
                # 节流窗口已过，或事件已等待超过 200ms（强制推送）
                if now - ts >= DEBOUNCE_S or now - ts >= 0.2:
                    ready.append((node_name, payload))
                    q.clear()
                if not q:
                    del self._pending[node_id]

        if not ready:
            return

        try:
            from event_system import event_bus
            # 推送带 payload 的 audio.changed 事件
            # payload 结构: {'devices': [{name, volume, muted, channels}, ...]}
            event_bus.publish('audio.changed', {'devices': ready})
        except Exception as e:
            logger.debug(f"推送 pw-mon 事件失败: {e}")


pw_mon_listener = _PwMonListener()
