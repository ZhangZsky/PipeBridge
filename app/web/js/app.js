const API_BASE = '';

// CSS.escape polyfill for older WebViews
if (typeof CSS !== 'undefined' && !CSS.escape) {
    CSS.escape = function (value) {
        if (value == null) {
            return '';
        }
        value = String(value);
        var length = value.length;
        var result = '';
        for (var i = 0; i < length; i++) {
            var ch = value.charAt(i);
            var code = value.charCodeAt(i);
            if (code === 0x0000) {
                throw new Error('Invalid character: \\0');
            }
            if ((code >= 0x0001 && code <= 0x001F) || code === 0x007F) {
                result += '\\' + code.toString(16) + ' ';
            } else if (i === 0 && code >= 0x0030 && code <= 0x0039) {
                result += '\\3' + ch + ' ';
            } else if (i === 1 && code >= 0x0030 && code <= 0x0039 && result.charAt(0) === '-') {
                result += '\\3' + ch + ' ';
            } else if (code >= 0x0080 || ch === '-' || ch === '_' || (code >= 0x0030 && code <= 0x0039) || (code >= 0x0041 && code <= 0x005A) || (code >= 0x0061 && code <= 0x007A)) {
                result += ch;
            } else {
                result += '\\' + ch;
            }
        }
        return result;
    };
}

const BT_TYPE_LABELS = {
    'audio-card': '音频设备',
    'audio-headset': '耳机',
    'audio-headphones': '头戴式耳机',
    'audio-speakers': '音箱',
    'input-keyboard': '键盘',
    'input-mouse': '鼠标',
    'input-gaming': '游戏设备',
    'input-tablet': '手写板',
    'phone': '手机',
    'computer': '电脑',
    'printer': '打印机',
    'camera-video': '摄像机',
    'camera-photo': '相机',
    'network-wireless': '无线网络',
    'modem': '调制解调器',
    'scanner': '扫描仪',
    'video-display': '显示器',
    'video-camera': '摄像头'
};

const AUDIO_TYPE_LABELS = {
    'bluetooth': '蓝牙音频',
    'usb': 'USB声卡',
    'hdmi': 'HDMI输出',
    'internal': '内置声卡',
    'beeper': '蜂鸣器',
    'microphone': '麦克风',
    'linein': '线路输入',
    'iec958': 'S/PDIF数字',
};

const VIDEO_TYPE_LABELS = {
    'hdmi': 'HDMI输出',
    'display': '内置屏幕',
    'displayport': 'DisplayPort',
    'vga': 'VGA输出',
    'virtual': '虚拟显示',
    'camera': '摄像头',
    'screen_capture': '屏幕采集',
    'v4l2': 'V4L2设备',
    'loopback': '环回设备',
    'hdmi_capture': 'HDMI采集',
    'capture_card': '采集卡',
    'other': '其他'
};

let currentTab = 'bluetooth';
let isLoading = false;
let scannedDevices = [];
let currentController = null;
let reconnectMonitorData = null;
let reconnectTimer = null;

const FORM_FACTOR_LABELS = {
    'internal': '内置', 'speaker': '音箱', 'headset': '耳机',
    'handset': '手持', 'tv': '电视', 'webcam': '摄像头',
    'microphone': '麦克风', 'car': '车载', 'hifi': 'Hi-Fi',
    'computer': '电脑', 'portable': '便携', 'laptop': '笔记本',
    'headphone': '头戴耳机', 'phone': '手机', 'btspeaker': '蓝牙音箱',
    'monitor': '显示器', 'projector': '投影仪',
};

// 通用提示
function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.animation = 'slideUp 0.3s ease reverse';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

function safeToastData(data, fallback) {
    if (typeof data === 'string') return data;
    if (data && typeof data === 'object' && data.message) return data.message;
    return fallback;
}

let _loadingDeviceMac = null;

function setLoading(loading, action) {
    isLoading = loading;
    const scanBtn = document.getElementById('scanBtn');

    if (!loading) {
        _loadingDeviceMac = null;
        document.querySelectorAll('.device-card.loading').forEach(card => card.classList.remove('loading'));
        if (scanBtn) {
            scanBtn.disabled = false;
            scanBtn.innerHTML = `
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
                </svg>
                扫描
            `;
        }
        const activePanel = document.querySelector('.tab-panel.active');
        if (activePanel) {
            activePanel.querySelectorAll('.device-actions .btn').forEach(btn => {
                btn.disabled = false;
                btn.style.opacity = '';
                btn.textContent = btn.dataset.originalText || btn.textContent;
            });
        }
        return;
    }

    if (action === 'scan') {
        if (scanBtn) {
            scanBtn.disabled = true;
            scanBtn.innerHTML = `
                <svg class="spin-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <circle cx="12" cy="12" r="10"/>
                </svg>
                扫描中...
            `;
        }
        const activePanel = document.querySelector('.tab-panel.active');
        if (activePanel) {
            activePanel.querySelectorAll('.device-actions .btn').forEach(btn => {
                btn.disabled = true;
                btn.style.opacity = '0.5';
            });
        }
    }
}

function setDeviceLoading(mac, loading, label) {
    if (loading) {
        _loadingDeviceMac = mac;
    } else {
        _loadingDeviceMac = null;
    }
    const cards = document.querySelectorAll('.device-card');
    for (const card of cards) {
        const actionBtns = card.querySelectorAll('.device-actions .btn');
        for (const btn of actionBtns) {
            const btnMac = btn.dataset.mac;
            if (btnMac === mac) {
                btn.disabled = loading;
                btn.style.opacity = loading ? '0.5' : '';
                if (loading) {
                    btn.dataset.originalText = btn.textContent;
                    btn.textContent = label || '处理中...';
                } else {
                    btn.textContent = btn.dataset.originalText || btn.textContent;
                }
            } else if (loading) {
                btn.disabled = true;
                btn.style.opacity = '0.5';
            } else {
                btn.disabled = false;
                btn.style.opacity = '';
                btn.textContent = btn.dataset.originalText || btn.textContent;
            }
        }
    }
}

function showScanningState() {
    const container = document.getElementById('bluetoothDeviceList');
    if (container) {
        container.innerHTML = `
            <div class="scanning-state">
                <div class="scanning-animation">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M6.5 6.5l11 11L12 23V1l5.5 5.5-11 11"/>
                    </svg>
                    <div class="scanning-waves">
                        <span></span><span></span><span></span>
                    </div>
                </div>
                <p>正在扫描附近蓝牙设备...</p>
            </div>
        `;
    }
}

function showPairingState(mac, deviceName) {
    const container = document.getElementById('bluetoothDeviceList');
    const displayName = deviceName || mac;
    if (container) {
        container.innerHTML = `
            <div class="scanning-state pairing-state">
                <div class="pairing-animation">
                    <span class="pairing-dot"></span>
                    <span class="pairing-dot"></span>
                    <span class="pairing-dot"></span>
                </div>
                <p>正在与 <strong>${displayName}</strong> 配对...</p>
            </div>
        `;
    }
}

async function apiCall(endpoint, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), endpoint.includes('/bluetooth/connect') ? 60000 : 30000);
    try {
        const response = await fetch(`${API_BASE}${endpoint}`, {
            ...options,
            headers: {
                ...(options.body ? {'Content-Type': 'application/json'} : {}),
                ...options.headers
            },
            signal: options.signal || controller.signal
        });
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return await response.json();
    } catch (error) {
        if (error.name === 'AbortError') {
            throw new Error('请求超时');
        }
        throw error;
    } finally {
        clearTimeout(timeout);
    }
}

// 统一设备卡片渲染
function renderDeviceCard(type, device, options = {}) {
    const { isDefault, defaultSink, defaultSource, pwMacs } = options;

    if (type === 'bluetooth') {
        return _renderBtCard(device);
    } else if (type === 'audio') {
        return _renderAudioCard(device, { isDefault, defaultSink, defaultSource, pwMacs });
    } else if (type === 'video') {
        return _renderVideoCard(device, { isDefault });
    }
    return '';
}

// 蓝牙设备卡片
function _renderBtCard(device) {
    const isPaired = device._isPaired || false;
    const isConnected = device._isConnected || false;
    const deviceType = device._deviceType || '';
    const displayName = device._displayName || '未知蓝牙设备';
    const typeLabel = BT_TYPE_LABELS[deviceType] || (deviceType ? deviceType : '');
    const deviceVendor = device._vendor || '';
    const deviceBattery = device._battery || '';
    const deviceRssi = device._rssi || '';
    const deviceTxPower = device._txPower || '';
    const isTrusted = device._trusted || false;
    const servicesResolved = device._servicesResolved || false;
    const deviceModalias = device._modalias || '';
    const deviceUuid = device._uuid || [];
    const deviceBlocked = device._blocked || false;
    const deviceClass = device._deviceClass || '';
    const adapterPath = device._adapterPath || '';
    const deviceIconName = device._icon || '';
    const deviceAppearance = device._appearance || '';
    const deviceAddressType = device._addressType || '';
    const deviceManufacturerId = device._manufacturerId || '';

    let rssiHtml = '';
    if (deviceRssi) {
        const rssiVal = parseInt(deviceRssi);
        const bars = rssiVal > -50 ? 5 : rssiVal > -65 ? 4 : rssiVal > -80 ? 3 : rssiVal > -90 ? 2 : 1;
        let barColor = rssiVal > -65 ? '#22c55e' : rssiVal > -80 ? '#eab308' : '#ef4444';
        let barHtml = '';
        for (let i = 0; i < 5; i++) {
            barHtml += `<span class="signal-bar ${i < bars ? 'active' : ''}" style="${i < bars ? `background:${barColor}` : ''}"></span>`;
        }
        rssiHtml = `<div class="device-detail-row"><span class="detail-label">信号</span><span class="detail-value"><span class="signal-bars">${barHtml}</span> ${deviceRssi}</span></div>`;
    }

    return `
        <div class="device-card ${isConnected ? 'connected' : ''} ${isLoading ? 'loading' : ''}">
            <div class="device-header">
                <div class="device-info">
                    <div class="device-name">${displayName}</div>
                    ${isPaired ? `
                    <button class="btn-rename" data-action="rename" data-mac="${device.mac}" data-name="${displayName.replace(/"/g, '&quot;')}" title="重命名设备">✎</button>
                    ` : ''}
                </div>
                ${isPaired ? `
                    <span class="status-badge disconnected">已配对</span>
                    ${isConnected ? '<span class="status-badge connected">已连接</span>' : ''}
                ` : ''}
                ${isConnected && reconnectMonitorData?.monitoring && isAudioDeviceType(deviceType) ? '<span class="status-badge reconnect-monitor">自动重连</span>' : ''}
            </div>
            <div class="device-details">
                ${(() => {
                    const rows = [];
                    if (typeLabel) rows.push(`<div class="device-detail-row"><span class="detail-label">类型</span><span class="detail-value">${typeLabel}</span></div>`);
                    rows.push(`<div class="device-detail-row"><span class="detail-label">MAC</span><span class="detail-value mono">${device.mac}</span></div>`);
                    if (rssiHtml) rows.push(rssiHtml);
                    if (deviceAppearance) rows.push(`<div class="device-detail-row"><span class="detail-label">外观</span><span class="detail-value">${deviceAppearance}</span></div>`);
                    if (deviceAddressType) rows.push(`<div class="device-detail-row"><span class="detail-label">地址</span><span class="detail-value">${deviceAddressType}</span></div>`);
                    if (deviceVendor) rows.push(`<div class="device-detail-row"><span class="detail-label">厂商</span><span class="detail-value">${deviceVendor}</span></div>`);
                    if (deviceBattery) rows.push(`<div class="device-detail-row"><span class="detail-label">电量</span><span class="detail-value">${deviceBattery}</span></div>`);
                    if (deviceTxPower) rows.push(`<div class="device-detail-row"><span class="detail-label">发射功率</span><span class="detail-value">${deviceTxPower}</span></div>`);
                    if (deviceModalias) rows.push(`<div class="device-detail-row"><span class="detail-label">设备ID</span><span class="detail-value mono" style="font-size:0.65rem;word-break:break-all">${deviceModalias}</span></div>`);
                    if (deviceManufacturerId) rows.push(`<div class="device-detail-row"><span class="detail-label">厂商ID</span><span class="detail-value mono" style="font-size:0.65rem">${deviceManufacturerId}</span></div>`);
                    if (deviceUuid.length > 0) rows.push(`<div class="device-detail-row"><span class="detail-label">UUID</span><span class="detail-value mono" style="font-size:0.6rem;word-break:break-all">${deviceUuid.join(', ')}</span></div>`);
                    if (deviceBlocked) rows.push(`<div class="device-detail-row"><span class="detail-label">阻塞</span><span class="detail-value" style="color:var(--color-warning, #d97706)">是</span></div>`);
                    if (deviceIconName) rows.push(`<div class="device-detail-row"><span class="detail-label">图标</span><span class="detail-value mono" style="font-size:0.65rem">${deviceIconName}</span></div>`);
                    if (deviceClass) rows.push(`<div class="device-detail-row"><span class="detail-label">设备类</span><span class="detail-value mono" style="font-size:0.65rem">${deviceClass}</span></div>`);
                    if (adapterPath) rows.push(`<div class="device-detail-row" style="border-bottom:none;margin-bottom:0"><span class="detail-label">适配器</span><span class="detail-value mono" style="font-size:0.6rem">${adapterPath}</span></div>`);
                    if (rows.length <= 3) return rows.join('');
                    return rows.slice(0, 3).join('') +
                        '<div class="device-detail-toggle" data-action="toggleDetails"><span>详细信息</span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg></div>' +
                        '<div class="device-detail-advanced">' + rows.slice(3).join('') + '</div>';
                })()}
            </div>
            <div class="device-actions">
                ${!isPaired ? `
                    <button class="btn btn-primary" data-action="pair" data-mac="${device.mac}">配对</button>
                ` : `
                    ${isConnected ? `
                        <button class="btn btn-secondary" data-action="disconnect" data-mac="${device.mac}">断开</button>
                    ` : `
                        <button class="btn btn-secondary" data-action="connect" data-mac="${device.mac}">连接</button>
                    `}
                    <button class="btn btn-danger" data-action="remove" data-mac="${device.mac}">删除</button>
                `}
            </div>
        </div>
    `;
}

// 音频设备卡片
function _renderAudioCard(device, { isDefault, defaultSink, defaultSource, pwMacs }) {
    const needsActivate = device.needsActivate === true;
    const isConnected = device.connected === true;
    const displayName = device.friendly_name || device.name;
    const isBtDevice = device.isBluetooth || device.name.includes('bluez_');
    const audioType = device.audio_type || (isBtDevice ? 'bluetooth' : '');

    let typeLabel;
    if (isBtDevice) {
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
        <div class="device-card ${isDefault ? 'default-device' : ''} ${isBtDevice ? 'bluetooth-audio' : ''}" data-device="${deviceName}">
            <div class="device-header">
                <div class="device-info">
                    <div class="device-name">${displayName}</div>
                    ${devDesc && devDesc !== displayName ? `<div class="device-subname">${devDesc}</div>` : ''}
                </div>
                ${isDefault && device.role !== 'source' ? '<span class="status-badge connected">默认输出</span>' : ''}
                ${isDefault && device.role === 'source' ? '<span class="status-badge connected">默认输入</span>' : ''}
                ${!isDefault && device.role === 'source' ? '<span class="status-badge">音频输入</span>' : ''}
                ${typeLabel ? `<span class="status-badge type-badge">${typeLabel}</span>` : ''}
                ${isBtDevice && isConnected ? '<span class="status-badge connected">蓝牙已连接</span>' : ''}
            </div>

            <div class="device-details">
                <div class="device-detail-row">
                    <span class="detail-label">状态</span>
                    <span class="detail-value ${isInactive ? 'inactive-state' : ''}">${stateText}</span>
                </div>
                <div class="device-detail-row">
                    <span class="detail-label">驱动</span>
                    <span class="detail-value">${drv || devApi || 'PipeWire'} ${devBus ? `(${devBus})` : ''}</span>
                </div>
                <div class="device-detail-row">
                    <span class="detail-label">采样</span>
                    <span class="detail-value">${[device.sample_format, device.sample_rate ? device.sample_rate + ' Hz' : null, device.channel_count ? device.channel_count + 'ch' : null].filter(Boolean).join(' / ')}</span>
                </div>
                <div class="device-detail-row">
                    <span class="detail-label">声道映射</span>
                    <span class="detail-value mono" style="font-size:0.65rem">${chMapText}</span>
                </div>
                ${(device.ports && device.ports.length > 0) ? `
                <div class="device-detail-row">
                    <span class="detail-label">端口</span>
                    <span class="detail-value" style="font-size:0.65rem">${device.ports.map(p => p.name === device.active_port ? `<strong>${p.description || p.name}</strong>` : (p.description || p.name)).join(' | ')}</span>
                </div>
                ` : (device.active_port ? `
                <div class="device-detail-row">
                    <span class="detail-label">端口</span>
                    <span class="detail-value">${device.active_port}</span>
                </div>
                ` : '')}
                ${audioType !== 'beeper' ? `
                <div class="device-detail-row volume-control-row">
                    <span class="detail-label">音量</span>
                    <div class="volume-control">
                        <button class="mute-btn ${device.muted ? 'muted' : ''}" data-action="toggleMute" data-device="${deviceName}">
                            ${device.muted
                                ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>'
                                : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>'
                            }
                        </button>
                        <input type="range" class="volume-slider" min="0" max="${Math.max(100, device.volume || 0)}" value="${device.volume || 0}" data-device="${deviceName}">
                        <span class="volume-text ${device.muted ? 'muted-text' : ''}">${device.muted ? '静音' : `${device.volume || 0}%`}</span>
                        ${device.volume_db ? `<span class="volume-db">${device.volume_db > 0 ? '+' : ''}${device.volume_db} dB</span>` : ''}
                    </div>
                </div>
                ` : ''}
                ${audioType !== 'beeper' && (device.channel_count || 0) >= 2 ? `
                <div class="device-detail-row volume-control-row">
                    <span class="detail-label">平衡</span>
                    <div class="balance-control">
                        <span class="balance-label">L</span>
                        <input type="range" class="balance-slider" min="-100" max="100" value="${Math.round((device.balance || 0) * 100)}" data-device="${deviceName}">
                        <span class="balance-label">R</span>
                        <span class="balance-value" id="balance-val-${device.node_id}">${(() => { const b = device.balance || 0; const abs = Math.round(Math.abs(b) * 100); return b < 0 ? `L ${abs}%` : b > 0 ? `R ${abs}%` : '0'; })()}</span>
                    </div>
                </div>
                ` : ''}
                <div class="device-detail-toggle" data-action="toggleDetails">
                    <span>详细信息</span>
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
                </div>
                <div class="device-detail-advanced">
                    <div class="device-detail-row">
                        <span class="detail-label">节点</span>
                        <span class="detail-value">${device.node_id != null ? '#' + device.node_id : '-'}${device.card_index != null && device.card_index !== device.node_id ? ` / Card ${device.card_index}` : ''}</span>
                    </div>
                    ${alsaCardName ? `<div class="device-detail-row"><span class="detail-label">ALSA 卡</span><span class="detail-value">${alsaCardName}</span></div>` : ''}
                    ${pcmText ? `<div class="device-detail-row"><span class="detail-label">PCM 设备</span><span class="detail-value mono" style="font-size:0.65rem">${pcmText}</span></div>` : ''}
                    ${vendorText ? `<div class="device-detail-row"><span class="detail-label">硬件ID</span><span class="detail-value mono" style="font-size:0.6rem">${vendorText}</span></div>` : ''}
                    ${busPath ? `<div class="device-detail-row"><span class="detail-label">总线路径</span><span class="detail-value mono" style="font-size:0.6rem">${busPath}</span></div>` : ''}
                    ${devFormFactor ? `<div class="device-detail-row"><span class="detail-label">形态</span><span class="detail-value">${FORM_FACTOR_LABELS[devFormFactor] || devFormFactor}</span></div>` : ''}
                    ${devDescription ? `<div class="device-detail-row"><span class="detail-label">设备描述</span><span class="detail-value" style="font-size:0.7rem">${devDescription}</span></div>` : ''}
                    ${nodeDriver ? `<div class="device-detail-row"><span class="detail-label">节点驱动</span><span class="detail-value mono" style="font-size:0.65rem">${nodeDriver}</span></div>` : ''}
                    ${monitorSource ? `<div class="device-detail-row"><span class="detail-label">监听源</span><span class="detail-value mono" style="font-size:0.65rem">${monitorSource}</span></div>` : ''}
                    ${(device.ports && device.ports.length > 0) ? `<div class="device-detail-row"><span class="detail-label">可用端口</span><span class="detail-value" style="font-size:0.65rem">${device.ports.map(p => p.description || p.name).join(', ')}</span></div>` : ''}
                    <div class="device-detail-row" style="border-bottom:none;margin-bottom:0">
                        <span class="detail-label">通道音量</span>
                        <span class="detail-value mono channel-volumes" style="font-size:0.65rem">${(device.channels && device.channels.length > 0) ? device.channels.map(c => `${c.channel}: ${c.effective_volume ?? c.volume}%`).join(' / ') : '-'}</span>
                    </div>
                </div>
            </div>

            <div class="device-actions">
                ${needsActivate ? `<button class="btn btn-accent" data-action="activateDevice" data-device="${deviceName}">激活设备</button>` : ''}
                ${!isDefault && !needsActivate && device.role !== 'source' ? `<button class="btn btn-secondary" data-action="setDefault" data-device="${deviceName}">设为默认</button>` : ''}
                ${!needsActivate && device.role !== 'source' ? `<button class="btn btn-accent" data-action="playDing" data-device="${deviceName}" data-channels="${encodeURIComponent(JSON.stringify((device.channels || []).map(c => ({position: (c.position || c.channel || '').toUpperCase(), label: CH_POS_LABELS[c.position || c.channel] || c.channel}))))}">播放测试</button>` : ''}
                ${isBtDevice && isConnected ? `<button class="btn btn-danger" data-action="disconnectBtAudio" data-mac="${device.mac}">断开</button>` : ''}
            </div>
        </div>
    `;
}

// 视频设备卡片
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

    // DRM 专属扩展信息
    const drmConnectorType = ext['connector_type'] || '';
    const drmConnectorIndex = ext['connector_index'] || '';
    const edidMonitorName = ext['edid_monitor_name'] || '';
    const edidPhysicalSize = ext['edid_physical_size'] || '';
    const dpmsStatus = ext['dpms_status'] || '';
    const drmStatus = ext['drm_status'] || '';

    // V4L2 专属扩展信息
    const v4l2Device = ext['v4l2_device'] || '';
    const v4l2Name = ext['v4l2_name'] || '';
    const devFormFactor = ext['device.form_factor'] || '';
    const devIcon = ext['device.icon_name'] || '';
    const devDescription = ext['device.description'] || '';
    const nodeDriver = ext['node.driver'] || '';
    const drmEnabled = ext['drm_enabled'] || '';
    const v4l2Caps = ext['v4l2_caps'] || '';
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

    let edidInfo = '';
    if (edidMonitorName) {
        edidInfo = edidMonitorName;
        if (edidPhysicalSize) edidInfo += ` / ${edidPhysicalSize}`;
    }

    return `
        <div class="device-card ${isDefault ? 'default-device' : ''}">
            <div class="device-header">
                <div class="device-info">
                    <div class="device-name">${device.friendly_name || device.name}</div>
                </div>
                ${isDefault ? '<span class="status-badge connected">默认输出</span>' : ''}
                ${typeLabel ? `<span class="status-badge type-badge">${typeLabel}</span>` : ''}
                ${device.role === 'source' ? '<span class="status-badge connected">视频源</span>' : ''}
                ${device.source ? `<span class="status-badge type-badge">${device.source}</span>` : ''}
                ${edidInfo ? `<span class="status-badge type-badge">${edidInfo}</span>` : ''}
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
                    <span class="detail-value mono" style="font-size:0.65rem">${formatText || '-'}</span>
                </div>
                ${connInfo ? `<div class="device-detail-row"><span class="detail-label">连接器</span><span class="detail-value">${connInfo}</span></div>` : ''}
                ${dpmsStatus ? `<div class="device-detail-row"><span class="detail-label">DPMS</span><span class="detail-value">${dpmsStatus}</span></div>` : ''}
                <div class="device-detail-toggle" data-action="toggleDetails">
                    <span>详细信息</span>
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
                </div>
                <div class="device-detail-advanced">
                    <div class="device-detail-row">
                        <span class="detail-label">名称</span>
                        <span class="detail-value mono" style="font-size:0.65rem">${device.name}</span>
                    </div>
                    <div class="device-detail-row">
                        <span class="detail-label">媒体类</span>
                        <span class="detail-value">${device.media_class || '-'}</span>
                    </div>
                    <div class="device-detail-row">
                        <span class="detail-label">支持格式</span>
                        <span class="detail-value" style="font-size:0.65rem">${(device.formats && device.formats.length > 0) ? device.formats.join(', ') : '-'}</span>
                    </div>
                    <div class="device-detail-row">
                        <span class="detail-label">节点ID</span>
                        <span class="detail-value">${device.node_id != null ? '#' + device.node_id : (v4l2Device || drmConnector || '-')}</span>
                    </div>
                    ${vendorText ? `<div class="device-detail-row"><span class="detail-label">硬件ID</span><span class="detail-value mono" style="font-size:0.6rem">${vendorText}</span></div>` : ''}
                    ${objSerial ? `<div class="device-detail-row"><span class="detail-label">序列号</span><span class="detail-value mono" style="font-size:0.65rem">${objSerial}</span></div>` : ''}
                    ${devApi ? `<div class="device-detail-row"><span class="detail-label">设备 API</span><span class="detail-value mono" style="font-size:0.65rem">${devApi}</span></div>` : ''}
                    ${devBus ? `<div class="device-detail-row"><span class="detail-label">总线</span><span class="detail-value">${devBus}</span></div>` : ''}
                    ${busPath ? `<div class="device-detail-row"><span class="detail-label">总线路径</span><span class="detail-value mono" style="font-size:0.6rem">${busPath}</span></div>` : ''}
                    ${prioritySession ? `<div class="device-detail-row"><span class="detail-label">会话优先级</span><span class="detail-value">${prioritySession}</span></div>` : ''}
                    ${priorityDriver ? `<div class="device-detail-row"><span class="detail-label">驱动优先级</span><span class="detail-value">${priorityDriver}</span></div>` : ''}
                    ${v4l2Device ? `<div class="device-detail-row"><span class="detail-label">V4L2 设备</span><span class="detail-value mono" style="font-size:0.65rem">${v4l2Device}</span></div>` : ''}
                    ${v4l2Name ? `<div class="device-detail-row"><span class="detail-label">V4L2 名称</span><span class="detail-value" style="font-size:0.7rem">${v4l2Name}</span></div>` : ''}
                    ${drmConnector && drmConnector !== device.name.replace('drm_', '') ? `<div class="device-detail-row"><span class="detail-label">DRM 连接器</span><span class="detail-value mono" style="font-size:0.65rem">${drmConnector}</span></div>` : ''}
                    ${drmConnector ? `<div class="device-detail-row"><span class="detail-label">DRM 路径</span><span class="detail-value mono" style="font-size:0.6rem">/sys/class/drm/${drmConnector}</span></div>` : ''}
                    ${factoryName ? `<div class="device-detail-row"><span class="detail-label">工厂</span><span class="detail-value mono" style="font-size:0.65rem">${factoryName}</span></div>` : ''}
                    ${devFormFactor ? `<div class="device-detail-row"><span class="detail-label">形态</span><span class="detail-value">${FORM_FACTOR_LABELS[devFormFactor] || devFormFactor}</span></div>` : ''}
                    ${devIcon ? `<div class="device-detail-row"><span class="detail-label">图标</span><span class="detail-value mono" style="font-size:0.65rem">${devIcon}</span></div>` : ''}
                    ${devDescription ? `<div class="device-detail-row"><span class="detail-label">设备描述</span><span class="detail-value" style="font-size:0.7rem">${devDescription}</span></div>` : ''}
                    ${nodeDriver ? `<div class="device-detail-row"><span class="detail-label">节点驱动</span><span class="detail-value mono" style="font-size:0.65rem">${nodeDriver}</span></div>` : ''}
                    ${drmEnabled ? `<div class="device-detail-row"><span class="detail-label">DRM 启用</span><span class="detail-value">${drmEnabled}</span></div>` : ''}
                    ${v4l2Caps ? `<div class="device-detail-row" style="border-bottom:none;margin-bottom:0"><span class="detail-label">V4L2 能力</span><span class="detail-value" style="font-size:0.65rem">${v4l2Caps}</span></div>` : ''}
                </div>
            </div>
            <div class="device-actions">
                ${!isDefault ? `<button class="btn btn-secondary" data-action="setDefaultVideo" data-device="${device.name}">设为默认</button>` : ''}
            </div>
        </div>
    `;
}

// 蓝牙相关
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
        await updateBluetoothStatus();
    } catch (error) {
        showToast('可发现设置失败: ' + error.message, 'error');
        const switchEl = document.getElementById('discoverableSwitch');
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

            let _renameDone = false;  // 防止 finishRename 重复执行
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
                    _renameDone = true;  // 阻止 blur 再次触发
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

async function connectDevice(mac) {
    setDeviceLoading(mac, true, '连接中...');
    try {
        const result = await apiCall('/api/bluetooth/connect', {
            method: 'POST',
            body: JSON.stringify({ mac: mac })
        });
        if (result.success) {
            setDeviceLoading(mac, false);  // 连接成功，恢复按钮状态
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
            } else {
                showToast(err, 'error');
            }
            setDeviceLoading(mac, false);
        }
    } catch (error) {
        showToast('连接失败: ' + error.message, 'error');
        setDeviceLoading(mac, false);
    }
}

async function handlePairWithPin() {
    const pin = document.getElementById('pinInput').value.trim();
    if (!pin) {
        showToast('请输入 PIN 码', 'error');
        return;
    }
    if (currentPairingMac) {
        await pairDevice(currentPairingMac, pin);
    }
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
    }
}

// 音频相关
async function getAudioDevices() {
    try {
        const data = await apiCall('/api/audio/devices');
        const result = data.data || { devices: [], default: '', default_source: '' };
        if (data.error) result.error = data.error;
        return result;
    } catch (error) {
        return { devices: [], default: '', default_source: '', error: error.message };
    }
}

function isAudioDeviceType(deviceType) {
    return ['audio-card', 'audio-headset', 'audio-headphones', 'audio-speakers'].includes(deviceType);
}

async function scanAudioDevices() {
    try {
        const data = await apiCall('/api/audio/scan', { method: 'POST' });
        const result = data.data || { devices: [], default: '', default_source: '' };
        if (data.error) result.error = data.error;
        return result;
    } catch (error) {
        return { devices: [], default: '', default_source: '', error: error.message };
    }
}

async function setDefaultDevice(deviceName) {
    const btn = document.querySelector(`[data-action="setDefault"][data-device="${deviceName}"]`);
    if (btn) { btn.disabled = true; btn.style.opacity = '0.5'; }
    try {
        const result = await apiCall('/api/audio/default', {
            method: 'POST',
            body: JSON.stringify({ device: deviceName })
        });
        showToast(result.data || '已设为默认设备', 'success');
        await renderAudioDevices(true);
    } catch (error) {
        showToast('设置默认设备失败: ' + error.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.style.opacity = ''; }
    }
}

async function activateAudioDevice(deviceName) {
    const btn = document.querySelector(`[data-action="activateDevice"][data-device="${CSS.escape(deviceName)}"]`);
    if (btn) { btn.disabled = true; btn.textContent = '激活中...'; }
    try {
        const result = await apiCall('/api/audio/activate', {
            method: 'POST',
            body: JSON.stringify({ device: deviceName })
        });
        if (result.success) {
            showToast(result.data || '设备已激活', 'success');
            await renderAudioDevices(true);
        } else {
            showToast(result.error || '激活失败', 'error');
        }
    } catch (error) {
        showToast('激活设备失败: ' + error.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '激活设备'; }
    }
}

let _channelTestStop = {};

async function playTestSound(deviceName, channels) {
    if (_channelTestStop[deviceName]) {
        _channelTestStop[deviceName] = false;
        return;
    }
    const btn = document.querySelector(`[data-action="playDing"][data-device="${CSS.escape(deviceName)}"], [data-action="testSound"][data-device="${CSS.escape(deviceName)}"]`);
    if (!channels || !channels.length) {
        showToast('未找到设备声道信息', 'error');
        return;
    }
    if (btn && btn.isConnected) { btn.style.opacity = '0.5'; btn.textContent = '停止测试'; }
    _channelTestStop[deviceName] = false;
    try {
        const tested = [];
        for (let i = 0; i < channels.length; i++) {
            if (_channelTestStop[deviceName]) break;
            const ch = channels[i];
            if (btn && btn.isConnected) { btn.textContent = `测试: ${ch.label} (${i + 1}/${channels.length})`; }
            try {
                const result = await apiCall('/api/audio/test-channel', {
                    method: 'POST',
                    body: JSON.stringify({ device: deviceName, position: ch.position })
                });
                if (result.success) {
                    tested.push(ch.label);
                }
            } catch (e) {}
            if (i < channels.length - 1 && !_channelTestStop[deviceName]) {
                await new Promise(r => setTimeout(r, 300));
            }
        }
        if (_channelTestStop[deviceName]) {
            showToast(`测试已停止，已测试: ${tested.join(', ')}`, 'info');
        } else {
            showToast(`声道测试完成: ${tested.join(', ')}`, 'success');
        }
    } catch (error) {
        showToast('播放测试音失败: ' + error.message, 'error');
    } finally {
        delete _channelTestStop[deviceName];
        if (btn && btn.isConnected) { btn.style.opacity = ''; btn.textContent = '播放测试'; }
    }
}

let _volumeTimers = {};

function _updateChannelDisplay(deviceName, channels, volume) {
    if (!deviceName) return;
    const card = document.querySelector(`.device-card[data-device="${CSS.escape(deviceName)}"]`);
    if (!card) return;
    if (channels && channels.length) {
        const chEl = card.querySelector('.channel-volumes');
        if (chEl) {
            chEl.textContent = channels.map(c => `${c.channel}: ${c.effective_volume ?? c.volume}%`).join(' / ');
        }
    }
    if (volume !== undefined && volume !== null) {
        const volText = card.querySelector('.volume-text');
        if (volText && !volText.classList.contains('muted-text')) {
            volText.textContent = `${volume}%`;
        }
        const slider = card.querySelector('.volume-slider');
        if (slider) {
            slider.value = Math.min(volume, 100);
        }
    }
}

async function setVolume(deviceName, volume) {
    const key = deviceName || '__default__';
    if (_volumeTimers[key]) clearTimeout(_volumeTimers[key]);
    _volumeTimers[key] = setTimeout(async () => {
        try {
            const result = await apiCall('/api/audio/volume', {
                method: 'POST',
                body: JSON.stringify({ device: deviceName, volume })
            });
            if (result.success && result.channels) {
                _updateChannelDisplay(deviceName, result.channels, result.verified_volume);
            }
        } catch (error) {
            showToast('设置音量失败: ' + error.message, 'error');
        }
        delete _volumeTimers[key];
    }, 200);
}

async function toggleMute(deviceName) {
    const btn = document.querySelector(`[data-action="toggleMute"][data-device="${CSS.escape(deviceName)}"]`);
    if (!btn) return;
    const wasMuted = btn.classList.contains('muted');
    const card = btn.closest('.device-card');
    const slider = card?.querySelector('.volume-slider');

    const svgMuted = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>';
    const svgUnmuted = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>';

    btn.classList.toggle('muted', !wasMuted);
    btn.innerHTML = !wasMuted ? svgMuted : svgUnmuted;
    if (card) {
        const volText = card.querySelector('.volume-text');
        if (volText) {
            volText.textContent = wasMuted ? `${slider?.value || 0}%` : '静音';
            volText.classList.toggle('muted-text', !wasMuted);
        }
    }

    try {
        await apiCall('/api/audio/mute', {
            method: 'POST',
            body: JSON.stringify({ device: deviceName, mute: !wasMuted })
        });
    } catch (error) {
        btn.classList.toggle('muted', wasMuted);
        btn.innerHTML = wasMuted ? svgMuted : svgUnmuted;
        if (card) {
            const volText = card.querySelector('.volume-text');
            if (volText) {
                volText.textContent = wasMuted ? '静音' : `${slider?.value || 0}%`;
                volText.classList.toggle('muted-text', wasMuted);
            }
        }
        showToast('设置静音失败: ' + error.message, 'error');
    }
}

let _balanceTimers = {};

async function setBalance(deviceName, balance) {
    const key = deviceName || '__default__';
    if (_balanceTimers[key]) clearTimeout(_balanceTimers[key]);
    _balanceTimers[key] = setTimeout(async () => {
        try {
            const result = await apiCall('/api/audio/balance', {
                method: 'POST',
                body: JSON.stringify({ device: deviceName, balance: balance / 100 })
            });
            if (!result.success) {
                showToast('设置平衡失败: ' + (result.error || '未知错误'), 'error');
            } else if (result.channels) {
                _updateChannelDisplay(deviceName, result.channels);
            }
        } catch (error) {
            showToast('设置平衡失败: ' + error.message, 'error');
            renderAudioDevices();
        }
        delete _balanceTimers[key];
    }, 200);
}

// 视频相关
async function getVideoDevices() {
    try {
        const result = await apiCall('/api/video/devices');
        return result.data || { devices: [] };
    } catch (e) {
        return { devices: [] };
    }
}

async function scanVideoDevices() {
    try {
        const data = await apiCall('/api/video/scan', { method: 'POST' });
        const result = data.data || { devices: [], default: '' };
        if (data.error) result.error = data.error;
        return result;
    } catch (error) {
        return { devices: [], default: '', error: error.message };
    }
}

async function setDefaultVideoDevice(deviceName) {
    const btn = document.querySelector(`[data-action="setDefaultVideo"][data-device="${deviceName}"]`);
    if (btn) { btn.disabled = true; btn.style.opacity = '0.5'; }
    try {
        const result = await apiCall('/api/video/default', {
            method: 'POST',
            body: JSON.stringify({ device: deviceName })
        });
        showToast(result.data || '已设为默认视频设备', 'success');
        await renderVideoDevices(true);
    } catch (error) {
        showToast('设置默认视频设备失败: ' + error.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.style.opacity = ''; }
    }
}

// 渲染视频设备列表
async function renderVideoDevices(forceScan = false) {
    const container = document.getElementById('videoDeviceList');
    if (!container) return;

    const result = forceScan ? await scanVideoDevices() : await getVideoDevices();
    const devices = result.devices || [];
    const defaultVideo = result.default || '';

    if (devices.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polygon points="23 7 16 12 23 17 23 7"/>
                    <rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
                </svg>
                <p>暂无视频设备</p>
                <p class="empty-state-sub">HDMI 显示器或摄像头等设备连接后会自动显示</p>
            </div>
        `;
        return;
    }

    _renderVideoList(container, devices, defaultVideo);
}

function _renderVideoList(container, devices, defaultVideo) {
    container.innerHTML = devices.map(device => {
        const isDefault = device.is_default || device.name === defaultVideo;
        return renderDeviceCard('video', device, { isDefault });
    }).join('');
    _bindVideoActions(container);
}

function _bindVideoActions(container) {
    container.querySelectorAll('.btn[data-action]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            if (isLoading) return;
            const action = e.currentTarget.dataset.action;
            if (action === 'setDefaultVideo') {
                await setDefaultVideoDevice(e.currentTarget.dataset.device);
            }
        });
    });
}

// 更新蓝牙状态
async function updateBluetoothStatus() {
    try {
        const status = await fetchBluetoothStatus();
        const data = status.data || {};

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
            'service_running': '服务运行中',
            'hardware_detected': '检测到硬件',
            'not_detected': '未检测到蓝牙',
            'error': '状态异常'
        };

        overallText.textContent = statusTexts[data.status] || '未知状态';

        if (data.status === 'active') {
            overallDot.className = 'status-dot active';
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
            scanBtn.style.display = (data.status === 'active' || data.status === 'service_running') ? 'inline-flex' : 'none';
        }

        if (data.controllers && data.controllers.length > 0) {
            const ctrl = data.controllers[0];
            currentController = ctrl;
            const isPowered = ctrl.powered;
            const isUp = ctrl.status === 'UP';
            const isDiscoverable = ctrl.discoverable;

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
            }

            if (controllerInfoInline) controllerInfoInline.innerHTML = '';

            if (controllersGrid) {
                controllersGrid.innerHTML = `
                    <div class="controller-card collapsed">
                        <div class="controller-summary" id="controllerSummary">
                            <div class="controller-summary-left">
                                <div class="status-dot ${isPowered ? 'active' : ''}"></div>
                                <span class="controller-summary-name">${ctrl.alias || ctrl.name || '-'}</span>
                                <span class="controller-summary-sep">·</span>
                                <span class="controller-summary-bus">${ctrl.type ? ctrl.type + ' / ' : ''}${ctrl.bus || '-'}</span>
                                <span class="controller-summary-sep">·</span>
                                <span class="controller-summary-mac">${ctrl.mac || '-'}</span>
                            </div>
                            <svg class="controller-expand-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <polyline points="6 9 12 15 18 9"/>
                            </svg>
                        </div>
                        <div class="controller-detail">
                            <div class="controller-info">
                                <div class="info-item"><span class="info-label">MAC 地址</span><span class="info-value mono">${ctrl.mac || '-'}</span></div>
                                <div class="info-item"><span class="info-label">控制器名称</span><span class="info-value mono">${ctrl.name || '-'}</span></div>
                                <div class="info-item"><span class="info-label">别名</span><span class="info-value">${ctrl.alias || '-'}</span></div>
                                <div class="info-item"><span class="info-label">总线类型</span><span class="info-value">${ctrl.bus || '-'}</span></div>
                                <div class="info-item"><span class="info-label">控制器类型</span><span class="info-value">${ctrl.type || '-'}</span></div>
                                <div class="info-item"><span class="info-label">制造商</span><span class="info-value">${ctrl.manufacturer || '-'}${ctrl.manufacturer_id ? ' (' + ctrl.manufacturer_id + ')' : ''}</span></div>
                                <div class="info-item"><span class="info-label">HCI 版本</span><span class="info-value">${ctrl.hci_version || '-'}${ctrl.hci_revision ? ' ' + ctrl.hci_revision : ''}</span></div>
                                <div class="info-item"><span class="info-label">设备类</span><span class="info-value mono">${ctrl.device_class || '-'}</span></div>
                                <div class="info-item"><span class="info-label">功能特征</span><span class="info-value" style="font-size:0.6rem;word-break:break-all">${ctrl.features || '-'}</span></div>
                                <div class="info-item"><span class="info-label">数据包类型</span><span class="info-value" style="font-size:0.6rem;word-break:break-all">${ctrl.packet_types || '-'}</span></div>
                                <div class="info-item"><span class="info-label">链路策略</span><span class="info-value">${ctrl.link_policy || '-'}</span></div>
                                <div class="info-item"><span class="info-label">链路模式</span><span class="info-value">${ctrl.link_mode || '-'}</span></div>
                                <div class="info-item"><span class="info-label">电源</span><span class="info-value ${isPowered ? 'success' : 'warning'}">${isPowered ? '开启' : '关闭'}</span></div>
                                <div class="info-item"><span class="info-label">可发现</span><span class="info-value ${isDiscoverable ? 'success' : ''}">${isDiscoverable ? '是' : '否'}</span></div>
                            </div>
                        </div>
                    </div>
                `;
                const summaryEl = document.getElementById('controllerSummary');
                if (summaryEl) {
                    summaryEl.addEventListener('click', () => {
                        summaryEl.closest('.controller-card').classList.toggle('collapsed');
                    });
                }
            }
        } else {
            currentController = null;
            if (powerToggle) powerToggle.style.display = 'none';
            if (discoverableToggle) discoverableToggle.style.display = 'none';
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
}

// 渲染蓝牙设备列表
async function renderBluetoothDevices(devices) {
    const container = document.getElementById('bluetoothDeviceList');
    if (!container) return;
    scannedDevices = devices || [];

    const pairedDevices = await getPairedDevices();
    const pairedMap = new Map(pairedDevices.map(d => [d.mac, d]));

    const allDevices = [...pairedDevices];
    for (const d of scannedDevices) {
        if (!pairedMap.has(d.mac)) allDevices.push(d);
    }

    const deviceCountEl = document.getElementById('deviceCount');
    if (deviceCountEl) {
        deviceCountEl.textContent = allDevices.length > 0 ? `${allDevices.length} 个设备` : '';
    }

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
        // 准备蓝牙设备的上下文信息
        device._isPaired = !!pairedInfo || device.paired === true;
        device._isConnected = pairedInfo?.connected === true || device.connected === true;
        device._deviceType = device.type || pairedInfo?.type || pairedInfo?.icon || '';
        device._vendor = pairedInfo?.vendor || '';
        device._battery = pairedInfo?.battery || '';
        device._trusted = pairedInfo?.trusted || false;
        device._rssi = pairedInfo?.rssi || (device.rssi != null ? device.rssi + ' dBm' : '');
        device._txPower = pairedInfo?.tx_power || '';
        device._servicesResolved = pairedInfo?.services_resolved || false;
        device._modalias = pairedInfo?.modalias || '';
        device._uuid = pairedInfo?.uuid || [];
        device._blocked = pairedInfo?.blocked || false;
        device._deviceClass = pairedInfo?.device_class || '';
        device._adapterPath = pairedInfo?.adapter_path || '';
        device._icon = pairedInfo?.icon || '';
        device._appearance = pairedInfo?.appearance || '';
        device._addressType = pairedInfo?.address_type || '';
        device._manufacturerId = pairedInfo?.manufacturer_id || '';

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

        return renderDeviceCard('bluetooth', device);
    }).join('');

    container.querySelectorAll('.btn[data-action], .btn-rename[data-action]').forEach(btn => {
        btn.addEventListener('click', handleDeviceAction);
    });
}

// 从设备列表中提取 PipeWire MAC 集合
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

// 渲染音频设备列表
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
                    bt_type: bt.type || bt.icon || ''
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
    let html = allAudioDevices.map(device => {
        const isDefault = device.is_default || device.name === defaultSink || (device.role === 'source' && device.name === defaultSource);
        return renderDeviceCard('audio', device, { isDefault, defaultSink, defaultSource, pwMacs });
    }).join('');

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
                await disconnectDevice(mac);  // disconnectDevice 内部已调用 renderAudioDevices
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
        slider.addEventListener('input', updateText);
        slider.addEventListener('change', async (e) => {
            if (isLoading) return;
            await setVolume(e.currentTarget.dataset.device, parseInt(e.currentTarget.value));
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
}

// Tab 切换
function switchTab(tabName) {
    currentTab = tabName;

    document.querySelectorAll('.tab-item').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tabName);
    });

    document.querySelectorAll('.tab-panel').forEach(panel => {
        panel.classList.toggle('active', panel.id === `${tabName}Tab`);
    });

    if (tabName === 'bluetooth') {
        updateBluetoothStatus();
        if (scannedDevices.length === 0) loadInitialDevices();
        startBtStatusRefresh();
        startReconnectPolling();
    } else if (tabName === 'audio') {
        if (!lastAudioSnapshot) renderAudioDevices();
        startAudioRefresh();
    } else if (tabName === 'video') {
        renderVideoDevices();
    } else if (tabName === 'system') {
        startDependencyRefresh();
        // 并行请求重连状态和系统概览
        Promise.all([pollReconnectStatus(), fetchSystemOverview()]).then(([_, overviewData]) => {
            if (overviewData) renderSystemOverview(overviewData);
        });
    }
}

let audioRefreshTimer = null;
let btStatusRefreshTimer = null;
let lastBtSnapshot = '';
let lastAudioSnapshot = '';

function startAudioRefresh() {
    if (audioRefreshTimer) return;
    audioRefreshTimer = setInterval(async () => {
        if (currentTab === 'audio') {
            try {
                const audioResult = await getAudioDevices();
                const devices = audioResult.devices || [];
                if (devices.length > 0) {
                    const snapshot = devices.map(d => `${d.name}|${d.state}|${d.volume || 0}|${(d.channels || []).map(c => c.effective_volume || c.volume || 0).join(',')}${audioResult.default}`).join(';');
                    if (snapshot !== lastAudioSnapshot) {
                        lastAudioSnapshot = snapshot;
                        const activeSlider = document.activeElement;
                        const isAdjustingVolume = activeSlider && activeSlider.classList.contains('volume-slider');
                        const isAdjustingBalance = activeSlider && activeSlider.classList.contains('balance-slider');
                        if (isAdjustingVolume || isAdjustingBalance) {
                            _updateAudioDevicesInPlace(devices, audioResult);
                        } else {
                            renderAudioDevices();
                        }
                    }
                }
            } catch (e) {}
        }
    }, 3000);
}

function _updateAudioDevicesInPlace(devices, audioResult) {
    const defaultName = audioResult.default || '';
    devices.forEach(d => {
        const card = document.querySelector(`.device-card[data-device="${CSS.escape(d.name)}"]`);
        if (!card) return;
        const slider = card.querySelector('.volume-slider');
        if (slider && document.activeElement !== slider) {
            slider.value = d.volume || 0;
        }
        const volText = card.querySelector('.volume-text');
        if (volText && !volText.classList.contains('muted-text')) {
            volText.textContent = `${d.volume || 0}%`;
        }
        const muteBtn = card.querySelector('.mute-btn');
        if (muteBtn) {
            muteBtn.classList.toggle('muted', d.muted);
        }
        const chEl = card.querySelector('.channel-volumes');
        if (chEl && d.channels && d.channels.length) {
            chEl.textContent = d.channels.map(c => `${c.channel}: ${c.effective_volume ?? c.volume}%`).join(' / ');
        }
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
    });
}

function startBtStatusRefresh() {
    if (btStatusRefreshTimer) return;
    btStatusRefreshTimer = setInterval(async () => {
        if (currentTab === 'bluetooth') {
            try {
                const pairedDevices = await getPairedDevices();
                const snapshot = pairedDevices.map(d => `${d.mac}|${d.connected}`).join(';');
                if (snapshot !== lastBtSnapshot) {
                    lastBtSnapshot = snapshot;
                    if (scannedDevices.length > 0) {
                        _mergePairedIntoScanned(pairedDevices);
                        await renderBluetoothDevices(scannedDevices);
                    } else {
                        // 未扫描过但有已配对设备变化（如手机主动连接），自动加载
                        _mergePairedIntoScanned(pairedDevices);
                        await renderBluetoothDevices(scannedDevices);
                    }
                }
            } catch (e) {}
        }
    }, 3000);
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
            scannedDevices[idx] = { ...scannedDevices[idx], ...info, connected: info.connected ?? scannedDevices[idx].connected };
        } else {
            scannedDevices.push({ ...info });
        }
    }
}

function startKeepAlive() {
    setInterval(async () => {
        try {
            const result = await apiCall('/api/bluetooth/keep-alive', { method: 'POST' });
            const connected = (result.data && result.data.connected) || [];
            const snapshot = connected.map(d => d.mac).sort().join(';');
            const prevConnected = (scannedDevices || []).filter(d => d.connected);
            const prevSnapshot = prevConnected.map(d => d.mac).sort().join(';');
            if (snapshot !== prevSnapshot) {
                if (currentTab === 'bluetooth') {
                    const pairedDevices = await getPairedDevices();
                    lastBtSnapshot = pairedDevices.map(d => `${d.mac}|${d.connected}`).join(';');
                    _mergePairedIntoScanned(pairedDevices);
                    await renderBluetoothDevices(scannedDevices);
                }
            }
        } catch (e) {}
    }, 60000);
}

async function pollReconnectStatus() {
    try {
        const result = await apiCall('/api/bluetooth/reconnect/status');
        reconnectMonitorData = result.data || {};
        updateReconnectIndicator();
    } catch (e) {}
}

function updateReconnectIndicator() {
    const indicator = document.getElementById('reconnectIndicator');
    if (!indicator) return;
    const devices = reconnectMonitorData?.reconnecting_devices || [];
    const count = devices.length;
    if (count > 0) {
        indicator.style.display = 'flex';
        const countEl = indicator.querySelector('.reconnect-count');
        if (countEl) countEl.textContent = count;
        const detailEl = indicator.querySelector('.reconnect-detail');
        if (detailEl) {
            const names = devices.map(d => d.name || d.mac).slice(0, 3).join(', ');
            detailEl.textContent = names + (devices.length > 3 ? '...' : '');
        }
    } else {
        indicator.style.display = 'none';
    }
}

function createReconnectIndicator() {
    const scanBtn = document.getElementById('scanBtn');
    const statusRight = scanBtn ? scanBtn.parentElement : document.querySelector('.status-right');
    if (!statusRight) return;
    const indicator = document.createElement('div');
    indicator.id = 'reconnectIndicator';
    indicator.className = 'reconnect-indicator';
    indicator.style.display = 'none';
    indicator.innerHTML = `
        <svg class="reconnect-icon spin-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="23 4 23 10 17 10"/>
            <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
        </svg>
        <span>正在重连 <span class="reconnect-count">0</span> 个设备</span>
        <span class="reconnect-detail"></span>
    `;
    statusRight.appendChild(indicator);
}

function startReconnectPolling() {
    if (reconnectTimer) return;
    pollReconnectStatus();
    reconnectTimer = setInterval(pollReconnectStatus, 5000);
}

async function toggleAutoReconnect(enabled) {
    try {
        const result = await apiCall('/api/system/reconnect', {
            method: 'POST',
            body: JSON.stringify({ enabled: enabled })
        });
        if (result.success) {
            showToast(enabled ? '自动重连已开启' : '自动重连已关闭', 'success');
            reconnectMonitorData = reconnectMonitorData || {};
            reconnectMonitorData.monitoring = enabled;
            const toggle = document.getElementById('autoReconnectSwitch');
            if (toggle) toggle.checked = enabled;
            const desc = document.getElementById('reconnectDesc');
            if (desc) desc.textContent = enabled ? '监控中' : '已禁用';
            await pollReconnectStatus();
        } else {
            showToast(result.error || '操作失败', 'error');
            const toggle = document.getElementById('autoReconnectSwitch');
            if (toggle) toggle.checked = !enabled;
        }
    } catch (error) {
        showToast('操作失败: ' + error.message, 'error');
        const toggle = document.getElementById('autoReconnectSwitch');
        if (toggle) toggle.checked = !enabled;
    }
}

// 系统概览
let dependencyRefreshTimer = null;

async function fetchSystemOverview() {
    try {
        const result = await apiCall('/api/system/overview');
        const data = result.data || {};
        return data;
    } catch (e) {
        return null;
    }
}

function renderSystemOverview(data) {
    const container = document.getElementById('systemOverview');
    const fixAllBtn = document.getElementById('fixAllBtn');
    if (!container) return;

    if (!data) {
        container.innerHTML = '<div class="empty-state"><p>无法获取系统状态</p></div>';
        return;
    }

    const allOk = data.all_ok;
    if (fixAllBtn) fixAllBtn.style.display = allOk ? 'none' : 'inline-flex';

    // 顶部状态卡片行
    const pwRunning = !!data.pipewire;
    const wpRunning = !!data.wireplumber;
    const btRunning = !!data.bluetooth_service;
    const btAudioReady = !!data.bluetooth_audio_ready;
    const spaPluginOk = !!data.spa_bluetooth_plugin;

    let statusRow = `<div class="overview-status-row">
        ${_overviewCard('PipeWire', pwRunning ? '运行中' : '未运行', pwRunning ? 'ok' : 'error', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>')}
        ${_overviewCard('WirePlumber', wpRunning ? '运行中' : '未运行', wpRunning ? 'ok' : 'error', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9"/></svg>')}
        ${_overviewCard('蓝牙服务', btRunning ? '运行中' : '未运行', btRunning ? 'ok' : 'error', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6.5 6.5l11 11L12 23V1l5.5 5.5-11 11"/></svg>')}
        ${_overviewCard('蓝牙音频', btAudioReady ? '就绪' : (spaPluginOk ? '未就绪' : '插件缺失'), btAudioReady ? 'ok' : (spaPluginOk ? 'warning' : 'error'), '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6.5 6.5l11 11L12 23V1l5.5 5.5-11 11"/></svg>')}
    </div>`;

    // 中间统计行
    const audioCount = (data.audio_devices && data.audio_devices.count) || 0;
    const audioDefault = (data.audio_devices && data.audio_devices.default) || '';
    const videoCount = (data.video_devices && data.video_devices.count) || 0;
    const videoDefault = (data.video_devices && data.video_devices.default) || '';
    const btConnected = data.bluetooth_connected || 0;

    let statsRow = `<div class="overview-stats-row">
        ${_overviewStat(audioCount, '音频设备', audioDefault || '')}
        ${_overviewStat(videoCount, '视频设备', videoDefault || '')}
        ${_overviewStat(btConnected, '已连接蓝牙')}
    </div>`;

    // 收集所有依赖项状态（用于折叠时显示圆点）
    const deps = data.dependencies || {};
    const depDots = [];
    function _collectDepDot(name, ok, critical) {
        depDots.push({ name, ok: !!ok, error: !ok && !!critical });
    }
    if (deps.pipewire) _collectDepDot('PipeWire', deps.pipewire.running, true);
    if (data.wireplumber !== undefined) {
        // wireplumber 状态从 overview 数据取
        const wpRunning = !!data.wireplumber;
        _collectDepDot('WirePlumber', wpRunning, true);
    }
    (deps.packages || []).forEach(p => { _collectDepDot(p.name, p.installed, p.critical); });
    (deps.services || []).forEach(s => { _collectDepDot(s.name, s.active, s.critical); });
    (deps.commands || []).forEach(c => { _collectDepDot(c.name, c.exists, c.critical); });
    if (deps.spa_bluetooth_plugin !== undefined) _collectDepDot('SPA插件', deps.spa_bluetooth_plugin, false);
    // python 包单独标记
    ['python3-dbus', 'python3-gi', 'python3-fastapi', 'python3-uvicorn'].forEach(n => {
        const p = (deps.packages || []).find(x => x.name === n);
        if (p) _collectDepDot(p.name, p.installed, p.critical);
    });

    // 取最重要的两个依赖（PipeWire 和蓝牙服务）在折叠栏显示
    const pw = deps.pipewire;
    const pwOk = pw && pw.running;
    const btSvc = (deps.services || []).find(s => s.type === 'bluetooth') || {};
    const btOk = btSvc.active;

    const dotHtml = depDots.map(d =>
        `<span class="dep-dot ${d.error ? 'error' : (d.ok ? 'ok' : 'warn')}" title="${d.name}: ${d.error ? '异常' : (d.ok ? '正常' : '警告')}"></span>`
    ).join('');

    // 展开后的完整内容（不含"所有依赖正常"行，该行始终显示）
    let depContentHtml = '';
    if (!allOk && deps.critical_missing && deps.critical_missing.length > 0) {
        depContentHtml = `<div class="dependency-warning"><strong>缺少关键依赖:</strong> ${deps.critical_missing.join(', ')}</div>`;
    }
    const depContent = depContentHtml + _renderDepSections({...deps, spa_bluetooth_plugin: data.spa_bluetooth_plugin, auto_reconnect: data.auto_reconnect});

    // "所有依赖正常"行始终显示
    const allOkBanner = allOk
        ? '<div class="dependency-all-ok"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg><span>所有依赖正常</span></div>'
        : '';

    container.innerHTML = statusRow + statsRow + allOkBanner + `
        <div class="controller-card collapsed" id="depCard">
            <div class="controller-summary" id="depSummary">
                <div class="controller-summary-left">
                    <div class="status-dot ${allOk ? 'active' : ''}"></div>
                    <span class="controller-summary-name">依赖详情</span>
                    <span class="dep-summary-items">
                        <span class="dep-summary-item ${pwOk ? 'ok' : 'error'}" title="PipeWire: ${pwOk ? '运行中' : '未运行'}">PW ${pwOk ? 'ON' : 'OFF'}</span>
                        <span class="dep-summary-item ${btOk ? 'ok' : 'error'}" title="蓝牙服务: ${btOk ? '运行中' : '未运行'}">BT ${btOk ? 'ON' : 'OFF'}</span>
                    </span>
                    <span class="dep-dots">${dotHtml}</span>
                </div>
                <svg class="controller-expand-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="6 9 12 15 18 9"/>
                </svg>
            </div>
            <div class="controller-detail">${depContent}</div>
        </div>
    `;

    document.getElementById('depSummary').addEventListener('click', () => {
        document.getElementById('depCard').classList.toggle('collapsed');
    });

    const autoReconnectSwitch = document.getElementById('autoReconnectSwitch');
    if (autoReconnectSwitch) {
        autoReconnectSwitch.addEventListener('change', (e) => {
            toggleAutoReconnect(e.target.checked);
        });
    }
}

function _overviewCard(title, statusText, statusClass, iconSvg) {
    return `<div class="overview-card">
        <div class="overview-card-icon ${statusClass}">${iconSvg}</div>
        <div class="overview-card-info">
            <div class="overview-card-title">${title}</div>
            <div class="overview-card-status ${statusClass}">${statusText}</div>
        </div>
    </div>`;
}

function _overviewStat(value, label, sub) {
    return `<div class="overview-stat">
        <div class="overview-stat-value">${value}</div>
        <div class="overview-stat-label">${label}${sub ? '<span class="overview-stat-sub">' + sub + '</span>' : ''}</div>
    </div>`;
}

function _renderDepSections(deps) {
    function renderItem(item, type) {
        let statusClass, statusText;
        if (type === 'package') {
            statusClass = item.installed ? 'status-ok' : (item.critical ? 'status-error' : 'status-warning');
            statusText = item.installed ? '已安装' : '未安装';
        } else if (type === 'service') {
            statusClass = item.active ? 'status-ok' : (item.critical ? 'status-error' : 'status-warning');
            statusText = item.active ? '运行中' : '未运行';
        } else {
            statusClass = item.exists ? 'status-ok' : (item.critical ? 'status-error' : 'status-warning');
            statusText = item.exists ? '可用' : '不可用';
        }
        return `<div class="dependency-item ${statusClass}"><span class="dep-name">${item.name}</span><span class="dep-desc">${item.desc}</span><span class="dep-status">${statusText}</span></div>`;
    }

    function filterByType(items, typeVal) {
        return (items || []).filter(i => i.type === typeVal);
    }

    let pwItems = '';
    if (deps.pipewire) {
        const pw = deps.pipewire;
        const cls = pw.running ? 'status-ok' : 'status-error';
        const txt = pw.running ? '运行中' : '未运行';
        pwItems += `<div class="dependency-item ${cls}"><span class="dep-name">pipewire</span><span class="dep-desc">${pw.desc}</span><span class="dep-status">${txt}</span></div>`;
    }

    let audioCoreItems = filterByType(deps.packages, 'audio-core').map(p => renderItem(p, 'package')).join('');
    let audioToolItems = filterByType(deps.commands, 'audio-core').map(c => renderItem(c, 'command')).join('');

    let btStackItems = '';
    filterByType(deps.packages, 'bluetooth').forEach(p => { btStackItems += renderItem(p, 'package'); });
    filterByType(deps.services, 'bluetooth').forEach(s => { btStackItems += renderItem(s, 'service'); });
    if (deps.spa_bluetooth_plugin !== undefined) {
        const spaOk = deps.spa_bluetooth_plugin;
        btStackItems += `<div class="dependency-item ${spaOk ? 'status-ok' : 'status-error'}"><span class="dep-name">SPA 蓝牙插件</span><span class="dep-desc">libspa-bluetooth .so 文件</span><span class="dep-status">${spaOk ? '已加载' : '缺失'}</span></div>`;
    }

    let btToolItems = '';
    filterByType(deps.commands, 'bluetooth').forEach(c => { btToolItems += renderItem(c, 'command'); });

    let systemItems = '';
    filterByType(deps.services, 'system').forEach(s => { systemItems += renderItem(s, 'service'); });

    let pythonBindItems = filterByType(deps.packages, 'python').filter(p => ['python3-dbus', 'python3-gi'].includes(p.name)).map(p => renderItem(p, 'package')).join('');
    let pythonWebItems = filterByType(deps.packages, 'python').filter(p => ['python3-fastapi', 'python3-uvicorn'].includes(p.name)).map(p => renderItem(p, 'package')).join('');

    // 自动重连开关（放入左列）
    let reconnectItems = '';
    const ar = deps.auto_reconnect;
    const isMonitoring = reconnectMonitorData?.monitoring || (ar && ar.monitoring) || false;
    const reconnectDevices = (ar && ar.reconnecting_devices) || [];
    const manualDisconnects = (ar && ar.manual_disconnects) || [];
    reconnectItems += `<div class="dependency-item ${isMonitoring ? 'status-ok' : ''}"><span class="dep-name">蓝牙音频自动重连</span><span class="dep-desc" id="reconnectDesc">${isMonitoring ? '监控中' : '已禁用'}</span><span class="dep-status"><label class="switch"><input type="checkbox" id="autoReconnectSwitch" ${isMonitoring ? 'checked' : ''}><span class="slider"></span></label></span></div>`;
    if (reconnectDevices.length > 0) {
        reconnectItems += `<div class="dependency-item status-warning"><span class="dep-name">等待重连</span><span class="dep-desc">${reconnectDevices.join(', ')}</span><span class="dep-status">等待中</span></div>`;
    }
    if (manualDisconnects.length > 0) {
        reconnectItems += `<div class="dependency-item"><span class="dep-name">手动断开</span><span class="dep-desc">${manualDisconnects.join(', ')}</span><span class="dep-status">已忽略</span></div>`;
    }

    let leftCol = '';
    leftCol += '<div class="dependency-section"><h3>▸ PipeWire 服务状态</h3><div class="dependency-list">' + pwItems + '</div></div>';
    leftCol += '<div class="dependency-section"><h3>▸ 音频核心组件</h3><div class="dependency-list">' + audioCoreItems + '</div></div>';
    leftCol += '<div class="dependency-section"><h3>▸ 音频工具</h3><div class="dependency-list">' + audioToolItems + '</div></div>';
    leftCol += '<div class="dependency-section"><h3>▸ 自动重连</h3><div class="dependency-list">' + reconnectItems + '</div></div>';

    let rightCol = '';
    rightCol += '<div class="dependency-section"><h3>▸ 蓝牙协议栈</h3><div class="dependency-list">' + btStackItems + '</div></div>';
    rightCol += '<div class="dependency-section"><h3>▸ 蓝牙工具</h3><div class="dependency-list">' + btToolItems + '</div></div>';
    if (systemItems) {
        rightCol += '<div class="dependency-section"><h3>▸ 系统服务</h3><div class="dependency-list">' + systemItems + '</div></div>';
    }
    rightCol += '<div class="dependency-section"><h3>▸ Python 系统绑定</h3><div class="dependency-list">' + pythonBindItems + '</div></div>';
    rightCol += '<div class="dependency-section"><h3>▸ Python Web 框架</h3><div class="dependency-list">' + pythonWebItems + '</div></div>';

    return '<div class="dependency-row"><div class="dependency-column">' + leftCol + '</div><div class="dependency-column">' + rightCol + '</div></div>';
}

async function fixAllDependencies() {
    const fixAllBtn = document.getElementById('fixAllBtn');
    fixAllBtn.disabled = true;
    fixAllBtn.textContent = '修复中...';
    try {
        showToast('正在检查和修复依赖...', 'info');
        const result = await apiCall('/api/system/fix', { method: 'POST' });
        if (result.success) {
            const data = result.data || result;
            const messages = [];
            if (data.packages) {
                messages.push(data.packages.success ? '系统包: ' + data.packages.message : '系统包: ' + (data.packages.error || '失败'));
            }
            if (data.pipewire) {
                messages.push(data.pipewire.success ? 'PipeWire: ' + data.pipewire.message : 'PipeWire: ' + (data.pipewire.error || '失败'));
            }
            if (data.services) {
                messages.push(data.services.success ? '服务: ' + data.services.message : '服务: ' + (data.services.error || '失败'));
            }
            if (data.bluetooth_audio) {
                messages.push(data.bluetooth_audio.success ? '蓝牙音频: ' + (data.bluetooth_audio.message || '已修复') : '蓝牙音频: ' + (data.bluetooth_audio.error || '修复失败'));
            }
            if (messages.length > 0) {
                showToast(messages.join(' | '), messages.some(m => m.includes('失败')) ? 'error' : 'success');
            } else {
                showToast('修复完成', 'success');
            }
            const overview = await fetchSystemOverview();
            if (overview) renderSystemOverview(overview);
        } else {
            showToast('修复失败: ' + (result.error || '未知错误'), 'error');
        }
    } catch (e) {
        showToast('修复失败: ' + e.message, 'error');
    } finally {
        fixAllBtn.disabled = false;
        fixAllBtn.textContent = '一键修复';
    }
}

function startDependencyRefresh() {
    if (dependencyRefreshTimer) return;
    dependencyRefreshTimer = setInterval(async () => {
        if (currentTab === 'system') {
            const data = await fetchSystemOverview();
            if (data) renderSystemOverview(data);
        }
    }, 30000);
}

function initTimers() {
    startAudioRefresh();
}

document.addEventListener('DOMContentLoaded', () => {
    document.addEventListener('click', (e) => {
        const toggle = e.target.closest('.device-detail-toggle');
        if (toggle) {
            const card = toggle.closest('.device-card');
            const isExpanding = !card.classList.contains('expanded');
            const container = card.parentElement;
            if (container && isExpanding) {
                container.querySelectorAll('.device-card.expanded').forEach(c => {
                    if (c !== card) c.classList.remove('expanded');
                });
            }
            card.classList.toggle('expanded');
        }
    });

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

    // 第二步：后台并行刷新实时数据
    Promise.all([
        updateBluetoothStatus(),
        renderAudioDevices(),
        loadInitialDevices(),
        renderVideoDevices(),
        fetchSystemOverview().then(data => {
            if (data) renderSystemOverview(data);
        })
    ]).catch(e => console.warn('初始化部分失败:', e));
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
