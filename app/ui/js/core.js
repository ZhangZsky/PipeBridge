// 统一网关前缀推导：飞牛 fnOS 通过 /app/PipeBridge 转发请求，前端所有绝对路径（/api、/api/events、静态资源）
// 都需带上该前缀才能命中网关路由。从当前页面路径中截取 /app/{appname} 作为前缀，
// 若未匹配到网关前缀（如本地直连调试），则回退为空前缀，兼容直连访问。
const GATEWAY_PREFIX = (function () {
    const m = window.location.pathname.match(/^\/app\/[^/]+/);
    return m ? m[0] : '';
})();
const API_BASE = window.location.origin + GATEWAY_PREFIX;

function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function escapeAttr(str) {
    return escapeHtml(str);
}

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
    'input-joystick': '游戏手柄',
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
    'other': '其他'
};

let currentTab = 'system';
let isLoading = false;
let scannedDevices = [];
let currentController = null;
let reconnectMonitorData = null;

const FORM_FACTOR_LABELS = {
    'internal': '内置', 'speaker': '音箱', 'headset': '耳机',
    'handset': '手持', 'tv': '电视', 'webcam': '摄像头',
    'microphone': '麦克风', 'car': '车载', 'hifi': 'Hi-Fi',
    'computer': '电脑', 'portable': '便携', 'laptop': '笔记本',
    'headphone': '头戴耳机', 'phone': '手机', 'btspeaker': '蓝牙音箱',
    'monitor': '显示器', 'projector': '投影仪',
};

function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    
    if (container.querySelector(`.toast-${type}`)?.textContent === message) return;
    
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

// 蓝牙"启动中"自愈轮询：SSE bt_status 快照可能因初始化竞态漏发 bluetooth.changed，
// 导致 starting→active 界面卡住。此处 200ms 兜底轮询，一旦离开 starting 立即停止。
let _btStartingTimer = null;
let _btStartingBusy = false;

function _startBtStartingWatch() {
    if (_btStartingTimer) return;
    _btStartingTimer = setInterval(() => {
        if (_btStartingBusy) return;
        _btStartingBusy = true;
        Promise.resolve()
            .then(() => updateBluetoothStatus())
            .catch(e => console.warn('bt starting watch error:', e))
            .finally(() => { _btStartingBusy = false; });
    }, 200);
}

function _stopBtStartingWatch() {
    if (_btStartingTimer) {
        clearInterval(_btStartingTimer);
        _btStartingTimer = null;
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
                <p>正在与 <strong>${escapeHtml(displayName)}</strong> 配对...</p>
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
            // 后端业务异常以 4xx/5xx + {success:false, error, code} 返回，
            // 优先读取响应体中的 error 文案作为错误信息，读取失败再退回通用 HTTP 文案，
            // 使 catch 分支的 error.message 能展示后端真实原因而非 "HTTP error!"。
            let msg = `HTTP error! status: ${response.status}`;
            try {
                const errBody = await response.json();
                if (errBody && errBody.error) msg = errBody.error;
            } catch (_) { /* 响应体非 JSON 或为空，沿用通用文案 */ }
            throw new Error(msg);
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