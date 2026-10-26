'use strict';
// Service worker: shows pushes, handles Approve / Decline buttons, opens the right page on tap.
const VERSION = '2026-10-27.1-{{version}}';   // bump on code changes; {{version}} changes with the config

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

// Windows that told us they run as the installed app (display-mode standalone). A browser tab of the
// same site is also a "window" client, and reusing it would open the tab instead of the app.
const appWindows = new Set();
self.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'mode' && e.source) {
    if (e.data.standalone) appWindows.add(e.source.id); else appWindows.delete(e.source.id);
  }
});

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
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const app = wins.filter((w) => appWindows.has(w.id));
    const same = app.find((w) => w.url === url);
    if (same) return same.focus();
    if (app.length) {                     // an open installed-app window: bring it forward and go there
      const w = await app[0].focus();
      return (w || app[0]).navigate(url).catch(() => self.clients.openWindow(url));
    }
    // No installed-app window open: openWindow on an in-scope URL opens the installed app (Android
    // WebAPK installs via Chrome / Samsung Internet), otherwise a browser tab.
    return self.clients.openWindow(url);
  })());
});
