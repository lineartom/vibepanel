'use strict';

// ── Utilities ────────────────────────────────────────────

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function $(id) { return document.getElementById(id); }

// ── Navigation ───────────────────────────────────────────

let activePage = 'console';

function navigate(page) {
  if (page === activePage) return;
  activePage = page;

  document.querySelectorAll('.page').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nav-link').forEach(el => el.classList.remove('active'));

  $(`page-${page}`).classList.add('active');
  document.querySelectorAll(`.nav-link[data-page="${page}"]`).forEach(el => el.classList.add('active'));

  if (page === 'players') loadPlayers();
}

document.querySelectorAll('.nav-link').forEach(link => {
  link.addEventListener('click', e => {
    e.preventDefault();
    navigate(link.dataset.page);
  });
});

// ── Console / SSE ────────────────────────────────────────

const consoleOut      = $('console-out');
const consoleDot      = $('console-conn').querySelector('.conn-dot');
const consoleText     = $('console-conn-text');
const sidebarDot      = $('sidebar-status').querySelector('.conn-dot');
const sidebarText     = $('sidebar-status-text');

function setConnState(state, label) {
  [consoleDot, sidebarDot].forEach(d => {
    d.className = 'conn-dot' + (state ? ` ${state}` : '');
  });
  consoleText.textContent = label;
  sidebarText.textContent = label;
}

function initConsole() {
  let es;

  function connect() {
    if (es) es.close();
    setConnState('', 'Connecting…');

    es = new EventSource('/api/console/stream');

    es.onopen = () => setConnState('live', 'Live');

    es.onmessage = e => {
      let data;
      try { data = JSON.parse(e.data); } catch { return; }

      if (data.error) {
        setConnState('error', 'Error');
        return;
      }
      if (typeof data.content === 'string') {
        const atBottom = consoleOut.scrollHeight - consoleOut.clientHeight <= consoleOut.scrollTop + 60;
        consoleOut.textContent = data.content;
        if (atBottom) consoleOut.scrollTop = consoleOut.scrollHeight;
      }
    };

    es.onerror = () => {
      setConnState('error', 'Reconnecting…');
      es.close();
      setTimeout(connect, 3000);
    };
  }

  connect();
}

// ── Players ──────────────────────────────────────────────

const playersBody  = $('players-body');
const btnRefresh   = $('btn-refresh');
let loadingPlayers = false;

async function loadPlayers() {
  if (loadingPlayers) return;
  loadingPlayers = true;
  btnRefresh.disabled = true;
  btnRefresh.textContent = 'Loading…';
  playersBody.innerHTML = '<p class="hint">Querying server…</p>';

  try {
    const res  = await fetch('/api/players');
    const data = await res.json();

    if (!data.ok) {
      playersBody.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">&#x26A0;&#xFE0F;</div>
          <p>${esc(data.error || 'Unknown error')}</p>
        </div>`;
      return;
    }

    let html = `
      <div class="players-stat">
        <span class="players-num">${data.count}</span>
        <span class="players-denom">/ ${data.max} online</span>
      </div>`;

    if (data.count === 0) {
      html += `
        <div class="empty-state">
          <div class="empty-icon">&#x1F634;</div>
          <p>No players currently online</p>
        </div>`;
    } else {
      html += '<div class="player-list">';
      data.players.forEach(name => {
        html += `
          <div class="player-row">
            <div class="player-face">&#x1F9D1;</div>
            <span class="player-name">${esc(name)}</span>
          </div>`;
      });
      html += '</div>';
    }

    playersBody.innerHTML = html;
  } catch (err) {
    playersBody.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">&#x26A0;&#xFE0F;</div>
        <p>Network error: ${esc(err.message)}</p>
      </div>`;
  } finally {
    loadingPlayers = false;
    btnRefresh.disabled = false;
    btnRefresh.textContent = '↺ Refresh';
  }
}

btnRefresh.addEventListener('click', loadPlayers);

// ── Say ──────────────────────────────────────────────────

const sayInput    = $('say-input');
const btnSay      = $('btn-say');
const charCount   = $('char-count');
const sayFeedback = $('say-feedback');
const sayHistory  = $('say-history');
const MAX_LEN     = 256;
let history       = [];

sayInput.addEventListener('input', () => {
  const n = sayInput.value.length;
  charCount.textContent = `${n} / ${MAX_LEN}`;
  charCount.style.color = n > MAX_LEN * 0.9 ? 'var(--red)' : '';
});

// Ctrl/Cmd+Enter submits
sayInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) sendSay();
});

btnSay.addEventListener('click', sendSay);

async function sendSay() {
  const msg = sayInput.value.trim();
  if (!msg) return;

  btnSay.disabled = true;
  btnSay.textContent = 'Sending…';
  sayFeedback.textContent = '';
  sayFeedback.className = '';

  try {
    const res  = await fetch('/api/say', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ message: msg }),
    });
    const data = await res.json();

    if (data.ok) {
      sayFeedback.textContent = '✓ Sent';
      sayFeedback.className = 'fb-ok';
      addHistory(msg);
      sayInput.value = '';
      charCount.textContent = `0 / ${MAX_LEN}`;
      charCount.style.color = '';
      setTimeout(() => { sayFeedback.textContent = ''; }, 3000);
    } else {
      sayFeedback.textContent = `Error: ${data.error}`;
      sayFeedback.className = 'fb-error';
    }
  } catch (err) {
    sayFeedback.textContent = `Network error: ${err.message}`;
    sayFeedback.className = 'fb-error';
  } finally {
    btnSay.disabled = false;
    btnSay.textContent = 'Send';
  }
}

function addHistory(msg) {
  const t = new Date().toLocaleTimeString();
  history.unshift({ msg, t });
  if (history.length > 30) history.pop();
  renderHistory();
}

function renderHistory() {
  if (history.length === 0) {
    sayHistory.innerHTML = '<p class="hint">No messages sent yet.</p>';
    return;
  }
  sayHistory.innerHTML = history.map(h => `
    <div class="history-row">
      <div class="history-time">${esc(h.t)}</div>
      <div class="history-msg">${esc(h.msg)}</div>
    </div>`).join('');
}

// ── Boot ─────────────────────────────────────────────────

renderHistory();
initConsole();
