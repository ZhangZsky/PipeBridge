async function getVideoDevices() {
    try {
        const result = await apiCall('/api/video/devices');
        return result.data || { devices: [] };
    } catch (e) {
        return { devices: [] };
    }
}

async function getVideoStreams() {
    try {
        const result = await apiCall('/api/video/streams');
        return result.data || [];
    } catch (e) {
        return [];
    }
}

async function routeVideoStream(streamId, targetDevice) {
    return apiCall('/api/video/route/stream', {
        method: 'POST',
        body: JSON.stringify({ stream_id: streamId, target_device: targetDevice })
    });
}

async function unlinkVideoStream(streamId, linkId) {
    return apiCall('/api/video/route/stream', {
        method: 'DELETE',
        body: JSON.stringify({ stream_id: streamId, link_id: linkId })
    });
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
        await renderVideoDevices();
    } catch (error) {
        showToast('设置默认视频设备失败: ' + error.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.style.opacity = ''; }
    }
}

async function renderVideoDevices() {
    const container = document.getElementById('videoDeviceList');
    if (!container) return;

    const result = await getVideoDevices();
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
                <p class="empty-state-sub">HDMI/DP 显示器等输出设备连接后会自动显示</p>
            </div>
        `;
        return;
    }

    _renderVideoList(container, devices, defaultVideo);
    await renderVideoStreams(devices);
}

function _renderVideoList(container, devices, defaultVideo) {
    container.innerHTML = devices.map(device => {
        const isDefault = device.is_default || device.name === defaultVideo;
        return renderDeviceCard('video', device, { isDefault });
    }).join('');
    _bindVideoActions(container);
}

async function renderVideoStreams(devices) {
    const container = document.getElementById('videoStreamList');
    if (!container) return;

    const streams = await getVideoStreams();
    if (!streams || streams.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polygon points="5 3 19 12 5 21 5 3"/>
                </svg>
                <p>暂无活动视频流</p>
                <p class="empty-state-sub">应用输出视频流后可在此路由到指定输出</p>
            </div>
        `;
        return;
    }

    // 可路由目标：仅 PipeWire 视频输出设备（排除 DRM 显示器 card/HDMI/DP）
    const targets = (devices || []).filter(d => {
        const n = (d.name || '').toUpperCase();
        return !(n.startsWith('CARD') || n.includes('HDMI') || n.includes('DP-'));
    });

    container.innerHTML = streams.map(s => {
        const title = s.friendly_name || s.name || `流 #${s.node_id}`;
        const app = s.application ? `<span class="stream-app">${s.application}</span>` : '';
        const connected = (s.connected_outputs || []).length
            ? (s.connected_outputs || []).join(', ')
            : '未连接';
        const targetOpts = targets.map(t =>
            `<option value="${t.name}">${t.friendly_name || t.name}</option>`
        ).join('');
        const links = (s.links || []).map(l =>
            `<div class="stream-link-row">
                <span>${l.connected_node_name || l.connected_node_id || '端口'} (link ${l.link_id})</span>
                <button class="btn btn-secondary btn-sm stream-unlink-btn" data-stream="${s.node_id}" data-link="${l.link_id}">断开</button>
            </div>`
        ).join('');
        return `
            <div class="device-card stream-card">
                <div class="device-card-header">
                    <div class="device-name">${title} ${app}</div>
                    <div class="device-sub">${s.media_class} · 已连接: ${connected}</div>
                </div>
                <div class="stream-route-row">
                    <select class="stream-target-select" data-stream="${s.node_id}">
                        <option value="">选择输出设备…</option>
                        ${targetOpts}
                    </select>
                    <button class="btn btn-primary btn-sm stream-route-btn" data-stream="${s.node_id}">路由</button>
                </div>
                ${links ? `<div class="stream-links">${links}</div>` : ''}
            </div>
        `;
    }).join('');

    _bindStreamActions(container);
}

function _bindStreamActions(container) {
    container.querySelectorAll('.stream-route-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            const streamId = btn.dataset.stream;
            const sel = container.querySelector(`.stream-target-select[data-stream="${streamId}"]`);
            const target = sel ? sel.value : '';
            if (!target) { showToast('请先选择目标输出设备', 'info'); return; }
            btn.disabled = true;
            try {
                const res = await routeVideoStream(streamId, target);
                showToast((res.data && res.data.message) || '视频流路由成功', 'success');
                await renderVideoDevices();
            } catch (err) {
                showToast('路由失败: ' + err.message, 'error');
            } finally {
                btn.disabled = false;
            }
        });
    });

    container.querySelectorAll('.stream-unlink-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            btn.disabled = true;
            try {
                await unlinkVideoStream(btn.dataset.stream, btn.dataset.link);
                showToast('已断开视频链接', 'success');
                await renderVideoDevices();
            } catch (err) {
                showToast('断开失败: ' + err.message, 'error');
            } finally {
                btn.disabled = false;
            }
        });
    });
}

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

    container.querySelectorAll('.display-layout-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const connector = btn.dataset.connector;
            
            const allConnectors = [];
            container.querySelectorAll('.display-layout-btn').forEach(b => {
                const c = b.dataset.connector;
                if (c && c !== connector) allConnectors.push(c);
            });
            if (allConnectors.length === 0) {
                showToast('仅有一个显示器，无需配置布局', 'info');
                return;
            }
            
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
            
            document.getElementById('layoutScale').addEventListener('input', (ev) => {
                document.getElementById('layoutScaleVal').textContent = ev.target.value + 'x';
            });
            
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
                    
                    if (rotation !== 'normal') {
                        await apiCall('/api/video/display-rotation', {
                            method: 'POST',
                            body: JSON.stringify({ output: connector, rotation })
                        });
                    }
                    
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

    container.querySelectorAll('.video-res-select').forEach(sel => {
        
        const currentRes = sel.dataset.currentRes;
        if (currentRes) sel.value = currentRes;
        
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

function _updateVideoRateOptions(resSelect) {
    const connector = resSelect.dataset.connector;
    const rateSelect = resSelect.closest('.device-details')?.querySelector(`.video-rate-select[data-connector="${connector}"]`);
    if (!rateSelect) return;
    const selectedRes = resSelect.value;
    rateSelect.innerHTML = '<option value="">自动</option>';
    if (!selectedRes) return;
    
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
    // 默认选中设备当前刷新率：仅当选择的分辨率与设备当前分辨率一致时才应用，
    // 避免用户切换到其它分辨率后错误地把当前刷新率标为选中。
    const currentRate = resSelect.dataset.currentRate;
    const currentRes = resSelect.dataset.currentRes;
    if (currentRate && selectedRes === currentRes) {
        const match = Array.from(rateSelect.options).find(
            o => o.value && Math.round(parseFloat(o.value)) === Math.round(parseFloat(currentRate))
        );
        if (match) rateSelect.value = match.value;
    }
}

async function _applyVideoDisplaySettings(el) {
    const connector = el.dataset.connector;
    const details = el.closest('.device-details');
    const resSelect = details?.querySelector(`.video-res-select[data-connector="${connector}"]`);
    const rateSelect = details?.querySelector(`.video-rate-select[data-connector="${connector}"]`);
    if (!resSelect || !rateSelect) return;
    const resolution = resSelect.value;
    const refreshRate = rateSelect.value;
    if (!resolution && !refreshRate) return; 
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
