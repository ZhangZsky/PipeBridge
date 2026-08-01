async function updateBluetoothStatus() {
    let btStatus = null;
    try {
        const status = await fetchBluetoothStatus();
        const data = status.data || {};
        btStatus = data.status;

        const overallDot = document.getElementById('overallStatusDot');
        const overallText = document.getElementById('overallStatusText');
        const installBtn = document.getElementById('installDriverBtn');
        const powerToggle = document.getElementById('powerToggle');
        const powerSwitch = document.getElementById('powerSwitch');
        const discoverableToggle = document.getElementById('discoverableToggle');
        const discoverableSwitch = document.getElementById('discoverableSwitch');
        const controllerInfoInline = document.getElementById('controllerInfoInline');
        const controllersGrid = document.getElementById('controllersGrid');

        if (!overallDot || !overallText) return;

        const statusTexts = {
            'active': '蓝牙就绪',
            'starting': '启动中...',
            'service_running': '服务运行中',
            'hardware_detected': '检测到硬件',
            'not_detected': '未检测到蓝牙',
            'error': '状态异常'
        };

        overallText.textContent = statusTexts[data.status] || '未知状态';

        if (data.status === 'active') {
            overallDot.className = 'status-dot active';
        } else if (data.status === 'starting') {
            overallDot.className = 'status-dot starting';
        } else if (data.status === 'service_running' || data.status === 'hardware_detected') {
            overallDot.className = 'status-dot warning';
        } else if (data.status === 'error') {
            overallDot.className = 'status-dot error';
        } else {
            overallDot.className = 'status-dot';
        }

        if (installBtn) {
            installBtn.style.display = (data.status === 'hardware_detected' || data.status === 'not_detected') ? 'inline-flex' : 'none';
        }

        const scanBtn = document.getElementById('scanBtn');
        if (scanBtn) {
            scanBtn.style.display = (data.status === 'active' || data.status === 'starting' || data.status === 'service_running') ? 'inline-flex' : 'none';
        }

        if (data.controllers && data.controllers.length > 0) {
            // 多适配器（USB/内置）兼容：顶部开关绑定"活动控制器"，优先已上电者，否则取第一个
            const ctrl = data.controllers.find(c => c.powered) || data.controllers[0];
            currentController = ctrl;
            const isPowered = ctrl.powered;
            const isUp = ctrl.status === 'UP';
            const isDiscoverable = ctrl.discoverable;
            const isPairable = ctrl.pairable;

            if (powerToggle && powerSwitch) {
                powerToggle.style.display = 'flex';
                powerSwitch.checked = isPowered;
                powerToggle.classList.toggle('active', isPowered);
                powerSwitch.disabled = false;
            }

            if (discoverableToggle && discoverableSwitch) {
                discoverableToggle.style.display = 'flex';
                discoverableSwitch.checked = isDiscoverable;
                discoverableToggle.classList.toggle('active', isDiscoverable);
                discoverableSwitch.disabled = !isUp || !isPowered;
                
                const timeoutInput = document.getElementById('discoverableTimeout');
                const timeoutLabel = document.getElementById('discoverableTimeoutLabel');
                if (timeoutInput) timeoutInput.style.display = isDiscoverable ? '' : 'none';
                if (timeoutLabel) timeoutLabel.style.display = isDiscoverable ? '' : 'none';
            }

            const pairableToggle = document.getElementById('pairableToggle');
            const pairableSwitch = document.getElementById('pairableSwitch');
            if (pairableToggle && pairableSwitch) {
                pairableToggle.style.display = 'flex';
                pairableSwitch.checked = isPairable;
                pairableToggle.classList.toggle('active', isPairable);
                pairableSwitch.disabled = !isUp || !isPowered;
            }

            const reconnectToggle = document.getElementById('reconnectToggle');
            const autoReconnectSwitch = document.getElementById('autoReconnectSwitch');
            if (reconnectToggle && autoReconnectSwitch) {
                reconnectToggle.style.display = 'flex';
                const isMonitoring = reconnectMonitorData?.monitoring || false;
                autoReconnectSwitch.checked = isMonitoring;
                reconnectToggle.classList.toggle('active', isMonitoring);
                autoReconnectSwitch.disabled = !isPowered;
            }

            if (controllerInfoInline) controllerInfoInline.innerHTML = '';

            if (controllersGrid) {
                controllersGrid.innerHTML = data.controllers.map(c => {
                    const cPowered = c.powered;
                    const cDiscoverable = c.discoverable;
                    const isActive = c.mac === ctrl.mac;
                    return `
                    <div class="controller-card collapsed">
                        <div class="controller-summary controllerSummary">
                            <div class="controller-summary-left">
                                <div class="status-dot ${cPowered ? 'active' : ''}"></div>
                                <span class="controller-summary-name">${c.alias || c.name || '-'}</span>
                                ${isActive && data.controllers.length > 1 ? '<span class="status-badge connected" style="margin-left:6px">活动</span>' : ''}
                                <span class="controller-summary-sep">·</span>
                                <span class="controller-summary-bus">${c.type ? c.type + ' / ' : ''}${c.bus || '-'}</span>
                                <span class="controller-summary-sep">·</span>
                                <span class="controller-summary-mac">${c.mac || '-'}</span>
                            </div>
                            <svg class="controller-expand-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <polyline points="6 9 12 15 18 9"/>
                            </svg>
                        </div>
                        <div class="controller-detail">
                            <div class="controller-info">
                                <div class="info-item"><span class="info-label">MAC 地址</span><span class="info-value mono">${c.mac || '-'}</span></div>
                                <div class="info-item"><span class="info-label">控制器名称</span><span class="info-value mono">${c.name || '-'}</span></div>
                                <div class="info-item"><span class="info-label">别名</span><span class="info-value">${c.alias || '-'}</span></div>
                                <div class="info-item"><span class="info-label">总线类型</span><span class="info-value">${c.bus || '-'}</span></div>
                                <div class="info-item"><span class="info-label">控制器类型</span><span class="info-value">${c.type || '-'}</span></div>
                                <div class="info-item"><span class="info-label">制造商</span><span class="info-value">${c.manufacturer || '-'}${c.manufacturer_id ? ' (' + c.manufacturer_id + ')' : ''}</span></div>
                                <div class="info-item"><span class="info-label">HCI 版本</span><span class="info-value">${c.hci_version || '-'}${c.hci_revision ? ' ' + c.hci_revision : ''}</span></div>
                                <div class="info-item"><span class="info-label">设备类</span><span class="info-value mono">${c.device_class || '-'}</span></div>
                                <div class="info-item"><span class="info-label">功能特征</span><span class="info-value info-value-xs">${c.features || '-'}</span></div>
                                <div class="info-item"><span class="info-label">数据包类型</span><span class="info-value info-value-xs">${c.packet_types || '-'}</span></div>
                                <div class="info-item"><span class="info-label">链路策略</span><span class="info-value">${c.link_policy || '-'}</span></div>
                                <div class="info-item"><span class="info-label">链路模式</span><span class="info-value">${c.link_mode || '-'}</span></div>
                                <div class="info-item"><span class="info-label">电源</span><span class="info-value ${cPowered ? 'success' : 'warning'}">${cPowered ? '开启' : '关闭'}</span></div>
                                <div class="info-item"><span class="info-label">可发现</span><span class="info-value ${cDiscoverable ? 'success' : ''}">${cDiscoverable ? '是' : '否'}</span></div>
                            </div>
                        </div>
                    </div>
                    `;
                }).join('');
                controllersGrid.querySelectorAll('.controllerSummary').forEach(summaryEl => {
                    summaryEl.addEventListener('click', () => {
                        summaryEl.closest('.controller-card').classList.toggle('collapsed');
                    });
                });
            }
        } else {
            currentController = null;
            if (powerToggle) powerToggle.style.display = 'none';
            if (discoverableToggle) discoverableToggle.style.display = 'none';
            const reconnectToggle = document.getElementById('reconnectToggle');
            if (reconnectToggle) reconnectToggle.style.display = 'none';
            if (controllerInfoInline) controllerInfoInline.innerHTML = '';

            if (controllersGrid) {
                const usbDevices = data.usb_devices || [];
                if (usbDevices.length > 0) {
                    controllersGrid.innerHTML = usbDevices.map(usb => `
                        <div class="controller-card">
                            <div class="controller-summary">
                                <div class="controller-summary-left">
                                    <div class="status-dot warning"></div>
                                    <span class="controller-summary-name">${usb.name || '蓝牙设备'}</span>
                                    <span class="controller-summary-sep">·</span>
                                    <span class="controller-summary-bus">USB Bus ${usb.bus}</span>
                                    <span class="controller-summary-sep">·</span>
                                    <span class="controller-summary-mac">${usb.id || '-'}</span>
                                </div>
                            </div>
                        </div>
                    `).join('');
                } else {
                    controllersGrid.innerHTML = '<div class="empty-state"><p>未检测到蓝牙控制器</p></div>';
                }
            }
        }
    } catch (error) {
        const overallText = document.getElementById('overallStatusText');
        const overallDot = document.getElementById('overallStatusDot');
        if (overallText) overallText.textContent = '状态获取失败';
        if (overallDot) overallDot.className = 'status-dot';
    }

    // 蓝牙"启动中"状态的自愈刷新：优先由 RefreshManager 统一处理
    // （后端 EventDetector 1s 检测 bt_status 变化 → SSE bluetooth.changed → 前端刷新）；
    // 但后端快照存在初始化竞态可能漏发事件，故 starting 时额外启动 200ms 兜底轮询，
    // 一旦状态变为非 starting 立即停止，避免界面卡在"启动中"。
    if (btStatus === 'starting') {
        _startBtStartingWatch();
    } else if (btStatus !== null) {
        // 仅在成功获取到状态时才停止（获取失败 btStatus 为 null，保持既有轮询继续重试）
        _stopBtStartingWatch();
    }
}

async function renderBluetoothDevices(devices, cachedPairedDevices = null) {
    const container = document.getElementById('bluetoothDeviceList');
    if (!container) return;
    scannedDevices = devices || [];

    const pairedDevices = cachedPairedDevices || await getPairedDevices();
    const pairedMap = new Map(pairedDevices.map(d => [d.mac, d]));

    const allDevices = [...pairedDevices];
    for (const d of scannedDevices) {
        if (!pairedMap.has(d.mac)) allDevices.push(d);
    }

    let audioSources = [];
    try {
        const srcResult = await apiCall('/api/bluetooth/audio-sources');
        audioSources = srcResult.data || [];
    } catch (e) { console.warn('get audio sources error:', e); }

    if (allDevices.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M6.5 6.5l11 11L12 23V1l5.5 5.5-11 11"/>
                </svg>
                <p>点击"扫描"查找附近设备</p>
            </div>
        `;
        return;
    }

    container.innerHTML = allDevices.map(device => {
        const pairedInfo = pairedMap.get(device.mac);
        
        device._isPaired = !!pairedInfo || device.paired === true;
        device._isConnected = pairedInfo?.connected === true || device.connected === true;
        device._deviceType = device.type || pairedInfo?.type || pairedInfo?.icon || '';
        device._vendor = pairedInfo?.vendor || '';
        device._battery = pairedInfo?.battery || '';
        device._rssi = (() => {
            const pr = pairedInfo?.rssi || '';
            const dr = device.rssi != null ? (typeof device.rssi === 'number' ? device.rssi + ' dBm' : String(device.rssi)) : '';
            
            if (!pr && !dr) return '';
            if (!pr) return dr;
            if (!dr) return pr;
            
            const drVal = parseInt(dr);
            const prVal = parseInt(pr);
            if (isNaN(drVal)) return pr;
            if (isNaN(prVal)) return dr;
            return drVal >= prVal ? dr : pr;
        })();
        device._txPower = pairedInfo?.tx_power || '';
        device._servicesResolved = pairedInfo?.services_resolved || false;
        device._modalias = pairedInfo?.modalias || '';
        device._uuid = pairedInfo?.uuid || [];
        device._deviceClass = pairedInfo?.device_class || '';
        device._adapterPath = pairedInfo?.adapter_path || '';
        device._icon = pairedInfo?.icon || '';
        device._appearance = pairedInfo?.appearance || '';
        device._addressType = pairedInfo?.address_type || '';
        device._manufacturerId = pairedInfo?.manufacturer_id || '';
        device._btAudioRole = pairedInfo?.bt_audio_role || device.bt_audio_role || '';
        device._isAudio = (pairedInfo?.is_audio === true) || (device.is_audio === true);

        let displayName = (pairedInfo?.alias || '') || (device.name || '未知设备').trim();
        if (displayName.startsWith('Device ') || displayName.startsWith('Controller ')) {
            const macInName = displayName.match(/([0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2})/);
            if (macInName) {
                displayName = displayName.replace(macInName[0], '').replace(/^(Device|Controller)\s*/, '').trim();
                displayName = displayName.replace(/\s*(Discovering|Connected|Paired|Bonded).*$/i, '').trim();
            }
        }
        if (!displayName || displayName.length < 2) displayName = '未知蓝牙设备';
        device._displayName = displayName;

        return renderDeviceCard('bluetooth', device, { audioSources });
    }).join('');

    container.querySelectorAll('.btn[data-action], .btn-rename[data-action]').forEach(btn => {
        btn.addEventListener('click', handleDeviceAction);
    });
    
    container.querySelectorAll('.bt-profile-select').forEach(sel => {
        _loadBtProfiles(sel);
        sel.addEventListener('change', _handleBtProfileChange);
    });
    container.querySelectorAll('.bt-mic-toggle').forEach(btn => {
        btn.addEventListener('click', _handleBtMicToggle);
    });
    _applyDeviceCardCollapse(container);
}

function _buildPwMacs(devices) {
    const pwMacs = new Set();
    for (const d of devices) {
        const macMatch = (d.name || '').match(/([0-9a-fA-F]{2})[:_]?([0-9a-fA-F]{2})[:_]?([0-9a-fA-F]{2})[:_]?([0-9a-fA-F]{2})[:_]?([0-9a-fA-F]{2})[:_]?([0-9a-fA-F]{2})/);
        if (macMatch) {
            pwMacs.add(macMatch.slice(1).join(':').toUpperCase());
        }
    }
    return pwMacs;
}

async function renderAudioDevices(forceScan = false) {
    const container = document.getElementById('audioDeviceList');
    if (!container) return;

    const audioResult = forceScan ? await scanAudioDevices() : await getAudioDevices();
    const audioDevices = audioResult.devices || [];
    let defaultSink = audioResult.default || '';
    let defaultSource = audioResult.default_source || '';

    const pwMacs = _buildPwMacs(audioDevices);

    if (audioDevices.length === 0) {
        const errorMsg = audioResult.error || '';
        container.innerHTML = `
            <div class="empty-state">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
                    <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
                </svg>
                <p>暂无音频设备</p>
                <p class="empty-state-sub">${errorMsg ? errorMsg : '正在检索蓝牙设备…'}</p>
            </div>
        `;
        await _supplementBtAudioDevices(container, audioDevices, defaultSink, defaultSource, pwMacs);
        return;
    }

    _renderAudioList(container, audioDevices, defaultSink, defaultSource, pwMacs);
    await _supplementBtAudioDevices(container, audioDevices, defaultSink, defaultSource, pwMacs);
}

async function _supplementBtAudioDevices(container, audioDevices, defaultSink, defaultSource, pwMacs) {
    try {
        const pairedDevices = await getPairedDevices();
        const isAudioDevice = (d) => {
            if (d.is_audio === true) return true;
            const t = (d.type || d.icon || d.name || '').toLowerCase();
            return t.includes('audio') || t.includes('headset') || t.includes('speaker') || t.includes('headphone') || t.includes('sound');
        };

        const connectedBtAudio = pairedDevices.filter(d => d.connected && isAudioDevice(d));

        const allAudioDevices = [...audioDevices];
        const pwNames = new Set(audioDevices.map(d => d.name.toLowerCase()));

        for (const bt of connectedBtAudio) {
            if (pwMacs.has(bt.mac)) continue;
            const btName = (bt.alias || bt.name || '').toLowerCase();
            if (!pwNames.has(btName)) {
                allAudioDevices.push({
                    name: bt.alias || bt.name,
                    friendly_name: bt.alias || bt.name,
                    state: '已连接',
                    isBluetooth: true,
                    mac: bt.mac,
                    connected: true,
                    audio_type: 'bluetooth',
                    bt_type: bt.type || bt.icon || '',
                    role: bt.bt_audio_role || 'sink'
                });
                pwNames.add(btName);
            }
        }

        if (allAudioDevices.length > audioDevices.length) {
            _renderAudioList(container, allAudioDevices, defaultSink, defaultSource, pwMacs);
        } else if (audioDevices.length === 0) {
            const subEl = container.querySelector('.empty-state-sub');
            if (subEl) subEl.textContent = '未找到蓝牙音频设备';
        }
    } catch (e) {
        if (audioDevices.length === 0) {
            const subEl = container.querySelector('.empty-state-sub');
            if (subEl) subEl.textContent = '蓝牙检索失败';
        }
    }
}

function _renderAudioList(container, allAudioDevices, defaultSink, defaultSource, pwMacs) {
    let html = '';
    if (allAudioDevices.length > 0) {
        html += allAudioDevices.map(device => {
            const isDefault = device.is_default || device.name === defaultSink || device.name === defaultSource;
            return renderDeviceCard('audio', device, { isDefault, defaultSink, defaultSource, pwMacs });
        }).join('');
    }
    container.innerHTML = html;
    _bindAudioActions(container);
}

function _bindAudioActions(container) {
    container.querySelectorAll('.btn[data-action]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            if (isLoading) return;
            const action = e.currentTarget.dataset.action;
            if (action === 'activateDevice') {
                await activateAudioDevice(e.currentTarget.dataset.device);
            } else if (action === 'setDefault') {
                setDefaultDevice(e.currentTarget.dataset.device);
            } else if (action === 'playDing' || action === 'testSound') {
                const dev = e.currentTarget.dataset.device;
                if (_channelTestStop[dev] === false) {
                    _channelTestStop[dev] = true;
                } else {
                    let chData = [];
                    try { chData = JSON.parse(decodeURIComponent(e.currentTarget.dataset.channels || '[]')); } catch (_) {}
                    await playTestSound(dev, chData);
                }
            } else if (action === 'disconnectBtAudio') {
                const mac = e.currentTarget.dataset.mac;
                await disconnectDevice(mac);  
            }
        });
    });

    container.querySelectorAll('.mute-btn[data-action="toggleMute"]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            if (isLoading) return;
            await toggleMute(e.currentTarget.dataset.device);
        });
    });

    container.querySelectorAll('.volume-slider').forEach(slider => {
        const updateText = () => {
            const textEl = slider.parentElement.querySelector('.volume-text');
            if (textEl) {
                textEl.textContent = `${slider.value}%`;
                textEl.classList.remove('muted-text');
            }
        };
        slider.addEventListener('input', () => {
            // 拖动期间记录目标音量，防止 SSE 推送覆盖正在拖动的滑块
            _markDeviceAdjusting(slider.dataset.device, { opType: 'volume', targetVolume: parseInt(slider.value) });
            updateText();
        });
        slider.addEventListener('change', (e) => {
            if (isLoading) return;
            const device = e.currentTarget.dataset.device;
            const volume = parseInt(e.currentTarget.value);
            // 去抖：快速拖动/连点会连续触发 change，合并为最后一次写入，
            // 避免多次异步 setVolume 竞争导致最终生效值与目标不一致（跳变）。
            if (_volumeTimers[device]) clearTimeout(_volumeTimers[device]);
            _markDeviceAdjusting(device, { opType: 'volume', targetVolume: volume });
            _volumeTimers[device] = setTimeout(() => {
                delete _volumeTimers[device];
                setVolume(device, volume);
            }, 120);
        });
    });

    container.querySelectorAll('.channel-volume-slider').forEach(slider => {
        const updateText = () => {
            const textEl = slider.parentElement.querySelector('.channel-vol-text');
            if (textEl) textEl.textContent = `${slider.value}%`;
        };
        slider.addEventListener('input', updateText);
        slider.addEventListener('change', async (e) => {
            if (isLoading) return;
            const device = e.currentTarget.dataset.device;
            const channel = parseInt(e.currentTarget.dataset.channel);
            const volume = parseInt(e.currentTarget.value);
            try {
                const result = await apiCall('/api/audio/volume/channel', {
                    method: 'POST',
                    body: JSON.stringify({ device, channel, volume })
                });
                
                const card = e.currentTarget.closest('.device-card');
                if (card && result.data) {
                    const chEl = card.querySelector('.channel-volumes');
                    if (chEl) {
                        
                        const sliders = card.querySelectorAll('.channel-volume-slider');
                        const labels = card.querySelectorAll('.channel-vol-text');
                        const parts = [];
                        sliders.forEach((s, i) => {
                            const chLabel = s.parentElement.querySelector('.channel-label');
                            parts.push(`${chLabel?.textContent || 'CH' + i}: ${s.value}%`);
                        });
                        chEl.textContent = parts.join(' / ');
                    }
                }
            } catch (error) {
                showToast('设置声道音量失败: ' + error.message, 'error');
            }
        });
    });

    container.querySelectorAll('.balance-slider').forEach(slider => {
        const updateBalanceText = () => {
            const val = parseInt(slider.value);
            const labelEl = slider.parentElement.querySelector('.balance-value');
            if (labelEl) {
                const absVal = Math.abs(val);
                const side = val < 0 ? 'L' : val > 0 ? 'R' : '';
                labelEl.textContent = side ? `${side} ${absVal}%` : '0';
            }
        };
        slider.addEventListener('input', updateBalanceText);
        slider.addEventListener('change', async (e) => {
            if (isLoading) return;
            await setBalance(e.currentTarget.dataset.device, parseInt(e.currentTarget.value));
        });
    });

    container.querySelectorAll('.audio-port-select').forEach(sel => {
        sel.addEventListener('change', async (e) => {
            const device = e.target.dataset.device;
            const route = e.target.value;
            sel.disabled = true;
            try {
                await apiCall('/api/audio/route', {
                    method: 'POST',
                    body: JSON.stringify({ device, route })
                });
                showToast('端口已切换', 'success');
            } catch (err) {
                showToast('端口切换失败: ' + err.message, 'error');
            } finally {
                sel.disabled = false;
            }
        });
    });

    container.querySelectorAll('.audio-profile-select').forEach(sel => {
        sel.addEventListener('change', async (e) => {
            const device = e.target.dataset.device;
            const profile = e.target.value;
            if (!profile) return;
            sel.disabled = true;
            try {
                await apiCall('/api/audio/profile', {
                    method: 'POST',
                    body: JSON.stringify({ device, profile })
                });
                showToast('Profile 已切换', 'success');
            } catch (err) {
                showToast('Profile 切换失败: ' + err.message, 'error');
            } finally {
                sel.disabled = false;
            }
        });
    });
    _applyDeviceCardCollapse(container);
}

async function _loadAudioProfiles(selectEl) {
    const device = selectEl.dataset.device;
    try {
        const result = await apiCall(`/api/audio/profiles/${encodeURIComponent(device)}`);
        const data = result.data || {};
        const profiles = data.profiles || [];
        const activeProfile = data.active_profile || '';
        selectEl.innerHTML = '';
        if (profiles.length === 0) {
            selectEl.innerHTML = '<option value="">无可用 Profile</option>';
            return;
        }
        profiles.forEach(p => {
            const opt = document.createElement('option');
            opt.value = p.name;
            opt.textContent = p.description || p.name;
            if (p.name === activeProfile) opt.selected = true;
            selectEl.appendChild(opt);
        });
    } catch (e) {
        selectEl.innerHTML = '<option value="">获取失败</option>';
    }
}

function switchTab(tabName) {
    currentTab = tabName;

    // UI 切换
    document.querySelectorAll('.tab-item').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tabName);
    });
    document.querySelectorAll('.tab-panel').forEach(panel => {
        panel.classList.toggle('active', panel.id === `${tabName}Tab`);
    });

    // 页面特定的非刷新逻辑（刷新统一由 onTabSwitch 处理）
    if (tabName === 'bluetooth') {
        if (scannedDevices.length === 0) loadInitialDevices();
    }

    // 统一刷新：切换页面时始终刷新当前页面数据
    RefreshManager.onTabSwitch(tabName);
}

let lastBtSnapshot = '';
let lastAudioSnapshot = '';

let sse = null;
let sseErrorCount = 0;
let sseFallbackTimers = {};
const SSE_MAX_ERRORS = 5;

// 统一刷新管理器：脏标记 + 防抖，统一 SSE 事件与 fallback 轮询的刷新入口。
const RefreshManager = {
    FALLBACK_INTERVAL: 200, // SSE 断线兜底轮询间隔（ms）
    DEBOUNCE_DELAY: 100,    // SSE 事件到达后的防抖延迟（ms）
    _dirty: {},           // 脏标记：各页面是否有未刷新的数据变化
    _debounceTimers: {},  // 防抖定时器
    _fallbackBusy: false, // 兜底轮询防重入标记

    // 各页面刷新函数（SSE 事件和 fallback 共用，确保刷新内容一致）
    _refreshers: {
        audio: async (payload) => {
            if (payload && payload.devices) {
                _applyAudioPayload(payload.devices);
            } else {
                // 兜底全量刷新：拉取完整列表后增量更新 UI（避免重渲染以保护滑块状态）
                const audioResult = await getAudioDevices();
                const devices = audioResult.devices || [];
                _updateAudioDevicesInPlace(devices, audioResult);
            }
        },
        bluetooth: async () => {
            await updateBluetoothStatus();
            try {
                const pairedDevices = await getPairedDevices();
                lastBtSnapshot = pairedDevices.map(d => `${d.mac}|${d.connected}`).join(';');
                _mergePairedIntoScanned(pairedDevices);
                await renderBluetoothDevices(scannedDevices, pairedDevices);
            } catch (e) { console.warn('bt refresh error:', e); }
            refreshTransferList();
            pollReconnectStatus();
        },
        video: () => { renderVideoDevices(); },
        system: async () => {
            const data = await fetchSystemOverview();
            if (data) renderSystemOverview(data);
        },
    },

    // SSE 事件到达：无论是否在当前页面都标记脏，当前页面立即防抖刷新
    onEvent(tab, payload) {
        this._dirty[tab] = true;
        if (currentTab === tab) {
            this._debounce(tab, payload);
        }
    },

    // 切换页面时：始终刷新当前页面数据（确保数据最新）
    onTabSwitch(tab) {
        this._dirty[tab] = false;
        const refresher = this._refreshers[tab];
        if (refresher) {
            try { refresher(); } catch (e) { console.warn(`tab switch refresh [${tab}] error:`, e); }
        }
    },

    // 防抖刷新：SSE 事件到达后统一延迟合并刷新
    _debounce(tab, payload) {
        clearTimeout(this._debounceTimers[tab]);
        this._debounceTimers[tab] = setTimeout(() => {
            // 用户已切换页面则跳过（onTabSwitch 会负责刷新）
            if (currentTab !== tab) return;
            this._dirty[tab] = false;
            const refresher = this._refreshers[tab];
            if (refresher) {
                try { refresher(payload); } catch (e) { console.warn(`refresh [${tab}] error:`, e); }
            }
        }, this.DEBOUNCE_DELAY);
    },

    // SSE 断开时的统一兜底轮询：200ms 刷新当前可见页面。
    // 防重入守卫 _fallbackBusy：上一轮刷新（含多个异步 HTTP 请求）未完成则跳过本轮，
    // 避免 200ms 间隔下请求堆积雪崩。
    startFallback() {
        if (sseFallbackTimers._active) return;
        sseFallbackTimers._active = true;
        this._fallbackBusy = false;
        sseFallbackTimers._main = setInterval(() => {
            if (this._fallbackBusy) return;
            const refresher = this._refreshers[currentTab];
            if (!refresher) return;
            this._fallbackBusy = true;
            Promise.resolve()
                .then(() => refresher())
                .catch(e => console.warn(`fallback refresh [${currentTab}] error:`, e))
                .finally(() => { this._fallbackBusy = false; });
        }, RefreshManager.FALLBACK_INTERVAL);
    },

    stopFallback() {
        Object.keys(sseFallbackTimers).forEach(k => {
            if (k !== '_active') clearInterval(sseFallbackTimers[k]);
        });
        sseFallbackTimers = {};
    }
};

function initSSE() {
    try {
        sse = new EventSource('/api/events');

        sse.onopen = () => {
            sseErrorCount = 0;
            RefreshManager.stopFallback();
        };

        sse.addEventListener('audio.changed', (e) => {
            let payload = null;
            try {
                const parsed = JSON.parse(e.data || '{}');
                if (parsed && parsed.data && Array.isArray(parsed.data.devices)) {
                    payload = parsed.data;
                }
            } catch (err) { /* 解析失败按兜底处理 */ }
            RefreshManager.onEvent('audio', payload);
        });

        sse.addEventListener('bluetooth.changed', () => {
            RefreshManager.onEvent('bluetooth');
        });

        sse.addEventListener('filetransfer.changed', () => {
            // 文件传输与蓝牙共用页面，标记蓝牙页面脏
            RefreshManager.onEvent('bluetooth');
        });

        sse.addEventListener('video.changed', () => {
            RefreshManager.onEvent('video');
        });

        sse.addEventListener('system.changed', () => {
            RefreshManager.onEvent('system');
        });

        sse.onerror = () => {
            sseErrorCount++;
            if (sseErrorCount >= SSE_MAX_ERRORS) {
                if (sse) sse.close();
                sse = null;
                RefreshManager.startFallback();
            }
        };
    } catch (e) {
        RefreshManager.startFallback();
    }
}

function startSSEFallback() { RefreshManager.startFallback(); }
function stopSSEFallback() { RefreshManager.stopFallback(); }

// 应用 pw-mon 实时 payload：仅更新 payload 中涉及的设备
function _applyAudioPayload(devices) {
    if (!Array.isArray(devices)) return;
    devices.forEach(d => {
        if (!d || !d.name) return;
        const card = document.querySelector(`.device-card[data-device="${CSS.escape(d.name)}"]`);
        if (!card) {
            // 卡片不存在（可能是新设备），触发一次全量刷新
            renderAudioDevices();
            return;
        }
        // 状态锁：匹配成功或未锁定才应用实际值；仍在调整中则跳过覆盖。
        let canApply = true;
        if (_isDeviceAdjusting(d.name)) {
            const completed = _tryCompleteAdjusting(d.name, d);
            canApply = completed || !_isDeviceAdjusting(d.name);
        }
        if (!canApply) {
            // 仍在调整中，仅更新非锁定字段（如 channel display）
            if (d.channels && d.channels.length) {
                const chEl = card.querySelector('.channel-volumes');
                if (chEl) {
                    chEl.textContent = d.channels.map((v, i) => `CH${i}: ${v}%`).join(' / ');
                }
            }
            return;
        }
        // 已释放锁或未锁定：应用实际值
        const slider = card.querySelector('.volume-slider');
        if (slider && document.activeElement !== slider) {
            slider.max = 100;
            if (typeof d.volume === 'number') {
                slider.value = Math.min(d.volume, 100);
            }
        }
        const volText = card.querySelector('.volume-text');
        if (typeof d.muted === 'boolean') {
            const muteBtn = card.querySelector('.mute-btn');
            if (muteBtn) muteBtn.classList.toggle('muted', d.muted);
            if (volText) {
                if (d.muted) {
                    volText.textContent = '静音';
                    volText.classList.add('muted-text');
                } else {
                    volText.classList.remove('muted-text');
                    if (typeof d.volume === 'number') {
                        volText.textContent = `${Math.min(d.volume, 100)}%`;
                    }
                }
            }
        } else if (volText && !volText.classList.contains('muted-text') && typeof d.volume === 'number') {
            volText.textContent = `${Math.min(d.volume, 100)}%`;
        }
        if (d.channels && d.channels.length) {
            const chEl = card.querySelector('.channel-volumes');
            if (chEl) {
                // payload 中 channels 是数字数组（各声道音量百分比）
                chEl.textContent = d.channels.map((v, i) => `CH${i}: ${v}%`).join(' / ');
            }
        }
    });
}

function _updateAudioDevicesInPlace(devices, audioResult) {
    const defaultName = audioResult.default || '';
    devices.forEach(d => {
        const card = document.querySelector(`.device-card[data-device="${CSS.escape(d.name)}"]`);
        if (!card) return;
        // 状态锁：兜底全量刷新同样走状态匹配，channels 对象数组转数字数组
        const matchPayload = {
            volume: typeof d.volume === 'number' ? d.volume : undefined,
            muted: typeof d.muted === 'boolean' ? d.muted : undefined,
            channels: Array.isArray(d.channels) ? d.channels.map(c => c.effective_volume ?? c.volume) : undefined,
        };
        let canApply = true;
        if (_isDeviceAdjusting(d.name)) {
            const completed = _tryCompleteAdjusting(d.name, matchPayload);
            canApply = completed || !_isDeviceAdjusting(d.name);
        }
        // 非锁定字段（默认标记、平衡滑块）始终更新，不受音量锁影响
        const defaultBadge = card.querySelector('.default-badge');
        const isDefault = d.name === defaultName;
        if (defaultBadge) {
            defaultBadge.style.display = isDefault ? '' : 'none';
        }
        const balSlider = card.querySelector('.balance-slider');
        if (balSlider && document.activeElement !== balSlider && d.balance !== undefined) {
            balSlider.value = Math.round((d.balance || 0) * 100);
            const balLabel = balSlider.parentElement.querySelector('.balance-value');
            if (balLabel) {
                const bv = d.balance || 0;
                const abs = Math.round(Math.abs(bv) * 100);
                balLabel.textContent = bv < 0 ? `L ${abs}%` : bv > 0 ? `R ${abs}%` : '0';
            }
        }
        if (!canApply) return;
        // 已释放锁或未锁定：应用实际值
        const slider = card.querySelector('.volume-slider');
        if (slider && document.activeElement !== slider) {
            slider.max = 100;
            slider.value = Math.min(d.volume || 0, 100);
        }
        const volText = card.querySelector('.volume-text');
        if (volText && !volText.classList.contains('muted-text')) {
            volText.textContent = `${Math.min(d.volume || 0, 100)}%`;
        }
        const muteBtn = card.querySelector('.mute-btn');
        if (muteBtn) {
            muteBtn.classList.toggle('muted', d.muted);
        }
        const chEl = card.querySelector('.channel-volumes');
        if (chEl && d.channels && d.channels.length) {
            chEl.textContent = d.channels.map(c => `${c.channel}: ${c.effective_volume ?? c.volume}%`).join(' / ');
        }
    });
}

async function loadInitialDevices() {
    const pairedDevices = await getPairedDevices();
    if (pairedDevices.length > 0) {
        _mergePairedIntoScanned(pairedDevices);
        await renderBluetoothDevices(scannedDevices);
    }
}

function _mergePairedIntoScanned(pairedDevices) {
    const pairedMap = new Map(pairedDevices.map(d => [d.mac, d]));
    for (const [mac, info] of pairedMap) {
        const idx = scannedDevices.findIndex(d => d.mac === mac);
        if (idx >= 0) {
            const scanRssi = scannedDevices[idx].rssi;
            const merged = { ...scannedDevices[idx], ...info, connected: info.connected ?? scannedDevices[idx].connected };
            if (scanRssi != null && (info.rssi == null || info.rssi === '')) {
                merged.rssi = scanRssi;
            }
            scannedDevices[idx] = merged;
        } else {
            scannedDevices.push({ ...info });
        }
    }
    
    if (scannedDevices.length > 100) {
        const pairedMacs = new Set(pairedMap.keys());
        scannedDevices = scannedDevices.filter(d => pairedMacs.has(d.mac) || d.paired === false);
    }
}