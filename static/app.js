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

// Bumped on every session switch. A request carries the epoch it was issued
// under; if the admin has moved to another server by the time the reply lands,
// the reply belongs to a page that is no longer on screen and must be dropped.
let sessionEpoch = 0;

const STALE = Symbol('stale-session');

// Session-scoped fetch. Returns parsed JSON, or STALE if we switched servers
// while the request was in flight. Callers must bail on STALE — rendering it
// would put one server's data under another server's tab. Network failures
// still throw, so existing catch blocks keep working.
async function sessionJson(path, opts) {
  const epoch = sessionEpoch;
  const res   = await fetch(api(path), opts);
  const data  = await res.json();
  return epoch === sessionEpoch ? data : STALE;
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
    sessionEpoch++;              // discard replies still in flight for the old server
    currentSession = tab;
    serverRunning  = null;
    serverError    = null;
    jarsLoaded     = false;
    selectedJar    = null;
    startMode      = 'jar';
    startModePicked = false;
    srvPort        = null;
    resetSessionUi();
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

// Wipe every element that holds a *result* for the server we're leaving.
// The pages share one DOM across sessions, so anything left behind reads as
// though it belongs to the server being switched to — at best a stale message,
// at worst a live action button (the mods conflict notice) now aimed at the
// wrong server's files.
function resetSessionUi() {
  // Feedback lines. The second value is the element's base class, if it has one.
  [['start-feedback',     ''],
   ['say-feedback',       ''],
   ['mods-op-feedback',   ''],
   ['worlds-op-feedback', 'worlds-op-feedback'],
   ['world-save-feedback', ''],
   ['players-op-feedback', ''],
   ['add-player-feedback', '']].forEach(([id, base]) => {
    const el = $(id);
    if (!el) return;
    el.innerHTML = '';
    el.className = base;
  });

  $('dl-output').textContent = '';
  $('dl-output-wrap').hidden = true;

  // Not cosmetic: a choice left from the previous server misreports this one's
  // setting, and the next click would POST that intent at the wrong session.
  // Back to 'never' rather than merely unchecked — a radio group has no blank
  // state, and 'never' is the one value that cannot start something by mistake.
  $('srv-startpolicy-card').hidden = true;
  startPolicy = 'never';
  setStartPolicyRadios('never');
  startPolicyRadios().forEach(r => { r.disabled = false; });
  $('srv-startpolicy-note').textContent = '';

  // The same, and the cached state behind its note as well: a "Backing up the
  // world now…" left from the server we just left would both misreport this one
  // and — through stopBackupBusy — hold its Start button for a backup that is
  // nothing to do with it.
  $('srv-stopbackup-card').hidden  = true;
  $('srv-stopbackup').checked      = false;
  $('srv-stopbackup').disabled     = false;
  $('srv-stopbackup-note').textContent = '';
  stopBackupPlan = null;
  stopBackupLast = null;
  stopBackupBusy = false;

  // Drafts aimed at one particular server. The start form included: a script
  // name is one game's, and left behind it would be typed at another's pane.
  $('script-input').value     = '';
  $('script-suggestions').innerHTML = '';
  setStartMode('jar');          // after the clear: it re-reads the field
  $('say-input').value        = '';
  $('char-count').textContent = `0 / ${MAX_LEN}`;
  $('char-count').style.color = '';
  $('world-name').value       = '';
  $('add-player-name').value  = '';
  $('add-player-uuid').value  = '';
  $('add-player-op').checked  = false;

  // Data that is about to be re-fetched: show a placeholder rather than the
  // previous server's numbers while that request is in flight.
  const loading = '<p class="hint">Loading&hellip;</p>';
  $('console-out').textContent      = '';
  $('srv-identity').hidden          = true;
  $('srv-port-card').hidden         = true;
  $('srv-status-card').innerHTML    = loading;
  $('jar-list-wrap').innerHTML      = loading;
  $('mods-active-list').innerHTML   = loading;
  $('mods-inactive-list').innerHTML = loading;
  $('worlds-list-wrap').innerHTML   = loading;
  $('players-online').innerHTML     = loading;
  $('roster-list').innerHTML        = loading;
  $('suggest-list').innerHTML       = loading;
  ['mods-active-count', 'mods-inactive-count',
   'roster-count', 'suggest-count'].forEach(id => { $(id).textContent = ''; });
  $('mods-disk-info').hidden   = true;
  $('worlds-disk-info').hidden = true;
  $('whitelist-state').hidden  = true;

  onlineData = null;
  rosterData = null;
  // Scrolled up reading the old server's console? That position means nothing
  // in the new server's buffer, so start it at the tail. Deliberately not done
  // in reconnectConsole(), which also runs on every transport drop — a dropped
  // connection must not yank an admin who is reading back.
  consoleStick = true;
  // A load still running for the old server would otherwise make loadPlayers()
  // skip the new one as "already loading", leaving the page stuck on Loading…
  loadingPlayers = false;

  renderHistory();   // draws the session we just switched to
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
  // The console has been filling up while it was hidden, and nothing could
  // scroll it there — pin it now that it has a height to scroll.
  if (page === 'console' && consoleStick) consoleScrollToBottom();
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

// Follow the tail unless the admin has scrolled up to read back. This is a
// sticky flag rather than a measurement taken at render time, because the page
// is display:none until it is navigated to: everything measures 0 while hidden,
// so a render-time test reads "at bottom", the scroll assignment does nothing,
// and the admin arrives at the *top* of the buffer with the test now stuck at
// false. The flag survives that because only a real scroll event clears it, and
// no scroll events fire on a hidden element.
let consoleStick = true;
const CONSOLE_STICK_SLACK = 60;   // px of tolerance for "still at the bottom"

function consoleAtBottom() {
  return consoleOut.scrollHeight - consoleOut.clientHeight
         <= consoleOut.scrollTop + CONSOLE_STICK_SLACK;
}

function consoleScrollToBottom() {
  consoleOut.scrollTop = consoleOut.scrollHeight;
}

// Our own scrolling fires this too, which is harmless: it re-measures as
// at-bottom and leaves the flag set.
consoleOut.addEventListener('scroll', () => { consoleStick = consoleAtBottom(); });

// Wrapping changes with the width — on a phone, the URL bar sliding away is
// enough — and that moves the bottom out from under a pinned view.
window.addEventListener('resize', () => { if (consoleStick) consoleScrollToBottom(); });

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
      consoleOut.textContent = data.content;
      if (consoleStick) consoleScrollToBottom();
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
    const data = await sessionJson('/api/players');
    if (data === STALE) return;          // switched servers mid-request
    onlineData = data;
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
    const data = await sessionJson('/api/players/roster');
    if (data === STALE) return;
    rosterData = data;
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
    const data = await sessionJson(endpoint, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });
    if (data === STALE) return;          // action ran on the right server; UI moved on

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
    const data = await sessionJson('/api/players/add', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ name, uuid, op: $('add-player-op').checked }),
    });
    if (data === STALE) return;

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
  } finally {
    // finally, not a trailing statement: a STALE bail must still un-stick the
    // button, since it now belongs to the server we switched to.
    btn.disabled    = false;
    btn.textContent = '+ Add';
  }
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
const sayHistoryEl = $('say-history');
const MAX_LEN     = 256;
// Per session — a broadcast went to one server, so it must not appear under
// another server's tab. Kept (not cleared) so switching back shows it again.
const sayHistory  = {};

function currentHistory() {
  return (sayHistory[currentSession] ||= []);
}

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
    const data = await sessionJson('/api/say', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ message: msg }),
    });
    if (data === STALE) return;

    if (data.ok) {
      // The server may have removed characters a shell could act on; show what
      // was actually broadcast rather than what was typed.
      const sent = data.sent ?? msg;
      sayFeedback.textContent = sent === msg ? '✓ Sent' : '✓ Sent (some characters removed)';
      sayFeedback.className = 'fb-ok';
      addHistory(sent);
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
  const h = currentHistory();
  h.unshift({ msg, t });
  if (h.length > 30) h.pop();
  renderHistory();
}

function renderHistory() {
  const h = currentHistory();
  if (h.length === 0) {
    sayHistoryEl.innerHTML = '<p class="hint">No messages sent yet.</p>';
    return;
  }
  sayHistoryEl.innerHTML = h.map(entry => `
    <div class="history-row">
      <div class="history-time">${esc(entry.t)}</div>
      <div class="history-msg">${esc(entry.msg)}</div>
    </div>`).join('');
}

// ── Server running state (shared across pages) ───────────

let serverRunning = null;
let serverError   = null;   // set when the tmux pane itself can't be reached

async function fetchServerRunning() {
  try {
    const data = await sessionJson('/api/server/status');
    if (data === STALE) return STALE;
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

  // `peak` is in the same unit as `used`; the notch is left unmarked by design —
  // the tooltip carries the number so the row stays as narrow as it was.
  function bar(label, used, total, extra, peak, peakText) {
    const frac = v => total > 0 ? Math.max(0, Math.min(100, v / total * 100)) : 0;
    const pct  = frac(used);
    const cls  = pct > 90 ? 'stat-fill--danger' : pct > 75 ? 'stat-fill--warn' : '';
    const mark = peak == null ? ''
      : `<div class="stat-peak" style="left:${frac(peak).toFixed(1)}%" ` +
        `title="peak ${esc(peakText)}"></div>`;
    return `<div class="stat-row">
      <span class="stat-label">${label}</span>
      <div class="stat-bar"><div class="stat-fill ${cls}" style="width:${pct.toFixed(1)}%"></div>${mark}</div>
      <span class="stat-value">${extra}</span>
    </div>`;
  }

  let html = '';
  const cpu = stats.cpu;
  if (cpu) {
    const pct = Math.min(100, cpu.load_1m / cpu.cores * 100);
    html += bar('CPU', cpu.load_1m, cpu.cores,
      `${pct.toFixed(0)}%&ensp;<span class="stat-detail">${cpu.load_1m} / ${cpu.cores} cores</span>`,
      cpu.peak, `${cpu.peak} load`);
  }
  if (stats.ram) {
    html += bar('RAM', stats.ram.used, stats.ram.total,
      `${fmtBytes(stats.ram.used)} / ${fmtBytes(stats.ram.total)}`,
      stats.ram.peak, fmtBytes(stats.ram.peak));
  }
  if (stats.disk) {
    html += bar('Disk', stats.disk.used, stats.disk.total,
      `${fmtBytes(stats.disk.used)} / ${fmtBytes(stats.disk.total)}`,
      stats.disk.peak, fmtBytes(stats.disk.peak));
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

// Per-session cache: { [sessionName]: { running, jar, playerCount, playersLoaded, heap } }
const overviewCache   = {};
let overviewPollTimer = null;

const REFRESH_MIN = 1, REFRESH_MAX = 3600, REFRESH_DEFAULT = 15;
const REFRESH_KEY = 'vibepanel.overviewRefreshSecs';
const REFRESH_ON_KEY = 'vibepanel.overviewRefreshOn';

// Clamp rather than reject: this rate only drives a repeating fetch, so the
// worst a silly value can do is hammer the panel — and the box is a spinner,
// so a stray keystroke shouldn't be able to.
function refreshSecs() {
  const raw = parseInt(localStorage.getItem(REFRESH_KEY), 10);
  if (!Number.isFinite(raw)) return REFRESH_DEFAULT;
  return Math.max(REFRESH_MIN, Math.min(REFRESH_MAX, raw));
}

// Anything but an explicit 'off' means on, so a missing or garbled key leaves
// auto-refresh working rather than silently leaving a stale page on screen.
function refreshOn() {
  return localStorage.getItem(REFRESH_ON_KEY) !== 'off';
}

function overviewStartPolling() {
  loadOverview();
  overviewRestartTimer();
}

// Off leaves overviewPollTimer null, which is the same state as being on
// another page — in both cases there is simply nothing scheduled.
function overviewRestartTimer() {
  clearInterval(overviewPollTimer);
  overviewPollTimer = refreshOn()
    ? setInterval(refreshOverviewStatus, refreshSecs() * 1000)
    : null;
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
      if (!data.running) overviewCache[s].heap = null;
    } catch {
      overviewCache[s] = { ...overviewCache[s], running: null };
    }
    return s;
  }));
  renderOverviewCards();

  // 2. Fetch player counts and heap usage only for sessions that are running.
  await Promise.all(targets.filter(s => overviewCache[s]?.running).map(async s => {
    const players = (async () => {
      try {
        const res  = await fetch(`/api/players?s=${encodeURIComponent(s)}`);
        const data = await res.json();
        overviewCache[s].playerCount  = data.ok ? data.count  : null;
        overviewCache[s].playerMax    = data.ok ? data.max    : null;
        overviewCache[s].playersLoaded = true;
      } catch {
        overviewCache[s].playersLoaded = true;
      }
    })();
    await Promise.all([players, fetchHeap(s)]);
  }));
  renderOverviewCards();
}

// Sampling the heap is also what records its peak server-side, so this runs on
// every overview refresh — but only on those. No timer of its own. The read is
// a 32 KB file on the panel's own host, so it is cheap enough to sit inline.
async function fetchHeap(session) {
  let heap = null;
  try {
    const res = await fetch(`/api/server/heap?s=${encodeURIComponent(session)}`);
    heap = await res.json();
  } catch { /* leave heap null: card simply shows no bar */ }
  if (overviewCache[session]) overviewCache[session].heap = heap;
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
      if (data.running) await fetchHeap(s);
      else overviewCache[s].heap = null;
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
        ${heapBlock(d)}
        ${players}
        ${jarLine}
      </div>`;
  }).join('');

  grid.querySelectorAll('.overview-card').forEach(card => {
    card.addEventListener('click', () => goToSession(card.dataset.session));
  });
}

// Heap bar: the track is the reserved heap (-Xmx), the fill is the live set as
// of the last GC, and the notch is the highest live set seen since this JVM
// started. "live" rather than "used" on purpose — the JVM publishes these
// counters at collection boundaries, so this is what survived the last GC, not
// what is occupied this instant. See _read_heap() for why that's the better
// number to draw.
function heapBlock(d) {
  const h = d?.heap;
  if (!h) return '';
  if (!h.ok) {
    return `<div class="overview-heap-note" title="${esc(h.error || '')}">heap unavailable</div>`;
  }
  const max = h.reserved || h.committed || 0;
  if (!max) return '';
  const pct     = v => Math.max(0, Math.min(100, v / max * 100));
  const usedPct = pct(h.used);
  const peakPct = pct(h.peak);
  const gcs     = h.collections != null ? `, after ${h.collections.toLocaleString()} collections` : '';
  const title   = `${fmtBytes(h.used)} live${gcs} — peak ${fmtBytes(h.peak)}, ` +
                  `${fmtBytes(h.committed)} committed, ${fmtBytes(max)} reserved`;
  return `
    <div class="overview-heap" title="${esc(title)}">
      <div class="heap-bar">
        <div class="heap-bar-used" style="width:${usedPct.toFixed(1)}%"></div>
        <div class="heap-bar-peak" style="left:${peakPct.toFixed(1)}%"></div>
      </div>
      <div class="heap-figures">
        <span class="heap-fig-used">${fmtBytes(h.used)} live</span>
        <span class="heap-fig-peak">peak ${fmtBytes(h.peak)}</span>
        <span class="heap-fig-max">max ${fmtBytes(max)}</span>
      </div>
    </div>`;
}

function goToSession(session) {
  clickSessionTab(session);
}

$('btn-overview-refresh').addEventListener('click', loadOverview);

const refreshInput  = $('overview-refresh-secs');
const refreshToggle = $('btn-refresh-toggle');

// The interval box stays visible when auto-refresh is off, but inert: the rate
// it holds is not in play, and hiding it would shuffle the header on every
// toggle. The manual Refresh button is unaffected either way.
function applyRefreshUi() {
  const on = refreshOn();
  refreshToggle.textContent = on ? 'every' : 'off';
  refreshToggle.title = on ? 'Turn auto-refresh off' : 'Turn auto-refresh on';
  refreshInput.disabled = !on;
  $('refresh-rate').classList.toggle('refresh-rate--off', !on);
}

refreshInput.value = refreshSecs();
applyRefreshUi();

// 'change' rather than 'input': committing on every keystroke would restart the
// timer at 1 s the moment someone types the "1" of "120".
refreshInput.addEventListener('change', () => {
  localStorage.setItem(REFRESH_KEY, refreshInput.value);
  const secs = refreshSecs();
  localStorage.setItem(REFRESH_KEY, secs);   // store the clamped value, not the typed one
  refreshInput.value = secs;                 // and show what was actually accepted
  if (activePage === 'overview') overviewRestartTimer();
});

refreshToggle.addEventListener('click', () => {
  localStorage.setItem(REFRESH_ON_KEY, refreshOn() ? 'off' : 'on');
  applyRefreshUi();
  // Restarting covers both directions: it schedules a timer when switching on,
  // and clears the existing one when switching off.
  if (activePage === 'overview') overviewRestartTimer();
});

$('btn-reset-peaks').addEventListener('click', async () => {
  const btn = $('btn-reset-peaks');
  btn.disabled = true;
  try {
    await fetch('/api/peaks/reset', { method: 'POST' });
  } catch { /* nothing to undo: a failed reset just leaves the old peaks */ }
  btn.disabled = false;
  // Peaks are recorded server-side as a side effect of sampling, so the way to
  // see the reset is to sample again — which loadOverview does for every card.
  loadOverview();
});

// ── Server ───────────────────────────────────────────────

let srvPollTimer = null;
let selectedJar  = null;
let jarsLoaded   = false;
let srvPort      = null;
// 'jar' | 'script'. Which of the two start forms is live, and the only thing
// that decides what /api/server/start is sent — never "whichever field has
// something in it", since the other one usually does too.
let startMode    = 'jar';
// Whether the admin has picked a mode themselves this visit, in which case the
// remembered one must not overwrite it on the next load.
let startModePicked = false;

function srvStartPolling() {
  loadServerStatus();
  loadJars();
  loadServerIdentity();
  loadLatestMinecraft();
  loadStartPolicy();
  loadStopBackup();
  srvPollTimer = setInterval(loadServerStatus, 5000);
}

async function loadLatestMinecraft() {
  try {
    const data = await sessionJson('/api/server/latest-minecraft');
    if (data === STALE) return;
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
    const data = await sessionJson('/api/server/identity');
    if (data === STALE) return;
    if (!data.ok) { wrap.hidden = true; $('srv-port-card').hidden = true; return; }

    // Port comes from this session's server.properties and the Bedrock port from
    // its Geyser config, which most servers won't have; the public IP is host-wide
    // and looked up once when the panel started, so it may be absent too.
    srvPort = data.port ?? null;
    const bedrockPort = data.bedrock_port ?? null;
    const publicIp    = data.public_ip || null;

    $('srv-port-field').hidden = !srvPort;
    if (srvPort) $('srv-port-value').textContent = srvPort;

    $('srv-bedrock-field').hidden = !bedrockPort;
    if (bedrockPort) $('srv-bedrock-value').textContent = bedrockPort;

    $('srv-ip-field').hidden = !publicIp;
    if (publicIp) $('srv-ip-value').textContent = publicIp;

    $('srv-port-card').hidden = !srvPort && !bedrockPort && !publicIp;

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

  if (data === STALE) return;   // reply was for the server we just left
  if (!data) {
    card.innerHTML = `<p class="hint">Could not reach server.</p>`;
    return;
  }
  // Before the branches: the stopped branch re-enables the start form, and
  // whether Start is actually allowed depends on this.
  const wasBusy  = stopBackupBusy;
  stopBackupLast = data.stop_backup || null;
  stopBackupBusy = stopBackupLast?.state === 'running';
  renderStopBackupNote();

  const startSec = $('srv-start-section');
  if (data.running) {
    // What it is running, read off the process itself: the jar, and the heap it
    // was given. Either can be unknown — a command line with no -jar, a -Xmx we
    // don't recognise, a privilege wrapper we couldn't see through — so the line
    // is built from whichever we have, and omitted when we have neither.
    const ran = [data.jar, data.mem].filter(Boolean).map(esc).join(' &middot; ');
    card.innerHTML = `
      <div class="srv-status-row">
        <span class="srv-dot running"></span>
        <span class="srv-status-label">Running</span>
        <button id="btn-stop" class="btn btn-danger btn-sm">&#x25A0; Stop</button>
      </div>
      ${ran ? `<div class="srv-jar">${ran}</div>` : ''}`;
    // Stopped → running: the panel has just read that jar and heap off the
    // running process and remembered them, so re-read the form to match. The
    // case this is for is a server started by hand at the pane — its settings
    // were never typed into this page, and without this the form would sit
    // describing the previous run right up until someone pressed Start.
    if (wasRunning === false) reloadStartForm();
    // Same edge: under "unless it was stopped on purpose" the note is a reading
    // of the game's log, which this start has just rewritten.
    if (wasRunning === false) loadStartPolicy();
    // Keep the start section visible but grayed out and inert.
    startSec.hidden = false;
    startSec.classList.add('srv-start-disabled');
    setStartFormDisabled(true);
    $('btn-stop').addEventListener('click', stopServer);
  } else {
    // Say when a backup is holding Start, rather than leaving a button that is
    // grayed out for no visible reason.
    card.innerHTML = `
      <div class="srv-status-row">
        <span class="srv-dot stopped"></span>
        <span class="srv-status-label">Stopped</span>
      </div>
      ${stopBackupBusy ? `<div class="srv-jar">Backing up the world&hellip;</div>` : ''}`;
    // On running → stopped, re-fetch the jar list so the just-saved
    // last-used jar becomes the preselected default.
    if (wasRunning === true) reloadStartForm();
    // Same edge, and the edge out of a backup: the note is what reports a stop
    // the panel handled on its own, and the plan is where its problems appear.
    if (wasRunning === true || (wasBusy && !stopBackupBusy)) loadStopBackup();
    // And the start policy's own note, which under "unless it was stopped on
    // purpose" is a verdict on the stop that just happened. Leaving it would
    // have the card claiming the last run crashed while someone was watching
    // the `stop` they typed take effect.
    if (wasRunning === true) loadStartPolicy();
    startSec.hidden = false;
    startSec.classList.remove('srv-start-disabled');
    setStartFormDisabled(false);
  }
}

// Every input in the start form goes inert together while the server runs —
// including the mode radios, so the form cannot be switched under a running
// server and left describing something it isn't.
function setStartFormDisabled(disabled) {
  $('mem-input').disabled    = disabled;
  $('script-input').disabled = disabled;
  document.querySelectorAll('input[name="start-mode"]')
          .forEach(r => { r.disabled = disabled; });
  if (disabled) $('btn-start').disabled = true;
  else updateStartEnabled();
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

// Re-read the start form from the panel, discarding this visit's jar pick.
//
// Both flags have to go: loadJars() loads once per visit, and even when made to
// run again it treats an existing selectedJar as the admin's choice and leaves
// it alone. On either edge of a run there is no such choice to protect — the
// form is inert while the server is up — and the point of re-reading is to
// replace whatever was picked before with what the server actually ran.
function reloadStartForm() {
  jarsLoaded  = false;
  selectedJar = null;
  loadJars();
}

// Loads the whole start form — the jar list, and the script name beside it,
// since both come from /api/server/jars in one reply.
async function loadJars() {
  if (jarsLoaded) return;
  const wrap = $('jar-list-wrap');
  try {
    const data = await sessionJson('/api/server/jars');
    if (data === STALE) return;

    if (!data.ok) {
      wrap.innerHTML = `<p class="hint">Error: ${esc(data.error)}</p>`;
      return;
    }

    // Script fields first, and above the empty-jars return below: a server
    // started by its own script very often has no jar in server-jars at all,
    // and that must not be what stops its script name being filled in.
    if (data.last_script && document.activeElement !== $('script-input')) {
      $('script-input').value = data.last_script;
    }
    $('script-suggestions').innerHTML = (data.scripts || [])
      .map(s => `<option value="${esc(s)}"></option>`).join('');
    // Same "don't clobber a choice being made" rule as the jar and memory
    // fields: the remembered mode is a default, not a correction.
    if (!startModePicked) setStartMode(data.last_mode === 'script' ? 'script' : 'jar');

    if (data.jars.length === 0) {
      wrap.innerHTML = `<p class="hint">No .jar files found in <code>${esc(data.jars_dir)}</code>.</p>`;
      updateStartEnabled();
      return;
    }

    // Default to the jar this server last ran (unless the user already
    // picked one this visit), else auto-select if only one jar.
    if (!selectedJar && data.last_jar && data.jars.includes(data.last_jar)) {
      selectedJar = data.last_jar;
    }
    if (data.jars.length === 1) selectedJar = data.jars[0];

    // Same idea for memory: offer what this server last ran with, so a 4G
    // server doesn't quietly come back at the 1024M default. Never clobber a
    // value the admin is in the middle of typing.
    if (data.last_mem && document.activeElement !== $('mem-input')) {
      $('mem-input').value = data.last_mem;
    }

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

    updateStartEnabled();
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
  updateStartEnabled();
}

// ── Jar or start script ───────────────────────────────────
//
// The two forms are exclusive, so only the live one is shown and only the live
// one is read when Start is pressed. The other keeps its value — an admin who
// looks at the script field and goes back to jars should find their jar still
// selected — it simply has no say until its radio is picked again.

function setStartMode(mode) {
  startMode = mode === 'script' ? 'script' : 'jar';
  document.querySelectorAll('input[name="start-mode"]').forEach(r => {
    r.checked = r.value === startMode;
  });
  $('start-jar-fields').hidden    = startMode !== 'jar';
  $('start-script-fields').hidden = startMode !== 'script';
  updateStartEnabled();
}

// Start needs whatever the live form needs, and nothing from the other one.
function updateStartEnabled() {
  const ready = startMode === 'script'
    ? $('script-input').value.trim() !== ''
    : !!selectedJar;
  // Held while the world is being tarred after the last stop: starting into it
  // would corrupt the archive. /api/server/start refuses too — this is so the
  // button matches, and the status card says why.
  $('btn-start').disabled = !ready || serverRunning === true || stopBackupBusy;
}

document.querySelectorAll('input[name="start-mode"]').forEach(radio => {
  radio.addEventListener('change', () => {
    if (serverRunning === true) return;   // visible but inert while running
    startModePicked = true;
    setStartMode(radio.value);
  });
});

$('script-input').addEventListener('input', updateStartEnabled);

// ── Start policy ──────────────────────────────────────────
//
// A standing per-server policy, so it stays togglable whether the server is
// running or stopped — unlike the start form, which goes inert while it runs.
//
// The last value the server confirmed, so a failed write has something to go
// back to. Three radios cannot be restored by flipping a boolean the way the
// checkbox this replaced could.
let startPolicy = 'never';

function startPolicyRadios() {
  return document.querySelectorAll('input[name="start-policy"]');
}

function setStartPolicyRadios(policy) {
  startPolicyRadios().forEach(r => { r.checked = r.value === policy; });
}

function renderStartPolicy(d) {
  $('srv-startpolicy-card').hidden = false;
  startPolicy = d.start_policy;
  setStartPolicyRadios(startPolicy);
  startPolicyRadios().forEach(r => { r.disabled = false; });

  // Nothing is said about what the policy *would* do: each option is a sentence
  // that already says it, and `reason` is a diagnostic — the store and the
  // panel's own boot log are where to go for that. Only a problem earns a line,
  // because it is the setting quietly not working rather than something to know.
  $('srv-startpolicy-note').textContent = d.problem
    ? (d.start_policy === 'never' ? `${d.problem}.` : `Cannot start it: ${d.problem}.`)
    : '';
}

async function loadStartPolicy() {
  try {
    const data = await sessionJson('/api/server/start-policy');
    if (data === STALE) return;      // reply was for the server we just left
    if (!data.ok) { $('srv-startpolicy-card').hidden = true; return; }
    renderStartPolicy(data);
  } catch (_) {
    $('srv-startpolicy-card').hidden = true;
  }
}

startPolicyRadios().forEach(radio => {
  radio.addEventListener('change', async () => {
    const want = radio.value;
    const had  = startPolicy;
    startPolicyRadios().forEach(r => { r.disabled = true; });
    try {
      const data = await sessionJson('/api/server/start-policy', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ start_policy: want }),
      });
      if (data === STALE) return;    // the write landed; only the redraw is stale
      if (data.ok) { renderStartPolicy(data); return; }
      setStartPolicyRadios(had);
      $('srv-startpolicy-note').textContent = data.error || 'Could not save that.';
    } catch (err) {
      setStartPolicyRadios(had);
      $('srv-startpolicy-note').textContent = err.message;
    } finally {
      startPolicyRadios().forEach(r => { r.disabled = false; });
    }
  });
});

// ── Back up the world on stop ─────────────────────────────────
//
// The note under the checkbox is written from two sources that arrive
// separately: the plan, fetched when the page opens and whenever the setting
// changes, and the live state of the current backup, which rides along on the
// status poll. Keep the last of each so either can redraw the line alone.
//
// It carries *state* only — what a backup is doing or did — never an
// explanation of what the checkbox means. The label says that, and saying it
// twice buried the one line that matters: "Backing up the world now…", which is
// why Start is held.

let stopBackupPlan = null;
let stopBackupLast = null;
// Whether a backup is tarring right now, in which case Start is held — see
// updateStartEnabled(). Separate from stopBackupLast so it survives the plan
// fetch overwriting the last-result line.
let stopBackupBusy = false;

function renderStopBackupNote() {
  const note = $('srv-stopbackup-note');
  const d    = stopBackupPlan;
  const bits = [];

  // A problem is not an explanation — it is the setting quietly not working, so
  // it keeps its line.
  if (d && d.problem) {
    bits.push(d.backup_on_stop ? `Cannot back up yet: ${d.problem}.` : `${d.problem}.`);
  }

  // The last backup this panel ran, from the status poll. Worth its own line:
  // it is the only place a stop that happened while nobody was looking — a
  // crash, a `stop` typed at the pane — shows up as having been handled.
  const last = stopBackupLast;
  if (last) {
    if (last.state === 'running') {
      bits.push('Backing up the world now…');
    } else if (last.state === 'done') {
      bits.push(`Last backup ${last.at}: ${last.filename} (${fmtBytes(last.size)}).`);
    } else if (last.state === 'failed') {
      bits.push(`Last backup ${last.at} failed: ${last.error}`);
    } else if (last.state === 'skipped') {
      bits.push(`Last stop ${last.at} was not backed up: ${last.error}.`);
    }
  }
  note.textContent = bits.join(' ');
}

function renderStopBackup(d) {
  $('srv-stopbackup-card').hidden = false;
  $('srv-stopbackup').checked     = !!d.backup_on_stop;
  $('srv-stopbackup').disabled    = false;
  stopBackupPlan = d;
  // The plan carries the last result too, so a page opened after the fact still
  // shows it; the poll then keeps it current.
  if (d.last) stopBackupLast = d.last;
  renderStopBackupNote();
}

async function loadStopBackup() {
  try {
    const data = await sessionJson('/api/server/backup-on-stop');
    if (data === STALE) return;      // reply was for the server we just left
    if (!data.ok) { $('srv-stopbackup-card').hidden = true; return; }
    renderStopBackup(data);
  } catch (_) {
    $('srv-stopbackup-card').hidden = true;
  }
}

$('srv-stopbackup').addEventListener('change', async () => {
  const box  = $('srv-stopbackup');
  const want = box.checked;
  box.disabled = true;
  try {
    const data = await sessionJson('/api/server/backup-on-stop', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ backup_on_stop: want }),
    });
    if (data === STALE) return;      // the write landed; only the redraw is stale
    if (data.ok) { renderStopBackup(data); return; }
    box.checked = !want;
    $('srv-stopbackup-note').textContent = data.error || 'Could not save that.';
  } catch (err) {
    box.checked = !want;
    $('srv-stopbackup-note').textContent = err.message;
  } finally {
    box.disabled = false;
  }
});

$('btn-srv-refresh').addEventListener('click', () => {
  jarsLoaded = false;
  loadServerStatus();
  loadJars();
  loadServerIdentity();
  loadLatestMinecraft();
  loadStartPolicy();
  loadStopBackup();
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
    const data = await sessionJson('/api/server/download-fabric', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ version: version || null }),
    });
    if (data === STALE) { btn.disabled = false; btn.textContent = '↓ Download'; return; }

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
  const mem      = $('mem-input').value.trim().toUpperCase();
  const script   = $('script-input').value.trim();
  const btn      = $('btn-start');
  const feedback = $('start-feedback');

  // Only the live form is checked here. Both fields go in the body, but `mode`
  // travels with them and decides which one the server reads — and it checks
  // all of this again; these two are only here to save an obvious round trip.
  if (startMode === 'script') {
    if (!script) return;
    if (script.includes('/') || script.includes('\\')) {
      feedback.textContent = 'Script must be a plain filename in the game directory — no slashes.';
      feedback.className = 'fb-error';
      return;
    }
  } else {
    if (!selectedJar) return;
    if (!/^\d+[MG]$/.test(mem)) {
      feedback.textContent = 'Invalid memory format — use e.g. 1024M or 2G';
      feedback.className = 'fb-error';
      return;
    }
  }

  btn.disabled = true;
  btn.textContent = 'Starting…';
  feedback.textContent = '';
  feedback.className = '';

  try {
    const data = await sessionJson('/api/server/start', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ mode: startMode, jar: selectedJar, mem, script }),
    });
    if (data === STALE) { btn.textContent = '▶ Start Server'; return; }

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
    const data = await sessionJson('/api/mods/list');
    if (data === STALE) return;
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
    const data = await sessionJson(endpoint, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ filename }),
    });
    if (data === STALE) return;

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
    const data = await sessionJson('/api/mods/delete', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ filename, location: 'both' }),
    });
    if (data === STALE) return;

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
    const data = await sessionJson('/api/worlds/list');
    if (data === STALE) return;
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
    const data = await sessionJson('/api/worlds/save', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ name: nameInput.value.trim() }),
    });
    if (data === STALE) return;

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
    const data = await sessionJson('/api/worlds/load', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ filename }),
    });
    if (data === STALE) return;

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
    const data = await sessionJson('/api/worlds/delete', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ filename }),
    });
    if (data === STALE) return;

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
    const data = await sessionJson('/api/worlds/delete-autosaves', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    if (data === STALE) return;

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
