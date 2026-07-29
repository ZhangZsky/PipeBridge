const API_BASE = window.location.origin;

// HTML 转义工具函数，防止设备名等动态文本导致 XSS 或 DOM 破损
function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// 转义用于 HTML 属性值的字符串（用于 data-* 属性拼接）
function escapeAttr(str) {
    return escapeHtml(str);
}

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
    'bluetooth-source': '蓝牙输入',
    'usb': 'USB声卡',
    'hdmi': 'HDMI输出',
    'internal': '内置声卡',
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
    // 去重：相同消息不重复添加
    if (container.querySelector(`.toast-${type}`)?.textContent === message) return;
    // 上限：最多 5 条
    while (container.children.length >= 5) container.firstChild.remove();
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
                btn.innerHTML = btn.dataset.originalHtml || btn.innerHTML;
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
                    btn.dataset.originalHtml = btn.innerHTML;
                    btn.textContent = label || '处理中...';
                } else {
                    btn.innerHTML = btn.dataset.originalHtml || btn.innerHTML;
                }
            } else if (loading) {
                btn.disabled = true;
                btn.style.opacity = '0.5';
            } else if (btn.dataset.originalHtml) {
                btn.innerHTML = btn.dataset.originalHtml;
                delete btn.dataset.originalHtml;
            }
        }
    }
}

let _scanTimer = null;

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
                <p>正在扫描附近蓝牙设备... <span id="scanElapsed">0s</span></p>
            </div>
        `;
        let elapsed = 0;
        if (_scanTimer) clearInterval(_scanTimer);
        _scanTimer = setInterval(() => {
            elapsed++;
            const el = document.getElementById('scanElapsed');
            if (el) el.textContent = elapsed + 's';
        }, 1000);
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
        const contentType = response.headers.get('content-type') || '';
        if (!contentType.includes('application/json')) {
            throw new Error(`服务器返回非 JSON 响应 (HTTP ${response.status})`);
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

// 在 HTML 字符串生成时就处理折叠：超过 maxVisible 的行设置 display:none，末尾添加展开按钮
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

// 蓝牙设备卡片
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
            </div>
            <div class="device-details">
                ${(() => {
                    const rows = [];
                    if (typeLabel) rows.push(`<div class="device-detail-row"><span class="detail-label">类型</span><span class="detail-value">${typeLabel}</span></div>`);
                    rows.push(`<div class="device-detail-row"><span class="detail-label">MAC</span><span class="detail-value mono">${device.mac}</span></div>`);
                    if (rssiHtml) rows.push(rssiHtml);
                    // 已连接的音频设备：显示 Profile 选择器和麦克风开关
                    if (isConnected && isAudioDeviceType(deviceType)) {
                        rows.push(`<div class="device-detail-row"><span class="detail-label">音频模式</span><span class="detail-value"><select class="detail-select bt-profile-select" data-mac="${device.mac}"><option value="">加载中...</option></select></span></div>`);
                        rows.push(`<div class="device-detail-row"><span class="detail-label">麦克风</span><span class="detail-value"><button class="btn btn-sm bt-mic-toggle" data-mac="${device.mac}" data-enabled="false">关闭</button></span></div>`);
                    }
                    if (deviceAppearance) rows.push(`<div class="device-detail-row"><span class="detail-label">外观</span><span class="detail-value">${deviceAppearance}</span></div>`);
                    if (deviceAddressType) rows.push(`<div class="device-detail-row"><span class="detail-label">地址</span><span class="detail-value">${deviceAddressType}</span></div>`);
                    if (deviceVendor) rows.push(`<div class="device-detail-row"><span class="detail-label">厂商</span><span class="detail-value">${deviceVendor}</span></div>`);
                    if (deviceBattery) rows.push(`<div class="device-detail-row"><span class="detail-label">电量</span><span class="detail-value">${deviceBattery}</span></div>`);
                    if (deviceTxPower) rows.push(`<div class="device-detail-row"><span class="detail-label">发射功率</span><span class="detail-value">${deviceTxPower}</span></div>`);
                    if (deviceModalias) rows.push(`<div class="device-detail-row"><span class="detail-label">设备ID</span><span class="detail-value mono detail-value-sm">${deviceModalias}</span></div>`);
                    if (deviceManufacturerId) rows.push(`<div class="device-detail-row"><span class="detail-label">厂商ID</span><span class="detail-value mono detail-value-sm">${deviceManufacturerId}</span></div>`);
                    if (deviceUuid.length > 0) rows.push(`<div class="device-detail-row"><span class="detail-label">UUID</span><span class="detail-value mono detail-value-xs">${deviceUuid.join(', ')}</span></div>`);

                    if (deviceIconName) rows.push(`<div class="device-detail-row"><span class="detail-label">图标</span><span class="detail-value mono detail-value-sm">${deviceIconName}</span></div>`);
                    if (deviceClass) rows.push(`<div class="device-detail-row"><span class="detail-label">设备类</span><span class="detail-value mono detail-value-sm">${deviceClass}</span></div>`);
                    if (adapterPath) rows.push(`<div class="device-detail-row detail-row-last"><span class="detail-label">适配器</span><span class="detail-value mono detail-value-xs">${adapterPath}</span></div>`);
                    return rows.join('');
                })()}
            </div>
            <div class="device-actions">
                ${!isPaired ? `
                    <button class="btn btn-primary" data-action="pair" data-mac="${device.mac}">配对</button>
                ` : `
                    ${isConnected ? `
                        <button class="btn btn-secondary" data-action="disconnect" data-mac="${device.mac}">断开</button>
                        ${(() => {
                            const OPP_UUID = '00001105-0000-1000-8000-00805f9b34fb';
                            const hasOpp = deviceUuid.some(u => u.toLowerCase().replace(/-/g, '').includes(OPP_UUID.replace(/-/g, '').toLowerCase()));
                            const isAudio = isAudioDeviceType(deviceType);
                            return (hasOpp || (!isAudio && deviceUuid.length > 0)) ? `<button class="btn btn-sm btn-accent" data-action="sendFile" data-mac="${device.mac}" data-name="${device.name || device.mac}">发送文件</button>` : '';
                        })()}
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
    const needsActivate = device.needs_activate === true;
    const isConnected = device.connected === true;
    const displayName = device.friendly_name || device.name;
    const isBtDevice = device.isBluetooth || device.name.includes('bluez_');
    const audioType = device.audio_type || (isBtDevice ? 'bluetooth' : '');
    const isBtSource = isBtDevice && device.role === 'source';

    let typeLabel;
    if (isBtSource) {
        // 蓝牙输入只显示设备具体类型（手机/耳机等），"蓝牙输入"由连接徽章统一显示
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
                ${typeLabel ? `<span class="status-badge type-badge">${typeLabel}</span>` : ''}
                ${isBtDevice && isConnected && !isBtSource ? '<span class="status-badge connected">蓝牙已连接</span>' : ''}
                ${isBtSource && isConnected && !isDefault ? '<span class="status-badge connected">蓝牙输入</span>' : ''}
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
                    <span class="detail-value mono detail-value-sm">${chMapText}</span>
                </div>
                ${!isBtSource && !needsActivate ? `
                <div class="device-detail-row volume-control-row">
                    <span class="detail-label">音量</span>
                    <div class="volume-control">
                        <button class="mute-btn ${device.muted ? 'muted' : ''}" data-action="toggleMute" data-device="${deviceName}">
                            ${device.muted
                                ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>'
                                : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>'
                            }
                        </button>
                        <input type="range" class="volume-slider" min="0" max="100" value="${Math.min(device.volume || 0, 100)}" data-device="${deviceName}">
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
                            <span class="channel-label">${ch.channel || 'CH' + i}</span>
                            <input type="range" class="channel-volume-slider" min="0" max="100" value="${ch.volume || 0}" data-device="${deviceName}" data-channel="${i}">
                            <span class="channel-vol-text">${ch.volume || 0}%</span>
                        </div>`
                    ).join('')}</div>`;
                })()}
                ` : ''}
                ${!isBtSource && !needsActivate && (device.channel_count || 0) >= 2 ? `
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
                    ${(device.ports && device.ports.length > 0) ? `
                    <div class="device-detail-row">
                        <span class="detail-label">端口</span>
                        <span class="detail-value"><select class="detail-select audio-port-select" data-device="${deviceName}">${device.ports.map(p => `<option value="${p.name}" ${p.name === device.active_port ? 'selected' : ''}>${p.description || p.name}</option>`).join('')}</select></span>
                    </div>
                    ` : (device.active_port ? `
                    <div class="device-detail-row">
                        <span class="detail-label">端口</span>
                        <span class="detail-value">${device.active_port}</span>
                    </div>
                    ` : '')}
                    ${!isBtDevice && !needsActivate ? `
                    <div class="device-detail-row">
                        <span class="detail-label">Profile</span>
                        <span class="detail-value"><select class="detail-select audio-profile-select" data-device="${deviceName}">${(() => {
                            const profiles = device.profiles || [];
                            const activeProfile = device.active_profile || '';
                            if (profiles.length === 0) return '<option value="">无可用 Profile</option>';
                            return profiles.map(p => `<option value="${p.name}" ${p.name === activeProfile ? 'selected' : ''}>${p.description || p.name}</option>`).join('');
                        })()}</select></span>
                    </div>
                    ` : ''}
                    <div class="device-detail-row">
                        <span class="detail-label">节点</span>
                        <span class="detail-value">${device.node_id != null ? '#' + device.node_id : '-'}${device.card_index != null && device.card_index !== device.node_id ? ` / Card ${device.card_index}` : ''}</span>
                    </div>
                    ${alsaCardName ? `<div class="device-detail-row"><span class="detail-label">声卡</span><span class="detail-value">${alsaCardName}</span></div>` : ''}
                    ${pcmText ? `<div class="device-detail-row"><span class="detail-label">PCM 设备</span><span class="detail-value mono detail-value-sm">${pcmText}</span></div>` : ''}
                    ${vendorText ? `<div class="device-detail-row"><span class="detail-label">硬件ID</span><span class="detail-value mono detail-value-xs">${vendorText}</span></div>` : ''}
                    ${busPath ? `<div class="device-detail-row"><span class="detail-label">总线路径</span><span class="detail-value mono detail-value-xs">${busPath}</span></div>` : ''}
                    ${devFormFactor ? `<div class="device-detail-row"><span class="detail-label">形态</span><span class="detail-value">${FORM_FACTOR_LABELS[devFormFactor] || devFormFactor}</span></div>` : ''}
                    ${devDescription ? `<div class="device-detail-row"><span class="detail-label">设备描述</span><span class="detail-value detail-value-md">${devDescription}</span></div>` : ''}
                    ${nodeDriver ? `<div class="device-detail-row"><span class="detail-label">节点驱动</span><span class="detail-value mono detail-value-sm">${nodeDriver}</span></div>` : ''}
                    ${monitorSource ? `<div class="device-detail-row"><span class="detail-label">监听源</span><span class="detail-value mono detail-value-sm">${monitorSource}</span></div>` : ''}
                    <div class="device-detail-row detail-row-last">
                        <span class="detail-label">通道音量</span>
                        <span class="detail-value mono channel-volumes detail-value-sm">${(device.channels && device.channels.length > 0) ? device.channels.map(c => `${c.channel}: ${c.effective_volume ?? c.volume}%`).join(' / ') : '-'}</span>
                    </div>
            </div>

            <div class="device-actions">
                ${needsActivate ? `<button class="btn btn-accent" data-action="activateDevice" data-device="${deviceName}">激活设备</button>` : ''}
                ${!isDefault && !needsActivate && device.role !== 'source' ? `<button class="btn btn-secondary" data-action="setDefault" data-device="${deviceName}">设为默认</button>` : ''}
                ${!needsActivate && device.role !== 'source' ? `<button class="btn btn-accent" data-action="playDing" data-device="${deviceName}" data-channels="${encodeURIComponent(JSON.stringify((device.channels || []).map(c => ({position: (c.position || c.channel || '').toUpperCase(), label: CH_POS_LABELS[c.position || c.channel] || c.channel}))))}">播放测试</button>` : ''}
                ${isBtDevice && isConnected && !isBtSource ? `<button class="btn btn-danger" data-action="disconnectBtAudio" data-mac="${device.mac}">断开</button>` : ''}
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
                    <div class="device-name-group">
                        <div class="device-name">${device.friendly_name || device.name}</div>
                        ${devDescription && devDescription !== (device.friendly_name || device.name) ? `<div class="device-subname">${escapeHtml(devDescription)}</div>` : (v4l2Name && v4l2Name !== (device.friendly_name || device.name) ? `<div class="device-subname">${escapeHtml(v4l2Name)}</div>` : '')}
                    </div>
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
                    <span class="detail-value mono detail-value-sm">${formatText || '-'}</span>
                </div>
                ${connInfo ? `<div class="device-detail-row"><span class="detail-label">连接器</span><span class="detail-value">${connInfo}</span></div>` : ''}
                ${dpmsStatus ? `<div class="device-detail-row"><span class="detail-label">DPMS</span><span class="detail-value">${dpmsStatus}</span></div>` : ''}
                ${drmConnector ? (() => {
                    const modes = device.formats || [];
                    if (modes.length === 0) return '';
                    // 解析 modes 为分辨率选项
                    const resMap = {};
                    modes.forEach(m => {
                        const match = m.match(/^(\d+x\d+)(?:@(\d+\.?\d*)Hz)?$/);
                        if (match) {
                            const res = match[1];
                            if (!resMap[res]) resMap[res] = [];
                            if (match[2]) resMap[res].push(match[2]);
                        }
                    });
                    const resOptions = Object.keys(resMap).map(r => `<option value="${r}">${r}</option>`).join('');
                    const currentRes = resolution.replace('×', 'x');
                    const formatsJson = JSON.stringify(modes).replace(/"/g, '&quot;');
                    return `<div class="device-detail-row"><span class="detail-label">切换分辨率</span><span class="detail-value"><select class="video-select video-res-select" data-connector="${drmConnector}" data-current-res="${currentRes}" data-formats="${formatsJson}"><option value="">自动</option>${resOptions}</select></span></div>` +
                           `<div class="device-detail-row"><span class="detail-label">刷新率</span><span class="detail-value"><select class="video-select video-rate-select" data-connector="${drmConnector}"><option value="">自动</option></select></span></div>`;
                })() : ''}
                    <div class="device-detail-row">
                        <span class="detail-label">名称</span>
                        <span class="detail-value mono detail-value-sm">${device.name}</span>
                    </div>
                    <div class="device-detail-row">
                        <span class="detail-label">媒体类</span>
                        <span class="detail-value">${device.media_class || '-'}</span>
                    </div>
                    <div class="device-detail-row">
                        <span class="detail-label">支持格式</span>
                        <span class="detail-value detail-value-sm">${(device.formats && device.formats.length > 0) ? device.formats.join(', ') : '-'}</span>
                    </div>
                    <div class="device-detail-row">
                        <span class="detail-label">节点ID</span>
                        <span class="detail-value">${device.node_id != null ? '#' + device.node_id : (v4l2Device || drmConnector || '-')}</span>
                    </div>
                    ${vendorText ? `<div class="device-detail-row"><span class="detail-label">硬件ID</span><span class="detail-value mono detail-value-xs">${vendorText}</span></div>` : ''}
                    ${objSerial ? `<div class="device-detail-row"><span class="detail-label">序列号</span><span class="detail-value mono detail-value-sm">${objSerial}</span></div>` : ''}
                    ${devApi ? `<div class="device-detail-row"><span class="detail-label">设备 API</span><span class="detail-value mono detail-value-sm">${devApi}</span></div>` : ''}
                    ${devBus ? `<div class="device-detail-row"><span class="detail-label">总线</span><span class="detail-value">${devBus}</span></div>` : ''}
                    ${busPath ? `<div class="device-detail-row"><span class="detail-label">总线路径</span><span class="detail-value mono detail-value-xs">${busPath}</span></div>` : ''}
                    ${prioritySession ? `<div class="device-detail-row"><span class="detail-label">会话优先级</span><span class="detail-value">${prioritySession}</span></div>` : ''}
                    ${priorityDriver ? `<div class="device-detail-row"><span class="detail-label">驱动优先级</span><span class="detail-value">${priorityDriver}</span></div>` : ''}
                    ${v4l2Device ? `<div class="device-detail-row"><span class="detail-label">V4L2 设备</span><span class="detail-value mono detail-value-sm">${v4l2Device}</span></div>` : ''}
                    ${v4l2Name ? `<div class="device-detail-row"><span class="detail-label">V4L2 名称</span><span class="detail-value detail-value-md">${v4l2Name}</span></div>` : ''}
                    ${drmConnector && drmConnector !== device.name.replace('drm_', '') ? `<div class="device-detail-row"><span class="detail-label">DRM 连接器</span><span class="detail-value mono detail-value-sm">${drmConnector}</span></div>` : ''}
                    ${drmConnector ? `<div class="device-detail-row"><span class="detail-label">DRM 路径</span><span class="detail-value mono detail-value-xs">/sys/class/drm/${drmConnector}</span></div>` : ''}
                    ${factoryName ? `<div class="device-detail-row"><span class="detail-label">工厂</span><span class="detail-value mono detail-value-sm">${factoryName}</span></div>` : ''}
                    ${devFormFactor ? `<div class="device-detail-row"><span class="detail-label">形态</span><span class="detail-value">${FORM_FACTOR_LABELS[devFormFactor] || devFormFactor}</span></div>` : ''}
                    ${devIcon ? `<div class="device-detail-row"><span class="detail-label">图标</span><span class="detail-value mono detail-value-sm">${devIcon}</span></div>` : ''}
                    ${devDescription ? `<div class="device-detail-row"><span class="detail-label">设备描述</span><span class="detail-value detail-value-md">${devDescription}</span></div>` : ''}
                    ${nodeDriver ? `<div class="device-detail-row"><span class="detail-label">节点驱动</span><span class="detail-value mono detail-value-sm">${nodeDriver}</span></div>` : ''}
                    ${drmEnabled ? `<div class="device-detail-row"><span class="detail-label">DRM 启用</span><span class="detail-value">${drmEnabled}</span></div>` : ''}
                    ${v4l2Caps ? `<div class="device-detail-row detail-row-last"><span class="detail-label">V4L2 能力</span><span class="detail-value detail-value-sm">${v4l2Caps}</span></div>` : ''}
            </div>
            <div class="device-actions">
                ${!isDefault ? `<button class="btn btn-secondary" data-action="setDefaultVideo" data-device="${device.name}">设为默认</button>` : ''}
                ${drmConnector ? `<button class="btn btn-sm btn-secondary display-layout-btn" data-connector="${drmConnector}" title="设置显示器布局">布局</button>` : ''}
                ${device.video_type === 'camera' ? `<button class="btn btn-sm btn-secondary v4l2-controls-btn" data-device="${device.name}" title="调节摄像头参数">参数</button>` : ''}
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
        // 开启可发现时，同时设置超时
        if (enabled) {
            const timeoutInput = document.getElementById('discoverableTimeout');
            const timeout = timeoutInput ? parseInt(timeoutInput.value) : 0;
            if (timeout > 0) {
                try {
                    await apiCall('/api/bluetooth/discoverable-timeout', {
                        method: 'POST',
                        body: JSON.stringify({ timeout: timeout })
                    });
                } catch (e) { /* 超时设置失败不影响主流程 */ }
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
                // 连接超时或未找到设备时，先扫描刷新设备状态再重试
                logger.info(`连接失败，先扫描再重试: ${mac}, 错误: ${err}`);
                try {
                    // 执行快速扫描刷新设备状态
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
            // 网络或其他异常时，先扫描刷新设备状态再重试
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

// ── 文件传输 ──
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

        // 绑定取消按钮
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

        // 有活跃传输时启动轮询并自动展开
        const hasActive = transfers.some(t => t.status === 'queued' || t.status === 'active');
        if (hasActive) {
            startTransferPoll();
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

function startTransferPoll() {
    if (_transferPollTimer) return;
    _transferPollTimer = setInterval(() => {
        if (currentTab !== 'bluetooth') {  // 不在蓝牙标签页时停止轮询
            stopTransferPoll();
            return;
        }
        refreshTransferList();
    }, 3000);
}

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
    return ['audio-card', 'audio-headset', 'audio-headphones', 'audio-speakers', 'audio-input-microphone', 'audio-input'].includes(deviceType);
}

// 加载蓝牙音频 Profile 列表到下拉框
async function _loadBtProfiles(selectEl) {
    const mac = selectEl.dataset.mac;
    try {
        const result = await apiCall(`/api/bluetooth/audio-profiles/${mac}`);
        const profiles = result.data || [];
        selectEl.innerHTML = '';
        if (profiles.length === 0) {
            selectEl.innerHTML = '<option value="">无可用模式</option>';
            return;
        }
        let hasActive = false;
        profiles.forEach(p => {
            const opt = document.createElement('option');
            opt.value = p.name;
            opt.textContent = p.description || p.name;
            if (!p.available) opt.disabled = true;
            if (p.active) { opt.selected = true; hasActive = true; }
            selectEl.appendChild(opt);
        });
        // 无活跃标记时默认选第一个可用项
        if (!hasActive) {
            const firstAvailable = selectEl.querySelector('option:not([disabled])');
            if (firstAvailable) firstAvailable.selected = true;
        }
        // 更新麦克风按钮状态
        const micBtn = selectEl.closest('.device-details')?.querySelector('.bt-mic-toggle');
        if (micBtn) {
            const currentProfile = selectEl.value.toLowerCase();
            const isHfp = currentProfile.includes('hfp') || currentProfile.includes('hsp');
            micBtn.dataset.enabled = isHfp ? 'true' : 'false';
            micBtn.textContent = isHfp ? '开启' : '关闭';
            micBtn.classList.toggle('btn-accent', isHfp);
        }
    } catch (e) {
        selectEl.innerHTML = '<option value="">获取失败</option>';
    }
}

// 切换蓝牙音频 Profile
async function _handleBtProfileChange(e) {
    const selectEl = e.target;
    const mac = selectEl.dataset.mac;
    const profile = selectEl.value;
    if (!profile) return;
    selectEl.disabled = true;
    try {
        await apiCall('/api/bluetooth/audio-profile/switch', {
            method: 'POST',
            body: JSON.stringify({ mac, profile })
        });
        showToast('音频模式已切换', 'success');
        // 更新麦克风按钮状态
        const micBtn = selectEl.closest('.device-details')?.querySelector('.bt-mic-toggle');
        if (micBtn) {
            const isHfp = profile.toLowerCase().includes('hfp') || profile.toLowerCase().includes('hsp');
            micBtn.dataset.enabled = isHfp ? 'true' : 'false';
            micBtn.textContent = isHfp ? '开启' : '关闭';
            micBtn.classList.toggle('btn-accent', isHfp);
        }
    } catch (err) {
        showToast('切换音频模式失败: ' + err.message, 'error');
    } finally {
        selectEl.disabled = false;
    }
}

// 蓝牙麦克风开关
async function _handleBtMicToggle(e) {
    const btn = e.target;
    const mac = btn.dataset.mac;
    const isEnabled = btn.dataset.enabled === 'true';
    btn.disabled = true;
    try {
        if (isEnabled) {
            await apiCall('/api/bluetooth/microphone/disable', {
                method: 'POST',
                body: JSON.stringify({ mac })
            });
            btn.dataset.enabled = 'false';
            btn.textContent = '关闭';
            btn.classList.remove('btn-accent');
            showToast('麦克风已关闭，已切回 A2DP 高质量模式', 'success');
        } else {
            await apiCall('/api/bluetooth/microphone/enable', {
                method: 'POST',
                body: JSON.stringify({ mac })
            });
            btn.dataset.enabled = 'true';
            btn.textContent = '开启';
            btn.classList.add('btn-accent');
            showToast('麦克风已开启', 'success');
        }
        // 同步 Profile 下拉框
        const selectEl = btn.closest('.device-details')?.querySelector('.bt-profile-select');
        if (selectEl) await _loadBtProfiles(selectEl);
    } catch (err) {
        showToast('麦克风切换失败: ' + err.message, 'error');
    } finally {
        btn.disabled = false;
    }
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
            } catch (e) { console.warn('channel test error:', e); }
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
        // 同步更新声道独立音量滑块
        const chSliders = card.querySelectorAll('.channel-volume-slider');
        channels.forEach((ch, i) => {
            if (i < chSliders.length) {
                const chVol = ch.effective_volume ?? ch.volume;
                chSliders[i].value = chVol;
                const chText = chSliders[i].parentElement.querySelector('.channel-vol-text');
                if (chText) chText.textContent = `${chVol}%`;
            }
        });
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

let _volumeChangeTime = 0;

async function setVolume(deviceName, volume) {
    _volumeChangeTime = Date.now();
    try {
        const result = await apiCall('/api/audio/volume', {
            method: 'POST',
            body: JSON.stringify({ device: deviceName, volume })
        });
        const data = result.data || {};
        if (result.success) {
            const verified = data.verified_volume ?? volume;
            // 验证值与设置值差异≤5%时使用设置值，避免点击时微小跳变
            const displayVol = Math.abs(verified - volume) <= 5 ? volume : verified;
            _updateChannelDisplay(deviceName, data.channels, displayVol);
        }
    } catch (error) {
        showToast('设置音量失败: ' + error.message, 'error');
    }
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
    _volumeChangeTime = Date.now();
    try {
        const result = await apiCall('/api/audio/balance', {
            method: 'POST',
            body: JSON.stringify({ device: deviceName, balance: balance / 100 })
        });
        const data = result.data || {};
        if (!result.success) {
            showToast('设置平衡失败: ' + (result.error || '未知错误'), 'error');
        } else if (data.channels) {
            _updateChannelDisplay(deviceName, data.channels);
        }
    } catch (error) {
        showToast('设置平衡失败: ' + error.message, 'error');
        renderAudioDevices();
    }
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
    const btn = document.querySelector(`[data-action="setDefaultVideo"][data-device="${CSS.escape(deviceName)}"]`);
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

// 绑定设备卡片展开/收起按钮的点击事件（折叠已在 HTML 生成时处理）
function _applyDeviceCardCollapse(container) {
    container.querySelectorAll('.detail-toggle-btn').forEach(btn => {
        if (btn._toggleBound) return;
        btn._toggleBound = true;
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const details = btn.closest('.device-details');
            if (!details) return;
            const rows = details.querySelectorAll('.device-detail-row');
            const max = parseInt(btn.dataset.max) || 5;
            if (rows.length <= max) return;
            const isHidden = rows[max].style.display === 'none';
            rows.forEach((row, i) => {
                if (i >= max) row.style.display = isHidden ? '' : 'none';
            });
            btn.textContent = isHidden ? '收起详情' : `展开详情 (共${rows.length}项)`;
        });
    });
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

    // 显示器布局按钮
    container.querySelectorAll('.display-layout-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const connector = btn.dataset.connector;
            // 收集所有 DRM 连接器
            const allConnectors = [];
            container.querySelectorAll('.display-layout-btn').forEach(b => {
                const c = b.dataset.connector;
                if (c && c !== connector) allConnectors.push(c);
            });
            if (allConnectors.length === 0) {
                showToast('仅有一个显示器，无需配置布局', 'info');
                return;
            }
            // 弹出简单选择
            const relations = [
                { value: 'left-of', label: '左侧' },
                { value: 'right-of', label: '右侧' },
                { value: 'above', label: '上方' },
                { value: 'below', label: '下方' },
                { value: 'same-as', label: '镜像' },
                { value: 'primary', label: '主显示器' },
            ];
            const relOptions = relations.map(r => `<option value="${r.value}">${r.label}</option>`).join('');
            const connOptions = allConnectors.map(c => `<option value="${c}">${c}</option>`).join('');
            const rotations = [
                { value: 'normal', label: '正常' },
                { value: 'left', label: '左转90°' },
                { value: 'right', label: '右转90°' },
                { value: 'inverted', label: '倒转180°' },
            ];
            const rotOptions = rotations.map(r => `<option value="${r.value}">${r.label}</option>`).join('');
            const html = `<div class="layout-dialog-overlay" id="layoutDialog">
                <div class="layout-dialog">
                    <h4>显示器设置 - ${connector}</h4>
                    <div class="layout-dialog-row">
                        <label>布局</label>
                        <select id="layoutRelation">${relOptions}</select>
                    </div>
                    <div class="layout-dialog-row" id="layoutRelativeRow">
                        <label>相对</label>
                        <select id="layoutRelativeTo">${connOptions}</select>
                    </div>
                    <div class="layout-dialog-row">
                        <label>旋转</label>
                        <select id="layoutRotation">${rotOptions}</select>
                    </div>
                    <div class="layout-dialog-row">
                        <label>缩放</label>
                        <input type="range" id="layoutScale" min="0.5" max="3" step="0.25" value="1">
                        <span id="layoutScaleVal">1x</span>
                    </div>
                    <div class="layout-dialog-actions">
                        <button class="btn btn-secondary" id="layoutCancel">取消</button>
                        <button class="btn btn-primary" id="layoutApply">应用</button>
                    </div>
                </div>
            </div>`;
            document.body.insertAdjacentHTML('beforeend', html);
            // 缩放滑块
            document.getElementById('layoutScale').addEventListener('input', (ev) => {
                document.getElementById('layoutScaleVal').textContent = ev.target.value + 'x';
            });
            // 关系选择变化时切换"相对"行显示
            document.getElementById('layoutRelation').addEventListener('change', (ev) => {
                document.getElementById('layoutRelativeRow').style.display = ev.target.value === 'primary' ? 'none' : '';
            });
            document.getElementById('layoutCancel').addEventListener('click', () => {
                document.getElementById('layoutDialog')?.remove();
            });
            document.getElementById('layoutApply').addEventListener('click', async () => {
                const relation = document.getElementById('layoutRelation').value;
                const relativeTo = document.getElementById('layoutRelativeTo')?.value;
                const rotation = document.getElementById('layoutRotation').value;
                const scale = parseFloat(document.getElementById('layoutScale').value);
                document.getElementById('layoutDialog')?.remove();
                try {
                    // 应用布局
                    if (relation !== 'primary' || relativeTo) {
                        await apiCall('/api/video/display-layout', {
                            method: 'POST',
                            body: JSON.stringify({ output: connector, relation, relative_to: relativeTo })
                        });
                    } else {
                        await apiCall('/api/video/display-layout', {
                            method: 'POST',
                            body: JSON.stringify({ output: connector, relation: 'primary' })
                        });
                    }
                    // 应用旋转
                    if (rotation !== 'normal') {
                        await apiCall('/api/video/display-rotation', {
                            method: 'POST',
                            body: JSON.stringify({ output: connector, rotation })
                        });
                    }
                    // 应用缩放
                    if (scale !== 1) {
                        await apiCall('/api/video/display-scale', {
                            method: 'POST',
                            body: JSON.stringify({ output: connector, scale })
                        });
                    }
                    showToast('显示器设置已应用', 'success');
                } catch (error) {
                    showToast('设置失败: ' + error.message, 'error');
                }
            });
        });
    });

    // V4L2 摄像头参数调节
    container.querySelectorAll('.v4l2-controls-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const deviceName = btn.dataset.device;
            try {
                const result = await apiCall(`/api/video/v4l2-controls/${encodeURIComponent(deviceName)}`);
                const controls = result.data || [];
                if (controls.length === 0) {
                    showToast('该设备无可调参数', 'info');
                    return;
                }
                const rows = controls.filter(c => c.type === 'int').map(c => {
                    const label = c.name.replace(/_/g, ' ');
                    return `<div class="layout-dialog-row">
                        <label title="${c.name}">${label}</label>
                        <input type="range" min="${c.min}" max="${c.max}" step="${c.step || 1}" value="${c.value}" data-control="${c.name}" class="v4l2-ctrl-slider">
                        <span class="v4l2-ctrl-val">${c.value}</span>
                    </div>`;
                }).join('');
                const html = `<div class="layout-dialog-overlay" id="v4l2Dialog">
                    <div class="layout-dialog layout-dialog-scrollable">
                        <h4>摄像头参数 - ${deviceName}</h4>
                        ${rows || '<p class="text-muted">无可调参数</p>'}
                        <div class="layout-dialog-actions">
                            <button class="btn btn-secondary" id="v4l2Close">关闭</button>
                        </div>
                    </div>
                </div>`;
                document.body.insertAdjacentHTML('beforeend', html);
                // 滑块事件
                document.querySelectorAll('#v4l2Dialog .v4l2-ctrl-slider').forEach(slider => {
                    slider.addEventListener('input', () => {
                        slider.nextElementSibling.textContent = slider.value;
                    });
                    slider.addEventListener('change', async () => {
                        try {
                            await apiCall('/api/video/v4l2-control', {
                                method: 'POST',
                                body: JSON.stringify({ device: deviceName, control: slider.dataset.control, value: parseInt(slider.value) })
                            });
                        } catch (error) {
                            showToast('设置参数失败: ' + error.message, 'error');
                        }
                    });
                });
                document.getElementById('v4l2Close').addEventListener('click', () => {
                    document.getElementById('v4l2Dialog')?.remove();
                });
            } catch (error) {
                showToast('获取参数失败: ' + error.message, 'error');
            }
        });
    });

    // 分辨率切换：联动刷新率下拉框
    container.querySelectorAll('.video-res-select').forEach(sel => {
        // 设置当前分辨率为选中
        const currentRes = sel.dataset.currentRes;
        if (currentRes) sel.value = currentRes;
        // 初始化刷新率选项
        _updateVideoRateOptions(sel);
        sel.addEventListener('change', () => {
            _updateVideoRateOptions(sel);
            _applyVideoDisplaySettings(sel);
        });
    });
    container.querySelectorAll('.video-rate-select').forEach(sel => {
        sel.addEventListener('change', () => _applyVideoDisplaySettings(sel));
    });
    _applyDeviceCardCollapse(container);
}

// 更新刷新率下拉框选项
function _updateVideoRateOptions(resSelect) {
    const connector = resSelect.dataset.connector;
    const rateSelect = resSelect.closest('.device-details')?.querySelector(`.video-rate-select[data-connector="${connector}"]`);
    if (!rateSelect) return;
    const selectedRes = resSelect.value;
    rateSelect.innerHTML = '<option value="">自动</option>';
    if (!selectedRes) return;
    // 从 data-formats 属性获取原始 modes 数据
    const formatsJson = resSelect.dataset.formats;
    if (!formatsJson) return;
    let modes = [];
    try { modes = JSON.parse(formatsJson); } catch(e) { return; }
    const rateSet = new Set();
    modes.forEach(m => {
        const match = m.match(/^(\d+x\d+)(?:@(\d+\.?\d*)Hz)?$/);
        if (match && match[1] === selectedRes && match[2]) rateSet.add(match[2]);
    });
    rateSet.forEach(r => {
        const opt = document.createElement('option');
        opt.value = r;
        opt.textContent = r + 'Hz';
        rateSelect.appendChild(opt);
    });
}

// 应用分辨率/刷新率设置
async function _applyVideoDisplaySettings(el) {
    const connector = el.dataset.connector;
    const details = el.closest('.device-details');
    const resSelect = details?.querySelector(`.video-res-select[data-connector="${connector}"]`);
    const rateSelect = details?.querySelector(`.video-rate-select[data-connector="${connector}"]`);
    if (!resSelect || !rateSelect) return;
    const resolution = resSelect.value;
    const refreshRate = rateSelect.value;
    if (!resolution && !refreshRate) return; // 两者都为"自动"时不触发
    try {
        await apiCall('/api/video/display-output', {
            method: 'POST',
            body: JSON.stringify({ connector, resolution: resolution || null, refresh_rate: refreshRate || null })
        });
        showToast('显示设置已应用', 'success');
    } catch (err) {
        showToast('设置失败: ' + err.message, 'error');
    }
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
                // 可发现超时输入框：仅在可发现开启时显示
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

            // 更新自动重连开关状态
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
                                <div class="info-item"><span class="info-label">功能特征</span><span class="info-value info-value-xs">${ctrl.features || '-'}</span></div>
                                <div class="info-item"><span class="info-label">数据包类型</span><span class="info-value info-value-xs">${ctrl.packet_types || '-'}</span></div>
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
}

// 渲染蓝牙设备列表
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

    // 加载蓝牙音频源列表（用于音频源路由）
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
        // 准备蓝牙设备的上下文信息
        device._isPaired = !!pairedInfo || device.paired === true;
        device._isConnected = pairedInfo?.connected === true || device.connected === true;
        device._deviceType = device.type || pairedInfo?.type || pairedInfo?.icon || '';
        device._vendor = pairedInfo?.vendor || '';
        device._battery = pairedInfo?.battery || '';
        device._rssi = (() => {
            const pr = pairedInfo?.rssi || '';
            const dr = device.rssi != null ? (typeof device.rssi === 'number' ? device.rssi + ' dBm' : String(device.rssi)) : '';
            // 优先使用有值的，都有值时优先使用扫描结果（更实时）
            if (!pr && !dr) return '';
            if (!pr) return dr;
            if (!dr) return pr;
            // 两者都有值，使用信号更强的（数值更大）
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
    // 蓝牙音频 Profile 选择器和麦克风开关
    container.querySelectorAll('.bt-profile-select').forEach(sel => {
        _loadBtProfiles(sel);
        sel.addEventListener('change', _handleBtProfileChange);
    });
    container.querySelectorAll('.bt-mic-toggle').forEach(btn => {
        btn.addEventListener('click', _handleBtMicToggle);
    });
    _applyDeviceCardCollapse(container);
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
                // 更新折叠区通道音量文本
                const card = e.currentTarget.closest('.device-card');
                if (card && result.data) {
                    const chEl = card.querySelector('.channel-volumes');
                    if (chEl) {
                        // 重新读取所有声道滑块的当前值
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

    // 端口切换
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

    // Profile 下拉框切换（初始数据已由设备列表提供，无需额外加载）
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

// 加载音频设备 Profile 列表
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

// Tab 切换
function switchTab(tabName) {
    currentTab = tabName;

    document.querySelectorAll('.tab-item').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tabName);
    });

    document.querySelectorAll('.tab-panel').forEach(panel => {
        panel.classList.toggle('active', panel.id === `${tabName}Tab`);
    });

    // 切换标签时加载初始数据，后续刷新由 SSE 事件驱动
    if (tabName === 'bluetooth') {
        pollReconnectStatus().then(() => updateBluetoothStatus());
        if (scannedDevices.length === 0) loadInitialDevices();
        startReconnectPolling();
    } else {
        // 离开蓝牙标签时停止重连轮询
        if (reconnectTimer) {
            clearInterval(reconnectTimer);
            reconnectTimer = null;
        }
    }
    if (tabName === 'audio') {
        if (!lastAudioSnapshot) renderAudioDevices();
    } else if (tabName === 'video') {
        renderVideoDevices();
    } else if (tabName === 'system') {
        Promise.all([pollReconnectStatus(), fetchSystemOverview()]).then(([_, overviewData]) => {
            if (overviewData) renderSystemOverview(overviewData);
        });
    }
}

let lastBtSnapshot = '';
let lastAudioSnapshot = '';

// ── SSE 事件驱动刷新 ──
let sse = null;
let sseErrorCount = 0;
let sseFallbackTimers = {};
const SSE_MAX_ERRORS = 5;

function initSSE() {
    try {
        sse = new EventSource('/api/events');

        sse.onopen = () => {
            sseErrorCount = 0;
            stopSSEFallback();
        };

        sse.addEventListener('audio.changed', () => {
            if (currentTab === 'audio') _debouncedAudioRefresh();
        });

        sse.addEventListener('bluetooth.changed', () => {
            if (currentTab === 'bluetooth') _debouncedBtRefresh();
        });

        sse.addEventListener('video.changed', () => {
            if (currentTab === 'video') _debouncedVideoRefresh();
        });

        sse.addEventListener('system.changed', () => {
            if (currentTab === 'system') _debouncedSystemRefresh();
        });

        sse.onerror = () => {
            sseErrorCount++;
            if (sseErrorCount >= SSE_MAX_ERRORS) {
                // 达到最大错误数，关闭 EventSource 避免无限重连，启用轮询降级
                if (sse) sse.close();
                sse = null;
                if (!sseFallbackTimers._active) startSSEFallback();
            }
        };
    } catch (e) {
        startSSEFallback();
    }
}

function startSSEFallback() {
    if (sseFallbackTimers._active) return;
    sseFallbackTimers._active = true;
    sseFallbackTimers.audio = setInterval(() => {
        if (currentTab === 'audio') { renderAudioDevices(); }
    }, 3000);
    sseFallbackTimers.bluetooth = setInterval(async () => {
        if (currentTab === 'bluetooth') {
            try {
                const pairedDevices = await getPairedDevices();
                const snapshot = pairedDevices.map(d => `${d.mac}|${d.connected}`).join(';');
                if (snapshot !== lastBtSnapshot) {
                    lastBtSnapshot = snapshot;
                    _mergePairedIntoScanned(pairedDevices);
                    await renderBluetoothDevices(scannedDevices);
                }
            } catch (e) { console.warn('SSE fallback bt refresh error:', e); }
        }
    }, 3000);
    sseFallbackTimers.video = setInterval(() => {
        if (currentTab === 'video') renderVideoDevices();
    }, 5000);
    sseFallbackTimers.system = setInterval(async () => {
        if (currentTab === 'system') {
            try {
                const data = await fetchSystemOverview();
                if (data) renderSystemOverview(data);
            } catch (e) { console.warn('SSE fallback system refresh error:', e); }
        }
    }, 30000);
}

function stopSSEFallback() {
    Object.keys(sseFallbackTimers).forEach(k => {
        if (k !== '_active') clearInterval(sseFallbackTimers[k]);
    });
    sseFallbackTimers = {};
}

// 防抖刷新：200ms 内多次事件只触发一次
let _audioDebounce = null;
function _debouncedAudioRefresh() {
    clearTimeout(_audioDebounce);
    _audioDebounce = setTimeout(async () => {
        if (currentTab !== 'audio') return;
        try {
            const audioResult = await getAudioDevices();
            const devices = audioResult.devices || [];
            const active = document.activeElement;
            const isAdjusting = active && (active.classList.contains('volume-slider') || active.classList.contains('balance-slider') || active.classList.contains('channel-volume-slider'));
            // 音量变更后 1 秒内或正在调整时，使用就地更新避免滑块被重渲染覆盖
            if (isAdjusting || Date.now() - _volumeChangeTime < 1000) {
                _updateAudioDevicesInPlace(devices, audioResult);
            } else {
                renderAudioDevices();
            }
        } catch (e) { console.warn('debounced audio refresh error:', e); }
    }, 200);
}

let _btDebounce = null;
function _debouncedBtRefresh() {
    clearTimeout(_btDebounce);
    _btDebounce = setTimeout(async () => {
        if (currentTab !== 'bluetooth') return;
        try {
            const pairedDevices = await getPairedDevices();
            lastBtSnapshot = pairedDevices.map(d => `${d.mac}|${d.connected}`).join(';');
            _mergePairedIntoScanned(pairedDevices);
            await renderBluetoothDevices(scannedDevices, pairedDevices);
        } catch (e) { console.warn('debounced bt refresh error:', e); }
    }, 200);
}

let _videoDebounce = null;
function _debouncedVideoRefresh() {
    clearTimeout(_videoDebounce);
    _videoDebounce = setTimeout(() => { if (currentTab === 'video') renderVideoDevices(); }, 200);
}

let _systemDebounce = null;
function _debouncedSystemRefresh() {
    clearTimeout(_systemDebounce);
    _systemDebounce = setTimeout(async () => {
        if (currentTab !== 'system') return;
        try {
            const data = await fetchSystemOverview();
            if (data) renderSystemOverview(data);
        } catch (e) { console.warn('debounced system refresh error:', e); }
    }, 200);
}

function _updateAudioDevicesInPlace(devices, audioResult) {
    const defaultName = audioResult.default || '';
    devices.forEach(d => {
        const card = document.querySelector(`.device-card[data-device="${CSS.escape(d.name)}"]`);
        if (!card) return;
        const slider = card.querySelector('.volume-slider');
        if (slider && document.activeElement !== slider) {
            // 音量上限固定 100%，超过部分为增益（cubic > 1.0），按需求舍弃不显示
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
    // 清理已不在配对列表中的旧扫描记录（保留最近100条，防止数组无限增长）
    if (scannedDevices.length > 100) {
        const pairedMacs = new Set(pairedMap.keys());
        scannedDevices = scannedDevices.filter(d => pairedMacs.has(d.mac) || d.paired === false);
    }
}

function startKeepAlive() {
    const keepAliveTimer = setInterval(async () => {
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
        } catch (e) { console.warn('keepalive refresh error:', e); }
    }, 60000);
    window.addEventListener('beforeunload', () => clearInterval(keepAliveTimer));
}

async function pollReconnectStatus() {
    try {
        const result = await apiCall('/api/bluetooth/reconnect/status');
        reconnectMonitorData = result.data || {};
        updateReconnectIndicator();
    } catch (e) { console.warn('poll reconnect status error:', e); }
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
        const result = await apiCall('/api/bluetooth/reconnect', {
            method: 'POST',
            body: JSON.stringify({ enabled: enabled })
        });
        if (result.success) {
            showToast(enabled ? '自动重连已开启' : '自动重连已关闭', 'success');
            reconnectMonitorData = reconnectMonitorData || {};
            reconnectMonitorData.monitoring = enabled;
            const toggle = document.getElementById('autoReconnectSwitch');
            if (toggle) toggle.checked = enabled;
            const reconnectToggle = document.getElementById('reconnectToggle');
            if (reconnectToggle) reconnectToggle.classList.toggle('active', enabled);
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

    // deps 提前定义：顶部卡片和依赖圆点都需要使用
    const deps = data.dependencies || {};

    // 顶部状态卡片行
    const pwRunning = !!data.pipewire;
    const wpRunning = !!data.wireplumber;
    const btRunning = !!data.bluetooth_service;
    const btAudioReady = !!data.bluetooth_audio_ready;
    const spaPluginOk = !!data.spa_bluetooth_plugin;
    const btHardware = deps.bluetooth_hardware !== undefined ? !!deps.bluetooth_hardware : true;

    // 蓝牙音频卡片状态：无硬件时显示"无硬件"（warning），有硬件时按就绪状态显示
    let btAudioLabel, btAudioStatus;
    if (!btHardware) {
        btAudioLabel = '无硬件';
        btAudioStatus = 'warning';
    } else if (btAudioReady) {
        btAudioLabel = '就绪';
        btAudioStatus = 'ok';
    } else {
        btAudioLabel = spaPluginOk ? '未就绪' : '插件缺失';
        btAudioStatus = spaPluginOk ? 'warning' : 'error';
    }

    let statusRow = `<div class="overview-status-row">
        ${_overviewCard('PipeWire', pwRunning ? '运行中' : '未运行', pwRunning ? 'ok' : 'error', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>')}
        ${_overviewCard('WirePlumber', wpRunning ? '运行中' : '未运行', wpRunning ? 'ok' : 'error', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9"/></svg>')}
        ${_overviewCard('蓝牙服务', btRunning ? '运行中' : '未运行', btRunning ? 'ok' : 'error', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6.5 6.5l11 11L12 23V1l5.5 5.5-11 11"/></svg>', btRunning ? `<button class="btn btn-sm btn-secondary svc-restart-btn" data-service="bluetooth" title="重启蓝牙服务">重启</button>` : `<button class="btn btn-sm btn-primary svc-start-btn" data-service="bluetooth" title="启动蓝牙服务">启动</button>`)}
        ${_overviewCard('蓝牙音频', btAudioLabel, btAudioStatus, '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6.5 6.5l11 11L12 23V1l5.5 5.5-11 11"/></svg>')}
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
    const depDots = [];
    function _collectDepDot(name, ok, critical) {
        depDots.push({ name, ok: !!ok, error: !ok && !!critical });
    }
    if (deps.pipewire) _collectDepDot('PipeWire', deps.pipewire.running, true);
    if (deps.wireplumber) _collectDepDot('WirePlumber', deps.wireplumber.running, true);
    if (deps.pipewire_pulse) _collectDepDot('pipewire-pulse', deps.pipewire_pulse.running, true);
    (deps.packages || []).forEach(p => { _collectDepDot(p.name, p.installed, p.critical); });
    (deps.services || []).forEach(s => { _collectDepDot(s.name, s.active, s.critical); });
    (deps.commands || []).forEach(c => { _collectDepDot(c.name, c.exists, c.critical); });
    if (deps.spa_bluetooth_plugin !== undefined) _collectDepDot('SPA插件', deps.spa_bluetooth_plugin, true);
    // 无蓝牙硬件时显示"蓝牙硬件"为 warning（非 critical error），有硬件时才检查端点
    if (deps.bluetooth_hardware !== undefined && !deps.bluetooth_hardware) {
        _collectDepDot('蓝牙硬件', false, false);
    } else if (deps.bluetooth_audio_ready !== undefined) {
        _collectDepDot('蓝牙音频端点', deps.bluetooth_audio_ready, true);
    }
    // python 包已在上面 packages 遍历中收集，无需重复

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
    const depContent = depContentHtml + _renderDepSections({...deps, spa_bluetooth_plugin: data.spa_bluetooth_plugin});

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

    // 服务控制按钮事件
    container.querySelectorAll('.svc-restart-btn, .svc-start-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const service = btn.dataset.service;
            const action = btn.classList.contains('svc-restart-btn') ? 'restart' : 'start';
            btn.disabled = true;
            btn.textContent = action === 'restart' ? '重启中...' : '启动中...';
            try {
                const result = await apiCall(`/api/system/service/${action}`, {
                    method: 'POST',
                    body: JSON.stringify({ service })
                });
                if (result.success) {
                    showToast(safeToastData(result.data, '操作成功'), 'success');
                    setTimeout(() => fetchSystemOverview().then(d => { if (d) renderSystemOverview(d); }), 2000);
                } else {
                    showToast(safeToastData(result.error, '操作失败'), 'error');
                }
            } catch (error) {
                showToast('操作失败: ' + error.message, 'error');
            } finally {
                btn.disabled = false;
            }
        });
    });
}

function _overviewCard(title, statusText, statusClass, iconSvg, actionHtml) {
    return `<div class="overview-card">
        <div class="overview-card-icon ${statusClass}">${iconSvg}</div>
        <div class="overview-card-info">
            <div class="overview-card-title">${title}</div>
            <div class="overview-card-status ${statusClass}">${statusText}${actionHtml || ''}</div>
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
    if (deps.wireplumber) {
        const wp = deps.wireplumber;
        const cls = wp.running ? 'status-ok' : 'status-error';
        const txt = wp.running ? '运行中' : '未运行';
        pwItems += `<div class="dependency-item ${cls}"><span class="dep-name">wireplumber</span><span class="dep-desc">${wp.desc}</span><span class="dep-status">${txt}</span></div>`;
    }
    if (deps.pipewire_pulse) {
        const pwp = deps.pipewire_pulse;
        const cls = pwp.running ? 'status-ok' : 'status-error';
        const txt = pwp.running ? '运行中' : '未运行';
        pwItems += `<div class="dependency-item ${cls}"><span class="dep-name">pipewire-pulse</span><span class="dep-desc">${pwp.desc}</span><span class="dep-status">${txt}</span></div>`;
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
    if (deps.bluetooth_hardware !== undefined && !deps.bluetooth_hardware) {
        // 无蓝牙硬件时显示硬件缺失提示（warning），不显示端点未注册（error）
        btStackItems += `<div class="dependency-item status-warning"><span class="dep-name">蓝牙硬件</span><span class="dep-desc">USB 蓝牙适配器</span><span class="dep-status">未检测到</span></div>`;
    } else if (deps.bluetooth_audio_ready !== undefined) {
        const ready = deps.bluetooth_audio_ready;
        btStackItems += `<div class="dependency-item ${ready ? 'status-ok' : 'status-error'}"><span class="dep-name">蓝牙音频端点</span><span class="dep-desc">BlueZ MediaEndpoint1 注册状态</span><span class="dep-status">${ready ? '已注册' : '未注册'}</span></div>`;
    }

    let btToolItems = '';
    filterByType(deps.commands, 'bluetooth').forEach(c => { btToolItems += renderItem(c, 'command'); });

    let systemItems = '';
    filterByType(deps.services, 'system').forEach(s => { systemItems += renderItem(s, 'service'); });

    let pythonBindItems = filterByType(deps.packages, 'python').filter(p => ['python3-dbus', 'python3-gi'].includes(p.name)).map(p => renderItem(p, 'package')).join('');
    let pythonWebItems = filterByType(deps.packages, 'python').filter(p => ['python3-fastapi', 'python3-uvicorn'].includes(p.name)).map(p => renderItem(p, 'package')).join('');

    let leftCol = '';
    leftCol += '<div class="dependency-section"><h3>▸ PipeWire 服务状态</h3><div class="dependency-list">' + pwItems + '</div></div>';
    leftCol += '<div class="dependency-section"><h3>▸ 音频核心组件</h3><div class="dependency-list">' + audioCoreItems + '</div></div>';
    leftCol += '<div class="dependency-section"><h3>▸ 音频工具</h3><div class="dependency-list">' + audioToolItems + '</div></div>';

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
    if (!fixAllBtn) return;
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

    // ── 文件传输事件绑定 ──
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

    // 文件传输区域折叠切换
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

    // 初始化文件传输状态
    loadObexReceiveStatus();
    refreshTransferList();
    loadReceivedFiles();

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
