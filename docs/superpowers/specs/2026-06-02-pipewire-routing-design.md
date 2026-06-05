# MediaHub PipeWire 原生路由设计

## 目标

让 MediaHub 成为硬件与播放器之间的中间管理层，播放器通过 RESTful API 选择输出设备，无需直接接触硬件层。

## 新增能力

1. **应用级音频路由** — 将指定应用的音频流路由到指定 Sink（含 USB 声卡）
2. **视频输出路由** — 将视频流定向到指定显示器/输出
3. **蓝牙音频输入** — 管理蓝牙麦克风/HFP 的 Source 注册和路由
4. **USB 声卡支持** — USB 音频设备的热插拔检测和路由管理

## 架构

### 新增模块

- `app/route_manager.py` — PipeWire 原生路由核心逻辑

### 增强模块

- `app/bluetooth_manager.py` — 蓝牙音频输入（Source）管理
- `app/audio_manager.py` — 音频流查询、路由 API、USB 声卡支持
- `app/video_manager.py` — 视频流查询、输出路由
- `app/app.py` — 新增路由相关 API 端点

## 核心实现

### route_manager.py

通过 `pw-dump` 获取所有 Node/Link/Client 信息，通过 `pw-cli` 创建/删除 Link 实现路由。

关键函数：
- `get_audio_streams()` — 列出所有活跃音频流（含应用名、当前输出设备、link 状态）
- `route_audio_stream(stream_node_id, target_sink_name)` — 将音频流路由到指定 Sink
- `unlink_stream(stream_node_id, link_id)` — 断开指定路由链接
- `get_video_streams()` — 列出所有活跃视频流
- `route_video_stream(stream_node_id, target_output_name)` — 将视频流路由到指定输出
- `get_bluetooth_audio_sources()` — 列出蓝牙音频输入设备
- `route_bluetooth_source(source_name, target_app)` — 将蓝牙音频输入路由到指定应用

### 路由原理

PipeWire 中每个音频流是一个 `Audio/Playback` Node（output port），每个 Sink 是 `Audio/Sink` Node（input port）。
路由 = 创建 Link 连接 output port → input port。

1. 通过 `pw-dump` 找到流 Node 的 output port 和目标 Sink 的 input port
2. 通过 `pw-cli link <output_port> <input_port>` 创建链接
3. 断开旧链接：`pw-cli unlink <link_id>`

### USB 声卡

USB 声卡在 PipeWire 中自动注册为 ALSA Sink/Source，通过 `device.bus = "usb"` 属性识别。
MediaHub 已支持 USB 音频设备检测（`audio_type = 'usb'`），新增：
- USB 声卡热插拔事件检测
- USB 声卡作为默认设备候选
- USB 声卡的输入（Source）路由

### 蓝牙音频输入

蓝牙 HFP/HSP 连接后，BlueZ 通过 `org.bluez.MediaEndpoint1` 注册音频 endpoint。
PipeWire 的 SPA Bluetooth 插件将其创建为 `Audio/Source` Node。
新增：
- 检测蓝牙 Source 节点
- 蓝牙 Source 到应用的输入路由
- HFP/HSP profile 切换支持

## API 设计

### 音频路由

```
GET  /api/audio/streams              — 列出所有活跃音频流
POST /api/audio/route/stream         — {stream_id, target_device} 将音频流路由到指定设备
DELETE /api/audio/route/stream       — {stream_id, link_id} 断开路由链接
```

### 视频路由

```
GET  /api/video/streams              — 列出所有活跃视频流
POST /api/video/route/stream         — {stream_id, target_device} 将视频流路由到指定输出
```

### 蓝牙音频输入

```
GET  /api/bluetooth/audio-sources    — 列出蓝牙音频输入设备
POST /api/bluetooth/audio-source/route — {source_name, target_app} 路由蓝牙输入到应用
```

## 数据流

```
播放器 → HTTP API → MediaHub → pw-cli link → PipeWire → 硬件输出
                                    ↑
                              pw-dump (状态查询)
```
