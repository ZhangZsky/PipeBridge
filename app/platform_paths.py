# 系统路径与命令常量集中定义，避免硬编码
# 所有音频操作统一使用 PipeWire/WirePlumber，不依赖 ALSA/PulseAudio 工具

# 系统路径

SYS_DRM = '/sys/class/drm'
SYS_VIDEO4LINUX = '/sys/class/video4linux'
RUN_USER_PREFIX = '/run/user'
VAR_RUN_DBUS = '/var/run/dbus/system_bus_socket'
SYSFS_BLUETOOTH = '/sys/class/bluetooth'
FALLBACK_SOUND = '/usr/share/sounds/freedesktop/stereo/message.oga'

# ── WirePlumber 配置目录 ─────────────────────────────────

WP_SYSTEM_CONF_DIR = '/etc/wireplumber/wireplumber.conf.d'
WP_USER_CONF_SUBDIR = '.config/wireplumber/wireplumber.conf.d'
WP_STATE_DIR = '.local/state/wireplumber'

# 命令名称（仅二进制名，非完整路径）

CMD_WPCTL = 'wpctl'
CMD_PW_DUMP = 'pw-dump'
CMD_PW_CLI = 'pw-cli'
CMD_PW_PLAY = 'pw-play'
CMD_V4L2_CTL = 'v4l2-ctl'
CMD_XRANDR = 'xrandr'
CMD_MODETEST = 'modetest'
CMD_BLUETOOTHCTL = 'bluetoothctl'
CMD_HCICONFIG = 'hciconfig'
CMD_HCITOOL = 'hcitool'
CMD_LSUSB = 'lsusb'
CMD_SYSTEMCTL = 'systemctl'
CMD_SPEAKER_TEST = 'speaker-test'

# ── 音频测试资源路径 ─────────────────────────────────

SOUNDS_DIR = '/usr/share/sounds/alsa'
