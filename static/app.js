'use strict';
// Pusharr front end: sign-in, this device's push subscription, preferences, mutes, approvals, admin.
// Names and wording come from the config: the server fills them into index.html (body data-*).

const $ = (s) => document.querySelector(s);
const SERVER = document.body.dataset.server;
let state = null;       // /api/me
let shows = [];

async function api(path, body) {
  const opts = { credentials: 'same-origin', headers: {} };
  if (body !== undefined) {
    opts.method = 'POST';
    opts.headers['Content-Type'] = 'application/json';
    if (state) opts.headers['X-CSRF-Token'] = state.csrf;
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && !['/api/login', '/api/me'].includes(path)) { show('signin'); throw new Error('signed out'); }
  if (!r.ok) throw new Error(data.error || `Error ${r.status}`);
  return data;
}

function show(id) {
  for (const m of ['loading', 'install', 'signin', 'app', 'note', 'settings']) $('#' + m).hidden = m !== id;
  $('#pointer').hidden = !(id === 'install' && env.how === 'ios-safari');
}

const standalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
const env = detectEnv(navigator.userAgent, { standalone, maxTouchPoints: navigator.maxTouchPoints });
const store = {          // storage can throw (private mode, blocked site data)
  get(k, s = localStorage) { try { return s.getItem(k); } catch { return null; } },
  set(k, v, s = localStorage) { try { s.setItem(k, v); } catch { /* ignore */ } },
};
let installPrompt = null;
const pushSupported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;

function b64ToBytes(s) {
  const pad = '='.repeat((4 - (s.length % 4)) % 4);
  const raw = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

async function registration() {
  return navigator.serviceWorker.register('/sw.js', { scope: '/' });
}

async function currentSub() {
  if (!pushSupported) return null;
  const reg = await registration();
  return reg.pushManager.getSubscription();
}

async function renderDevice() {
  const iosLocked = env.platform === 'ios' && !env.installed;
  $('#ios-lock').hidden = !iosLocked;
  if (iosLocked) {         // iOS web push only works from the home-screen app
    for (const b of ['#enable', '#disable']) $(b).hidden = true;
    $('#test').hidden = !state.devices;
    $('#device-status').textContent = '';
    $('#device-count').textContent = state.devices
      ? `Alerts go to ${state.devices} device${state.devices === 1 ? '' : 's'} signed in as you.` : '';
    return;
  }
  const sub = await currentSub().catch(() => null);
  let status;
  if (!pushSupported) {
    status = "This browser can't receive notifications.";
  } else if (Notification.permission === 'denied') {
    status = 'Notifications are blocked for this site. Allow them in your browser or phone settings, then reload.';
  } else if (sub) {
    status = `✅ This device gets ${SERVER} alerts.`;
  } else {
    status = 'This device is not getting alerts yet.';
  }
  $('#device-status').textContent = status;
  $('#enable').hidden = !pushSupported || !!sub || Notification.permission === 'denied';
  $('#disable').hidden = !sub;
  $('#test').hidden = !state.devices;
  $('#device-count').textContent = state.devices
    ? `Alerts go to ${state.devices} device${state.devices === 1 ? '' : 's'} signed in as you.` : '';
  $('#device-via').hidden = !sub;
  $('#device-hint').hidden = !(sub && !env.installed && env.platform === 'android');
  if (sub) {
    $('#device-via').textContent = `Notifications on this device come through: ${await browserName()}`
      + (env.installed ? ' (installed app)' : ' (browser tab)');
    api('/api/subscribe', subBody(sub)).then((me) => { state = me; }).catch(() => {});   // keep it fresh
  }
}

function subBody(sub) {
  const j = sub.toJSON();
  return { endpoint: j.endpoint, keys: j.keys, ua: `[${env.installed ? 'installed app' : 'browser tab'}] ${navigator.userAgent}` };
}

async function browserName() {
  try { if (navigator.brave && await navigator.brave.isBrave()) return 'Brave'; } catch { /* not Brave */ }
  return { chrome: 'Chrome', samsung: 'Samsung Internet', edge: 'Edge', opera: 'Opera', firefox: 'Firefox',
    safari: 'Safari', 'in-app': 'an in-app browser' }[env.browser] || 'this browser';
}

function tellWorkerMode() {      // lets the service worker reuse only installed-app windows
  if (navigator.serviceWorker && navigator.serviceWorker.controller) {
    navigator.serviceWorker.controller.postMessage({ type: 'mode', standalone: env.installed });
  }
}

async function enable() {
  const btn = $('#enable');
  btn.disabled = true;
  try {
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') throw new Error('Notifications were not allowed.');
    const reg = await registration();
    await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true, applicationServerKey: b64ToBytes(state.vapid_public_key),
    });
    state = await api('/api/subscribe', subBody(sub));
  } catch (e) {
    alert(e.message);
  } finally {
    btn.disabled = false;
    renderDevice();
  }
}

async function disable() {
  const sub = await currentSub();
  if (sub) {
    state = await api('/api/unsubscribe', { endpoint: sub.endpoint });
    await sub.unsubscribe();
  }
  renderDevice();
}

async function test() {
  const btn = $('#test');
  btn.disabled = true;
  try {
    const r = await api('/api/test', {});
    btn.textContent = r.delivered ? `Sent to ${r.delivered} device${r.delivered === 1 ? '' : 's'}` : 'Not delivered';
  } catch (e) { alert(e.message); }
  setTimeout(() => { btn.textContent = 'Send test notification'; btn.disabled = false; }, 4000);
}

// "New in <library>" rows: one per Jellyfin library this person can see (from /api/me)
function renderLibraryToggles() {
  const box = $('#lib-toggles');
  box.replaceChildren(...(state.libraries || []).map((lib) => {
    const row = Object.assign(document.createElement('div'), { className: 'toggle-mode' });
    const label = Object.assign(document.createElement('label'), { className: 'toggle' });
    const cb = Object.assign(document.createElement('input'), { type: 'checkbox' });
    cb.dataset.pref = lib.key;
    label.append(cb, Object.assign(document.createElement('span'), { textContent: `${lib.emoji} New in ${lib.label}` }));
    const sel = document.createElement('select');
    sel.dataset.pref = `${lib.key}_mode`;
    sel.setAttribute('aria-label', `When for new in ${lib.label}`);
    sel.append(new Option('Straight away', 'instant'), new Option(document.body.dataset.digestLabel, 'digest'));
    for (const el of [cb, sel]) el.addEventListener('change', () => savePref(el));
    row.append(label, sel);
    return row;
  }));
}

function renderPrefs() {
  const libKeys = (state.libraries || []).map((l) => l.key).join(',');
  if ($('#lib-toggles').dataset.keys !== libKeys) { renderLibraryToggles(); $('#lib-toggles').dataset.keys = libKeys; }
  for (const el of document.querySelectorAll('[data-pref]')) {
    const v = state.prefs[el.dataset.pref];
    if (el.type === 'checkbox') el.checked = !!v; else el.value = v;
  }
  for (const lib of state.libraries || []) {
    const sel = document.querySelector(`select[data-pref="${lib.key}_mode"]`);
    if (sel) sel.disabled = !state.prefs[lib.key];
  }
  // notification types switched off in the config aren't offered
  for (const el of document.querySelectorAll('[data-type]')) el.hidden = !(state.types || []).includes(el.dataset.type);
}

async function savePref(el) {
  const v = el.type === 'checkbox' ? el.checked : el.value;
  try {
    const r = await api('/api/prefs', { prefs: { [el.dataset.pref]: v } });
    state.prefs = r.prefs;
  } catch (e) { alert(e.message); }
  renderPrefs();
}

function renderMutes() {
  const ul = $('#mutes');
  ul.replaceChildren();
  for (const [key, name] of Object.entries(state.mutes)) {
    const li = document.createElement('li');
    li.textContent = name;
    const b = document.createElement('button');
    b.textContent = '✕';
    b.setAttribute('aria-label', `Unmute ${name}`);
    b.onclick = async () => { state.mutes = (await api('/api/mutes', { remove: key })).mutes; renderMutes(); };
    li.append(b);
    ul.append(li);
  }
}

async function loadShows() {
  shows = await api('/api/shows').catch(() => []);
  const dl = $('#shows');
  dl.replaceChildren(...shows.map((s) => Object.assign(document.createElement('option'), { value: s.name })));
}

async function mute(ev) {
  ev.preventDefault();
  const name = $('#mute-input').value.trim();
  const s = shows.find((x) => x.name.toLowerCase() === name.toLowerCase());
  if (!s) { alert('Pick a show from the list.'); return; }
  state.mutes = (await api('/api/mutes', { add: s })).mutes;
  $('#mute-input').value = '';
  renderMutes();
}

function ago(ts) {
  if (!ts) return '—';
  const m = Math.round((Date.now() / 1000 - ts) / 60);
  if (m < 60) return `${m} min ago`;
  if (m < 2880) return `${Math.round(m / 60)} h ago`;
  return `${Math.round(m / 1440)} days ago`;
}

async function renderAdmin() {
  $('#admin').hidden = !state.user.is_admin;
  if (!state.user.is_admin) return;
  const users = await api('/api/admin/users');
  const labels = { episodes: 'eps', requests: 'req', issues: 'fix', announcements: 'news', joins: 'joins' };
  for (const lib of state.libraries || []) labels[lib.key] = lib.label.toLowerCase();
  $('#users').replaceChildren(...users.map((u) => {
    const tr = document.createElement('tr');
    const on = Object.entries(labels).filter(([k]) => u.prefs[k])
      .map(([k, l]) => (k.startsWith('lib_') && u.prefs[k + '_mode'] === 'digest' ? `${l} (digest)` : l));
    const cells = [
      Object.assign(document.createElement('input'), { type: 'checkbox', value: u.id, title: `Send only to ${u.name}` }),
      u.name + (u.signed_in ? '' : ' (not signed in)'),
      String(u.devices), ago(u.last_push_ok), on.join(', ') + (u.mutes ? ` · ${u.mutes} muted` : ''),
    ];
    for (const c of cells) {
      const td = document.createElement('td');
      if (typeof c === 'string') td.textContent = c; else td.append(c);
      tr.append(td);
    }
    return tr;
  }));
}

// ---------- pending requests (admins) ----------

const DONE = { approved: 'Approved ✓', declined: 'Declined', gone: 'No longer exists' };

async function renderPending() {
  $('#pending').hidden = !state.user.is_approver;
  if (!state.user.is_approver) return;
  const [reqs, issues] = await Promise.all([
    api('/api/admin/requests').catch(() => []), api('/api/admin/issues').catch(() => [])]);
  $('#pending-empty').hidden = reqs.length + issues.length > 0;
  $('#pending-list').replaceChildren(...issues.map(issueItem), ...reqs.map(pendingItem));
  if (location.hash === '#pending') $('#pending').scrollIntoView();
}

function pendingItem(r) {
  const li = document.createElement('li');
  const img = r.poster ? Object.assign(document.createElement('img'), { alt: '', loading: 'lazy', src: r.poster })
    : Object.assign(document.createElement('div'), { className: 'no-poster' });
  const body = document.createElement('div');
  body.className = 'req-body';
  const tag = Object.assign(document.createElement('span'), { className: 'req-tag', textContent: 'Request' });
  const t = Object.assign(document.createElement('p'), { className: 'req-title',
    textContent: `${r.title} (${[r.year, r.what].filter(Boolean).join(', ')})` });
  const by = Object.assign(document.createElement('p'), { className: 'req-by', textContent: `Requested by ${r.by}` });
  const actions = document.createElement('div');
  actions.className = 'req-actions';
  const approve = Object.assign(document.createElement('button'), { textContent: 'Approve' });
  const decline = Object.assign(document.createElement('button'), { textContent: 'Decline', className: 'secondary' });
  const reasonBox = document.createElement('div');
  reasonBox.className = 'req-reason';
  reasonBox.hidden = true;
  const reason = Object.assign(document.createElement('input'), { placeholder: 'Reason (optional, sent to them)', maxLength: 200 });
  const confirmDecline = Object.assign(document.createElement('button'), { textContent: 'Decline request', className: 'secondary' });
  reasonBox.append(reason, confirmDecline);
  const decide = async (action) => {
    for (const b of [approve, decline, confirmDecline]) b.disabled = true;
    try {
      const res = await api('/api/admin/requests/decide', { id: r.id, action, reason: reason.value });
      const text = DONE[res.outcome] || (res.outcome.startsWith('already-')
        ? `Already ${res.outcome.slice(8)} in ${document.body.dataset.requestName}` : res.outcome);
      actions.replaceChildren(Object.assign(document.createElement('span'), { className: 'req-done', textContent: text }));
      reasonBox.hidden = true;
    } catch (e) {
      alert(e.message);
      for (const b of [approve, decline, confirmDecline]) b.disabled = false;
    }
  };
  approve.onclick = () => decide('approve');
  decline.onclick = () => { reasonBox.hidden = false; reason.focus(); };
  confirmDecline.onclick = () => decide('decline');
  actions.append(approve, decline);
  body.append(tag, t, by, actions, reasonBox);
  li.append(img, body);
  return li;
}

const ISSUE_DONE = { approved: 'Approved ✓, getting a new copy', ignored: 'Ignored', expired: 'Expired',
  unknown: 'No longer waiting' };

function issueItem(i) {
  const li = document.createElement('li');
  const img = i.poster ? Object.assign(document.createElement('img'), { alt: '', loading: 'lazy', src: i.poster })
    : Object.assign(document.createElement('div'), { className: 'no-poster' });
  const body = document.createElement('div');
  body.className = 'req-body';
  const tag = Object.assign(document.createElement('span'), { className: 'req-tag issue',
    textContent: `Problem report · ${(i.kind || '').toLowerCase()}` });
  const t = Object.assign(document.createElement('p'), { className: 'req-title', textContent: i.title });
  const by = Object.assign(document.createElement('p'), { className: 'req-by', textContent: `Reported by ${i.reporter}` });
  const comment = Object.assign(document.createElement('p'), { className: 'req-comment',
    textContent: i.comment ? `“${i.comment}”` : '' });
  const actions = document.createElement('div');
  actions.className = 'req-actions';
  const approve = Object.assign(document.createElement('button'), { textContent: 'Approve: get a new copy' });
  const ignore = Object.assign(document.createElement('button'), { textContent: 'Ignore', className: 'secondary' });
  const decide = async (action) => {
    approve.disabled = ignore.disabled = true;
    try {
      const res = await api('/api/admin/issues/decide', { id: i.issue_id, action });
      const text = ISSUE_DONE[res.outcome] || (res.outcome.startsWith('already-')
        ? `Already ${res.outcome.slice(8)}` : res.outcome);
      actions.replaceChildren(Object.assign(document.createElement('span'), { className: 'req-done', textContent: text }));
    } catch (e) {
      alert(e.message);
      approve.disabled = ignore.disabled = false;
    }
  };
  approve.onclick = () => decide('approve');
  ignore.onclick = () => decide('ignore');
  actions.append(approve, ignore);
  body.append(tag, t, by);
  if (i.comment) body.append(comment);
  body.append(actions);
  li.append(img, body);
  return li;
}

async function announce(ev) {
  ev.preventDefault();
  const f = ev.target;
  const users = [...document.querySelectorAll('#users input:checked')].map((i) => i.value);
  const to = users.length ? `${users.length} selected` : 'everyone';
  if (!confirm(`Send this announcement to ${to}?`)) return;
  try {
    const r = await api('/api/admin/announce', {
      title: f.elements.namedItem('title').value, body: f.elements.namedItem('body').value, users,
    });
    const sent = Object.values(r).filter((x) => x.delivered).length;
    $('#announce-result').textContent = `Sent: reached ${sent} of ${Object.keys(r).length} people.`;
    f.reset();
  } catch (e) { $('#announce-result').textContent = e.message; }
}

async function signin(ev) {
  ev.preventDefault();
  const f = ev.target;
  $('#signin-error').textContent = '';
  try {
    state = await api('/api/login', { username: f.username.value, password: f.password.value });
    f.password.value = '';
    enter();
  } catch (e) { $('#signin-error').textContent = e.message; }
}

let qcTimer = null;

function qcStop(message) {
  clearTimeout(qcTimer);
  qcTimer = null;
  $('#qc').hidden = !message;
  $('#qc-start').hidden = !document.body.dataset.qc;
  if (message) { $('#qc-code').textContent = ''; $('#qc-status').textContent = message; }
}

async function qcStart() {
  $('#signin-error').textContent = '';
  try {
    const r = await api('/api/qc/start', {});
    $('#qc-code').textContent = r.code;
    $('#qc-status').textContent = 'Waiting for you to approve it…';
    $('#qc').hidden = false;
    $('#qc-start').hidden = true;
    const until = Date.now() + r.expires_in * 1000;
    const poll = async () => {
      if (Date.now() > until) return qcStop('That code has expired. Tap the button for a new one.');
      try {
        const res = await fetch('/api/qc/poll', {
          method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: r.id }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok && !data.pending) { qcStop(); state = data; enter(); return; }
        if (!res.ok) return qcStop(data.error || 'Quick Connect failed. Try again.');
      } catch { /* network blip: keep trying */ }
      qcTimer = setTimeout(poll, 3000);
    };
    qcTimer = setTimeout(poll, 3000);
  } catch (e) { $('#signin-error').textContent = e.message; }
}

async function signout() {
  await api('/api/logout', {}).catch(() => {});
  state = null;
  show('signin');
}

// ---------- links (links.* in the config) ----------

const STORES = [         // [store key, label, OSes it's the first choice for]
  ['ios', 'App Store', ['ios', 'mac']],
  ['android', 'Google Play', ['android']],
  ['windows', 'Microsoft Store', ['windows']],
  ['android_tv', 'Android TV (APK)', ['android-tv']],
  ['other', 'Other platforms', ['linux', 'other']],
];

function setLink(el, url) {
  el.hidden = !url;
  if (url) el.href = url;
}

let linksCache = null;
async function getLinks() {
  if (!linksCache) {
    try { linksCache = await (await fetch('/api/links', { credentials: 'same-origin' })).json(); } catch { linksCache = {}; }
  }
  return linksCache;
}

// An app's store link for this device: [key, label] of the first fitting store, else "other platforms"
function appStore(app) {
  const stores = STORES.filter(([k]) => app && app.stores[k]);
  return stores.find(([, , oses]) => oses.includes(env.os)) || stores.find(([k]) => k === 'other') || stores[0];
}

const newLink = (href, text, cls) => Object.assign(document.createElement('a'),
  { href, textContent: text, target: '_blank', rel: 'noopener noreferrer', className: cls || '' });

async function renderLinks() {
  const links = await getLinks();
  const apps = links.apps || [];
  const alts = links.alternatives || [];
  setLink($('#l-watch'), links.watch);
  setLink($('#l-request'), links.request);
  setLink($('#f-watch'), links.watch);
  setLink($('#f-request'), links.request);
  $('#l-apps').replaceChildren(...apps.map((app) => {
    const first = appStore(app);
    const box = Object.assign(document.createElement('div'), { className: 'app-pick' });
    box.append(Object.assign(document.createElement('h3'), { textContent: `Recommended app: ${app.name}` }));
    if (app.description) box.append(Object.assign(document.createElement('p'), { className: 'muted', textContent: app.description }));
    box.append(newLink(app.stores[first[0]], `Get ${app.name} · ${first[1]}`, 'button-link'));
    const rest = STORES.filter(([k]) => app.stores[k] && k !== first[0]);
    if (rest.length) {
      const more = Object.assign(document.createElement('p'), { className: 'muted small' });
      more.append('Also on: ');
      rest.forEach(([k, label], i) => { if (i) more.append(' · '); more.append(newLink(app.stores[k], label)); });
      box.append(more);
    }
    return box;
  }));
  $('#l-alts').replaceChildren(...alts.map((a) => {
    const p = Object.assign(document.createElement('p'), { className: 'muted small alt-app' });
    const [before, after] = (a.text.includes('{link}') ? a.text : `${a.text} {link}`).split('{link}');
    p.append(before, newLink(a.url, a.name), after || '');
    return p;
  }));
  const first = apps.length ? appStore(apps[0]) : null;
  const getApp = links.get_app || (first ? apps[0].stores[first[0]] : (alts[0] || {}).url);
  const any = links.watch || links.request || apps.length || alts.length;
  $('#links').hidden = !any;
  setLink($('#f-apps'), getApp);
  $('#footer').hidden = !(any || getApp);
}

// ---------- note page: what a tapped notification opens ----------

const noteId = (location.pathname.match(/^\/n\/([A-Za-z0-9_-]{8,40})$/) || [])[1];

async function showNote(attempt = 0) {
  show('note');
  let n = null;
  try { n = await api(`/api/note/${noteId}`); } catch (e) { if (e.message === 'signed out') return; }
  $('#note-missing').hidden = !!n;
  if (!n) return;
  $('#note-title').textContent = n.title;
  $('#note-body').textContent = n.body || '';
  $('#note-when').textContent = new Date(n.created * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  const img = $('#note-img');
  img.hidden = !n.image;
  if (n.image) img.src = n.image;
  setLink($('#note-watch'), n.links.watch);
  // apps' deep links (e.g. app://play?...) only work where the app registers them (play_on)
  const known = (await getLinks()).apps || [];
  const buttons = [];
  for (const a of (n.links.apps || []).filter((x) => x.play_on.includes(env.os))) {
    buttons.push(Object.assign(document.createElement('a'), { href: a.url, textContent: `▶ Play in ${a.name}`,
      rel: 'noopener noreferrer', className: 'button-link big-link secondary-link' }));
    // a deep link does nothing when the app isn't installed: offer the store for this device under it
    const app = known.find((x) => x.name === a.name);
    const store = app && appStore(app);
    if (store) buttons.push(newLink(app.stores[store[0]], `Nothing happens? Get ${a.name}`, 'get-app'));
  }
  $('#note-apps').replaceChildren(...buttons);
  setLink($('#note-request'), n.links.request);
  // the item may still be on its way into Jellyfin (just imported): look again for a little while
  if (n.resolving && !n.links.watch && attempt < 12) setTimeout(() => showNote(attempt + 1), 5000);
}

// ---------- notification taps (with the service worker) ----------

function goTo(url) {
  const u = new URL(url, location.origin);
  if (u.origin === location.origin && u.pathname + u.hash !== location.pathname + location.hash) location.assign(u.href);
}

function askPending() {   // an installed-app window brought forward by a tap may not have been navigated
  if (env.installed && navigator.serviceWorker && navigator.serviceWorker.controller) {
    navigator.serviceWorker.controller.postMessage({ type: 'pending?' });
  }
}

// ---------- install as an app ----------

function installedFlagKey() { return `ba-installed-${env.platform}`; }

async function knownInstalledHere() {
  if (store.get(installedFlagKey())) return true;            // set by appinstalled / by the installed app
  if (state && (state.installed_platforms || []).includes(env.platform)) return true;
  try {
    if (navigator.getInstalledRelatedApps) return (await navigator.getInstalledRelatedApps()).length > 0;
  } catch { /* not supported */ }
  return false;
}

function renderInstall() {
  for (const el of document.querySelectorAll('#install [data-how]')) {
    el.hidden = !el.dataset.how.split(' ').includes(env.how);
  }
  $('#copy-url').value = location.origin + '/';
  $('#install-btn').hidden = !installPrompt;
  $('#install-fallback').hidden = !!installPrompt;
  $('#install-skip').textContent = env.how === 'none' ? 'Continue'
    : env.platform === 'ios' ? 'Continue in the browser (no notifications here)' : 'Continue in the browser for now';
}

function showInstall() { renderInstall(); show('install'); window.scrollTo(0, 0); }

async function continueFromInstall() {
  store.set('ba-skip-install', '1', sessionStorage);
  if (state) enter(); else show('signin');
}

async function installNow() {
  if (!installPrompt) return;
  installPrompt.prompt();
  const choice = await installPrompt.userChoice.catch(() => ({}));
  installPrompt = null;
  if (choice.outcome === 'accepted') installedDone(); else renderInstall();
}

function installedDone() {
  store.set(installedFlagKey(), '1');
  $('#installed-msg').hidden = false;
  $('#install-btn').hidden = true;
  $('#install-fallback').hidden = true;
}

window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();      // keep it for our own big button
  installPrompt = e;
  if (!$('#install').hidden) renderInstall();
});
window.addEventListener('appinstalled', installedDone);

async function copyLink() {
  const url = $('#copy-url').value;
  try { await navigator.clipboard.writeText(url); } catch {
    $('#copy-url').select();
    try { document.execCommand('copy'); } catch { /* user can copy by hand */ }
  }
  $('#copy-btn').textContent = 'Copied ✓';
  setTimeout(() => { $('#copy-btn').textContent = 'Copy link'; }, 2500);
}

async function renderOpenAppBanner() {
  const hide = env.installed || store.get('ba-hide-open-app', sessionStorage) || !(await knownInstalledHere());
  $('#open-app').hidden = !!hide;
  $('#open-app-where').textContent = env.platform === 'desktop' ? 'apps or Dock' : 'home screen';
  return !hide;
}

function enter() {
  $('#name').textContent = state.user.name;
  if (noteId) { showNote(); return; }
  if (location.pathname === '/settings') {
    if (state.user.is_admin && window.showSettings) { show('settings'); window.showSettings(); return; }
    history.replaceState(null, '', '/');
  }
  if (env.installed) {     // remember (here and on the server) that this platform has the app
    store.set(installedFlagKey(), '1');
    if (!(state.installed_platforms || []).includes(env.platform)) {
      api('/api/installed', { platform: env.platform }).then((me) => { state = me; }).catch(() => {});
    }
  } else {
    renderOpenAppBanner();
  }
  show('app');
  renderPrefs();
  renderMutes();
  renderDevice();
  renderAdmin();
  renderPending();
  loadShows();
}

document.addEventListener('DOMContentLoaded', async () => {
  // sign-in methods switched off in the config (jellyfin.sign_in)
  $('#signin-form').hidden = !document.body.dataset.password;
  $('#qc-start').hidden = !document.body.dataset.qc;
  $('#signin-or').hidden = !(document.body.dataset.password && document.body.dataset.qc);
  $('#signin-form').addEventListener('submit', signin);
  $('#signout').addEventListener('click', signout);
  $('#qc-start').addEventListener('click', qcStart);
  $('#qc-cancel').addEventListener('click', () => qcStop());
  $('#enable').addEventListener('click', enable);
  $('#disable').addEventListener('click', disable);
  $('#test').addEventListener('click', test);
  $('#mute-form').addEventListener('submit', mute);
  $('#announce-form').addEventListener('submit', announce);
  for (const el of document.querySelectorAll('[data-pref]')) el.addEventListener('change', () => savePref(el));   // fixed rows
  window.addEventListener('hashchange', () => { if (location.hash === '#pending' && state) renderPending(); });
  $('#install-btn').addEventListener('click', installNow);
  $('#install-skip').addEventListener('click', continueFromInstall);
  $('#show-install').addEventListener('click', showInstall);
  $('#copy-btn').addEventListener('click', copyLink);
  $('#open-app-close').addEventListener('click', () => {
    store.set('ba-hide-open-app', '1', sessionStorage);
    $('#open-app').hidden = true;
  });
  document.body.classList.add(`is-${env.platform}`);
  renderLinks();
  if (env.ipad) document.body.classList.add('is-ipad');
  if (pushSupported) {
    registration().catch(() => {});
    navigator.serviceWorker.ready.then(() => { tellWorkerMode(); askPending(); }).catch(() => {});
    navigator.serviceWorker.addEventListener('controllerchange', tellWorkerMode);
    navigator.serviceWorker.addEventListener('message', (e) => {
      const d = e.data || {};
      if (d.type === 'which' && e.ports && e.ports[0]) e.ports[0].postMessage({ standalone: env.installed });
      if (d.type === 'go' && d.url) goTo(d.url);
    });
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') askPending(); });
  }
  try { state = await api('/api/me'); } catch { state = null; }
  // Not installed, not known to be installed, not skipped this session: install screen first
  // (not when a notification brought them to /n/<id>: show them what it was about).
  if (!noteId && location.pathname !== '/settings' && !env.installed && !store.get('ba-skip-install', sessionStorage)
      && !(await knownInstalledHere())) {
    showInstall();
  } else if (state) {
    enter();
  } else {
    show('signin');
    if (!env.installed) renderOpenAppBanner();
  }
});
