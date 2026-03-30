/* Basic Workbench — Dashboard JavaScript */

const API = '';  // Same origin

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let currentView = 'agents';
let currentChannel = null;
let currentDMAgent = null;
let selectedAgent = null;
let agentRefreshTimer = null;

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
        const view = btn.dataset.view;
        document.getElementById(`view-${view}`).classList.add('active');
        currentView = view;
        if (view === 'agents') loadAgents();
        if (view === 'channels') loadChannels();
        if (view === 'messages') loadDMAgents();
    });
});

// ---------------------------------------------------------------------------
// API Helpers
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Agents View
// ---------------------------------------------------------------------------

async function loadAgents() {
    const agents = await apiFetch('/api/agents');
    const grid = document.getElementById('agent-list');
    if (!agents.length) {
        grid.innerHTML = '<p class="placeholder">No agents yet. Click "+ Add Agent" to get started.</p>';
        return;
    }
    grid.innerHTML = agents.map(a => `
        <div class="agent-card" onclick="showAgentDetail('${a.name}')">
            <div class="card-header">
                <span class="agent-name">${esc(a.name)}</span>
                <span class="agent-status ${a.status}">${a.status}</span>
            </div>
            <div class="card-meta">
                <div>Role: ${esc(a.role || 'developer')}</div>
                <div>Model: ${esc(a.model || 'sonnet')}</div>
                ${a.cwd ? `<div>Repo: ${esc(a.cwd)}</div>` : ''}
            </div>
        </div>
    `).join('');
}

// ---------------------------------------------------------------------------
// Agent Detail Modal
// ---------------------------------------------------------------------------

async function showAgentDetail(name) {
    selectedAgent = name;
    const modal = document.getElementById('modal-agent-detail');
    document.getElementById('detail-agent-name').textContent = name;
    modal.classList.remove('hidden');

    // Load agent data
    refreshAgentDetail();
    // Auto-refresh every 3s
    agentRefreshTimer = setInterval(refreshAgentDetail, 3000);
}

async function refreshAgentDetail() {
    if (!selectedAgent) return;
    try {
        const agent = await apiFetch(`/api/agents/${selectedAgent}`);
        document.getElementById('agent-output').textContent = agent.output || '(no output)';
        const msgsDiv = document.getElementById('agent-detail-messages');
        if (agent.messages && agent.messages.length) {
            msgsDiv.innerHTML = agent.messages.reverse().map(renderMsg).join('');
        } else {
            msgsDiv.innerHTML = '<p class="placeholder">No messages yet</p>';
        }
    } catch (e) {
        console.error('Failed to refresh agent detail:', e);
    }
}

function closeDetailModal() {
    document.getElementById('modal-agent-detail').classList.add('hidden');
    selectedAgent = null;
    if (agentRefreshTimer) { clearInterval(agentRefreshTimer); agentRefreshTimer = null; }
}

// Stop/Restart buttons
document.getElementById('btn-stop-agent').addEventListener('click', async () => {
    if (!selectedAgent) return;
    await apiFetch(`/api/agents/${selectedAgent}`, { method: 'DELETE' });
    closeDetailModal();
    loadAgents();
});

document.getElementById('btn-restart-agent').addEventListener('click', async () => {
    if (!selectedAgent) return;
    await apiFetch(`/api/agents/${selectedAgent}/restart`, { method: 'POST' });
    refreshAgentDetail();
});

// Detail tabs
document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
        document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
    });
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
    form.querySelector('[name="prompt"]').value = '';  // Uses preset
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
        alert(`Failed to spawn agent: ${err.message}`);
    }
});

// ---------------------------------------------------------------------------
// Channels View
// ---------------------------------------------------------------------------

async function loadChannels() {
    const channels = await apiFetch('/api/channels');
    const list = document.getElementById('channel-list');
    if (!channels.length) {
        list.innerHTML = '<li class="placeholder">No channels yet</li>';
        return;
    }
    list.innerHTML = channels.map(ch => `
        <li onclick="selectChannel('${esc(ch.channel)}')"
            class="${currentChannel === ch.channel ? 'active' : ''}">
            #${esc(ch.channel)} <small>(${ch.count})</small>
        </li>
    `).join('');
    if (currentChannel) loadChannelMessages(currentChannel);
}

async function selectChannel(name) {
    currentChannel = name;
    document.querySelectorAll('#channel-list li').forEach(li => li.classList.remove('active'));
    // Re-highlight
    loadChannels();
    loadChannelMessages(name);
}

async function loadChannelMessages(channel) {
    const msgs = await apiFetch(`/api/channels/${channel}/messages`);
    const pane = document.getElementById('channel-messages');
    if (!msgs.length) {
        pane.innerHTML = `<p class="placeholder">No messages in #${esc(channel)}</p>`;
        return;
    }
    pane.innerHTML = msgs.map(renderMsg).join('');
    pane.scrollTop = pane.scrollHeight;
}

// ---------------------------------------------------------------------------
// DMs View
// ---------------------------------------------------------------------------

async function loadDMAgents() {
    const agents = await apiFetch('/api/agents');
    const list = document.getElementById('dm-agent-list');
    list.innerHTML = agents.map(a => `
        <li onclick="selectDMAgent('${esc(a.name)}')"
            class="${currentDMAgent === a.name ? 'active' : ''}">
            ${esc(a.name)}
            <span class="agent-status ${a.status}" style="font-size:10px; padding:1px 6px;">${a.status}</span>
        </li>
    `).join('');
    if (currentDMAgent) loadDMMessages(currentDMAgent);
}

async function selectDMAgent(name) {
    currentDMAgent = name;
    loadDMAgents();
    loadDMMessages(name);
}

async function loadDMMessages(name) {
    const msgs = await apiFetch(`/api/agents/${name}/messages`);
    const pane = document.getElementById('dm-messages');
    if (!msgs.length) {
        pane.innerHTML = `<p class="placeholder">No messages for ${esc(name)}</p>`;
        return;
    }
    pane.innerHTML = msgs.map(renderMsg).join('');
    pane.scrollTop = pane.scrollHeight;
}

// ---------------------------------------------------------------------------
// SSE — Real-time updates
// ---------------------------------------------------------------------------

let eventSource = null;
let heartbeatTimer = null;

function connectSSE() {
    const statusDot = document.getElementById('connection-status');

    eventSource = new EventSource(`${API}/api/feed/stream`);

    eventSource.addEventListener('system.heartbeat', () => {
        statusDot.className = 'status-dot connected';
        statusDot.title = 'Connected';
        resetHeartbeatWatchdog();
    });

    eventSource.addEventListener('new_message', (e) => {
        // Refresh current view if relevant
        if (currentView === 'channels' && currentChannel) loadChannelMessages(currentChannel);
        if (currentView === 'messages' && currentDMAgent) loadDMMessages(currentDMAgent);
    });

    eventSource.addEventListener('agent_status_change', () => {
        if (currentView === 'agents') loadAgents();
    });

    eventSource.onopen = () => {
        statusDot.className = 'status-dot connected';
        statusDot.title = 'Connected';
        resetHeartbeatWatchdog();
    };

    eventSource.onerror = () => {
        statusDot.className = 'status-dot reconnecting';
        statusDot.title = 'Reconnecting...';
    };
}

function resetHeartbeatWatchdog() {
    if (heartbeatTimer) clearTimeout(heartbeatTimer);
    heartbeatTimer = setTimeout(() => {
        console.warn('SSE heartbeat timeout — reconnecting');
        if (eventSource) { eventSource.close(); eventSource = null; }
        connectSSE();
    }, 45000);
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function esc(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function renderMsg(m) {
    const time = m.created_at ? new Date(m.created_at).toLocaleTimeString() : '';
    const sender = m.from_agent || m.sender || '?';
    return `
        <div class="msg">
            <div class="msg-header">
                <span class="msg-sender">${esc(sender)}</span>
                <span class="msg-time">${esc(time)}</span>
                ${m.to_agent ? `<span class="msg-time">to ${esc(m.to_agent)}</span>` : ''}
                ${m.channel ? `<span class="msg-time">#${esc(m.channel)}</span>` : ''}
            </div>
            <div class="msg-body">${esc(m.body)}</div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

loadAgents();
connectSSE();
