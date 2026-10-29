-- PipeBridge: 关闭 bluez 监控的 logind 仲裁
-- 原因: 本机为无本地控制台会话的 headless NAS, logind 报告 seat 状态为 "offline",
--       bluez.lua 在 logind 可用时仅按 seat 激活监控, 导致蓝牙音频监控永不启动、
--       pw-dump 中无 api.bluez5.* 节点, PipeBridge 的 bluetooth_audio_ready 恒为 false。
-- 处置: 置 with-logind=false, 令 bluez.lua 走 createMonitor() 直接创建路径。
bluez_monitor = bluez_monitor or {}
bluez_monitor.properties = bluez_monitor.properties or {}
bluez_monitor.properties["with-logind"] = false
