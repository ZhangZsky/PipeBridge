# PipeBridge

> 基于 PipeWire / BlueZ 的 fnOS 多媒体硬件管理中间层

PipeBridge 是一个运行在飞牛 fnOS 平台上的系统级应用，为 NAS 用户提供蓝牙、音频、视频设备的图形化管理能力。后端采用 FastAPI 常驻监听 Unix Socket，通过 SSE 实时推送设备事件，前端为嵌入式 Web UI。

## 功能概览

### 蓝牙管理

- 扫描、配对、连接、断开、删除设备
- 信任 / 阻塞设备管理
- 设备别名重命名（本机别名 + 远端设备别名）
- 电源控制与可发现 / 可配对模式切换
- 配对代理（Agent）交互：PIN / PassKey 请求与自动应答
- HFP/HSP（麦克风） ↔ A2DP（纯音频）Profile 切换
- OBEX 文件收发（发送队列、接收监听、接收 Agent 自修复）
- 蓝牙网络共享（NAP Tethering）与服务端广播 / 入站连接查看
- 自动重连（支持手动断开识别与不可恢复错误冷却）
- AVRCP 媒体键桥接（播放/暂停/停止状态同步、绝对音量同步）
- 蓝牙适配器三级故障复位（软复位 → USB 重枚举 → 模块重载）
- 多设备并发连接序列化（避免控制器 socket 竞争）

### 音频管理

- 输出设备（Sink）与输入设备（Source）枚举与详情
- 默认设备设置 / 取消（完全由用户手动掌控）
- 音量控制、静音切换、声道平衡
- 端口（Port）与 Profile 切换
- USB 声卡热插拔自动识别
- 播放测试音验证（含蓝牙预热与 drain 排空防截断）
- 多设备同时播放（combine-sink 虚拟合成输出）
- 按 Stream 路由（pw-metadata target.object 独立路由）
- 实时电平（peak）采集
- 音量刻度正确处理（wpctl cubic scale / pw-cli raw 值区分）

### 视频管理

- DRM 多显示器输出枚举与详情
- 显示器布局、旋转、缩放
- EDID 解析（厂商、产品 ID、物理尺寸、显示器名称）
- 默认视频输出设备设置 / 取消
- 视频流路由查询与管理

### 系统管理

- 依赖一键检测与修复（系统包 / 服务 / 命令完整性）
- PipeWire / WirePlumber / pipewire-pulse 服务启动、停止与状态检测
- 蓝牙 / D-Bus 服务重启
- WirePlumber 配置规则部署（防挂起、IEC958、BlueZ 自动默认关闭）
- 主板蜂鸣器（pcspkr）driver\_override 物理拦截，避免抢占默认输出
- 运行日志在线查看与导出
- 系统概览面板（音频 / 视频 / 蓝牙 / 依赖并行采集）

## 技术架构

```
┌─────────────────────────────────────────────┐
│                   前端 Web UI                │
│            (app/ui/ · 嵌入式单页应用)         │
└──────────────────┬──────────────────────────┘
                   │ HTTP API + SSE
┌──────────────────┴──────────────────────────┐
│              FastAPI 后端 (app.py)           │
│         监听 Unix Socket (app.sock)          │
├──────────┬──────────┬──────────┬────────────┤
│ Bluetooth│  Audio   │  Video   │  System    │
│ Router   │  Router  │  Router  │  Router    │
├──────────┼──────────┼──────────┼────────────┤
│bluetooth_│ audio_   │ video_   │ system_    │
│manager   │ manager  │ manager  │ manager    │
├──────────┴──────────┴──────────┴────────────┤
│              event_system (SSE 事件总线)      │
│              pw_mon_listener (实时监听)       │
├─────────────────────────────────────────────┤
│         PipeWire / WirePlumber / BlueZ       │
│              (D-Bus 系统服务)                 │
└─────────────────────────────────────────────┘
```

### 目录结构

```
PipeBridge/
├── app/                        # 后端应用
│   ├── app.py                  # FastAPI 入口，生命周期与中间件
│   ├── config.py               # 配置文件读写（持久化用户设置）
│   ├── lifecycle.py            # 启动自检与修复、信号处理
│   ├── event_system.py         # SSE 事件总线（订阅/发布/早期缓冲）
│   ├── pw_mon_listener.py      # PipeWire 实时事件监听（pw-dump -m）
│   ├── exceptions.py           # 统一异常体系
│   ├── platform_paths.py       # 系统路径与命令常量
│   ├── utils.py                # PipeWire 操作工具函数
│   ├── audio_helpers.py        # 音量控制辅助（cubic/线性刻度转换）
│   ├── audio_manager.py        # 音频设备管理
│   ├── video_manager.py        # 视频输出设备管理
│   ├── bluetooth_manager.py    # 蓝牙核心管理（BlueZ D-Bus）
│   ├── bluetooth_agent.py      # 配对/OBEX D-Bus Agent（dbus 缺失可降级）
│   ├── bluetooth_extras.py     # 自动重连与 OBEX 文件收发
│   ├── bluetooth_advanced.py   # 蓝牙进阶（别名/广播/网络共享）
│   ├── bt_audio_profiles.py    # 蓝牙音频 Profile 协商
│   ├── avrcp_bridge.py         # AVRCP 媒体键与音量桥接
│   ├── route_manager.py        # PipeWire 端口/链接路由管理
│   ├── system_manager.py       # 系统依赖检测与 WirePlumber 配置
│   ├── routes/                 # API 路由
│   │   ├── bluetooth.py        # /api/bluetooth/*
│   │   ├── audio.py            # /api/audio/*
│   │   ├── video.py            # /api/video/*
│   │   ├── system.py           # /api/system/*
│   │   ├── events.py           # /api/events (SSE)
│   │   └── helpers.py          # 统一响应契约与参数校验
│   └── ui/                     # 前端静态资源
│       ├── index.html          # 单页入口
│       ├── config              # fnOS 桌面入口声明（网关前缀/Socket）
│       ├── css/                # 样式
│       ├── js/                 # 前端逻辑（core/render/updates/各模块）
│       └── images/             # 图标资源
├── cmd/                        # fnOS 应用生命周期脚本
│   ├── main                    # 启动/停止/状态检查
│   ├── install_init            # 安装初始化（系统依赖安装）
│   ├── install_callback        # 安装回调
│   ├── uninstall_init          # 卸载初始化（清理进程与配置）
│   ├── uninstall_callback      # 卸载回调
│   ├── upgrade_init            # 升级初始化
│   ├── upgrade_callback        # 升级回调
│   ├── config_init             # 配置初始化
│   └── config_callback         # 配置回调
├── config/                     # fnOS 应用配置
│   ├── privilege               # 权限声明（root 运行，pipebridge 用户 + audio/bluetooth/video 组）
│   └── resource                # 资源声明（PipeBridge 数据共享目录）
├── wizard/                     # 卸载向导
│   └── uninstall
├── manifest                    # fnOS 应用清单
├── ICON.PNG                    # 应用图标
└── ICON_256.PNG                # 应用图标（高清）
```

## 运行环境

| 项目    | 要求                                                                                |
| ----- | --------------------------------------------------------------------------------- |
| 操作系统  | 飞牛 fnOS ≥ 1.2.0302                                                                |
| 平台    | x86                                                                               |
| 应用版本  | 0.34                                                                              |
| 运行权限  | root（应用主进程与 PipeWire / WirePlumber）                                               |
| 附属用户  | pipebridge（supplementary: audio, bluetooth, video），仅由平台创建用于数据目录/兼容，PipeWire 不以其运行 |
| 单用户约束 | PW 实例全系统唯一：pgrep/pkill 始终按目标 UID(root)过滤，其他用户（如桌面用户）的 PW 进程不会被误检/误杀               |

> 依赖基线：目标机 Python 依赖由系统 apt 提供（Debian 12 → fastapi 0.92.0 / uvicorn 0.17.6 / starlette 0.26.1）。
> `FastAPI(lifespan=...)` 自 0.93 才支持，本项目通过注入 `app.router.lifespan_context` 兼容，勿改回构造参数写法。

### 系统依赖

| 依赖包                  | 说明                       | 关键 |
| -------------------- | ------------------------ | -- |
| pipewire             | PipeWire 音频服务            | 是  |
| pipewire-pulse       | PulseAudio 兼容层           | 是  |
| wireplumber          | 会话管理器                    | 是  |
| libspa-0.2-bluetooth | PipeWire 蓝牙支持            | 是  |
| bluez                | 蓝牙协议栈                    | 是  |
| python3-dbus         | Python D-Bus 绑定          | 是  |
| python3-gi           | PyGObject (GLib)         | 是  |
| python3-fastapi      | Web 框架                   | 是  |
| python3-uvicorn      | ASGI 服务器                 | 是  |
| pipewire-alsa        | ALSA 桥接（speaker-test 依赖） | 否  |
| alsa-utils           | speaker-test 声道测试工具      | 否  |
| bluez-tools          | 蓝牙 CLI 工具                | 否  |
| bluez-firmware       | 蓝牙固件                     | 否  |

依赖检测除包状态外，还会检查命令可用性、PipeWire / WirePlumber / pipewire-pulse 进程运行情况、`libspa-0.2-bluetooth` 插件 `.so` 是否就位以及蓝牙音频整体就绪状态，可在系统页一键修复。

> 缺失 python3-dbus 时应用不会崩溃，蓝牙功能自动降级禁用，其余功能正常。

## API 概览

| 模块 | 路径前缀             | 说明                             |
| -- | ---------------- | ------------------------------ |
| 蓝牙 | `/api/bluetooth` | 扫描、配对、连接、Profile 切换、OBEX 等     |
| 音频 | `/api/audio`     | 设备枚举、默认设备、音量、Profile、播放测试      |
| 视频 | `/api/video`     | 显示器枚举、默认设备、流路由                 |
| 系统 | `/api/system`    | 依赖检测、一键修复、服务启停/重启、日志查看与导出、健康检查 |
| 事件 | `/api/events`    | SSE 实时事件流（30s 心跳）              |

响应契约统一为 `{success, data}`：业务数据一律置于 `data`，异常返回 `{success: false, error, code}`，并按 code 映射 HTTP 状态码（如 `DEVICE_NOT_FOUND` → 404、`INVALID_PARAM` → 400，其余默认 500）。

### SSE 事件类型

- `bluetooth.changed` — 蓝牙设备/适配器状态变化
- `audio.changed` — 音频设备/音量/静音变化
- `video.changed` — 视频设备变化
- `system.changed` — 系统状态变化
- `filetransfer.changed` — OBEX 文件传输进度变化
- `mediakey` — AVRCP 媒体键事件

> SSE 连接数有上限，超限时返回 `event: error`；空闲 30s 发送 `: heartbeat` 注释行保活。

## 网关与部署

PipeBridge 通过 fnOS 统一网关接入，后端监听 Unix Socket（`app.sock`），网关前缀默认为 `/app/PipeBridge`。支持以下部署场景的自适应：

- 网关保留前缀转发
- 网关剥离前缀反代
- 反向代理任意子路径
- 直连访问

前端 `<base>` 由后端注入确定值，无需纯前端猜测。

Socket 权限默认收窄为 `0660`（仅属主与同组可读写），防止本机其他用户绕过网关直连。若网关身份既非 root 也不在该属组导致 502，可临时设置环境变量 `PIPEBRIDGE_SOCKET_MODE=666` 放开后再排查。

### 环境变量

| 变量                       | 默认值               | 说明                                                       |
| ------------------------ | ----------------- | -------------------------------------------------------- |
| `TRIM_APPDEST`           | 应用安装目录            | Socket 与静态资源基准路径                                         |
| `TRIM_GATEWAY_SOCKET`    | `app.sock`        | 网关 Unix Socket 文件名                                       |
| `TRIM_GATEWAY_PREFIX`    | `/app/PipeBridge` | 网关前缀（须与 `app/ui/config` 一致）                              |
| `TRIM_USERNAME`          | `pipebridge`      | 旧版升级残留清理的目标用户（install/uninstall 兼容路径），不再决定 PipeWire 运行用户 |
| `TRIM_PKGVAR`            | —                 | 日志（app.log / install.log）与 PID 文件目录                      |
| `LOG_LEVEL`              | `INFO`            | 日志级别                                                     |
| `PIPEBRIDGE_SOCKET_MODE` | `660`             | Socket 权限（八进制）                                           |

## 构建

本项目为 fnOS 原生应用包，通过 fnOS 应用打包工具构建。无需编译步骤，直接打包目录结构即可。

## 许可证

第三方来源（thirdparty），版权归维护者所有。

## 维护者

- **维护者**: zhangzsky
- **联系方式**: [QQ 群](https://qm.qq.com/q/mVrB8ASTXc)
- **分发者**: 山归山 ([snote.cn](https://snote.cn/))

