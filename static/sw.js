'use strict';
// Service worker: shows pushes, handles Approve / Decline buttons, opens the right page on tap.
const VERSION = '2026-10-27.2-{{version}}';   // bump on code changes; {{version}} changes with the config

const APP = '{{app}}';
const ICON = '{{icon:icon-192.png}}';
const BADGE = '{{icon:badge-96.png}}';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch { d = { title: APP, body: event.data && event.data.text() }; }
  const opts = {
    body: d.body || '',
    icon: ICON,
    badge: BADGE,
    data: { url: d.url || '/', token: d.token || null },
  };
  if (d.image) opts.image = d.image;          // big picture on Android/desktop Chrome; ignored on iOS
  if (d.tag) { opts.tag = d.tag; opts.renotify = !d.silent; }
  if (d.silent) opts.silent = true;
  if (d.actions && d.token) opts.actions = d.actions;   // Approve / Decline (Android, desktop)
  event.waitUntil(self.registration.showNotification(d.title || APP, opts));
});

const RESULT = {
  approved: 'Approved ✓', declined: 'Declined', ignored: 'Ignored', expired: 'That report has expired',
  unknown: 'No longer waiting for you',
  'already-approved': 'Already approved in {{request_name}}', 'already-declined': 'Already declined in {{request_name}}',
  'already-completed': 'Already handled in {{request_name}}', 'already-failed': 'Already handled in {{request_name}}',
  gone: 'That request no longer exists',
};

async function act(notification, action) {
  const { token } = notification.data || {};
  let title, body = '';
  try {
    const sub = await self.registration.pushManager.getSubscription();
    const r = await fetch('/api/action', {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token, action, endpoint: sub ? sub.endpoint : '' }),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok) {
      title = data.text || RESULT[data.outcome] || data.outcome;
      body = data.title || '';
    } else {
      title = data.error === 'already used' ? 'Already answered on this phone' : "Couldn't do that from here";
      body = `Open ${APP} → Pending requests.`;
    }
  } catch {
    title = `Couldn't reach ${APP}`;
    body = 'Open the app to approve or decline.';
  }
  await self.registration.showNotification(title, {
    body, tag: notification.tag, silent: true, renotify: false,
    icon: ICON, badge: BADGE, data: { url: '/#pending' },
  });
}

// Which window is the installed app? A browser tab of the same site is also a "window" client, and
// reusing it would open the tab instead of the app. The worker is stopped whenever it's idle, so
// nothing is remembered between taps: each tap asks the open windows (they know their display mode).
const appWindows = new Set();     // windows that said so since this worker started (a hint only)
const pending = { url: null, at: 0 };   // the last tap, for an app window that comes forward un-navigated

self.addEventListener('message', (e) => {
  const d = e.data || {};
  if (d.type === 'mode' && e.source) {
    if (d.standalone) appWindows.add(e.source.id); else appWindows.delete(e.source.id);
  }
  if (d.type === 'pending?' && e.source && pending.url && Date.now() - pending.at < 30000) {
    e.source.postMessage({ type: 'go', url: pending.url });
    pending.url = null;
  }
});

async function appClients() {
  const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  const answers = await Promise.all(wins.map((w) => new Promise((resolve) => {
    const ch = new MessageChannel();
    const done = (v) => { clearTimeout(timer); ch.port1.close(); resolve(v); };
    const timer = setTimeout(() => done(appWindows.has(w.id) ? w : null), 500);
    ch.port1.onmessage = (ev) => done(ev.data && ev.data.standalone ? w : null);
    try { w.postMessage({ type: 'which' }, [ch.port2]); } catch { done(appWindows.has(w.id) ? w : null); }
  })));
  return answers.filter(Boolean);
}

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  if (['approve', 'decline', 'ignore'].includes(event.action) && event.notification.data && event.notification.data.token) {
    event.waitUntil(act(event.notification, event.action));
    return;
  }
  // Always an absolute URL inside the app's scope ('/'), e.g. /n/<id> or /#pending.
  let url = new URL((event.notification.data && event.notification.data.url) || '/', self.location.origin);
  if (url.origin !== self.location.origin) url = new URL('/', self.location.origin);
  url = url.href;
  pending.url = url;
  pending.at = Date.now();
  event.waitUntil((async () => {
    const app = await appClients();
    const same = app.find((w) => w.url === url);
    if (same) { pending.url = null; return same.focus(); }
    if (app.length) {                     // an open installed-app window: bring it forward and go there
      const w = (await app[0].focus()) || app[0];
      try {
        await w.navigate(url);
        pending.url = null;
      } catch {
        w.postMessage({ type: 'go', url });    // not controlled by this worker: the page navigates itself
      }
      return;
    }
    // No installed-app window open: openWindow on an in-scope URL opens the installed app (Android
    // WebAPK installs via Chrome / Samsung Internet), otherwise a browser tab. If Android only brings
    // the running app forward, the app asks for `pending` when it becomes visible.
    return self.clients.openWindow(url);
  })());
});
