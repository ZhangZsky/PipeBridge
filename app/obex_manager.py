"""OBEX 文件传输管理器 —— 使用 obexctl + systemd --user obex 服务"""

import os
import json
import time
import shutil
import logging
import threading
import subprocess
import re

from utils import run_command
import platform_paths
from exceptions import CommandError, DeviceNotFoundError

logger = logging.getLogger('MediaHub')

# 接收文件存放目录
RECEIVE_DIR = os.path.join(os.path.expanduser('~'), 'Downloads', 'bluetooth')
# 发送文件临时目录
SEND_TMP_DIR = '/tmp/mediahub_obex_send'

# 传输状态
TRANSFER_QUEUED = 'queued'
TRANSFER_ACTIVE = 'active'
TRANSFER_COMPLETE = 'complete'
TRANSFER_ERROR = 'error'
TRANSFER_CANCELLED = 'cancelled'

# 内存中的传输记录
_transfers = {}
_transfers_lock = threading.Lock()
_transfer_counter = 0
# 已完成传输记录的最大保留数量，防止无界增长
_MAX_COMPLETED_TRANSFERS = 200

# OBEX 服务状态
_obex_server_running = False
_receive_monitor_thread = None
_receive_known_files = set()


def _ensure_dirs():
    os.makedirs(RECEIVE_DIR, exist_ok=True)
    os.makedirs(SEND_TMP_DIR, exist_ok=True)


def _next_transfer_id():
    global _transfer_counter
    with _transfers_lock:
        _transfer_counter += 1
        return f't{_transfer_counter}'


# 检查 obexctl 是否可用
def _check_obexctl():
    result = run_command('which obexctl 2>/dev/null', timeout=3)
    if not result['success'] or not result['stdout'].strip():
        raise CommandError('obexctl 未安装，请安装 bluez-obexd 包')
    return True


# 确保 OBEX 服务运行
def _ensure_obex_service():
    # 检查 obexd 是否运行
    result = run_command('pgrep -x obexd 2>/dev/null', timeout=3)
    if result['stdout'].strip():
        return True
    # 尝试启动 systemd --user obex 服务
    result = run_command('systemctl --user start obex 2>/dev/null', timeout=5)
    if not result['success']:
        # 回退：直接启动 obexd
        result = run_command('obexd -r /tmp/obex-inbox -r ~/Downloads/bluetooth 2>/dev/null &', timeout=3)
    time.sleep(0.5)
    # 再次检查
    result = run_command('pgrep -x obexd 2>/dev/null', timeout=3)
    return bool(result['stdout'].strip())


# 通过 OBEX OPP 发送文件到蓝牙设备
def send_file(mac, file_path, file_name=None):
    _check_obexctl()
    _ensure_obex_service()

    if not os.path.exists(file_path):
        raise CommandError(f'文件不存在: {file_path}')

    if not file_name:
        file_name = os.path.basename(file_path)

    file_size = os.path.getsize(file_path)
    transfer_id = _next_transfer_id()

    transfer = {
        'id': transfer_id,
        'mac': mac,
        'file_name': file_name,
        'file_size': file_size,
        'direction': 'send',
        'status': TRANSFER_QUEUED,
        'progress': 0,
        'created_at': time.time(),
    }

    with _transfers_lock:
        _transfers[transfer_id] = transfer
        # 自动清理超量的已完成传输记录，防止无界增长
        completed = [tid for tid, t in _transfers.items()
                     if t['status'] in (TRANSFER_COMPLETE, TRANSFER_ERROR, TRANSFER_CANCELLED)]
        if len(completed) > _MAX_COMPLETED_TRANSFERS:
            # 按创建时间排序，移除最旧的
            completed.sort(key=lambda tid: _transfers[tid].get('created_at', 0))
            for tid in completed[:len(completed) - _MAX_COMPLETED_TRANSFERS]:
                del _transfers[tid]

    # 在后台线程中执行发送
    t = threading.Thread(target=_do_send, args=(transfer_id, mac, file_path), daemon=True)
    t.start()

    return transfer


# 在后台线程中执行 obexctl 发送
def _do_send(transfer_id, mac, file_path):
    with _transfers_lock:
        transfer = _transfers.get(transfer_id)
        if not transfer:
            return
        transfer['status'] = TRANSFER_ACTIVE
        transfer['started_at'] = time.time()

    proc = None
    try:
        # 使用 subprocess.Popen 与 obexctl 交互，比 echo 管道更可靠
        proc = subprocess.Popen(
            ['obexctl'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        output_lines = []

        # 读取 obexctl 输出
        def _read_output():
            try:
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        output_lines.append(line)
                        logger.debug(f"obexctl: {line}")
                        # 解析进度
                        m = re.search(r'Transfer\s+(\d+)%', line)
                        if m:
                            pct = int(m.group(1))
                            with _transfers_lock:
                                t = _transfers.get(transfer_id)
                                if t:
                                    t['progress'] = pct
            except Exception:
                pass

        reader = threading.Thread(target=_read_output, daemon=True)
        reader.start()

        # 等待 obexctl 就绪
        time.sleep(0.5)

        # 发送命令序列
        commands = f"connect {mac}\n"
        proc.stdin.write(commands)
        proc.stdin.flush()
        time.sleep(2)  # 等待连接建立

        # 检查连接是否成功
        connect_ok = any('Connection successful' in l or 'Connected: yes' in l for l in output_lines[-10:])
        connect_fail = any('Connection failed' in l or 'Error' in l for l in output_lines[-10:])

        if connect_fail and not connect_ok:
            with _transfers_lock:
                transfer = _transfers.get(transfer_id)
                if transfer:
                    transfer['status'] = TRANSFER_ERROR
                    transfer['error'] = '蓝牙连接失败，设备可能不支持 OBEX 文件传输'
                    transfer['completed_at'] = time.time()
            logger.warning(f"OBEX 连接失败: {mac}")
            try:
                proc.stdin.write("quit\n")
                proc.stdin.flush()
            except Exception:
                pass
            # 确保子进程被回收，防止僵尸进程和 FD 泄露
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            return

        # 发送文件
        send_cmd = f"send {file_path}\n"
        proc.stdin.write(send_cmd)
        proc.stdin.flush()

        # 等待传输完成（最长等待文件大小相关超时）
        file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        timeout = max(60, file_size // 50000 + 30)
        start_wait = time.time()

        while time.time() - start_wait < timeout:
            time.sleep(1)
            with _transfers_lock:
                t = _transfers.get(transfer_id)
                if t and t['status'] == TRANSFER_CANCELLED:
                    break
            # 检查是否传输完成
            recent = output_lines[-5:] if len(output_lines) >= 5 else output_lines
            if any('Transfer successful' in l or 'Transfer complete' in l for l in recent):
                break
            if any('Transfer failed' in l or 'Error' in l for l in recent):
                break

        # 断开连接
        try:
            proc.stdin.write("disconnect\n")
            proc.stdin.flush()
            time.sleep(0.5)
            proc.stdin.write("quit\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

        # 等待进程结束
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()  # 必须回收，防止僵尸进程

        reader.join(timeout=3)

        output = '\n'.join(output_lines)
        with _transfers_lock:
            transfer = _transfers.get(transfer_id)
            if not transfer:
                return

            if transfer['status'] == TRANSFER_CANCELLED:
                pass  # 已由 cancel_transfer 设置
            elif 'Transfer successful' in output or 'Transfer complete' in output:
                transfer['status'] = TRANSFER_COMPLETE
                transfer['progress'] = 100
                transfer['completed_at'] = time.time()
                logger.info(f"OBEX 发送成功: {transfer['file_name']} -> {mac}")
            elif 'Transfer failed' in output:
                transfer['status'] = TRANSFER_ERROR
                transfer['error'] = '传输失败，对方可能拒绝了文件或连接中断'
                transfer['completed_at'] = time.time()
                logger.warning(f"OBEX 传输失败: {transfer['file_name']} -> {mac}")
            elif 'not connected' in output.lower() or 'Connection failed' in output:
                transfer['status'] = TRANSFER_ERROR
                transfer['error'] = '连接失败，设备可能不支持文件传输'
                transfer['completed_at'] = time.time()
                logger.warning(f"OBEX 连接失败: {transfer['file_name']} -> {mac}")
            elif 'No route' in output or 'Host is down' in output:
                transfer['status'] = TRANSFER_ERROR
                transfer['error'] = '设备不可达，请确认设备在线'
                transfer['completed_at'] = time.time()
                logger.warning(f"OBEX 设备不可达: {transfer['file_name']} -> {mac}")
            else:
                # 超时或输出不明确
                transfer['status'] = TRANSFER_ERROR
                transfer['error'] = '传输超时或结果未知'
                transfer['completed_at'] = time.time()
                logger.warning(f"OBEX 传输结果未知: {transfer['file_name']} -> {mac}, 输出: {output[:300]}")

    except Exception as e:
        with _transfers_lock:
            transfer = _transfers.get(transfer_id)
            if transfer:
                transfer['status'] = TRANSFER_ERROR
                transfer['error'] = str(e)[:200]
                transfer['completed_at'] = time.time()
        logger.error(f"OBEX 发送异常: {e}")
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                pass

    finally:
        # 清理临时文件
        try:
            if file_path.startswith(SEND_TMP_DIR):
                os.unlink(file_path)
        except OSError:
            pass


# 取消传输
def cancel_transfer(transfer_id):
    with _transfers_lock:
        transfer = _transfers.get(transfer_id)
        if not transfer:
            raise CommandError(f'传输 {transfer_id} 不存在')
        if transfer['status'] in (TRANSFER_COMPLETE, TRANSFER_ERROR, TRANSFER_CANCELLED):
            raise CommandError(f'传输已结束，无法取消')
        transfer['status'] = TRANSFER_CANCELLED
        transfer['completed_at'] = time.time()
    return {'message': f'传输 {transfer_id} 已取消'}


# 获取所有传输记录
def get_transfers():
    with _transfers_lock:
        return list(_transfers.values())


# 清除已完成的传输记录
def clear_transfers():
    with _transfers_lock:
        to_remove = [tid for tid, t in _transfers.items()
                     if t['status'] in (TRANSFER_COMPLETE, TRANSFER_ERROR, TRANSFER_CANCELLED)]
        for tid in to_remove:
            del _transfers[tid]
    return {'message': f'已清除 {len(to_remove)} 条记录'}


# 后台线程：监控接收目录中的新文件
def _monitor_received_files():
    global _obex_server_running
    _ensure_dirs()
    # 记录当前已有文件
    _receive_known_files.clear()
    try:
        for entry in os.listdir(RECEIVE_DIR):
            full_path = os.path.join(RECEIVE_DIR, entry)
            if os.path.isfile(full_path):
                _receive_known_files.add(entry)
    except OSError:
        pass

    _cycle_count = 0  # 周期计数器，用于定期清理已删除文件
    while _obex_server_running:
        time.sleep(2)
        if not _obex_server_running:
            break
        _cycle_count += 1
        # 每 30 个周期（约 60 秒）清理一次已删除的文件条目，防止集合无限增长
        if _cycle_count % 30 == 0:
            try:
                current_files = set(os.listdir(RECEIVE_DIR))
                _receive_known_files.intersection_update(current_files)
            except OSError:
                pass
        try:
            for entry in os.listdir(RECEIVE_DIR):
                if entry in _receive_known_files:
                    continue
                full_path = os.path.join(RECEIVE_DIR, entry)
                if not os.path.isfile(full_path):
                    continue
                _receive_known_files.add(entry)
                # 新文件到达，创建接收传输记录
                file_size = os.path.getsize(full_path)
                transfer_id = _next_transfer_id()
                transfer = {
                    'id': transfer_id,
                    'mac': '',
                    'file_name': entry,
                    'file_size': file_size,
                    'direction': 'receive',
                    'status': TRANSFER_COMPLETE,
                    'progress': 100,
                    'created_at': time.time(),
                    'completed_at': time.time(),
                }
                with _transfers_lock:
                    _transfers[transfer_id] = transfer
                    # 自动清理超量的已完成传输记录
                    completed = [tid for tid, t in _transfers.items()
                                 if t['status'] in (TRANSFER_COMPLETE, TRANSFER_ERROR, TRANSFER_CANCELLED)]
                    if len(completed) > _MAX_COMPLETED_TRANSFERS:
                        completed.sort(key=lambda tid: _transfers[tid].get('created_at', 0))
                        for tid in completed[:len(completed) - _MAX_COMPLETED_TRANSFERS]:
                            del _transfers[tid]
                logger.info(f"OBEX 接收文件: {entry} ({_format_file_size(file_size)})")
        except OSError:
            pass


def _format_file_size(size):
    if size < 1024:
        return f'{size} B'
    if size < 1048576:
        return f'{size / 1024:.1f} KB'
    return f'{size / 1048576:.1f} MB'


# 启动 OBEX 接收服务
def start_obex_server():
    global _obex_server_running, _receive_monitor_thread
    if _obex_server_running:
        return {'message': '接收服务已在运行', 'receive_dir': RECEIVE_DIR}

    _ensure_dirs()
    obex_ok = _ensure_obex_service()
    if not obex_ok:
        raise CommandError('OBEX 服务启动失败，请确认 bluez-obexd 已安装')

    _obex_server_running = True
    # 启动文件监控线程
    _receive_monitor_thread = threading.Thread(target=_monitor_received_files, daemon=True)
    _receive_monitor_thread.start()
    logger.info("OBEX 接收服务已就绪")
    return {'message': '接收服务已启动', 'receive_dir': RECEIVE_DIR}


# 停止 OBEX 接收服务
def stop_obex_server():
    global _obex_server_running
    _obex_server_running = False
    logger.info("OBEX 接收服务已停止")
    return {'message': '接收服务已停止'}


# 检查 OBEX 接收服务是否运行
def is_obex_server_running():
    return _obex_server_running


# 获取已接收文件列表
def get_received_files():
    _ensure_dirs()
    files = []
    try:
        for entry in os.listdir(RECEIVE_DIR):
            full_path = os.path.join(RECEIVE_DIR, entry)
            if os.path.isfile(full_path):
                stat = os.stat(full_path)
                files.append({
                    'name': entry,
                    'size': stat.st_size,
                    'modified': stat.st_mtime,
                    'path': full_path,
                })
    except OSError:
        pass
    # 按修改时间倒序
    files.sort(key=lambda f: f['modified'], reverse=True)
    return files


# 保存上传的文件到临时目录，返回路径
def save_upload_file(upload_file):
    _ensure_dirs()
    file_name = upload_file.filename or 'unknown'
    # 安全化文件名
    safe_name = ''.join(c for c in file_name if c.isalnum() or c in '._-').strip()
    if not safe_name:
        safe_name = 'upload_file'
    # 避免文件名冲突
    dest = os.path.join(SEND_TMP_DIR, safe_name)
    if os.path.exists(dest):
        name, ext = os.path.splitext(safe_name)
        dest = os.path.join(SEND_TMP_DIR, f'{name}_{int(time.time())}{ext}')

    with open(dest, 'wb') as f:
        shutil.copyfileobj(upload_file.file, f)

    return dest
