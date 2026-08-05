
function renderDeviceCard(type, device, options = {}) {
    const { isDefault, defaultSink, defaultSource, pwMacs, audioSources } = options;
    let html = '';
    let maxVisible = 5;
    if (type === 'bluetooth') {
        html = _renderBtCard(device, { audioSources: audioSources || [] });
        maxVisible = 3;
    } else if (type === 'audio') {
        html = _renderAudioCard(device, { isDefault, defaultSink, defaultSource, pwMacs });
        maxVisible = 8;
    } else if (type === 'video') {
        html = _renderVideoCard(device, { isDefault });
        maxVisible = 8;
    }
    return _applyCollapseToHtml(html, maxVisible);
}

function _applyCollapseToHtml(html, maxVisible) {
    const temp = document.createElement('div');
    temp.innerHTML = html;
    temp.querySelectorAll('.device-details').forEach(details => {
        const rows = details.querySelectorAll('.device-detail-row');
        if (rows.length <= maxVisible) return;
        rows.forEach((row, i) => {
            if (i >= maxVisible) {
                row.style.display = 'none';
                row.dataset.extra = '1';
            }
        });
        const toggleBtn = document.createElement('div');
        toggleBtn.className = 'detail-toggle-btn';
        toggleBtn.dataset.max = maxVisible;
        toggleBtn.textContent = `展开详情 (共${rows.length}项)`;
        details.appendChild(toggleBtn);
    });
    return temp.innerHTML;
}

function _renderBtCard(device, { audioSources = [] } = {}) {
    const isPaired = device._isPaired || false;
    const isConnected = device._isConnected || false;
    const deviceType = device._deviceType || '';
    const displayName = device._displayName || '未知蓝牙设备';
    const typeLabel = BT_TYPE_LABELS[deviceType] || (deviceType ? deviceType : '');
    const deviceVendor = device._vendor || '';
    const deviceBattery = device._battery || '';
    const deviceRssi = device._rssi || '';
    const deviceTxPower = device._txPower || '';
    const servicesResolved = device._servicesResolved || false;
    const deviceModalias = device._modalias || '';
    const deviceUuid = device._uuid || [];
    const deviceClass = device._deviceClass || '';
    const adapterPath = device._adapterPath || '';
    const deviceIconName = device._icon || '';
    const deviceAppearance = device._appearance || '';
    const deviceAddressType = device._addressType || '';
    const deviceManufacturerId = device._manufacturerId || '';
    const btAudioRole = device._btAudioRole || '';
    const isAudioDev = device._isAudio || isAudioDeviceType(deviceType);

    let rssiHtml = '';
    if (deviceRssi) {
        const rssiVal = parseInt(deviceRssi);
        const bars = rssiVal > -50 ? 5 : rssiVal > -65 ? 4 : rssiVal > -80 ? 3 : rssiVal > -90 ? 2 : 1;
        let barColor = rssiVal > -65 ? '#22c55e' : rssiVal > -80 ? '#eab308' : '#ef4444';
        let barHtml = '';
        for (let i = 0; i < 5; i++) {
            barHtml += `<span class="signal-bar ${i < bars ? 'active' : ''}" style="${i < bars ? `background:${barColor}` : ''}"></span>`;
        }
        rssiHtml = `<div class="device-detail-row"><span class="detail-label">信号</span><span class="detail-value"><span class="signal-bars">${barHtml}</span> ${escapeHtml(deviceRssi)}</span></div>`;
    }

    return `
        <div class="device-card ${isConnected ? 'connected' : ''} ${isPaired && !isConnected ? 'offline' : ''} ${isLoading ? 'loading' : ''}">
            <div class="device-header">
                <div class="device-info">
                    <div class="device-name">${escapeHtml(displayName)}</div>
                    ${isPaired ? `
                    <button class="btn-rename" data-action="rename" data-mac="${escapeAttr(device.mac)}" data-name="${escapeAttr(displayName)}" title="重命名设备">✎</button>
                    ` : ''}
                </div>
                ${isConnected ? '<span class="status-badge connected">已连接</span>' : (isPaired ? '<span class="status-badge disconnected">已配对</span>' : '')}
                ${isConnected && reconnectMonitorData?.monitoring && isAudioDeviceType(deviceType) ? '<span class="status-badge reconnect-monitor">自动重连</span>' : ''}
                ${isAudioDev && btAudioRole === 'sink' ? '<span class="status-badge type-badge">音频输出</span>' : ''}
                ${isAudioDev && btAudioRole === 'source' ? '<span class="status-badge type-badge">音频输入</span>' : ''}
                ${deviceBattery ? `<span class="status-badge">电量 ${escapeHtml(deviceBattery)}</span>` : ''}
            </div>
            <div class="device-details">
                ${(() => {
                    const rows = [];
                    if (typeLabel) rows.push(`<div class="device-detail-row"><span class="detail-label">类型</span><span class="detail-value">${escapeHtml(typeLabel)}</span></div>`);
                    rows.push(`<div class="device-detail-row"><span class="detail-label">MAC</span><span class="detail-value mono">${escapeHtml(device.mac)}</span></div>`);
                    if (rssiHtml) rows.push(rssiHtml);
                    if (isConnected && isAudioDeviceType(deviceType)) {
                        rows.push(`<div class="device-detail-row"><span class="detail-label">音频模式</span><span class="detail-value"><select class="detail-select bt-profile-select" data-mac="${escapeAttr(device.mac)}"><option value="">加载中...</option></select></span></div>`);
                        rows.push(`<div class="device-detail-row"><span class="detail-label">麦克风</span><span class="detail-value"><button class="btn btn-sm bt-mic-toggle" data-mac="${escapeAttr(device.mac)}" data-enabled="false">关闭</button></span></div>`);
                    }
                    if (deviceAppearance) rows.push(`<div class="device-detail-row"><span class="detail-label">外观</span><span class="detail-value">${escapeHtml(deviceAppearance)}</span></div>`);
                    if (deviceAddressType) rows.push(`<div class="device-detail-row"><span class="detail-label">地址</span><span class="detail-value">${escapeHtml(deviceAddressType)}</span></div>`);
                    if (deviceVendor) rows.push(`<div class="device-detail-row"><span class="detail-label">厂商</span><span class="detail-value">${escapeHtml(deviceVendor)}</span></div>`);
                    if (deviceBattery) rows.push(`<div class="device-detail-row"><span class="detail-label">电量</span><span class="detail-value">${escapeHtml(deviceBattery)}</span></div>`);
                    if (deviceTxPower) rows.push(`<div class="device-detail-row"><span class="detail-label">发射功率</span><span class="detail-value">${escapeHtml(deviceTxPower)}</span></div>`);
                    if (deviceModalias) rows.push(`<div class="device-detail-row"><span class="detail-label">设备ID</span><span class="detail-value mono detail-value-sm">${escapeHtml(deviceModalias)}</span></div>`);
                    if (deviceManufacturerId) rows.push(`<div class="device-detail-row"><span class="detail-label">厂商ID</span><span class="detail-value mono detail-value-sm">${escapeHtml(deviceManufacturerId)}</span></div>`);
               if (deviceUuid.length > 0) rows.push(`<div class="device-detail-row"><span class="detail-label">UUID</span><span class="detail-value mono detail-value-xs">${escapeHtml(deviceUuid.join(', '))}</span></div>`);

                    if (deviceIconName) rows.push(`<div class="device-detail-row"><span class="detail-label">图标</span><span class="detail-value mono detail-value-sm">${escapeHtml(deviceIconName)}</span></div>`);
                    if (deviceClass) rows.push(`<div class="device-detail-row"><span class="detail-label">设备类</span><span class="detail-value mono detail-value-sm">${escapeHtml(deviceClass)}</span></div>`);
                    if (adapterPath) rows.push(`<div class="device-detail-row detail-row-last"><span class="detail-label">适配器</span><span class="detail-value mono detail-value-xs">${escapeHtml(adapterPath)}</span></div>`);
                    return rows.join('');
                })()}
            </div>
            <div class="device-actions">
                ${!isPaired ? `
                    <button class="btn btn-primary" data-action="pair" data-mac="${escapeAttr(device.mac)}">配对</button>
                ` : `
                    ${isConnected ? `
                        <button class="btn btn-secondary" data-action="disconnect" data-mac="${escapeAttr(device.mac)}">断开</button>
                        ${(() => {
                            const OPP_UUID = '00001105-0000-1000-8000-00805f9b34fb';
                            const hasOpp = deviceUuid.some(u => u.toLowerCase().replace(/-/g, '').includes(OPP_UUID.replace(/-/g, '').toLowerCase()));
                            const isAudio = isAudioDeviceType(deviceType);
                            const isInput = isInputDeviceType(deviceType);
                            // HID 输入设备（鼠标/键盘/手柄）不支持 OBEX 文件传输，仅在显式声明 OPP 时展示
                            return (hasOpp || (!isAudio && !isInput && deviceUuid.length > 0)) ? `<button class="btn btn-sm btn-accent" data-action="sendFile" data-mac="${escapeAttr(device.mac)}" data-name="${escapeAttr(device.name || device.mac)}">发送文件</button>` : '';
                        })()}
                    ` : `
                        <button class="btn btn-secondary" data-action="connect" data-mac="${escapeAttr(device.mac)}">连接</button>
                    `}
                    <button class="btn btn-danger" data-action="remove" data-mac="${escapeAttr(device.mac)}">删除</button>
                `}
            </div>
        </div>
    `;
}

function _renderAudioCard(device, { isDefault, defaultSink, defaultSource, pwMacs }) {
    const needsActivate = device.needs_activate === true;
    const isConnected = device.connected === true;
    const displayName = device.friendly_name || device.name;
    const isBtDevice = device.isBluetooth || device.name.includes('bluez_');
    const audioType = device.audio_type || (isBtDevice ? 'bluetooth' : '');
    const isBtSource = isBtDevice && device.role === 'source';

    let typeLabel;
    if (isBtSource) {
        
        typeLabel = BT_TYPE_LABELS[device.bt_type] || '';
    } else if (isBtDevice) {
        typeLabel = BT_TYPE_LABELS[device.bt_type] || AUDIO_TYPE_LABELS['bluetooth'] || '';
    } else {
        typeLabel = AUDIO_TYPE_LABELS[audioType] || '';
    }

    let deviceName = device.name;

    const ext = device.extended || {};
    const devApi = ext['device.api'] || '';
    const devBus = ext['device.bus'] || '';
    const alsaCardName = ext['alsa.card_name'] || '';
    const vendorId = ext['device.vendor.id'] || '';
    const productId = ext['device.product.id'] || '';
    const drv = ext['alsa.driver'] || '';
    const devNick = ext['device.nick'] || '';
    const busPath = ext['device.bus_path'] || '';
    const alsaPcmCard = ext['alsa.pcm.card'] || '';
    const alsaPcmDevice = ext['alsa.pcm.device'] || '';
    const alsaCardIdx = ext['alsa.card'] || '';
    const monitorSource = device.monitor_source || '';
    const devIcon = ext['device.icon_name'] || '';
    const devFormFactor = ext['device.form_factor'] || '';
    const devDescription = ext['device.description'] || '';
    const nodeDriver = ext['node.driver'] || '';

    const devDesc = device.description || devNick || '';

    const CH_POS_LABELS = {
        'FL': '前左', 'FR': '前右', 'FC': '前中', 'LFE': '低音',
        'BL': '后左', 'BR': '后右', 'FLC': '前中左', 'FRC': '前中右',
        'BC': '后中', 'SL': '侧左', 'SR': '侧右', 'TC': '顶中',
        'TFL': '顶前左', 'TFC': '顶前中', 'TFR': '顶前右',
        'TBL': '顶后左', 'TBC': '顶后中', 'TBR': '顶后右',
        'MONO': '单声道',
    };

    let chMapText = '-';
    if (device.channels && device.channels.length > 0) {
        chMapText = device.channels.map(c => {
            const pos = c.position || c.channel;
            return CH_POS_LABELS[pos] || c.channel;
        }).join(' / ');
    } else if (device.channel_count) {
        chMapText = `${device.channel_count} 声道`;
    }

    let vendorText = '';
    if (vendorId && productId) {
        vendorText = `VEN_${vendorId}:DEV_${productId}`;
    } else if (vendorId) {
        vendorText = `VEN_${vendorId}`;
    }

    let pcmText = '';
    if (alsaPcmCard && alsaPcmDevice) {
        pcmText = `hw:${alsaPcmCard},${alsaPcmDevice}`;
    } else if (alsaPcmCard) {
        pcmText = `hw:${alsaPcmCard}`;
    }

    const stateText = device.state || (isConnected ? '已连接' : '可用');
    const isInactive = stateText.includes('未激活');

    return `
        <div class="device-card ${isDefault ? 'default-device' : ''} ${isBtDevice ? 'bluetooth-audio' : ''}" data-device="${escapeAttr(deviceName)}">
            <div class="device-header">
                <div class="device-info">
                    <div class="device-name-group">
                        <div class="device-name">${escapeHtml(displayName)}</div>
                        ${devDesc && devDesc !== displayName ? `<div class="device-subname">${escapeHtml(devDesc)}</div>` : ''}
                    </div>
                </div>
                ${isDefault && device.role !== 'source' ? '<span class="status-badge connected default-badge">默认输出</span>' : ''}
                ${isDefault && device.role === 'source' ? '<span class="status-badge connected default-badge">默认输入</span>' : ''}
                ${!isDefault && device.role === 'source' ? '<span class="input-device-badge"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="10" height="10"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/></svg>输入设备</span>' : ''}
                ${typeLabel ? `<span class="status-badge type-badge">${escapeHtml(typeLabel)}</span>` : ''}
                ${isBtDevice && isConnected && !isBtSource ? '<span class="status-badge connected">蓝牙已连接</span>' : ''}
                ${isBtSource && isConnected && !isDefault && !typeLabel ? '<span class="status-badge connected">蓝牙输入</span>' : ''}
            </div>

            <div class="device-details">
                <div class="device-detail-row">
                    <span class="detail-label">状态</span>
                    <span class="detail-value ${isInactive ? 'inactive-state' : ''}">${stateText}</span>
                </div>
                <div class="device-detail-row">
                    <span class="detail-label">驱动</span>
                    <span class="detail-value">${escapeHtml(drv || devApi || 'PipeWire')} ${devBus ? `(${escapeHtml(devBus)})` : ''}</span>
                </div>
                <div class="device-detail-row">
                    <span class="detail-label">采样</span>
                    <span class="detail-value">${[device.sample_format, device.sample_rate ? device.sample_rate + ' Hz' : null, device.channel_count ? device.channel_count + 'ch' : null].filter(Boolean).join(' / ')}</span>
                </div>
                <div class="device-detail-row">
                    <span class="detail-label">声道映射</span>
                    <span class="detail-value mono detail-value-sm">${chMapText}</span>
                </div>
                ${audioType !== 'beeper' && !isBtSource && !needsActivate ? `
                <div class="device-detail-row volume-control-row">
                    <span class="detail-label">音量</span>
                    <div class="volume-control">
                        <button class="mute-btn ${device.muted ? 'muted' : ''}" data-action="toggleMute" data-device="${escapeAttr(deviceName)}">
                            ${device.muted
                                ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>'
                                : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>'
                            }
                        </button>
                        <input type="range" class="volume-slider" min="0" max="100" value="${Math.min(device.volume || 0, 100)}" data-device="${escapeAttr(deviceName)}">
                        <span class="volume-text ${device.muted ? 'muted-text' : ''}">${device.muted ? '静音' : `${Math.min(device.volume || 0, 100)}%`}</span>

                    </div>
                </div>
                ${(() => {
                    const channels = device.channels || [];
                    if (channels.length < 2) return '';
                    const allSame = channels.every(c => c.volume === channels[0].volume);
                    if (allSame) return '';
                    return `<div class="channel-volumes-row">${channels.map((ch, i) =>
                        `<div class="channel-volume-item">
                            <span class="channel-label">${escapeHtml(ch.channel || 'CH' + i)}</span>
                            <input type="range" class="channel-volume-slider" min="0" max="100" value="${ch.volume || 0}" data-device="${escapeAttr(deviceName)}" data-channel="${i}">
                            <span class="channel-vol-text">${ch.volume || 0}%</span>
                        </div>`
                    ).join('')}</div>`;
                })()}
                ` : ''}
                ${audioType !== 'beeper' && !isBtSource && !needsActivate && (device.channel_count || 0) >= 2 ? `
                <div class="device-detail-row volume-control-row">
                    <span class="detail-label">平衡</span>
                    <div class="balance-control">
                        <span class="balance-label">L</span>
                        <input type="range" class="balance-slider" min="-100" max="100" value="${Math.round((device.balance || 0) * 100)}" data-device="${escapeAttr(deviceName)}">
                        <span class="balance-label">R</span>
                        <span class="balance-value" id="balance-val-${device.node_id}">${(() => { const b = device.balance || 0; const abs = Math.round(Math.abs(b) * 100); return b < 0 ? `L ${abs}%` : b > 0 ? `R ${abs}%` : '0'; })()}</span>
                    </div>
                </div>
                ` : ''}
                    ${(device.ports && device.ports.length > 0) ? `
                    <div class="device-detail-row">
                        <span class="detail-label">端口</span>
                        <span class="detail-value"><select class="detail-select audio-port-select" data-device="${escapeAttr(deviceName)}">${device.ports.map(p => `<option value="${escapeAttr(p.name)}" ${p.name === device.active_port ? 'selected' : ''}>${escapeHtml(p.description || p.name)}</option>`).join('')}</select></span>
                    </div>
                    ` : (device.active_port ? `
                    <div class="device-detail-row">
                        <span class="detail-label">端口</span>
                        <span class="detail-value">${escapeHtml(device.active_port)}</span>
                    </div>
                    ` : '')}
                    ${!isBtDevice && !needsActivate ? `
                    <div class="device-detail-row">
                        <span class="detail-label">Profile</span>
                        <span class="detail-value"><select class="detail-select audio-profile-select" data-device="${escapeAttr(deviceName)}">${(() => {
                            const profiles = device.profiles || [];
                            const activeProfile = device.active_profile || '';
                            if (profiles.length === 0) return '<option value="">无可用 Profile</option>';
                            return profiles.map(p => `<option value="${escapeAttr(p.name)}" ${p.name === activeProfile ? 'selected' : ''}>${escapeHtml(p.description || p.name)}</option>`).join('');
                        })()}</select></span>
                    </div>
                    ` : ''}
                    <div class="device-detail-row">
                        <span class="detail-label">节点</span>
                        <span class="detail-value">${device.node_id != null ? '#' + device.node_id : '-'}${device.card_index != null && device.card_index !== device.node_id ? ` / Card ${device.card_index}` : ''}</span>
                    </div>
                    ${alsaCardName ? `<div class="device-detail-row"><span class="detail-label">声卡</span><span class="detail-value">${escapeHtml(alsaCardName)}</span></div>` : ''}
                    ${pcmText ? `<div class="device-detail-row"><span class="detail-label">PCM 设备</span><span class="detail-value mono detail-value-sm">${escapeHtml(pcmText)}</span></div>` : ''}
                    ${vendorText ? `<div class="device-detail-row"><span class="detail-label">硬件ID</span><span class="detail-value mono detail-value-xs">${escapeHtml(vendorText)}</span></div>` : ''}
                    ${busPath ? `<div class="device-detail-row"><span class="detail-label">总线路径</span><span class="detail-value mono detail-value-xs">${escapeHtml(busPath)}</span></div>` : ''}
                    ${devFormFactor ? `<div class="device-detail-row"><span class="detail-label">形态</span><span class="detail-value">${escapeHtml(FORM_FACTOR_LABELS[devFormFactor] || devFormFactor)}</span></div>` : ''}
                    ${devDescription ? `<div class="device-detail-row"><span class="detail-label">设备描述</span><span class="detail-value detail-value-md">${escapeHtml(devDescription)}</span></div>` : ''}
                    ${nodeDriver ? `<div class="device-detail-row"><span class="detail-label">节点驱动</span><span class="detail-value mono detail-value-sm">${escapeHtml(nodeDriver)}</span></div>` : ''}
                    ${monitorSource ? `<div class="device-detail-row"><span class="detail-label">监听源</span><span class="detail-value mono detail-value-sm">${escapeHtml(monitorSource)}</span></div>` : ''}
                    <div class="device-detail-row detail-row-last">
                        <span class="detail-label">通道音量</span>
                        <span class="detail-value mono channel-volumes detail-value-sm">${(device.channels && device.channels.length > 0) ? device.channels.map(c => `${escapeHtml(c.channel)}: ${escapeHtml(String(c.effective_volume ?? c.volume))}%`).join(' / ') : '-'}</span>
                    </div>
            </div>

            <div class="device-actions">
                ${needsActivate ? `<button class="btn btn-accent" data-action="activateDevice" data-device="${escapeAttr(deviceName)}">激活设备</button>` : ''}
                ${!isDefault && !needsActivate && device.role !== 'source' && audioType !== 'beeper' ? `<button class="btn btn-secondary" data-action="setDefault" data-device="${escapeAttr(deviceName)}">设为默认</button>` : ''}
                ${(!needsActivate || audioType === 'beeper') && device.role !== 'source' ? `<button class="btn btn-accent" data-action="playDing" data-device="${escapeAttr(deviceName)}" data-channels="${encodeURIComponent(JSON.stringify((device.channels || []).map(c => ({position: (c.position || c.channel || '').toUpperCase(), label: CH_POS_LABELS[c.position || c.channel] || c.channel}))))}">播放测试</button>` : ''}
                ${isBtDevice && isConnected && !isBtSource ? `<button class="btn btn-danger" data-action="disconnectBtAudio" data-mac="${escapeAttr(device.mac)}">断开</button>` : ''}
            </div>
        </div>
    `;
}

function _renderVideoCard(device, { isDefault }) {
    const typeLabel = VIDEO_TYPE_LABELS[device.video_type] || device.video_type || '';
    const resolution = device.width && device.height ? `${device.width}×${device.height}` : '';
    const fpsText = device.fps > 0 ? `${device.fps} FPS` : '';
    const formatText = device.pixel_format || '';

    const ext = device.extended || {};
    const devApi = ext['device.api'] || '';
    const devBus = ext['device.bus'] || '';
    const vendorId = ext['device.vendor.id'] || ext['vendor_id'] || '';
    const productId = ext['device.product.id'] || ext['product_id'] || '';
    const factoryName = ext['factory.name'] || '';
    const objSerial = ext['object.serial'] || '';
    const busPath = ext['device.bus_path'] || '';
    const prioritySession = ext['priority.session'] || '';
    const priorityDriver = ext['priority.driver'] || '';

    const drmConnectorType = ext['connector_type'] || '';
    const drmConnectorIndex = ext['connector_index'] || '';
    const edidMonitorName = ext['edid_monitor_name'] || '';
    const edidPhysicalSize = ext['edid_physical_size'] || '';
    const edidVendor = ext['edid_vendor'] || '';
    const edidProductId = ext['edid_product_id'] || 0;
    const dpmsStatus = ext['dpms_status'] || '';
    const drmStatus = ext['drm_status'] || '';

    const v4l2Device = ext['v4l2_device'] || '';
    const v4l2Name = ext['v4l2_name'] || '';
    const devFormFactor = ext['device.form_factor'] || '';
    const devIcon = ext['device.icon_name'] || '';
    const devDescription = ext['device.description'] || '';
    const nodeDriver = ext['node.driver'] || '';
    const drmEnabled = ext['drm_enabled'] || '';
    const v4l2Caps = ext['v4l2_caps'] || '';
    const dvSignal = ext['dv_signal'] || false;
    const dvWidth = ext['dv_width'] || 0;
    const dvHeight = ext['dv_height'] || 0;
    const dvFps = ext['dv_fps'] || 0;
    const dvInterlaced = ext['dv_interlaced'] || false;
    const drmConnector = ext['connector'] || '';

    let vendorText = '';
    if (vendorId && productId) {
        vendorText = `VEN_${vendorId}:DEV_${productId}`;
    } else if (vendorId) {
        vendorText = `VEN_${vendorId}`;
    }

    let connInfo = '';
    if (drmConnectorType) {
        const connLabel = drmConnectorType.toUpperCase();
        connInfo = `${connLabel} ${drmConnectorIndex}`;
        if (drmStatus) connInfo += ` (${drmStatus})`;
    }

    const displayName = edidMonitorName || device.friendly_name || device.name;

    return `
        <div class="device-card ${isDefault ? 'default-device' : ''}">
            <div class="device-header">
                <div class="device-info">
                    <div class="device-name-group">
                        <div class="device-name">${escapeHtml(displayName)}</div>
                        ${devDescription && devDescription !== displayName ? `<div class="device-subname">${escapeHtml(devDescription)}</div>` : (v4l2Name && v4l2Name !== displayName ? `<div class="device-subname">${escapeHtml(v4l2Name)}</div>` : '')}
                    </div>
                </div>
                ${isDefault ? '<span class="status-badge connected">默认输出</span>' : ''}
                ${typeLabel ? `<span class="status-badge type-badge">${escapeHtml(typeLabel)}</span>` : ''}
                ${device.role === 'source' ? '<span class="status-badge connected">视频源</span>' : ''}
                ${device.source ? `<span class="status-badge type-badge">${escapeHtml(device.source)}</span>` : ''}
            </div>
            <div class="device-details">
                <div class="device-detail-row">
                    <span class="detail-label">分辨率</span>
                    <span class="detail-value">${resolution || '-'}</span>
                </div>
                <div class="device-detail-row">
                    <span class="detail-label">帧率</span>
                    <span class="detail-value">${fpsText || '-'}</span>
                </div>
                <div class="device-detail-row">
                    <span class="detail-label">像素格式</span>
                    <span class="detail-value mono detail-value-sm">${escapeHtml(formatText) || '-'}</span>
                </div>
                ${connInfo ? `<div class="device-detail-row"><span class="detail-label">连接器</span><span class="detail-value">${escapeHtml(connInfo)}</span></div>` : ''}
                ${edidMonitorName ? `<div class="device-detail-row"><span class="detail-label">显示器名称</span><span class="detail-value">${escapeHtml(edidMonitorName)}</span></div>` : ''}
                ${edidVendor ? `<div class="device-detail-row"><span class="detail-label">厂商</span><span class="detail-value mono">${escapeHtml(edidVendor)}</span></div>` : ''}
                ${edidProductId ? `<div class="device-detail-row"><span class="detail-label">产品ID</span><span class="detail-value mono">0x${edidProductId.toString(16).toUpperCase().padStart(4, '0')}</span></div>` : ''}
                ${edidPhysicalSize ? `<div class="device-detail-row"><span class="detail-label">物理尺寸</span><span class="detail-value">${escapeHtml(edidPhysicalSize)}</span></div>` : ''}
                ${dpmsStatus ? `<div class="device-detail-row"><span class="detail-label">DPMS</span><span class="detail-value">${escapeHtml(dpmsStatus)}</span></div>` : ''}
                ${drmConnector ? (() => {
                    const modes = device.formats || [];
                    if (modes.length === 0) return '';
                    const resMap = {};
                    modes.forEach(m => {
                        const match = m.match(/^(\d+x\d+)(?:@(\d+\.?\d*)Hz)?$/);
                        if (match) {
                            const res = match[1];
                            if (!resMap[res]) resMap[res] = [];
                            if (match[2]) resMap[res].push(match[2]);
                        }
                    });
                    const resOptions = Object.keys(resMap).map(r => `<option value="${escapeAttr(r)}">${escapeHtml(r)}</option>`).join('');
                    const currentRes = resolution.replace('×', 'x');
                    const currentRate = device.fps ? String(Math.round(device.fps)) : '';
                    const formatsJson = escapeAttr(JSON.stringify(modes));
                    return `<div class="device-detail-row"><span class="detail-label">切换分辨率</span><span class="detail-value"><select class="video-select video-res-select" data-connector="${escapeAttr(drmConnector)}" data-current-res="${escapeAttr(currentRes)}" data-current-rate="${escapeAttr(currentRate)}" data-formats="${formatsJson}"><option value="">自动</option>${resOptions}</select></span></div>` +
                           `<div class="device-detail-row"><span class="detail-label">刷新率</span><span class="detail-value"><select class="video-select video-rate-select" data-connector="${escapeAttr(drmConnector)}"><option value="">自动</option></select></span></div>`;
                })() : ''}
                    <div class="device-detail-row">
                        <span class="detail-label">名称</span>
                        <span class="detail-value mono detail-value-sm">${escapeHtml(device.name)}</span>
                    </div>
                    <div class="device-detail-row">
                        <span class="detail-label">媒体类</span>
                        <span class="detail-value">${escapeHtml(device.media_class) || '-'}</span>
                    </div>
                    <div class="device-detail-row">
                        <span class="detail-label">支持格式</span>
                        <span class="detail-value detail-value-sm">${(device.formats && device.formats.length > 0) ? escapeHtml(device.formats.join(', ')) : '-'}</span>
                    </div>
                    <div class="device-detail-row">
                        <span class="detail-label">节点ID</span>
                        <span class="detail-value">${device.node_id != null ? '#' + device.node_id : escapeHtml(v4l2Device || drmConnector || '-')}</span>
                    </div>
                    ${vendorText ? `<div class="device-detail-row"><span class="detail-label">硬件ID</span><span class="detail-value mono detail-value-xs">${escapeHtml(vendorText)}</span></div>` : ''}
                    ${objSerial ? `<div class="device-detail-row"><span class="detail-label">序列号</span><span class="detail-value mono detail-value-sm">${escapeHtml(objSerial)}</span></div>` : ''}
                    ${devApi ? `<div class="device-detail-row"><span class="detail-label">设备 API</span><span class="detail-value mono detail-value-sm">${escapeHtml(devApi)}</span></div>` : ''}
                    ${devBus ? `<div class="device-detail-row"><span class="detail-label">总线</span><span class="detail-value">${escapeHtml(devBus)}</span></div>` : ''}
                    ${busPath ? `<div class="device-detail-row"><span class="detail-label">总线路径</span><span class="detail-value mono detail-value-xs">${escapeHtml(busPath)}</span></div>` : ''}
                    ${prioritySession ? `<div class="device-detail-row"><span class="detail-label">会话优先级</span><span class="detail-value">${escapeHtml(prioritySession)}</span></div>` : ''}
                    ${priorityDriver ? `<div class="device-detail-row"><span class="detail-label">驱动优先级</span><span class="detail-value">${escapeHtml(priorityDriver)}</span></div>` : ''}
                    ${v4l2Device ? `<div class="device-detail-row"><span class="detail-label">V4L2 设备</span><span class="detail-value mono detail-value-sm">${escapeHtml(v4l2Device)}</span></div>` : ''}
                    ${v4l2Name ? `<div class="device-detail-row"><span class="detail-label">V4L2 名称</span><span class="detail-value detail-value-md">${escapeHtml(v4l2Name)}</span></div>` : ''}
                    ${drmConnector && drmConnector !== device.name.replace('drm_', '') ? `<div class="device-detail-row"><span class="detail-label">DRM 连接器</span><span class="detail-value mono detail-value-sm">${escapeHtml(drmConnector)}</span></div>` : ''}
                    ${drmConnector ? `<div class="device-detail-row"><span class="detail-label">DRM 路径</span><span class="detail-value mono detail-value-xs">/sys/class/drm/${escapeHtml(drmConnector)}</span></div>` : ''}
                    ${factoryName ? `<div class="device-detail-row"><span class="detail-label">工厂</span><span class="detail-value mono detail-value-sm">${escapeHtml(factoryName)}</span></div>` : ''}
                    ${devFormFactor ? `<div class="device-detail-row"><span class="detail-label">形态</span><span class="detail-value">${escapeHtml(FORM_FACTOR_LABELS[devFormFactor] || devFormFactor)}</span></div>` : ''}
                    ${devIcon ? `<div class="device-detail-row"><span class="detail-label">图标</span><span class="detail-value mono detail-value-sm">${escapeHtml(devIcon)}</span></div>` : ''}
                    ${devDescription ? `<div class="device-detail-row"><span class="detail-label">设备描述</span><span class="detail-value detail-value-md">${escapeHtml(devDescription)}</span></div>` : ''}
                    ${nodeDriver ? `<div class="device-detail-row"><span class="detail-label">节点驱动</span><span class="detail-value mono detail-value-sm">${escapeHtml(nodeDriver)}</span></div>` : ''}
                    ${drmEnabled ? `<div class="device-detail-row"><span class="detail-label">DRM 启用</span><span class="detail-value">${escapeHtml(drmEnabled)}</span></div>` :''}
                    ${device.video_type === 'hdmi_capture' ? (() => {
                        if (!dvSignal) return '<div class="device-detail-row"><span class="detail-label">输入信号</span><span class="detail-value" style="color:#e74c3c">无信号</span></div>';
                        const dvRes = dvWidth && dvHeight ? `${dvWidth}×${dvHeight}` : '-';
                        const dvFpsText = dvFps > 0 ? `${dvFps} FPS` : '';
                        const dvScan = dvInterlaced ? '隔行' : '逐行';
                        return `<div class="device-detail-row"><span class="detail-label">输入信号</span><span class="detail-value">${dvRes}${dvFpsText ? ' / ' + dvFpsText : ''} / ${dvScan}</span></div>`;
                    })() : ''}
                    ${v4l2Caps ? `<div class="device-detail-row detail-row-last"><span class="detail-label">V4L2 能力</span><span class="detail-value detail-value-sm">${escapeHtml(v4l2Caps)}</span></div>` : ''}
            </div>
            <div class="device-actions">
                ${!isDefault ? `<button class="btn btn-secondary" data-action="setDefaultVideo" data-device="${escapeAttr(device.name)}">设为默认</button>` : ''}
                ${drmConnector ? `<button class="btn btn-sm btn-secondary display-layout-btn" data-connector="${escapeAttr(drmConnector)}" title="设置显示器布局">布局</button>` : ''}
                ${device.video_type === 'camera' ? `<button class="btn btn-sm btn-secondary v4l2-controls-btn" data-device="${escapeAttr(device.name)}" title="调节摄像头参数">参数</button>` : ''}
            </div>
        </div>
    `;
}
