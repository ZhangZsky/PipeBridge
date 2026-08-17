# PipeWire 实时事件监听器：长驻 pw-dump -m 子进程订阅事件流(每次变化输出一个完整 JSON 数组)，解析节点音量/静音/状态变化，节流合并后经 event_bus 推送带 payload 的 audio.changed 供前端增量更新，子进程退出自动重启，alsa 设备音量推送前经 wpctl 复核
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
# 累积缓冲区上限：防止畸形输出导致缓冲无界增长(单个 JSON 数组远小于此值)
_MAX_BUFFER_LEN = 4 * 1024 * 1024

# 节点媒体类型白名单：只关心音频相关节点
_AUDIO_MEDIA_CLASSES = {'Audio/Sink', 'Audio/Source', 'Audio/Playback', 'Audio/Record'}


class _PwMonListener:
    def __init__(self):
        self._thread = None
        self._proc = None
        self._running = False
        # {node_id: last_pushed_payload_json} 用于变更检测
        self._last_payload = {}
        # {node_id: node_name} 记录节点名，移除事件(info:null 无 props)时回填名称供前端定位
        self._last_names = {}
        # {node_id: deque([timestamps])} 用于节流合并
        self._pending = defaultdict(deque)
        self._pending_lock = threading.Lock()
        self._flusher_started = False
        # {device_id: ...} Device→Node 映射：蓝牙经 wpctl 改音量时 WirePlumber 只更新 Device 的 Route(mixer)，
        # Node Props.channelVolumes 未必变，导致 pw-mon 收不到 Node 事件而漏推。
        # 故额外监听 Device Route 变化并回溯其关联 Node 主动复核推送。
        self._node_device = {}                   # {node_id: device_id}
        self._device_nodes = defaultdict(dict)   # {device_id: {node_id: node_name}}
        self._last_route_sig = {}                # {device_id: 上次 Route 音量签名} 去重

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name='pw-mon-listener')
        self._thread.start()
        logger.info("pw-dump 实时监听器已启动")

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
                logger.debug(f"停止 pw-dump 子进程失败: {e}")
        self._proc = None

    def _run(self):
        while self._running:
            try:
                self._consume_stream()
            except Exception as e:
                logger.warning(f"pw-dump 监听异常: {e}")
            if not self._running:
                break
            time.sleep(RESTART_DELAY_S)

    def _consume_stream(self):
        env = _get_pw_env()
        try:
            # -m/--monitor 进入监控模式：首次输出完整快照数组，之后每次变化输出一个完整 JSON 数组(pretty-print 多行)
            self._proc = subprocess.Popen(
                ['pw-dump', '-m'],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                text=False,
                bufsize=0,
            )
        except FileNotFoundError:
            logger.error("未找到 pw-dump 命令，无法启动实时监听")
            self._running = False
            return
        except Exception as e:
            logger.error(f"启动 pw-dump 失败: {e}")
            return

        # 子进程(重)启动后清空去重缓存，避免重启前后音量相同导致首次真实变化被去重跳过
        self._last_payload.clear()
        self._last_names.clear()
        self._node_device.clear()
        self._device_nodes.clear()
        self._last_route_sig.clear()

        # 启动后台 flusher（合并节流事件）
        if not self._flusher_started:
            ft = threading.Thread(target=self._flush_loop, daemon=True, name='pw-mon-flusher')
            ft.start()
            self._flusher_started = True

        # pw-dump -m 输出多行 pretty-print JSON 数组，无法逐行解析：按顶层方括号配对累积一个完整数组后整体解析
        buf = b''
        depth = 0        # 顶层 [ ] 嵌套深度(仅统计括号，字符串内的括号需忽略)
        in_str = False   # 是否处于 JSON 字符串字面量内
        escape = False    # 上一个字符是否为转义符 \
        start_idx = -1    # 当前顶层数组起始下标
        while self._running:
            chunk = self._proc.stdout.read(4096)
            if not chunk:
                # 子进程结束
                break
            buf += chunk
            if len(buf) > _MAX_BUFFER_LEN:
                # 畸形输出兜底：丢弃并重置状态，避免内存无界增长
                logger.warning("pw-dump 累积缓冲超上限，重置解析状态")
                buf = b''
                depth = 0
                in_str = False
                escape = False
                start_idx = -1
                continue
            # 扫描新到达的字节，按括号配对切分出完整的顶层 JSON 数组
            i = 0
            n = len(buf)
            while i < n:
                c = buf[i]
                if in_str:
                    if escape:
                        escape = False
                    elif c == 0x5C:  # 反斜杠 \
                        escape = True
                    elif c == 0x22:  # 引号 "
                        in_str = False
                elif c == 0x22:  # 引号 "
                    in_str = True
                elif c == 0x5B:  # 左方括号 [
                    if depth == 0:
                        start_idx = i
                    depth += 1
                elif c == 0x5D:  # 右方括号 ]
                    if depth > 0:
                        depth -= 1
                        if depth == 0 and start_idx >= 0:
                            self._handle_array(buf[start_idx:i + 1])
                            # 已消费到 i，裁剪缓冲并重置扫描
                            buf = buf[i + 1:]
                            start_idx = -1
                            i = -1
                            n = len(buf)
                i += 1

    def _handle_array(self, raw):
        # 解析一个完整的顶层 JSON 数组(pw-dump -m 每次变化输出的对象列表)，逐个对象处理
        try:
            arr = json.loads(raw.decode('utf-8', errors='replace'))
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(arr, list):
            return
        for obj in arr:
            if isinstance(obj, dict):
                self._handle_object(obj)

    @staticmethod
    def _coerce_id(val):
        # pw-dump 的 id 字段可能是 int 或字符串数字，统一归一化为 int，无法解析返回 None
        if isinstance(val, bool):
            return None
        if isinstance(val, int):
            return val
        if isinstance(val, str) and val.strip().lstrip('-').isdigit():
            try:
                return int(val.strip())
            except ValueError:
                return None
        return None

    def _handle_object(self, obj):
        # pw-dump -m 对象结构: {"id":N,"type":"PipeWire:Interface:Node","info":{"props":{...},"params":{"Props":[...]}}}；移除时 info 为 null
        obj_type = obj.get('type')
        if obj_type == 'PipeWire:Interface:Device':
            self._handle_device(obj)
            return
        if obj_type != 'PipeWire:Interface:Node':
            return
        node_id = obj.get('id')
        if node_id is None:
            return

        info = obj.get('info')
        # info 为 null 表示该节点被移除：直接通知前端该节点消失
        if info is None:
            node_name = self._last_names.pop(node_id, '')
            self._last_payload.pop(node_id, None)
            # 清理 Device→Node 映射
            dev_id = self._node_device.pop(node_id, None)
            if dev_id is not None:
                self._device_nodes.get(dev_id, {}).pop(node_id, None)
            self._schedule_push(node_id, node_name, {'removed': True})
            return
        if not isinstance(info, dict):
            return

        props = info.get('props', {}) or {}
        media_class = props.get('media.class', '')
        node_name = props.get('node.name', '')
        if node_name:
            self._last_names[node_id] = node_name

        # 仅关注音频节点
        if media_class not in _AUDIO_MEDIA_CLASSES:
            return

        # 记录 Node→Device 映射：供 Device Route 音量变化时回溯关联节点(蓝牙经 wpctl 改音量走 Route)
        # pw-dump 中 device.id 可能是 int 或字符串数字，统一归一化为 int，否则映射建立失败导致 Device Route 变化无法回溯 Node → 蓝牙音量不实时刷新
        dev_id = self._coerce_id(props.get('device.id'))
        if dev_id is not None:
            self._node_device[node_id] = dev_id
            self._device_nodes[dev_id][node_id] = node_name

        payload = self._extract_payload(info, props, node_id, node_name)
        if payload is None:
            return

        # 与上次推送的 payload 对比，无变化则跳过
        payload_json = json.dumps(payload, sort_keys=True)
        if self._last_payload.get(node_id) == payload_json:
            return
        self._last_payload[node_id] = payload_json
        # 软上限防护：removed 漏报时避免去重缓存无界增长，超阈值整体重置(下次事件重建)
        if len(self._last_payload) > 256:
            self._last_payload.clear()
            self._last_payload[node_id] = payload_json
        self._schedule_push(node_id, node_name, payload)

    def _handle_device(self, obj):
        # 处理 PipeWire:Interface:Device 的 Route(mixer)音量变化。
        # 蓝牙经外部 wpctl 改音量时，WirePlumber 仅更新 Device 的 Output Route.props.channelVolumes，
        # 关联 Node 的 Props.channelVolumes 未必同步刷新，导致 pw-mon 收不到 Node 事件而漏推。
        # 故在此监听 Device Route 音量变化，去重后回溯其关联蓝牙 Node 主动触发 wpctl 复核推送。
        device_id = self._coerce_id(obj.get('id'))
        if device_id is None:
            return
        info = obj.get('info')
        # Device 移除：清理映射
        if info is None:
            self._last_route_sig.pop(device_id, None)
            for nid in list(self._device_nodes.get(device_id, {})):
                self._node_device.pop(nid, None)
            self._device_nodes.pop(device_id, None)
            return
        if not isinstance(info, dict):
            return
        params = info.get('params', {}) or {}
        if not isinstance(params, dict):
            return
        routes = params.get('Route', [])
        if isinstance(routes, dict):
            routes = [routes]
        # 提取 Output Route 的 channelVolumes 作为签名
        sig = None
        for r in routes:
            if not isinstance(r, dict) or r.get('direction') != 'Output':
                continue
            rprops = r.get('props', {}) or {}
            cv = rprops.get('channelVolumes')
            if cv is not None:
                sig = repr(cv)
                break
        if sig is None:
            return
        # 去重：Route 音量未变则忽略(避免与 Node 事件重复推送)
        if self._last_route_sig.get(device_id) == sig:
            return
        self._last_route_sig[device_id] = sig
        # 回溯关联 Node：仅对蓝牙 Node 主动触发复核推送(alsa 走 Node 事件路径已覆盖，避免重复)
        for nid, nname in list(self._device_nodes.get(device_id, {}).items()):
            if not (isinstance(nname, str) and nname.startswith('bluez_')):
                continue
            payload = {
                'node_id': nid,
                'name': nname,
                'media_class': '',
                'volume': 0,
                'channels': [0],
                '_route_probe': True,   # 占位推送：仅当 wpctl 复核成功才推真实值，失败则丢弃
            }
            # _verify_alsa_volume 会用 wpctl get-volume 覆盖为真实值(已支持蓝牙)
            self._schedule_push(nid, nname, payload)

    def _extract_payload(self, info, props, node_id, node_name):
        # 从节点 info/props 提取音量相关字段，返回 payload dict 或 None
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

        # 计算平均音量百分比：蓝牙(bluez_)启用 hw-volume 时为线性刻度直接用，普通设备为 cubic 刻度需开立方还原
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
        # 将事件加入待推送队列，由 flusher 节流合并后批量推送
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

        # alsa(非蓝牙)设备真实音量存于 Device Route，Node Props.channelVolumes 恒为透传值不可信，推送前用 wpctl get-volume 复核覆盖(放在 flush 阶段降低调用频率)
        cleaned = []
        for _node_name, payload in ready:
            self._verify_alsa_volume(payload)
            # Device Route 触发的占位探针：wpctl 复核失败(仍带 _route_probe)则丢弃，避免误推 0
            if isinstance(payload, dict) and payload.get('_route_probe'):
                continue
            cleaned.append((_node_name, payload))
        ready = cleaned
        if not ready:
            return

        try:
            from event_system import event_bus
            # 推送带 payload 的 audio.changed 事件，payload 结构: {'devices': [{name, volume, muted, channels}, ...]}
            event_bus.publish('audio.changed', {'devices': ready})
        except Exception as e:
            logger.debug(f"推送 pw-dump 事件失败: {e}")

    def _verify_alsa_volume(self, payload):
        # 用 wpctl 复核真实音量并就地覆盖 payload(wpctl 走 WirePlumber mixer-api，与 set-volume 及外部程序 r1.toolbox 同图层)。
        # alsa 设备真实音量存于 Device Route，Node Props.channelVolumes 恒为透传值不可信；
        # 蓝牙设备经外部 wpctl 改音量后 Node Props.channelVolumes 未必同步(可能 stale/跳回 1.0)，故蓝牙同样复核。
        # wpctl 读取失败时保留原值不阻断推送。
        if not isinstance(payload, dict) or payload.get('removed'):
            return
        # 无音量字段(仅 mute 变化等)无需复核
        if 'volume' not in payload:
            return
        node_id = payload.get('node_id')
        if node_id is None:
            return
        try:
            from audio_helpers import volume_controller
            wpctl_pct = volume_controller._wpctl_get_volume(node_id)
        except Exception as e:
            logger.debug(f"pw-dump 复核音量失败: {e}")
            return
        if wpctl_pct is None:
            return
        payload['volume'] = wpctl_pct
        # wpctl 返回聚合音量，各声道同步为该真实值(与 set_volume 回填口径一致)
        if isinstance(payload.get('channels'), list) and payload['channels']:
            payload['channels'] = [wpctl_pct for _ in payload['channels']]
        # 复核成功：清除占位探针标记，允许正常推送
        payload.pop('_route_probe', None)


pw_mon_listener = _PwMonListener()
