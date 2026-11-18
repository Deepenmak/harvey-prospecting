let currentTab = 'today';
let companyDrill = false;      // true while viewing a single company's contacts
let _companies = [], _prospects = [], _campaigns = [];
let _signals = null;           // last /api/signals payload, for the cohort builder
let _desk = { items: [], i: 0 };

// ── Appearance ──
//
// Three states, not two: "auto" follows the OS and is the default, so the
// dashboard matches the rest of your machine until you deliberately override
// it. Stored per-browser; nothing is sent anywhere.

const THEMES = ['auto', 'light', 'dark'];

function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === 'auto') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', mode);
  document.querySelectorAll('[data-theme-opt]').forEach((b) =>
    b.setAttribute('aria-pressed', String(b.dataset.themeOpt === mode)));
}

function setTheme(mode) {
  try { localStorage.setItem('sellbird-theme', mode); } catch { /* private mode */ }
  applyTheme(mode);
}

(function initTheme() {
  let saved = 'auto';
  try {
    // Carry the choice over from Harvey, Sellbird's old name.
    const legacy = localStorage.getItem('harvey-theme');
    if (legacy && !localStorage.getItem('sellbird-theme')) localStorage.setItem('sellbird-theme', legacy);
    if (legacy) localStorage.removeItem('harvey-theme');
    saved = localStorage.getItem('sellbird-theme') || 'auto';
  } catch { /* ignore */ }
  applyTheme(THEMES.includes(saved) ? saved : 'auto');
})();

// ── Utilities ──

function escHtml(s) {
  if (s === null || s === undefined || s === '') return '';
  return String(s).replace(/[&<>"']/g, ch => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]
  ));
}

// A value placed inside an onclick handler as a JS string: JSON-quote it,
// then escape for the attribute (escHtml alone is undone before the JS runs).
function jsq(v) {
  return escHtml(JSON.stringify(String(v ?? '')));
}

// The reason a failed action gave, or a fallback when it gave none.
function failMsg(data, fallback) {
  return (data && (data.message || data.error || data.detail)) || fallback;
}

// Lookup tables keyed by stored strings: "constructor" must not hit Object's.
function own(table, key) {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

// Links built from collected data (websites, profiles) open only as http(s):
// a javascript: URL from a scraped page must never run in the dashboard.
function safeHref(u) {
  const s = String(u || '').trim();
  return /^https?:\/\//i.test(s) ? escHtml(s) : '';
}

// ── One status vocabulary ──
//
// Sellbird's tables each carry their own words for state: a prospect is `new`,
// a campaign is `draft`, an email is `pending_review`, a signal is `proposed`.
// Five of those mean "waiting on you" and the old dashboard styled every one
// differently. Everything now resolves through this map, so a status reads the
// same way no matter which table it came out of.
//
// tone: waiting (needs a human) | active (in flight) | good | bad | idle
const STATUS = {
  // prospects
  new:            ['New', 'active'],
  contacted:      ['Contacted', 'active'],
  replied:        ['Replied', 'good'],
  accepted_offer: ['Said yes to your offer', 'good'],
  interested:     ['Interested', 'good'],
  meeting:        ['Meeting booked', 'note'],
  not_interested: ['Not interested', 'bad'],
  bounced:        ['Bounced', 'bad'],
  write_failed:   ["Couldn't write", 'bad'],
  no_tip:         ['Nothing to write about yet', 'idle'],
  queued:         ['Queued', 'active'],
  opted_out:      ['Opted out', 'bad'],
  // campaigns
  draft:          ['Draft', 'waiting'],
  active:         ['Active', 'good'],
  completed:      ['Completed', 'idle'],
  paused:         ['Paused', 'bad'],
  // conversations
  open:           ['Open', 'active'],
  closed:         ['Closed', 'idle'],
  closed_won:     ['Won', 'good'],
  closed_lost:    ['Lost', 'bad'],
  objection:      ['Objection', 'waiting'],
  question:       ['Question', 'active'],
  ooo:            ['Out of office', 'idle'],
  wrong_person:   ['Wrong person', 'waiting'],
  unsubscribe:    ['Unsubscribed', 'bad'],
  escalate:       ['Needs a human', 'bad'],
  needs_human:    ['Needs you', 'waiting'],
  // outbox
  pending_review: ['Waiting on you', 'waiting'],
  approved:       ['Approved', 'good'],
  sending:        ['Sending now', 'active'],
  scheduled:      ['Scheduled', 'active'],
  sent:           ['Sent', 'good'],
  awaiting_reply: ['Awaiting reply', 'active'],
  failed:         ['Failed', 'bad'],
  rejected:       ['Rejected', 'idle'],
  cancelled:      ['Cancelled', 'idle'],
  // signals
  proposed:       ['Waiting on you', 'waiting'],
  confirmed:      ['Confirmed', 'good'],
  // email deliverability
  verified:       ['Verified', 'good'],
  risky:          ['Catch-all', 'waiting'],
  guess:          ['Unverified', 'idle'],
  invalid:        ['Invalid', 'bad'],
  // runs
  running:        ['Running', 'active'],
  stale:          ['Stale', 'bad'],
  stalled:        ['Stopped', 'bad'],
  // imported lists
  analysing:      ['Reading it', 'active'],
  review:         ['Waiting on you', 'waiting'],
  enriching:      ['Enriching', 'active'],
  imported:       ['In pipeline', 'good'],
  discarded:      ['Discarded', 'idle'],
};

function statusMeta(status) {
  const key = String(status || '').toLowerCase().replace(/[^a-z0-9]+/g, '_');
  const hit = own(STATUS, key);
  if (hit) return { label: hit[0], tone: hit[1] };
  const label = String(status || 'unknown').replace(/_/g, ' ');
  return { label: label.charAt(0).toUpperCase() + label.slice(1), tone: 'idle' };
}

function badge(status) {
  const m = statusMeta(status);
  return '<span class="badge t-' + m.tone + '">' + escHtml(m.label) + '</span>';
}

function formatDate(d) {
  if (!d) return '';
  try {
    let t = String(d).trim().replace(' ', 'T');
    if (/^\d{4}-\d\d-\d\dT/.test(t) && !/[zZ]$|[+-]\d\d:?\d\d$/.test(t)) t += 'Z';  // stored UTC
    const dt = new Date(t);
    if (isNaN(dt)) return escHtml(d);
    return dt.toLocaleString('en-US', {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  } catch { return escHtml(d); }
}

// ── Small render helpers ──

// The sprite in index.html holds every icon; this just references one.
function icon(name, cls) {
  return '<svg class="icon' + (cls ? ' ' + cls : '') + '" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
}

function initials(first, last) {
  const a = String(first || '').trim(), b = String(last || '').trim();
  const ch = x => Array.from(x)[0] || '';  // whole characters, not UTF-16 halves
  const s = (ch(a) + ch(b)) || '?';
  return escHtml(s.toUpperCase());
}

// Stored times are UTC, often without a zone ("2026-10-21 03:07:57").
function parseTs(d) {
  let t = String(d || '').trim().replace(' ', 'T');
  if (t && !/[zZ]$|[+-]\d\d:?\d\d$/.test(t)) t += 'Z';
  return new Date(t);
}

// "3m ago", "5h ago", "Yesterday", then a date: recent things read faster.
function relTime(d) {
  if (!d) return '';
  const dt = parseTs(d);
  if (isNaN(dt)) return escHtml(d);
  const mins = Math.round((Date.now() - dt) / 60000);
  if (mins < -1 && mins > -60) return 'in ' + -mins + 'm';
  if (mins < -1 && mins > -60 * 24) return 'in ' + Math.round(-mins / 60) + 'h';
  if (mins >= -1 && mins < 1) return 'just now';
  if (mins >= 1 && mins < 60) return mins + 'm ago';
  if (mins >= 60 && mins < 60 * 24) return Math.round(mins / 60) + 'h ago';
  if (mins >= 60 * 24 && mins < 60 * 48) return 'Yesterday';
  const opts = {month: 'short', day: 'numeric'};
  if (dt.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  return dt.toLocaleDateString('en-US', opts);
}

function dayLabel(d) {
  const dt = parseTs(d);
  if (isNaN(dt)) return '';
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const that = new Date(dt); that.setHours(0, 0, 0, 0);
  const diff = Math.round((today - that) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  return dt.toLocaleDateString('en-US', {weekday: 'long', month: 'short', day: 'numeric'});
}

function timeOfDay(d) {
  const dt = parseTs(d);
  return isNaN(dt) ? '' : dt.toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit'});
}

// Columns of a tiny bar chart; the last day is "today".
function sparkBars(values, title) {
  const vals = (values || []).map(v => Number(v) || 0);
  if (!vals.length) return '';
  const max = Math.max(...vals, 1);
  return '<span class="spark" role="img" aria-label="' + escHtml(title || 'trend') + '">' +
    vals.map((v, i) => '<i class="' + (v ? 'on' : '') + '" style="height:' +
      Math.max(2, Math.round(v / max * 28)) + 'px" title="' + v + '"></i>').join('') + '</span>';
}

// A segmented meter, 0-100.
function meterHtml(pct, segments) {
  const n = segments || 10;
  const v = Math.max(0, Math.min(100, Number(pct) || 0));
  const on = Math.round(v / 100 * n);
  return '<span class="meter-row"><span class="meter" role="meter" aria-valuemin="0" ' +
    'aria-valuemax="100" aria-valuenow="' + v + '">' +
    Array.from({length: n}, (_, i) => '<i class="' + (i < on ? 'on' : '') + '"></i>').join('') +
    '</span><span class="v">' + v + '</span></span>';
}

const SOURCE_LABELS = {
  company_website: 'Team page', known_company_backfill: 'Team page',
  scraped_mailto: 'Published address', hunter_domain_search: 'Hunter', linkedin_search: 'LinkedIn',
  job_board: 'Job board', web_search: 'Web search', company_inbox: 'Company inbox',
  osm: 'OpenStreetMap', dataforseo_listings: 'DataForSEO', dataforseo_serp: 'DataForSEO SERP', dataforseo_maps: 'DataForSEO Maps',
  serper: 'Serper', tinyfish: 'TinyFish', scavio_maps: 'Scavio Maps', scavio_search: 'Scavio Search',
};
function sourceLabel(s) {
  if (!s) return '';
  return escHtml(Object.prototype.hasOwnProperty.call(SOURCE_LABELS, s) ? SOURCE_LABELS[s]
    : String(s).replace(/_/g, ' ').replace(/^./, c => c.toUpperCase()));
}

const AGENT_ICONS = {scout: 'discover', writer: 'pen', sender: 'campaigns', handler: 'message',
                     analyst: 'usage', user: 'contacts', main: 'refresh'};
// One plain sentence per logged action, from the details each agent records.
function actionText(a) {
  const d = a.details || {};
  const who = escHtml(d.prospect_email || d.prospect || d.to || '');
  const camp = escHtml(d.campaign_name || d.campaign || '');
  const intent = d.intent ? ' · ' + escHtml(String(d.intent).replace(/_/g, ' ')) : '';
  const n = (v, one, many) => (Number(v) || 0) + ' ' + ((Number(v) || 0) === 1 ? one : many);
  switch (a.action_type) {
    case 'prospect': return 'Found ' + n(d.prospects_found, 'new contact', 'new contacts');
    case 'write_campaign': return 'Wrote “' + camp + '” for ' + n(d.prospect_count, 'contact', 'contacts');
    case 'write_failed': return 'Couldn’t write for some contacts' + (camp ? ' in “' + camp + '”' : '');
    case 'stage_campaign': return 'Queued ' + n(d.staged, 'email', 'emails') + ' from “' + camp + '”' +
      (d.mode ? ' · ' + escHtml(d.mode) : '');
    case 'send_campaign': return 'Sent “' + camp + '” to Instantly · ' + n(d.leads_added, 'lead', 'leads');
    case 'email_sent': return d.kind === 'reply' ? 'Sent a reply to ' + who
      : 'Sent email ' + escHtml(String(d.step || 1)) + ' to ' + who;
    case 'reply': return 'Answered ' + who + intent;
    case 'reply_queued': return 'Drafted a reply to ' + who + intent;
    case 'opt_out': return who + ' opted out' + (d.why ? ' · ' + escHtml(d.why) : '');
    case 'opt_out_undone': return 'You undid ' + (who || 'a contact') + '’s opt-out';
    case 'reply_after_opt_out': return who + ' wrote again after opting out · no reply sent';
    case 'escalation': return 'Flagged ' + who + ' for you' + (d.reason ? ' · ' + escHtml(d.reason) : '');
    case 'bounce': return 'Email to ' + who + ' bounced';
    case 'manual_send': return 'You marked a contact as emailed by hand';
    case 'manual_send_undone': return 'You undid a hand-sent mark';
    case 'outbox_edited': return 'You edited a draft';
    case 'conversation_status':
      return d.to === 'closed' ? 'You closed a conversation'
        : d.from === 'needs_human' ? 'You handed a conversation back to Sellbird' : 'You reopened a conversation';
  }
  const text = escHtml(String(a.action_type || '').replace(/_/g, ' '));
  return a.agent === 'main' ? 'Heartbeat: ' + text : text;
}

function emptyState(glyph, title, copy) {
  return '<div class="empty"><div class="glyph">' + glyph + '</div>' +
    '<div class="title">' + title + '</div>' +
    '<div class="copy">' + copy + '</div></div>';
}

function offlineState() {
  return emptyState('&#9888;', 'Dashboard can\'t reach the server',
    'The dashboard process may have stopped. Restart it with <b>sellbird dashboard</b> and refresh this page.');
}

// How many requests in a row never reached the server. A refusal or a 500
// is an answer and doesn't count; only a request that couldn't be sent does.
// The refresh schedule reads this and slows right down, so a dashboard that
// has been stopped is asked once a minute instead of several times a second
// for as long as the tab stays open.
let _transportFailures = 0;

async function api(path, opts) {
  try {
    const r = await authFetch(path, opts);
    _transportFailures = 0;
    if (await lockIfRefused(r)) return null;
    if (!r.ok) {
      // An action the server refused says why ("a run is already going");
      // pass that body on so the toast can show it. Reads stay null.
      if (!opts || !opts.method || opts.method === 'GET') return null;
      const body = await r.json().catch(() => null);
      return body && typeof body === 'object' ? {success: false, ...body} : null;
    }
    return await r.json();
  } catch {
    _transportFailures += 1;
    return null;
  }
}

// ── Locked: no token, a wrong one, or the server can't load its own ──

let _locked = false;

// How this dashboard is unlocked: a token pasted from the terminal, or a
// username and password. /api/instance is public and says which, so a
// refused request can offer the right way in instead of guessing.
let _authMode = null;

async function loadAuthMode() {
  try {
    // authFetch, like everything else here: /api/instance needs no token,
    // but the same-origin guard in authFetch is not worth stepping around.
    const r = await authFetch('/api/instance');
    if (r.ok) _authMode = await r.json();
  } catch { /* null means "offer the token box", which is always safe */ }
  return _authMode;
}

// Come back to the tab we were on, not to Today.
function signInUrl() {
  return '/login?next=' + encodeURIComponent(window.location.pathname + window.location.hash);
}

function goSignIn() { window.location.replace(signInUrl()); }

async function lockIfRefused(r) {
  if (r.status !== 401 && r.status !== 503) {
    if (r.ok && _locked) hideLock();
    return false;
  }
  if (r.status === 401) {
    const mode = _authMode || await loadAuthMode();
    if (mode && mode.login_enabled) {
      // The sign-in ran out or was revoked. Drop the dead token first, or the
      // sign-in page would see it, believe us, and send us straight back.
      _locked = true;
      clearAuthToken();
      goSignIn();
      return true;
    }
  }
  let why = '';
  if (r.status === 503) {
    try { why = ((await r.json()) || {}).message || ''; } catch { /* ignore */ }
  }
  showLock(r.status === 503
    ? 'The dashboard server can\'t use its token file, so it is refusing every request. ' + why
    : authToken()
      ? 'The saved token was not accepted (the dashboard may have a new one).'
      : 'This tab doesn\'t have the dashboard token yet.');
  return true;
}

// Ends this sign-in on the server, so the token is dead everywhere and not
// just forgotten by this tab.
async function signOut() {
  try { await authFetch('/api/auth/logout', {method: 'POST'}); } catch { /* going anyway */ }
  _locked = true;
  clearAuthToken();
  window.location.replace(_authMode && _authMode.login_enabled ? '/login' : '/');
}

// ── Who is signed in ──
//
// On a deployment the password is what stands between the pipeline and the
// internet, so an operator needs to see how many browsers hold a live
// sign-in and be able to end all of them. Nothing here identifies a person:
// a sign-in records when it was made and what browser made it, and the
// surest way to end someone's access is still to change the password, which
// ends every sign-in made under the old one.

async function loadSignIns() {
  const head = document.getElementById('signins-head');
  const card = document.getElementById('signins-card');
  if (!card) return;
  if (!(_authMode && _authMode.login_enabled)) {
    if (head) head.style.display = 'none';
    card.innerHTML = '';
    return;
  }
  const data = await api('/api/auth/sessions');
  if (!data) { card.innerHTML = ''; return false; }
  if (head) head.style.display = '';
  const rows = data.sessions || [];
  const hours = n => Math.round(Number(n || 0) / 3600);
  card.innerHTML = '<div class="card"><div class="settings-row"><div>' +
    '<h3>' + rows.length + (rows.length === 1 ? ' sign-in' : ' sign-ins') + '</h3>' +
    '<p class="desc">Each one lasts ' + hours(data.max_age) + ' hours, or ' +
    hours(data.idle_timeout) + ' hours unused. Changing the dashboard password ends ' +
    'every sign-in made under the old one, so that is how you take someone\'s access away.</p>' +
    (data.warning ? '<div class="notice bad">' + escHtml(data.warning) + '</div>' : '') +
    (rows.length ? '<div class="signin-list">' + rows.map(r =>
      '<div class="signin"><span class="name">' +
        escHtml(r.label || 'a browser') + '</span>' +
        '<span class="when">signed in ' + relTime(tsIso(r.created_at)) + '</span>' +
        '<span class="when">last used ' + relTime(tsIso(r.last_seen_at)) + '</span>' +
      '</div>').join('') + '</div>' : '') +
    '</div><div class="fields">' +
    (rows.length ? '<button class="btn btn-secondary btn-sm" onclick="signOutEveryone()">' +
      'Sign everyone out</button>' : '') +
    '</div></div></div>';
}

// Seconds-since-epoch to something relTime() understands.
function tsIso(seconds) {
  const n = Number(seconds || 0);
  return n > 0 ? new Date(n * 1000).toISOString() : '';
}

async function signOutEveryone() {
  if (!confirm('Sign every browser out, including this one?')) return;
  const res = await api('/api/auth/sessions', {method: 'DELETE'});
  if (res && res.success) {
    showToast('Signed out ' + res.ended + (res.ended === 1 ? ' browser' : ' browsers'), 'success');
    clearAuthToken();
    _locked = true;
    window.location.replace('/login');
  } else {
    showToast(failMsg(res, 'Could not sign everyone out.'), 'error');
  }
}

// The Sign out button belongs to sign-ins only: a token pasted from the
// terminal has nothing on the server to end.
async function loadSignedInAs() {
  const el = document.getElementById('sign-out');
  if (!el) return;
  const me = await api('/api/auth/me');
  el.style.display = me && me.kind === 'session' ? '' : 'none';
}

function showLock(reason) {
  _locked = true;
  const el = document.getElementById('lock-screen');
  if (!el) return;
  document.getElementById('lock-reason').textContent = reason;
  const byPassword = !!(_authMode && _authMode.login_enabled);
  const tokenWay = document.getElementById('lock-token-way');
  const loginWay = document.getElementById('lock-login-way');
  if (tokenWay) tokenWay.style.display = byPassword ? 'none' : '';
  if (loginWay) loginWay.style.display = byPassword ? '' : 'none';
  el.style.display = 'flex';
  const dot = document.getElementById('header-dot');
  const text = document.getElementById('header-status-text');
  if (dot) dot.className = 'status-dot offline';
  if (text) text.textContent = 'Locked';
}

function hideLock() {
  _locked = false;
  const el = document.getElementById('lock-screen');
  if (el) el.style.display = 'none';
}

function unlockWithToken(event) {
  event.preventDefault();
  const input = document.getElementById('lock-token');
  const value = (input.value || '').trim();
  input.value = '';
  if (!setAuthToken(value)) {
    document.getElementById('lock-reason').textContent =
      'That doesn\'t look like a dashboard token. Copy it again from the link or data/dashboard-token.';
    return;
  }
  hideLock();
  loadCurrentTab();
  loadSellbirdStatus();
}

// Downloads go through the same authorised fetch and are handed to the
// browser as a local blob, so no download URL ever carries the token.
async function downloadCsv(path, filename, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await authFetch(path);
    if (await lockIfRefused(r)) return;
    if (!r.ok) { showToast('Export failed.', 'error'); return; }
    const href = URL.createObjectURL(await r.blob());
    const a = document.createElement('a');
    a.href = href;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 10000);
  } catch {
    showToast('Export failed — is the dashboard still running?', 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function showToast(msg, type) {
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2600);
}

function toggleVisibility(inputId) {
  const el = document.getElementById(inputId);
  el.type = el.type === 'password' ? 'text' : 'password';
}

// ── Tabs, as real routes ──
//
// Every tab has a URL: #/outbox can be bookmarked or pasted to someone,
// Back and Forward move between the tabs you visited instead of leaving the
// dashboard, and a reload keeps your place. A hash and not a path, so this
// stays one static page the server never has to route.

const TABS = ['today', 'company', 'outbox', 'inboxes', 'conversations',
              'signals', 'discover', 'companies', 'imports', 'prospects',
              'campaigns', 'activity', 'usage', 'controls', 'settings', 'help'];

// Where the internal id would read oddly in a link, the route says what the
// tab is called in the sidebar. Both spellings are understood on the way in.
const ROUTE_NAMES = {prospects: 'contacts', discover: 'apollo', company: 'your-company'};
const TAB_NAMES = {};
for (const id of TABS) {
  TAB_NAMES[id] = id;
  if (ROUTE_NAMES[id]) TAB_NAMES[ROUTE_NAMES[id]] = id;
}

function routeOf(id) { return own(ROUTE_NAMES, id) || id; }

// The tab a hash asks for, or '' for anything unrecognised.
function tabFromHash(hash) {
  const name = String(hash || '').replace(/^#\/?/, '').split(/[?&#]/)[0].toLowerCase();
  return own(TAB_NAMES, name) || '';
}

function navButton(id) {
  return document.querySelector('nav button[data-tab="' + id + '"]');
}

function tabLabel(id) {
  const btn = navButton(id);
  const label = btn && btn.querySelector('.label');
  return label ? label.textContent.trim() : '';
}

// opts.fromRouter: the URL already says this, don't touch history.
// opts.replace:    replace the current entry instead of adding one.
function showTab(id, btn, opts) {
  if (!own(TAB_NAMES, id)) id = 'today';
  currentTab = id;
  if (id === 'companies') companyDrill = false;
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('nav button[data-tab]').forEach(b => b.classList.remove('active'));
  const section = document.getElementById(id);
  if (section) section.classList.add('active');
  const button = btn || navButton(id);
  if (button) button.classList.add('active');
  const label = tabLabel(id);
  document.title = label ? label + ' — Sellbird' : 'Sellbird';
  if (!(opts && opts.fromRouter)) setRoute(id, opts && opts.replace);
  loadCurrentTab();
}

function setRoute(id, replace) {
  const target = '#/' + routeOf(id);
  if (window.location.hash === target) return;
  try {
    const state = {tab: id};
    if (replace) history.replaceState(state, '', target);
    else history.pushState(state, '', target);
  } catch {
    window.location.hash = target;   // private modes that refuse the History API
  }
}

// Jump to a tab from a link that isn't itself a nav button.
function goTab(id) { showTab(id); }

// Back and Forward: the URL is the truth, so never write to history here.
window.addEventListener('popstate', () => {
  const id = tabFromHash(window.location.hash) || 'today';
  showTab(id, null, {fromRouter: true});
});

// One listener for the whole sidebar: a nav button carries only its data-tab,
// so nothing has to keep an inline handler and a selector in step.
document.addEventListener('click', e => {
  const btn = e.target.closest && e.target.closest('nav button[data-tab]');
  if (btn) showTab(btn.dataset.tab, btn);
});

// Open on whatever the URL asks for, replacing the entry the browser already
// has, so the first Back leaves the dashboard rather than bouncing in place.
function startRouting() {
  showTab(tabFromHash(window.location.hash) || 'today', null, {replace: true});
}

function loadCurrentTab() {
  switch (currentTab) {
    case 'today': loadLiveStatus(); loadRenameNotice(); loadToday(); loadTrend(); loadFunnel(); loadSetupStatus(); loadRuns(); loadTodayActivity(); break;
    case 'signals': loadSignals(); break;
    case 'discover': loadApollo(); break;
    case 'companies': if (!companyDrill) loadCompanies(); break;
    case 'imports': loadImports(); break;
    case 'prospects': loadProspects(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'outbox': loadOutbox(); break;
    case 'conversations': loadConversations(); break;
    case 'activity': loadActivity(); break;
    case 'inboxes': loadInboxes(); break;
    case 'usage': loadUsage(); break;
    case 'settings': loadSettings(); loadSendingModeCard(); loadHours(); loadSignIns(); break;
    case 'company': loadCompany(); break;
    case 'controls': loadSellbirdStatus(); loadLogs(); break;
  }
}

// ── Today: what needs a human ──

async function loadToday() {
  const data = await api('/api/today');
  const el = document.getElementById('today-queue');
  if (!data) { el.innerHTML = offlineState(); return; }
  if (data.error) {
    // A failed check must never read as "Nothing needs you".
    el.innerHTML = emptyState(icon('alert'), 'Couldn\'t check what needs you',
      'The dashboard hit an error reading Sellbird\'s data: ' + escHtml(data.error));
    return;
  }

  navCount('nav-today', (data.items || []).filter(i => i.tone !== 'good').length);
  navCount('nav-outbox', (data.stats || {}).outbox_pending || 0);
  navCount('nav-signals', (data.stats || {}).signals_proposed || 0);
  navCount('nav-conversations', (data.stats || {}).needs_human || 0);
  _todayStats = data.stats || {};
  renderFigures(_todayStats);

  const items = data.items || [];
  if (!items.length) {
    el.innerHTML = '<div class="queue-clear">' +
      '<div class="queue-icon">' + icon('check') + '</div><div>' +
      '<div class="qtitle">Nothing needs you</div>' +
      '<div class="qdetail">Sellbird has everything it needs. Anything that requires a ' +
      'decision — an email to approve, a signal to confirm — shows up here.</div>' +
      '</div></div>';
    return;
  }

  const glyph = {bad: 'alert', warn: 'flag', good: 'check'};
  el.innerHTML = '<div class="queue">' + items.map(it => {
    const tone = escHtml(it.tone || 'warn');
    return '<div class="queue-item ' + tone + '">' +
      '<div class="queue-icon">' + icon(glyph[it.tone] || 'flag') + '</div>' +
      '<div class="qtext">' +
        '<div class="qtitle">' + escHtml(it.title) + '</div>' +
        '<div class="qdetail">' + escHtml(it.detail) + '</div>' +
      '</div>' +
      '<button class="btn btn-secondary btn-sm" onclick="goTab(' + jsq(it.tab) + ')">' +
        escHtml(it.action) + icon('arrow') + '</button>' +
    '</div>';
  }).join('') + '</div>';
}

// ── Live status: the "what's happening?" answer, kept on screen ──

async function loadLiveStatus() {
  const el = document.getElementById('live-status');
  if (!el) return;
  const d = await api('/api/live-status');
  if (!d || !Array.isArray(d.steps)) {
    // Keep the last good numbers on screen, but never let them pass for live.
    const flag = el.querySelector('.live-flag');
    if (flag) flag.textContent = "Can't reach Sellbird right now — numbers below may be old.";
    else el.innerHTML = '<p class="muted live-flag">Can\'t reach Sellbird right now. ' +
      'Is the dashboard window still open?</p>';
    return;
  }
  const glyph = {done: icon('check'), running: '<span class="pulse"></span>', partial: '', waiting: ''};
  const fill = {done: 'var(--good)', running: 'var(--accent)', partial: 'var(--accent)', waiting: 'var(--line-strong)'};
  const rows = d.steps.map(s => {
    const st = Object.prototype.hasOwnProperty.call(glyph, s.state) ? s.state : 'waiting';
    const pct = s.total ? Math.min(100, Math.round(100 * s.done / s.total)) : 0;
    const bar = s.total
      ? '<div class="bar"><span style="width:' + pct + '%;background:' + fill[st] + '"></span></div>'
      : '';
    return '<div class="step">' +
      '<span class="step-icon ' + st + '">' + glyph[st] + '</span>' +
      '<span class="step-label">' + escHtml(s.label) + '</span>' +
      '<span class="step-detail">' + escHtml(s.detail) + '</span>' + bar +
    '</div>';
  }).join('');
  const jobName = r => r.stage === 'profile' ? 'Reading websites'
    : r.stage === 'discover' ? 'Searching for businesses (' + (r.provider || '') + ')'
    : String(r.stage || 'Job');
  const running = d.running || [];
  const runningHtml = running.length
    ? running.map(r => '<span class="live-now"><span class="pulse"></span>' + escHtml(jobName(r)) +
        ' · ' + r.elapsed_min + ' min' + (r.eta_text ? ' · ' + escHtml(r.eta_text) : '') + '</span>').join('')
    : '<span class="live-now">Nothing running right now</span>';
  const stoppedHtml = (d.stopped || []).length
    ? '<div class="banner bad" style="margin:8px 0 0">' + icon('alert') + '<p style="color:inherit">' +
      (d.stopped || []).map(r => escHtml(jobName(r))).join(', ') + ' stopped before finishing.</p></div>'
    : '';
  // Whether anything is going to carry out the advice below. Without this,
  // a pipeline nobody is working reads exactly like one that is: the steps
  // sit at zero and "Next" names a job no agent has ever started.
  const blocked = d.blocked || [];
  const blockedHtml = blocked.length
    ? '<div class="banner bad" style="margin:8px 0 0">' + icon('alert') +
      '<p style="color:inherit"><b>Sellbird can\'t run: ' + escHtml(blocked[0].title.toLowerCase()) +
      '.</b> ' + escHtml(blocked[0].detail) + '</p>' +
      (blocked[0].tab ? '<button class="btn btn-secondary btn-sm" onclick="goTab(' +
        jsq(blocked[0].tab) + ')">Fix it</button>' : '') + '</div>'
    : d.paused_reason
      ? '<div class="banner wait" style="margin:8px 0 0">' + icon('alert') +
        '<p style="color:inherit"><b>Sellbird has paused itself.</b> ' +
        escHtml(d.paused_reason) + '</p></div>'
      : d.loop_running === false
        ? '<div class="banner wait" style="margin:8px 0 0">' + icon('alert') +
          '<p style="color:inherit">Sellbird isn\'t running, so nothing below moves on its own.</p>' +
          '<button class="btn btn-secondary btn-sm" onclick="goTab(\'controls\')">Start it</button></div>'
        : '';
  el.innerHTML =
    '<div class="live-head">' + runningHtml +
      '<span class="setup-pct">Cost so far $' + Number(d.cost_usd || 0).toFixed(2) + '</span></div>' +
    blockedHtml + stoppedHtml +
    '<div class="steps">' + rows + '</div>' +
    (d.next ? '<div class="live-next">' + icon('arrow') + '<span><b>Next</b> · ' + escHtml(d.next) + '</span>' +
      (/^Write first emails/.test(d.next) && !d.loop_running && !blocked.length
        ? writeNowButton()
        : d.next_tab ? '<button class="btn btn-secondary btn-sm" onclick="goTab(' + jsq(d.next_tab) + ')">Go</button>' : '') +
      '</div>' : '') +
    '<div class="live-flag">Updated ' + new Date().toLocaleTimeString() + ' · refreshes every 10 seconds</div>';
}


// Writing the first emails by hand, for the hours before anyone starts the
// loop. Same agent and same rules as the heartbeat's: deliverable addresses
// only, and every draft lands in the Outbox for approval. Nothing is sent.
let _writePoll = null;
let _writeNote = {text: '', tone: ''};

function writeNowButton(disabled) {
  return '<button class="btn btn-primary btn-sm" id="btn-write-now" onclick="writeNow()"' +
    (disabled ? ' disabled' : '') + '>Write them now</button>' +
    '<span id="write-now-note" class="' + (_writeNote.tone || 'muted') + '">' +
    (_writeNote.text ? ' ' + escHtml(_writeNote.text) : '') + '</span>';
}

function writeNote(text, tone) {
  _writeNote = {text: text || '', tone: tone || 'muted'};
  const el = document.getElementById('write-now-note');
  if (el) {
    el.className = _writeNote.tone;
    el.textContent = _writeNote.text ? ' ' + _writeNote.text : '';
  }
}

async function writeNow() {
  const btn = document.getElementById('btn-write-now');
  if (btn) btn.disabled = true;
  writeNote('Writing. One email at a time, so this takes a minute.');
  const data = await api('/api/writer/run', {method: 'POST'});
  if (!data || !data.success) {
    writeNote((data && data.message) || 'Could not start writing.', 'bad-text');
    if (btn) btn.disabled = false;
    return;
  }
  renderWriteNow();
}

// Picks up a pass already going, so switching tabs doesn't lose it.
async function renderWriteNow() {
  if (_writePoll) { clearInterval(_writePoll); _writePoll = null; }
  const tick = async () => {
    const d = await api('/api/writer/run');
    if (!d) return;
    const btn = document.getElementById('btn-write-now');
    if (d.running) {
      if (btn) btn.disabled = true;
      writeNote('Writing. One email at a time, so this takes a minute.');
      return;
    }
    clearInterval(_writePoll); _writePoll = null;
    if (btn) btn.disabled = false;
    const r = d.report;
    if (!r) { writeNote(''); return; }
    if (r.error) { writeNote(r.error, 'bad-text'); return; }
    if (!r.campaigns) {
      writeNote('Nothing to write for. A contact needs a verified or catch-all address.');
      return;
    }
    showToast(r.campaigns + ' campaign(s) written. The drafts are in the Outbox.', 'success');
    writeNote('');
    if (currentTab === 'campaigns') loadCampaigns();
    if (currentTab === 'today') { loadLiveStatus(); loadToday(); }
  };
  await tick();
  if (!_writePoll) _writePoll = setInterval(tick, 3000);
}

// KPI cards. Totals come from /api/today; the 14-day bars from /api/trend.
function renderFigures(stats) {
  const el = document.getElementById('today-kpis');
  if (!el) return;
  const t = _trend || {};
  const sum = a => (a || []).reduce((x, y) => x + (Number(y) || 0), 0);
  const last7 = a => sum((a || []).slice(-7));
  const card = (label, value, spark, note) =>
    '<div class="kpi"><div class="kpi-label">' + label + '</div>' +
    '<div class="kpi-main"><span class="kpi-value' + (value ? '' : ' zero') + '">' + value + '</span>' + spark + '</div>' +
    '<span class="kpi-note">' + note + '</span></div>';

  // Instantly sends from its own servers, so the Outbox never sees them.
  const sent = stats.email_provider === 'instantly'
    ? card('Emails sent', '&mdash;', '', 'Instantly tracks sends')
    : card('Emails sent', sum(t.sent), sparkBars(t.sent, 'Emails sent per day, last 14 days'),
           'last 14 days · <b>' + (stats.outbox_approved || 0) + '</b> scheduled');
  const waiting = (stats.outbox_pending || 0) + (stats.needs_human || 0);
  const waitNote = [
    stats.outbox_pending ? stats.outbox_pending + ' to approve' : '',
    stats.needs_human ? stats.needs_human + ' handed to you' : '',
  ].filter(Boolean).join(' · ') || 'nothing to approve';
  const facts = document.getElementById('today-facts');
  if (facts) facts.innerHTML =
    '<span><b>' + (stats.companies || 0) + '</b> companies</span>' +
    '<span><b>' + (stats.unprofiled || 0) + '</b> sites not read yet</span>' +
    '<span><b>' + (stats.signals_confirmed || 0) + '</b> signals on</span>';

  el.innerHTML = '<div class="kpis">' +
    card('Contacts found', stats.prospects || 0, sparkBars(t.found, 'Contacts found per day, last 14 days'),
         '<b>+' + last7(t.found) + '</b> this week') +
    sent +
    card('Conversations', stats.open_conversations || 0, sparkBars(t.replies, 'New replies per day, last 14 days'),
         '<b>' + sum(t.replies) + '</b> new in 14 days') +
    card('Waiting on you', waiting, '', waitNote) +
  '</div>';
}

let _todayStats = {};
let _trend = null;

async function loadTrend() {
  const t = await api('/api/trend?days=14&tz=' + -new Date().getTimezoneOffset());
  if (t && Array.isArray(t.days)) { _trend = t; renderFigures(_todayStats); }
}

// Contacts by stage: one bar, one legend, from /api/stats.
async function loadFunnel() {
  const el = document.getElementById('today-figures');
  if (!el) return;
  const data = await api('/api/stats');
  if (!data) return;  // offline: keep the last numbers; Today's queue says so
  if (data.error) { el.innerHTML = '<p class="muted" style="font-size:13px">Couldn\'t read the pipeline.</p>'; return; }
  const total = (data.prospects || {}).total || 0;
  const pipe = data.pipeline || {};
  const stages = [
    ['To write', pipe.to_write || 0, 'c-accent-soft'],
    ['Contacted', pipe.contacted || 0, 'c-accent'],
    ['Replied', pipe.replied || 0, 'c-active'],
    ['Interested', pipe.interested || 0, 'c-good'],
    ['Ready to meet', pipe.ready_to_meet || 0, 'c-note'],
    ['Closed out', pipe.closed_out || 0, 'c-idle'],
  ];
  if (!total) {
    el.innerHTML = '<p class="muted" style="font-size:13px">No contacts yet. Scout adds them as it finds people.</p>';
    return;
  }
  el.innerHTML =
    '<div class="funnel-bar" role="img" aria-label="Contacts by stage">' +
      stages.filter(s => s[1] > 0).map(s =>
        '<span class="seg ' + s[2] + '" style="flex:' + s[1] + '" title="' + s[0] + ': ' + s[1] + '"></span>').join('') +
    '</div>' +
    '<div class="legend-rows">' + stages.map(s =>
      '<div class="legend-row"><span class="legend-key"><i class="' + s[2] + '"></i>' + s[0] + '</span>' +
      '<span class="legend-val">' + s[1] + '</span>' +
      '<span class="legend-pct">' + Math.round(100 * s[1] / total) + '%</span></div>'
    ).join('') + '</div>';
}

async function loadTodayActivity() {
  const el = document.getElementById('today-activity');
  if (!el) return;
  const data = await api('/api/activity');
  if (!Array.isArray(data)) return;  // offline: Today's queue says so
  // Heartbeats are noise here; the Activity tab keeps them.
  const shown = data.filter(a => a.agent !== 'main').slice(0, 8);
  if (!shown.length) {
    el.innerHTML = '<p class="muted" style="font-size:13px">' +
      'Nothing yet. Every action Sellbird takes shows up here.</p>';
    return;
  }
  el.innerHTML = '<div class="timeline">' + shown.map(a =>
    '<div class="tl-item">' +
      '<span class="tl-time">' + relTime(a.created_at) + '</span>' +
      '<span class="tl-icon">' + icon(own(AGENT_ICONS, a.agent) || 'activity') + '</span>' +
      '<span class="tl-text">' + actionText(a) + '</span>' +
      '<span class="tl-agent">' + escHtml(a.agent) + '</span>' +
    '</div>').join('') + '</div>';
}

function navCount(id, n) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = n > 0 ? n : '';
  el.className = 'nav-count' + (n > 0 ? ' on' : '');
}

async function loadRuns() {
  const el = document.getElementById('today-runs');
  const block = document.getElementById('today-runs-block');
  if (!el) return;
  const runs = await api('/api/runs');
  if (!Array.isArray(runs) || !runs.length) {
    if (block) block.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  if (block) block.style.display = '';
  el.innerHTML = '<div class="runs">' + runs.slice(0, 5).map(r =>
    '<div class="run">' +
      '<span class="name">' + escHtml(r.stage) + '</span>' + badge(r.status) +
      '<span class="when">' + relTime(r.started_at) + '</span>' +
      '<span class="stats">' + (r.records || 0) + ' records · ' +
        (r.cost_usd ? '$' + Number(r.cost_usd).toFixed(4) : 'free') + '</span>' +
    '</div>').join('') + '</div>';
}

// ── Setup checklist (lives on Today, and disappears once it's done) ──

// Shown once after an upgrade from Harvey moved this install's files.
async function loadRenameNotice() {
  const el = document.getElementById('today-renamed');
  if (!el) return;
  const data = await api('/api/rename-notice');
  if (!data || !data.show) { el.innerHTML = ''; return; }
  el.innerHTML =
    '<div class="card rename-card">' +
      '<div><h3>Harvey is now Sellbird</h3>' +
        '<p class="muted">Same project, new name. Your settings, data and sign-in came with you. ' +
        'The <code>harvey</code> command still works for now; use <code>sellbird</code> from here on.</p></div>' +
      '<div class="rename-actions">' +
        '<a class="btn btn-ghost" href="https://github.com/ethanplusai/sellbird#upgrading-from-harvey" target="_blank" rel="noopener">What changed</a>' +
        '<button class="btn btn-secondary" onclick="dismissRenameNotice()">Got it</button>' +
      '</div></div>';
}

async function dismissRenameNotice() {
  document.getElementById('today-renamed').innerHTML = '';
  await api('/api/rename-notice/dismiss', {method: 'POST'});
}

async function loadSetupStatus() {
  const el = document.getElementById('today-setup');
  if (!el) return;
  const data = await api('/api/setup-status');
  if (!data || !data.checks) { el.innerHTML = ''; return; }

  const pct = data.percent || 0;
  // Finished setup disappears completely rather than greeting you forever.
  if (pct === 100) { el.innerHTML = ''; return; }

  // The first unfinished required step is the one to do now.
  const next = data.checks.find(c => c.required && !c.done);
  const renderCheck = (c, optional) =>
    '<div class="check-item' + (c === next ? ' current' : '') + '">' +
      (c.done ? '<span class="check-icon done">' + icon('check') + '</span>'
              : '<span class="check-icon pending"></span>') +
      '<div class="check-info">' +
        '<div class="check-label ' + (c.done ? 'done' : '') + '">' + escHtml(c.label) +
          (optional ? '<span class="optional-tag">optional</span>' : '') + '</div>' +
        (!c.done ? '<div class="check-help">' + escHtml(c.help) + '</div>' : '') +
        (!c.done && c.id === 'warmup_service' ? warmupServiceActions(c) : '') +
      '</div></div>';

  const required = data.checks.filter(c => c.required);
  const optional = data.checks.filter(c => !c.required);
  const done = required.filter(c => c.done).length;

  el.innerHTML =
    '<div class="card setup-card">' +
      '<div class="setup-head"><div><h3>Finish setting up Sellbird</h3>' +
        '<p class="muted">Sellbird starts finding prospects and sending once these are done.</p></div>' +
        '<span class="setup-pct">' + done + ' of ' + required.length + ' done</span></div>' +
      '<div class="bar thick" style="margin-bottom:8px"><span style="width:' + pct + '%;background:var(--accent)"></span></div>' +
      required.map(c => renderCheck(c, false)).join('') +
      (optional.length
        ? '<details style="margin-top:10px"><summary class="muted" ' +
          'style="font-size:12px">' + optional.length + ' optional</summary>' +
          '<div style="margin-top:6px">' +
          optional.map(c => renderCheck(c, true)).join('') + '</div></details>'
        : '') +
    '</div>';
}

// The warmup step: get TrulyInbox, paste its key, or say you use another service.
function warmupServiceActions(c) {
  return '<div class="btn-group warmup-actions">' +
      '<a class="btn btn-primary btn-sm" href="' + safeHref(c.link) + '" target="_blank" ' +
        'rel="noopener sponsored">Get TrulyInbox</a>' +
      '<input class="form-input" id="trulyinbox-key" type="password" autocomplete="off" ' +
        'placeholder="Paste your TrulyInbox API key" aria-label="TrulyInbox API key" ' +
        'onkeydown="if (event.key === \'Enter\') connectTrulyInbox()">' +
      '<button class="btn btn-secondary btn-sm" id="trulyinbox-go" onclick="connectTrulyInbox()">Connect</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="useOtherWarmup()">I use another service</button>' +
    '</div>';
}

async function connectTrulyInbox() {
  const input = document.getElementById('trulyinbox-key');
  const go = document.getElementById('trulyinbox-go');
  if (!input || !input.value.trim()) return;
  if (go) go.disabled = true;
  const data = await api('/api/trulyinbox/connect', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key: input.value.trim()}),
  });
  if (go) go.disabled = false;
  if (data && data.success) {
    input.value = '';
    showToast(data.message || 'TrulyInbox connected', 'success');
    loadSetupStatus();
  } else {
    showToast((data && data.message) || 'Could not connect TrulyInbox', 'error');
  }
}

async function useOtherWarmup() {
  const data = await api('/api/trulyinbox/other', {method: 'POST'});
  if (data && data.success) loadSetupStatus();
}

// ── Signals: Sellbird proposes, you confirm ──

async function loadSignals() {
  const data = await api('/api/signals');
  const sumEl = document.getElementById('signals-summary');
  const grpEl = document.getElementById('signals-groups');
  if (!data || data.error) { grpEl.innerHTML = offlineState(); sumEl.innerHTML = ''; return; }
  _signals = data;

  const s = data.summary || {};
  navCount('nav-signals', s.proposed || 0);
  const parts = [['Confirmed', s.confirmed || 0, 'c-good'], ['Waiting on you', s.proposed || 0, 'c-wait'],
                 ['Turned off', s.rejected || 0, 'c-idle']];
  const all = parts.reduce((x, p) => x + p[1], 0) || 1;
  sumEl.innerHTML = '<div class="card sig-summary"><div class="funnel-bar">' +
    parts.filter(p => p[1]).map(p => '<span class="seg ' + p[2] + '" style="flex:' + p[1] + '"></span>').join('') +
    '</div><div class="funnel-legend">' + parts.map(p =>
      '<div class="legend-item"><span class="legend-key"><i class="' + p[2] + '"></i>' + p[0] + '</span>' +
      '<span class="legend-val">' + p[1] + '<small>' + Math.round(100 * p[1] / all) + '%</small></span></div>'
    ).join('') + '</div></div>';

  grpEl.innerHTML = (data.groups || []).map(g => {
    // Only the undecided ones: "Confirm all" must never re-enable a signal
    // you turned off (or "Skip all" turn off one you confirmed).
    const pending = escHtml(JSON.stringify(g.signals.filter(x => x.status === 'proposed').map(x => x.code)));
    const undecided = g.signals.filter(x => x.status === 'proposed').length;
    return '<div class="sig-group">' +
      '<div class="sig-group-head"><div>' +
        '<h3>' + escHtml(g.label) + '</h3><p>' + escHtml(g.blurb) + '</p>' +
      '</div>' +
      (undecided
        ? '<div style="display:flex;gap:6px;flex-shrink:0">' +
            '<button class="btn btn-primary btn-sm" onclick="setSignals(' + pending +
              ', \'confirmed\')">Confirm all ' + undecided + '</button>' +
            '<button class="btn btn-secondary btn-sm" onclick="setSignals(' + pending +
              ', \'rejected\')">Skip all ' + undecided + '</button>' +
          '</div>'
        : '') +
      '</div>' +
      '<div class="card flush">' + g.signals.map(sigRow).join('') + '</div>' +
    '</div>';
  }).join('');

  renderCohortBuilder();
}

function sigRow(sig) {
  const free = /free|included/i.test(sig.cost_note || '');
  const costCls = free ? 'free' : (/\$/.test(sig.cost_note || '') ? 'paid' : '');
  const seen = sig.companies
    ? '<span class="cost-chip">seen on ' + sig.companies +
      (sig.companies === 1 ? ' company' : ' companies') + ' so far</span>' : '';
  const floor = sig.confidence_floor > 0
    ? '<span class="cost-chip">only recorded above ' +
      Math.round(sig.confidence_floor * 100) + '% confidence</span>' : '';

  const decide = sig.status === 'confirmed'
    ? '<button class="btn btn-secondary btn-sm" onclick="setSignals([' + jsq(sig.code) +
        '],\'rejected\')">Turn off</button>'
    : sig.status === 'rejected'
      ? '<button class="btn btn-secondary btn-sm" onclick="setSignals([' + jsq(sig.code) +
          '],\'confirmed\')">Turn on</button>'
      : '<button class="btn btn-primary btn-sm" onclick="setSignals([' + jsq(sig.code) +
          '],\'confirmed\')">Confirm</button>' +
        '<button class="btn btn-secondary btn-sm" onclick="setSignals([' + jsq(sig.code) +
          '],\'rejected\')">Skip</button>';

  return '<div class="sig-row ' + (sig.status === 'rejected' ? 'rejected' : '') + '">' +
    '<div class="sig-main">' +
      '<div class="sig-label">' + escHtml(sig.label || sig.code) +
        '<span class="sig-code">' + escHtml(sig.code) + '</span></div>' +
      '<div class="sig-desc">' + escHtml(sig.description) + '</div>' +
      '<div class="sig-meta">' +
        '<span class="cost-chip ' + costCls + '">' + escHtml(sig.cost_note || 'cost unknown') + '</span>' +
        floor + seen + badge(sig.status) +
      '</div>' +
    '</div>' +
    '<div class="sig-decide">' + decide + '</div>' +
  '</div>';
}

async function setSignals(codes, status) {
  const data = await api('/api/signals/status', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({codes: codes, status: status}),
  });
  if (data && data.success) {
    showToast(
      status === 'confirmed'
        ? 'Confirmed ' + data.changed + ' signal' + (data.changed === 1 ? '' : 's') +
          ' — Sellbird will collect ' + (data.changed === 1 ? 'it' : 'them') + ' from now on.'
        : data.changed + ' signal' + (data.changed === 1 ? '' : 's') + ' turned off.',
      'success');
  } else {
    showToast(failMsg(data, 'Could not update that signal.'), 'error');
  }
  loadSignals();
}

// ── Apollo: who exists, found free, priced before it is bought ──
//
// Apollo's people search costs nothing and withholds the addresses, so the
// list can be looked at and cut down before a credit is spent. Candidates
// land on the Imports tab, where buying their addresses is priced and capped.

let _apolloPoll = null;

async function loadApollo() {
  const data = await api('/api/apollo/segments');
  const el = document.getElementById('apollo-segments');
  if (!data || data.error) {
    el.innerHTML = offlineState();
    return;
  }
  renderApolloLedger(data);

  const pick = document.getElementById('apollo-segment');
  if (pick && !pick.options.length) {
    pick.innerHTML = '<option value="mix">Every segment, in rotation</option>' +
      (data.segments || []).map(s =>
        '<option value="' + escHtml(s.key) + '">' + escHtml(s.label || s.key) + '</option>').join('');
  }
  const limit = document.getElementById('apollo-limit');
  if (limit && !limit.dataset.set) {
    limit.value = data.per_run_credits || 25;
    limit.dataset.set = '1';
  }

  if (!(data.segments || []).length) {
    el.innerHTML = emptyState(icon('discover'), 'No segments yet',
      'Who Apollo is asked about lives in <b>segments.yaml</b>: titles, locations and company size per segment.');
    return;
  }
  el.innerHTML = '<div class="inbox-help">' + data.segments.map(s =>
    '<div class="inset"><h4>' + escHtml(s.label || s.key) + '</h4>' +
    '<p class="desc">' + escHtml((s.locations || []).join(', ')) +
    (s.employees && s.employees.length ? ' &middot; ' + escHtml(staffRange(s.employees)) : '') +
    ' &middot; ' + Number(s.titles) + ' titles</p>' +
    '<p class="desc"><b>' + Number(s.fetched) + '</b> contacts fetched so far' +
    (Number(s.with_email) ? ', ' + Number(s.with_email) + ' with an address' : '') +
    '</p></div>').join('') + '</div>';

  loadApolloRuns();
}

// Every run this workspace has made, free searches included. The rows come
// from the database rather than from exports/plan.csv, which the next plan
// run replaces.
async function loadApolloRuns() {
  const el = document.getElementById('apollo-runs');
  if (!el) return;
  const data = await api('/api/apollo/runs?limit=20');
  const runs = (data && data.runs) || [];
  if (!runs.length) {
    el.innerHTML = '<div class="card"><p class="desc">No Apollo runs yet. ' +
      'Every search, paid or free, is recorded here once you make one.</p></div>';
    return;
  }
  el.innerHTML = '<table class="table"><thead><tr><th>When</th><th>Segment</th>' +
    '<th>What it did</th><th>Searched</th><th>Contacts</th><th>Credits</th>' +
    '</tr></thead><tbody>' + runs.map(r =>
      '<tr><td>' + escHtml(String(r.ran_at || '').slice(0, 16)) + '</td>' +
      '<td>' + escHtml(r.segment || 'every segment') + '</td>' +
      '<td>' + escHtml(runMode(r.mode)) + '</td>' +
      '<td>' + Number(r.searched || 0) + '</td>' +
      '<td>' + Number(r.contacts || 0) + '</td>' +
      '<td>' + (Number(r.credits_used) ? '<b>' + Number(r.credits_used) + '</b>'
                                       : 'free') + '</td></tr>').join('') +
    '</tbody></table>';
}

function runMode(mode) {
  return {fetch: 'Bought addresses', plan: 'Free search',
          dry: 'Dry run', adopted: 'Brought in from the old files'}[mode] || mode || '';
}

// Nothing here calls Apollo. It reads the two contact files into the
// database and writes them again from it, which is both the repair after a
// redeploy and how judgement written into the CSV becomes durable.
async function syncApollo() {
  const out = document.getElementById('apollo-result');
  if (out) out.innerHTML = '<div class="notice wait">Checking the record. This spends nothing.</div>';
  const data = await api('/api/apollo/sync', {method: 'POST'});
  if (out) {
    out.innerHTML = data && data.success
      ? '<div class="notice good">' + escHtml(data.message) + '</div>'
      : '<div class="notice bad">' + escHtml((data && data.message) ||
          'The record could not be checked.') + '</div>';
  }
  loadApollo();
}

// Apollo writes a headcount band as "200,5000". Say it the way a person would.
function staffRange(bands) {
  return bands.map(band => String(band).split(',')
    .map(n => Number(n).toLocaleString()).join(' to ')).join(', ') + ' staff';
}

function renderApolloLedger(data) {
  const el = document.getElementById('apollo-ledger');
  if (!el) return;
  const cap = Number(data.phase_credit_cap) || 0;
  el.innerHTML = '<div class="inbox-summary">' +
    '<span><b>' + Number(data.stored_contacts || 0) + '</b> contacts kept' +
    (Number(data.stored_with_email) ? ', ' + Number(data.stored_with_email) +
      ' with an address' : '') + '</span>' +
    '<span><b>' + Number(data.credits_used) + '</b> credits spent, all time' +
    (cap ? ' of <b>' + cap + '</b>' : '') + '</span>' +
    '<span><b>' + Number(data.credits_today) + '</b> today</span>' +
    '<span><b>' + Number(data.people_seen) + '</b> people seen, never bought twice</span>' +
    '<span>' + (data.key_set ? 'Key set' : '<b>No API key yet</b>') + '</span></div>' +
    '<p class="desc">Every contact and every credit above is kept in the database on ' +
      'the data volume, not in the export files, so a redeploy keeps all of it. ' +
      'The CSVs in exports/ are written from it after every run.</p>' +
    (data.repaired ? '<div class="notice good">The export files were missing rows the ' +
      'database had. They have been written again.</div>' : '') +
    (data.key_set ? '' : '<div class="notice bad">Apollo has no API key here. ' +
      'Add it on the Settings tab before searching.</div>');
}

async function findCandidates() {
  const go = document.getElementById('apollo-go');
  const out = document.getElementById('apollo-result');
  go.disabled = true;
  out.innerHTML = '<div class="notice wait">Searching Apollo. This spends nothing.</div>';
  const data = await api('/api/apollo/candidates', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      segment: document.getElementById('apollo-segment').value || 'mix',
      limit: Number(document.getElementById('apollo-limit').value) || 25,
    })
  });
  if (!data || !data.success) {
    go.disabled = false;
    out.innerHTML = '<div class="notice bad">' + escHtml(failMsg(data, 'Could not search.')) + '</div>';
    return;
  }
  if (!_apolloPoll) _apolloPoll = setInterval(pollCandidates, 2500);
}

async function pollCandidates() {
  const run = await api('/api/imports/run/status');
  if (!run || run.state === 'running') return;
  clearInterval(_apolloPoll); _apolloPoll = null;
  const go = document.getElementById('apollo-go');
  const out = document.getElementById('apollo-result');
  if (go) go.disabled = false;
  if (!out) return;
  if (run.state === 'failed') {
    out.innerHTML = '<div class="notice bad">' + escHtml(run.message || 'That search failed.') + '</div>';
    return;
  }
  out.innerHTML = '<div class="notice good">' + escHtml(run.message || 'Candidates staged.') +
    ' <a href="#" onclick="openStagedBatch(&quot;' + escHtml(run.batch_id || '') +
    '&quot;);return false">Review them</a></div>';
  showToast(run.message || 'Candidates staged.', 'success');
  loadApollo();
}

function openStagedBatch(id) {
  if (id) _importBatch = id;
  showTab('imports');
}


// ── Cohort builder: a prospect list is a query ──

let _cohort = { require: new Set(), exclude: new Set(), ranges: {} };
let _cohortOffset = 0;
let _cohortGeneration = 0;
const COHORT_PAGE = 50;

function renderCohortBuilder() {
  const el = document.getElementById('cohort-builder');
  if (!el || !_signals) return;

  const confirmed = (_signals.groups || [])
    .flatMap(g => g.signals)
    .filter(s => s.status === 'confirmed');

  if (!confirmed.length) {
    el.innerHTML = '<p class="muted" style="font-size:13px">' +
      'Confirm some signals above and they become the building blocks here.</p>';
    document.getElementById('cohort-result').innerHTML = '';
    return;
  }

  const col = (title, key, hint) =>
    '<div><div class="subhead" style="margin-top:0">' + title +
      ' <span class="muted" style="font-weight:400;text-transform:none;letter-spacing:0">' +
      hint + '</span></div><div class="cohort-grid">' +
    confirmed.map(s =>
      '<label class="cohort-pick"><input type="checkbox" ' +
        (_cohort[key].has(s.code) ? 'checked ' : '') +
        'onchange="toggleCohort(' + jsq(key) + ',' + jsq(s.code) + ',this.checked)">' +
        '<span>' + escHtml(s.label || s.code) + '</span>' +
        '<span class="n">' + (s.companies || 0) + '</span></label>'
    ).join('') + '</div></div>';

  // Numeric signals (ranks, ratings) take a range: "organic rank 11-30".
  const numeric = confirmed.filter(s => s.value_type === 'num');
  const bound = (code, side) => {
    const v = (_cohort.ranges[code] || {})[side];
    return '<input type="number" class="cohort-bound" placeholder="' + side + '" ' +
      'value="' + (v === undefined || v === null ? '' : escHtml(String(v))) + '" ' +
      'aria-label="' + escHtml(code) + ' ' + side + '" ' +
      'onchange="setCohortRange(' + jsq(code) + ',' + jsq(side) + ',this.value)">';
  };
  const rangeCol = numeric.length ?
    '<div style="height:18px"></div><div><div class="subhead" style="margin-top:0">In range ' +
      '<span class="muted" style="font-weight:400;text-transform:none;letter-spacing:0">' +
      '&mdash; e.g. organic rank 11 to 30; leave a side blank for no limit</span></div>' +
      '<div class="cohort-grid">' +
      numeric.map(s =>
        '<div class="cohort-pick cohort-range"><span>' + escHtml(s.label || s.code) + '</span>' +
          bound(s.code, 'min') + '<span class="muted">to</span>' + bound(s.code, 'max') + '</div>'
      ).join('') + '</div></div>' : '';

  el.innerHTML = col('Must have', 'require', '&mdash; every company in the cohort carries all of these') +
    '<div style="height:18px"></div>' +
    col('Must not have', 'exclude', '&mdash; disqualifiers') + rangeCol;
}

function setCohortRange(code, side, value) {
  const r = _cohort.ranges[code] || {min: null, max: null};
  r[side] = value === '' || isNaN(Number(value)) ? null : Number(value);
  if (r.min === null && r.max === null) delete _cohort.ranges[code];
  else _cohort.ranges[code] = r;
  _cohortOffset = 0;
  runCohort();
}

function toggleCohort(key, code, on) {
  if (on) _cohort[key].add(code); else _cohort[key].delete(code);
  // A company can't be both required and excluded on the same signal.
  const other = key === 'require' ? 'exclude' : 'require';
  if (on) _cohort[other].delete(code);
  _cohortOffset = 0;  // a new cohort starts on its first page
  runCohort();
  renderCohortBuilder();
}

async function runCohort() {
  const generation = ++_cohortGeneration;
  const offset = _cohortOffset;
  const el = document.getElementById('cohort-result');
  if (!el) return;
  const require = [..._cohort.require], exclude = [..._cohort.exclude];
  const ranges = _cohortRanges();
  if (!require.length && !ranges.length) { el.innerHTML = ''; return; }
  // Remove the prior export button as soon as its criteria become stale.
  el.innerHTML = '<p class="muted">Loading cohort…</p>';

  const data = await api('/api/cohort', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({require: require, exclude: exclude, ranges: ranges,
                          offset: offset, limit: COHORT_PAGE}),
  });
  if (generation !== _cohortGeneration) return;
  if (!data || data.error) {
    el.innerHTML = data ? '<div class="test-result error">' + escHtml(data.error) + '</div>' : '';
    return;
  }

  let html = '<div style="border-top:1px solid var(--border);margin-top:20px;padding-top:20px">' +
    '<div class="cohort-size">' + data.size + '</div>' +
    '<div class="muted" style="font-size:13px;margin-top:2px">compan' +
      (data.size === 1 ? 'y matches' : 'ies match') + ' this cohort right now</div>';

  if (data.companies && data.companies.length) {
    html += '<div class="table-card" style="margin-top:16px"><table><thead><tr>' +
      '<th>Company</th><th>Domain</th><th>Industry</th><th>Location</th>' +
      '</tr></thead><tbody>' +
      data.companies.map(c =>
        '<tr><td>' + escHtml(c.name) + '</td><td class="muted">' + escHtml(c.domain) + '</td>' +
        '<td class="muted">' + escHtml(c.industry) + '</td>' +
        '<td class="muted">' + escHtml(c.location) + '</td></tr>'
      ).join('') + '</tbody></table></div>';

    const first = data.offset + 1, last = data.offset + data.companies.length;
    const hasPrev = data.offset > 0, hasNext = last < data.size;
    html += '<div style="display:flex;align-items:center;gap:8px;margin-top:10px">' +
      '<span class="muted" style="font-size:12px">Showing ' + first + '&ndash;' + last +
        ' of ' + data.size + '</span>' +
      '<span style="flex:1"></span>' +
      '<button class="btn btn-secondary btn-sm" onclick="pageCohort(-1)"' +
        (hasPrev ? '' : ' disabled') + '>Prev</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="pageCohort(1)"' +
        (hasNext ? '' : ' disabled') + '>Next</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="exportCohort(this)">Export CSV</button>' +
      '</div>';
  } else if (data.size === 0) {
    html += '<p class="muted" style="font-size:13px;margin-top:12px">' +
      'No company carries all of those yet. Either loosen the cohort, or run ' +
      'prospecting to collect more.</p>';
  } else if (_cohortOffset > 0) {
    // The cohort shrank under us (a re-profile, a delete): back to page one.
    _cohortOffset = 0;
    return runCohort();
  }
  el.innerHTML = html + '</div>';
}

function _cohortRanges() {
  return Object.entries(_cohort.ranges || {})
    .filter(([, r]) => r.min !== null || r.max !== null)
    .map(([code, r]) => ({code: code, min: r.min, max: r.max}));
}

async function exportCohort(button) {
  const qs = new URLSearchParams({require: [..._cohort.require].join(','),
                                  exclude: [..._cohort.exclude].join(',')});
  // Ranges travel as "CODE:min:max", a blank side meaning no limit.
  const ranges = Object.entries(_cohort.ranges || {})
    .map(([code, r]) => code + ':' + (r.min ?? '') + ':' + (r.max ?? ''));
  if (ranges.length) qs.set('ranges', ranges.join(','));
  button.disabled = true;
  button.textContent = 'Exporting…';
  let url;
  let link;
  try {
    const response = await authFetch('/api/cohort.csv?' + qs);
    if (await lockIfRefused(response)) throw new Error('Dashboard authorization required');
    if (!response.ok) throw new Error('Export failed (HTTP ' + response.status + ')');
    url = URL.createObjectURL(await response.blob());
    link = document.createElement('a');
    link.href = url;
    link.download = 'cohort.csv';
    document.body.appendChild(link);
    link.click();
    button.textContent = 'Export CSV';
  } catch (error) {
    button.textContent = 'Export failed — retry';
  } finally {
    if (link) link.remove();
    // Give the browser time to start consuming the download before revoking.
    if (url) setTimeout(() => URL.revokeObjectURL(url), 1000);
    button.disabled = false;
  }
}

function pageCohort(dir) {
  _cohortOffset = Math.max(0, _cohortOffset + dir * COHORT_PAGE);
  runCohort();
}

// ── Settings ──

async function loadSettings() {
  const data = await api('/api/settings');
  if (!data) return;

  // Active provider indicator
  const prov = data.provider || 'instantly';
  const provEl = document.getElementById('active-provider');
  if (provEl) provEl.textContent = prov;

  // Secret fields never echo a value — show a "saved" placeholder instead.
  const savedPh = (id, isSet, base) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = '';
    el.placeholder = isSet ? 'Saved — enter new value to change' : base;
  };
  const tag = (id, ok, okLabel) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = 'email-tag ' + (ok ? 'verified' : 'guess');
    el.textContent = ok ? okLabel : 'not set';
  };

  // Non-secret values repopulate
  document.getElementById('gmail-id').value = data.gmail_client_id || '';
  document.getElementById('smtp-host').value = data.smtp_host || '';
  document.getElementById('smtp-port').value = data.smtp_port || '';
  document.getElementById('smtp-user').value = data.smtp_username || '';
  document.getElementById('imap-host').value = data.imap_host || '';
  document.getElementById('imap-port').value = data.imap_port || '';
  document.getElementById('imap-user').value = data.imap_username || '';
  document.getElementById('m365-client').value = data.m365_client_id || '';
  document.getElementById('m365-tenant').value = data.m365_tenant_id || '';
  document.getElementById('linkedin-email').value = data.linkedin_email || '';
  document.getElementById('cf-account-id').value = data.cloudflare_account_id || '';

  savedPh('gmail-secret', data.gmail_client_secret_set, 'Enter client secret');
  savedPh('smtp-pass', data.smtp_password_set, 'Enter password / app password');
  savedPh('imap-pass', data.imap_password_set, 'Same as SMTP password');
  document.getElementById('imap-same-pass').checked = !data.imap_password_set;
  savedPh('apollo-key', data.apollo_api_key_set, 'Enter your Apollo API key');
  savedPh('instantly-key', data.instantly_api_key_set, 'Enter your Instantly API key');
  const ap = document.getElementById('apollo-status');
  if (ap) {
    ap.className = 'email-tag ' + (data.apollo_api_key_set ? 'verified' : 'guess');
    ap.textContent = data.apollo_api_key_set ? 'connected' : 'no key yet';
  }
  savedPh('reoon-key', data.reoon_api_key_set, '600 free/mo — reoon.com/email-verifier');
  savedPh('zerobounce-key', data.zerobounce_api_key_set, '100 free/mo — best for M365/Workspace catch-alls');
  savedPh('hunter-key', data.hunter_api_key_set, '50 free/mo + email-pattern lookup');
  savedPh('linkedin-password', data.linkedin_password_set, 'Enter password');
  savedPh('cf-api-token', data.cloudflare_api_token_set, 'Your Cloudflare API Token');

  // Gmail auth status chip
  const g = document.getElementById('gmail-status');
  if (g) {
    const realId = /^\d+-[0-9a-z]+\.apps\.googleusercontent\.com$/.test((data.gmail_client_id || '').trim());
    if (data.gmail_client_id && !realId) { g.className = 'email-tag invalid'; g.textContent = 'client ID isn\'t real'; }
    else if (data.gmail_authorized) { g.className = 'email-tag verified'; g.textContent = 'authorized'; }
    else if (data.gmail_client_secret_set) { g.className = 'email-tag risky'; g.textContent = 'sign in on Inboxes'; }
    else { g.className = 'email-tag guess'; g.textContent = 'not set up'; }
  }
  const m = document.getElementById('m365-status');
  if (m) {
    const realM = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test((data.m365_client_id || '').trim());
    if (data.m365_client_id && !realM) { m.className = 'email-tag invalid'; m.textContent = 'client ID isn\'t real'; }
    else if (data.m365_authorized) { m.className = 'email-tag verified'; m.textContent = 'authorized'; }
    else if (data.m365_client_id) { m.className = 'email-tag risky'; m.textContent = 'sign in on Inboxes'; }
    else { m.className = 'email-tag guess'; m.textContent = 'not set up'; }
  }
  tag('reoon-status', data.reoon_api_key_set, 'set');
  tag('zerobounce-status', data.zerobounce_api_key_set, 'set');
  tag('hunter-status', data.hunter_api_key_set, 'set');
}

function saveGmail() {
  const payload = {GMAIL_CLIENT_ID: document.getElementById('gmail-id').value.trim()};
  const sec = document.getElementById('gmail-secret').value;
  if (sec) payload.GMAIL_CLIENT_SECRET = sec;
  saveEnv(payload, 'Saved. Now sign in on the Inboxes tab.').then(loadSettings);
}

function saveSmtp() {
  const payload = {
    SMTP_HOST: document.getElementById('smtp-host').value.trim(),
    SMTP_PORT: document.getElementById('smtp-port').value.trim(),
    SMTP_USERNAME: document.getElementById('smtp-user').value.trim(),
  };
  const pass = document.getElementById('smtp-pass').value;
  if (pass) payload.SMTP_PASSWORD = pass;
  // Blank IMAP fields fall back to the SMTP ones. A typed IMAP password wins;
  // otherwise the checkbox clears it back to the SMTP one, and an unticked
  // empty box keeps whatever IMAP password is saved.
  payload.IMAP_HOST = document.getElementById('imap-host').value.trim();
  payload.IMAP_PORT = document.getElementById('imap-port').value.trim();
  payload.IMAP_USERNAME = document.getElementById('imap-user').value.trim();
  const imapPass = document.getElementById('imap-pass').value;
  if (imapPass) payload.IMAP_PASSWORD = imapPass;
  else if (document.getElementById('imap-same-pass').checked) payload.IMAP_PASSWORD = '';
  saveEnv(payload, 'SMTP settings saved.').then(loadSettings);
}

function saveMicrosoft() {
  const client = document.getElementById('m365-client').value.trim();
  if (!client) { showToast('Paste the Application (client) ID first.', 'error'); return; }
  saveEnv({M365_CLIENT_ID: client, M365_TENANT_ID: document.getElementById('m365-tenant').value.trim()},
          'Saved. Now sign in on the Inboxes tab.').then(loadSettings);
}

function saveVerifiers() {
  const payload = {};
  const r = document.getElementById('reoon-key').value;
  const z = document.getElementById('zerobounce-key').value;
  const h = document.getElementById('hunter-key').value;
  if (r) payload.REOON_API_KEY = r;
  if (z) payload.ZEROBOUNCE_API_KEY = z;
  if (h) payload.HUNTER_API_KEY = h;
  if (!Object.keys(payload).length) { showToast('Enter at least one key first.', 'error'); return; }
  saveEnv(payload, 'Verification keys saved.').then(loadSettings);
}

async function saveEnv(payload, okMsg) {
  const data = await api('/api/settings/env', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  if (data && data.success) showToast(okMsg, 'success');
  else showToast((data && data.message) || 'Save failed — is the dashboard still running?', 'error');
}

// Secrets are only sent when typed: an empty box means "keep the saved one".
function saveInstantly() {
  const key = document.getElementById('instantly-key').value.trim();
  if (!key) { showToast('Enter a new key first. The saved one is kept.', 'error'); return; }
  saveEnv({INSTANTLY_API_KEY: key}, 'Instantly API key saved.');
}

// ── Working hours: the setting that could stop the agent starting ──

async function loadHours() {
  const data = await api('/api/settings/hours');
  if (!data || data.error) return;
  const set = (id, value) => { const el = document.getElementById(id); if (el) el.value = value; };
  set('quiet-start', data.quiet_start || '22:00');
  set('quiet-end', data.quiet_end || '07:00');
  set('quiet-tz', data.timezone || '');
  set('window-start', data.window_start || '08:00');
  set('window-end', data.window_end || '17:00');
  const on = document.getElementById('window-on');
  if (on) on.checked = !!data.send_window;
  const weekdays = document.getElementById('window-weekdays');
  if (weekdays) weekdays.checked = data.window_weekdays_only !== false;
  toggleWindowFields();

  const note = document.getElementById('quiet-tz-note');
  const chip = document.getElementById('hours-status');
  if (data.problem) {
    if (note) note.textContent = data.problem;
    if (chip) { chip.className = 'email-tag invalid'; chip.textContent = 'using ' + data.resolved_timezone; }
  } else {
    if (note) note.textContent = data.resolved_timezone && data.resolved_timezone !== data.timezone
      ? 'Read as ' + data.resolved_timezone + '.' : '';
    if (chip) { chip.className = 'email-tag verified'; chip.textContent = data.resolved_timezone || ''; }
  }
}

function toggleWindowFields() {
  const on = document.getElementById('window-on');
  const fields = document.getElementById('window-fields');
  if (fields) fields.style.display = on && on.checked ? '' : 'none';
}

async function saveHours() {
  const val = id => (document.getElementById(id) || {}).value || '';
  const on = id => !!(document.getElementById(id) || {}).checked;
  const data = await api('/api/settings/hours', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      quiet_start: val('quiet-start'), quiet_end: val('quiet-end'),
      timezone: val('quiet-tz'),
      send_window: on('window-on'), window_start: val('window-start'),
      window_end: val('window-end'), window_weekdays_only: on('window-weekdays'),
      window_recipient_timezone: true,
    })
  });
  showToast(data && data.success ? (data.message || 'Saved.')
    : failMsg(data, 'Could not save those hours.'),
    data && data.success ? 'success' : 'error');
  loadHours();
  loadToday();
}

function saveApollo() {
  const key = document.getElementById('apollo-key').value.trim();
  if (!key) { showToast('Enter the Apollo API key first.', 'error'); return; }
  saveEnv({APOLLO_HARVEY_KEY: key}, 'Apollo key saved. Staged lists can be enriched now.')
    .then(loadSettings);
}

function saveLinkedIn() {
  const payload = {LINKEDIN_EMAIL: document.getElementById('linkedin-email').value.trim()};
  const pass = document.getElementById('linkedin-password').value;
  if (pass) payload.LINKEDIN_PASSWORD = pass;
  saveEnv(payload, 'LinkedIn credentials saved.');
}

function saveCloudflare() {
  const payload = {CLOUDFLARE_ACCOUNT_ID: document.getElementById('cf-account-id').value.trim()};
  const token = document.getElementById('cf-api-token').value.trim();
  if (token) payload.CLOUDFLARE_API_TOKEN = token;
  saveEnv(payload, 'Cloudflare credentials saved.');
}

// ── Import contacts: a list fetched somewhere else, as JSON ──
//
// A cloud run, another machine or the leads dashboard produces a contact list
// this workspace has never seen. The files are read here in the browser and
// sent as one JSON body: nothing is uploaded to anyone else, nothing is spent
// and nothing is sent to a prospect.

function openImportContacts() {
  const d = document.getElementById('import-dialog');
  d.innerHTML =
    '<div class="modal-head"><div><h3 id="import-dialog-title">Add contacts</h3>' +
      '<div class="sub">A CSV from anywhere, a Sellbird export, or a list of addresses. ' +
      'It is staged for review first: nothing is enriched and no credit is spent until you say so.</div></div>' +
      '<button class="btn btn-ghost btn-sm btn-icon" onclick="closeImportDialog()" aria-label="Close">' + icon('close') + '</button></div>' +
    '<div class="modal-body">' +
      '<div class="form-group"><label class="form-label" for="import-contacts-file">File (.csv or .json)</label>' +
        '<input type="file" class="form-input" id="import-contacts-file" accept=".csv,.tsv,.json,text/csv,application/json">' +
        '<p class="desc">A file wins over anything pasted below.</p></div>' +
      '<div class="form-group"><label class="form-label" for="import-contacts-paste">Or paste it</label>' +
        '<textarea class="form-input" id="import-contacts-paste" rows="6" spellcheck="false" ' +
        'placeholder="Paste a CSV, a contacts JSON, or one email address per line."></textarea></div>' +
      '<details class="form-group"><summary class="form-label">A Sellbird export, with its ledger</summary>' +
        '<p class="desc">Import it straight into the pipeline instead of staging it. The ledger file ' +
        'carries the credits already spent and the people a fetch has seen, so the next run does not pay for them twice.</p>' +
        '<input type="file" class="form-input" id="import-runs-file" accept=".json,application/json" ' +
        'aria-label="Ledger file (runs.json)"></details>' +
      '<p class="desc" id="import-contacts-result" role="status"></p>' +
    '</div>' +
    '<div class="modal-foot"><button class="btn btn-ghost" onclick="closeImportDialog()">Cancel</button>' +
      '<button class="btn btn-secondary" id="import-contacts-direct" onclick="importContacts(true)">Import directly</button>' +
      '<button class="btn btn-primary" id="import-contacts-go" onclick="importContacts(false)">Stage for review</button></div>';
  if (!d.open) d.showModal();
}

function closeImportDialog() {
  const d = document.getElementById('import-dialog');
  if (d.open) d.close();
}

async function importContacts(direct) {
  const el = document.getElementById('import-contacts-result');
  const go = document.getElementById(direct ? 'import-contacts-direct' : 'import-contacts-go');
  const file = (document.getElementById('import-contacts-file').files || [])[0];
  const runsEl = document.getElementById('import-runs-file');
  const runsFile = ((runsEl && runsEl.files) || [])[0];
  const pasted = document.getElementById('import-contacts-paste').value.trim();
  if (!file && !pasted) { el.textContent = 'Choose a file, or paste the list.'; return; }
  go.disabled = true;
  el.textContent = file ? 'Reading ' + file.name + '…' : 'Reading…';
  let text;
  try {
    text = file ? await file.text() : pasted;
  } catch (e) {
    go.disabled = false;
    el.textContent = 'That file could not be read.';
    return;
  }

  if (direct) return importDirectly(text, runsFile, el, go);

  el.textContent = 'Staging…';
  const data = await api('/api/imports', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: file ? file.name : 'pasted', text: text})
  });
  go.disabled = false;
  if (data && data.success) {
    showToast(data.message || 'Staged', 'success');
    closeImportDialog();
    _importBatch = data.batch_id;
    showTab('imports');
  } else {
    el.textContent = failMsg(data, 'Could not stage that file.');
    showToast(el.textContent, 'error');
  }
}

// The older path, kept for a Sellbird export that is already finished: it
// goes straight into the pipeline, ledger and all, with nothing to decide.
async function importDirectly(text, runsFile, el, go) {
  let payload;
  try {
    payload = {contacts: JSON.parse(text)};
    if (payload.contacts && Array.isArray(payload.contacts.contacts)) {
      if (Array.isArray(payload.contacts.runs)) payload.runs = payload.contacts.runs;
      payload.contacts = payload.contacts.contacts;
    }
    if (runsFile) payload.runs = JSON.parse(await runsFile.text());
  } catch (e) {
    go.disabled = false;
    el.textContent = 'Importing directly needs a Sellbird JSON export. Stage a CSV instead.';
    return;
  }
  el.textContent = 'Importing…';
  const data = await api('/api/contacts/import', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  go.disabled = false;
  if (data && data.success) {
    showToast(data.message || 'Contacts imported', 'success');
    closeImportDialog();
    loadProspects();
    loadToday();
  } else {
    el.textContent = failMsg(data, 'Could not import that list.');
    showToast(el.textContent, 'error');
  }
}

// ── Imports: a staged list, priced before it costs anything ──
//
// Every list that arrives from outside stops here first. The page shows what
// is in it, what Sellbird read it as, and what each Apollo operation would
// cost on these exact rows. Nothing is called until a cap is approved.

let _importBatch = null;     // the open batch, or null for the list
let _importRun = null;       // poll timer while an enrichment runs
const IMPORT_FIELDS = ['', 'email', 'first_name', 'last_name', 'title',
                       'company', 'domain', 'linkedin_url', 'full_name'];
const ROW_STATE = {
  staged: ['Staged', 't-idle'], enriched: ['Enriched', 't-good'],
  imported: ['In pipeline', 't-good'], duplicate: ['Already yours', 't-waiting'],
  failed: ['Failed', 't-bad'], skipped: ['Skipped', 't-idle'],
};

function importActions(html) {
  document.getElementById('imports-actions').innerHTML = html;
}

async function loadImports() {
  if (_importBatch) return loadImportBatch(_importBatch);
  const el = document.getElementById('imports-body');
  const data = await api('/api/imports');
  if (!data) { el.innerHTML = offlineState(); return; }
  importActions('<button class="btn btn-primary" onclick="openImportContacts()">' +
    icon('plus') + 'Add contacts</button>');
  const batches = data.batches || [];
  const count = document.getElementById('nav-imports');
  const waiting = batches.filter(b => b.status === 'review').length;
  if (count) count.textContent = waiting ? String(waiting) : '';
  if (!batches.length) {
    el.innerHTML = emptyState(icon('imports'), 'Nothing staged',
      'Drop in a CSV, a Sellbird export, or a list of addresses and Sellbird will read it, ' +
      'tell you what is usable, and price the ways of enriching it before anything is spent.');
    return;
  }
  let html = '<div class="inbox-summary"><span><b>' + batches.length + '</b> ' +
    (batches.length === 1 ? 'list' : 'lists') + '</span><span><b>' +
    Number(data.credits_today || 0) + '</b> Apollo credits spent today</span>' +
    (Number(data.daily_cap) > 0 ? '<span>Daily cap <b>' + Number(data.daily_cap) + '</b></span>' : '') +
    '</div><div class="table-card"><table><thead><tr><th>List</th><th>Rows</th>' +
    '<th>Enriched</th><th>In pipeline</th><th>Credits</th><th>Status</th><th>Added</th></tr></thead><tbody>';
  for (const b of batches) {
    const c = b.counts || {};
    html += '<tr style="cursor:pointer" onclick="openImport(' + jsq(b.id) + ')">' +
      '<td><div class="cell-stack"><span class="cell-main">' + escHtml(b.name || 'pasted') + '</span>' +
        '<span class="cell-sub">' + escHtml(b.source || '') + '</span></div></td>' +
      '<td>' + Number(c.total || 0) + '</td>' +
      '<td>' + Number(c.enriched || 0) + '</td>' +
      '<td>' + Number(c.imported || 0) + '</td>' +
      '<td>' + Number(b.credits_spent || 0) + '</td>' +
      '<td>' + badge(b.status) + '</td>' +
      '<td class="muted">' + relTime(b.created_at) + '</td></tr>';
  }
  el.innerHTML = html + '</tbody></table></div>';
}

function openImport(id) { _importBatch = id; loadImports(); }

function closeImport() {
  _importBatch = null;
  if (_importRun) { clearInterval(_importRun); _importRun = null; }
  loadImports();
}

async function loadImportBatch(id) {
  const el = document.getElementById('imports-body');
  const data = await api('/api/imports/' + encodeURIComponent(id));
  if (!data) { el.innerHTML = offlineState(); return; }
  if (data.success === false) { closeImport(); return; }
  const b = data.batch || {};
  const a = b.analysis || {};
  const c = b.counts || {};
  importActions('<button class="btn btn-ghost" onclick="closeImport()">All lists</button>' +
    '<button class="btn btn-secondary" onclick="promoteImport()">Add ready rows to pipeline</button>');

  let html = '<div class="card"><div class="card-title">' + escHtml(b.name || 'pasted') +
    ' ' + badge(b.status) + '</div>' +
    '<p>' + escHtml(a.summary || 'Staged and waiting.') + '</p>' +
    '<div class="modal-stats">' +
      '<div class="inset"><div class="k">Rows</div><div class="v">' + Number(c.total || 0) + '</div></div>' +
      '<div class="inset"><div class="k">With an address</div><div class="v">' + Number((a.quality || {}).with_email || 0) + '</div></div>' +
      '<div class="inset"><div class="k">Already yours</div><div class="v">' + Number(c.duplicate || 0) + '</div></div>' +
      '<div class="inset"><div class="k">Credits spent</div><div class="v">' + Number(b.credits_spent || 0) + '</div></div>' +
    '</div>' +
    importSteps(b, c) +
    (a.pending ? '<p class="desc">Claude is reading the file now. What you see is Sellbird\'s own reading of it.</p>'
      : a.by === 'claude' ? '<p class="desc">Read by Claude.</p>'
      : '<p class="desc">Read by Sellbird itself.</p>') +
    (a.warnings || []).map(w => '<div class="notice wait">' + escHtml(w) + '</div>').join('') +
    '</div>';

  const run = data.run || {};
  if (run.state === 'running' && run.batch_id === id) {
    html += '<div class="notice wait"><b>Enriching.</b> ' + escHtml(run.op || '') + ' is running.</div>';
  } else if (run.state === 'done' && run.batch_id === id && run.message) {
    html += '<div class="notice good">' + escHtml(run.message) + '</div>';
  } else if (run.state === 'failed' && run.batch_id === id) {
    html += '<div class="notice bad">' + escHtml(run.message || 'That run failed.') + '</div>';
  }

  html += '<div class="card"><div class="card-title">How to enrich this list</div>' +
    '<p class="desc">What each one would cost on these rows, before anything is called. ' +
    'Apollo charges nothing for a person it cannot find, so a run usually comes in under its estimate.</p>' +
    '<div class="inbox-help">';
  for (const op of (data.ops || [])) {
    const recommended = ((a.recommended || [])[0] || {}).op === op.key;
    html += '<div class="inset">' +
      '<h4>' + escHtml(op.label) + (recommended ? ' <span class="badge t-good">Recommended</span>' : '') +
        (op.danger ? ' <span class="badge t-bad">Expensive</span>' : '') + '</h4>' +
      '<p>' + escHtml(op.blurb) + '</p>' +
      '<p class="desc"><b>' + (op.free ? 'Free' : op.credits_per_unit + ' credit' +
        (op.credits_per_unit > 1 ? 's' : '') + ' per ' + escHtml(op.unit)) + '</b> &middot; ' +
        op.eligible + ' of these rows qualify &middot; ' +
        (op.free ? 'no credits' : 'up to <b>' + op.estimated_credits + ' credits</b>') +
        (op.plan_note ? ' &middot; ' + escHtml(op.plan_note) : '') + '</p>' +
      sharedNote(op) + alreadyNote(op) +
      (op.available === false
        ? '<p class="desc">Not wired up in this fork yet. The price is here so ' +
          'you can see what it would cost.</p>' : '') +
      '<button class="btn ' + (op.danger ? 'btn-danger' : recommended ? 'btn-primary' : 'btn-secondary') +
        ' btn-sm"' + (op.eligible && op.available !== false ? '' : ' disabled') +
        ' onclick="askEnrich(' + jsq(op.key) + ')">' +
        (op.available === false ? 'Not available yet'
          : op.eligible ? 'Run this' : 'No rows qualify') + '</button></div>';
  }
  html += '</div></div>';

  html += mappingCard(b);
  html += importRowsTable(data.rows || []);
  const ready = Number(c.enriched || 0) +
    (data.rows || []).filter(r => r.status === 'staged' && r.email).length;
  if (ready) {
    html += '<div class="card"><div class="card-title">Ready to go into the pipeline</div>' +
      '<p>' + ready + ' of these are usable contacts. Until you add them, they are not in ' +
      'Companies, Contacts or any campaign: a staged row is a candidate, not a contact.</p>' +
      '<div class="btn-group"><button class="btn btn-primary" onclick="promoteImport()">Add ' +
      ready + ' contact' + (ready === 1 ? '' : 's') + ' to the pipeline</button></div></div>';
  }
  html += '<div class="card"><div class="btn-group">' +
    '<button class="btn btn-ghost btn-sm" onclick="discardImport()">Discard this list</button>' +
    '<button class="btn btn-ghost btn-sm" onclick="deleteImport()">Delete it</button></div></div>';
  el.innerHTML = html;

  if (run.state === 'running' && run.batch_id === id) {
    if (!_importRun) _importRun = setInterval(pollImportRun, 2000);
  } else if (_importRun) {
    clearInterval(_importRun); _importRun = null;
  }
  // Claude's reading replaces Sellbird's as soon as it lands.
  if (a.pending && _importBatch === id) setTimeout(() => {
    if (_importBatch === id) loadImportBatch(id);
  }, 3000);
}

// Where this list has got to. Three steps, and only the last one puts
// anything in Companies or Contacts — which is not obvious from a page full
// of rows that look like contacts already.
function importSteps(batch, counts) {
  const staged = Number(counts.staged || 0) + Number(counts.duplicate || 0);
  const bought = Number(counts.enriched || 0);
  const inPipeline = Number(counts.imported || 0);
  const steps = [
    ['Staged', staged + ' waiting', 'free', staged > 0],
    ['Addresses bought', bought ? bought + ' enriched' : 'none yet',
     Number(batch.credits_spent || 0) + ' credits', bought > 0],
    ['In the pipeline', inPipeline ? inPipeline + ' contacts' : 'none yet',
     'Companies and Contacts', inPipeline > 0],
  ];
  return '<div class="import-steps">' + steps.map(([title, detail, note, done], i) =>
    '<div class="inset' + (done ? '' : ' muted') + '">' +
      '<h4>' + (i + 1) + '. ' + title + (done ? ' ' + icon('check') : '') + '</h4>' +
      '<p class="desc">' + escHtml(detail) + ' &middot; ' + escHtml(note) + '</p></div>').join('') +
    '</div>';
}

function mappingCard(b) {
  const mapping = b.mapping || {};
  const columns = Object.keys(mapping);
  if (!columns.length) return '';
  let html = '<div class="card"><div class="card-title">What each column means</div>' +
    '<p class="desc">Sellbird read the headings. Correct anything it got wrong and the rows are re-read ' +
    'from the original file, which is kept exactly as it arrived.</p><div class="inbox-help">';
  for (const col of columns) {
    html += '<div class="inset"><h4>' + escHtml(col) + '</h4>' +
      '<select class="form-input" data-col="' + escHtml(col) + '">' +
      IMPORT_FIELDS.map(f => '<option value="' + f + '"' + (mapping[col] === f ? ' selected' : '') +
        '>' + (f ? escHtml(f.replace(/_/g, ' ')) : 'ignore') + '</option>').join('') +
      '</select></div>';
  }
  return html + '</div><div class="btn-group"><button class="btn btn-secondary btn-sm" ' +
    'onclick="saveMapping()">Re-read with this mapping</button></div></div>';
}

function importRowsTable(rows) {
  if (!rows.length) return '';
  const shown = rows.slice(0, 100);
  let html = '<div class="table-card"><table class="nowrap"><thead><tr><th>Name</th><th>Email</th>' +
    '<th>Company</th><th>Title</th><th>State</th><th>Route</th><th>Credits</th></tr></thead><tbody>';
  for (const r of shown) {
    const state = ROW_STATE[r.status] || [r.status, 't-idle'];
    html += '<tr><td>' + escHtml(((r.first_name || '') + ' ' + (r.last_name || '')).trim() || '—') + '</td>' +
      '<td>' + escHtml(r.email || '') + (r.email_status === 'verified' ?
        ' <span class="verified" title="Apollo verified">&#10003;</span>' : '') + '</td>' +
      '<td>' + escHtml(r.company || r.domain || '') + '</td>' +
      '<td>' + escHtml(r.title || '') + '</td>' +
      '<td><span class="badge ' + state[1] + '">' + escHtml(state[0]) + '</span></td>' +
      '<td class="muted">' + escHtml(r.route || '') + '</td>' +
      '<td>' + Number(r.credits || 0) + '</td></tr>';
  }
  return html + '</tbody></table></div><div class="table-foot">' + shown.length +
    ' of ' + rows.length + ' rows</div>';
}

async function saveMapping() {
  const mapping = {};
  document.querySelectorAll('#imports-body select[data-col]').forEach(s => {
    if (s.value) mapping[s.getAttribute('data-col')] = s.value;
  });
  const data = await api('/api/imports/' + encodeURIComponent(_importBatch) + '/mapping', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({mapping: mapping})
  });
  showToast(data && data.success ? (data.message || 'Re-read.') :
    failMsg(data, 'Could not save that mapping.'), data && data.success ? 'success' : 'error');
  loadImports();
}

// Approving a spend: the estimate, then a cap, then the run.
// A company record is one credit a company, however many of its people are
// on the list. Say so, or the price looks like a mistake.
function sharedNote(op) {
  if (!Number(op.shared)) return '';
  return '<p class="desc t-good">' + Number(op.eligible) + ' of these rows share ' +
    Number(op.charged) + ' ' + plural(op.unit, Number(op.charged)) +
    ', so you are charged for ' + Number(op.charged) + ', not ' +
    Number(op.eligible) + '.</p>';
}

// Rows that already hold what this would buy. Showing the number is how the
// user can see that a second run on the same list costs nothing.
function alreadyNote(op) {
  if (!Number(op.already_have)) return '';
  return '<p class="desc">' + Number(op.already_have) +
    ' already have this, so they are left out of the price.</p>';
}

function plural(word, count) {
  if (count === 1) return word;
  return word.endsWith('y') ? word.slice(0, -1) + 'ies' : word + 's';
}

async function askEnrich(opKey) {
  const data = await api('/api/imports/' + encodeURIComponent(_importBatch) + '/estimate', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({op: opKey})
  });
  if (!data || data.success === false) {
    showToast(failMsg(data, 'Could not price that.'), 'error');
    return;
  }
  const d = document.getElementById('import-dialog');
  const free = data.free;
  d.innerHTML =
    '<div class="modal-head"><div><h3 id="import-dialog-title">' + escHtml(data.label) + '</h3>' +
      '<div class="sub">' + data.rows + ' rows qualify' +
      (data.skipped ? ', ' + data.skipped + ' do not (they need ' + escHtml(data.needs) +
        ', or already have what this buys)' : '') +
      '.</div></div>' +
      '<button class="btn btn-ghost btn-sm btn-icon" onclick="closeImportDialog()" aria-label="Close">' + icon('close') + '</button></div>' +
    '<div class="modal-body">' +
      (free ? '<div class="notice good">This one is free. No credits are involved.</div>'
            : '<div class="notice wait">This will cost up to <b>' + data.credits +
              ' credits</b>. Apollo charges nothing for a person it cannot find.' +
              (Number(data.shared) ? ' These ' + data.rows + ' rows share ' +
                Number(data.charged) + ' ' + plural(data.unit, Number(data.charged)) +
                ', and a ' + escHtml(data.unit) + ' is charged once.' : '') + '</div>' +
              '<div class="form-group"><label class="form-label" for="enrich-cap">Spend at most</label>' +
              '<input class="form-input" id="enrich-cap" type="number" min="1" step="1" value="' +
              data.credits + '"><p class="desc">The run stops dead at this number, even mid-list.</p></div>') +
      '<p class="desc" id="enrich-result" role="status"></p></div>' +
    '<div class="modal-foot"><button class="btn btn-ghost" onclick="closeImportDialog()">Cancel</button>' +
      '<button class="btn btn-primary" id="enrich-go" onclick="runEnrich(' + jsq(opKey) + ')">' +
      (free ? 'Run it' : 'Spend up to ' + data.credits + ' credits') + '</button></div>';
  if (!d.open) d.showModal();
}

async function runEnrich(opKey) {
  const capEl = document.getElementById('enrich-cap');
  const go = document.getElementById('enrich-go');
  go.disabled = true;
  const data = await api('/api/imports/' + encodeURIComponent(_importBatch) + '/enrich', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({op: opKey, max_credits: capEl ? Number(capEl.value) || 0 : 0})
  });
  go.disabled = false;
  if (data && data.success) {
    closeImportDialog();
    showToast('Running. Credits are counted as they are spent.', 'success');
    if (!_importRun) _importRun = setInterval(pollImportRun, 2000);
    loadImports();
  } else {
    const el = document.getElementById('enrich-result');
    if (el) el.textContent = failMsg(data, 'Could not start that run.');
    showToast(failMsg(data, 'Could not start that run.'), 'error');
  }
}

async function pollImportRun() {
  const run = await api('/api/imports/run/status');
  if (!run || run.state === 'running') return;
  clearInterval(_importRun); _importRun = null;
  if (run.message) showToast(run.message, run.state === 'failed' ? 'error' : 'success');
  loadImports();
}

async function promoteImport() {
  const data = await api('/api/imports/' + encodeURIComponent(_importBatch) + '/import', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({})
  });
  showToast(data && data.success ? (data.message || 'Imported.') :
    failMsg(data, 'Could not import those rows.'), data && data.success ? 'success' : 'error');
  loadImports();
  loadToday();
}

async function discardImport() {
  const data = await api('/api/imports/' + encodeURIComponent(_importBatch) + '/discard',
    {method: 'POST'});
  if (data && data.success) { showToast(data.message || 'Discarded.', 'success'); closeImport(); }
  else showToast(failMsg(data, 'Could not discard that list.'), 'error');
}

async function deleteImport() {
  const data = await api('/api/imports/' + encodeURIComponent(_importBatch), {method: 'DELETE'});
  if (data && data.success) { showToast('Deleted.', 'success'); closeImport(); }
  else showToast(failMsg(data, 'Could not delete that list.'), 'error');
}

async function testInstantly() {
  const key = document.getElementById('instantly-key').value.trim();
  const el = document.getElementById('instantly-test-result');
  el.innerHTML = '<div class="test-result pending">Testing&hellip;</div>';
  const data = await api('/api/settings/test-instantly', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({api_key: key})
  });
  if (!data) {
    el.innerHTML = '<div class="test-result error">Could not reach the dashboard server.</div>';
    return;
  }
  el.innerHTML = '<div class="test-result ' + (data.success ? 'success' : 'error') + '">' + escHtml(failMsg(data, '')) + '</div>';
}

// ── Controls ──

async function loadSellbirdStatus() {
  const data = await api('/api/sellbird/status');
  const headerDot = document.getElementById('header-dot');
  const headerText = document.getElementById('header-status-text');

  if (_locked) return;  // the header already says "Locked"
  if (!data) {
    headerDot.className = 'status-dot offline';
    headerText.textContent = 'Offline';
    return;
  }
  const running = !!data.running;

  headerDot.className = 'status-dot ' + (running ? 'running' : 'stopped');
  headerText.textContent = running ? 'Sellbird is running' : 'Sellbird is stopped';
  document.getElementById('control-dot').className = 'dot ' + (running ? 'running' : 'stopped');
  const label = document.getElementById('control-label');
  label.className = 'label ' + (running ? 'running' : 'stopped');
  label.textContent = running ? 'Running' : 'Stopped';

  const meta = document.getElementById('control-meta');
  if (running && data.pid) {
    let info = 'PID ' + escHtml(String(data.pid));
    if (data.started_at) info += ' &middot; started ' + formatDate(data.started_at);
    meta.innerHTML = info;
  } else {
    meta.innerHTML = 'Sellbird wakes every few minutes, does what needs doing, and sleeps.';
  }

  // "Stopped" on its own sent people hunting through container logs. Say
  // what is in the way, and say it whether or not a process is alive: a
  // loop that pauses every cycle looks exactly like a healthy one here.
  const why = document.getElementById('control-why');
  if (why) {
    const blocked = data.blocked || [];
    why.innerHTML = blocked.map(b =>
      '<div class="notice bad"><b>' + escHtml(b.title) + '</b><br>' + escHtml(b.detail) +
      (b.tab ? '<br><a href="#" onclick="goTab(' + jsq(b.tab) + ');return false">Take me there</a>' : '') +
      '</div>').join('') +
      (!blocked.length && data.paused_reason
        ? '<div class="notice wait"><b>Sellbird has paused itself</b><br>' +
          escHtml(data.paused_reason) + '</div>' : '');
  }
  const headerWhy = (data.blocked || [])[0];
  if (headerWhy && !running) headerText.textContent = headerWhy.title;

  document.getElementById('btn-start').style.display = running ? 'none' : '';
  document.getElementById('btn-stop').style.display = running ? '' : 'none';
}

async function startSellbird() {
  const btn = document.getElementById('btn-start');
  btn.disabled = true;
  const data = await api('/api/sellbird/start', {method: 'POST'});
  if (data && data.success) showToast('Sellbird started.', 'success');
  else showToast((data && data.message) || 'Failed to start.', 'error');
  btn.disabled = false;
  loadSellbirdStatus();
}

async function stopSellbird() {
  const btn = document.getElementById('btn-stop');
  btn.disabled = true;
  const data = await api('/api/sellbird/stop', {method: 'POST'});
  if (data && data.success) showToast('Sellbird stopped.', 'success');
  else showToast((data && data.message) || 'Failed to stop.', 'error');
  btn.disabled = false;
  loadSellbirdStatus();
}

async function loadLogs() {
  const data = await api('/api/sellbird/logs');
  const el = document.getElementById('log-viewer');
  if (data && data.lines && data.lines.length) {
    const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.textContent = data.lines.join('\n');
    if (stick) el.scrollTop = el.scrollHeight;
  } else {
    el.textContent = 'No logs yet. Start Sellbird to see activity.';
  }
}

// ── Pipeline data ──

// Calls today per subscription — each labelled with the provider it ran on.
function brainCallChips(data) {
  const chip = (label, n) => '<span class="chip">' + label + ' calls today <b>' + escHtml(String(n || 0)) + '</b></span>';
  const codex = data.codex_calls_today || 0;
  const st = data.brain_status || {};
  // A paused brain must not look healthy: say so, and why.
  const paused = st.status === 'paused'
    ? '<span class="chip" title="' + escHtml(st.reason || '') + '">' +
      escHtml(st.provider === 'codex' ? 'Codex' : 'Brain') + ' <b>paused</b>: ' +
      escHtml((st.reason || '').slice(0, 120)) + '</span>'
    : '';
  if (data.brain_provider === 'codex') {
    return paused + chip('Codex', codex) + (data.claude_calls_today ? chip('Claude', data.claude_calls_today) : '');
  }
  return paused + chip('Claude', data.claude_calls_today) + (codex ? chip('Codex', codex) : '');
}

function fmtTokens(n) {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}


function emailTag(p) {
  if (!p.email) return '';
  // Fall back to the legacy boolean for rows predating email_status.
  const status = p.email_status || (p.email_verified ? 'verified' : 'guess');
  if (!own(STATUS, status)) return '';
  const m = statusMeta(status);
  return ' <span class="mini t-' + m.tone + '" title="Email ' + escHtml(m.label.toLowerCase()) + '">' +
    escHtml(m.label) + '</span>';
}

// Quota gauges for the ACTIVE provider only: Claude's are the numbers
// `/usage` shows in Claude Code; Codex's are the ChatGPT plan's limits.
// When they can't be read, say so rather than hiding the card.
function quotaCardHtml(data) {
  const provider = data && data.quota_provider;
  if (!provider) return '';
  const title = provider === 'codex' ? 'Codex (ChatGPT plan) Quota' : 'Claude Subscription Quota';
  let html = '<h2>' + escHtml(title) + '</h2>';
  if (!data.quota || !Object.keys(data.quota).length) {
    const why = provider === 'codex'
      ? "Quota unavailable: Sellbird couldn't read your ChatGPT plan's limits (check codex login, the Codex CLI version, and network)."
      : "Quota unavailable: Sellbird couldn't read your Claude subscription's limits (check claude login and network).";
    return html + '<p class="muted">' + escHtml(why) + ' Until it can, budgets count Sellbird\'s own calls.</p>';
  }
  const labels = {five_hour: '5-hour window', seven_day: 'Weekly', primary: 'Primary window',
                  secondary: 'Secondary window', limit_reached: 'Usage limit reached'};
  for (const [key, w] of Object.entries(data.quota)) {
    const pct = Math.min(100, Math.max(0, w.utilization || 0));
    const color = pct >= 80 ? 'yellow' : 'green';
    const resets = w.resets_at ? 'resets ' + formatDate(w.resets_at) : '';
    html += '<div class="progress-wrap">' +
      '<div class="progress-label">' +
        '<span class="text">' + escHtml(labels[key] || key) + (resets ? ' &middot; ' + escHtml(resets) : '') + '</span>' +
        '<span class="pct">' + pct.toFixed(0) + '%</span>' +
      '</div>' +
      '<div class="progress-bar"><div class="progress-fill ' + color + '" style="width:' + pct + '%"></div></div>' +
    '</div>';
  }
  return html;
}

async function loadUsage() {
  const data = await api('/api/usage');
  const statsEl = document.getElementById('usage-stats');
  if (!data) { statsEl.innerHTML = offlineState(); return; }

  const quotaEl = document.getElementById('usage-quota');
  const qHtml = quotaCardHtml(data);
  quotaEl.innerHTML = qHtml;
  quotaEl.style.display = qHtml ? 'block' : 'none';

  // Totals cards — usage-first (calls is the headline; tokens as chips).
  // No dollars: on a subscription plan usage isn't billed per token.
  const t = data.totals || {};
  const card = (label, p) => {
    p = p || {};
    return '<div class="stat-card"><div class="label">' + label + '</div>' +
      '<div class="value">' + (p.calls || 0) + '</div>' +
      '<div class="breakdown">' +
        '<span class="chip">calls</span>' +
        '<span class="chip">out <b>' + fmtTokens(p.output_tokens) + '</b></span>' +
        '<span class="chip">in <b>' + fmtTokens(p.input_tokens) + '</b></span>' +
        '<span class="chip">cached <b>' + fmtTokens(p.cache_read_tokens) + '</b></span>' +
      '</div></div>';
  };
  statsEl.innerHTML = card('Today', t.today) + card('Last 7 Days', t.week) + card('Last 30 Days', t.month);

  // Daily columns — output tokens per day (the work done)
  const dailyEl = document.getElementById('usage-daily');
  const days = (data.by_day || []).slice(-30);
  if (days.length) {
    const maxOut = Math.max(...days.map(d => d.output_tokens || 0), 1);
    dailyEl.innerHTML = '<h2>Output tokens per day <span class="hint muted" style="font-weight:400">last ' +
      days.length + ' days · peak ' + fmtTokens(maxOut) + '</span></h2>' +
      '<div class="chart-cols" role="img" aria-label="Output tokens per day">' + days.map(d =>
        '<span class="col" title="' + escHtml(d.day || '') + ': ' + fmtTokens(d.output_tokens) + ' out, ' +
          (d.calls || 0) + ' calls"><i style="height:' + Math.max(2, (d.output_tokens || 0) / maxOut * 100) + '%"></i></span>'
      ).join('') + '</div>' +
      '<div class="chart-axis"><span>' + escHtml(days[0].day || '') + '</span><span>' +
        escHtml(days[days.length - 1].day || '') + '</span></div>';
    dailyEl.style.display = 'block';
  } else {
    dailyEl.style.display = 'none';
  }

  // Breakdown tables — calls + tokens, no cost column
  const tablesEl = document.getElementById('usage-tables');
  const table = (title, rows, keyName) => {
    if (!rows || !rows.length) return '';
    const max = Math.max(...rows.map(r => r.calls || 0), 1);
    let h = '<div class="card flush"><div class="card-head"><h3>' + title + '</h3></div><table><thead><tr>' +
      '<th>' + keyName + '</th><th>Calls</th><th class="num">Input</th><th class="num">Output</th><th class="num">Cache read</th>' +
      '</tr></thead><tbody>';
    for (const r of rows) {
      h += '<tr><td class="cell-main">' + escHtml(String(r[keyName.toLowerCase()] || '')) + '</td>' +
        '<td><span class="meter-row"><span class="bar" style="width:120px"><span style="width:' +
          (100 * (r.calls || 0) / max) + '%;background:var(--accent)"></span></span><span class="v">' + (r.calls || 0) + '</span></span></td>' +
        '<td class="num muted">' + fmtTokens(r.input_tokens) + '</td>' +
        '<td class="num">' + fmtTokens(r.output_tokens) + '</td>' +
        '<td class="num muted">' + fmtTokens(r.cache_read_tokens) + '</td></tr>';
    }
    return h + '</tbody></table></div>';
  };

  const anyRows = (data.by_agent || []).length || (data.by_task || []).length;
  if (!anyRows) {
    tablesEl.innerHTML = emptyState(icon('usage'), 'No usage recorded yet',
      'Once Sellbird starts making model calls (Claude or Codex), every one is logged here with exact calls and tokens by provider, agent and task.');
  } else {
    const providers = data.by_provider || [];
    const showProviders = providers.length > 1 || (providers.length === 1 && providers[0].provider !== 'claude');
    tablesEl.innerHTML =
      (showProviders ? table('By Provider (30 days)', providers, 'Provider') : '') +
      table('By Agent (30 days)', data.by_agent, 'Agent') +
      table('By Task (30 days)', data.by_task, 'Task') +
      table('By Model (30 days)', data.by_model, 'Model');
  }
}

// ── Outbox: a decisions desk, not a wall of drafts ──
//
// Approving mail is a queue of one-at-a-time judgements. Showing all of them
// stacked invites a single "approve all" reflex, which is exactly the review
// the approval ladder exists to prevent. One email fills the pane; the rest
// wait in the rail.

async function loadOutbox() {
  const [data, mode] = await Promise.all([api('/api/outbox'), api('/api/sending-mode')]);
  _sendingMode = mode && !mode.error ? mode : null;
  const banner = document.getElementById('outbox-banner');
  const desk = document.getElementById('outbox-desk');
  const list = document.getElementById('outbox-list');
  const actions = document.getElementById('outbox-actions');
  if (!data) { desk.innerHTML = offlineState(); list.innerHTML = ''; return; }
  if (data.error) {
    // Never let a failed read pass for "Nothing to review".
    desk.innerHTML = emptyState(icon('alert'), 'Couldn\'t read the Outbox', escHtml(data.error));
    list.innerHTML = '';
    return;
  }

  const pending = data.pending || [];
  _desk.items = pending;
  _desk.footer = data.footer || '';
  // Keep the desk on the draft being edited even if the list shifted.
  const editingAt = _desk.editing ? pending.findIndex(p => p.id === _desk.editing) : -1;
  if (editingAt >= 0) _desk.i = editingAt;
  else if (_desk.editing) {
    _desk.editing = null;
    _desk.unsaved = null;
  }
  if (_desk.i >= pending.length) _desk.i = Math.max(0, pending.length - 1);
  navCount('nav-outbox', pending.length);

  actions.innerHTML =
    (pending.length && !data.paused
      ? (pending.length > pending.filter(p => heldReason(p)).length
        ? '<button class="btn btn-primary btn-sm" onclick="outboxApproveAll()" ' +
          'title="Emails Sellbird held are left for you to approve one by one">Approve all ' +
          (pending.length - pending.filter(p => heldReason(p)).length) + '</button>' : '') : '') +
    (data.paused
      ? '<button class="btn btn-secondary btn-sm" onclick="sendingToggle(\'resume\')">' +
        'Resume sending</button>'
      : '<button class="btn btn-secondary btn-sm" onclick="sendingToggle(\'pause\')">' +
        'Pause all sending</button>');

  // The kill switch gets a banner only when it's actually on — a permanent
  // bar for a thing that isn't happening is just noise.
  banner.innerHTML = sendingModeBanner(_sendingMode) + (data.paused
    ? '<div class="banner bad">' + icon('alert') + '<div><b>Sending is paused</b>' +
        '<p>' + escHtml(data.paused) + '. Approved mail stays queued until you resume.</p></div></div>'
    : '');
  // Only once there is mail to warn about: a fresh install sends nothing.
  const anyMail = pending.length || (data.approved || []).length || (data.sent || []).length;
  if (data.compliance_notice && anyMail) {
    banner.innerHTML +=
      '<div class="banner wait">' + icon('alert') + '<div><b>Add your postal address</b>' +
        '<p>' + escHtml(data.compliance_notice) + '</p></div></div>';
  }

  desk.innerHTML = pending.length ? renderDesk(pending, _desk.i) :
    '<div class="card">' + emptyState(icon('check'), 'Nothing to review',
      _sendingMode && _sendingMode.mode === 'autopilot'
        ? 'Autopilot is on, so Sellbird sends on its own. Anything it holds for you shows up here.'
        : 'Every draft Sellbird writes lands here first. Approve one and it sends on schedule.') +
    '</div>';

  const table = (title, rows, cols) => {
    if (!rows || !rows.length) return '';
    let h = '<div class="subhead">' + title + ' <span class="hint">' + rows.length + '</span></div>' +
      '<div class="table-card"><table class="nowrap clip"><thead><tr>' +
      cols.map(c => '<th>' + c[0] + '</th>').join('') + '</tr></thead><tbody>';
    for (const r of rows) {
      h += '<tr>' + cols.map(c => '<td' + (c[2] ? ' class="muted"' : '') + '>' +
        (c[3] ? c[1](r) : escHtml(String(c[1](r) ?? ''))) + '</td>').join('') + '</tr>';
    }
    return h + '</tbody></table></div>';
  };

  list.innerHTML =
    table('Approved &amp; scheduled', data.approved, [
      ['To', r => r.to_email], ['Step', r => r.step], ['Subject', r => r.subject],
      ['Sends', r => r.status === 'sending' ? badge(r.status) : escHtml(formatDate(r.send_at)), true, true],
    ]) +
    table('Recently sent', data.sent, [
      ['To', r => r.to_email], ['Step', r => r.step], ['Subject', r => r.subject],
      ['Sent', r => formatDate(r.sent_at), true],
    ]) +
    table('Didn\'t send', data.failed, [
      ['To', r => r.to_email], ['Status', r => badge(r.status), false, true],
      ['Reason', r => r.error, true], ['Updated', r => formatDate(r.updated_at), true],
    ]);
}

const CONTACT_SOURCES = {
  known_company_backfill: 'team page on their site',
  company_website: 'team page on their site',
  scraped_mailto: 'address published on their site',
  linkedin_search: 'LinkedIn search',
  hunter_domain_search: 'Hunter domain search',
  job_board: 'job board',
  web_search: 'web search',
};

// What the draft is built on, so every claim can be checked against it.
function renderDeskContext(ctx) {
  if (!ctx) return '';
  const row = (k, v) => v ? '<div class="k">' + k + '</div><div class="v">' + v + '</div>' : '';
  const href = safeHref(ctx.website);
  const site = href
    ? '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' +
        escHtml(ctx.website.replace(/^https?:\/\//i, '')) + ' &#8599;</a>'
    : '';
  const who = [ctx.contact, ctx.contact_title].filter(Boolean).map(escHtml).join(' &middot; ');
  const email = [ctx.email_status, ctx.score ? 'score ' + ctx.score : '']
    .filter(Boolean).map(escHtml).join(' &middot; ');
  const signals = Object.entries(ctx.signals || {})
    .map(([k, v]) => escHtml(k) + ': <b>' + escHtml(v) + '</b>').join('<br>');
  return '<div class="desk-context">' +
    row('Company', escHtml(ctx.company)) +
    row('Website', site) +
    row('About', escHtml(ctx.description)) +
    row('Contact', who) +
    row('Found via', escHtml(Object.prototype.hasOwnProperty.call(CONTACT_SOURCES, ctx.contact_source)
      ? CONTACT_SOURCES[ctx.contact_source] : ctx.contact_source)) +
    row('Email', email) +
    row('Signals', signals) +
    row('Campaign', ctx.campaign ? escHtml(ctx.campaign) +
      ' <span class="muted">(how they were found, not a fact about them)</span>' : '') +
    row('Writer notes', escHtml(ctx.notes)) +
  '</div>';
}

// ── Approval or autopilot ──
// Approval (the default): every email waits in the Outbox for you.
// Autopilot (channels.email.require_approval: false): Sellbird sends on its
// own, and still holds what gate.autopilot_review flags. Turning autopilot
// on takes typing the word; turning it off is one click and works at once.
let _sendingMode = null, _autopilotArmed = false, _modeBusy = false;

function sendingModeBanner(m) {
  if (!m) return '';
  if (m.mode === 'autopilot') {
    return '<div class="banner wait mode-banner">' + icon('alert') + '<div><b>Autopilot is on</b>' +
      '<p>Sellbird sends email on its own. It holds anything that links to a site that isn’t yours or ' +
      'quotes its own instructions; those wait here' + (m.held ? ' (' + m.held + ' now)' : '') + '.</p></div>' +
      '<button class="btn btn-secondary btn-sm" onclick="setSendingMode(\'approval\')"' +
        (_modeBusy ? ' disabled' : '') + '>Turn off autopilot</button></div>';
  }
  const instantly = m.provider === 'instantly';
  return '<div class="banner mode-banner">' + icon('check') + '<div><b>Approval mode</b>' +
    '<p>' + (instantly
      ? 'Instantly can’t hold single emails, so campaigns aren’t sent to Instantly until you turn on autopilot.'
      : 'Every email Sellbird writes waits here until you approve it.') +
    ' <a href="#" onclick="goTab(\'settings\');return false">About autopilot</a></p></div></div>';
}

function sendingModeCard(m) {
  if (!m) return '<div class="card muted">Couldn’t read the sending mode.</div>';
  const auto = m.mode === 'autopilot';
  const instantly = m.provider === 'instantly';
  const domains = (m.your_domains || []).map(escHtml).join(', ') || 'none yet';
  let html = '<div class="card mode-card"><h3>Sending mode: ' + (auto ? 'Autopilot' : 'Approval') + '</h3>' +
    '<p><b>Approval</b> (the default): every email Sellbird writes, first emails, follow-ups and replies, ' +
      'waits in the Outbox until you approve it.' +
      (instantly ? ' On Instantly, which can’t hold single emails, that means campaigns aren’t deployed.' : '') + '</p>' +
    '<p><b>Autopilot</b>: Sellbird sends on its own, on schedule, within your daily limit and quiet hours. ' +
      'Opt-outs, bounces and Pause all sending work the same. It still holds an email for you when it links ' +
      'to, or gives an address at, a site that isn’t yours, or quotes Sellbird’s own instructions: text from ' +
      'websites and replies can try to steer what it writes. The first reply after you undo an opt-out waits too.</p>' +
    '<p class="muted">Counted as yours: ' + domains + (m.booking_page ? '; your booking page ' + escHtml(m.booking_page) : '') +
      '. Add others under <code>channels.email.link_domains</code> in <code>' + escHtml(m.file) + '</code>.</p>';
  if (auto) {
    html += '<div class="convo-actions"><span class="muted">Turning it off works at once: emails autopilot ' +
      'approved that haven’t gone out come back to the Outbox.' +
      (instantly ? ' Campaigns already on Instantly keep running until you pause them there.' : '') + '</span>' +
      '<button class="btn btn-primary btn-sm" onclick="setSendingMode(\'approval\')"' +
        (_modeBusy ? ' disabled' : '') + '>Turn off autopilot</button></div>';
  } else if (_autopilotArmed) {
    html += '<div class="convo-actions mode-confirm"><span class="muted">Approve a batch or two by hand first ' +
      'and read what Sellbird writes. Type <b>autopilot</b> to turn it on. It starts from Sellbird’s next cycle.</span>' +
      '<input class="form-input" id="autopilot-confirm" autocomplete="off" aria-label="Type autopilot to confirm" ' +
        'oninput="document.getElementById(\'autopilot-go\').disabled = this.value.trim().toLowerCase() !== \'autopilot\'" ' +
        'onkeydown="if (event.key === \'Enter\' && this.value.trim().toLowerCase() === \'autopilot\') setSendingMode(\'autopilot\')">' +
      '<button class="btn btn-primary btn-sm" id="autopilot-go" disabled onclick="setSendingMode(\'autopilot\')">Turn on autopilot</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="_autopilotArmed=false;renderSendingModeCard()">Cancel</button></div>';
  } else {
    html += '<div class="convo-actions"><button class="btn btn-secondary btn-sm" ' +
      'onclick="_autopilotArmed=true;renderSendingModeCard()">Turn on autopilot…</button></div>';
  }
  return html + '</div>';
}

function renderSendingModeCard() {
  const el = document.getElementById('sending-mode-card');
  if (el) el.innerHTML = sendingModeCard(_sendingMode);
  const input = document.getElementById('autopilot-confirm');
  if (input) input.focus();
}

async function loadSendingModeCard() {
  const m = await api('/api/sending-mode');
  _sendingMode = m && !m.error ? m : null;
  renderSendingModeCard();
}

async function setSendingMode(mode) {
  if (_modeBusy) return;
  const body = {mode};
  if (mode === 'autopilot') {
    const input = document.getElementById('autopilot-confirm');
    body.confirm = input ? input.value.trim().toLowerCase() : '';
  }
  _modeBusy = true;
  document.querySelectorAll('.mode-banner button, .mode-card button').forEach(b => { b.disabled = true; });
  try {
    const res = await api('/api/sending-mode', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    if (res && res.success) {
      _autopilotArmed = false;
      showToast(mode === 'autopilot'
        ? 'Autopilot is on from Sellbird’s next cycle. Held emails still wait for you.'
        : 'Autopilot is off. Every email waits for you' +
          (res.requeued ? '; ' + res.requeued + ' queued by autopilot came back to the Outbox.' : '.') +
          (_sendingMode && _sendingMode.provider === 'instantly'
            ? ' Campaigns already on Instantly keep running: pause them in Instantly.' : ''), 'success');
    } else {
      showToast(failMsg(res, 'Couldn’t change the sending mode.'), 'error');
    }
  } finally {
    _modeBusy = false;
  }
  loadSendingModeCard();
  loadOutbox();
}

// Why autopilot sent this back for a human (sender: gate.autopilot_review).
const HELD_PREFIX = 'held for your review: ';   // state.HELD_FOR_REVIEW
function heldReason(item) {
  const e = String((item && item.error) || '');
  const why = e.startsWith(HELD_PREFIX) ? e.slice(HELD_PREFIX.length) : '';
  return why.charAt(0).toUpperCase() + why.slice(1);
}

function renderDesk(items, i) {
  const cur = items[i];
  const rail = items.map((it, n) => {
    const who = it.context && it.context.contact ? it.context.contact : it.to_email;
    const parts = String(who || '').split(/[\s@.]+/);
    return '<div class="desk-item ' + (n === i ? 'active' : '') + '" onclick="deskGo(' + n + ')">' +
      '<span class="avatar">' + initials(parts[0], parts[1]) + '</span>' +
      '<div class="di-text"><div class="to">' + escHtml(who) +
        (heldReason(it) ? ' <span class="tag t-bad" title="Sellbird held this instead of sending it">Held</span>' : '') +
        '</div>' +
      '<div class="sub">' + escHtml(it.subject || '(no subject)') + '</div></div>' +
    '</div>';
  }).join('');

  const editing = _desk.editing === cur.id;
  const unsaved = (editing && _desk.unsaved) || {subject: cur.subject || '', body: cur.body || ''};
  // A reply keeps its subject: Gmail threads by it.
  const fixedSubject = cur.kind === 'reply'
    ? ' readonly title="A reply keeps its subject so it stays in the thread"' : '';
  const draft = editing
    ? '<input id="desk-subject" class="desk-edit-subject" value="' + escHtml(unsaved.subject) + '"' +
        fixedSubject + ' oninput="deskKeep()">' +
      '<textarea id="desk-body" class="desk-edit-body" rows="14" oninput="deskKeep()">' +
        escHtml(unsaved.body) + '</textarea>'
    : '<div class="subject">' + escHtml(cur.subject || '(no subject)') + '</div>' +
      '<div class="body">' + escHtml(cur.body) + '</div>';
  // Cold email leaves with the footer underneath; show it as it will go out.
  // Not added again when the body already ends with it, as in the Sender.
  const added = cur.kind === 'sequence' && !(cur.body || '').trimEnd().endsWith(_desk.footer)
    ? _desk.footer : '';
  const footer = added
    ? '<div class="desk-footer" title="Added when it sends (compliance in sellbird.yaml)">' +
        escHtml(added) + '</div>' : '';
  const actions = editing
    ? '<button class="btn btn-primary" onclick="deskSave(' + jsq(cur.id) + ')">Save</button>' +
      '<button class="btn btn-secondary" onclick="deskEdit(null)">Cancel</button>'
    : '<button class="btn btn-primary" onclick="outboxAct(' + jsq(cur.id) + ',\'approve\')">' +
        'Approve <kbd>A</kbd></button>' +
      '<button class="btn btn-secondary" onclick="deskEdit(' + jsq(cur.id) + ')">' +
        'Edit <kbd>E</kbd></button>' +
      '<button class="btn btn-secondary" onclick="outboxAct(' + jsq(cur.id) + ',\'reject\')">' +
        'Reject <kbd>R</kbd></button>';

  return '<div class="desk">' +
    '<div class="desk-list">' + rail + '</div>' +
    '<div class="desk-pane">' +
      '<div class="to-line">To <b>' + escHtml(cur.to_email) + '</b>' +
        '<span class="tag">' + (cur.kind === 'reply' ? 'Reply' : 'Email ' + escHtml(String(cur.step))) + '</span>' +
        '<span class="muted">sends ' + formatDate(cur.send_at) + '</span></div>' +
      renderDeskContext(cur.context) +
      (heldReason(cur)
        ? '<div class="banner wait desk-held">' + icon('alert') + '<div><b>Sellbird held this instead of sending it</b>' +
          '<div>' + escHtml(heldReason(cur)) + '. Read it before approving: text from a website or an ' +
          'email can try to steer what Sellbird writes.</div></div></div>'
        : '') +
      draft + footer +
      '<div class="desk-actions">' + actions +
        '<span class="muted" style="font-size:12px;margin-left:auto">' +
          (i + 1) + ' of ' + items.length + '</span>' +
      '</div>' +
    '</div></div>';
}

function deskEdit(id) {
  _desk.editing = id;
  _desk.unsaved = null;
  document.getElementById('outbox-desk').innerHTML = renderDesk(_desk.items, _desk.i);
  if (id) document.getElementById('desk-body').focus();
}

// What's typed survives a re-render (switching tabs and back reloads the desk).
function deskKeep() {
  _desk.unsaved = {
    subject: document.getElementById('desk-subject').value,
    body: document.getElementById('desk-body').value,
  };
}

// While a draft is being edited, nothing else on the desk may act on the
// stored version of it: Save or Cancel first.
function deskBusy() {
  if (!_desk.editing) return false;
  showToast('Save or cancel your edit first.', 'error');
  return true;
}

async function deskSave(id) {
  const subject = document.getElementById('desk-subject').value.trim();
  const body = document.getElementById('desk-body').value.trim();
  if (!subject || !body) { showToast('Subject and body are both required.', 'error'); return; }
  let data = null;
  try {
    const r = await authFetch('/api/outbox/' + encodeURIComponent(id) + '/edit', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({subject: subject, body: body}),
    });
    if (await lockIfRefused(r)) return;
    data = await r.json();
  } catch {}
  if (data && data.success) {
    _desk.editing = null;
    _desk.unsaved = null;
    showToast('Saved. Still waiting for your approval.', 'success');
    loadOutbox();
  } else {
    showToast((data && data.message) || 'Save failed.', 'error');
  }
}

function deskGo(n) {
  if (n < 0 || n >= _desk.items.length || deskBusy()) return;
  _desk.i = n;
  document.getElementById('outbox-desk').innerHTML = renderDesk(_desk.items, n);
}

// Keyboard review. Ignored while typing into a field, so Settings still works.
document.addEventListener('keydown', e => {
  if (currentTab !== 'outbox' || e.metaKey || e.ctrlKey || e.altKey) return;
  // Mid-edit, A would approve the stored draft, not what's typed. Only
  // Escape (cancel) works until the edit is saved or cancelled.
  if (_desk.editing) {
    const typing = /^(input|textarea)$/i.test(e.target.tagName || '');
    if (e.key === 'Escape') { e.preventDefault(); deskEdit(null); }
    else if (!typing && 'jkare'.includes(e.key.toLowerCase())) { e.preventDefault(); deskBusy(); }
    return;
  }
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || e.target.isContentEditable) return;
  const cur = _desk.items[_desk.i];
  const k = e.key.toLowerCase();
  if (k === 'j') { e.preventDefault(); deskGo(_desk.i + 1); }
  else if (k === 'k') { e.preventDefault(); deskGo(_desk.i - 1); }
  else if (k === 'a' && cur) { e.preventDefault(); outboxAct(cur.id, 'approve'); }
  else if (k === 'r' && cur) { e.preventDefault(); outboxAct(cur.id, 'reject'); }
  else if (k === 'e' && cur) { e.preventDefault(); deskEdit(cur.id); }
});

async function outboxAct(id, action) {
  if (deskBusy()) return;
  const data = await api('/api/outbox/' + encodeURIComponent(id) + '/' + action, {method: 'POST'});
  if (data && data.success) showToast(action === 'approve' ? 'Approved — will send on schedule.' : 'Rejected.', 'success');
  else showToast(failMsg(data, 'Action failed.'), 'error');
  loadOutbox();
}

async function outboxApproveAll() {
  if (deskBusy()) return;
  const data = await api('/api/outbox/approve-all', {method: 'POST'});
  if (data && data.success) showToast('Approved ' + data.approved + ' email(s).' + (data.held
    ? ' ' + data.held + ' Sellbird held ' + (data.held === 1 ? 'is' : 'are') + ' left for you to read and approve one by one.'
    : ''), 'success');
  else showToast(failMsg(data, 'Approve-all failed.'), 'error');
  loadOutbox();
}

async function sendingToggle(action) {
  const data = await api('/api/sending/' + action, {method: 'POST'});
  if (data && data.success) showToast(action === 'pause' ? 'Sending paused.' : 'Sending resumed.', 'success');
  else showToast(failMsg(data, action === 'pause' ? 'Could not pause sending.' : 'Could not resume sending.'), 'error');
  loadOutbox();
}

const COMPANIES_PER_PAGE = 50;
let _companiesPage = 0;

async function loadCompanies() {
  companyDrill = false;
  const el = document.getElementById('companies-list');
  const data = await api('/api/companies');
  if (!data) { el.innerHTML = offlineState(); return; }
  _companies = data;
  if (!data.length) {
    el.innerHTML = emptyState(icon('companies'), 'No companies yet',
      'Confirm your <b>Signals</b>, then find businesses on the <b>Discover</b> tab. ' +
      'OpenStreetMap is free and needs no account.');
    return;
  }
  renderCompaniesPage(_companiesPage);
}

function renderCompaniesPage(page) {
  const el = document.getElementById('companies-list');
  const data = _companies;
  const pages = Math.max(1, Math.ceil(data.length / COMPANIES_PER_PAGE));
  _companiesPage = Math.min(Math.max(0, page), pages - 1);
  const start = _companiesPage * COMPANIES_PER_PAGE;
  let html = '<div class="table-card"><table class="nowrap"><thead><tr><th>Company</th>' +
    '<th title="Company fit, 0-100: sector, decision maker, reachability, buying signal. Hover a score for why.">Fit</th>' +
    '<th title="Hover a status for why the company was or wasn\'t contacted">Status</th>' +
    '<th>Industry</th><th>Location</th><th class="num">Contacts</th><th>Added</th></tr></thead><tbody>';
  data.slice(start, start + COMPANIES_PER_PAGE).forEach((c, j) => {
    const i = start + j;
    const website = c.website || (c.domain ? 'https://' + c.domain : '');
    const domain = safeHref(website)
      ? '<a href="' + safeHref(website) + '" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">' +
        escHtml(c.domain || website.replace(/^https?:\/\//i, '')) + '</a>'
      : escHtml(c.domain);
    html += '<tr class="clickable" onclick="showCompanyContacts(' + i + ')">' +
      '<td><div class="cell-stack"><span class="cell-main">' + escHtml(c.name) + '</span>' +
        '<span class="cell-sub">' + domain + '</span></div></td>' +
      '<td title="' + escHtml((c.fit_reasons || []).join(' · ')) + '">' +
        (typeof c.fit === 'number' ? meterHtml(c.fit) : '') + '</td>' +
      '<td title="' + escHtml(c.register_why || '') + '">' + escHtml(c.register_status || '') + '</td>' +
      '<td>' + (c.industry ? '<span class="tag">' + escHtml(c.industry) + '</span>' : '') + '</td>' +
      '<td>' + escHtml(c.location) + '</td>' +
      '<td class="num">' + (c.contact_count || 0) + '</td>' +
      '<td class="muted">' + relTime(c.created_at) + '</td></tr>';
  });
  const pager = pages > 1
    ? ' &nbsp; <button class="btn btn-ghost btn-sm" ' + (_companiesPage === 0 ? 'disabled ' : '') +
        'onclick="renderCompaniesPage(' + (_companiesPage - 1) + ')">&larr; Prev</button> ' +
      'Page ' + (_companiesPage + 1) + ' of ' + pages +
      ' <button class="btn btn-ghost btn-sm" ' + (_companiesPage >= pages - 1 ? 'disabled ' : '') +
        'onclick="renderCompaniesPage(' + (_companiesPage + 1) + ')">Next &rarr;</button>'
    : '';
  el.innerHTML = html + '</tbody></table></div>' +
    '<div class="table-foot">' + (start + 1) + '–' + Math.min(start + COMPANIES_PER_PAGE, data.length) +
    ' of ' + data.length + ' compan' + (data.length === 1 ? 'y' : 'ies') + pager + '</div>';
}

// Basic facts about one company, above its contacts: where it came from,
// why it fits, what's been done, and what Sellbird saw on its own site.
function companyInfoCard(company, profile) {
  const c = (profile && profile.company) || company;
  const reg = (profile && profile.register) || {};
  const obs = (profile && profile.observations) || [];
  const link = (url, text) => safeHref(url)
    ? '<a href="' + safeHref(url) + '" target="_blank" rel="noopener noreferrer">' + escHtml(text || url) + '</a>'
    : escHtml(text || url || '');
  const website = c.website || (c.domain ? 'https://' + c.domain : '');
  const row = (k, v) => v ? '<tr><td class="muted" style="width:170px;vertical-align:top">' + k +
    '</td><td style="white-space:normal">' + v + '</td></tr>' : '';
  const latest = {};
  for (const o of obs) {
    const key = o.signal_code + '|' + (o.value_text || o.value_num);
    if (!latest[key] && (o.value_text || o.value_num !== null)) latest[key] = o;
  }
  const by = code => Object.values(latest).filter(o => o.signal_code === code);
  const list = (items, max) => items.slice(0, max).map(o =>
    (o.evidence_url ? link(o.evidence_url, o.value_text || String(o.value_num)) : escHtml(o.value_text || String(o.value_num)))
  ).join('<br>');
  const people = by('DECISION_MAKER_TITLE').length ? by('DECISION_MAKER_TITLE') : by('CONTACT_FOUND');
  const form = by('CONTACT_FORM_URL')[0];
  const shown = new Set(['DECISION_MAKER_TITLE', 'CONTACT_FOUND', 'CONTACT_FORM_URL', 'HIRING_ROLE', 'FOUND_IN_SERP', 'TECH_STACK']);
  const other = Object.values(latest).filter(o => !shown.has(o.signal_code));
  let html = '<div class="card" style="margin-bottom:16px"><div class="card-head"><h3>' + escHtml(c.name || c.domain) + '</h3>' +
    '<span class="meta">' + (typeof company.fit === 'number' ? 'Fit ' + company.fit + ' / 100' : '') + '</span></div>' +
    '<table><tbody>' +
    row('Website', link(website, c.domain || website)) +
    row('Sector', escHtml(c.industry)) +
    row('Area', escHtml(c.location)) +
    row('Why selected', escHtml(reg.why_selected || (company.fit_reasons || []).join(' · '))) +
    row('Status', '<b>' + escHtml(reg.status || company.register_status || '') + '</b>' +
      (reg.why_not_contacted || company.register_why ? ' — ' + escHtml(reg.why_not_contacted || company.register_why) : '')) +
    row('People named on site', list(people, 8)) +
    row('Contact form', form ? link(form.value_text, form.value_text) : '') +
    row('Hiring', list(by('HIRING_ROLE'), 6)) +
    row('Tech on website', escHtml(by('TECH_STACK').map(o => o.value_text).slice(0, 1).join(''))) +
    row('Other signals', other.slice(0, 8).map(o => escHtml(o.label) + ': ' +
      escHtml(o.value_text || String(o.value_num))).join('<br>')) +
    row('Found via', c.source_url ? link(c.source_url, 'search result') + ' · ' + escHtml(c.source || '') : escHtml(c.source || '')) +
    row('First found', escHtml((c.created_at || '').slice(0, 10))) +
    '</tbody></table></div>';
  return html;
}

async function showCompanyContacts(index) {
  const company = _companies[index];
  if (!company) return;
  companyDrill = true;
  const el = document.getElementById('companies-list');
  const [data, profile] = await Promise.all([
    api('/api/companies/' + encodeURIComponent(company.id) + '/contacts'),
    api('/api/companies/' + encodeURIComponent(company.id) + '/profile'),
  ]);
  let html = '<button class="btn btn-ghost btn-sm" onclick="loadCompanies()" style="margin-bottom:12px">' +
    '&larr; All companies</button>' + companyInfoCard(company, profile) +
    '<div class="card flush"><div class="card-head"><h3>Contacts</h3>' +
    '<span class="meta">' + escHtml([company.industry, company.location].filter(Boolean).join(' · ')) + '</span></div>';
  if (!data || !data.length) {
    html += '<div class="empty"><div class="copy">No contacts found at this company yet.</div></div></div>';
  } else {
    html += '<table><thead><tr><th>Name</th><th>Email</th><th>Phone</th><th>LinkedIn</th><th>Status</th><th>Source</th></tr></thead><tbody>';
    for (const p of data) {
      const phoneIcon = p.phone_verified ? ' <span class="muted">&#10003;</span>' : '';
      html += '<tr><td><div class="cell-row"><span class="avatar">' + initials(p.first_name, p.last_name) + '</span>' +
          '<div class="cell-stack"><span class="cell-main">' + escHtml(p.first_name) + ' ' + escHtml(p.last_name) + '</span>' +
          '<span class="cell-sub">' + escHtml(p.title) + '</span></div></div></td>' +
        '<td>' + escHtml(p.email) + emailTag(p) + '</td>' +
        '<td>' + escHtml(p.phone) + phoneIcon + '</td>' +
        '<td>' + (safeHref(p.linkedin_url) ? '<a href="' + safeHref(p.linkedin_url) + '" target="_blank" rel="noopener noreferrer">Profile</a>' : '') + '</td>' +
        '<td>' + badge(p.status) + '</td><td class="muted">' + sourceLabel(p.source) + '</td></tr>';
    }
    html += '</tbody></table></div>';
  }
  el.innerHTML = html;
}

async function submitFeedback(entityType, entityId, promptText) {
  const comment = prompt(promptText || 'Add your feedback:');
  if (!comment) return;
  const data = await api('/api/feedback', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({entity_type: entityType, entity_id: entityId, comment: comment})
  });
  if (data && data.success) showToast(entityType === 'campaign'
    ? 'Saved. Sellbird follows it in the emails it writes from now on.'
    : 'Saved. Sellbird uses it if it writes this person\'s first email (Gmail, SMTP or Microsoft 365).', 'success');
  else showToast((data && data.message) || 'Could not save feedback.', 'error');
}

function fbProspect(i) {
  const p = _prospects[i];
  if (p) submitFeedback('contact', p.id, 'Anything Sellbird should know before writing to this person?');
}

function fbCampaign(i) {
  const c = _campaigns[i];
  if (c) submitFeedback('campaign', c.id, 'What should Sellbird do differently in the emails it writes next? (e.g. "shorter; don\'t mention reviews")');
}

// The lead view. One row holds everything known about a person: who they
// are, the reviewer's fit, what the research found, whether it held up, the
// angle, the draft, and where they stand in the pipeline. /api/leads joins
// Apollo's record to the pipeline; this draws it.
//
// Search, filters and sort live outside the table, so the 15-second refresh
// re-renders rows without losing what you typed.
let _contactQuery = '', _contactFilter = 'all';
let _leadSeg = '', _leadFit = '', _leadIntent = '';
let _leadSummary = {}, _leadSegments = [], _leadSort = '', _leadDir = 1, _leadOpen = '';
const CONTACT_FILTERS = [
  ['all', 'All', null],
  ['todo', 'To write', ['new', 'queued']],
  ['contacted', 'Contacted', ['contacted']],
  ['replied', 'Replied', ['replied', 'interested', 'meeting', 'accepted_offer']],
  ['fetched', 'Not in the pipeline', ['fetched']],
  ['closed', 'Closed out', null],
];
// Best first. A blank fit sits above a ruled-out one: "nobody has reviewed
// this yet" is worth more than "we looked and said no".
const FIT_ORDER = {a: 0, b: 1, '': 2, skip: 3};
const INTENT_ORDER = {high: 0, medium: 1, low: 2, unknown: 3, '': 4};
// Eight columns is what fits beside the sidebar without scrolling. The
// segment rides on the company cell rather than taking a column of its own,
// and everything else about a lead is one click away in the dossier.
// Eight columns is what fits beside the sidebar. The widths are fixed and
// add up to a hundred, so the table never scrolls sideways however long a
// job title or an angle runs: the cell shows what fits and the dossier
// under it shows all of it. The segment rides on the company cell.
const LEAD_COLS = [
  ['fit', 'Fit', '7%'], ['intent', 'Intent', '8%'], ['name', 'Name', '20%'],
  ['company', 'Company', '17%'], ['email', 'Email', '18%'],
  ['angle', 'Why they would buy', '20%'], ['status', 'Stands at', '10%'],
];

async function loadProspects() {
  const el = document.getElementById('prospects-table');
  const data = await api('/api/leads');
  if (!data || data.error) { el.innerHTML = offlineState(); return; }
  _prospects = data.rows || [];
  _leadSummary = data.summary || {};
  _leadSegments = data.segments || [];
  renderLeadStats();
  renderLeadSegments();
  if (!_prospects.length) {
    el.innerHTML = emptyState(icon('contacts'), 'No contacts yet',
      'Nothing has been fetched yet. The <a href="#" onclick="goTab(\'discover\');return false">Apollo tab</a> ' +
      'searches for free and stages what it finds, and the Scout agent finds people on the web once Sellbird is running. ' +
      'Already have a list? <a href="#" onclick="openImportContacts();return false">Add contacts</a>.');
    return;
  }
  if (!document.getElementById('contact-rows')) {
    el.innerHTML = leadToolbar();
    // A rebuild after an offline blip must show the search and filters still applied.
    leadSetValue('contact-search', _contactQuery);
    leadSetValue('lead-seg', _leadSeg);
    leadSetValue('lead-fit', _leadFit);
    leadSetValue('lead-intent', _leadIntent);
  }
  renderContacts();
}

function leadSetValue(id, value) {
  const el = document.getElementById(id);
  if (el) el.value = value || '';
}

function leadToolbar() {
  const segs = _leadSegments.map(s =>
    '<option value="' + escHtml(s.key) + '">' + escHtml(s.label || s.key) + '</option>').join('');
  return '<div class="toolbar">' +
    '<label class="search">' + icon('discover') +
      '<input class="form-input" id="contact-search" type="search" ' +
      'placeholder="Search name, company, title, angle" aria-label="Search contacts" ' +
      'oninput="_contactQuery=this.value;renderContacts()"></label>' +
    (segs ? '<select class="form-input pick" id="lead-seg" aria-label="Filter by segment" ' +
      'onchange="_leadSeg=this.value;renderContacts()">' +
      '<option value="">Every segment</option>' + segs + '</select>' : '') +
    '<select class="form-input pick" id="lead-fit" aria-label="Filter by fit" ' +
      'onchange="_leadFit=this.value;renderContacts()">' +
      '<option value="">Any fit</option><option value="a">Strong only</option>' +
      '<option value="b">Fit only</option><option value="none">Unreviewed</option>' +
      '<option value="skip">Ruled out</option></select>' +
    '<select class="form-input pick" id="lead-intent" aria-label="Filter by intent" ' +
      'onchange="_leadIntent=this.value;renderContacts()">' +
      '<option value="">Any intent</option><option value="buying">Showing intent</option>' +
      '<option value="high">High only</option><option value="none">Not researched yet</option>' +
      '</select>' +
    '</div>' +
    '<div class="segmented" id="contact-filters" role="group" aria-label="Filter by where they stand"></div>' +
    '<div id="contact-rows"></div>';
}

// The figures above the table. Every one is a count of the rows below it, so
// any of them can be checked by filtering.
function renderLeadStats() {
  const el = document.getElementById('lead-stats');
  if (!el) return;
  const s = _leadSummary;
  const n = v => Number(v || 0).toLocaleString();
  const tile = (label, value, notes) =>
    '<div class="stat-card"><div class="label">' + label + '</div>' +
    '<div class="value">' + value + '</div>' +
    (notes && notes.length ? '<div class="breakdown">' + notes.filter(Boolean).map(t =>
      '<span class="chip">' + t + '</span>').join('') + '</div>' : '') + '</div>';
  el.innerHTML = '<div class="stats-grid">' +
    tile('Contacts', n(s.contacts), [
      '<b>' + n(s.with_email) + '</b> with an address',
      '<b>' + n(s.in_pipeline) + '</b> in the pipeline']) +
    tile('Verified emails', n(s.verified),
      [Number(s.with_email) ? 'of ' + n(s.with_email) + ' addresses' : '']) +
    tile('Strong fits', n(s.strong),
      [Number(s.ruled_out) ? '<b>' + n(s.ruled_out) + '</b> ruled out' : '']) +
    tile('Showing buying intent', n(s.intent),
      ['of <b>' + n(s.researched) + '</b> researched',
       Number(s.checked) ? '<b>' + n(s.checked) + '</b> fact-checked' : '']) +
    tile('First-touch drafts', n(s.drafted), ['written, none sent from here']) +
    tile('Apollo credits spent', n(s.credits_used), ['one credit, one contact']) +
    '</div>';
}

// Where the credits have gone, and what each segment is actually returning.
function renderLeadSegments() {
  const el = document.getElementById('lead-segments');
  if (!el) return;
  const rows = _leadSegments.filter(s => Number(s.contacts) > 0);
  if (!rows.length) { el.innerHTML = ''; return; }
  const max = Math.max.apply(null, rows.map(s => Number(s.contacts) || 0)) || 1;
  el.innerHTML = '<div class="card"><div class="card-head"><h3>Leads by segment</h3>' +
    '<span class="meta">Strong fits in green</span></div>' +
    '<div class="lead-bars">' + rows.map(s => {
      const strong = Number(s.strong) || 0, total = Number(s.contacts) || 0;
      return '<div class="lead-bar"><span class="k" title="' + escHtml(s.label || s.key) + '">' +
        escHtml(s.label || s.key) + '</span>' +
        '<span class="bar" role="img" aria-label="' + strong + ' strong of ' + total + '">' +
          '<span class="c-good" style="width:' + (100 * strong / max) + '%"></span>' +
          '<span class="c-accent-soft" style="width:' + (100 * (total - strong) / max) + '%"></span>' +
        '</span>' +
        '<span class="v">' + total + ' <small>' + strong + ' strong</small></span></div>';
    }).join('') + '</div></div>';
}

function contactMatches(p, f) {
  const known = ['new', 'queued', 'contacted', 'replied', 'interested', 'meeting',
                 'accepted_offer', 'fetched'];
  if (f === 'all') return true;
  if (f === 'closed') return !known.includes(p.status);
  const hit = CONTACT_FILTERS.find(x => x[0] === f);
  return !!(hit && hit[2] && hit[2].includes(p.status));
}

function leadMatches(p) {
  if (_leadSeg && p.segment !== _leadSeg) return false;
  const fit = String(p.fit || '').toLowerCase();
  if (_leadFit === 'none') { if (fit) return false; }
  else if (_leadFit && fit !== _leadFit) return false;
  const intent = String(p.intent || '').toLowerCase();
  if (_leadIntent === 'none') { if (intent) return false; }
  else if (_leadIntent === 'buying') { if (intent !== 'high' && intent !== 'medium') return false; }
  else if (_leadIntent && intent !== _leadIntent) return false;
  const q = _contactQuery.trim().toLowerCase();
  if (q && ![p.name, p.company, p.title, p.email, p.angle, p.fit_reason, p.signals,
             p.segment_label].join(' ').toLowerCase().includes(q)) return false;
  return true;
}

function setContactFilter(f) { _contactFilter = f; renderContacts(); }

// Clicking a heading sorts by it; clicking it again reverses. No sort means
// the order the server sent: fit, then intent, then the fact check, then score.
function sortLeads(key) {
  if (_leadSort === key) {
    if (_leadDir === -1) { _leadSort = ''; _leadDir = 1; } else _leadDir = -1;
  } else { _leadSort = key; _leadDir = 1; }
  renderContacts();
}

function leadSortKey(p) {
  const low = v => String(v === undefined || v === null ? '' : v).toLowerCase();
  switch (_leadSort) {
    case 'fit': { const v = own(FIT_ORDER, low(p.fit)); return v === undefined ? 9 : v; }
    case 'intent': { const v = own(INTENT_ORDER, low(p.intent)); return v === undefined ? 9 : v; }
    case 'segment': return low(p.segment_label || p.segment);
    case 'status': return low(statusMeta(p.status).label);
    case 'name': return low(p.name);
    default: return low(p[_leadSort]);
  }
}

function leadKey(p) { return String(p.apollo_id || p.id || p.email || ''); }

function toggleLead(key) {
  _leadOpen = _leadOpen === key ? '' : key;
  renderContacts();
}

function renderContacts() {
  const rowsEl = document.getElementById('contact-rows');
  const filtersEl = document.getElementById('contact-filters');
  if (!rowsEl) return;
  filtersEl.innerHTML = CONTACT_FILTERS.map(f =>
    '<button class="' + (f[0] === _contactFilter ? 'on' : '') + '" aria-pressed="' +
      (f[0] === _contactFilter) + '" onclick="setContactFilter(' + jsq(f[0]) + ')">' +
      f[1] + ' <span class="n">' +
      _prospects.filter(p => contactMatches(p, f[0]) && leadMatches(p)).length +
      '</span></button>').join('');

  let rows = _prospects.map((p, i) => [p, i])
    .filter(([p]) => contactMatches(p, _contactFilter) && leadMatches(p));
  if (_leadSort) {
    rows.sort((a, b) => {
      const x = leadSortKey(a[0]), y = leadSortKey(b[0]);
      return (x < y ? -1 : x > y ? 1 : a[1] - b[1]) * _leadDir;
    });
  }
  if (!rows.length) {
    rowsEl.innerHTML = '<div class="table-card">' + emptyState(icon('discover'), 'No matches',
      'Nothing matches that search and those filters.') + '</div>';
    return;
  }
  const arrow = _leadDir === 1 ? ' ↑' : ' ↓';
  let html = '<div class="table-card"><table class="nowrap lead-table"><colgroup>' +
    LEAD_COLS.map(c => '<col style="width:' + c[2] + '">').join('') +
    '</colgroup><thead><tr>' +
    LEAD_COLS.map(c => '<th class="sortable"' +
      (_leadSort === c[0] ? ' aria-sort="' + (_leadDir === 1 ? 'ascending' : 'descending') + '"' : '') +
      '><button type="button" onclick="sortLeads(' + jsq(c[0]) + ')">' + c[1] +
      (_leadSort === c[0] ? arrow : '') + '</button></th>').join('') +
    '</tr></thead><tbody>';
  for (const [p, i] of rows) {
    const key = leadKey(p);
    const open = _leadOpen === key;
    html += '<tr class="lead-row' + (open ? ' on' : '') + '" tabindex="0" ' +
        'onclick="toggleLead(' + jsq(key) + ')" ' +
        'onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();toggleLead(' + jsq(key) + ')}" ' +
        'aria-expanded="' + open + '">' +
      '<td>' + leadPill(p.fit_label, p.fit_tone) + '</td>' +
      '<td>' + leadPill(p.intent_label, p.intent_tone) + '</td>' +
      '<td><div class="cell-row"><span class="avatar">' + initials(p.first_name, p.last_name) + '</span>' +
        '<div class="cell-stack"><span class="cell-main">' + escHtml(p.name || '(no name)') + '</span>' +
        '<span class="cell-sub">' + escHtml(p.title) + '</span></div></div></td>' +
      '<td><div class="cell-stack"><span class="cell-main">' + escHtml(p.company) + '</span>' +
        '<span class="cell-sub" title="' + escHtml(p.segment_label || p.segment) + '">' +
        escHtml([p.segment_short || p.segment_label,
          p.employees ? Number(p.employees).toLocaleString() + ' staff' : ''
        ].filter(Boolean).join(' · ')) + '</span></div></td>' +
      '<td><div class="cell-stack"><span>' + escHtml(p.email) + '</span>' +
        '<span class="cell-sub">' + emailTag(p).trim() + '</span></div></td>' +
      '<td class="lead-angle"><span class="clamp">' + escHtml(p.angle) + '</span></td>' +
      '<td class="lead-stands" title="' + escHtml(statusMeta(p.status).label) + '">' +
        badge(p.status) + '</td></tr>';
    if (open) html += '<tr class="detail-row"><td colspan="' + LEAD_COLS.length + '">' +
      leadDossier(p, i) + '</td></tr>';
    if (p.id && _optOut.id === p.id) html += '<tr class="detail-row"><td colspan="' +
      LEAD_COLS.length + '">' + optOutPanel(p) + '</td></tr>';
  }
  rowsEl.innerHTML = html + '</tbody></table></div>' +
    '<div class="table-foot">' + rows.length + ' of ' + _prospects.length + ' contacts</div>';
}

function leadPill(label, tone) {
  if (!label) return '<span class="muted">—</span>';
  return '<span class="badge t-' + (tone || 'idle') + '">' + escHtml(label) + '</span>';
}

// Everything on one lead, in the order a person reads it: why they would
// buy, then who they are, then what has been written, then where they stand.
function leadDossier(p, i) {
  const rows = pairs => {
    const live = pairs.filter(x => x[1] !== '' && x[1] !== undefined && x[1] !== null);
    if (!live.length) return '<p class="desc">Nothing recorded yet.</p>';
    return '<dl class="lead-dl">' + live.map(x =>
      '<dt>' + x[0] + '</dt><dd>' + x[1] + '</dd>').join('') + '</dl>';
  };
  const t = v => escHtml(v === undefined || v === null ? '' : String(v));
  const link = safeHref(p.linkedin_url);
  const check = p.check_label
    ? '<span class="mini t-' + (p.check_tone || 'idle') + '">' + escHtml(p.check_label) + '</span>' +
      (p.verify_notes ? ' ' + t(p.verify_notes) : '')
    : '';
  const why = rows([
    ['Angle', t(p.angle)],
    ['Why it fits', t(p.fit_reason)],
    ['Buying signals', t(p.signals)],
    ['Red flags', t(p.red_flags)],
    ['Fact check', check],
    ['Buying state', t(p.buying_label)],
    ['Account tier', p.account_tier ? t(p.account_tier) + ' — worth two or three contacts' : ''],
    ['Open roles', p.open_roles ? t(p.open_roles) + (p.hiring_roles ? ' · ' + t(p.hiring_roles) : '') : ''],
    ['Tech stack', t(p.tech_stack)],
  ]);
  const who = rows([
    ['Role', t([p.title, p.seniority].filter(Boolean).join(' · '))],
    ['Company', t(p.company) + (p.domain ? ' · ' + t(p.domain) : '')],
    ['Staff', p.employees ? t(Number(p.employees).toLocaleString()) : ''],
    ['Industry', t(p.industry)],
    ['Where', t(p.location)],
    ['Email', p.email ? t(p.email) + emailTag(p) : 'none found'],
    ['Phone', p.phone ? t(p.phone) + (p.phone_verified ? ' ✓' : '') : ''],
    ['LinkedIn', link ? '<a href="' + link + '" target="_blank" rel="noopener noreferrer">Profile</a>' : ''],
    ['Segment', t(p.segment_label || p.segment)],
    ['Score', p.score ? meterHtml(p.score) + ' <span class="muted">' + Number(p.score) + '</span>' : ''],
    ['Found', t(sourceLabel(p.source)) + (p.fetched_at ? ' · ' + t(relTime(p.fetched_at)) : '')],
  ]);
  const stands = rows([
    ['Status', badge(p.status) + (p.in_pipeline ? '' : ' <span class="muted">fetched, never promoted</span>')],
    ['Stage', p.stage ? badge(p.stage) : ''],
    ['Emails sent', p.sent_count ? Number(p.sent_count) : ''],
    ['Last touch', p.last_touch ? t(relTime(p.last_touch)) : ''],
    ['Next send', p.next_send ? t(formatDate(p.next_send)) : ''],
    // personalization_notes is the angle, fit reason, signals and red flags
    // squashed into one string for the Writer. Printing it under a panel
    // that already shows all four is noise, so it only appears for a
    // contact that has none of them.
    ['Notes', (p.angle || p.fit_reason || p.signals) ? '' : t(p.notes)],
  ]);
  const draft = p.draft_subject || p.draft_body
    ? '<div class="lead-block"><h4>First-touch draft <span class="muted">never sent from here</span></h4>' +
      (p.draft_subject ? '<p class="lead-subject">' + t(p.draft_subject) + '</p>' : '') +
      (p.draft_body ? '<div class="lead-draft">' + t(p.draft_body) + '</div>' : '') + '</div>'
    : '';
  const later = [['Call brief', p.call_brief], ['Call notes', p.call_notes],
                 ['Deal check', p.deal_check], ['Proposal draft', p.proposal_draft]]
    .filter(x => x[1]).map(x => '<div class="lead-block"><h4>' + x[0] + '</h4>' +
      '<div class="lead-draft">' + t(x[1]) + '</div></div>').join('');
  const acts = !p.id ? '' : '<div class="lead-acts">' +
    (p.status === 'opted_out' || p.opt_out_undone || _optOut.id === p.id
      ? '<button class="btn btn-secondary btn-sm" ' +
        'onclick="event.stopPropagation();toggleOptOut(' + i + ')" ' +
        'aria-expanded="' + (_optOut.id === p.id) + '">' +
        (p.status === 'opted_out' ? 'Why did it stop emailing them?' : 'Opt-out history') +
        '</button>' : '') +
    '<button class="btn btn-secondary btn-sm" ' +
      'onclick="event.stopPropagation();fbProspect(' + i + ')">' +
      'Tell Sellbird something about them</button></div>';
  return '<div class="lead-dossier">' +
    '<div><h4>Why they would buy</h4>' + why + '</div>' +
    '<div><h4>Who they are</h4>' + who + '</div>' +
    '<div><h4>Where they stand</h4>' + stands + '</div>' +
    '</div>' + draft + later + acts;
}

// Why someone is opted out, and undoing it. Sellbird never moves anyone off
// "opted out" on its own, so a mistaken one (a bare "stop" that meant
// something else) needs you: see what triggered it, then undo it here.
let _optOut = {id: null, data: null, armed: false, busy: false};
const OPT_OUT_RULES = {
  phrase: 'Matched an opt-out phrase',
  no_thanks: 'Matched “no thanks”',
  only_words: 'Their reply was only an opt-out word',
  model: 'The model’s judgement (no phrase matched)',
};

async function toggleOptOut(i) {
  const p = _prospects[i];
  if (!p) return;
  if (_optOut.id === p.id) { _optOut = {id: null, data: null, armed: false, busy: false}; renderContacts(); return; }
  _optOut = {id: p.id, data: null, armed: false, busy: false};
  renderContacts();
  const data = await api('/api/prospects/' + encodeURIComponent(p.id) + '/opt-out');
  if (_optOut.id !== p.id) return;   // closed or switched meanwhile
  _optOut.data = data || {error: true};
  renderContacts();
}

function optOutPanel(p) {
  const d = _optOut.data;
  const name = escHtml([p.first_name, p.last_name].filter(Boolean).join(' ') || p.email);
  if (!d) return '<div class="optout-panel muted">Loading…</div>';
  if (d.error) return '<div class="optout-panel muted">Couldn’t load the opt-out record. Try again.</div>';
  let html = '<div class="optout-panel"><h4>' + (d.opted_out ? 'Why Sellbird stopped emailing ' : 'Opt-out history for ') +
    name + '</h4>';
  if (d.recorded) {
    html += '<p>' + escHtml(d.why) + '</p><div class="optout-meta">' +
      escHtml(own(OPT_OUT_RULES, d.rule) || d.rule || '') + (d.at ? ' · ' + relTime(d.at) : '') + '</div>';
  } else if (d.opted_out || (d.history || []).length) {
    html += '<p>They opted out before Sellbird kept a record of why.' +
      (d.message ? ' The last thing they wrote:' : ' There’s no reply from them on file.') + '</p>';
  }
  if (d.message) {
    html += '<div class="thread-msg received"><div class="sender">' + escHtml(p.first_name || 'They') +
      (d.at ? ' &middot; ' + relTime(d.at) : '') + '</div>' + escHtml(d.message) + '</div>';
  }
  const undone = (d.history || []).filter(h => h.action === 'opt_out_undone');
  if (undone.length) {
    html += '<div class="optout-meta">' + undone.map(h => 'You undid an opt-out ' + relTime(h.at)).join(' · ') + '</div>';
  }
  if (!d.opted_out) {
    html += '<div class="convo-actions"><span class="muted">Not opted out now (' +
      escHtml(statusMeta(d.status).label) + ').</span></div>';
  } else if (_optOut.armed) {
    html += '<div class="convo-actions"><span class="muted">Sellbird will treat them as having replied. Nothing is ' +
      'sent automatically, emails the opt-out cancelled stay cancelled, and Sellbird’s answer to their next ' +
      'reply waits for your approval, even on autopilot. Only undo it if they didn’t mean to opt out.</span>' +
      '<button class="btn btn-primary btn-sm" onclick="undoOptOut()"' + (_optOut.busy ? ' disabled' : '') +
        '>Confirm undo</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="_optOut.armed=false;renderContacts()">Cancel</button></div>';
  } else {
    html += '<div class="convo-actions"><button class="btn btn-secondary btn-sm" ' +
      'onclick="_optOut.armed=true;renderContacts()">Undo opt-out</button></div>';
  }
  return html + '</div>';
}

async function undoOptOut() {
  const id = _optOut.id;
  if (!id || _optOut.busy) return;
  _optOut.busy = true;
  renderContacts();
  const res = await api('/api/prospects/' + encodeURIComponent(id) + '/opt-out/undo', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({confirm: true}),
  });
  if (res && res.success) showToast('Opt-out undone. They’re marked as replied; Sellbird sends nothing on its own.', 'success');
  else showToast(failMsg(res, 'Could not undo the opt-out. Try again.'), 'error');
  if (_optOut.id !== id) return;
  _optOut.armed = false;
  _optOut.busy = false;
  _optOut.data = await api('/api/prospects/' + encodeURIComponent(id) + '/opt-out') || {error: true};
  loadProspects();
}

// From a conversation: the contact, with the opt-out record open.
async function showOptOut(prospectId, email) {
  goTab('prospects');
  _contactQuery = email || '';
  const search = document.getElementById('contact-search');
  if (search) search.value = _contactQuery;
  await loadProspects();
  const i = _prospects.findIndex(p => p.id === prospectId);
  if (i >= 0 && _optOut.id !== prospectId) toggleOptOut(i);
}

// Opens a filled-in compose window in whatever Gmail account the browser is
// logged into. Nothing is sent from here — the user reads it and presses Send.
// Same values as the Sender's _render, so a hand-sent email reads the same.
function fillMergeVars(text, r) {
  const inbox = r.source === 'company_inbox';
  const vals = {
    first_name: r.first_name || (inbox ? 'there' : ''), last_name: r.last_name || '',
    company: r.company || '', title: r.title || '',
    personalization: r.personalization_notes || '',
  };
  return String(text || '').replace(/\{\{\s*(\w+)\s*\}\}/g, (m, k) => {
    k = k.toLowerCase();
    return Object.prototype.hasOwnProperty.call(vals, k) ? vals[k] : m;
  });
}

let _gmailBusy = false;  // one compose window per click, never two

async function openInGmail(ci, si, ri) {
  if (_gmailBusy) return;
  const c = _campaigns[ci];
  const step = c && (c.sequence || [])[si];
  const r = c && (c.recipients || [])[ri];
  if (!step || !r) return;
  // Cold email carries the postal address and opt-out line, sent by hand too.
  const filled = fillMergeVars(step.body, r).trimEnd();
  const body = filled + (c.footer && !filled.endsWith(c.footer) ? '\n\n' + c.footer : '');
  // authuser picks the Google account Sellbird sends as, not just whichever
  // one the browser happens to be signed into.
  const url = 'https://mail.google.com/mail/?view=cm&fs=1' +
    (c.send_as ? '&authuser=' + encodeURIComponent(c.send_as) : '') +
    '&to=' + encodeURIComponent(r.email) +
    '&su=' + encodeURIComponent(fillMergeVars(step.subject, r)) +
    '&body=' + encodeURIComponent(body);
  const left = /\{\{\s*\w+\s*\}\}/.test(fillMergeVars(step.subject, r) + fillMergeVars(step.body, r));
  // No 'noopener' feature string: with it window.open always returns null,
  // so a blocked pop-up could not be told apart from a successful one. The
  // tab opens blank right away (inside the click, so it isn't blocked) and
  // only gets the draft once the send is recorded.
  const w = window.open('', '_blank');
  if (!w) {
    showToast('Your browser blocked the Gmail tab. Allow pop-ups for this page and click again.', 'error');
    return;
  }
  w.opener = null;
  // Record the send BEFORE the user can press Send in Gmail, so Sellbird can
  // never email them as well — not even if they forget to tick the box.
  _gmailBusy = true;
  let claimed = r.sent_by_hand;
  try { if (!claimed) claimed = await markSent(ci, ri, true); } finally { _gmailBusy = false; }
  if (!claimed) { w.close(); return; }
  w.location.href = url;
  if (left) showToast('This draft still has a {{placeholder}} in it. Fix it in Gmail before sending.', 'error');
}

async function markSent(ci, ri, sent) {
  const r = (_campaigns[ci] && _campaigns[ci].recipients || [])[ri];
  if (!r) return;
  let res = null;
  try {
    const resp = await authFetch('/api/prospects/' + encodeURIComponent(r.id) + '/sent', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sent}),
    });
    if (await lockIfRefused(resp)) return;
    res = await resp.json();
  } catch {}
  const ok = !!(res && res.success);
  if (!ok) showToast((res && res.message) || 'Could not save that. Try again.', 'error');
  else if (!sent && res.sent) showToast('They already replied, so this stays marked as sent.');
  else showToast(sent ? 'Marked as sent by you.' : 'Unmarked.', 'success');
  loadCampaigns();
  // True only when this call changed it: a status means they replied or
  // opted out, 'already' means another click or tab got there first.
  return ok && sent === !!res.sent && !res.status && !res.already;
}

// Where a campaign's people stand, as one stacked bar.
function campaignProgress(c) {
  const rs = c.recipients || [];
  if (!rs.length) return '';
  const count = st => rs.filter(r => st.includes(r.status)).length;
  const parts = [
    ['Contacted', count(['contacted']), 'c-accent'],
    ['Replied', count(['replied']), 'c-active'],
    ['Interested', count(['interested', 'meeting']), 'c-good'],
    ['Waiting', count(['new', 'queued']), 'c-accent-soft'],
  ];
  const closed = rs.length - parts.reduce((x, p) => x + p[1], 0);
  if (closed > 0) parts.push(['Closed out', closed, 'c-idle']);
  return '<div class="campaign-progress"><div class="bar" role="img" aria-label="Where this campaign\'s people stand">' +
    parts.filter(p => p[1]).map(p => '<span class="' + p[2] + '" style="width:' + (100 * p[1] / rs.length) +
      '%" title="' + p[0] + ': ' + p[1] + '"></span>').join('') + '</div>' +
    '<div class="campaign-legend">' + parts.filter(p => p[1]).map(p =>
      '<span><i class="' + p[2] + '"></i>' + p[0] + ' <b>' + p[1] + '</b></span>').join('') + '</div></div>';
}

async function loadCampaigns() {
  const el = document.getElementById('campaigns-list');
  const data = await api('/api/campaigns');
  if (!data) { el.innerHTML = offlineState(); return; }
  _campaigns = data;
  if (!data.length) {
    const live = await api('/api/live-status') || {};
    const waiting = (live.steps || []).find(x => x.label === 'Write first emails');
    const left = waiting ? Math.max(0, Number(waiting.total) - Number(waiting.done)) : 0;
    const stop = (live.blocked || [])[0];
    el.innerHTML = emptyState(icon('campaigns'), 'No campaigns yet',
      'The Writer drafts a 3-email sequence for every contact with a deliverable address, ' +
      'and does it by itself once Sellbird is running. ' +
      (left ? '<b>' + left + '</b> contact(s) are waiting for one now.'
            : 'Nothing is waiting for one right now.')) +
      (!left ? ''
        : stop
          ? '<div class="notice bad"><b>' + escHtml(stop.title) + '</b><br>' + escHtml(stop.detail) +
            (stop.tab ? '<br><a href="#" onclick="goTab(' + jsq(stop.tab) + ');return false">Take me there</a>' : '') +
            '</div>'
          : live.loop_running
            ? '<p class="desc">Sellbird is running, so it writes these by itself within a cycle.</p>'
            : '<div class="write-now">' + writeNowButton() + '</div>');
    if (left && !stop && !live.loop_running) renderWriteNow();
    return;
  }
  let html = '';
  data.forEach((c, i) => {
    let stepsHtml = '';
    (c.sequence || []).forEach((step, s) => {
      const sendBtns = (c.recipients || []).map((r, ri) => {
        // Only offered when Sellbird can't send itself and this person can
        // still be emailed (the server decides: can_send_by_hand).
        if (r.sent_by_hand) {
          const tick = '<label style="display:inline-flex;align-items:center;gap:4px;font-size:13px;cursor:pointer">' +
            '<input type="checkbox" checked onchange="markSent(' + i + ',' + ri + ',this.checked)"> ' +
            '&#10003; Sent by you</label>';
          if (s === 0) return '<span class="rcpt-who">' + escHtml(r.email) + '</span>' + tick;
          // Follow-ups are the user's too: Sellbird never emails someone they emailed.
          return '<span class="rcpt-who">' + escHtml(r.email) + '</span>' +
            '<button class="btn btn-secondary btn-sm" onclick="openInGmail(' + i + ',' + s + ',' + ri + ')">' +
            'Open in Gmail</button>' +
            '<span class="muted" style="font-size:12px">Sellbird won\'t send follow-ups to people you emailed yourself.</span>';
        }
        if (s !== 0) return '';
        if (!r.can_send_by_hand) {
          // Sellbird sends here; this only records an email the user already sent.
          return r.can_mark_sent
            ? '<label class="muted" style="display:inline-flex;align-items:center;gap:4px;font-size:12px;cursor:pointer">' +
              '<input type="checkbox" onchange="markSent(' + i + ',' + ri + ',this.checked)"> ' +
              'I already emailed ' + escHtml(r.email) + ' myself</label>'
            : '';
        }
        const risky = r.email_status === 'risky'
          ? ' <span class="mini t-waiting">Catch-all, may bounce</span>' : '';
        return '<span class="rcpt-who">' + escHtml(r.email) + risky + '</span>' +
          '<button class="btn btn-secondary btn-sm" onclick="openInGmail(' + i + ',' + s + ',' + ri + ')">' +
          'Open in Gmail</button>' +
          '<label><input type="checkbox" onchange="markSent(' + i + ',' + ri + ',this.checked)"> Mark as sent</label>'
      }).filter(Boolean).map(x => '<div class="rcpt">' + x + '</div>').join('');
      stepsHtml += '<div class="email-step"><span class="step-dot">' + escHtml(String(step.step || '?')) + '</span>' +
        '<div><div class="step-num">' + (s === 0 ? 'First email' : 'Follow-up') +
        (step.delay_days ? ' &middot; ' + escHtml(String(step.delay_days)) + ' days after the last' : '') + '</div>' +
        '<div class="subject">' + escHtml(step.subject) + '</div>' +
        '<div class="body">' + escHtml(step.body) + '</div>' +
        (sendBtns ? '<div class="step-actions">' + sendBtns + '</div>' : '') +
        '</div></div>';
    });
    const pc = (c.prospect_ids || []).length;
    html += '<div class="campaign-card"><div class="campaign-top">' +
      '<div class="campaign-title"><h3>' + escHtml(c.name || 'Untitled Campaign') + '</h3>' +
        '<button class="btn btn-ghost btn-sm" onclick="fbCampaign(' + i + ')">Feedback</button></div>' +
      '<div class="meta">' + badge(c.status) + '<span>' + pc + ' prospect' + (pc !== 1 ? 's' : '') + '</span>' +
        '<span>' + (c.sequence || []).length + ' emails</span><span>Created ' + relTime(c.created_at) + '</span></div>' +
      campaignProgress(c) + '</div>' +
      '<div class="sequence">' +
      (stepsHtml || '<p class="muted" style="font-size:13px;padding-top:16px">No email steps in this campaign.</p>') + '</div></div>';
  });
  el.innerHTML = html;
}

// The whole exchange in order: Sellbird's cold emails (from the Outbox), the
// replies, Sellbird's answers, and any answer still waiting for approval.
let _convos = [], _convoFilter = 'needs';
const STAGE_LABELS = {
  initial_outreach: 'Outreach', engaged: 'Engaged', qualifying: 'Qualifying', presenting: 'Presenting',
  negotiating: 'Negotiating', closing: 'Closing', closed_won: 'Won', closed_lost: 'Lost',
};
const CONVO_FILTERS = [
  ['needs', 'Needs you', c => !!c.draft || c.status === 'needs_human'],
  ['open', 'Open', c => c.status !== 'closed' && c.status !== 'awaiting_reply'],
  ['awaiting', 'Awaiting reply', c => c.status === 'awaiting_reply'],
  ['closed', 'Closed', c => c.status === 'closed'],
  ['all', 'All', () => true],
];

async function loadConversations() {
  const el = document.getElementById('conversations-list');
  const data = await api('/api/conversations?awaiting=1');
  if (!data) { el.innerHTML = offlineState(); return; }
  _convos = data;
  if (!data.length) {
    el.innerHTML = emptyState(icon('conversations'), 'No conversations yet',
      'No prospects have replied so far. When they do, the Handler agent classifies each reply and responds — every thread shows up here.');
    return;
  }
  // Open on "Needs you" only when something needs you.
  if (_convoFilter === 'needs' && !data.some(CONVO_FILTERS[0][2]))
    _convoFilter = data.some(CONVO_FILTERS[1][2]) ? 'open' : 'all';
  renderConversations();
}

function setConvoFilter(f) { _convoFilter = f; renderConversations(); }

function renderConversations() {
  const el = document.getElementById('conversations-list');
  const filter = CONVO_FILTERS.find(f => f[0] === _convoFilter) || CONVO_FILTERS[CONVO_FILTERS.length - 1];
  const bar = '<div class="toolbar"><div class="segmented" role="group" aria-label="Filter conversations">' +
    CONVO_FILTERS.map(f => '<button class="' + (f[0] === filter[0] ? 'on' : '') + '" aria-pressed="' + (f[0] === filter[0]) +
      '" onclick="setConvoFilter(' + jsq(f[0]) + ')">' + f[1] + ' <span class="n">' +
      _convos.filter(f[2]).length + '</span></button>').join('') + '</div></div>';
  // Handed-to-you first, then drafts waiting; otherwise newest first (as served).
  const rank = c => c.status === 'needs_human' ? 0 : c.draft ? 1 : 2;
  const shown = _convos.filter(filter[2]).map((c, i) => [c, i])
    .sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map(x => x[0]);
  if (!shown.length) {
    el.innerHTML = bar + '<div class="card">' + emptyState(icon('check'), 'Nothing here',
      'No conversation matches this filter.') + '</div>';
    return;
  }
  el.innerHTML = bar + shown.map(convoCard).join('');
}

function convoCard(c) {
  const items = (c.emails || []).map(e => ({ts: e.sent_at, email: e}))
    .concat((c.thread || []).map(m => ({ts: m.timestamp, msg: m})))
    .map(it => ({...it, key: +parseTs(it.ts)}))
    // An item with no time goes last instead of scrambling the order.
    .sort((a, b) => (isNaN(a.key) ? Infinity : a.key) - (isNaN(b.key) ? Infinity : b.key) || 0);
  const first = c.first_name || 'Them';
  const thread = items.map(it => {
    if (it.email) {
      return '<div class="thread-email"><div class="te-head">' + icon('campaigns') +
        '<b>Email ' + escHtml(String(it.email.step)) + '</b><span>' + escHtml(it.email.subject) + '</span>' +
        '<span class="te-when">' + relTime(it.email.sent_at) + '</span></div>' +
        '<div class="te-body">' + escHtml(it.email.body) + '</div></div>';
    }
    const m = it.msg, sent = m.sender !== 'prospect';  // 'harvey' (the old name) is ours too
    return '<div class="thread-msg ' + (sent ? 'sent' : 'received') + '"><div class="sender">' +
      (sent ? 'Sellbird' : escHtml(first)) + ' &middot; ' + relTime(m.timestamp) + '</div>' +
      escHtml(m.content) + '</div>';
  }).join('');
  const draft = c.draft
    ? '<div class="thread-draft"><div class="sender">' + icon('pen') + 'Sellbird drafted a reply · waiting for you</div>' +
      '<div class="td-body">' + escHtml(c.draft.body) + '</div>' +
      '<button class="btn btn-primary btn-sm" onclick="goTab(\'outbox\')">Review in Outbox' + icon('arrow') + '</button></div>'
    : '';
  const name = [c.first_name, c.last_name].filter(Boolean).join(' ') || c.company || 'Unknown';
  const stage = own(STAGE_LABELS, c.stage) ? '<span class="tag">' + STAGE_LABELS[c.stage] + '</span>' : '';
  return '<div class="convo-card"><div class="convo-head">' +
    '<span class="avatar">' + initials(c.first_name, c.last_name) + '</span>' +
    '<div><h3>' + escHtml(name) + '</h3><div class="sub">' +
      escHtml([c.company, c.prospect_email].filter(Boolean).join(' · ')) + ' · ' + relTime(c.updated_at) + '</div></div>' +
    // An escalation's intent badge says the same as its status: show one.
    '<div class="tags">' + (c.intent && !(c.status === 'needs_human' && c.intent === 'escalate') ? badge(c.intent) : '') +
      stage + badge(c.status) + '</div></div>' +
    '<div class="thread">' + (thread || '<p class="muted" style="font-size:13px">No messages in this thread yet.</p>') +
    draft + '</div>' + convoActions(c) + '</div>';
}

// Sellbird answers new replies only on open conversations: an escalated one
// waits for you to reply yourself, then close it or hand it back.
function convoActions(c) {
  if (c.status === 'needs_human' && c.prospect_status === 'opted_out') {
    // A complaint or legal threat from someone who opted out: handing it
    // back wouldn't help, Sellbird never answers them.
    return '<div class="convo-actions"><span class="muted">' + icon('alert') +
      'Opted out, and Sellbird handed this to you. Reply from your inbox if you need to, then:</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="setConvoStatus(' + jsq(c.id) + ',\'closed\')">Close</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="showOptOut(' + jsq(c.prospect_id) + ',' + jsq(c.prospect_email) +
      ')">Why opted out?</button></div>';
  }
  if (c.status === 'needs_human') {
    return '<div class="convo-actions"><span class="muted">' + icon('alert') +
      'Sellbird handed this to you. Reply from your inbox, then:</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="setConvoStatus(' + jsq(c.id) + ',\'open\')">Hand back to Sellbird</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="setConvoStatus(' + jsq(c.id) + ',\'closed\')">Close</button></div>';
  }
  if (c.status === 'open') {
    return '<div class="convo-actions"><button class="btn btn-ghost btn-sm" onclick="setConvoStatus(' +
      jsq(c.id) + ',\'closed\')">Close conversation</button></div>';
  }
  if (c.status === 'closed' && c.prospect_status === 'opted_out') {
    // Reopening wouldn't help: Sellbird never answers someone who opted out.
    return '<div class="convo-actions"><span class="muted">' + icon('alert') + 'Opted out. Sellbird won’t write to them.</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="showOptOut(' + jsq(c.prospect_id) + ',' + jsq(c.prospect_email) +
      ')">Why opted out?</button></div>';
  }
  if (c.status === 'closed') {
    return '<div class="convo-actions"><button class="btn btn-ghost btn-sm" onclick="setConvoStatus(' +
      jsq(c.id) + ',\'open\')">Reopen</button></div>';
  }
  return '';
}

async function setConvoStatus(id, status) {
  const data = await api('/api/conversations/' + encodeURIComponent(id) + '/status', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({status: status}),
  });
  if (data && data.success) showToast(status === 'open' ? 'Open again. Sellbird will answer the next reply.' : 'Closed. A new reply starts a new conversation.', 'success');
  else showToast(failMsg(data, 'Could not update that conversation.'), 'error');
  loadConversations();
  loadToday();
}

// ── Inboxes: the main mailbox and the extra ones you connected ──

const INBOX_STATE = {
  sending: ['Sending', 't-good'],
  paused: ['Paused', 't-waiting'],
  not_connected: ['Not connected', 't-bad'],
};
const INBOX_KINDS = {
  gmail: ['Gmail', 'Google Workspace or Gmail. Sign in with Google in your browser.'],
  microsoft: ['Microsoft 365', 'Outlook or Microsoft 365. Sign in with Microsoft in your browser.'],
  smtp: ['Other (SMTP)', 'Any other email host, with its SMTP server and password.'],
};
let _inboxes = null;
let _inboxPoll = null;

function inboxBadge(ib) {
  const [label, tone] = INBOX_STATE[ib.state] || [ib.state, 't-idle'];
  return '<span class="badge ' + tone + '">' + escHtml(label) + '</span>';
}

function inboxMeter(ib) {
  const cap = Math.max(1, Number(ib.cold_cap) || 0);
  const sent = Number(ib.cold_sent_today) || 0;
  const pct = Math.min(100, Math.round(sent / cap * 100));
  return '<div class="inbox-meter"><div class="line"><span>Cold emails today</span><span>' +
    sent + ' / ' + Number(ib.cold_cap) + '</span></div>' +
    '<div class="progress-bar"><div class="progress-fill ' + (pct >= 100 ? 'yellow' : 'green') +
    '" style="width:' + pct + '%"></div></div></div>';
}

function inboxDate(iso) {
  if (!iso) return '';
  const d = new Date(iso.length === 10 ? iso + 'T00:00:00' : iso);
  return isNaN(d) ? iso : d.toLocaleDateString(undefined, {month: 'short', day: 'numeric', year: 'numeric'});
}

async function loadInboxes() {
  const el = document.getElementById('inboxes-list');
  const actions = document.getElementById('inboxes-actions');
  const data = await api('/api/inboxes');
  if (!data) { el.innerHTML = offlineState(); return; }
  _inboxes = data;
  if (!data.native) {
    actions.innerHTML = '';
    el.innerHTML = emptyState(icon('inboxes'), 'Instantly sends for you',
      'Sending through several of your own inboxes works with Gmail, Microsoft 365 or SMTP. ' +
      'Set channels.email.provider in sellbird.yaml to one of those to use it.');
    return;
  }
  actions.innerHTML = '<button class="btn btn-primary" onclick="openAddInbox()">' + icon('plus') +
    'Add inbox</button>';

  let html = '';
  if (data.sending_paused) {
    html += '<div class="notice wait"><b>All sending is paused:</b> ' + escHtml(data.sending_paused) +
      '. Resume it on the Outbox tab.</div>';
  }
  const list = data.inboxes || [];
  const sending = list.filter(i => i.state === 'sending');
  const capToday = sending.reduce((n, i) => n + (Number(i.cold_cap) || 0), 0);
  const sentToday = list.reduce((n, i) => n + (Number(i.cold_sent_today) || 0), 0);
  const waiting = list.reduce((n, i) => n + (Number(i.first_emails_waiting) || 0), 0);
  html += '<div class="inbox-summary">' +
    '<span><b>' + list.length + '</b> ' + (list.length === 1 ? 'inbox' : 'inboxes') + ', <b>' + sending.length + '</b> sending</span>' +
    '<span><b>' + sentToday + '</b>' + (capToday ? ' of <b>' + Math.min(capToday, Number(data.max_daily_sends)) + '</b>' : '') + ' cold emails sent today</span>' +
    '<span><b>' + waiting + '</b> first emails waiting to go</span></div>';

  html += '<div class="inbox-list">';
  list.forEach((ib, i) => {
    const letter = (ib.address || '?').charAt(0).toUpperCase();
    const sub = [ib.name, ib.main ? 'Main mailbox' : (INBOX_KINDS[ib.provider === 'google' ? 'gmail' : ib.provider] || [ib.provider])[0]]
      .filter(Boolean).join(' · ');
    html += '<button class="inbox-row" onclick="openInbox(' + i + ')" aria-label="Open ' + escHtml(ib.address || 'main mailbox') + '">' +
      '<span class="inbox-avatar" aria-hidden="true">' + escHtml(letter) + '</span>' +
      '<span class="inbox-who"><div class="addr">' + escHtml(ib.address || '(main mailbox)') + '</div>' +
      '<div class="sub">' + escHtml(sub) + '</div></span>' +
      inboxBadge(ib) + inboxMeter(ib) +
      '<span class="inbox-waiting"><b>' + Number(ib.first_emails_waiting) + '</b>waiting</span>' +
      icon('chevron', 'chev') + '</button>';
  });
  html += '</div>';

  if (!data.several) {
    html += '<div class="card"><div class="card-title">Why add another inbox?</div><div class="inbox-help">' +
      '<div class="inset"><h4>Send more each day</h4><p>Each inbox sends up to ' + Number(data.per_inbox_daily_cap) +
      ' cold emails a day. Two inboxes, twice as many, all under your daily total of ' + Number(data.max_daily_sends) + '.</p></div>' +
      '<div class="inset"><h4>Protect your main address</h4><p>Send cold email from a second domain, so a bad week never touches your everyday mail.</p></div>' +
      '<div class="inset"><h4>Pick per campaign</h4><p>Choose which inboxes each campaign sends from. Replies always come back to the inbox that started the thread.</p></div>' +
      '</div><div class="btn-group"><button class="btn btn-secondary" onclick="openAddInbox()">' + icon('plus') + 'Add inbox</button></div></div>';
  }
  el.innerHTML = html;
}

// The dialog: one inbox's detail, or adding one.

function inboxDialog(html) {
  const d = document.getElementById('inbox-dialog');
  d.innerHTML = html;
  if (!d.open) d.showModal();
  return d;
}

function closeInboxDialog() {
  if (_inboxPoll) { clearInterval(_inboxPoll); _inboxPoll = null; }
  const d = document.getElementById('inbox-dialog');
  if (d.open) d.close();
}

function inboxDialogHead(title, sub, extra) {
  return '<div class="modal-head"><div><h3 id="inbox-dialog-title">' + title + '</h3>' +
    (sub ? '<div class="sub">' + sub + '</div>' : '') + '</div>' + (extra || '') +
    '<button class="btn btn-ghost btn-sm btn-icon" onclick="closeInboxDialog()" aria-label="Close">' + icon('close') + '</button></div>';
}

function openInbox(i) {
  const ib = (_inboxes && _inboxes.inboxes || [])[i];
  if (!ib) return;
  const campaigns = (_inboxes.campaigns || []);
  const pools = _inboxes.pools || {};
  const using = campaigns.filter(c => (pools[c.id] || []).includes(ib.id));
  const everyInbox = campaigns.filter(c => !pools[c.id]);
  let body = '';
  if (ib.state === 'not_connected') {
    body += '<div class="notice bad"><b>Not connected.</b> ' +
      escHtml(ib.connect_hint || 'Sellbird can\'t sign in to it from this computer. Check the connection to see why, or remove it and add it again.') + '</div>';
  }
  if (ib.paused_reason) {
    body += '<div class="notice wait"><b>Paused:</b> ' + escHtml(ib.paused_reason) + '</div>';
  }
  body += '<div class="modal-stats">' +
    '<div class="inset"><div class="k">Cold today</div><div class="v">' + Number(ib.cold_sent_today) + ' / ' + Number(ib.cold_cap) + '</div></div>' +
    '<div class="inset"><div class="k">First emails waiting</div><div class="v">' + Number(ib.first_emails_waiting) + '</div></div>' +
    '<div class="inset"><div class="k">Warming up since</div><div class="v">' + (ib.warmup_started_at ? escHtml(inboxDate(ib.warmup_started_at)) : '<span class="muted">Established</span>') + '</div></div>' +
    '</div>';
  body += '<div class="modal-section"><h4>Campaigns sending from it</h4>';
  if (!campaigns.length) body += '<p>No campaigns yet.</p>';
  else {
    const names = using.concat(everyInbox).map(c => escHtml(c.name || 'Untitled'));
    const shown = names.slice(0, 6).join(', ') + (names.length > 6 ? ', and ' + (names.length - 6) + ' more' : '');
    body += names.length ? '<p>' + shown + '</p>' : '<p>None. Every campaign has chosen other inboxes.</p>';
    if (everyInbox.length) body += '<p class="form-hint">Campaigns that haven\'t chosen inboxes send from all of them.</p>';
  }
  body += '</div>';

  if (ib.main) {
    const kind = ib.provider === 'gmail' || ib.provider === 'microsoft' ? ib.provider : '';
    if (ib.state === 'not_connected' && kind) {
      const can = (_inboxes.can_connect || {})[kind];
      body = '<div id="main-status"></div>' + (can
        ? '<div class="notice bad"><b>Not connected.</b> Sign in to the mailbox Sellbird sends from.</div>' +
          '<button class="btn btn-primary" id="main-go" onclick="connectMainMailbox(' + jsq(kind) + ')">Sign in with ' +
          (kind === 'gmail' ? 'Google' : 'Microsoft') + '</button><div style="height:16px"></div>'
        : connectSetupNotice(kind)) + body.replace(/<div class="notice bad"><b>Not connected.<\/b>[\s\S]*?<\/div>/, '');
    }
    body += '<div class="modal-section"><h4>Settings</h4><p>Your main mailbox comes from sellbird.yaml: ' +
      'its address is persona.email, its daily cap is channels.email.per_inbox_daily_cap, and pausing it pauses ' +
      'all sending, on the Outbox tab.</p></div>';
    inboxDialog(inboxDialogHead(escHtml(ib.address || 'Main mailbox'), inboxBadge(ib) + ' Main mailbox') +
      '<div class="modal-body">' + body + '</div>');
    return;
  }
  const id = jsq(ib.id);
  body += '<div class="modal-section"><h4>Daily cold-email cap</h4>' +
    '<div class="inline-edit"><input class="form-input" id="ib-cap" type="number" min="1" max="500" inputmode="numeric" placeholder="Default (' +
    Number(_inboxes.per_inbox_daily_cap) + ')" value="' + (ib.cap_is_yours ? Number(ib.cold_cap) : '') + '">' +
    '<button class="btn btn-secondary" onclick="saveInboxCap(' + id + ')">Save</button></div>' +
    '<div class="form-hint">Leave empty for the default. Warming inboxes start lower on their own.</div></div>';
  body += '<div class="modal-section"><h4>Sender name</h4>' +
    '<div class="inline-edit"><input class="form-input" id="ib-name" maxlength="100" placeholder="Your persona name" value="' + escHtml(ib.name || '') + '">' +
    '<button class="btn btn-secondary" onclick="saveInboxName(' + id + ')">Save</button></div>' +
    '<div class="form-hint">The name people see in From.</div></div>';
  body += '<div class="modal-section"><h4>Remove this inbox</h4><p>Sellbird stops sending from it, and threads it started end. Its sign-in stays in your keychain.</p>' +
    '<div class="inline-edit" style="margin-top:8px"><input class="form-input" id="ib-confirm" placeholder="Type ' + escHtml(ib.address) + ' to confirm">' +
    '<button class="btn btn-danger" onclick="removeInbox(' + id + ')">Remove</button></div></div>';

  const pauseBtn = ib.paused_reason
    ? '<button class="btn btn-primary" onclick="inboxAction(' + id + ',\'resume\')">Resume sending</button>'
    : '<button class="btn btn-secondary" onclick="inboxAction(' + id + ',\'pause\')"' + (ib.state === 'not_connected' ? ' disabled' : '') + '>Pause</button>';
  inboxDialog(inboxDialogHead(escHtml(ib.address), inboxBadge(ib) + ' ' + escHtml(ib.name || '')) +
    '<div class="modal-body">' + body + '</div>' +
    '<div class="modal-foot"><button class="btn btn-ghost" onclick="testInbox(' + id + ')">Check connection</button>' + pauseBtn + '</div>');
}

async function inboxPost(path, body, done, keepOpen) {
  const r = await api(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
                             body: JSON.stringify(body || {})});
  if (r && r.success) {
    if (done) showToast(done, 'success');
    if (!keepOpen) closeInboxDialog();
    loadInboxes();
  } else showToast((r && r.message) || 'That didn\'t work.', 'error');
  return r;
}

function inboxAction(id, action) {
  inboxPost('/api/inboxes/' + encodeURIComponent(id) + '/' + action, {},
            action === 'pause' ? 'Inbox paused' : 'Inbox sends again');
}

function saveInboxCap(id) {
  const v = document.getElementById('ib-cap').value.trim();
  inboxPost('/api/inboxes/' + encodeURIComponent(id) + '/cap', {cap: v === '' ? null : Number(v)}, 'Cap saved');
}

function saveInboxName(id) {
  inboxPost('/api/inboxes/' + encodeURIComponent(id) + '/name',
            {name: document.getElementById('ib-name').value.trim()}, 'Name saved');
}

function removeInbox(id) {
  inboxPost('/api/inboxes/' + encodeURIComponent(id) + '/remove',
            {confirm: document.getElementById('ib-confirm').value.trim().toLowerCase()}, 'Inbox removed');
}

async function testInbox(id) {
  showToast('Checking the connection...', 'info');
  const r = await api('/api/inboxes/' + encodeURIComponent(id) + '/test', {method: 'POST'});
  if (r && r.success) showToast(r.detail || (r.ok ? 'Connected' : 'Not connected'), r.ok ? 'success' : 'error');
  else showToast((r && r.message) || 'Couldn\'t check it.', 'error');
}

// Adding an inbox

let _addKind = 'gmail';

function openAddInbox(kind) {
  if (kind) _addKind = kind;
  const can = (_inboxes && _inboxes.can_connect) || {};
  const picks = Object.keys(INBOX_KINDS).map(k =>
    '<button class="provider-pick' + (k === _addKind ? ' on' : '') + '" onclick="openAddInbox(' + jsq(k) + ')" aria-pressed="' + (k === _addKind) + '">' +
    '<b>' + INBOX_KINDS[k][0] + '</b><span>' + INBOX_KINDS[k][1] + '</span></button>').join('');
  let form = '<div class="form-group"><label class="form-label" for="add-address">Email address</label>' +
    '<input class="form-input" id="add-address" type="email" autocomplete="off" placeholder="you@yourdomain.com" oninput="updateAddCommand()"></div>' +
    '<div class="form-row"><div class="form-group"><label class="form-label" for="add-name">Sender name <span class="optional-tag">optional</span></label>' +
    '<input class="form-input" id="add-name" maxlength="100" placeholder="Your persona name"></div>' +
    '<div class="form-group"><label class="form-label" for="add-warmup">Warming up since <span class="optional-tag">optional</span></label>' +
    '<input class="form-input" id="add-warmup" type="date"></div></div>' +
    '<div class="form-hint" style="margin:-8px 0 16px">Leave the date empty if this inbox has been sending normally for a while.</div>';
  let note = '';
  if (_addKind === 'smtp') {
    form += '<div class="form-row"><div class="form-group"><label class="form-label" for="add-host">SMTP server</label>' +
      '<input class="form-input" id="add-host" placeholder="smtp.yourhost.com"></div>' +
      '<div class="form-group" style="max-width:110px"><label class="form-label" for="add-port">Port</label>' +
      '<input class="form-input" id="add-port" type="number" value="587"></div></div>' +
      '<div class="form-group"><label class="form-label" for="add-password">Password or app password</label>' +
      '<input class="form-input" id="add-password" type="password" autocomplete="new-password"></div>' +
      '<details class="more-fields"><summary>Username and IMAP (for reading replies)</summary>' +
      '<div class="form-group"><label class="form-label" for="add-user">Username</label><input class="form-input" id="add-user" placeholder="Same as the address"></div>' +
      '<div class="form-row"><div class="form-group"><label class="form-label" for="add-imap">IMAP server</label><input class="form-input" id="add-imap" placeholder="Same as the SMTP server"></div>' +
      '<div class="form-group" style="max-width:110px"><label class="form-label" for="add-imap-port">Port</label><input class="form-input" id="add-imap-port" type="number" value="993"></div></div></details>';
    note = '<p class="form-hint">The password goes straight to your system keychain. Sellbird never shows it again.</p>';
  } else if (!can[_addKind]) {
    note = connectSetupNotice(_addKind);
  } else {
    note = '<p class="form-hint">A ' + INBOX_KINDS[_addKind][0] + ' sign-in opens in the browser on the computer running Sellbird. Sign in as the address above.</p>';
  }
  note += '<div class="modal-section"><h4>Or from your terminal</h4><div class="cmd-copy"><code id="add-cmd"></code>' +
    '<button class="btn btn-secondary btn-sm" onclick="copyAddCommand()">Copy</button></div></div>';
  const disabled = _addKind !== 'smtp' && !can[_addKind];
  inboxDialog(inboxDialogHead('Add an inbox', 'Connect an inbox you already own. Sellbird sends from it and reads its replies.') +
    '<div class="modal-body"><div class="provider-picks">' + picks + '</div><div style="height:16px"></div>' +
    '<div id="add-status"></div>' + form + note + '</div>' +
    '<div class="modal-foot"><button class="btn btn-ghost" onclick="closeInboxDialog()">Cancel</button>' +
    '<button class="btn btn-primary" id="add-go" onclick="submitAddInbox()"' + (disabled ? ' disabled' : '') + '>' +
    (_addKind === 'smtp' ? 'Connect' : 'Sign in with ' + (_addKind === 'gmail' ? 'Google' : 'Microsoft')) + '</button></div>');
  updateAddCommand();
}

function updateAddCommand() {
  const el = document.getElementById('add-cmd');
  if (!el) return;
  const addr = (document.getElementById('add-address').value || '').trim() || 'you@yourdomain.com';
  el.textContent = 'sellbird inboxes connect ' + _addKind + ' --address ' + addr +
    (_addKind === 'smtp' ? ' --smtp-host smtp.yourhost.com' : '');
}

function copyAddCommand() {
  const text = document.getElementById('add-cmd').textContent;
  navigator.clipboard.writeText(text).then(() => showToast('Copied', 'success'),
    () => showToast('Couldn\'t copy. Select the command instead.', 'error'));
}

function addStatus(cls, html) {
  const el = document.getElementById('add-status');
  if (el) el.innerHTML = html ? '<div class="notice ' + cls + '">' + html + '</div>' : '';
}

// Where the browser sign-in's own app (Google OAuth client / Microsoft app
// registration) is set up, when it isn't yet or doesn't look real.
const CONNECT_SETUP = {
  gmail: {
    field: 'gmail-id',
    steps: [
      'In <a href="https://console.cloud.google.com/apis/library/gmail.googleapis.com" target="_blank" rel="noopener noreferrer">Google Cloud Console</a>, pick or create a project and enable the Gmail API.',
      'Under <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener noreferrer">Credentials</a>, create an OAuth client ID of type <b>Desktop app</b>.',
      'Paste its Client ID and Client secret in Settings, under Gmail, and save.',
      'Come back here and sign in.',
    ],
  },
  microsoft: {
    field: 'm365-client',
    steps: [
      'In <a href="https://entra.microsoft.com" target="_blank" rel="noopener noreferrer">Entra</a>, go to App registrations and register an app for your organization only.',
      'Add the redirect URI <code>http://localhost:8766</code> as "Public client/native".',
      'Paste the Application (client) ID and tenant ID in Settings, under Microsoft 365, and save.',
      'Come back here and sign in.',
    ],
  },
};

function connectSetupNotice(kind) {
  const setup = CONNECT_SETUP[kind];
  const problem = ((_inboxes && _inboxes.connect_problems) || {})[kind] || '';
  return '<div class="notice info"><b>' + escHtml(problem) + '</b>' +
    '<ol class="setup-steps">' + setup.steps.map(t => '<li>' + t + '</li>').join('') + '</ol>' +
    '<button class="btn btn-secondary btn-sm" onclick="openSettingsAt(' + jsq(setup.field) + ')">Open Settings</button></div>';
}

function openSettingsAt(fieldId) {
  closeInboxDialog();
  const btn = Array.from(document.querySelectorAll('nav button'))
    .find(b => (b.getAttribute('onclick') || '').includes("'settings'"));
  showTab('settings', btn);
  setTimeout(() => {
    const el = document.getElementById(fieldId);
    if (!el) return;
    el.scrollIntoView({behavior: 'smooth', block: 'center'});
    el.focus({preventScroll: true});
  }, 300);
}

async function connectMainMailbox(kind) {
  const go = document.getElementById('main-go');
  if (go) go.disabled = true;
  const status = document.getElementById('main-status');
  const say = (cls, html) => { if (status) status.innerHTML = '<div class="notice ' + cls + '">' + html + '</div>'; };
  say('info', 'Opening the sign-in...');
  const r = await api('/api/inboxes/main/connect', {method: 'POST'});
  if (!r || !r.success) {
    if (go) go.disabled = false;
    say('bad', escHtml((r && r.message) || 'That didn\'t work.'));
    return;
  }
  say('wait', 'A ' + (kind === 'gmail' ? 'Google' : 'Microsoft') + ' sign-in opened in your browser. ' +
    'Sign in as your sending address and allow access. This page updates when you\'re done.');
  if (_inboxPoll) clearInterval(_inboxPoll);
  _inboxPoll = setInterval(async () => {
    const s = await api('/api/inboxes/connect/status');
    if (!s || s.state === 'waiting') return;
    clearInterval(_inboxPoll); _inboxPoll = null;
    if (s.state === 'connected') {
      showToast('Main mailbox connected', 'success');
      closeInboxDialog(); loadInboxes();
    } else {
      if (go) go.disabled = false;
      say('bad', escHtml(s.message || 'The sign-in didn\'t finish.'));
    }
  }, 1500);
}

async function submitAddInbox() {
  const v = id => { const el = document.getElementById(id); return el ? el.value.trim() : ''; };
  const body = {address: v('add-address'), name: v('add-name'), warmup_started: v('add-warmup')};
  const go = document.getElementById('add-go');
  if (_addKind === 'smtp') {
    Object.assign(body, {smtp_host: v('add-host'), smtp_port: Number(v('add-port') || 587),
      password: document.getElementById('add-password').value, username: v('add-user'),
      imap_host: v('add-imap'), imap_port: Number(v('add-imap-port') || 993)});
  }
  go.disabled = true;
  addStatus('info', _addKind === 'smtp' ? 'Connecting and checking the sign-in...' : 'Opening the sign-in...');
  const r = await api('/api/inboxes/connect/' + _addKind, {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  if (!r || !r.success) {
    go.disabled = false;
    addStatus('bad', escHtml((r && r.message) || 'That didn\'t work.'));
    return;
  }
  if (_addKind === 'smtp') {
    showToast('Connected ' + r.address, 'success');
    if (!r.test_ok) showToast('It\'s connected, but the check failed: ' + (r.test_detail || ''), 'error');
    closeInboxDialog(); loadInboxes();
    return;
  }
  addStatus('wait', 'A ' + (_addKind === 'gmail' ? 'Google' : 'Microsoft') + ' sign-in opened in your browser. ' +
    'Sign in as <b>' + escHtml(body.address.toLowerCase()) + '</b> and allow access. This page updates when you\'re done.');
  _inboxPoll = setInterval(async () => {
    const s = await api('/api/inboxes/connect/status');
    if (!s || s.state === 'waiting') return;
    clearInterval(_inboxPoll); _inboxPoll = null;
    if (s.state === 'connected') {
      showToast('Connected ' + s.address, 'success');
      if (s.message) showToast('It\'s connected, but the check failed: ' + s.message, 'error');
      closeInboxDialog(); loadInboxes();
    } else {
      go.disabled = false;
      addStatus('bad', escHtml(s.message || 'The sign-in didn\'t finish.'));
    }
  }, 1500);
}

async function loadActivity() {
  const el = document.getElementById('activity-list');
  const data = await api('/api/activity');
  if (!data) { el.innerHTML = offlineState(); return; }
  if (!data.length) {
    el.innerHTML = emptyState(icon('activity'), 'No activity yet',
      'Sellbird hasn\'t taken any actions. Every prospect found, email written, and reply handled will appear here the moment it happens.');
    return;
  }
  let html = '<div class="card"><div class="timeline">';
  let day = '';
  for (const a of data) {
    const label = dayLabel(a.created_at);
    if (label !== day) { day = label; html += '<div class="tl-day">' + escHtml(label) + '</div>'; }
    html += '<div class="tl-item' + (a.agent === 'main' ? ' quiet' : '') + '"><span class="tl-time">' + timeOfDay(a.created_at) + '</span>' +
      '<span class="tl-icon">' + icon(own(AGENT_ICONS, a.agent) || 'activity') + '</span>' +
      '<span class="tl-text">' + actionText(a) + '</span>' +
      '<span class="tl-agent">' + escHtml(a.agent) + '</span></div>';
  }
  el.innerHTML = html + '</div></div>';
}

// ── Init & live refresh ──

loadAuthMode();        // which way in to offer if a request is refused
startRouting();        // loads whichever tab the URL asks for
loadSellbirdStatus();  // the header's running light, on every tab
loadSignedInAs();      // shows Sign out when this is a sign-in, not a token

// ── Live refresh ──
//
// Four timers used to fire on their own schedules whatever else was
// happening. One ignored a hidden tab entirely, so a dashboard left open in
// a background window was polled every eight seconds for as long as the
// browser ran. None of them stopped when the server went away. And none
// waited for the previous request before sending the next, so a slow read
// (Contacts joins the whole lead record) could have three answers in flight
// rewriting the same table, and the last one to land won.
//
// One scheduler owns them now. A job runs only while the page is visible and
// unlocked, never twice at once, and everything backs off to a minute while
// requests aren't reaching the server at all. Coming back to the tab runs
// whatever is due straight away instead of waiting out the rest of an
// interval.

const OFFLINE_WAIT_MS = 60000;
const _refreshJobs = [];

// run() may return false to mean "that didn't work", which backs this one job
// off without slowing the others.
function every(ms, when, run) {
  _refreshJobs.push({ms, when, run, last: Date.now(), busy: false, failed: false});
}

function refreshWait(job) {
  return (job.failed || _transportFailures) ? Math.max(job.ms, OFFLINE_WAIT_MS) : job.ms;
}

async function runRefresh(job, now) {
  job.busy = true;
  job.last = now;
  try {
    job.failed = (await job.run()) === false;
  } catch {
    job.failed = true;
  } finally {
    job.busy = false;
    job.last = Date.now();     // measure the gap from the end, not the start
  }
}

function tickRefresh() {
  if (document.hidden || _locked) return;
  const now = Date.now();
  for (const job of _refreshJobs) {
    if (job.busy) continue;
    if (job.when && !job.when()) continue;
    if (now - job.last >= refreshWait(job)) runRefresh(job, now);
  }
}

// The header's running light, wherever you are.
every(8000, null, () => loadSellbirdStatus());

// The pipeline card, while Today is open.
every(10000, () => currentTab === 'today', () => loadLiveStatus());

// Nav counts stay live from any tab, so "something needs me" is visible
// without polling that tab's contents. Today refreshes them itself.
every(20000, () => currentTab !== 'today', async () => {
  const data = await api('/api/today');
  if (!data) return false;
  navCount('nav-today', (data.items || []).filter(i => i.tone !== 'good').length);
  navCount('nav-outbox', (data.stats || {}).outbox_pending || 0);
});

// The open tab's own data. Outbox, settings and help are deliberately left
// out — re-rendering the desk under someone mid-decision loses their place,
// and settings may be mid-edit.
every(15000, null, () => {
  switch (currentTab) {
    case 'today': return Promise.all([loadToday(), loadTrend(), loadFunnel(),
                                      loadRuns(), loadTodayActivity()]);
    case 'companies': return companyDrill ? undefined : loadCompanies();
    case 'prospects': return loadProspects();
    case 'campaigns': return loadCampaigns();
    case 'conversations': return loadConversations();
    case 'activity': return loadActivity();
    case 'inboxes': return loadInboxes();
    case 'usage': return loadUsage();
    case 'controls': return loadLogs();
  }
});

setInterval(tickRefresh, 1000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) tickRefresh(); });
window.addEventListener('focus', tickRefresh);


// ── Your company (sellbird.offer): what Sellbird writes from, and the offer ──

let _company = null;
let _offerIdeas = [];

function ce(t) {
  const d = document.createElement('div');
  d.textContent = t == null ? '' : String(t);
  return d.innerHTML;
}

function lines(list) { return (list || []).join('\n'); }
function splitLines(text) { return String(text || '').split('\n').map(x => x.trim()).filter(Boolean); }

async function loadCompany() {
  const body = document.getElementById('company-body');
  const data = await api('/api/company');
  if (!data) { body.innerHTML = offlineState(); return; }
  if (data.error) { body.innerHTML = `<p class="desc">${ce(data.error)}</p>`; return; }
  _company = data;
  const o = data.offer || {}, p = data.product || {}, icp = data.icp || {}, s = data.persona || {};
  const objections = Object.entries(p.objection_responses || {});
  body.innerHTML = `
  <section class="card company-card" aria-labelledby="co-offer-h">
    <div class="company-card-head">
      <h3 id="co-offer-h">Your offer</h3>
      <span class="hint">What every first email ends by offering</span>
    </div>
    ${o.proposed ? `<div class="offer-proposed">
      <div><span class="tag accent">Your agent proposed</span></div>
      <p class="offer-proposed-text">${ce(o.proposed.entry)}</p>
      ${o.proposed.why ? `<p class="desc">${ce(o.proposed.why)}</p>` : ''}
      <div class="offer-actions">
        <button class="btn btn-primary btn-sm" onclick="useProposedOffer()">Use this offer</button>
        <button class="btn btn-secondary btn-sm" onclick="dismissProposedOffer()">Dismiss</button>
      </div>
    </div>` : ''}
    <p class="desc">Each first email names something Sellbird found about the prospect, says what it costs them, and offers this as the fix. Make it free, specific, and something you can deliver this week. ${o.entry ? '' : '<b>No offer yet</b>, so first emails end with a question instead. '}The easiest way to work it out is to ask your coding agent: <i>"Help me work out my Sellbird offer."</i></p>
    <div class="form-group"><label class="form-label" for="offer-entry">Offer</label>
      <textarea class="form-input" id="offer-entry" rows="2" maxlength="300" placeholder="e.g. a written plan for getting you onto page one of Google">${ce(o.entry || '')}</textarea></div>
    <div class="offer-actions">
      <button class="btn btn-primary btn-sm" onclick="saveOffer()">Save offer</button>
      <button class="btn btn-secondary btn-sm" id="offer-preview-btn" onclick="previewOffer()">Preview an email</button>
      <button class="btn btn-secondary btn-sm" id="offer-suggest-btn" onclick="suggestOffers()">Suggest ideas</button>
    </div>
    <div id="offer-preview" class="offer-preview" hidden></div>
    <div id="offer-ideas" class="offer-ideas"></div>
  </section>

  <section class="card company-card" aria-labelledby="co-product-h">
    <div class="company-card-head"><h3 id="co-product-h">What you sell</h3></div>
    <div class="company-grid">
      <div class="form-group"><label class="form-label" for="co-p-name">Product or service</label>
        <input class="form-input" id="co-p-name" value="${ce(p.name)}"></div>
      <div class="form-group"><label class="form-label" for="co-p-pricing">Pricing</label>
        <input class="form-input" id="co-p-pricing" value="${ce(p.pricing)}"></div>
    </div>
    <div class="form-group"><label class="form-label" for="co-p-desc">What it is and who it's for</label>
      <textarea class="form-input" id="co-p-desc" rows="3">${ce(p.description)}</textarea></div>
    <div class="form-group"><label class="form-label" for="co-p-benefits">Key benefits <span class="hint">one per line</span></label>
      <textarea class="form-input" id="co-p-benefits" rows="4">${ce(lines(p.key_benefits))}</textarea></div>
    <div class="offer-actions"><button class="btn btn-primary btn-sm" onclick="saveCompanySection('product')">Save</button></div>
  </section>

  <section class="card company-card" aria-labelledby="co-icp-h">
    <div class="company-card-head"><h3 id="co-icp-h">Who you sell to</h3></div>
    <div class="company-grid">
      <div class="form-group"><label class="form-label" for="co-i-industries">Industries <span class="hint">one per line</span></label>
        <textarea class="form-input" id="co-i-industries" rows="4">${ce(lines(icp.industries))}</textarea></div>
      <div class="form-group"><label class="form-label" for="co-i-titles">Job titles <span class="hint">one per line</span></label>
        <textarea class="form-input" id="co-i-titles" rows="4">${ce(lines(icp.titles))}</textarea></div>
      <div class="form-group"><label class="form-label" for="co-i-size">Company size</label>
        <input class="form-input" id="co-i-size" value="${ce(icp.company_size)}"></div>
      <div class="form-group"><label class="form-label" for="co-i-geo">Where <span class="hint">one per line</span></label>
        <textarea class="form-input" id="co-i-geo" rows="2">${ce(lines(icp.geography))}</textarea></div>
    </div>
    <div class="offer-actions"><button class="btn btn-primary btn-sm" onclick="saveCompanySection('icp')">Save</button></div>
  </section>

  <section class="card company-card" aria-labelledby="co-sender-h">
    <div class="company-card-head"><h3 id="co-sender-h">Who it's from</h3>
      <span class="hint">Sellbird will not start until the company name is yours</span></div>
    <div class="company-grid">
      <div class="form-group"><label class="form-label" for="co-s-name">Sender name <span class="hint">signed under every email</span></label>
        <input class="form-input" id="co-s-name" value="${ce(s.name)}"></div>
      <div class="form-group"><label class="form-label" for="co-s-role">Role</label>
        <input class="form-input" id="co-s-role" value="${ce(s.role)}"></div>
      <div class="form-group"><label class="form-label" for="co-s-company">Company</label>
        <input class="form-input" id="co-s-company" value="${ce(s.company)}"></div>
      <div class="form-group"><label class="form-label" for="co-s-email">Reply-to email</label>
        <input class="form-input" id="co-s-email" value="${ce(s.email)}"></div>
    </div>
    <div class="form-group"><label class="form-label" for="co-s-tone">Tone</label>
      <input class="form-input" id="co-s-tone" value="${ce(s.tone)}"></div>
    <div class="offer-actions"><button class="btn btn-primary btn-sm" onclick="saveCompanySection('persona')">Save</button></div>
  </section>

  <section class="card company-card" aria-labelledby="co-obj-h">
    <div class="company-card-head"><h3 id="co-obj-h">Objections and answers</h3>
      <span class="hint">How replies handle pushback</span></div>
    <div id="co-objections" class="company-objections">
      ${(objections.length ? objections : [['', '']]).map(([k, v], i) => objectionRow(k, v, i)).join('')}
    </div>
    <div class="offer-actions">
      <button class="btn btn-secondary btn-sm" onclick="addObjection()">Add an objection</button>
      <button class="btn btn-primary btn-sm" onclick="saveCompanySection('product', true)">Save</button>
    </div>
  </section>

  <section class="card company-card" aria-labelledby="co-learned-h">
    <div class="company-card-head"><h3 id="co-learned-h">Private company notes</h3>
      <span class="hint">Stored on this server only</span></div>
    <p class="desc">Everything the agents should know that does not belong in a public code repository: real positioning, pricing detail, named competitors, who you have already spoken to. Paste it here. It is saved to this server's own storage, never to Git, and it survives redeploys.</p>
    <div class="form-group"><label class="form-label" for="co-knowledge">Product knowledge <span class="hint">what you sell, in your words</span></label>
      <textarea class="form-input" id="co-knowledge" rows="14" placeholder="What the product does, who it is for, the proof you can point to, pricing, how a deal usually closes...">${ce(data.knowledge || '')}</textarea></div>
    <div class="offer-actions"><button class="btn btn-primary btn-sm" onclick="saveKnowledge('knowledge')">Save product knowledge</button></div>
    <div class="form-group"><label class="form-label" for="co-competitors">Competitors <span class="hint">who you are up against, and how you win</span></label>
      <textarea class="form-input" id="co-competitors" rows="10" placeholder="Who else they are looking at, where each one is weak, what to say when a prospect names them...">${ce(data.competitors || '')}</textarea></div>
    <div class="offer-actions"><button class="btn btn-primary btn-sm" onclick="saveKnowledge('competitors')">Save competitors</button></div>
  </section>`;
  _offerIdeas = (o.entry_ideas || []).map(x => ({offer: x}));
  renderOfferIdeas();
  const badge = document.getElementById('nav-company');
  if (badge) badge.textContent = o.proposed ? '1' : '';
}

function objectionRow(k, v, i) {
  return `<div class="company-objection">
    <div class="form-group"><label class="form-label" for="co-ok-${i}">When they say</label>
      <input class="form-input co-ok" id="co-ok-${i}" value="${ce(k)}"></div>
    <div class="form-group"><label class="form-label" for="co-ov-${i}">Answer with</label>
      <textarea class="form-input co-ov" id="co-ov-${i}" rows="2">${ce(v)}</textarea></div>
  </div>`;
}

function addObjection() {
  const box = document.getElementById('co-objections');
  box.insertAdjacentHTML('beforeend', objectionRow('', '', box.children.length));
}

async function saveCompanySection(section, objectionsOnly) {
  const v = id => document.getElementById(id).value;
  let fields;
  if (section === 'product' && objectionsOnly) {
    const out = {};
    document.querySelectorAll('.company-objection').forEach(row => {
      const k = row.querySelector('.co-ok').value.trim(), a = row.querySelector('.co-ov').value.trim();
      if (k && a) out[k] = a;
    });
    fields = {objection_responses: out};
  } else if (section === 'product') {
    fields = {name: v('co-p-name'), pricing: v('co-p-pricing'), description: v('co-p-desc'),
              key_benefits: splitLines(v('co-p-benefits'))};
  } else if (section === 'icp') {
    fields = {industries: splitLines(v('co-i-industries')), titles: splitLines(v('co-i-titles')),
              company_size: v('co-i-size'), geography: splitLines(v('co-i-geo'))};
  } else if (section === 'persona') {
    fields = {name: v('co-s-name'), role: v('co-s-role'), tone: v('co-s-tone'),
              company: v('co-s-company'), email: v('co-s-email')};
  }
  const r = await api('/api/company', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                       body: JSON.stringify({section, fields})});
  if (r && r.success) showToast(r.message, 'success');
  else showToast((r && r.message) || 'Couldn\'t save.', 'error');
}

async function saveKnowledge(kind) {
  const el = document.getElementById(kind === 'knowledge' ? 'co-knowledge' : 'co-competitors');
  if (!el) return;
  const r = await api('/api/company/knowledge', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({kind, text: el.value})});
  if (r && r.success) showToast(r.message, 'success');
  else showToast((r && r.message) || 'Could not save.', 'error');
}

function renderOfferIdeas() {
  const box = document.getElementById('offer-ideas');
  if (!box) return;
  if (!_offerIdeas.length) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="form-label">Ideas to start from (click one to put it in the box, then make it yours)</div>' +
    _offerIdeas.map((idea, i) => `<button type="button" class="offer-idea" onclick="useOfferIdea(${i})">
      <span class="offer-idea-text">${ce(idea.offer)}</span>
      ${idea.finding ? `<span class="offer-idea-meta">Pairs with: ${ce(idea.finding)}</span>` : ''}
      ${idea.why ? `<span class="offer-idea-meta">${ce(idea.why)}</span>` : ''}
    </button>`).join('');
}

function useOfferIdea(i) {
  const el = document.getElementById('offer-entry');
  el.value = _offerIdeas[i].offer;
  el.focus();
}

async function suggestOffers() {
  const btn = document.getElementById('offer-suggest-btn');
  btn.disabled = true; btn.textContent = 'Thinking...';
  document.getElementById('offer-ideas').innerHTML =
    '<p class="desc">Sellbird is reading your product and coming up with offers that fit it...</p>';
  const r = await api('/api/offer/suggest', {method: 'POST'});
  btn.disabled = false; btn.textContent = 'Suggest ideas';
  if (!r || r.error || !r.ideas) { renderOfferIdeas(); showToast((r && r.error) || 'Sellbird couldn\'t come up with offers just now. Try again.', 'error'); return; }
  _offerIdeas = r.ideas;
  renderOfferIdeas();
}

async function previewOffer() {
  const entry = document.getElementById('offer-entry').value.trim();
  if (!entry) { showToast('Write an offer first.', 'error'); return; }
  const btn = document.getElementById('offer-preview-btn');
  const box = document.getElementById('offer-preview');
  btn.disabled = true; btn.textContent = 'Writing...';
  box.hidden = false;
  box.innerHTML = '<p class="desc">Writing a sample first email for one of your prospects. Nothing is saved or sent.</p>';
  box.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  const r = await api('/api/offer/preview', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                            body: JSON.stringify({entry})});
  btn.disabled = false; btn.textContent = 'Preview an email';
  if (!r || !r.ok) { box.innerHTML = `<p class="desc">${ce((r && r.message) || 'The preview failed. Try again.')}</p>`; return; }
  box.innerHTML = `<div class="form-label">Sample for ${ce(r.to)} (not saved, not sent)</div>
    <div class="offer-mail"><div class="offer-mail-subject">${ce(r.subject)}</div><pre>${ce(r.body)}</pre></div>`;
  box.scrollIntoView({block: 'nearest', behavior: 'smooth'});
}

async function saveOffer(entryOverride) {
  const entry = entryOverride != null ? entryOverride : document.getElementById('offer-entry').value.trim();
  const r = await api('/api/offer', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                     body: JSON.stringify({entry})});
  if (r && r.success) { showToast(r.message, 'success'); loadCompany(); }
  else showToast((r && r.message) || 'Couldn\'t save the offer.', 'error');
}

async function useProposedOffer() {
  if (_company && _company.offer && _company.offer.proposed) await saveOffer(_company.offer.proposed.entry);
}

async function dismissProposedOffer() {
  await api('/api/offer/propose', {method: 'DELETE'});
  loadCompany();
}

(function wireInboxDialog() {
  const d = document.getElementById('inbox-dialog');
  if (!d) return;
  d.addEventListener('close', () => { if (_inboxPoll) { clearInterval(_inboxPoll); _inboxPoll = null; } });
  d.addEventListener('click', e => { if (e.target === d) closeInboxDialog(); });
})();
