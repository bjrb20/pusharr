'use strict';
// Notification taps in the service worker (static/sw.js), with fake windows: the installed app is found by
// asking the open windows (not from memory the worker loses when it's stopped), browser tabs are skipped,
// and an app window that can't be navigated, or comes forward without navigating, still gets there.
//
//   docker run --rm -v "$PWD":/w -w /w node:22-alpine node tests/test_sw.js
const fs = require('fs');

let ok = 0;
function check(cond, what) {
  if (!cond) { console.error('FAIL', what); process.exit(1); }
  ok += 1;
  console.log('ok  ', what);
}

function load() {
  const listeners = {};
  const self = {
    location: { origin: 'https://alerts.test' },
    addEventListener: (type, fn) => { listeners[type] = fn; },
    skipWaiting() {}, registration: { showNotification: async () => {} },
    clients: { windows: [], opened: [], claim: async () => {},
      matchAll: async () => self.clients.windows,
      openWindow: async (url) => { self.clients.opened.push(url); } },
  };
  const src = fs.readFileSync('static/sw.js', 'utf8').replace(/\{\{[^}]+\}\}/g, 'x');
  new Function('self', 'MessageChannel', 'fetch', src)(self, MessageChannel, async () => ({}));
  return { self, listeners };
}

function win(id, url, { standalone = null, navigable = true } = {}) {
  const w = { id, url, navigated: [], messages: [], focused: 0,
    focus: async () => { w.focused += 1; return w; },
    navigate: async (to) => { if (!navigable) throw new Error('not controlled'); w.navigated.push(to); w.url = to; return w; },
    postMessage: (msg, ports) => {
      w.messages.push(msg);
      if (msg.type === 'which' && ports && standalone !== null) { ports[0].postMessage({ standalone }); ports[0].close(); }
    } };
  return w;
}

async function tap(env, url) {
  let done;
  env.listeners.notificationclick({
    action: '', notification: { close() {}, data: { url } },
    waitUntil: (p) => { done = p; },
  });
  await done;
}

(async () => {
  let env = load();
  const tab = win('tab', 'https://alerts.test/', { standalone: false });
  const app = win('app', 'https://alerts.test/', { standalone: true });
  env.self.clients.windows = [tab, app];
  await tap(env, '/n/abcdefgh123');
  check(app.navigated[0] === 'https://alerts.test/n/abcdefgh123' && !tab.navigated.length && !env.self.clients.opened.length,
    'the open installed-app window goes to the note; the browser tab is left alone (no memory needed)');

  env = load();
  const tab2 = win('tab', 'https://alerts.test/', { standalone: false });
  env.self.clients.windows = [tab2];
  await tap(env, '/n/abcdefgh123');
  check(env.self.clients.opened[0] === 'https://alerts.test/n/abcdefgh123' && !tab2.navigated.length,
    'only a browser tab open: the note opens via openWindow (the installed app on Android)');

  env = load();
  const app2 = win('app', 'https://alerts.test/', { standalone: true, navigable: false });
  env.self.clients.windows = [app2];
  await tap(env, '/n/abcdefgh123');
  check(app2.messages.some((m) => m.type === 'go' && m.url === 'https://alerts.test/n/abcdefgh123'),
    'app window the worker cannot navigate: told to go there itself');

  env = load();
  const silent = win('old', 'https://alerts.test/', { standalone: null });   // doesn't answer (e.g. old page)
  env.self.clients.windows = [silent];
  await tap(env, '/n/abcdefgh123');
  check(env.self.clients.opened.length === 1, 'a window that does not answer is not mistaken for the app');
  const asker = win('app', 'https://alerts.test/');
  env.listeners.message({ data: { type: 'pending?' }, source: asker });
  check(asker.messages[0] && asker.messages[0].type === 'go' && asker.messages[0].url === 'https://alerts.test/n/abcdefgh123',
    'the app coming forward without navigating asks for the tapped note and gets it');
  const again = win('app2', 'https://alerts.test/');
  env.listeners.message({ data: { type: 'pending?' }, source: again });
  check(!again.messages.length, '... only once');

  env = load();
  const onNote = win('app', 'https://alerts.test/n/abcdefgh123', { standalone: true });
  env.self.clients.windows = [onNote];
  await tap(env, '/n/abcdefgh123');
  check(onNote.focused === 1 && !onNote.navigated.length, 'already on that note: just brought forward');

  env = load();
  await tap(env, 'https://evil.test/x');
  check(env.self.clients.opened[0] === 'https://alerts.test/', 'outside URLs are never opened');

  console.log(`\nall ${ok} service worker checks passed`);
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
