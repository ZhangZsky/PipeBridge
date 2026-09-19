
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

    const deps = data.dependencies || {};

    const pwRunning = !!data.pipewire;
    const wpRunning = !!data.wireplumber;
    const btRunning = !!data.bluetooth_service;
    const btAudioReady = !!data.bluetooth_audio_ready;
    const spaPluginOk = !!data.spa_bluetooth_plugin;
    // 蓝牙硬件标记在 overview 顶层(data.bluetooth_hardware)，非 dependencies 内；
    // 兼容旧结构再回退 deps，最终缺省 true(有硬件)避免误报"无硬件"。
    const btHardware = data.bluetooth_hardware !== undefined
        ? !!data.bluetooth_hardware
        : (deps.bluetooth_hardware !== undefined ? !!deps.bluetooth_hardware : true);

    let btAudioLabel, btAudioStatus;
    if (!btHardware) {
        btAudioLabel = '无硬件';
        btAudioStatus = 'warning';
    } else if (btAudioReady) {
        btAudioLabel = '就绪';
        btAudioStatus = 'ok';
    } else if (!spaPluginOk) {
        btAudioLabel = '插件缺失';
        btAudioStatus = 'error';
    } else if (btRunning) {
        // 蓝牙服务运行中但音频端点未就绪：端点正在注册，显示启动中
        btAudioLabel = '启动中...';
        btAudioStatus = 'warning';
    } else {
        btAudioLabel = '未运行';
        btAudioStatus = 'error';
    }

    let statusRow = `<div class="overview-status-row">
        ${_overviewCard('PipeWire', pwRunning ? '运行中' : '未运行', pwRunning ? 'ok' : 'error', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>')}
        ${_overviewCard('WirePlumber', wpRunning ? '运行中' : '未运行', wpRunning ? 'ok' : 'error', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9"/></svg>')}
        ${_overviewCard('蓝牙服务', btRunning ? '运行中' : '未运行', btRunning ? 'ok' : 'error', '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6.5 6.5l11 11L12 23V1l5.5 5.5-11 11"/></svg>', btRunning ? `<button class="btn btn-sm btn-secondary svc-restart-btn" data-service="bluetooth" title="重启蓝牙服务">重启</button>` : `<button class="btn btn-sm btn-primary svc-start-btn" data-service="bluetooth" title="启动蓝牙服务">启动</button>`)}
        ${_overviewCard('蓝牙音频', btAudioLabel, btAudioStatus, '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6.5 6.5l11 11L12 23V1l5.5 5.5-11 11"/></svg>')}
    </div>`;

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
    
    if (deps.bluetooth_hardware !== undefined && !deps.bluetooth_hardware) {
        _collectDepDot('蓝牙硬件', false, false);
    } else if (deps.bluetooth_audio_ready !== undefined) {
        _collectDepDot('蓝牙音频端点', deps.bluetooth_audio_ready, true);
    }
    
    const pw = deps.pipewire;
    const pwOk = pw && pw.running;
    const btSvc = (deps.services || []).find(s => s.type === 'bluetooth') || {};
    const btOk = btSvc.active;

    const dotHtml = depDots.map(d =>
        `<span class="dep-dot ${d.error ? 'error' : (d.ok ? 'ok' : 'warn')}" title="${d.name}: ${d.error ? '异常' : (d.ok ? '正常' : '警告')}"></span>`
    ).join('');

    let depContentHtml = '';
    if (!allOk && deps.critical_missing && deps.critical_missing.length > 0) {
        depContentHtml = `<div class="dependency-warning"><strong>缺少关键依赖:</strong> ${deps.critical_missing.join(', ')}</div>`;
    }
    const depContent = depContentHtml + _renderDepSections({...deps, spa_bluetooth_plugin: data.spa_bluetooth_plugin});

    const allOkBanner = allOk
        ? '<div class="dependency-all-ok"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg><span>所有依赖正常</span></div>'
        : '';

    container.innerHTML = statusRow + statsRow + allOkBanner + `
        <div class="controller-card ${_depCardExpanded ? '' : 'collapsed'}" id="depCard">
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
        <div class="controller-card ${_logCardExpanded ? '' : 'collapsed'}" id="logCard">
            <div class="controller-summary" id="logSummary">
                <div class="controller-summary-left">
                    <div class="status-dot active"></div>
                    <span class="controller-summary-name">运行日志</span>
                    <span class="log-summary-hint">查看运行 / 安装日志，支持全量导出</span>
                </div>
                <svg class="controller-expand-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="6 9 12 15 18 9"/>
                </svg>
   </div>
            <div class="controller-detail">
                <div class="log-toolbar">
                    <div class="log-tabs">
                        <button class="log-tab ${_currentLogType === 'runtime' ? 'active' : ''}" data-logtype="runtime">运行日志</button>
                        <button class="log-tab ${_currentLogType === 'install' ? 'active' : ''}" data-logtype="install">安装日志</button>
                    </div>
                    <div class="log-actions">
                        <button class="btn btn-sm btn-secondary" id="logRefreshBtn" title="刷新当前日志">刷新</button>
                        <button class="btn btn-sm btn-primary" id="logExportBtn" title="导出全部日志(安装 + 运行)为 zip">导出日志</button>
                    </div>
                </div>
                <div class="log-meta" id="logMeta"></div>
                <pre class="log-viewport" id="logViewport">点击展开以加载日志…</pre>
            </div>
        </div>
    `;

    document.getElementById('depSummary').addEventListener('click', () => {
        const depCard = document.getElementById('depCard');
        depCard.classList.toggle('collapsed');
        _depCardExpanded = !depCard.classList.contains('collapsed');
    });

    _bindLogCard();

    container.querySelectorAll('.svc-restart-btn, .svc-start-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const service = btn.dataset.service;
            const action = btn.classList.contains('svc-restart-btn') ? 'restart' : 'start';
            await _runServiceActionWithStatus(service, action, btn);
        });
    });

    // 系统组件未就绪时由 RefreshManager 统一处理：EventDetector 3s 检测 → SSE system.changed → 前端刷新。
}

// 运行日志栏当前选中的日志类型(runtime / install)，跨重新渲染保留用户选择。
let _currentLogType = 'runtime';
// 依赖详情 / 运行日志栏的展开态与日志加载态提升到模块级：
// 系统页由 SSE(system.changed) 驱动整体重绘，若展开态只存在于 DOM class 上，
// 重绘会把用户刚展开的卡片重新折叠（表现为"点开日志一两秒后自动收起"）。
let _depCardExpanded = false;
let _logCardExpanded = false;
let _logLoadedOnce = false;
// 日志内容缓存(按类型)：系统页由 SSE(system.changed) 驱动整体重绘，重绘会把日志区
// 重建为占位文本、补拉又先写"加载中…"再整体替换——三次跳变即"日志闪烁"。
// 缓存最近内容用于重绘后立即回填(无空窗)，补拉与 DOM diff，未变化不写 DOM。
let _logCache = {};

// 绑定"运行日志"栏的展开、切换、刷新、导出交互。
function _bindLogCard() {
    const summary = document.getElementById('logSummary');
    const card = document.getElementById('logCard');
    if (!summary || !card) return;

    // 重绘后若日志栏本就展开且此前已加载过：先用缓存立即回填(重绘把 viewport 重建为
    // 占位文本，回填消除内容空窗)，再节流补拉(2s 内拉过则跳过，避免高频 system.changed
    // 下每秒重拉 500 行)。
    if (_logCardExpanded && _logLoadedOnce) {
        const viewport = document.getElementById('logViewport');
        const meta = document.getElementById('logMeta');
        const cached = _logCache[_currentLogType];
        if (viewport && cached && cached.content) {
            viewport.textContent = cached.content;
            if (meta) meta.textContent = cached.meta || '';
            // 恢复用户浏览位置：拉取前在底部则仍跟随最新，否则回到原滚动位置
            viewport.scrollTop = cached.atBottom ? viewport.scrollHeight : cached.scrollTop;
        }
        if (!cached || Date.now() - cached.ts > 2000) {
            _loadLog(_currentLogType, { silent: true });
        }
    }

    summary.addEventListener('click', () => {
        card.classList.toggle('collapsed');
        _logCardExpanded = !card.classList.contains('collapsed');
        // 首次展开时才拉取日志，避免每次渲染系统页都请求。
        if (_logCardExpanded && !_logLoadedOnce) {
            _logLoadedOnce = true;
            _loadLog(_currentLogType);
        }
    });

    card.querySelectorAll('.log-tab').forEach(tab => {
        tab.addEventListener('click', (e) => {
            e.stopPropagation();
            const type = tab.dataset.logtype;
            if (type === _currentLogType) return;
            _currentLogType = type;
            card.querySelectorAll('.log-tab').forEach(t => t.classList.toggle('active', t === tab));
            // 切换类型内容必然更换，先清为"加载中…"作反馈(与补拉/刷新的 diff 更新不同)
            _loadLog(type, { clearFirst: true });
        });
    });

    const refreshBtn = document.getElementById('logRefreshBtn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            // 保留旧内容后台拉取(不清空)，完成后内容有变才替换——手动刷新不闪烁
            refreshBtn.disabled = true;
            try { await _loadLog(_currentLogType); }
            finally { refreshBtn.disabled = false; }
        });
    }

    const exportBtn = document.getElementById('logExportBtn');
    if (exportBtn) {
        exportBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            // 全量导出走浏览器直接下载(响应是 zip 二进制，不能经 apiCall 的 JSON 契约)。
            window.open(`${API_BASE}/api/system/logs/export`, '_blank');
            showToast('正在导出安装日志与运行日志…', 'info');
        });
    }
}

// 拉取并渲染指定类型日志的尾部内容。
// clearFirst: tab 切换场景，内容必然更换，先清为"加载中…"作反馈；
// silent: SSE 重绘补拉场景，不起手清空，拉到后与当前显示 diff，未变不写 DOM，失败静默；
// 默认(手动刷新/首次展开)：保留旧内容后台拉取，完成后 diff 替换，失败写错误提示。
async function _loadLog(type, { clearFirst = false, silent = false } = {}) {
    const viewport = document.getElementById('logViewport');
    const meta = document.getElementById('logMeta');
    if (!viewport) return;
    // 记录拉取前的浏览位置：用户翻阅历史时刷新不应把他拽回底部
    const atBottom = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 30;
    const prevScrollTop = viewport.scrollTop;
    if (clearFirst) {
        viewport.textContent = '加载中…';
        if (meta) meta.textContent = '';
    }
    try {
        const result = await apiCall(`/api/system/logs?type=${encodeURIComponent(type)}&lines=500`);
        const d = result.data || {};
        const lines = d.lines || [];
        const content = !d.exists
            ? (type === 'install' ? '暂无安装日志' : '暂无运行日志')
            : (lines.length === 0 ? '(日志为空)' : lines.join('\n'));
        const truncated = d.exists && d.total > d.returned;
        const metaText = truncated ? `共 ${d.total} 行，仅显示最新 ${d.returned} 行` : '';
        // 内容与当前显示一致则不写 DOM：重绘补拉时日志通常未变，跳过写入即消除闪烁
        if (content === viewport.textContent && metaText === (meta ? meta.textContent : '')) {
            _logCache[type] = { content, meta: metaText, ts: Date.now(), atBottom, scrollTop: prevScrollTop };
            return;
        }
        viewport.textContent = content;
        if (meta) meta.textContent = metaText;
        // 内容确有更新：拉取前在底部则跟随最新日志，否则保持用户滚动位置
        viewport.scrollTop = atBottom ? viewport.scrollHeight : prevScrollTop;
        _logCache[type] = { content, meta: metaText, ts: Date.now(), atBottom, scrollTop: prevScrollTop };
    } catch (e) {
        if (silent) {
            console.warn('日志后台刷新失败:', e);
            return;
        }
        viewport.textContent = '日志加载失败: ' + (e.message || e);
        if (meta) meta.textContent = '';
    }
}

// 服务启动/重启：用 toast 序列显示分步进度（与测试音等操作一致）
// 蓝牙服务重启时整合 USB 适配器重置：先 USB 硬件重置 → 再 systemctl restart → 等待激活 → 等待音频端点就绪
async function _runServiceActionWithStatus(service, action, btn) {
    const isBt = service === 'bluetooth';
    const actionLabel = action === 'restart' ? '重启' : '启动';
    const serviceLabel = isBt ? '蓝牙服务' : service;

    const origText = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = actionLabel + '中...'; }

    // 蓝牙重启时整合 USB 适配器重置，解决适配器卡死问题
    const usbReset = isBt && action === 'restart';
    if (usbReset) {
        showToast('正在重置 USB 蓝牙适配器...', 'info');
    } else {
        showToast(`正在${actionLabel}${serviceLabel}...`, 'info');
    }

    try {
        // 步骤 1：调用 API（蓝牙重启带 usb_reset 参数）
        const body = { service };
        if (usbReset) body.usb_reset = true;
        const result = await apiCall(`/api/system/service/${action}`, {
            method: 'POST',
            body: JSON.stringify(body)
        });
        if (!result.success) {
            showToast(safeToastData(result.error, `${serviceLabel}${actionLabel}失败`), 'error');
            return;
        }
        const didUsbReset = result.data && result.data.usb_reset;
        if (didUsbReset) {
            showToast('USB 适配器已重置，正在重启蓝牙服务...', 'info');
        } else {
            showToast(`${serviceLabel}已${actionLabel}，等待激活...`, 'info');
        }

        // 步骤 2：轮询等待服务激活
        // USB 重置后适配器需要更长时间初始化，延长超时
        const activateTimeout = didUsbReset ? 25000 : 15000;
        const activated = await _pollServiceStatus(service, activateTimeout, isBt);
        if (!activated) {
            showToast(`${serviceLabel}启动超时，请检查系统日志`, 'error');
            return;
        }

        // 步骤 3（仅蓝牙）：等待音频端点就绪
        if (isBt) {
            showToast('蓝牙服务已激活，等待音频端点就绪...', 'info');
            const audioTimeout = didUsbReset ? 30000 : 20000;
            const audioReady = await _pollBtAudioReady(audioTimeout);
            if (!audioReady) {
                showToast('蓝牙音频端点未就绪，请检查 SPA 插件', 'warning');
            } else {
                showToast(`${serviceLabel}${actionLabel}完成，音频端点已就绪`, 'success');
            }
        } else {
            showToast(`${serviceLabel}${actionLabel}完成`, 'success');
        }

        // 刷新系统概览
        const overview = await fetchSystemOverview();
        if (overview) renderSystemOverview(overview);
    } catch (error) {
        showToast('操作失败: ' + error.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = origText; }
    }
}

// 轮询系统概览，检查服务是否激活
async function _pollServiceStatus(service, timeoutMs, checkBtAudio) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
        try {
            const ov = await fetchSystemOverview();
            if (ov) {
                const svcRunning = _isServiceRunning(ov, service);
                if (svcRunning) return true;
            }
        } catch (e) { /* 忽略单次失败 */ }
        await new Promise(r => setTimeout(r, 1000));
    }
    return false;
}

function _isServiceRunning(ov, service) {
    if (service === 'bluetooth') return !!ov.bluetooth_service;
    const deps = ov.dependencies || {};
    const svcs = deps.services || [];
    const s = svcs.find(x => x.name === service || x.type === service);
    return s ? !!s.active : false;
}

// 轮询等待蓝牙音频端点就绪
async function _pollBtAudioReady(timeoutMs) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
        try {
            const ov = await fetchSystemOverview();
            if (ov && ov.bluetooth_audio_ready) return true;
        } catch (e) { /* 忽略单次失败 */ }
        await new Promise(r => setTimeout(r, 2000));
    }
    return false;
}

function _overviewCard(title, statusText, statusClass, iconSvg, actionHtml) {
    return `<div class="overview-card">
        <div class="overview-card-icon ${statusClass}">${iconSvg}</div>
        <div class="overview-card-info">
            <div class="overview-card-title">${title}</div>
            <div class="overview-card-status ${statusClass}">${statusText}</div>
        </div>
        ${actionHtml ? `<div class="overview-card-action">${actionHtml}</div>` : ''}
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
        
        btStackItems += `<div class="dependency-item status-warning"><span class="dep-name">蓝牙硬件</span><span class="dep-desc">USB 蓝牙适配器</span><span class="dep-status">未检测到</span></div>`;
    } else if (deps.bluetooth_audio_ready !== undefined) {
        const ready = deps.bluetooth_audio_ready;
        btStackItems += `<div class="dependency-item ${ready ? 'status-ok' : 'status-error'}"><span class="dep-name">蓝牙音频端点</span><span class="dep-desc">BlueZ MediaEndpoint1 注册状态</span><span class="dep-status">${ready ? '已注册' : '未注册'}</span></div>`;
    }

    let btToolItems = '';
    filterByType(deps.commands, 'bluetooth').forEach(c => { btToolItems += renderItem(c, 'command'); });

    let systemItems = '';
    filterByType(deps.services, 'system').forEach(s => { systemItems += renderItem(s, 'service'); });

    const pythonPkgs = filterByType(deps.packages, 'python');
    let pythonBindItems = pythonPkgs.filter(p => p.subtype === 'bind').map(p => renderItem(p, 'package')).join('');
    let pythonWebItems = pythonPkgs.filter(p => p.subtype === 'web').map(p => renderItem(p, 'package')).join('');
    // 无 subtype 的 python 包兜底归入 Web 分组，避免新增包漏显示。
    let pythonOtherItems = pythonPkgs.filter(p => p.subtype !== 'bind' && p.subtype !== 'web').map(p => renderItem(p, 'package')).join('');
    pythonWebItems += pythonOtherItems;

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
    showToast('正在检查和修复依赖...', 'info');
    try {
        const result = await apiCall('/api/system/fix', { method: 'POST' });
        if (result.success) {
            const data = result.data || {};
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
