async function fetchBluetoothStatus() {
    try {
        const data = await apiCall('/api/bluetooth/status');
        return data;
    } catch (error) {
        return { data: { status: 'error', controllers: [], usb_devices: [] } };
    }
}

async function togglePower(enabled) {
    setLoading(true, 'power');
    try {
        const result = await apiCall('/api/bluetooth/power', {
            method: 'POST',
            body: JSON.stringify({ power: enabled })
        });
        showToast(safeToastData(result.data, enabled ? '电源已开启' : '电源已关闭'), 'success');
        await updateBluetoothStatus();
    } catch (error) {
        showToast('电源控制失败: ' + error.message, 'error');
        const switchEl = document.getElementById('powerSwitch');
        if (switchEl) switchEl.checked = !enabled;
    } finally {
        setLoading(false);
    }
}

async function toggleDiscoverable(enabled) {
    setLoading(true, 'discoverable');
    try {
        const result = await apiCall('/api/bluetooth/discoverable', {
            method: 'POST',
            body: JSON.stringify({ discoverable: enabled })
        });
        showToast(result.data || (enabled ? '已设为可发现' : '已关闭可发现'), 'success');
        
        if (enabled) {
            const timeoutInput = document.getElementById('discoverableTimeout');
            const timeout = timeoutInput ? parseInt(timeoutInput.value) : 0;
            if (timeout > 0) {
                try {
                    await apiCall('/api/bluetooth/discoverable-timeout', {
                        method: 'POST',
                        body: JSON.stringify({ timeout: timeout })
                    });
                } catch (e) {  }
            }
        }
        await updateBluetoothStatus();
    } catch (error) {
        showToast('可发现设置失败: ' + error.message, 'error');
        const switchEl = document.getElementById('discoverableSwitch');
        if (switchEl) switchEl.checked = !enabled;
    } finally {
        setLoading(false);
    }
}

async function togglePairable(enabled) {
    setLoading(true, 'pairable');
    try {
        const result = await apiCall('/api/bluetooth/pairable', {
            method: 'POST',
            body: JSON.stringify({ pairable: enabled })
        });
        showToast(result.data || (enabled ? '已设为可配对' : '已关闭可配对'), 'success');
        await updateBluetoothStatus();
    } catch (error) {
        showToast('可配对设置失败: ' + error.message, 'error');
        const switchEl = document.getElementById('pairableSwitch');
        if (switchEl) switchEl.checked = !enabled;
    } finally {
        setLoading(false);
    }
}

async function renameDevice(mac) {
    const cards = document.querySelectorAll('.device-card');
    let displayName = '';
    for (const card of cards) {
        const btn = card.querySelector(`[data-action="rename"][data-mac="${CSS.escape(mac)}"]`);
        if (btn) {
            displayName = btn.dataset.name;
            const nameEl = card.querySelector('.device-name');
            if (nameEl) nameEl.style.display = 'none';
            btn.style.display = 'none';
            const existingInput = card.querySelector('.rename-input');
            if (existingInput) existingInput.remove();
            const input = document.createElement('input');
            input.type = 'text';
            input.className = 'rename-input';
            input.value = displayName;
            input.maxLength = 64;
            const infoDiv = card.querySelector('.device-info');
            infoDiv.appendChild(input);
            input.focus();
            input.select();

            let _renameDone = false;  
            const finishRename = async () => {
                if (_renameDone) return;
                _renameDone = true;
                const newName = input.value.trim();
                input.remove();
                if (nameEl) nameEl.style.display = '';
                btn.style.display = '';
                if (!newName || newName === displayName) return;
                try {
                    const result = await apiCall('/api/bluetooth/alias', {
                        method: 'POST',
                        body: JSON.stringify({ mac, alias: newName })
                    });
                    if (result.success) {
                        showToast(`已重命名为 ${newName}`, 'success');
                        btn.dataset.name = newName;
                        if (nameEl) nameEl.textContent = newName;
                        await loadInitialDevices();
                    } else {
                        showToast(result.error || '重命名失败', 'error');
                    }
                } catch (error) {
                    showToast('重命名失败: ' + error.message, 'error');
                }
            };

            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    finishRename();
                } else if (e.key === 'Escape') {
                    _renameDone = true;  
                    input.remove();
                    if (nameEl) nameEl.style.display = '';
                    btn.style.display = '';
                }
            });
            input.addEventListener('blur', finishRename);
            break;
        }
    }
}

async function installDriver() {
    const installBtn = document.getElementById('installDriverBtn');
    const originalText = installBtn ? installBtn.textContent : '';
    if (installBtn) {
        installBtn.disabled = true;
        installBtn.textContent = '安装中...';
    }
    showToast('正在安装蓝牙驱动，请稍候...', 'info');
    try {
        const result = await apiCall('/api/bluetooth/install', { method: 'POST' });
        if (result.success) {
            showToast(result.data || '驱动安装成功', 'success');
        } else {
            showToast(result.error || '驱动安装失败', 'error');
        }
        await updateBluetoothStatus();
    } catch (error) {
        showToast('驱动安装失败: ' + error.message, 'error');
    } finally {
        if (installBtn) {
            installBtn.disabled = false;
            installBtn.textContent = originalText;
        }
    }
}

async function scanDevices() {
    setLoading(true, 'scan');
    showScanningState();
    try {
        const result = await apiCall('/api/bluetooth/scan');
        const devices = result.data || [];
        scannedDevices = devices;
        if (devices.length > 0) {
            showToast(`发现 ${devices.length} 个设备`, 'success');
        } else {
            showToast('未发现设备，请确保附近有蓝牙设备处于可发现模式', 'info');
        }
        await renderBluetoothDevices(devices);
    } catch (error) {
        showToast('扫描失败: ' + error.message, 'error');
        renderBluetoothDevices([]);
    } finally {
        setLoading(false);
        if (_scanTimer) { clearInterval(_scanTimer); _scanTimer = null; }
    }
}

async function getPairedDevices() {
    try {
        const data = await apiCall('/api/bluetooth/devices');
        return data.data || [];
    } catch (error) {
        return [];
    }
}

let currentPairingMac = null;
let currentPairingName = null;

async function pairDevice(mac, pin) {
    if (pin === undefined) {
        setDeviceLoading(mac, true, '配对中...');
        const device = scannedDevices.find(d => d.mac === mac);
        const devName = device?.name || device?.alias || mac;
        currentPairingMac = mac;
        currentPairingName = devName;
        showPairingState(mac, devName);
    }
    try {
        const body = { mac: mac };
        if (pin !== undefined) body.pin = pin;
        const result = await apiCall('/api/bluetooth/pair', {
            method: 'POST',
            body: JSON.stringify(body)
        });
        if (result.success) {
            const name = result.device_name || currentPairingName || mac;
            const msg = (typeof result.data === 'string') ? result.data : (result.data?.data || `设备 ${name} 配对成功`);
            showToast(msg, 'success');
            hidePinDialog();
            setDeviceLoading(mac, false);
            currentPairingMac = null;
            currentPairingName = null;
            scannedDevices = scannedDevices.map(d =>
                d.mac === mac ? { ...d, paired: true, connected: result.connected || false } : d
            );
            await renderBluetoothDevices(scannedDevices);
        } else {
            if (!pin && result.needs_pin) {
                const deviceName = result.device_name || currentPairingName || mac;
                showPinDialog(mac, deviceName);
                return;
            }
            hidePinDialog();
            setDeviceLoading(mac, false);
            currentPairingMac = null;
            currentPairingName = null;
            const errMsg = result.error || '配对失败';
            if (errMsg.includes('控制器不可用') || errMsg.includes('无法上电') || errMsg.includes('未检测到蓝牙')) {
                showToast(errMsg + '，请先点击「安装驱动」', 'warning');
            } else if (errMsg.includes('未找到设备') || errMsg.includes('重新扫描')) {
                showToast(errMsg, 'warning');
            } else {
                showToast(errMsg, 'error');
            }
            await renderBluetoothDevices(scannedDevices);
        }
    } catch (error) {
        hidePinDialog();
        setDeviceLoading(mac, false);
        currentPairingMac = null;
        currentPairingName = null;
        showToast('配对请求出错: ' + error.message, 'error');
        await renderBluetoothDevices(scannedDevices);
    }
}

function showPinDialog(mac, deviceName) {
    currentPairingMac = mac;
    currentPairingName = deviceName;
    document.getElementById('pinDialogDevice').textContent = deviceName || mac;
    document.getElementById('pinInput').value = '';
    document.getElementById('pinDialog').style.display = 'block';
    setLoading(false);
    setTimeout(() => document.getElementById('pinInput').focus(), 100);
}

function hidePinDialog() {
    document.getElementById('pinDialog').style.display = 'none';
}

async function connectDevice(mac, retryCount = 0) {
    setDeviceLoading(mac, true, retryCount > 0 ? '重试中...' : '连接中...');
    try {
        const result = await apiCall('/api/bluetooth/connect', {
            method: 'POST',
            body: JSON.stringify({ mac: mac })
        });
        if (result.success) {
            setDeviceLoading(mac, false);
            if (result.warning) {
                showToast(result.warning, 'warning');
            } else {
                showToast(safeToastData(result.data, '连接成功'), 'success');
            }
            scannedDevices = scannedDevices.map(d =>
                d.mac === mac ? { ...d, connected: true, paired: true } : d
            );
            await renderBluetoothDevices(scannedDevices);
            await renderAudioDevices(true);
        } else {
            const err = result.error || '连接失败';
            if (err.includes('控制器不可用') || err.includes('无法上电') || err.includes('未检测到蓝牙')) {
                showToast(err + '，请先点击「安装驱动」', 'warning');
                setDeviceLoading(mac, false);
            } else if (retryCount === 0 && (err.includes('超时') || err.includes('未找到') || err.includes('连接失败'))) {
                
                logger.info(`连接失败，先扫描再重试: ${mac}, 错误: ${err}`);
                try {
                    
                    await apiCall('/api/bluetooth/scan');
                    await new Promise(resolve => setTimeout(resolve, 1000));
                } catch (scanErr) {
                    logger.warning(`重试前扫描失败: ${scanErr.message}`);
                }
                await connectDevice(mac, 1);
            } else {
                showToast(err, 'error');
                setDeviceLoading(mac, false);
            }
        }
    } catch (error) {
        if (retryCount === 0) {
            
            logger.info(`连接异常，先扫描再重试: ${mac}, 错误: ${error.message}`);
            try {
                await apiCall('/api/bluetooth/scan');
                await new Promise(resolve => setTimeout(resolve, 1000));
            } catch (scanErr) {
                logger.warning(`重试前扫描失败: ${scanErr.message}`);
            }
            await connectDevice(mac, 1);
        } else {
            showToast('连接失败: ' + error.message, 'error');
            setDeviceLoading(mac, false);
        }
    }
}

async function handlePairWithPin() {
    const pin = document.getElementById('pinInput').value.trim();
    if (!pin) {
        showToast('请输入 PIN 码', 'error');
        return;
    }
    const confirmBtn = document.getElementById('pinConfirmBtn');
    if (confirmBtn) confirmBtn.disabled = true;
    if (currentPairingMac) {
        await pairDevice(currentPairingMac, pin);
    }
    if (confirmBtn) confirmBtn.disabled = false;
}

async function disconnectDevice(mac) {
    setDeviceLoading(mac, true, '断开中...');
    try {
        const result = await apiCall('/api/bluetooth/disconnect', {
            method: 'POST',
            body: JSON.stringify({ mac: mac })
        });
        if (result.success) {
            showToast(safeToastData(result.data, '已断开连接'), 'success');
            scannedDevices = scannedDevices.map(d =>
                d.mac === mac ? { ...d, connected: false } : d
            );
            setDeviceLoading(mac, false);
            await renderBluetoothDevices(scannedDevices);
            await renderAudioDevices(true);
        } else {
            showToast(result.error || '断开连接失败', 'error');
            setDeviceLoading(mac, false);
        }
    } catch (error) {
        showToast('断开连接失败: ' + error.message, 'error');
        setDeviceLoading(mac, false);
    }
}

async function removeDevice(mac) {
    if (!confirm('确定要删除此设备吗？')) return;
    setDeviceLoading(mac, true, '删除中...');
    try {
        const result = await apiCall('/api/bluetooth/remove', {
            method: 'POST',
            body: JSON.stringify({ mac: mac })
        });
        if (result.success) {
            showToast(safeToastData(result.data, '设备已删除'), 'success');
            scannedDevices = scannedDevices.filter(d => d.mac !== mac);
        } else {
            showToast(safeToastData(result.error, '删除设备失败'), 'error');
        }
        setDeviceLoading(mac, false);
        await renderBluetoothDevices(scannedDevices);
    } catch (error) {
        showToast('删除设备失败: ' + error.message, 'error');
        setDeviceLoading(mac, false);
    }
}

function handleDeviceAction(event) {
    const btn = event.currentTarget;
    const action = btn.dataset.action;
    const mac = btn.dataset.mac;
    if (isLoading || _loadingDeviceMac) return;
    switch (action) {
        case 'pair': pairDevice(mac); break;
        case 'connect': connectDevice(mac); break;
        case 'disconnect': disconnectDevice(mac); break;
        case 'remove': removeDevice(mac); break;
        case 'rename': renameDevice(mac); break;

        case 'sendFile': openFileSendDialog(mac, btn.dataset.name); break;
    }
}

let _fileSendMac = null;
let _fileSendFile = null;
let _transferPollTimer = null;

function _formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
}

function openFileSendDialog(mac, name) {
    _fileSendMac = mac;
    _fileSendFile = null;
    const dialog = document.getElementById('fileSendDialog');
    document.getElementById('fileSendTarget').textContent = name || mac;
    document.getElementById('fileSendFileInfo').style.display = 'none';
    document.getElementById('fileSendProgress').style.display = 'none';
    document.getElementById('fileSendConfirmBtn').disabled = true;
    document.getElementById('fileSendConfirmBtn').textContent = '发送';
    document.getElementById('fileSendInput').value = '';
    dialog.style.display = '';
}

function _closeFileSendDialog() {
    document.getElementById('fileSendDialog').style.display = 'none';
    _fileSendMac = null;
    _fileSendFile = null;
}

function _onFileSelected(file) {
    _fileSendFile = file;
    document.getElementById('fileSendFileInfo').style.display = '';
    document.getElementById('fileSendName').textContent = file.name;
    document.getElementById('fileSendSize').textContent = _formatFileSize(file.size);
    document.getElementById('fileSendConfirmBtn').disabled = false;
    document.getElementById('fileSendProgress').style.display = 'none';
}

async function _doSendFile() {
    if (!_fileSendMac || !_fileSendFile) return;
    const confirmBtn = document.getElementById('fileSendConfirmBtn');
    confirmBtn.disabled = true;
    confirmBtn.textContent = '发送中...';
    document.getElementById('fileSendProgress').style.display = '';
    document.getElementById('fileSendProgressBar').style.width = '0%';
    document.getElementById('fileSendStatus').textContent = '上传中...';

    try {
        const formData = new FormData();
        formData.append('file', _fileSendFile);

        const xhr = new XMLHttpRequest();
        xhr.open('POST', `${API_BASE}/api/bluetooth/file/send?mac=${encodeURIComponent(_fileSendMac)}`);

        xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) {
                const pct = Math.round((e.loaded / e.total) * 100);
                document.getElementById('fileSendProgressBar').style.width = pct + '%';
                document.getElementById('fileSendStatus').textContent = `上传中 ${pct}%`;
            }
        };

        const result = await new Promise((resolve, reject) => {
            xhr.onload = () => {
                try {
                    const resp = JSON.parse(xhr.responseText);
                    if (xhr.status >= 200 && xhr.status < 300 && resp.success) {
                        resolve(resp);
                    } else {
                        reject(new Error(resp.message || resp.error || '发送失败'));
                    }
                } catch (e) {
                    reject(new Error('服务器响应解析失败'));
                }
            };
            xhr.onerror = () => reject(new Error('网络错误'));
            xhr.ontimeout = () => reject(new Error('请求超时'));
            xhr.timeout = 300000;
            xhr.send(formData);
        });

        document.getElementById('fileSendProgressBar').style.width = '100%';
        document.getElementById('fileSendStatus').textContent = '文件已提交，蓝牙传输中...';
        showToast('文件已提交发送', 'success');
        setTimeout(() => {
            _closeFileSendDialog();
            refreshTransferList();
        }, 1500);
    } catch (err) {
        showToast('发送失败: ' + err.message, 'error');
        document.getElementById('fileSendStatus').textContent = '发送失败: ' + err.message;
        confirmBtn.disabled = false;
        confirmBtn.textContent = '重试';
    }
}

async function refreshTransferList() {
    try {
        const result = await apiCall('/api/bluetooth/file/transfers');
        const transfers = result.data || [];
        const listEl = document.getElementById('fileTransferList');
        if (!listEl) return;

        if (transfers.length === 0) {
            listEl.innerHTML = '<div class="empty-state"><p>暂无文件传输</p></div>';
            stopTransferPoll();
            return;
        }

        listEl.innerHTML = transfers.map(t => {
            const statusMap = {
                queued: { label: '排队中', cls: 'transfer-queued' },
                active: { label: '传输中', cls: 'transfer-active' },
                complete: { label: '已完成', cls: 'transfer-complete' },
                error: { label: '失败', cls: 'transfer-error' },
                cancelled: { label: '已取消', cls: 'transfer-cancelled' }
            };
            const st = statusMap[t.status] || { label: t.status, cls: '' };
            const progress = t.progress != null ? Math.round(t.progress) : (t.status === 'complete' ? 100 : 0);
            const isCancellable = t.status === 'queued' || t.status === 'active';
            const dirIcon = t.direction === 'send'
                ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M22 2L11 13"/><path d="M22 2L15 22L11 13L2 9L22 2Z"/></svg>'
                : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
            return `<div class="transfer-item ${st.cls}">
                <div class="transfer-header">
                    <span class="transfer-icon">${dirIcon}</span>
                    <span class="transfer-filename" title="${t.file_name || ''}">${t.file_name || '未知文件'}</span>
                    <span class="transfer-size">${t.file_size ? _formatFileSize(t.file_size) : ''}</span>
                    <span class="transfer-status-badge ${st.cls}">${st.label}</span>
                    ${isCancellable ? `<button class="btn btn-sm btn-danger" data-transfer-cancel="${t.id}">取消</button>` : ''}
                </div>
                ${t.status === 'active' ? `<div class="transfer-progress-bar"><div class="transfer-progress-fill" style="width:${progress}%"></div></div>` : ''}
                <div class="transfer-meta">
                    <span>${t.direction === 'send' ? '发送到' : '来自'} ${t.device_name || t.device_mac || ''}</span>
                    ${t.error ? `<span class="transfer-error-msg">${t.error}</span>` : ''}
                </div>
            </div>`;
        }).join('');

        listEl.querySelectorAll('[data-transfer-cancel]').forEach(btn => {
            btn.addEventListener('click', async () => {
                try {
                    await apiCall('/api/bluetooth/file/cancel', {
                        method: 'POST',
                        body: JSON.stringify({ transfer_id: btn.dataset.transferCancel })
                    });
                    showToast('已取消传输', 'info');
                    refreshTransferList();
                } catch (e) {
                    showToast('取消失败: ' + e.message, 'error');
                }
            });
        });

        const hasActive = transfers.some(t => t.status === 'queued' || t.status === 'active');
        if (hasActive) {
            const ftList = document.getElementById('fileTransferList');
            const ftReceived = document.getElementById('receivedFilesSection');
            const ftHeader = document.getElementById('fileTransferHeader');
            const ftIcon = ftHeader ? ftHeader.querySelector('.collapse-icon') : null;
            if (ftList) ftList.style.display = '';
            if (ftReceived) ftReceived.style.display = '';
            if (ftIcon) ftIcon.style.transform = '';
        } else stopTransferPoll();
    } catch (e) {
        console.warn('获取传输列表失败:', e);
    }
}

// 文件传输列表已由 SSE filetransfer.changed 事件实时驱动，无需轮询。
function stopTransferPoll() {
    if (_transferPollTimer) {
        clearInterval(_transferPollTimer);
        _transferPollTimer = null;
    }
}

async function loadReceivedFiles() {
    try {
        const result = await apiCall('/api/bluetooth/file/received');
        const files = result.data || [];
        const section = document.getElementById('receivedFilesSection');
        const listEl = document.getElementById('receivedFilesList');
        if (!section || !listEl) return;

        if (files.length === 0) {
            section.style.display = 'none';
            return;
        }
        section.style.display = '';
        listEl.innerHTML = files.map(f => {
            const modTime = f.modified ? new Date(f.modified * 1000).toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}) : '';
            return `
            <div class="received-file-item">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                    <polyline points="14 2 14 8 20 8"/>
                </svg>
                <span class="received-file-name" title="${f.name}">${f.name}</span>
                <span class="received-file-size">${_formatFileSize(f.size)}</span>
                <span class="received-file-time">${modTime}</span>
            </div>`;
        }).join('');
    } catch (e) {
        console.warn('获取已接收文件失败:', e);
    }
}

async function loadObexReceiveStatus() {
    try {
        const result = await apiCall('/api/bluetooth/file/receive/status');
        const running = result.data && result.data.running;
        const sw = document.getElementById('obexReceiveSwitch');
        if (sw) sw.checked = !!running;
    } catch (e) {
        console.warn('获取 OBEX 接收状态失败:', e);
    }
}
