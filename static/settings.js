'use strict';
// Branding & settings page (admins): a form over config.yaml, plus a raw YAML editor. The server
// validates everything (config.py) and keeps the previous file as config.yaml.bak.

(() => {
  const $ = (s) => document.querySelector(s);
  let view = null;        // GET /api/admin/settings

  const THEME_LABELS = {
    bg: 'Page background', panel: 'Panels', panel_2: 'Cards inside panels', line: 'Borders', text: 'Text',
    text_2: 'Secondary text', muted: 'Muted text', heading: 'Headings', accent: 'Accent (buttons, toggles, wordmark)',
    accent_dark: 'Accent, hovered', accent_text: 'Text on accent', ok: 'Success', header_top: 'Header, top of the fade',
    header_line: 'Header bottom line', secondary: 'Secondary buttons', secondary_hover: 'Secondary buttons, hovered',
    field: 'Input fields', field_line: 'Input field borders', link: 'Links', theme_color: 'Browser bar (theme colour)',
    background_color: 'Splash screen background',
  };
  const TYPE_LABELS = {
    episodes: 'New episodes', requests: 'Requests ready', issues: 'Reported problems fixed', announcements: 'Announcements',
    approvals: 'Approvals (approvers)',
    auto_approved: 'Auto-approved requests (approvers)',
  };

  // [path, label, type, extra]; types: text, number, bool, select, color, font, list, yaml, textarea, icon, upload
  function sections(c) {
    const s = [
      ['Branding', [
        ['branding.app_name', 'App name', 'text'],
        ['branding.short_name', 'Short name (under the home-screen icon)', 'text'],
        ['branding.server_name', 'Your media server, as people know it ("… is ready on <name>")', 'text'],
        ['branding.description', 'Description (install prompts); {server} is the server name', 'text'],
        ['branding.icon_dir', 'Icon', 'icon'],
        ['branding.wordmark.style', 'Wordmark', 'select', ['text', 'image']],
        ['branding.wordmark.text', 'Wordmark text', 'text'],
        ['branding.wordmark.subtitle', 'Line under the wordmark', 'text'],
        ['branding.wordmark.weight', 'Wordmark weight (100–900)', 'number'],
        ['branding.wordmark.letter_spacing', 'Wordmark letter spacing (e.g. 2px)', 'text'],
        ['branding.wordmark.glow', 'Wordmark glow', 'bool'],
        ['branding.wordmark.image', 'Wordmark image (SVG, PNG or WebP)', 'upload', 'wordmark'],
        ['branding.fonts.body', 'Text font', 'font'],
        ['branding.fonts.wordmark', 'Wordmark font', 'font'],
      ]],
      ['Colours', Object.keys(c.branding.theme).map((k) => [`branding.theme.${k}`, THEME_LABELS[k] || k, 'color'])],
      ['Address and sign-in', [
        ['public_url', 'Public address of this app (https://)', 'text'],
        ['timezone', 'Time zone (e.g. Europe/London)', 'text'],
        ['jellyfin.url', 'Jellyfin, as this server reaches it', 'text'],
        ['jellyfin.public_url', 'Jellyfin, as people reach it (links and posters)', 'text'],
        ['jellyfin.sign_in.password', 'Sign in with username and password', 'bool'],
        ['jellyfin.sign_in.quick_connect', 'Sign in with Quick Connect', 'bool'],
        ['session.days', 'Stay signed in for (days)', 'number'],
        ['extra_origins', 'Extra allowed origins (one per line)', 'list'],
      ]],
      ['Apps and links', [
        ['links.watch', '"Watch" link (https://)', 'text'],
        ['links.watch_item', 'Link to one item; {jellyfin} {item_id} {server_id}', 'text'],
        ['links.watch_home', 'Link to the library; {jellyfin}', 'text'],
        ['links.request', '"Request" link (https://)', 'text'],
        ['links.request_name', 'Request service name', 'text'],
        ['links.request_item', 'Request page for one item; {request} {media} {tmdb}', 'text'],
        ['links.get_app', '"Get the app" link (empty: the first recommended app)', 'text'],
        ['links.recommended_apps', 'Recommended apps (YAML list: name, description, stores, play, play_on)', 'yaml'],
        ['links.alternative_apps', 'Alternative apps (YAML list: name, url, text with {link})', 'yaml'],
      ]],
      ['Libraries ("New in …" alerts)', [
        ['libraries', 'Per library, by name: show, label, emoji, default (on for new people), order. Found in Jellyfin: '
          + view.jellyfin_libraries.map((l) => `${l.name} (${l.type}${l.show ? '' : ', hidden'})`).join(', '), 'yaml'],
      ]],
      ['Notifications', [
        ...Object.keys(c.notifications.types).flatMap((k) => [
          [`notifications.types.${k}.enabled`, `${TYPE_LABELS[k] || k}: offered`, 'bool'],
          [`notifications.types.${k}.default`, `${TYPE_LABELS[k] || k}: on for new people`, 'bool'],
        ]),
        ['notifications.digest_hour', 'Daily digest at (hour, 0–23)', 'number'],
        ['notifications.batching.import_wait', 'Wait after an import before telling people (seconds)', 'number'],
        ['notifications.batching.library_wait', 'New films/shows: wait before sending (seconds)', 'number'],
        ['notifications.batching.library_burst_limit', 'New films/shows: more than this at once…', 'number'],
        ['notifications.batching.library_burst_window', '…within this many seconds → one summary', 'number'],
        ['notifications.wait_for_ready.enabled', 'Hold alerts until the item is in Jellyfin with its title, overview and poster', 'bool'],
        ['notifications.wait_for_ready.check_every', '… look every (seconds, at first)', 'number'],
        ['notifications.wait_for_ready.check_every_max', '… backing off to every (seconds)', 'number'],
        ['notifications.wait_for_ready.refresh_after', '… ask Jellyfin to look after (seconds)', 'number'],
        ['notifications.wait_for_ready.max_wait_hours', '… send anyway after (hours)', 'number'],
        ['notifications.follow.recent_watch_days', 'New episodes: watched in the last (days)', 'number'],
        ['notifications.follow.caught_up', 'New episodes: caught up on it', 'bool'],
        ['notifications.follow.watchlist', 'New episodes: on their watchlist', 'bool'],
        ['notifications.follow.requested', 'New episodes: requested it', 'bool'],
      ]],
      ['Messages', Object.entries(c.messages).flatMap(([k, v]) => {
        const vars = (view.message_vars[k] || []).map((x) => `{${x}}`).join(' ');
        return typeof v === 'object'
          ? [[`messages.${k}.title`, `${k}: title`, 'text', vars], [`messages.${k}.body`, `${k}: text`, 'textarea', vars]]
          : [[`messages.${k}`, k, 'text', vars]];
      })],
      ['Integrations', [
        ['integrations.seerr.enabled', 'Seerr / Jellyseerr / Overseerr', 'bool'],
        ['integrations.seerr.url', 'Seerr address (as this server reaches it)', 'text'],
        ['integrations.sonarr.enabled', 'Sonarr', 'bool'],
        ['integrations.sonarr.url', 'Sonarr address', 'text'],
        ['integrations.radarr.enabled', 'Radarr', 'bool'],
        ['integrations.radarr.url', 'Radarr address', 'text'],
        ['integrations.bazarr.enabled', 'Bazarr (subtitle problem reports)', 'bool'],
        ['integrations.bazarr.url', 'Bazarr address', 'text'],
        ['integrations.bazarr.language', 'Subtitle language code (e.g. en)', 'text'],
        ['integrations.bazarr.language_name', 'Subtitle language name', 'text'],
        ['integrations.wizarr.enabled', 'Wizarr → Seerr user sync', 'bool'],
        ['integrations.wizarr.database', "Wizarr's database file (mounted read-only)", 'text'],
        ['integrations.wizarr.sync_minutes', 'Sync every (minutes)', 'number'],
      ]],
      ['Approvals and replacement copies', [
        ['approvals.approvers', "Approvers: admins (Jellyfin administrators) or a YAML list of usernames", 'yaml'],
        ['approvals.token_hours', 'Approve buttons work for (hours)', 'number'],
        ['approvals.issue_expire_hours', 'Problem reports wait for an answer (hours)', 'number'],
        ['approvals.picker.languages', 'Wanted audio languages (one per line, as Radarr/Sonarr name them)', 'list'],
        ['approvals.picker.allow_original_language', "Also accept the title's original language", 'bool'],
        ['approvals.picker.dub_tags', 'Reject: foreign-dub tags (patterns, one per line)', 'list'],
        ['approvals.picker.language_tags', 'Tags that mean a wanted language is included', 'list'],
        ['approvals.picker.other_language_tags', 'Other-language tags (fine only with a wanted language)', 'list'],
        ['approvals.picker.hardsub_tags', 'Reject: hard-coded subtitle tags', 'list'],
        ['approvals.picker.camera_tags', 'Reject: camera / pre-release tags', 'list'],
        ['approvals.picker.min_size_mb.movie', 'Smallest film (MB)', 'number'],
        ['approvals.picker.min_size_mb.episode', 'Smallest episode (MB)', 'number'],
      ]],
      ['Server', [
        ['server.public_port', 'Public port (restart needed)', 'number'],
        ['server.internal_port', 'Internal API port (restart needed)', 'number'],
        ['server.webhook_port', 'Webhook port (restart needed)', 'number'],
        ['session.cookie_name', 'Session cookie name (changing it signs everyone out)', 'text'],
        ['vapid_subject', 'Push contact (mailto: or https://; empty = public address)', 'text'],
      ]],
    ];
    return s;
  }

  const getPath = (o, p) => p.split('.').reduce((x, k) => (x == null ? x : x[k]), o);
  function setPath(o, p, v) {
    const ks = p.split('.');
    const last = ks.pop();
    const t = ks.reduce((x, k) => (x[k] = x[k] && typeof x[k] === 'object' ? x[k] : {}), o);
    t[last] = v;
  }
  const el = (tag, props, ...kids) => { const e = Object.assign(document.createElement(tag), props || {}); e.append(...kids); return e; };

  async function api(path, body, raw) {
    const me = await (await fetch('/api/me', { credentials: 'same-origin' })).json();
    const opts = { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': me.csrf } };
    if (raw) { Object.assign(opts.headers, raw.headers); opts.body = raw.body; } else {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(path, opts);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `Error ${r.status}`);
    return data;
  }

  function toYaml(v) {        // small YAML writer for the yaml fields (lists of mappings, strings)
    if (typeof v === 'string') return v;
    const scalar = (x) => (typeof x === 'string' && (x === '' || /[:#{}[\],&*!|>'"%@`]|^\s|\s$/.test(x)) ? JSON.stringify(x) : String(x));
    const lines = [];
    const walk = (x, ind) => {
      if (Array.isArray(x)) {
        for (const item of x) {
          if (item && typeof item === 'object') {
            const sub = [];
            const save = lines.length;
            walk(item, ind + 2);
            sub.push(...lines.splice(save));
            lines.push(' '.repeat(ind) + '- ' + sub[0].trimStart(), ...sub.slice(1));
          } else lines.push(' '.repeat(ind) + '- ' + scalar(item));
        }
      } else {
        for (const [k, val] of Object.entries(x)) {
          if (val && typeof val === 'object' && (Array.isArray(val) ? val.length : Object.keys(val).length)) {
            lines.push(' '.repeat(ind) + k + ':');
            walk(val, ind + 2);
          } else lines.push(' '.repeat(ind) + k + ': ' + (Array.isArray(val) ? '[]' : val && typeof val === 'object' ? '{}' : scalar(val)));
        }
      }
    };
    walk(v, 0);
    return lines.join('\n') || (Array.isArray(v) ? '[]' : '{}');
  }

  function field([path, label, type, extra]) {
    const c = view.config;
    const v = getPath(c, path);
    const id = 'f-' + path.replace(/\W/g, '-');
    let input;
    if (type === 'bool') {
      input = el('input', { type: 'checkbox', id, checked: !!v });
      return el('label', { className: 'toggle setting', htmlFor: id }, input, el('span', { textContent: label }));
    }
    if (type === 'select' || type === 'font') {
      const opts = type === 'font' ? view.fonts : extra;
      input = el('select', { id }, ...opts.map((o) => el('option', { value: o, textContent: o, selected: o === v })));
    } else if (type === 'color') {
      input = el('input', { type: 'color', id, value: /^#[0-9a-f]{6}$/i.test(v) ? v : '#000000' });
    } else if (type === 'number') {
      input = el('input', { type: 'number', id, value: v });
    } else if (type === 'list') {
      input = el('textarea', { id, rows: Math.min(8, Math.max(2, (v || []).length + 1)), value: (v || []).join('\n') });
    } else if (type === 'yaml') {
      input = el('textarea', { id, rows: 6, value: toYaml(v), spellcheck: false, className: 'mono' });
    } else if (type === 'textarea') {
      input = el('textarea', { id, rows: 2, value: v || '' });
    } else {
      input = el('input', { type: 'text', id, value: v == null ? '' : v });
    }
    input.dataset.path = path;
    input.dataset.kind = type;
    const wrap = el('label', { className: 'setting', htmlFor: id }, el('span', { className: 'setting-label', textContent: label }));
    if (type === 'icon') return iconField(path, v);
    if (type === 'upload') {
      input.readOnly = true;
      const file = el('input', { type: 'file', accept: '.svg,.png,.webp,image/svg+xml,image/png,image/webp' });
      file.onchange = () => upload(extra, file.files[0], {}).then((r) => { input.value = r.value; }).catch((e) => alert(e.message));
      wrap.append(input, file);
      return wrap;
    }
    wrap.append(input);
    if (type === 'font') {
      const file = el('input', { type: 'file', accept: '.woff2,.ttf,.otf' });
      file.onchange = () => upload('font', file.files[0], {}).then((r) => {
        for (const s of document.querySelectorAll('select[data-kind="font"]')) {
          if (![...s.options].some((o) => o.value === r.value)) s.append(el('option', { value: r.value, textContent: r.value }));
        }
        input.value = r.value;
      }).catch((e) => alert(e.message));
      wrap.append(el('span', { className: 'muted small', textContent: 'Add a font: ' }), file);
    }
    if (extra && type !== 'select') wrap.append(el('span', { className: 'muted small', textContent: extra }));
    return wrap;
  }

  function iconField(path, v) {
    const preview = el('img', { className: 'icon-preview', alt: 'Current icon', src: v ? `/brand/${v}/icon-192.png` : '/icons/icon-192.png' });
    const select = el('select', { id: 'f-icon' }, el('option', { value: '', textContent: 'Built-in Pusharr icon', selected: !v }),
      ...view.icon_dirs.map((d) => el('option', { value: d, textContent: d, selected: d === v })));
    select.dataset.path = path;
    select.dataset.kind = 'select';
    select.onchange = () => { preview.src = select.value ? `/brand/${select.value}/icon-192.png` : '/icons/icon-192.png'; };
    const mode = el('select', {}, el('option', { value: 'mark', textContent: 'The SVG is a symbol: put it on a background' }),
      el('option', { value: 'full', textContent: 'The SVG is the whole icon' }));
    const bg = el('input', { type: 'color', value: '#000000' });
    const file = el('input', { type: 'file', accept: '.svg,image/svg+xml' });
    file.onchange = () => upload('icon', file.files[0], { mode: mode.value, background: bg.value }).then((r) => {
      if (![...select.options].some((o) => o.value === r.value)) select.append(el('option', { value: r.value, textContent: r.value }));
      select.value = r.value;
      select.onchange();
    }).catch((e) => alert(e.message));
    return el('div', { className: 'setting icon-setting' },
      el('span', { className: 'setting-label', textContent: 'Icon' }), preview, select,
      el('p', { className: 'muted small', textContent: 'Upload one SVG and every size is made from it (app icons, maskable, '
        + 'Apple, favicon and the monochrome status-bar badge).' }),
      el('label', { className: 'setting' }, el('span', { className: 'setting-label', textContent: 'New icon from an SVG' }), mode,
        el('span', { className: 'muted small', textContent: 'Background colour' }), bg, file));
  }

  async function upload(kind, f, params) {
    if (!f) throw new Error('No file chosen.');
    const q = new URLSearchParams({ kind, ...params });
    return api(`/api/admin/upload?${q}`, null, { headers: { 'X-Filename': f.name, 'Content-Type': 'application/octet-stream' }, body: f });
  }

  function collect() {
    const conf = JSON.parse(JSON.stringify(view.config));
    for (const input of document.querySelectorAll('#settings-form [data-path]')) {
      const { path, kind } = input.dataset;
      let v;
      if (kind === 'bool') v = input.checked;
      else if (kind === 'number') v = input.value === '' ? null : Number(input.value);
      else if (kind === 'list') v = input.value.split('\n').map((x) => x.trim()).filter(Boolean);
      else if (kind === 'yaml') v = { __yaml__: input.value };
      else v = input.value;
      setPath(conf, path, v);
    }
    return conf;
  }

  function showResult(r) {
    $('#settings-problems').hidden = r.ok;
    $('#settings-problems').replaceChildren(...(r.problems || []).map((p) => el('div', { textContent: p })));
    $('#settings-saved').hidden = !r.ok;
    if (r.ok) {
      $('#settings-saved').textContent = r.checked ? 'Looks fine.' : 'Saved. Reloading with the new settings…';
      if (!r.checked) setTimeout(() => location.reload(), 900);
    } else $('#settings-problems').scrollIntoView({ block: 'center' });
  }

  async function save(body) {
    try { showResult(Object.assign(await api('/api/admin/settings', body), { checked: !!body.check })); } catch (e) { alert(e.message); }
  }

  function render() {
    const form = $('#settings-form');
    form.replaceChildren(...sections(view.config).map(([title, fields]) => el('section', {},
      el('h2', { textContent: title }), ...fields.map(field))));
    form.append(el('div', { className: 'row sticky-save' }, el('button', { type: 'submit', textContent: 'Save settings' })));
    $('#settings-yaml-text').value = view.yaml;
    const missing = Object.entries(view.secrets).filter(([, set]) => !set).map(([k]) => k);
    if (missing.length) form.prepend(el('p', { className: 'muted small', textContent: `Not set in the env file: ${missing.join(', ')}` }));
  }

  window.showSettings = async () => {
    try {
      const r = await fetch('/api/admin/settings', { credentials: 'same-origin' });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `Error ${r.status}`);
      view = await r.json();
    } catch (e) { $('#settings-form').replaceChildren(el('p', { className: 'error', textContent: e.message })); return; }
    render();
  };

  document.addEventListener('DOMContentLoaded', () => {
    $('#settings-form').addEventListener('submit', (ev) => { ev.preventDefault(); save({ config: collect() }); });
    $('#settings-yaml-toggle').addEventListener('click', () => {
      const yaml = $('#settings-yaml').hidden;
      $('#settings-yaml').hidden = !yaml;
      $('#settings-form').hidden = yaml;
      $('#settings-yaml-toggle').textContent = yaml ? 'Edit with the form' : 'Edit as YAML';
    });
    $('#settings-yaml-check').addEventListener('click', () => save({ yaml: $('#settings-yaml-text').value, check: true }));
    $('#settings-yaml-save').addEventListener('click', () => save({ yaml: $('#settings-yaml-text').value }));
  });
})();
