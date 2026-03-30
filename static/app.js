/* Basic Workbench — Communication-first dashboard */

const API = '';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let selectedAgent = null;
let selectedChannel = null;
let terminalAgent = null;
let terminalTimer = null;

// ---------------------------------------------------------------------------
// Sidebar: Agents
// ---------------------------------------------------------------------------

async function loadAgents() {
    const agents = await apiFetch('/api/agents');
    const list = document.getElementById('agent-list');
    list.innerHTML = agents.map(a => `
        <li class="${selectedAgent === a.name ? 'active' : ''}"
            onclick="selectAgent('${esc(a.name)}')">
            <span class="agent-dot ${a.status}"></span>
            <span>${esc(a.name)}</span>
            <span class="agent-role">${esc(a.role || '')}</span>
        </li>
    `).join('') || '<li class="placeholder" style="padding:8px 16px;font-size:12px;">No agents yet</li>';
}

async function selectAgent(name) {
    selectedAgent = name;
    selectedChannel = null;
    loadAgents();
    loadChannelList();

    // Show agent pane
    showPane('pane-agent');
    document.getElementById('agent-pane-name').textContent = name;
    document.getElementById('agent-dm-input').placeholder = `Message ${name}...`;
    document.getElementById('agent-dm-input').focus();

    loadAgentStream(name);
    if (streamTimer) clearInterval(streamTimer);
    streamTimer = setInterval(() => loadAgentStream(selectedAgent), 3000);
}

// loadAgentMessages removed — using unified stream instead

// ---------------------------------------------------------------------------
// Sidebar: Channels
// ---------------------------------------------------------------------------

async function loadChannelList() {
    const channels = await apiFetch('/api/channels');
    const list = document.getElementById('channel-list');
    // Always show #general and #review, plus any with messages
    const names = new Set(['general', 'review']);
    channels.forEach(ch => names.add(ch.channel));
    const counts = {};
    channels.forEach(ch => { counts[ch.channel] = ch.message_count; });

    list.innerHTML = [...names].map(ch => `
        <li class="${selectedChannel === ch ? 'active' : ''}"
            onclick="selectChannel('${esc(ch)}')">
            <span>#${esc(ch)}</span>
            ${counts[ch] ? `<span class="agent-role">${counts[ch]}</span>` : ''}
        </li>
    `).join('');
}

async function selectChannel(name) {
    selectedChannel = name;
    selectedAgent = null;
    loadAgents();
    loadChannelList();

    showPane('pane-channel');
    document.getElementById('channel-pane-name').textContent = `#${name}`;
    document.getElementById('channel-input').placeholder = `Post to #${name}...`;
    document.getElementById('channel-input').focus();

    loadChannelMessages(name);
}

async function loadChannelMessages(channel) {
    const msgs = await apiFetch(`/api/channels/${channel}/messages`);
    const pane = document.getElementById('channel-messages');
    if (!msgs.length) {
        pane.innerHTML = `<p class="placeholder">No messages in #${esc(channel)} yet</p>`;
        return;
    }
    const wasAtBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 50;
    pane.innerHTML = msgs.map(renderMsg).join('');
    if (wasAtBottom) pane.scrollTop = pane.scrollHeight;
}

// ---------------------------------------------------------------------------
// Activity feed
// ---------------------------------------------------------------------------

function showActivityFeed() {
    selectedAgent = null;
    selectedChannel = null;
    loadAgents();
    loadChannelList();
    showPane('pane-activity');
    loadActivity();
}

async function loadActivity() {
    const msgs = await apiFetch('/api/activity?limit=100');
    const pane = document.getElementById('activity-feed');
    if (!msgs.length) {
        pane.innerHTML = '<p class="placeholder">No activity yet. Spawn some agents and start talking!</p>';
        return;
    }
    const wasAtBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 50;
    pane.innerHTML = msgs.map(m => {
        const time = m.created_at ? new Date(m.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : '';
        const sender = m.from_agent || '?';
        const isOp = sender === 'operator';
        const target = m.channel ? `#${m.channel}` : (m.to_agent ? `@${m.to_agent}` : '');
        const body = (m.body || '').slice(0, 200);
        return `
            <div class="activity-item" onclick="${m.channel ? `selectChannel('${esc(m.channel)}')` : (m.to_agent ? `selectAgent('${esc(m.to_agent)}')` : '')}">
                <span class="activity-time">${esc(time)}</span>
                <span class="activity-sender ${isOp ? 'op' : ''}">${esc(sender)}</span>
                <span class="activity-arrow">&rarr;</span>
                <span class="activity-target">${esc(target)}</span>
                <span class="activity-body">${esc(body)}</span>
            </div>
        `;
    }).join('');
    if (wasAtBottom) pane.scrollTop = pane.scrollHeight;
}

// ---------------------------------------------------------------------------
// Agent activity stream (terminal-style, merged activity + messages)
// ---------------------------------------------------------------------------

let streamTimer = null;

async function loadAgentStream(name) {
    if (!name) return;
    const [activity, msgs] = await Promise.all([
        apiFetch(`/api/agents/${name}/activity?limit=200`).catch(() => []),
        apiFetch(`/api/agents/${name}/messages?limit=100`).catch(() => []),
    ]);

    // Deduplicate: activity already includes user_message events that correspond to DB messages
    // Only add DB messages that are operator-originated (not already in activity)
    const items = [];
    for (const ev of activity) {
        items.push({ ts: ev.ts || '', src: 'activity', data: ev });
    }
    // Only add operator messages from DB (agent messages already appear as activity events)
    for (const m of msgs) {
        if (m.from_agent === 'operator') {
            items.push({ ts: m.created_at || '', src: 'message', data: m });
        }
    }
    items.sort((a, b) => a.ts.localeCompare(b.ts));

    const pane = document.getElementById('agent-stream');
    if (!items.length) {
        pane.innerHTML = '<p class="placeholder" style="padding:40px">Waiting for agent activity...</p>';
        return;
    }
    const wasAtBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 50;
    pane.innerHTML = items.map(item => {
        if (item.src === 'message') return renderStreamMsg(item.data);
        return renderStreamActivity(item.data);
    }).filter(Boolean).join('');
    if (wasAtBottom) pane.scrollTop = pane.scrollHeight;
}

function renderStreamActivity(ev) {
    const type = (ev.event || '').replace(/^agent\./, '').replace(/\.jsonl$/, '');
    const time = _fmtTime(ev.ts);
    let text = '', cls = 'act-out';

    switch (type) {
        case 'tool_call': {
            const tool = ev.tool || '?';
            const summary = ev.input_summary || '';
            if (tool === 'direct_message' || tool === 'post') {
                text = summary;
            } else if (tool === 'Bash' || tool === 'bash') {
                text = `$ ${summary}`;
            } else {
                text = summary ? `${tool.toLowerCase()}(${summary})` : tool.toLowerCase();
            }
            break;
        }
        case 'tool_result':
            if (!ev.is_error) return '';  // only show errors
            text = `\u23BF \u2717 ${(ev.result_summary || 'error').slice(0, 300)}`;
            cls = 'act-result act-error';
            break;
        case 'thinking':
            text = `thinking...`;
            cls = 'act-thinking';
            break;
        case 'message':
            text = (ev.text_preview || '').trim().slice(0, 400);
            cls = 'act-msg';
            break;
        case 'user_message': {
            const preview = (ev.text_preview || '').trim();
            if (!preview) return '';
            const sender = ev.sender || '';
            text = sender ? `\u2190 ${sender}: ${preview}` : `\u2190 ${preview}`;
            cls = 'act-in';
            break;
        }
        case 'action':
            return '';  // skip — redundant with tool_call
        case 'turn_complete':
        case 'usage':
            return '';  // skip noise
        default:
            return '';
    }
    if (!text.trim()) return '';
    return `<div class="act-line ${cls}"><span class="act-ts">${esc(time)}</span><span class="act-text">${esc(text)}</span></div>`;
}

function renderStreamMsg(m) {
    const time = _fmtTime(m.created_at);
    const sender = m.from_agent || '?';
    const isIncoming = sender === 'operator' || m.to_agent === selectedAgent;
    const cls = isIncoming ? 'act-in' : 'act-out';
    const arrow = isIncoming ? '\u2190' : '\u2192';
    const target = m.to_agent ? ` ${arrow} ${m.to_agent}` : (m.channel ? ` ${arrow} #${m.channel}` : '');
    return `<div class="act-line ${cls}"><span class="act-ts">${esc(time)}</span><span class="act-text">${esc(sender)}${target}: ${esc(m.body || '')}</span></div>`;
}

function _fmtTime(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    return isNaN(d) ? '' : d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
}

// ---------------------------------------------------------------------------
// Terminal overlay
// ---------------------------------------------------------------------------

document.getElementById('btn-agent-output').addEventListener('click', () => {
    if (!selectedAgent) return;
    showTerminal(selectedAgent);
});

async function showTerminal(name) {
    terminalAgent = name;
    document.getElementById('terminal-title').textContent = `${name} — Terminal`;
    document.getElementById('terminal-overlay').classList.remove('hidden');
    refreshTerminal();
    terminalTimer = setInterval(refreshTerminal, 3000);
}

async function refreshTerminal() {
    if (!terminalAgent) return;
    try {
        const agent = await apiFetch(`/api/agents/${terminalAgent}`);
        document.getElementById('terminal-output').textContent = agent.output || '(no output)';
    } catch (_) {}
}

function closeTerminal() {
    document.getElementById('terminal-overlay').classList.add('hidden');
    terminalAgent = null;
    if (terminalTimer) { clearInterval(terminalTimer); terminalTimer = null; }
}

// ---------------------------------------------------------------------------
// Agent actions
// ---------------------------------------------------------------------------

document.getElementById('btn-agent-stop').addEventListener('click', async () => {
    if (!selectedAgent) return;
    if (!confirm(`Stop agent "${selectedAgent}"?`)) return;
    await apiFetch(`/api/agents/${selectedAgent}`, { method: 'DELETE' });
    loadAgents();
});

document.getElementById('btn-agent-restart').addEventListener('click', async () => {
    if (!selectedAgent) return;
    await apiFetch(`/api/agents/${selectedAgent}/restart`, { method: 'POST' });
    loadAgents();
});

// ---------------------------------------------------------------------------
// Compose: send messages
// ---------------------------------------------------------------------------

document.getElementById('form-agent-dm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const input = document.getElementById('agent-dm-input');
    const text = input.value.trim();
    if (!text || !selectedAgent) return;
    try {
        await apiFetch(`/api/agents/${selectedAgent}/messages`, {
            method: 'POST', body: JSON.stringify({ body: text })
        });
        input.value = '';
        loadAgentMessages(selectedAgent);
    } catch (err) {
        alert(`Failed: ${err.message}`);
    }
});

document.getElementById('form-channel-post').addEventListener('submit', async (e) => {
    e.preventDefault();
    const input = document.getElementById('channel-input');
    const text = input.value.trim();
    if (!text || !selectedChannel) return;
    try {
        await apiFetch(`/api/channels/${selectedChannel}/messages`, {
            method: 'POST', body: JSON.stringify({ body: text })
        });
        input.value = '';
        loadChannelMessages(selectedChannel);
    } catch (err) {
        alert(`Failed: ${err.message}`);
    }
});

// ---------------------------------------------------------------------------
// Add Agent Modal
// ---------------------------------------------------------------------------

document.getElementById('btn-add-agent').addEventListener('click', () => {
    document.getElementById('modal-add-agent').classList.remove('hidden');
    document.querySelector('#form-add-agent [name="role"]').value = 'developer';
});

document.getElementById('btn-add-reviewer').addEventListener('click', () => {
    document.getElementById('modal-add-agent').classList.remove('hidden');
    const form = document.getElementById('form-add-agent');
    form.querySelector('[name="role"]').value = 'reviewer';
    form.querySelector('[name="name"]').value = 'reviewer';
    form.querySelector('[name="prompt"]').value = '';
});

function closeModal() {
    document.getElementById('modal-add-agent').classList.add('hidden');
    document.getElementById('form-add-agent').reset();
}

document.getElementById('form-add-agent').addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const data = {
        name: form.name.value.trim(),
        cwd: form.cwd.value.trim(),
        role: form.role.value,
        model: form.model.value,
        prompt: form.prompt.value.trim(),
    };
    try {
        await apiFetch('/api/agents', { method: 'POST', body: JSON.stringify(data) });
        closeModal();
        loadAgents();
    } catch (err) {
        alert(`Failed: ${err.message}`);
    }
});

// ---------------------------------------------------------------------------
// SSE — Real-time updates
// ---------------------------------------------------------------------------

let eventSource = null;
let heartbeatTimer = null;

function connectSSE() {
    const dot = document.getElementById('connection-status');
    eventSource = new EventSource(`${API}/api/feed/stream`);

    eventSource.addEventListener('system.heartbeat', () => {
        dot.className = 'status-dot connected';
        dot.title = 'Connected';
        resetHeartbeat();
    });

    eventSource.addEventListener('new_message', () => {
        if (selectedAgent) loadAgentStream(selectedAgent);
        if (selectedChannel) loadChannelMessages(selectedChannel);
        if (document.getElementById('pane-activity').classList.contains('active')) loadActivity();
        loadChannelList();
    });

    eventSource.addEventListener('agent_status_change', () => {
        loadAgents();
    });

    eventSource.onopen = () => {
        dot.className = 'status-dot connected';
        dot.title = 'Connected';
        resetHeartbeat();
    };
    eventSource.onerror = () => {
        dot.className = 'status-dot reconnecting';
        dot.title = 'Reconnecting...';
    };
}

function resetHeartbeat() {
    if (heartbeatTimer) clearTimeout(heartbeatTimer);
    heartbeatTimer = setTimeout(() => {
        if (eventSource) { eventSource.close(); eventSource = null; }
        connectSSE();
    }, 45000);
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function showPane(id) {
    document.querySelectorAll('.pane').forEach(p => p.classList.remove('active'));
    document.getElementById(id).classList.add('active');
}

async function apiFetch(path, opts = {}) {
    const res = await fetch(`${API}${path}`, {
        headers: { 'Content-Type': 'application/json', ...opts.headers },
        ...opts,
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({ error: res.statusText }));
        throw new Error(err.error || res.statusText);
    }
    return res.json();
}

function esc(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function renderMsg(m) {
    const time = m.created_at ? new Date(m.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : '';
    const sender = m.from_agent || '?';
    const isOp = sender === 'operator';
    const target = m.to_agent ? ` &rarr; ${esc(m.to_agent)}` : (m.channel ? ` in #${esc(m.channel)}` : '');
    return `
        <div class="msg">
            <div class="msg-header">
                <span class="msg-sender ${isOp ? 'operator' : ''}">${esc(sender)}</span>
                <span class="msg-meta">${target} &middot; ${esc(time)}</span>
            </div>
            <div class="msg-body">${esc(m.body)}</div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

loadAgents();
loadChannelList();
showActivityFeed();
connectSSE();
