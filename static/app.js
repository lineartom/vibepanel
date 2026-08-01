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

function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
  return `${(n / 1073741824).toFixed(1)} GB`;
}

function parseWorldSave(filename) {
  const m = filename.match(/^world-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})(?:-(.+))?\.tgz$/);
  if (!m) return { dateStr: filename, label: null, isAutosave: false };
  const dt = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
  return { dateStr: dt.toLocaleString(), label: m[7] || null, isAutosave: m[7] === 'autosave' };
}

// ── Sessions ─────────────────────────────────────────────

let sessions       = [];
let currentSession = '';
// 'overview' or a session name — tracks which tab is highlighted
let activeTab      = 'overview';

// Appends ?s=<session> to API paths when multiple sessions are configured.
function api(path) {
  if (sessions.length <= 1) return path;
  const sep = path.includes('?') ? '&' : '?';
  return path + sep + 's=' + encodeURIComponent(currentSession);
}

async function loadSessions() {
  try {
    const res  = await fetch('/api/sessions');
    const data = await res.json();
    sessions       = data.sessions || [];
    currentSession = sessions[0] || '';
  } catch (_) {
    sessions       = [];
    currentSession = '';
  }
  renderSessionTabs();
}

function renderSessionTabs() {
  const el = $('session-tabs');
  el.hidden = false;

  let html = `<button class="session-tab session-tab--overview${activeTab === 'overview' ? ' active' : ''}"
                      data-tab="overview">Overview</button>`;
  if (sessions.length > 0) {
    html += `<span class="session-tab-sep"></span>`;
  }
  sessions.forEach(s => {
    html += `<button class="session-tab${s === activeTab ? ' active' : ''}"
                     data-tab="${esc(s)}">${esc(s)}</button>`;
  });
  el.innerHTML = html;

  el.querySelectorAll('.session-tab').forEach(btn => {
    btn.addEventListener('click', () => clickSessionTab(btn.dataset.tab));
  });
}

function clickSessionTab(tab) {
  if (tab === 'overview') {
    activeTab = 'overview';
    renderSessionTabs();
    navigate('overview');
    return;
  }

  // Switching sessions keeps the current control page; coming from
  // Overview lands on the Server page.
  const targetPage   = activePage === 'overview' ? 'server' : activePage;
  const switching    = tab !== currentSession;
  const wasOnTarget  = activePage === targetPage;
  activeTab = tab;

  if (switching) {
    currentSession = tab;
    serverRunning  = null;
    jarsLoaded     = false;
    selectedJar    = null;
    srvPort        = null;
    reconnectConsole();
  }

  renderSessionTabs();
  navigate(targetPage);

  // navigate() short-circuits when already on the target page, so the
  // enter-hooks (loadMods, srvStartPolling, etc.) never run. Force them
  // so the new session's data loads.
  if (switching && wasOnTarget) {
    if (targetPage === 'server') srvStopPolling();
    runPageEnterHooks(targetPage);
  }
}

// ── Navigation ───────────────────────────────────────────

let activePage = 'overview';

function navigate(page) {
  if (page === activePage) return;

  // Page-leave hooks
  if (activePage === 'overview') overviewStopPolling();
  if (activePage === 'server')   srvStopPolling();

  activePage = page;

  // Keep session tab highlight in sync with sidebar navigation.
  if (page === 'overview') {
    activeTab = 'overview';
  } else if (activeTab === 'overview' && currentSession) {
    activeTab = currentSession;
  }
  renderSessionTabs();

  document.querySelectorAll('.page').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nav-link').forEach(el => el.classList.remove('active'));

  $(`page-${page}`).classList.add('active');
  document.querySelectorAll(`.nav-link[data-page="${page}"]`).forEach(el => el.classList.add('active'));

  runPageEnterHooks(page);
}

function runPageEnterHooks(page) {
  if (page === 'overview') overviewStartPolling();
  if (page === 'players')  fetchServerRunning().then(() => loadPlayers());
  if (page === 'say')      fetchServerRunning();
  if (page === 'server')   srvStartPolling();
  if (page === 'mods')     { fetchServerRunning(); loadMods(); loadSystemStats().then(() => renderDiskInfo('mods-disk-info')); }
  if (page === 'worlds')   { fetchServerRunning(); loadWorlds(); loadSystemStats().then(() => renderDiskInfo('worlds-disk-info')); }
}

document.querySelectorAll('.nav-link').forEach(link => {
  link.addEventListener('click', e => {
    e.preventDefault();
    navigate(link.dataset.page);
  });
});

// ── Console / SSE ────────────────────────────────────────

const consoleOut  = $('console-out');
const consoleDot  = $('console-conn').querySelector('.conn-dot');
const consoleText = $('console-conn-text');
const sidebarDot  = $('sidebar-status').querySelector('.conn-dot');
const sidebarText = $('sidebar-status-text');

function setConnState(state, label) {
  [consoleDot, sidebarDot].forEach(d => {
    d.className = 'conn-dot' + (state ? ` ${state}` : '');
  });
  consoleText.textContent = label;
  sidebarText.textContent = label;
}

let consoleEs = null;

function reconnectConsole() {
  if (consoleEs) { consoleEs.close(); consoleEs = null; }
  setConnState('', 'Connecting…');

  consoleEs = new EventSource(api('/api/console/stream'));

  consoleEs.onopen = () => setConnState('live', 'Live');

  consoleEs.onmessage = e => {
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

  consoleEs.onerror = () => {
    setConnState('error', 'Reconnecting…');
    consoleEs.close();
    consoleEs = null;
    setTimeout(reconnectConsole, 3000);
  };
}

function initConsole() {
  reconnectConsole();
}

// ── Players ──────────────────────────────────────────────

const btnRefresh   = $('btn-refresh');
let loadingPlayers = false;
let onlineData     = null;   // last /api/players response
let rosterData     = null;   // last /api/players/roster response

// Dashed or bare 32-hex, matching what the server accepts. Usernames are left to
// the admin — the server still validates them before they reach a console command.
const UUID_RE = /^([0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$/;

function emptyState(icon, text) {
  return `
    <div class="empty-state">
      <div class="empty-icon">${icon}</div>
      <p>${esc(text)}</p>
    </div>`;
}

async function loadPlayers() {
  if (loadingPlayers) return;
  loadingPlayers = true;
  btnRefresh.disabled = true;
  btnRefresh.textContent = 'Loading…';
  await Promise.all([loadOnline(), loadRoster()]);
  loadingPlayers = false;
  btnRefresh.disabled = false;
  btnRefresh.textContent = '↺ Refresh';
}

// ── Online ───────────────────────────────────────────────

async function loadOnline() {
  if (serverRunning === false) { onlineData = null; renderOnline(); return; }
  $('players-online').innerHTML = '<p class="hint">Querying server…</p>';
  try {
    const res = await fetch(api('/api/players'));
    onlineData = await res.json();
  } catch (err) {
    onlineData = { ok: false, error: `Network error: ${err.message}` };
  }
  renderOnline();
}

// Name → roster entry, so online rows can show UUID and badges too.
function rosterByName() {
  const map = {};
  (rosterData?.players || []).forEach(p => {
    if (p.name) map[String(p.name).toLowerCase()] = p;
  });
  return map;
}

function renderOnline() {
  const el = $('players-online');

  if (serverError) {
    $('players-online-title').hidden = true;
    el.innerHTML = `<p class="hint fb-error">&#x26A0;&#xFE0F; ${esc(serverError)}</p>`;
    return;
  }
  // Stopped: collapse the section to one line so the roster stays above the fold.
  if (serverRunning === false) {
    $('players-online-title').hidden = true;
    el.innerHTML = '<p class="hint">&#x26D4; Nobody online — the server is stopped.</p>';
    return;
  }
  $('players-online-title').hidden = false;
  if (!onlineData)      { el.innerHTML = '<p class="hint">Querying server…</p>'; return; }
  if (!onlineData.ok)   { el.innerHTML = emptyState('&#x26A0;&#xFE0F;', onlineData.error || 'Unknown error'); return; }

  let html = `
    <div class="players-stat">
      <span class="players-num">${onlineData.count}</span>
      <span class="players-denom">/ ${onlineData.max} online</span>
    </div>`;

  if (onlineData.count === 0) {
    html += emptyState('&#x1F634;', 'No players currently online');
  } else {
    const known = rosterByName();
    html += '<div class="player-list">';
    onlineData.players.forEach(name => {
      const p = known[name.toLowerCase()];
      html += `
        <div class="player-row">
          <div class="player-face">${p?.op ? '&#x1F451;' : '&#x1F9D1;'}</div>
          <div class="player-item-info">
            <div class="player-name">${esc(name)} ${playerBadges(p)}</div>
            ${p?.uuid ? `<div class="player-uuid">${esc(p.uuid)}</div>` : ''}
          </div>
        </div>`;
    });
    html += '</div>';
  }
  el.innerHTML = html;
}

// ── Roster ───────────────────────────────────────────────

function playerBadges(p) {
  if (!p) return '';
  let html = '';
  if (p.op) {
    const lvl = p.op_level != null ? ` ${p.op_level}` : '';
    html += `<span class="badge badge-op">OP${esc(lvl)}</span>`;
  }
  if (p.whitelisted) html += `<span class="badge badge-white">Whitelist</span>`;
  if (p.banned)      html += `<span class="badge badge-ban">Banned</span>`;
  return html;
}

async function loadRoster() {
  try {
    const res = await fetch(api('/api/players/roster'));
    rosterData = await res.json();
  } catch (err) {
    rosterData = { ok: false, error: `Network error: ${err.message}` };
  }
  renderRoster();
  renderOnline();   // badges/UUIDs for online rows come from the roster
}

function renderRoster() {
  const rosterEl  = $('roster-list');
  const suggestEl = $('suggest-list');
  const wlBadge   = $('whitelist-state');

  if (!rosterData?.ok) {
    const msg = `<p class="hint">Error: ${esc(rosterData?.error || 'Unknown error')}</p>`;
    rosterEl.innerHTML  = msg;
    suggestEl.innerHTML = msg;
    wlBadge.hidden = true;
    $('roster-count').textContent  = '';
    $('suggest-count').textContent = '';
    return;
  }

  wlBadge.hidden      = false;
  wlBadge.textContent = rosterData.whitelist_enabled ? 'Whitelist on' : 'Whitelist off';
  wlBadge.className   = 'badge ' + (rosterData.whitelist_enabled ? 'badge-white' : 'badge-neutral');

  // Roster
  const players = rosterData.players || [];
  $('roster-count').textContent = `(${players.length})`;

  if (players.length === 0) {
    // An empty roster usually means we resolved the wrong game directory, so say
    // which one we read and which files were actually there.
    const files   = rosterData.files || {};
    const present = Object.keys(files).filter(f => files[f]);
    const missing = Object.keys(files).filter(f => !files[f]);
    rosterEl.innerHTML = `
      <div class="empty-state empty-state--compact">
        <div class="empty-icon">&#x1F4C4;</div>
        <p>${present.length ? 'No entries in the player lists.' : 'No player lists found.'}</p>
        <p class="empty-detail">Read from <span class="mono">${esc(rosterData.game_dir || 'unknown')}</span></p>
        ${missing.length ? `<p class="empty-detail">Missing there: ${
          missing.map(f => `<span class="mono">${esc(f)}</span>`).join(', ')}</p>` : ''}
      </div>`;
  } else {
    const onlineNames = new Set(
      (onlineData?.ok ? onlineData.players : []).map(n => n.toLowerCase()));

    rosterEl.innerHTML = '<div class="player-list">' + players.map(p => {
      const isOnline = p.name && onlineNames.has(String(p.name).toLowerCase());
      const attrs    = `data-name="${esc(p.name || '')}" data-uuid="${esc(p.uuid || '')}"`;
      return `
        <div class="player-row player-row--roster">
          <div class="player-face">${p.op ? '&#x1F451;' : p.banned ? '&#x1F6AB;' : '&#x1F9D1;'}</div>
          <div class="player-item-info">
            <div class="player-name">
              ${esc(p.name || '(unknown)')}
              ${playerBadges(p)}
              ${isOnline ? '<span class="badge badge-online">Online</span>' : ''}
            </div>
            <div class="player-uuid">${esc(p.uuid || 'no UUID on record')}</div>
            ${p.banned && p.ban_reason
              ? `<div class="player-ban-reason">Ban reason: ${esc(p.ban_reason)}${
                   p.ban_expires && p.ban_expires !== 'forever'
                     ? ` &bull; expires ${esc(p.ban_expires)}` : ''}</div>`
              : ''}
          </div>
          <div class="player-actions">
            <button class="btn btn-ghost btn-sm btn-player-act" data-act="op" ${attrs}
                    data-op="${p.op ? '0' : '1'}">${p.op ? 'De-OP' : 'Make OP'}</button>
            ${p.banned
              ? `<button class="btn btn-ghost btn-sm btn-player-act" data-act="pardon" ${attrs}>Unban</button>`
              : `<button class="btn btn-danger btn-sm btn-player-act" data-act="ban" ${attrs}>Ban</button>`}
            <button class="btn btn-ghost btn-sm btn-player-act" data-act="remove" ${attrs}>Remove</button>
          </div>
        </div>`;
    }).join('') + '</div>';
  }

  // Suggestions
  const sugg = rosterData.suggestions || [];
  $('suggest-count').textContent = `(${sugg.length})`;

  if (sugg.length === 0) {
    suggestEl.innerHTML = rosterData.log_found
      ? `<p class="hint">No new players in <span class="mono">logs/latest.log</span>.</p>`
      : `<p class="hint">No <span class="mono">logs/latest.log</span> under
         <span class="mono">${esc(rosterData.game_dir || 'the game directory')}</span>.</p>`;
  } else {
    suggestEl.innerHTML = '<div class="player-list">' + sugg.map(s => {
      const attrs = `data-name="${esc(s.name)}" data-uuid="${esc(s.uuid)}"`;
      return `
        <div class="player-row player-row--roster">
          <div class="player-face">&#x1F50D;</div>
          <div class="player-item-info">
            <div class="player-name">${esc(s.name)}</div>
            <div class="player-uuid">${esc(s.uuid)}</div>
            <!-- "attempts", not "logins": most of these people were turned away
                 by the whitelist, which is the whole point of the list. -->
            <div class="player-seen">
              Last seen ${esc(s.last_seen)} &bull;
              ${s.seen} connection attempt${s.seen !== 1 ? 's' : ''}
            </div>
          </div>
          <div class="player-actions">
            <button class="btn btn-primary btn-sm btn-player-act" data-act="add" ${attrs}>&plus; Whitelist</button>
            <button class="btn btn-ghost btn-sm btn-player-act" data-act="add-op" ${attrs}>&plus; As OP</button>
            <button class="btn btn-danger btn-sm btn-player-act" data-act="ban" ${attrs}>Ban</button>
          </div>
        </div>`;
    }).join('') + '</div>';
  }

  document.querySelectorAll('.btn-player-act').forEach(btn => {
    btn.addEventListener('click', () => playerAction(btn.dataset));
  });
}

// ── Player mutations ─────────────────────────────────────

function playerAction(ds) {
  const { name, uuid, act } = ds;

  if (act === 'op')     return playerMutate('/api/players/op',
                                            { name, uuid, op: ds.op === '1' },
                                            ds.op === '1' ? `Op'ing ${name}` : `De-op'ing ${name}`);
  if (act === 'add')    return playerMutate('/api/players/add', { name, uuid, op: false },
                                            `Adding ${name}`);
  if (act === 'add-op') return playerMutate('/api/players/add', { name, uuid, op: true },
                                            `Adding ${name} as op`);
  if (act === 'pardon') return playerMutate('/api/players/ban', { name, uuid, ban: false },
                                            `Pardoning ${name}`);
  if (act === 'ban') {
    const reason = prompt(`Ban "${name}"?\n\nOptional reason:`, 'Banned by an operator.');
    if (reason === null) return;
    return playerMutate('/api/players/ban', { name, uuid, ban: true, reason }, `Banning ${name}`);
  }
  if (act === 'remove') {
    if (!confirm(`Remove "${name}" from the whitelist and ops?\n\nAny ban stays in place.`)) return;
    return playerMutate('/api/players/remove', { name, uuid }, `Removing ${name}`);
  }
}

async function playerMutate(endpoint, body, label) {
  const fb = $('players-op-feedback');
  fb.textContent = `${label}…`;
  fb.className   = '';
  document.querySelectorAll('.btn-player-act').forEach(b => { b.disabled = true; });

  try {
    const res  = await fetch(api(endpoint), {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });
    const data = await res.json();

    if (data.ok) {
      fb.textContent = `✓ ${data.message || 'Done'}`;
      fb.className   = 'fb-ok';
      setTimeout(() => {
        if (fb.className === 'fb-ok') { fb.textContent = ''; fb.className = ''; }
      }, 5000);
      // Console commands land asynchronously — give the server a moment to
      // rewrite its json files before we re-read them.
      setTimeout(() => loadPlayers(), data.via === 'console' ? 1400 : 0);
    } else {
      fb.textContent = `Error: ${data.error || 'Unknown error'}`;
      fb.className   = 'fb-error';
      loadRoster();
    }
  } catch (err) {
    fb.textContent = `Network error: ${err.message}`;
    fb.className   = 'fb-error';
    loadRoster();
  }
}

// ── Add player form ──────────────────────────────────────

async function addPlayer() {
  const input     = $('add-player-name');
  const uuidInput = $('add-player-uuid');
  const btn       = $('btn-add-player');
  const fb        = $('add-player-feedback');
  const name      = input.value.trim();
  const uuid      = uuidInput.value.trim();

  if (!name) return;   // nothing typed yet; the server validates the name itself

  // With the server stopped there is nothing that can resolve a name, so the
  // admin has to supply the UUID.
  if (serverRunning !== true) {
    if (!uuid) {
      fb.textContent = 'Enter the player’s UUID — with the server stopped there’s nothing to look it up with.';
      fb.className   = 'fb-error';
      return;
    }
    if (!UUID_RE.test(uuid)) {
      fb.textContent = 'That UUID doesn’t look right — expected 32 hex digits, dashes optional.';
      fb.className   = 'fb-error';
      return;
    }
  }

  btn.disabled = true;
  btn.textContent = 'Adding…';
  fb.textContent  = '';
  fb.className    = '';

  try {
    const res  = await fetch(api('/api/players/add'), {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ name, uuid, op: $('add-player-op').checked }),
    });
    const data = await res.json();

    if (data.ok) {
      fb.textContent = `✓ ${data.message || 'Added'}`;
      fb.className   = 'fb-ok';
      input.value     = '';
      uuidInput.value = '';
      $('add-player-op').checked = false;
      setTimeout(() => { fb.textContent = ''; fb.className = ''; }, 5000);
      setTimeout(() => loadPlayers(), data.via === 'console' ? 1400 : 0);
    } else {
      fb.textContent = `Error: ${data.error || 'Unknown error'}`;
      fb.className   = 'fb-error';
    }
  } catch (err) {
    fb.textContent = `Network error: ${err.message}`;
    fb.className   = 'fb-error';
  }
  btn.disabled    = false;
  btn.textContent = '+ Add';
}

btnRefresh.addEventListener('click', loadPlayers);
$('btn-add-player').addEventListener('click', addPlayer);
['add-player-name', 'add-player-uuid'].forEach(id => {
  $(id).addEventListener('keydown', e => { if (e.key === 'Enter') addPlayer(); });
});

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
    const res  = await fetch(api('/api/say'), {
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
let serverError   = null;   // set when the tmux pane itself can't be reached

async function fetchServerRunning() {
  try {
    const res  = await fetch(api('/api/server/status'));
    const data = await res.json();
    const changed = serverRunning !== data.running;
    serverRunning = data.running;
    serverError   = data.ok === false
      ? (data.error || 'Could not reach the tmux session.') : null;
    applyServerRunningState();
    // Player edits go via console when up and via json files when down, so a
    // transition changes what the page can do — reload it.
    if (changed && activePage === 'players') loadPlayers();
    return data;
  } catch (_) {
    return null;
  }
}

function applyServerRunningState() {
  const offline = serverRunning === false;
  const running = serverRunning === true;

  // Players: a running server resolves names itself, so the UUID box is inert and
  // says so; with it stopped we have no way to look a name up, so the UUID is required.
  const addUuid = $('add-player-uuid');
  addUuid.disabled    = running;
  addUuid.placeholder = running ? 'The running server resolves the UUID itself.' : 'UUID…';
  if (offline) renderOnline();

  // Say: disable inputs and show notice
  $('say-input').disabled = offline;
  $('btn-say').disabled   = offline;
  $('say-offline-note').hidden = !offline;

  // Mods: move disabled while server is running
  $('mods-running-note').hidden = !running;
  document.querySelectorAll('.btn-mod-move').forEach(btn => { btn.disabled = running; });

  // Worlds: save and load disabled while server is running
  $('worlds-running-note').hidden = !running;
  $('btn-world-save').disabled = running;
  document.querySelectorAll('.btn-world-load').forEach(btn => { btn.disabled = running; });
}

// ── System stats ────────────────────────────────────────

let cachedSystemStats = null;

async function loadSystemStats() {
  try {
    const res  = await fetch('/api/system/stats');
    const data = await res.json();
    if (data.ok) cachedSystemStats = data;
  } catch (_) {}
  return cachedSystemStats;
}

function renderSystemStats(stats) {
  const el = $('system-stats');
  if (!stats) { el.hidden = true; return; }

  function bar(label, used, total, extra) {
    const pct = total > 0 ? Math.min(100, used / total * 100) : 0;
    const cls = pct > 90 ? 'stat-fill--danger' : pct > 75 ? 'stat-fill--warn' : '';
    return `<div class="stat-row">
      <span class="stat-label">${label}</span>
      <div class="stat-bar"><div class="stat-fill ${cls}" style="width:${pct.toFixed(1)}%"></div></div>
      <span class="stat-value">${extra}</span>
    </div>`;
  }

  let html = '';
  const cpu = stats.cpu;
  if (cpu) {
    const pct = Math.min(100, cpu.load_1m / cpu.cores * 100);
    html += bar('CPU', cpu.load_1m, cpu.cores,
      `${pct.toFixed(0)}%&ensp;<span class="stat-detail">${cpu.load_1m} / ${cpu.cores} cores</span>`);
  }
  if (stats.ram) {
    html += bar('RAM', stats.ram.used, stats.ram.total,
      `${fmtBytes(stats.ram.used)} / ${fmtBytes(stats.ram.total)}`);
  }
  if (stats.disk) {
    html += bar('Disk', stats.disk.used, stats.disk.total,
      `${fmtBytes(stats.disk.used)} / ${fmtBytes(stats.disk.total)}`);
  }
  el.innerHTML = html;
  el.hidden = !html;
}

function renderDiskInfo(elementId) {
  const el = $(elementId);
  const d  = cachedSystemStats?.disk;
  if (!d) { el.hidden = true; return; }
  const pct = d.total > 0 ? (d.used / d.total * 100).toFixed(0) : 0;
  el.innerHTML = `Disk: ${fmtBytes(d.free)} free of ${fmtBytes(d.total)} (${pct}% used)`;
  el.hidden = false;
}

// ── Overview ─────────────────────────────────────────────

// Per-session cache: { [sessionName]: { running, jar, playerCount, playersLoaded } }
const overviewCache   = {};
let overviewPollTimer = null;

function overviewStartPolling() {
  loadOverview();
  overviewPollTimer = setInterval(refreshOverviewStatus, 15000);
}

function overviewStopPolling() {
  clearInterval(overviewPollTimer);
  overviewPollTimer = null;
}

async function loadOverview() {
  renderOverviewCards();

  // System stats (host-level) — fire alongside session status fetches.
  loadSystemStats().then(s => renderSystemStats(s));

  // 1. Fetch running status for all sessions in parallel.
  const targets = sessions.length ? sessions : [currentSession];
  const statuses = await Promise.all(targets.map(async s => {
    try {
      const res  = await fetch(`/api/server/status?s=${encodeURIComponent(s)}`);
      const data = await res.json();
      overviewCache[s] = { ...overviewCache[s], running: data.running, jar: data.jar, playerCount: null, playersLoaded: false };
    } catch {
      overviewCache[s] = { ...overviewCache[s], running: null };
    }
    return s;
  }));
  renderOverviewCards();

  // 2. Fetch player counts only for sessions that are running.
  await Promise.all(targets.filter(s => overviewCache[s]?.running).map(async s => {
    try {
      const res  = await fetch(`/api/players?s=${encodeURIComponent(s)}`);
      const data = await res.json();
      overviewCache[s].playerCount  = data.ok ? data.count  : null;
      overviewCache[s].playerMax    = data.ok ? data.max    : null;
      overviewCache[s].playersLoaded = true;
    } catch {
      overviewCache[s].playersLoaded = true;
    }
  }));
  renderOverviewCards();
}

async function refreshOverviewStatus() {
  loadSystemStats().then(s => renderSystemStats(s));
  const targets = sessions.length ? sessions : [currentSession];
  await Promise.all(targets.map(async s => {
    try {
      const res  = await fetch(`/api/server/status?s=${encodeURIComponent(s)}`);
      const data = await res.json();
      const wasRunning = overviewCache[s]?.running;
      overviewCache[s] = { ...overviewCache[s], running: data.running, jar: data.jar };
      // If a server just came up, fetch its player count too.
      if (!wasRunning && data.running) {
        overviewCache[s].playerCount   = null;
        overviewCache[s].playersLoaded = false;
        fetch(`/api/players?s=${encodeURIComponent(s)}`)
          .then(r => r.json())
          .then(d => {
            if (overviewCache[s]) {
              overviewCache[s].playerCount   = d.ok ? d.count : null;
              overviewCache[s].playerMax     = d.ok ? d.max   : null;
              overviewCache[s].playersLoaded = true;
              renderOverviewCards();
            }
          }).catch(() => {});
      }
    } catch { /* keep stale data */ }
  }));
  renderOverviewCards();
}

function renderOverviewCards() {
  const grid    = $('overview-grid');
  const targets = sessions.length ? sessions : [currentSession || ''];
  if (!targets[0]) { grid.innerHTML = '<p class="hint">No sessions configured.</p>'; return; }

  grid.innerHTML = targets.map(s => {
    const d       = overviewCache[s];
    const running = d?.running;
    const dotCls  = running === true ? 'running' : running === false ? 'stopped' : 'unknown';
    const label   = running === true ? 'Running' : running === false ? 'Stopped' : 'Checking…';

    let players = '';
    if (running) {
      if (d?.playersLoaded) {
        const n = d.playerCount ?? '?';
        const m = d.playerMax   != null ? ` / ${d.playerMax}` : '';
        players = `<div class="overview-players">${n}${m} online</div>`;
      } else {
        players = `<div class="overview-players overview-players-loading">…</div>`;
      }
    }

    const jarLine = d?.jar ? `<div class="overview-jar">${esc(d.jar)}</div>` : '';

    return `
      <div class="overview-card" data-session="${esc(s)}">
        <div class="overview-session-name">${esc(s)}</div>
        <div class="overview-status-row">
          <span class="srv-dot ${dotCls}"></span>
          <span class="overview-status-label">${label}</span>
        </div>
        ${players}
        ${jarLine}
      </div>`;
  }).join('');

  grid.querySelectorAll('.overview-card').forEach(card => {
    card.addEventListener('click', () => goToSession(card.dataset.session));
  });
}

function goToSession(session) {
  clickSessionTab(session);
}

$('btn-overview-refresh').addEventListener('click', loadOverview);

// ── Server ───────────────────────────────────────────────

let srvPollTimer = null;
let selectedJar  = null;
let jarsLoaded   = false;
let srvPort      = null;

function srvStartPolling() {
  loadServerStatus();
  loadJars();
  loadServerIdentity();
  loadLatestMinecraft();
  srvPollTimer = setInterval(loadServerStatus, 5000);
}

async function loadLatestMinecraft() {
  try {
    const res  = await fetch(api('/api/server/latest-minecraft'));
    const data = await res.json();
    if (data.ok && data.version) {
      $('fabric-version').placeholder = `e.g. ${data.version}`;
    }
  } catch (_) {}
}

async function loadServerIdentity() {
  const wrap   = $('srv-identity');
  const icon   = $('srv-icon');
  const motdEl = $('srv-motd');

  try {
    const res  = await fetch(api('/api/server/identity'));
    const data = await res.json();
    if (!data.ok) { wrap.hidden = true; $('srv-port-card').hidden = true; return; }

    srvPort = data.port ?? null;
    const portCard = $('srv-port-card');
    if (srvPort) {
      $('srv-port-value').textContent = srvPort;
      portCard.hidden = false;
    } else {
      portCard.hidden = true;
    }

    const motdLines = data.motd
      ? data.motd.split('\n').filter(l => l.length > 0)
      : [];

    if (!data.has_icon && motdLines.length === 0) {
      wrap.hidden = true;
      return;
    }

    icon.hidden = !data.has_icon;
    if (data.has_icon) {
      const iconBase = api('/api/server/icon');
      icon.src = iconBase + (iconBase.includes('?') ? '&' : '?') + `t=${Date.now()}`;
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
  const wasRunning = serverRunning;
  const data = await fetchServerRunning();

  if (!data) {
    card.innerHTML = `<p class="hint">Could not reach server.</p>`;
    return;
  }
  const startSec = $('srv-start-section');
  if (data.running) {
    card.innerHTML = `
      <div class="srv-status-row">
        <span class="srv-dot running"></span>
        <span class="srv-status-label">Running</span>
        <button id="btn-stop" class="btn btn-danger btn-sm">&#x25A0; Stop</button>
      </div>
      ${data.jar ? `<div class="srv-jar">${esc(data.jar)}</div>` : ''}`;
    // Keep the start section visible but grayed out and inert.
    startSec.hidden = false;
    startSec.classList.add('srv-start-disabled');
    $('btn-start').disabled = true;
    $('mem-input').disabled = true;
    $('btn-stop').addEventListener('click', stopServer);
  } else {
    card.innerHTML = `
      <div class="srv-status-row">
        <span class="srv-dot stopped"></span>
        <span class="srv-status-label">Stopped</span>
      </div>`;
    // On running → stopped, re-fetch the jar list so the just-saved
    // last-used jar becomes the preselected default.
    if (wasRunning === true) {
      jarsLoaded = false;
      loadJars();
    }
    startSec.hidden = false;
    startSec.classList.remove('srv-start-disabled');
    $('btn-start').disabled = !selectedJar;
    $('mem-input').disabled = false;
  }
}

async function stopServer() {
  const btn = $('btn-stop');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Stopping…';
  try {
    await fetch(api('/api/server/stop'), { method: 'POST' });
  } catch (_) { /* poll will surface any error */ }
  setTimeout(loadServerStatus, 2000);
}

async function loadJars() {
  if (jarsLoaded) return;
  const wrap = $('jar-list-wrap');
  try {
    const res  = await fetch(api('/api/server/jars'));
    const data = await res.json();

    if (!data.ok) {
      wrap.innerHTML = `<p class="hint">Error: ${esc(data.error)}</p>`;
      return;
    }
    if (data.jars.length === 0) {
      wrap.innerHTML = `<p class="hint">No .jar files found in <code>${esc(data.jars_dir)}</code>.</p>`;
      return;
    }

    // Default to the jar this server last ran (unless the user already
    // picked one this visit), else auto-select if only one jar.
    if (!selectedJar && data.last_jar && data.jars.includes(data.last_jar)) {
      selectedJar = data.last_jar;
    }
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
  if (serverRunning === true) return;   // list is visible but inert while running
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
    const res  = await fetch(api('/api/server/download-fabric'), {
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
    const res  = await fetch(api('/api/server/start'), {
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
    const res  = await fetch(api('/api/mods/list'));
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
  const endpoint = action === 'activate'
    ? api('/api/mods/activate')
    : api('/api/mods/deactivate');
  const opFb = $('mods-op-feedback');
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
    const res  = await fetch(api('/api/mods/delete'), {
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
    const res  = await fetch(api('/api/worlds/list'));
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
    const res  = await fetch(api('/api/worlds/save'), {
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
    const res  = await fetch(api('/api/worlds/load'), {
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
    const res  = await fetch(api('/api/worlds/delete'), {
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
    const res  = await fetch(api('/api/worlds/delete-autosaves'), {
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

(async () => {
  renderHistory();
  await loadSessions();
  initConsole();
  overviewStartPolling();
  setInterval(fetchServerRunning, 15000);
})();
