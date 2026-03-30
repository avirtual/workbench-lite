/* Basic Workbench — Communication-first dashboard */

const API = '';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let selectedAgent = null;
let selectedChannel = null;
let terminalAgent = null;
let terminalTimer = null;
let refreshTimer = null;

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

    loadAgentMessages(name);
    startMessageRefresh(() => loadAgentMessages(selectedAgent));
}

async function loadAgentMessages(name) {
    const msgs = await apiFetch(`/api/agents/${name}/messages`);
    const pane = document.getElementById('agent-dm-messages');
    if (!msgs.length) {
        pane.innerHTML = `<p class="placeholder">No messages yet. Say something!</p>`;
        return;
    }
    const wasAtBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 50;
    pane.innerHTML = msgs.map(renderMsg).join('');
    if (wasAtBottom) pane.scrollTop = pane.scrollHeight;
}

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
    startMessageRefresh(() => loadChannelMessages(selectedChannel));
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
    startMessageRefresh(loadActivity);
}

async function loadActivity() {
    // Combine recent messages across all channels + DMs
    let allMsgs = [];
    try {
        const channels = await apiFetch('/api/channels');
        for (const ch of channels.slice(0, 10)) {
            const msgs = await apiFetch(`/api/channels/${ch.channel}/messages?limit=20`);
            allMsgs.push(...msgs.map(m => ({ ...m, _type: 'channel' })));
        }
        const agents = await apiFetch('/api/agents');
        for (const a of agents.slice(0, 10)) {
            const msgs = await apiFetch(`/api/agents/${a.name}/messages?limit=20`);
            allMsgs.push(...msgs.map(m => ({ ...m, _type: 'dm' })));
        }
    } catch (_) {}

    // Dedupe by id and sort by time
    const seen = new Set();
    allMsgs = allMsgs.filter(m => { if (seen.has(m.id)) return false; seen.add(m.id); return true; });
    allMsgs.sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''));
    allMsgs = allMsgs.slice(-50);

    const pane = document.getElementById('activity-feed');
    if (!allMsgs.length) {
        pane.innerHTML = '<p class="placeholder">No activity yet. Spawn some agents and start talking!</p>';
        return;
    }
    pane.innerHTML = allMsgs.map(m => {
        const time = m.created_at ? new Date(m.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : '';
        const sender = m.from_agent || '?';
        const target = m.channel ? `#${m.channel}` : (m.to_agent || '');
        const body = (m.body || '').slice(0, 120);
        return `
            <div class="activity-item">
                <span class="activity-time">${esc(time)}</span>
                <span class="activity-sender">${esc(sender)}</span>
                <span class="activity-arrow">&rarr;</span>
                <span class="activity-target">${esc(target)}</span>
                <span class="activity-body">${esc(body)}</span>
            </div>
        `;
    }).join('');
    pane.scrollTop = pane.scrollHeight;
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
        // Refresh current view
        if (selectedAgent) loadAgentMessages(selectedAgent);
        if (selectedChannel) loadChannelMessages(selectedChannel);
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

function startMessageRefresh(fn) {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(fn, 5000);
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
