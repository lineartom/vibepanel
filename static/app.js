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

  // Page-leave hooks
  if (activePage === 'server') srvStopPolling();

  activePage = page;

  document.querySelectorAll('.page').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nav-link').forEach(el => el.classList.remove('active'));

  $(`page-${page}`).classList.add('active');
  document.querySelectorAll(`.nav-link[data-page="${page}"]`).forEach(el => el.classList.add('active'));

  // Page-enter hooks
  if (page === 'players') fetchServerRunning().then(() => { if (serverRunning !== false) loadPlayers(); });
  if (page === 'say')     fetchServerRunning();
  if (page === 'server')  srvStartPolling();
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
    btnRefresh.disabled = serverRunning === false;
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
    btnSay.disabled = serverRunning === false;
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

// ── Server running state (shared across pages) ───────────

let serverRunning = null;

async function fetchServerRunning() {
  try {
    const res  = await fetch('/api/server/status');
    const data = await res.json();
    serverRunning = data.running;
    applyServerRunningState();
    return data;
  } catch (_) {
    return null;
  }
}

function applyServerRunningState() {
  const offline = serverRunning === false;

  // Players: gate Refresh; replace hint with offline message if no real data yet
  $('btn-refresh').disabled = offline;
  if (offline && !$('players-body').querySelector('.players-stat')) {
    $('players-body').innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">&#x26D4;</div>
        <p>Server is not running.</p>
      </div>`;
  }

  // Say: disable inputs and show notice
  $('say-input').disabled = offline;
  $('btn-say').disabled   = offline;
  $('say-offline-note').hidden = !offline;
}

// ── Server ───────────────────────────────────────────────

let srvPollTimer  = null;
let selectedJar   = null;
let jarsLoaded    = false;

function srvStartPolling() {
  loadServerStatus();
  loadJars();
  srvPollTimer = setInterval(loadServerStatus, 5000);
}

function srvStopPolling() {
  clearInterval(srvPollTimer);
  srvPollTimer = null;
}

async function loadServerStatus() {
  const card = $('srv-status-card');
  const data = await fetchServerRunning();

  if (!data) {
    card.innerHTML = `<p class="hint">Could not reach server.</p>`;
    return;
  }
  if (data.running) {
    card.innerHTML = `
      <div class="srv-status-row">
        <span class="srv-dot running"></span>
        <span class="srv-status-label">Running</span>
      </div>
      ${data.jar ? `<div class="srv-jar">${esc(data.jar)}</div>` : ''}`;
    $('srv-start-section').hidden = true;
  } else {
    card.innerHTML = `
      <div class="srv-status-row">
        <span class="srv-dot stopped"></span>
        <span class="srv-status-label">Stopped</span>
      </div>`;
    $('srv-start-section').hidden = false;
    $('btn-start').disabled = !selectedJar;
  }
}

async function loadJars() {
  if (jarsLoaded) return;
  const wrap = $('jar-list-wrap');
  try {
    const res  = await fetch('/api/server/jars');
    const data = await res.json();

    if (!data.ok) {
      wrap.innerHTML = `<p class="hint">Error: ${esc(data.error)}</p>`;
      return;
    }
    if (data.jars.length === 0) {
      wrap.innerHTML = `<p class="hint">No .jar files found in <code>${esc(data.jars_dir)}</code>.</p>`;
      return;
    }

    // Auto-select if only one jar
    if (data.jars.length === 1) selectedJar = data.jars[0];

    wrap.innerHTML = '<div class="jar-list"></div>';
    const list = wrap.querySelector('.jar-list');
    data.jars.forEach(jar => {
      const row = document.createElement('div');
      row.className = 'jar-item' + (jar === selectedJar ? ' selected' : '');
      row.dataset.jar = jar;
      row.innerHTML = `<span class="jar-radio"></span><span class="jar-name">${esc(jar)}</span>`;
      row.addEventListener('click', () => selectJar(jar));
      list.appendChild(row);
    });

    $('btn-start').disabled = !selectedJar;
    jarsLoaded = true;
  } catch (err) {
    wrap.innerHTML = `<p class="hint">Error: ${esc(err.message)}</p>`;
  }
}

function selectJar(jar) {
  selectedJar = jar;
  $('jar-list-wrap').querySelectorAll('.jar-item').forEach(el => {
    el.classList.toggle('selected', el.dataset.jar === jar);
  });
  $('btn-start').disabled = false;
}

$('btn-srv-refresh').addEventListener('click', () => {
  jarsLoaded = false;
  loadServerStatus();
  loadJars();
});

// ── Download Fabric ───────────────────────────────────────

$('btn-download').addEventListener('click', async () => {
  const version    = $('fabric-version').value.trim();
  const btn        = $('btn-download');
  const outputWrap = $('dl-output-wrap');
  const output     = $('dl-output');

  if (version && !/^[a-zA-Z0-9][a-zA-Z0-9.\-]*$/.test(version)) {
    output.textContent = 'Invalid version string.';
    output.className = 'dl-output error';
    outputWrap.hidden = false;
    return;
  }

  btn.disabled = true;
  btn.textContent = '↓ Downloading…';
  outputWrap.hidden = true;

  try {
    const res  = await fetch('/api/server/download-fabric', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ version: version || null }),
    });
    const data = await res.json();

    const text = data.output || data.error || (data.ok ? 'Done.' : 'Unknown error.');
    output.textContent = text;
    output.className = 'dl-output' + (data.ok ? '' : ' error');
    outputWrap.hidden = false;

    if (data.ok) {
      // Refresh jar list so the new file appears immediately
      jarsLoaded = false;
      loadJars();
    }
  } catch (err) {
    output.textContent = `Network error: ${err.message}`;
    output.className = 'dl-output error';
    outputWrap.hidden = false;
  }

  btn.disabled = false;
  btn.textContent = '↓ Download';
});

$('btn-start').addEventListener('click', async () => {
  if (!selectedJar) return;
  const mem      = $('mem-input').value.trim().toUpperCase();
  const btn      = $('btn-start');
  const feedback = $('start-feedback');

  if (!/^\d+[MG]$/.test(mem)) {
    feedback.textContent = 'Invalid memory format — use e.g. 1024M or 2G';
    feedback.className = 'fb-error';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Starting…';
  feedback.textContent = '';
  feedback.className = '';

  try {
    const res  = await fetch('/api/server/start', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ jar: selectedJar, mem }),
    });
    const data = await res.json();

    if (data.ok) {
      feedback.textContent = '✓ Start command sent — server will appear as Running shortly.';
      feedback.className = 'fb-ok';
      setTimeout(loadServerStatus, 2500);
    } else {
      feedback.textContent = `Error: ${data.error}`;
      feedback.className = 'fb-error';
      btn.disabled = false;
    }
  } catch (err) {
    feedback.textContent = `Network error: ${err.message}`;
    feedback.className = 'fb-error';
    btn.disabled = false;
  }
  btn.textContent = '▶ Start Server';
});

// ── Boot ─────────────────────────────────────────────────

renderHistory();
initConsole();
fetchServerRunning();
setInterval(fetchServerRunning, 15000);
