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
function isInputDeviceType(deviceType) {
    return ['input-keyboard', 'input-mouse', 'input-gaming', 'input-joystick', 'input-tablet'].includes(deviceType);
}

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
        
        if (!hasActive) {
            const firstAvailable = selectEl.querySelector('option:not([disabled])');
            if (firstAvailable) firstAvailable.selected = true;
        }
        
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

// 状态锁：用户操作时记录目标状态，pw-mon 实时推送匹配目标后释放；3s 兜底超时强制释放。
// 容差：音量±2%、静音精确匹配、平衡±0.05。
const _ADJUST_TOLERANCE_VOL = 2;
const _ADJUST_TOLERANCE_BAL = 0.05;
const _ADJUST_FALLBACK_MS = 3000;

// _adjustingDevices: Map<deviceName, { targetVolume?, targetMuted?, targetBalance?, opType, expireTs }>
const _adjustingDevices = new Map();

function _markDeviceAdjusting(deviceName, target) {
    if (!deviceName) return;
    const expire = Date.now() + _ADJUST_FALLBACK_MS;
    const existing = _adjustingDevices.get(deviceName);
    // 合并目标：新操作覆盖同类型目标，保留其他类型目标
    const merged = existing ? { ...existing } : { opType: target.opType };
    Object.assign(merged, target);
    merged.expireTs = expire;
    _adjustingDevices.set(deviceName, merged);
}

function _clearDeviceAdjusting(deviceName) {
    if (!deviceName) return;
    _adjustingDevices.delete(deviceName);
}

function _isDeviceAdjusting(deviceName) {
    const state = _adjustingDevices.get(deviceName);
    if (!state) return false;
    if (Date.now() >= state.expireTs) {
        _adjustingDevices.delete(deviceName);
        return false;
    }
    return true;
}

// 状态匹配：实际状态与目标比较，匹配则释放锁。返回 true 表示已释放，false 表示仍在调整中。
function _tryCompleteAdjusting(deviceName, payload) {
    const state = _adjustingDevices.get(deviceName);
    if (!state) return false;
    if (Date.now() >= state.expireTs) {
        _adjustingDevices.delete(deviceName);
        return false;
    }
    let matched = false;
    if (state.targetVolume !== undefined && typeof payload.volume === 'number') {
        if (Math.abs(payload.volume - state.targetVolume) <= _ADJUST_TOLERANCE_VOL) {
            matched = true;
        }
    }
    if (state.targetMuted !== undefined && typeof payload.muted === 'boolean') {
        if (payload.muted === state.targetMuted) {
            matched = true;
        }
    }
    if (state.targetBalance !== undefined && Array.isArray(payload.channels) && payload.channels.length >= 2) {
        // 反算实际 balance: (right - left) / (right + left)
        const left = payload.channels[0];
        const right = payload.channels[1];
        const sum = left + right;
        const actualBal = sum > 0 ? (right - left) / sum : 0;
        if (Math.abs(actualBal - state.targetBalance) <= _ADJUST_TOLERANCE_BAL) {
            matched = true;
        }
    }
    if (matched) {
        _adjustingDevices.delete(deviceName);
    }
    return matched;
}

async function setVolume(deviceName, volume) {
    _markDeviceAdjusting(deviceName, { opType: 'volume', targetVolume: volume });
    try {
        const result = await apiCall('/api/audio/volume', {
            method: 'POST',
            body: JSON.stringify({ device: deviceName, volume })
        });
        const data = result.data || {};
        if (result.success) {
            const verified = data.verified_volume ?? volume;
            // 后端校准值与目标差异大时，更新目标为校准值（设备物理限制）
            if (Math.abs(verified - volume) > _ADJUST_TOLERANCE_VOL) {
                _markDeviceAdjusting(deviceName, { opType: 'volume', targetVolume: verified });
            }
            const displayVol = Math.abs(verified - volume) <= 5 ? volume : verified;
            _updateChannelDisplay(deviceName, data.channels, displayVol);
        }
    } catch (error) {
        showToast('设置音量失败: ' + error.message, 'error');
        _clearDeviceAdjusting(deviceName);
    }
}

async function toggleMute(deviceName) {
    const btn = document.querySelector(`[data-action="toggleMute"][data-device="${CSS.escape(deviceName)}"]`);
    if (!btn) return;
    const wasMuted = btn.classList.contains('muted');
    const targetMuted = !wasMuted;
    const card = btn.closest('.device-card');
    const slider = card?.querySelector('.volume-slider');

    _markDeviceAdjusting(deviceName, { opType: 'mute', targetMuted });

    const svgMuted = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>';
    const svgUnmuted = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>';

    btn.classList.toggle('muted', targetMuted);
    btn.innerHTML = targetMuted ? svgMuted : svgUnmuted;
    if (card) {
        const volText = card.querySelector('.volume-text');
        if (volText) {
            volText.textContent = wasMuted ? `${slider?.value || 0}%` : '静音';
            volText.classList.toggle('muted-text', targetMuted);
        }
    }

    try {
        await apiCall('/api/audio/mute', {
            method: 'POST',
            body: JSON.stringify({ device: deviceName, mute: targetMuted })
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
        _clearDeviceAdjusting(deviceName);
    }
}

let _balanceTimers = {};

async function setBalance(deviceName, balance) {
    _markDeviceAdjusting(deviceName, { opType: 'balance', targetBalance: balance / 100 });
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
        _clearDeviceAdjusting(deviceName);
    }
}
