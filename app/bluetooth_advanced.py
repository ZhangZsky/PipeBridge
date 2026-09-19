# 蓝牙进阶能力：蓝牙共享网络(tethering)
# 设计原则：能力探测+友好降级(缺 root/bnep/网桥工具时返回 available=False+reason 供前端禁用)；底层适配器能力复用 bluetooth_manager
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


def _publish_changed():
    try:
        from event_system import event_bus
        event_bus.publish('bluetooth.changed', {})
    except Exception as e:
        logger.debug(f"发布蓝牙变更事件失败: {e}")


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
