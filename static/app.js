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

// ── Utilities (continued) ────────────────────────────────

function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
  return `${(n / 1073741824).toFixed(2)} GB`;
}

function parseWorldSave(filename) {
  const m = filename.match(/^world-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})(?:-(.+))?\.tgz$/);
  if (!m) return { dateStr: filename, label: null, isAutosave: false };
  const dt = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
  return { dateStr: dt.toLocaleString(), label: m[7] || null, isAutosave: m[7] === 'autosave' };
}

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
  if (page === 'mods')    { fetchServerRunning(); loadMods(); }
  if (page === 'worlds')  { fetchServerRunning(); loadWorlds(); }
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

  // Mods: move disabled while server is running
  $('mods-running-note').hidden = !running;
  document.querySelectorAll('.btn-mod-move').forEach(btn => { btn.disabled = running; });

  // Worlds: save and load disabled while server is running
  const running = serverRunning === true;
  $('worlds-running-note').hidden = !running;
  $('btn-world-save').disabled = running;
  document.querySelectorAll('.btn-world-load').forEach(btn => { btn.disabled = running; });
}

// ── Server ───────────────────────────────────────────────

let srvPollTimer  = null;
let selectedJar   = null;
let jarsLoaded    = false;

function srvStartPolling() {
  loadServerStatus();
  loadJars();
  loadServerIdentity();
  loadLatestMinecraft();
  srvPollTimer = setInterval(loadServerStatus, 5000);
}

async function loadLatestMinecraft() {
  try {
    const res  = await fetch('/api/server/latest-minecraft');
    const data = await res.json();
    if (data.ok && data.version) {
      $('fabric-version').placeholder = `e.g. ${data.version}`;
    }
  } catch (_) {}
}

async function loadServerIdentity() {
  const wrap  = $('srv-identity');
  const icon  = $('srv-icon');
  const motdEl = $('srv-motd');

  try {
    const res  = await fetch('/api/server/identity');
    const data = await res.json();
    if (!data.ok) { wrap.hidden = true; return; }

    const motdLines = data.motd
      ? data.motd.split('\n').filter(l => l.length > 0)
      : [];

    if (!data.has_icon && motdLines.length === 0) {
      wrap.hidden = true;
      return;
    }

    icon.hidden = !data.has_icon;
    if (data.has_icon) {
      icon.src = `/api/server/icon?t=${Date.now()}`;
      icon.onerror = () => { icon.hidden = true; };
    }

    motdEl.hidden = motdLines.length === 0;
    if (motdLines.length > 0) {
      motdEl.innerHTML = motdLines
        .map(l => `<div class="srv-motd-line">${esc(l)}</div>`)
        .join('');
    }

    wrap.hidden = false;
  } catch (_) {
    wrap.hidden = true;
  }
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
        <button id="btn-stop" class="btn btn-danger btn-sm">&#x25A0; Stop</button>
      </div>
      ${data.jar ? `<div class="srv-jar">${esc(data.jar)}</div>` : ''}`;
    $('srv-start-section').hidden = true;
    $('btn-stop').addEventListener('click', stopServer);
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

async function stopServer() {
  const btn = $('btn-stop');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Stopping…';
  try {
    await fetch('/api/server/stop', { method: 'POST' });
  } catch (_) { /* poll will surface any error */ }
  setTimeout(loadServerStatus, 2000);
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
  loadServerIdentity();
  loadLatestMinecraft();
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

// ── Mods ─────────────────────────────────────────────────

async function loadMods() {
  $('mods-active-list').innerHTML   = '<p class="hint">Loading&hellip;</p>';
  $('mods-inactive-list').innerHTML = '<p class="hint">Loading&hellip;</p>';
  $('mods-active-count').textContent   = '';
  $('mods-inactive-count').textContent = '';

  try {
    const res  = await fetch('/api/mods/list');
    const data = await res.json();
    renderModsList(data);
  } catch (err) {
    const msg = `<p class="hint">Error: ${esc(err.message)}</p>`;
    $('mods-active-list').innerHTML   = msg;
    $('mods-inactive-list').innerHTML = msg;
  }
}

function renderModsList(data) {
  if (!data.ok) {
    const msg = `<p class="hint">Error: ${esc(data.error)}</p>`;
    $('mods-active-list').innerHTML   = msg;
    $('mods-inactive-list').innerHTML = msg;
    return;
  }

  const running = serverRunning === true;
  renderModsColumn($('mods-active-list'),   data.active,   'deactivate', running);
  renderModsColumn($('mods-inactive-list'), data.inactive, 'activate',   running);
  $('mods-active-count').textContent   = `(${data.active.length})`;
  $('mods-inactive-count').textContent = `(${data.inactive.length})`;
}

function renderModsColumn(container, mods, action, running) {
  if (mods.length === 0) {
    container.innerHTML = '<p class="hint">None.</p>';
    return;
  }

  const btnLabel = action === 'activate' ? 'Activate' : 'Deactivate';
  const btnClass = action === 'activate' ? 'btn-primary' : 'btn-ghost';

  let html = '<div class="mods-list">';
  mods.forEach(mod => {
    html += `
      <div class="mod-item">
        <div class="mod-item-info">
          <div class="mod-item-name">${esc(mod.name)}</div>
          <div class="mod-item-size">${fmtBytes(mod.size)}</div>
        </div>
        <button class="btn ${btnClass} btn-sm btn-mod-move"
                data-filename="${esc(mod.name)}"
                data-action="${action}"
                ${running ? 'disabled' : ''}>${btnLabel}</button>
      </div>`;
  });
  html += '</div>';
  container.innerHTML = html;

  container.querySelectorAll('.btn-mod-move').forEach(btn => {
    btn.addEventListener('click', () => moveMod(btn.dataset.filename, btn.dataset.action));
  });
}

async function moveMod(filename, action) {
  const endpoint = action === 'activate' ? '/api/mods/activate' : '/api/mods/deactivate';
  const opFb     = $('mods-op-feedback');
  opFb.textContent = '';
  opFb.className   = '';

  document.querySelectorAll('.btn-mod-move').forEach(b => { b.disabled = true; });

  try {
    const res  = await fetch(endpoint, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ filename }),
    });
    const data = await res.json();

    if (data.ok) {
      loadMods();
      return;
    }

    if (data.conflict) {
      opFb.innerHTML = `
        <div class="conflict-notice">
          <span>&#x26A0;&#xFE0F; <strong>${esc(filename)}</strong> exists in both directories
          with different content. Remove one version manually, or:</span>
          <button class="btn btn-danger btn-sm" id="btn-delete-conflict"
                  data-filename="${esc(filename)}">Delete Both</button>
        </div>`;
      $('btn-delete-conflict').addEventListener('click', e => {
        deleteBothConflict(e.currentTarget.dataset.filename);
      });
    } else {
      opFb.textContent = `Error: ${data.error}`;
      opFb.className   = 'fb-error';
      loadMods();
    }
  } catch (err) {
    opFb.textContent = `Network error: ${err.message}`;
    opFb.className   = 'fb-error';
    loadMods();
  }
}

async function deleteBothConflict(filename) {
  if (!confirm(`Delete both copies of "${filename}"?\n\nThis cannot be undone.`)) return;

  const opFb = $('mods-op-feedback');

  try {
    const res  = await fetch('/api/mods/delete', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ filename, location: 'both' }),
    });
    const data = await res.json();

    if (data.ok) {
      opFb.textContent = '';
      opFb.className   = '';
    } else {
      opFb.textContent = `Error: ${data.error}`;
      opFb.className   = 'fb-error';
    }
  } catch (err) {
    opFb.textContent = `Network error: ${err.message}`;
    opFb.className   = 'fb-error';
  }
  loadMods();
}

$('btn-mods-refresh').addEventListener('click', loadMods);

// ── Worlds ───────────────────────────────────────────────

async function loadWorlds() {
  const wrap = $('worlds-list-wrap');
  wrap.innerHTML = '<p class="hint">Loading&hellip;</p>';
  try {
    const res  = await fetch('/api/worlds/list');
    const data = await res.json();
    renderWorldsList(data);
  } catch (err) {
    wrap.innerHTML = `<p class="hint">Error: ${esc(err.message)}</p>`;
  }
}

function renderWorldsList(data) {
  const wrap = $('worlds-list-wrap');

  if (!data.ok) {
    wrap.innerHTML = `<p class="hint">Error: ${esc(data.error)}</p>`;
    return;
  }
  if (data.saves.length === 0) {
    wrap.innerHTML = '<p class="hint">No world saves found.</p>';
    return;
  }

  const hasAutosaves = data.saves.some(s => /autosave\.tgz$/.test(s.name));
  const running = serverRunning === true;

  let html = `
    <div class="worlds-header-row">
      <span class="worlds-total">${data.saves.length} save${data.saves.length !== 1 ? 's' : ''} &bull; ${fmtBytes(data.total_bytes)} total</span>
      ${hasAutosaves ? `<button id="btn-delete-autosaves" class="btn btn-ghost btn-sm">&#x1F5D1; Delete Autosaves</button>` : ''}
    </div>
    <div class="worlds-list">`;

  data.saves.forEach(save => {
    const { dateStr, label, isAutosave } = parseWorldSave(save.name);
    html += `
      <div class="world-item">
        <div class="world-item-info">
          <div class="world-item-date">${esc(dateStr)}</div>
          ${label ? `<div class="world-item-label${isAutosave ? ' autosave' : ''}">${esc(label)}</div>` : ''}
        </div>
        <div class="world-item-right">
          <span class="world-item-size">${fmtBytes(save.size)}</span>
          <div class="world-item-actions">
            <button class="btn btn-ghost btn-sm btn-world-load"
                    data-filename="${esc(save.name)}"${running ? ' disabled' : ''}>Load</button>
            <button class="btn btn-danger btn-sm btn-world-delete"
                    data-filename="${esc(save.name)}">Delete</button>
          </div>
        </div>
      </div>`;
  });

  html += '</div>';
  wrap.innerHTML = html;

  if (hasAutosaves) {
    $('btn-delete-autosaves').addEventListener('click', deleteAutosaves);
  }
  wrap.querySelectorAll('.btn-world-load').forEach(btn => {
    btn.addEventListener('click', () => loadWorld(btn.dataset.filename));
  });
  wrap.querySelectorAll('.btn-world-delete').forEach(btn => {
    btn.addEventListener('click', () => deleteWorld(btn.dataset.filename));
  });
}

async function saveWorld() {
  const nameInput = $('world-name');
  const btn       = $('btn-world-save');
  const fb        = $('world-save-feedback');
  const opFb      = $('worlds-op-feedback');

  btn.disabled = true;
  btn.textContent = 'Saving…';
  fb.textContent  = '';
  fb.className    = '';
  opFb.textContent = '';
  opFb.className   = '';

  try {
    const res  = await fetch('/api/worlds/save', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ name: nameInput.value.trim() }),
    });
    const data = await res.json();

    if (data.ok) {
      fb.textContent = `✓ Saved as ${data.filename} (${fmtBytes(data.size)})`;
      fb.className   = 'fb-ok';
      nameInput.value = '';
      setTimeout(() => { fb.textContent = ''; fb.className = ''; }, 5000);
      loadWorlds();
    } else {
      fb.textContent = `Error: ${data.error}`;
      fb.className   = 'fb-error';
    }
  } catch (err) {
    fb.textContent = `Network error: ${err.message}`;
    fb.className   = 'fb-error';
  } finally {
    btn.disabled    = serverRunning === true;
    btn.textContent = '💾 Save';
  }
}

async function loadWorld(filename) {
  if (!confirm(`Load "${filename}"?\n\nThe current world will be autosaved first, then replaced.`)) return;

  const opFb = $('worlds-op-feedback');
  opFb.textContent = 'Loading world…';
  opFb.className   = '';

  document.querySelectorAll('.btn-world-load, .btn-world-delete').forEach(b => { b.disabled = true; });

  try {
    const res  = await fetch('/api/worlds/load', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ filename }),
    });
    const data = await res.json();

    if (data.ok) {
      const note = data.autosaved ? ` (autosaved as ${data.autosaved})` : '';
      opFb.textContent = `✓ World loaded${note}.`;
      opFb.className   = 'fb-ok';
    } else {
      opFb.textContent = `Error: ${data.error}`;
      opFb.className   = 'fb-error';
    }
  } catch (err) {
    opFb.textContent = `Network error: ${err.message}`;
    opFb.className   = 'fb-error';
  }
  loadWorlds();
}

async function deleteWorld(filename) {
  if (!confirm(`Delete "${filename}"?\n\nThis cannot be undone.`)) return;

  const opFb = $('worlds-op-feedback');
  opFb.textContent = '';
  opFb.className   = '';

  try {
    const res  = await fetch('/api/worlds/delete', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ filename }),
    });
    const data = await res.json();

    if (!data.ok) {
      opFb.textContent = `Error: ${data.error}`;
      opFb.className   = 'fb-error';
    }
  } catch (err) {
    opFb.textContent = `Network error: ${err.message}`;
    opFb.className   = 'fb-error';
  }
  loadWorlds();
}

async function deleteAutosaves() {
  if (!confirm('Delete all autosave worlds?\n\nThis cannot be undone.')) return;

  const opFb = $('worlds-op-feedback');
  opFb.textContent = '';
  opFb.className   = '';

  try {
    const res  = await fetch('/api/worlds/delete-autosaves', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    const data = await res.json();

    if (data.ok) {
      opFb.textContent = `✓ Deleted ${data.deleted} autosave${data.deleted !== 1 ? 's' : ''}.`;
      opFb.className   = 'fb-ok';
      setTimeout(() => { opFb.textContent = ''; opFb.className = ''; }, 4000);
    } else {
      opFb.textContent = `Error: ${data.error}`;
      opFb.className   = 'fb-error';
    }
  } catch (err) {
    opFb.textContent = `Network error: ${err.message}`;
    opFb.className   = 'fb-error';
  }
  loadWorlds();
}

$('btn-worlds-refresh').addEventListener('click', loadWorlds);
$('btn-world-save').addEventListener('click', saveWorld);

// ── Boot ─────────────────────────────────────────────────

renderHistory();
initConsole();
fetchServerRunning();
setInterval(fetchServerRunning, 15000);
