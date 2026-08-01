// 冷启动重拉：首屏设备可能尚未枚举完成，对空结果做有限次递增间隔重拉，稳态由 SSE/pw-mon 接管。
const _COLD_START_RETRY_DELAYS = [1500, 3000, 5000]; // 递增间隔，最多 3 次
const _coldStartTimers = {};

// checkHasData 返回 true 表示已就绪停止重试；refetch 为重新拉取并渲染的异步函数。
function scheduleColdStartRetry(key, checkHasData, refetch) {
    if (_coldStartTimers[key]) return; // 已在调度中
    let attempt = 0;
    const run = async () => {
        _coldStartTimers[key] = null;
        // 已有数据 或 用户已离开触发场景则不再重试
        let hasData = false;
        try { hasData = await checkHasData(); } catch (e) { hasData = false; }
        if (hasData) return;
        if (attempt >= _COLD_START_RETRY_DELAYS.length) return;
        const delay = _COLD_START_RETRY_DELAYS[attempt++];
        _coldStartTimers[key] = setTimeout(async () => {
            _coldStartTimers[key] = null;
            try { await refetch(); } catch (e) { /* 忽略单次失败，等待下次 */ }
            run();
        }, delay);
    };
    run();
}

function stopColdStartRetry(key) {
    if (_coldStartTimers[key]) {
        clearTimeout(_coldStartTimers[key]);
        _coldStartTimers[key] = null;
    }
}

function initTimers() {
    initSSE();
}

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.tab-item').forEach(btn => {
        btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });

    document.getElementById('installDriverBtn').addEventListener('click', installDriver);
    document.getElementById('scanBtn').addEventListener('click', scanDevices);

    const powerSwitch = document.getElementById('powerSwitch');
    if (powerSwitch) {
        powerSwitch.addEventListener('change', (e) => togglePower(e.target.checked));
    }

    const discoverableSwitch = document.getElementById('discoverableSwitch');
    if (discoverableSwitch) {
        discoverableSwitch.addEventListener('change', (e) => toggleDiscoverable(e.target.checked));
    }

    const pairableSwitch = document.getElementById('pairableSwitch');
    if (pairableSwitch) {
        pairableSwitch.addEventListener('change', (e) => togglePairable(e.target.checked));
    }

    const autoReconnectSwitch = document.getElementById('autoReconnectSwitch');
    if (autoReconnectSwitch) {
        autoReconnectSwitch.addEventListener('change', (e) => toggleAutoReconnect(e.target.checked));
    }

    const pinConfirmBtn = document.getElementById('pinConfirmBtn');
    if (pinConfirmBtn) {
        pinConfirmBtn.addEventListener('click', handlePairWithPin);
    }

    const pinCancelBtn = document.getElementById('pinCancelBtn');
    if (pinCancelBtn) {
        pinCancelBtn.addEventListener('click', () => {
            hidePinDialog();
            if (currentPairingMac) setDeviceLoading(currentPairingMac, false);
            currentPairingMac = null;
            currentPairingName = null;
            renderBluetoothDevices(scannedDevices);
        });
    }

    const pinInput = document.getElementById('pinInput');
    if (pinInput) {
        pinInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') handlePairWithPin();
        });
    }

    const fileSelectArea = document.getElementById('fileSelectArea');
    const fileSendInput = document.getElementById('fileSendInput');
    if (fileSelectArea && fileSendInput) {
        fileSelectArea.addEventListener('click', () => fileSendInput.click());
        fileSelectArea.addEventListener('dragover', (e) => { e.preventDefault(); fileSelectArea.classList.add('dragover'); });
        fileSelectArea.addEventListener('dragleave', () => fileSelectArea.classList.remove('dragover'));
        fileSelectArea.addEventListener('drop', (e) => {
            e.preventDefault();
            fileSelectArea.classList.remove('dragover');
            if (e.dataTransfer.files.length > 0) _onFileSelected(e.dataTransfer.files[0]);
        });
        fileSendInput.addEventListener('change', () => {
            if (fileSendInput.files.length > 0) _onFileSelected(fileSendInput.files[0]);
        });
    }

    const fileSendCancelBtn = document.getElementById('fileSendCancelBtn');
    if (fileSendCancelBtn) fileSendCancelBtn.addEventListener('click', _closeFileSendDialog);

    const fileSendConfirmBtn = document.getElementById('fileSendConfirmBtn');
    if (fileSendConfirmBtn) fileSendConfirmBtn.addEventListener('click', _doSendFile);

    const obexReceiveSwitch = document.getElementById('obexReceiveSwitch');
    if (obexReceiveSwitch) {
        obexReceiveSwitch.addEventListener('change', async (e) => {
            try {
                if (e.target.checked) {
                    await apiCall('/api/bluetooth/file/receive/start', { method: 'POST' });
                    showToast('已开启文件接收', 'success');
                } else {
                    await apiCall('/api/bluetooth/file/receive/stop', { method: 'POST' });
                    showToast('已关闭文件接收', 'info');
                }
            } catch (err) {
                showToast('操作失败: ' + err.message, 'error');
                e.target.checked = !e.target.checked;
            }
        });
    }

    const clearTransfersBtn = document.getElementById('clearTransfersBtn');
    if (clearTransfersBtn) {
        clearTransfersBtn.addEventListener('click', async () => {
            try {
                await apiCall('/api/bluetooth/file/clear', { method: 'POST' });
                showToast('已清除传输记录', 'info');
                refreshTransferList();
            } catch (err) {
                showToast('清除失败: ' + err.message, 'error');
            }
        });
    }

    const fileTransferHeader = document.getElementById('fileTransferHeader');
    if (fileTransferHeader) {
        fileTransferHeader.addEventListener('click', () => {
            const list = document.getElementById('fileTransferList');
            const received = document.getElementById('receivedFilesSection');
            const icon = fileTransferHeader.querySelector('.collapse-icon');
            const isHidden = list.style.display === 'none';
            list.style.display = isHidden ? '' : 'none';
            if (received) received.style.display = isHidden ? '' : 'none';
            if (icon) icon.style.transform = isHidden ? '' : 'rotate(180deg)';
        });
    }

    loadObexReceiveStatus();
    refreshTransferList();
    loadReceivedFiles();

    Promise.all([
        updateBluetoothStatus(),
        renderAudioDevices(),
        loadInitialDevices(),
        renderVideoDevices(),
        fetchSystemOverview().then(data => {
            if (data) renderSystemOverview(data);
        })
    ]).catch(e => console.warn('初始化部分失败:', e));

    // 冷启动重拉：首屏空列表时延迟重拉，直至出现设备或达到重试上限。
    scheduleColdStartRetry(
        'audio',
        async () => {
            const r = await getAudioDevices();
            if ((r.devices || []).length > 0) return true;
            try {
                const paired = await getPairedDevices();
                return paired.some(d => d.connected);
            } catch (e) { return false; }
        },
        () => renderAudioDevices()
    );
    scheduleColdStartRetry(
        'video',
        async () => {
            const r = await getVideoDevices();
            return (r.devices || []).length > 0;
        },
        () => renderVideoDevices()
    );
    scheduleColdStartRetry(
        'bluetooth',
        async () => {
            const paired = await getPairedDevices();
            return paired.length > 0;
        },
        () => loadInitialDevices()
    );

    startKeepAlive();
    initTimers();
    createReconnectIndicator();

    const fixAllBtn = document.getElementById('fixAllBtn');
    if (fixAllBtn) {
        fixAllBtn.addEventListener('click', fixAllDependencies);
    }

    const audioScanBtn = document.getElementById('audioScanBtn');
    if (audioScanBtn) {
        audioScanBtn.addEventListener('click', async () => {
            audioScanBtn.disabled = true;
            audioScanBtn.innerHTML = '<svg class="spinner-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>扫描中...';
            try {
                await renderAudioDevices(true);
                showToast('扫描完成', 'success');
            } finally {
                audioScanBtn.disabled = false;
                audioScanBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>扫描';
            }
        });
    }

    const videoScanBtn = document.getElementById('videoScanBtn');
    if (videoScanBtn) {
        videoScanBtn.addEventListener('click', async () => {
            videoScanBtn.disabled = true;
            videoScanBtn.innerHTML = '<svg class="spinner-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>扫描中...';
            try {
                await renderVideoDevices(true);
                showToast('视频设备扫描完成', 'success');
            } finally {
                videoScanBtn.disabled = false;
                videoScanBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>扫描';
            }
        });
    }
});