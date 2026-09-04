# 蓝牙进阶能力：适配器别名/广播、服务端 Profile、入站设备与蓝牙共享网络(tethering)
# 设计原则：全部能力做能力探测+友好降级(缺 root/bnep/网桥工具时返回 available=False+reason 供前端禁用)；无客户端/服务端全局角色，可发现/可配对/接收文件(OBEX)/网络共享均为可自由组合的独立能力，底层适配器能力复用 bluetooth_manager
import os
import shlex
import logging
import threading

# dbus 为可降级依赖:缺失时容错导入,避免顶层硬 import 崩溃整个应用。
# 运行时函数经 bluetooth_manager._get_system_bus 门卫或上层 try/except 兜底。
try:
    import dbus
    HAS_DBUS = True
except ImportError:
    dbus = None
    HAS_DBUS = False

from utils import run_command
from exceptions import CommandError

logger = logging.getLogger('PipeBridge')

BLUEZ_SERVICE = 'org.bluez'
BLUEZ_IFACE_ADAPTER = 'org.bluez.Adapter1'
BLUEZ_IFACE_DEVICE = 'org.bluez.Device1'
DBUS_PROP_IFACE = 'org.freedesktop.DBus.Properties'


def _publish_changed():
    try:
        from event_system import event_bus
        event_bus.publish('bluetooth.changed', {})
    except Exception as e:
        logger.debug(f"发布蓝牙变更事件失败: {e}")


def get_alias():
    # 读取适配器别名(对外显示的设备名)
    import bluetooth_manager as bm
    adapter_path = bm._find_adapter_path()
    if not adapter_path:
        return ''
    try:
        return str(bm._get_property(BLUEZ_IFACE_ADAPTER, adapter_path, 'Alias'))
    except dbus.exceptions.DBusException:
        return ''


def set_alias(alias):
    # 设置适配器别名
    alias = (alias or '').strip()
    if not alias:
        raise CommandError('设备名不能为空')
    if len(alias) > 64:
        raise CommandError('设备名过长（最多 64 字符）')

    import bluetooth_manager as bm
    adapter_path = bm._find_adapter_path()
    if not adapter_path:
        raise CommandError('未找到蓝牙适配器')
    try:
        bm._set_property(BLUEZ_IFACE_ADAPTER, adapter_path, 'Alias', dbus.String(alias))
    except dbus.exceptions.DBusException as e:
        raise CommandError(f'设置设备名失败: {e}')
    _publish_changed()
    return {'alias': alias, 'message': '设备名已更新'}


def get_advertise():
    # 当前是否可被发现(广播)
    import bluetooth_manager as bm
    adapter_path = bm._find_adapter_path()
    if not adapter_path:
        return False
    try:
        return bool(bm._get_property(BLUEZ_IFACE_ADAPTER, adapter_path, 'Discoverable'))
    except dbus.exceptions.DBusException:
        return False


def set_advertise(enabled):
    # 开关可被发现(广播)
    import bluetooth_manager as bm
    adapter_path = bm._find_adapter_path()
    if not adapter_path:
        raise CommandError('未找到蓝牙适配器')
    try:
        bm._set_property(BLUEZ_IFACE_ADAPTER, adapter_path, 'Powered', dbus.Boolean(True))
        bm._set_property(BLUEZ_IFACE_ADAPTER, adapter_path, 'Discoverable', dbus.Boolean(bool(enabled)))
    except dbus.exceptions.DBusException as e:
        raise CommandError(f'切换广播失败: {e}')
    _publish_changed()
    return {'advertise': bool(enabled), 'message': '广播状态已更新'}


# UUID -> 人类可读 Profile 名称（服务端提供的常见服务）
_UUID_NAMES = {
    '00001105': 'OBEX 对象推送 (OPP)',
    '0000112f': '电话簿访问 (PBAP)',
    '0000110a': '音频源 (A2DP Source)',
    '0000110b': '音频接收 (A2DP Sink)',
    '0000111e': '免提 (HFP)',
    '0000110e': '远程控制 (AVRCP)',
    '00001116': '网络访问点 (NAP)',
    '00001115': '个人局域网 (PANU)',
    '00001112': '网关 (HSP AG)',
}


def get_server_profiles():
    # 列出适配器当前对外提供的服务 Profile(基于 UUIDs 属性解析)
    import bluetooth_manager as bm
    adapter_path = bm._find_adapter_path()
    if not adapter_path:
        return []
    try:
        uuids = bm._get_property(BLUEZ_IFACE_ADAPTER, adapter_path, 'UUIDs')
    except dbus.exceptions.DBusException:
        return []
    profiles = []
    for u in uuids:
        us = str(u).lower()
        short = us.split('-')[0] if '-' in us else us
        name = _UUID_NAMES.get(short)
        if name:
            profiles.append({'uuid': us, 'name': name})
    return profiles


def get_incoming_devices():
    # 列出已连接的入站设备(Connected=yes 的 Device1)
    import bluetooth_manager as bm
    devices = []
    try:
        for path, ifaces in bm._get_managed_objects().items():
            props = ifaces.get(BLUEZ_IFACE_DEVICE)
            if not props:
                continue
            if not bool(props.get('Connected', False)):
                continue
            devices.append({
                'mac': str(props.get('Address', '')),
                'name': str(props.get('Alias') or props.get('Name') or props.get('Address', '')),
                'paired': bool(props.get('Paired', False)),
                'trusted': bool(props.get('Trusted', False)),
            })
    except dbus.exceptions.DBusException as e:
        logger.debug(f"枚举入站设备失败: {e}")
    return devices


# 蓝牙共享网络(tethering/NAP)：通过 BlueZ NetworkServer1 注册 NAP，手机以 PANU 连入后内核 bnep 建 bnepN 接口加入网桥，dnsmasq 派 IP、iptables 做 NAT 出网；依赖 root+bnep 模块+bridge/dnsmasq/iptables，受限 NAS 容器常不可用故操作前先做能力探测

TETHER_BRIDGE = 'pan_pb0'
_tether_lock = threading.Lock()
_tether_state = {
    'active': False,
    'bridge': TETHER_BRIDGE,
    'ip': '',
    'clients': 0,
}


def _has_cmd(name):
    r = run_command(f'which {shlex.quote(name)} 2>/dev/null', timeout=3)
    return bool(r['success'] and r['stdout'].strip())


def _is_root():
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def _bnep_available():
    # bnep 内核模块是否可用(已加载或可加载)
    r = run_command('lsmod 2>/dev/null', timeout=3)
    if r['success'] and 'bnep' in r['stdout']:
        return True
    # 尝试探测模块文件是否存在（不实际加载）
    r2 = run_command('modinfo bnep 2>/dev/null', timeout=3)
    return bool(r2['success'] and r2['stdout'].strip())


def check_tethering_capability():
    # 探测蓝牙共享网络所需能力，返回 {available, reason, missing:[...]}
    missing = []
    if not _is_root():
        missing.append('root 权限')
    for c in ('brctl', 'ip', 'dnsmasq', 'iptables'):
        # brctl 与 ip 二选一即可建桥，这里优先探测 ip
        if c == 'brctl':
            continue
        if not _has_cmd(c):
            missing.append(c)
    if not _bnep_available():
        missing.append('bnep 内核模块')
    available = len(missing) == 0
    reason = '' if available else '缺少：' + '、'.join(missing)
    return {'available': available, 'reason': reason, 'missing': missing}


def get_tethering_status():
    cap = check_tethering_capability()
    with _tether_lock:
        st = dict(_tether_state)
    st['available'] = cap['available']
    st['reason'] = cap['reason']
    st['clientList'] = _list_tether_clients() if st['active'] else []
    st['clients'] = len(st['clientList'])
    return st


def _list_tether_clients():
    # 从网桥上枚举 bnep 从设备对应的连入客户端(尽力而为)
    clients = []
    r = run_command(f'ip link show master {shlex.quote(TETHER_BRIDGE)} 2>/dev/null', timeout=3)
    if not (r['success'] and r['stdout'].strip()):
        return clients
    for line in r['stdout'].splitlines():
        line = line.strip()
        if line.startswith(tuple('0123456789')) and 'bnep' in line:
            # 形如: "3: bnep0: <...>"
            parts = line.split(':')
            if len(parts) >= 2:
                clients.append({'iface': parts[1].strip(), 'mac': '', 'ip': ''})
    return clients


def start_tethering(bridge_ip='192.168.7.1'):
    # 开启蓝牙共享网络，前置能力不足时抛 CommandError(前端已据探测禁用)
    cap = check_tethering_capability()
    if not cap['available']:
        raise CommandError(f'当前环境不支持蓝牙共享网络（{cap["reason"]}）')

    import bluetooth_manager as bm
    adapter_path = bm._find_adapter_path()
    if not adapter_path:
        raise CommandError('未找到蓝牙适配器')

    bridge = TETHER_BRIDGE
    # 1) 建网桥并配 IP
    run_command('modprobe bnep 2>/dev/null', timeout=5)
    run_command(f'ip link add name {bridge} type bridge 2>/dev/null', timeout=5)
    run_command(f'ip addr add {shlex.quote(bridge_ip)}/24 dev {bridge} 2>/dev/null', timeout=5)
    run_command(f'ip link set {bridge} up 2>/dev/null', timeout=5)

    # 2) 注册 BlueZ NAP 服务，桥接到网桥
    try:
        bus = bm._get_system_bus()
        net_server = dbus.Interface(
            bus.get_object(BLUEZ_SERVICE, adapter_path),
            'org.bluez.NetworkServer1'
        )
        net_server.Register('nap', bridge)
    except dbus.exceptions.DBusException as e:
        raise CommandError(f'注册 NAP 服务失败: {e}')

    # 3) dnsmasq 在网桥派发 IP（DHCP）
    lo = bridge_ip.rsplit('.', 1)[0]
    dhcp_range = f'{lo}.10,{lo}.100,12h'
    run_command(
        f'dnsmasq --interface={bridge} --bind-interfaces '
        f'--dhcp-range={dhcp_range} --except-interface=lo '
        f'--pid-file=/tmp/pipebridge_pan_dnsmasq.pid 2>/dev/null &',
        timeout=5
    )
    # 4) 开启转发 + NAT
    run_command('sysctl -w net.ipv4.ip_forward=1 2>/dev/null', timeout=3)
    run_command(f'iptables -t nat -A POSTROUTING -s {lo}.0/24 -j MASQUERADE 2>/dev/null', timeout=5)

    with _tether_lock:
        _tether_state.update({'active': True, 'bridge': bridge, 'ip': bridge_ip})
    _publish_changed()
    return {'active': True, 'bridge': bridge, 'ip': bridge_ip, 'message': '蓝牙共享网络已开启'}


def stop_tethering():
    import bluetooth_manager as bm
    adapter_path = bm._find_adapter_path()
    bridge = TETHER_BRIDGE
    if adapter_path:
        try:
            bus = bm._get_system_bus()
            net_server = dbus.Interface(
                bus.get_object(BLUEZ_SERVICE, adapter_path),
                'org.bluez.NetworkServer1'
            )
            net_server.Unregister('nap')
        except dbus.exceptions.DBusException as e:
            logger.debug(f"取消注册 NAP 网络服务器失败: {e}")

    with _tether_lock:
        lo = (_tether_state.get('ip') or '192.168.7.1').rsplit('.', 1)[0]
    run_command(f'iptables -t nat -D POSTROUTING -s {lo}.0/24 -j MASQUERADE 2>/dev/null', timeout=5)
    run_command('kill "$(cat /tmp/pipebridge_pan_dnsmasq.pid 2>/dev/null)" 2>/dev/null', timeout=3)
    run_command(f'ip link set {bridge} down 2>/dev/null', timeout=5)
    run_command(f'ip link delete {bridge} type bridge 2>/dev/null', timeout=5)

    with _tether_lock:
        _tether_state.update({'active': False, 'ip': '', 'clients': 0})
    _publish_changed()
    return {'active': False, 'message': '蓝牙共享网络已关闭'}
