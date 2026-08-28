# PipeBridge

> 基于 PipeWire / BlueZ 的 fnOS 多媒体硬件管理中间层

PipeBridge 是一个运行在飞牛 fnOS 平台上的系统级应用，为 NAS 用户提供蓝牙、音频、视频设备的图形化管理能力。后端采用 FastAPI 常驻监听 Unix Socket，通过 SSE 实时推送设备事件，前端为嵌入式 Web UI。

## 功能概览

### 蓝牙管理

- 扫描、配对、连接、断开、删除设备
- 信任 / 阻塞设备管理
- 设备别名重命名
- 电源控制与可发现 / 可配对模式切换
- HFP/HSP（麦克风） ↔ A2DP（纯音频）Profile 切换
- OBEX 文件收发
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
- 音量刻度正确处理（wpctl cubic scale / pw-cli raw 值区分）

### 视频管理

- DRM 多显示器输出枚举与详情
- 显示器布局、旋转、缩放
- EDID 解析（厂商、产品 ID、物理尺寸、显示器名称）
- 默认视频输出设备设置 / 取消
- 视频流路由查询与管理

### 系统管理

- 依赖一键检测与修复（系统包 / 服务 / 命令完整性）
- PipeWire / WirePlumber 服务启动与状态检测
- 蓝牙 / D-Bus 服务重启
- WirePlumber 配置规则部署（防挂起、蜂鸣器屏蔽、IEC958、BlueZ 自动默认关闭）
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
│   ├── bluetooth_extras.py     # 自动重连管理器
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
│   │   └── events.py           # /api/events (SSE)
│   └── ui/                     # 前端静态资源
├── cmd/                        # fnOS 应用生命周期脚本
│   ├── main                    # 启动/停止/状态检查
│   ├── install_init/           # 安装初始化
│   ├── install_callback/       # 安装回调
│   ├── uninstall_init/         # 卸载初始化
│   ├── uninstall_callback/     # 卸载回调
│   ├── upgrade_init/           # 升级初始化
│   ├── upgrade_callback/       # 升级回调
│   ├── config_init/            # 配置初始化
│   └── config_callback/        # 配置回调
├── config/                     # fnOS 应用配置
│   ├── privilege               # 权限声明（root 运行，audio/bluetooth/video 组）
│   └── resource                # 资源声明（数据共享目录）
├── wizard/                     # 卸载向导
│   └── uninstall/
├── manifest                    # fnOS 应用清单
├── ICON.png                    # 应用图标
└── ICON_256.png                # 应用图标（高清）
```

## 运行环境

| 项目 | 要求 |
|------|------|
| 操作系统 | 飞牛 fnOS ≥ 1.2.0302 |
| 平台 | x86 |
| 运行权限 | root |
| 运行用户 | pipebridge（supplementary: audio, bluetooth, video） |

### 系统依赖

| 依赖包 | 说明 | 关键 |
|--------|------|------|
| pipewire | PipeWire 音频服务 | 是 |
| pipewire-pulse | PulseAudio 兼容层 | 是 |
| wireplumber | 会话管理器 | 是 |
| libspa-0.2-bluetooth | PipeWire 蓝牙支持 | 是 |
| bluez | 蓝牙协议栈 | 是 |
| python3-dbus | Python D-Bus 绑定 | 是 |
| python3-gi | PyGObject (GLib) | 是 |
| python3-fastapi | Web 框架 | 是 |
| python3-uvicorn | ASGI 服务器 | 是 |
| pipewire-alsa | ALSA 桥接（speaker-test） | 否 |
| alsa-utils | 声道测试工具 | 否 |
| bluez-tools | 蓝牙 CLI 工具 | 否 |
| bluez-firmware | 蓝牙固件 | 否 |

> 缺失 python3-dbus 时应用不会崩溃，蓝牙功能自动降级禁用，其余功能正常。

## API 概览

| 模块 | 路径前缀 | 说明 |
|------|----------|------|
| 蓝牙 | `/api/bluetooth` | 扫描、配对、连接、Profile 切换、OBEX 等 |
| 音频 | `/api/audio` | 设备枚举、默认设备、音量、Profile、播放测试 |
| 视频 | `/api/video` | 显示器枚举、默认设备、流路由 |
| 系统 | `/api/system` | 依赖检测、一键修复、服务重启、重连 |
| 事件 | `/api/events` | SSE 实时事件流（30s 心跳） |

### SSE 事件类型

- `bluetooth.changed` — 蓝牙设备/适配器状态变化
- `audio.changed` — 音频设备/音量/静音变化
- `video.changed` — 视频设备变化
- `system.changed` — 系统状态变化
- `mediakey` — AVRCP 媒体键事件

## 网关与部署

PipeBridge 通过 fnOS 统一网关接入，后端监听 Unix Socket（`app.sock`），网关前缀默认为 `/app/PipeBridge`。支持以下部署场景的自适应：

- 网关保留前缀转发
- 网关剥离前缀反代
- 反向代理任意子路径
- 直连访问

前端 `<base>` 由后端注入确定值，无需纯前端猜测。

## 构建

本项目为 fnOS 原生应用包，通过 fnOS 应用打包工具构建。无需编译步骤，直接打包目录结构即可。

## 许可证

第三方来源（thirdparty），版权归维护者所有。

## 维护者

- **维护者**: zhangzsky
- **联系方式**: [QQ 群](https://qm.qq.com/q/mVrB8ASTXc)
- **分发者**: 山归山 ([snote.cn](https://snote.cn/))