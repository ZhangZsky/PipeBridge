# 系统路径与命令常量集中定义
# 避免在业务代码中硬编码路径和命令名

# ── 系统路径 ──────────────────────────────────────────────

SYS_DRM = '/sys/class/drm'
SYS_VIDEO4LINUX = '/sys/class/video4linux'
PROC_ASOUND = '/proc/asound'
PROC_ASOUND_CARDS = '/proc/asound/cards'
RUN_USER_PREFIX = '/run/user'
VAR_RUN_DBUS = '/var/run/dbus/system_bus_socket'
SYSFS_BLUETOOTH = '/sys/class/bluetooth'
SOUNDS_DIR = '/usr/share/sounds/alsa'
FALLBACK_SOUND = '/usr/share/sounds/freedesktop/stereo/message.oga'

# ── WirePlumber 配置目录 ─────────────────────────────────

WP_SYSTEM_CONF_DIR = '/etc/wireplumber/wireplumber.conf.d'
WP_SYSTEM_LUA_DIR = '/etc/wireplumber/main.lua.d'
WP_USER_CONF_SUBDIR = '.config/wireplumber/wireplumber.conf.d'
WP_USER_LUA_SUBDIR = '.config/wireplumber/main.lua.d'
WP_STATE_DIR = '.local/state/wireplumber'

# ── 命令名称（仅二进制名，非完整路径）────────────────────

CMD_WPCTL = 'wpctl'
CMD_PW_DUMP = 'pw-dump'
CMD_PW_CLI = 'pw-cli'
CMD_PW_PLAY = 'pw-play'
CMD_PW_METADATA = 'pw-metadata'
CMD_PACTL = 'pactl'
CMD_V4L2_CTL = 'v4l2-ctl'
CMD_XRANDR = 'xrandr'
CMD_MODETEST = 'modetest'
CMD_APLAY = 'aplay'
CMD_ARECORD = 'arecord'
CMD_SPEAKER_TEST = 'speaker-test'
CMD_BLUETOOTHCTL = 'bluetoothctl'
CMD_HCICONFIG = 'hciconfig'
CMD_HCITOOL = 'hcitool'
CMD_LSUSB = 'lsusb'
CMD_BEEP = 'beep'
CMD_SYSTEMCTL = 'systemctl'
