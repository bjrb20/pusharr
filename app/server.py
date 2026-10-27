"""Pusharr web server.

Listeners (ports in config.yaml, server.*):
  public_port (8080)    the app and its JSON API, meant to sit behind an HTTPS reverse proxy
  internal_port (8001)  POST /notify for trusted services on the same Docker network (bearer token);
                        never publish this one
  webhook_port (8000)   Sonarr / Radarr / Seerr webhooks (notifier.py, basic auth); never publish this one

Security: session cookie is __Host-, Secure, HttpOnly, SameSite=Lax, and expires; every state-changing
request needs the session's CSRF token in X-CSRF-Token and a matching Origin; sign-in is rate-limited
per client and per username; passwords and tokens are never logged or stored.
"""
import hmac
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import approvals
import branding
import config
import core
import joins
import libraries
import notifier

ENV = config.ENV
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'static')
MAX_BODY = 64 * 1024
LOGIN_LIMITS = {'client': (10, 900), 'username': (5, 900),     # failed attempts per 15 min
                'qc': (10, 900),                                # Quick Connect codes requested per 15 min
                'action': (20, 900)}                            # rejected notification-button taps
TEMPLATED = {'/index.html', '/sw.js', '/app.js', '/settings.js'}      # {{placeholders}} filled from the config
QC_TTL = 300                        # a Quick Connect code is valid for 5 minutes
_qc = {}                            # attempt id -> {'secret', 'device', 'created'}; secrets never leave the server
_qc_lock = threading.Lock()
_fails = defaultdict(deque)
_fails_lock = threading.Lock()

SECURITY_HEADERS = {
    'Content-Security-Policy': "default-src 'self'; img-src 'self' data: https:; script-src 'self'; "
                               "style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                               "form-action 'self'",
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'same-origin',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
}


def rate_limited(kind, key):
    limit, window = LOGIN_LIMITS[kind]
    with _fails_lock:
        q = _fails[(kind, key)]
        while q and q[0] < time.time() - window:
            q.popleft()
        return len(q) >= limit


def record_fail(kind, key):
    with _fails_lock:
        _fails[(kind, key)].append(time.time())


class Base(BaseHTTPRequestHandler):
    server_version = 'Pusharr'
    sys_version = ''

    def log_message(self, *a):
        pass

    def send(self, code, body=b'', ctype='application/json', headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def raw_body(self, limit=MAX_BODY):
        n = int(self.headers.get('Content-Length') or 0)
        if n > limit:
            raise ValueError('body too large')
        return self.rfile.read(n) if n else b''

    def body(self):
        raw = self.raw_body()
        return json.loads(raw) if raw else {}


class Internal(Base):
    def do_POST(self):
        want = 'Bearer ' + config.secret('INTERNAL_TOKEN')
        if not hmac.compare_digest(self.headers.get('Authorization', ''), want):
            return self.send(401, {'error': 'unauthorized'})
        if self.path not in ('/notify', '/request-event', '/issue-event'):
            return self.send(404, {'error': 'not found'})
        try:
            if self.path == '/request-event':     # from the notifier: Seerr request pending / auto-approved
                b = self.body()
                return self.send(200, approvals.notify_pending(int(b['request_id']), auto=b.get('kind') == 'auto'))
            if self.path == '/issue-event':       # from the notifier: problem report to approve / info / resolved
                return self.send(200, approvals.notify_issue(self.body()))
            return self.send(200, core.notify(self.body()))
        except ValueError as e:
            return self.send(400, {'error': str(e)})
        except Exception:
            core.log('ERROR internal notify', traceback.format_exc()[-400:])
            return self.send(500, {'error': 'internal error'})

    def do_GET(self):
        self.send(200, {'ok': True}) if self.path == '/healthz' else self.send(404, {'error': 'not found'})


class Public(Base):
    # ---- helpers ----
    def client(self):
        # behind HAProxy (option forwardfor) the real client is the last X-Forwarded-For entry
        xff = self.headers.get('X-Forwarded-For')
        return xff.split(',')[-1].strip() if xff else self.client_address[0]

    def token(self):
        c = SimpleCookie(self.headers.get('Cookie', ''))
        name = config.get('session.cookie_name')
        return c[name].value if name in c else None

    def same_origin(self):
        origin = self.headers.get('Origin')
        if not origin:
            return True             # not sent by some browsers on same-origin requests; CSRF token still required
        public = (config.get('public_url') or '').rstrip('/')
        allowed = {public} if public else set()
        allowed |= {o.strip().rstrip('/') for o in config.get('extra_origins') or [] if o.strip()}
        return origin.rstrip('/') in allowed

    def cookie(self, value, max_age):
        return f'{config.get("session.cookie_name")}={value}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age={max_age}'

    def authed(self, need_csrf=True):
        user, sess = core.session_user(self.token())
        if not user:
            self.send(401, {'error': 'signed out'})
            return None, None
        if need_csrf:
            if not self.same_origin() or not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), sess['csrf']):
                self.send(403, {'error': 'bad CSRF token'})
                return None, None
        return user, sess

    def me(self, user, sess):
        subs = core.db().execute('select count(*) from subs where jf_id=?', (user['jf_id'],)).fetchone()[0]
        return {'user': {'name': user['name'], 'is_admin': bool(user['is_admin']),
                         'is_approver': core.is_approver(user)},
                'prefs': core.prefs_of(user), 'mutes': json.loads(user['mutes'] or '{}'), 'devices': subs,
                'csrf': sess['csrf'], 'vapid_public_key': config.secret('VAPID_PUBLIC_KEY'),
                'types': [k for k in core.PREF_TYPES if config.type_enabled(k)
                          and (k != 'joins' or joins.eligible(user))],     # new accounts: admins (by default)
                # "New in <library>": the shown Jellyfin libraries this person can see
                'libraries': [{'key': libraries.key(l['id']), 'label': l['label'], 'emoji': l['emoji']}
                              for l in libraries.visible_for(user)],
                'installed_platforms': json.loads(user['installed'] or '[]')}

    # ---- GET ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/healthz':
            return self.send(200, {'ok': True})
        if path == '/api/links':
            return self.send(200, links(), headers={'Cache-Control': 'max-age=300'})
        if path.startswith('/api/note/'):
            user, _ = self.authed(need_csrf=False)
            if not user:
                return
            n = core.note_for(path.rsplit('/', 1)[1], user)
            return self.send(200, n, headers={'Cache-Control': 'no-store'}) if n else self.send(404, {'error': 'not found'})
        if path == '/api/me':
            user, sess = self.authed(need_csrf=False)
            return user and self.send(200, self.me(user, sess), headers={'Cache-Control': 'no-store'})
        if path == '/api/shows':
            user, _ = self.authed(need_csrf=False)
            return user and self.send(200, core.jellyfin_shows())
        if path == '/api/admin/requests':
            user, _ = self.authed(need_csrf=False)
            if not user:
                return
            if not core.is_approver(user):
                return self.send(403, {'error': 'approvers only'})
            return self.send(200, approvals.pending(), headers={'Cache-Control': 'no-store'})
        if path == '/api/admin/issues':
            user, _ = self.authed(need_csrf=False)
            if not user:
                return
            if not core.is_approver(user):
                return self.send(403, {'error': 'approvers only'})
            return self.send(200, approvals.pending_issues(), headers={'Cache-Control': 'no-store'})
        if path == '/api/admin/settings':
            user, _ = self.authed(need_csrf=False)
            if not user:
                return
            if not user['is_admin']:
                return self.send(403, {'error': 'admins only'})
            return self.send(200, settings_view(), headers={'Cache-Control': 'no-store'})
        if path == '/api/admin/users':
            user, _ = self.authed(need_csrf=False)
            if not user:
                return
            if not user['is_admin']:
                return self.send(403, {'error': 'admins only'})
            return self.send(200, admin_users(), headers={'Cache-Control': 'no-store'})
        return self.static(path)

    do_HEAD = do_GET

    def static(self, path):
        if path in ('', '/', '/settings') or re.fullmatch(r'/n/[A-Za-z0-9_-]{8,40}', path):   # /n/<id>: that note
            path = '/index.html'
        elif path == '/favicon.ico':
            path = branding.icon_url('favicon.ico')
        if path == '/theme.css':
            return self.send(200, branding.theme_css(), 'text/css', {'Cache-Control': 'no-cache'})
        if path == '/manifest.webmanifest':
            return self.send(200, json.dumps(branding.manifest(), ensure_ascii=False), 'application/manifest+json',
                             {'Cache-Control': 'no-cache'})
        if path.startswith('/brand/'):
            f = branding.brand_file(path[len('/brand/'):])
            if not f:
                return self.send(404, 'not found', 'text/plain')
            with open(f, 'rb') as fh:
                return self.send(200, fh.read(), branding.BRAND_TYPES[os.path.splitext(f)[1].lower()],
                                 {'Cache-Control': 'max-age=86400'})
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', path) or '..' in path:
            return self.send(404, 'not found', 'text/plain')
        f = os.path.normpath(os.path.join(STATIC, path.lstrip('/')))
        if not f.startswith(os.path.normpath(STATIC)) or not os.path.isfile(f):
            return self.send(404, 'not found', 'text/plain')
        ctype = {'.webmanifest': 'application/manifest+json', '.js': 'text/javascript', '.woff2': 'font/woff2',
                 '.ico': 'image/x-icon'}.get(
            os.path.splitext(f)[1]) or mimetypes.guess_type(f)[0] or 'application/octet-stream'
        headers = {'Cache-Control': 'no-cache'}
        if path == '/sw.js':
            headers['Service-Worker-Allowed'] = '/'
        with open(f, 'rb') as fh:
            body = fh.read()
        if path in TEMPLATED:
            body = branding.fill_page(body.decode(), js=path.endswith('.js')).encode()
        self.send(200, body, ctype, headers)

    # ---- POST ----
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == '/api/login':
                return self.login()
            if path == '/api/qc/start':
                return self.qc_start()
            if path == '/api/qc/poll':
                return self.qc_check()
            if path == '/api/action':
                return self.action()
            user, sess = self.authed()
            if not user:
                return
            if path == '/api/admin/upload':
                return self.upload(user)
            data = self.body()
            if path == '/api/logout':
                core.end_session(self.token())
                return self.send(200, {'ok': True}, headers={'Set-Cookie': self.cookie('', 0)})
            if path == '/api/prefs':
                p = core.prefs_of(user)
                p.update(core.clean_prefs(data.get('prefs')))
                defaults = core.default_prefs()
                stored = {k: v for k, v in p.items() if defaults.get(k) != v}
                core.db().execute('update users set prefs=? where jf_id=?', (json.dumps(stored), user['jf_id']))
                return self.send(200, {'prefs': p})
            if path == '/api/mutes':
                mutes = json.loads(user['mutes'] or '{}')
                if data.get('add'):
                    key, name = str(data['add'].get('key', ''))[:64], str(data['add'].get('name', ''))[:200]
                    if re.fullmatch(r'(tvdb|jf):[A-Za-z0-9]+', key):
                        mutes[key] = name
                if data.get('remove'):
                    mutes.pop(str(data['remove']), None)
                core.db().execute('update users set mutes=? where jf_id=?', (json.dumps(mutes), user['jf_id']))
                return self.send(200, {'mutes': mutes})
            if path == '/api/subscribe':
                ep, keys = data.get('endpoint', ''), data.get('keys') or {}
                if not (ep.startswith('https://') and keys.get('p256dh') and keys.get('auth')) or len(ep) > 1000:
                    return self.send(400, {'error': 'bad subscription'})
                core.db().execute(
                    'insert into subs(jf_id, endpoint, p256dh, auth, ua, created) values(?,?,?,?,?,?) '
                    'on conflict(endpoint) do update set jf_id=excluded.jf_id, p256dh=excluded.p256dh, '
                    'auth=excluded.auth, ua=excluded.ua, fails=0',
                    (user['jf_id'], ep, keys['p256dh'][:200], keys['auth'][:100],
                     str(data.get('ua') or self.headers.get('User-Agent', ''))[:200], core.now()))
                core.log('device subscribed for', user['name'])
                return self.send(200, self.me(user, sess))
            if path == '/api/unsubscribe':
                core.db().execute('delete from subs where jf_id=? and endpoint=?', (user['jf_id'], data.get('endpoint', '')))
                return self.send(200, self.me(user, sess))
            if path == '/api/installed':
                plat = data.get('platform')
                if plat not in ('ios', 'android', 'desktop'):
                    return self.send(400, {'error': 'bad platform'})
                have = set(json.loads(user['installed'] or '[]')) | {plat}
                core.db().execute('update users set installed=? where jf_id=?', (json.dumps(sorted(have)), user['jf_id']))
                core.log(f'{user["name"]} is using the installed app on {plat}')
                return self.send(200, self.me(core.get_user(user['jf_id']), sess))
            if path == '/api/test':
                title, body = config.msg('test', user=user['name'])
                res = core.deliver(user, 'test', core.payload_for(title, body, tag='test'), force=True)
                return self.send(200, res)
            if path == '/api/admin/settings':
                if not user['is_admin']:
                    return self.send(403, {'error': 'admins only'})
                return self.save_settings(user, data)
            if path == '/api/admin/requests/decide':
                if not core.is_approver(user):
                    return self.send(403, {'error': 'approvers only'})
                if data.get('action') not in ('approve', 'decline'):
                    return self.send(400, {'error': 'bad action'})
                outcome, d = approvals.decide(int(data.get('id')), data['action'], str(data.get('reason') or ''))
                return self.send(200, {'outcome': outcome, 'request': d})
            if path == '/api/admin/issues/decide':
                if not core.is_approver(user):
                    return self.send(403, {'error': 'approvers only'})
                if data.get('action') not in ('approve', 'ignore'):
                    return self.send(400, {'error': 'bad action'})
                outcome, title = approvals.issue_decide(int(data.get('id')), data['action'])
                return self.send(200, {'outcome': outcome, 'title': title})
            if path == '/api/admin/announce':
                if not user['is_admin']:
                    return self.send(403, {'error': 'admins only'})
                title = str(data.get('title') or '').strip()[:100]
                text = str(data.get('body') or '').strip()[:300]
                if not title:
                    return self.send(400, {'error': 'title needed'})
                users = data.get('users') or None
                title, text = config.msg('announcement', title=title, body=text)
                res = core.notify({'category': 'announcements', 'users': users, 'title': title,
                                   'body': text, 'tag': f'announce-{core.now()}'})
                return self.send(200, res)
            return self.send(404, {'error': 'not found'})
        except ValueError as e:
            return self.send(400, {'error': str(e)[:100]})
        except Exception:
            core.log('ERROR', path, traceback.format_exc()[-400:])
            return self.send(500, {'error': 'internal error'})

    def save_settings(self, user, data):
        """Branding & settings page: {'config': {...}} or {'yaml': '...'}; with 'check': true only validates."""
        import yaml
        if 'yaml' in data:
            try:
                conf = yaml.safe_load(str(data['yaml'])) or {}
            except yaml.YAMLError as e:
                return self.send(200, {'ok': False, 'problems': [f'YAML: {e}'.replace('\n', ' ')[:300]]})
        else:
            conf = data.get('config')

            drop = object()

            def unwrap(node, path=''):      # the form sends list / mapping fields as {'__yaml__': text}
                if isinstance(node, dict) and set(node) == {'__yaml__'}:
                    if not str(node['__yaml__']).strip():
                        return drop             # emptied: back to the default
                    try:
                        return yaml.safe_load(str(node['__yaml__']))
                    except yaml.YAMLError as e:
                        raise ValueError(f'{path}: not valid YAML ({str(e).splitlines()[0]})')
                if isinstance(node, dict):
                    out = {k: unwrap(v, f'{path}.{k}'.lstrip('.')) for k, v in node.items()}
                    return {k: v for k, v in out.items() if v is not drop}
                return node
            try:
                conf = unwrap(conf)
            except ValueError as e:
                return self.send(200, {'ok': False, 'problems': [str(e)]})
        if not isinstance(conf, dict):
            return self.send(400, {'error': 'settings must be a mapping'})
        if data.get('check'):
            problems = config.validate(config.merge(config.DEFAULTS, conf))
        else:
            problems = config.save(conf)
            if not problems:
                core.log('settings saved by', user['name'])
        return self.send(200, {'ok': not problems, 'problems': problems, 'version': config.version()})

    def upload(self, user):
        """Settings page uploads (raw body, file name in X-Filename): ?kind=icon&mode=mark|full&background=%23000000,
        ?kind=font or ?kind=wordmark. Returns the value to put in the settings."""
        if not user['is_admin']:
            return self.send(403, {'error': 'admins only'})
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        data = self.raw_body(branding.MAX_UPLOAD)
        name = str(self.headers.get('X-Filename') or '')[:200]
        try:
            if q.get('kind') == 'icon':
                value = branding.upload_icon(data, q.get('mode', 'mark'), q.get('background', '#000000'), q.get('letter'))
            elif q.get('kind') == 'font':
                value = branding.upload_font(name, data)
            elif q.get('kind') == 'wordmark':
                value = branding.upload_wordmark(name, data)
            else:
                return self.send(400, {'error': 'kind: icon, font or wordmark'})
        except ValueError as e:
            return self.send(400, {'error': f'Expected {e}'})
        except subprocess.SubprocessError:
            return self.send(400, {'error': "Couldn't read that SVG."})
        core.log(f'{q.get("kind")} uploaded by', user['name'], '->', value)
        return self.send(200, {'value': value})

    def signed_in(self, res, how):
        jf_id, name, is_admin = res
        user = core.ensure_user(jf_id, name, is_admin, signed_in=True)
        try:
            libraries.refresh_user(jf_id)       # which libraries they can see, fresh at every sign-in
            user = core.get_user(jf_id)
        except Exception as e:
            core.log('could not read library access for', name, repr(e)[:100])
        token = core.new_session(jf_id)
        core.log(f'signed in ({how}):', name)
        _, sess = core.session_user(token)
        return self.send(200, self.me(user, sess),
                         headers={'Set-Cookie': self.cookie(token, core.session_days() * 86400)})

    def qc_start(self):
        if not self.same_origin():
            return self.send(403, {'error': 'bad origin'})
        if not config.get('jellyfin.sign_in.quick_connect'):
            return self.send(403, {'error': 'Quick Connect sign-in is switched off.'})
        client = self.client()
        if rate_limited('qc', client):
            return self.send(429, {'error': 'Too many attempts. Try again in 15 minutes.'})
        record_fail('qc', client)
        try:
            secret, code, device = core.qc_initiate()
        except urllib.error.HTTPError as e:
            server = config.get('branding.server_name')
            return self.send(503, {'error': f'Quick Connect is switched off on {server}.' if e.code in (401, 403)
                                   else f'{server} did not answer. Try again.'})
        attempt = secrets.token_urlsafe(24)
        with _qc_lock:
            for k in [k for k, v in _qc.items() if time.time() - v['created'] > QC_TTL]:
                del _qc[k]
            _qc[attempt] = {'secret': secret, 'device': device, 'created': time.time()}
        return self.send(200, {'id': attempt, 'code': code, 'expires_in': QC_TTL})

    def qc_check(self):
        if not self.same_origin():
            return self.send(403, {'error': 'bad origin'})
        attempt = str(self.body().get('id') or '')
        with _qc_lock:
            a = _qc.get(attempt)
        if not a or time.time() - a['created'] > QC_TTL:
            with _qc_lock:
                _qc.pop(attempt, None)
            return self.send(410, {'error': 'That code has expired. Get a new one.'})
        res = core.qc_poll(a['secret'], a['device'])
        if res is None:
            return self.send(200, {'pending': True})
        with _qc_lock:
            _qc.pop(attempt, None)
        if res is False:
            return self.send(410, {'error': 'Quick Connect was not approved. Get a new code.'})
        return self.signed_in(res, 'Quick Connect')

    def action(self):
        """Approve / Decline tapped on a notification. Authorised by the button's token (signed,
        single-use, short-lived, bound to this device's push subscription), not by a cookie."""
        if not self.same_origin():
            return self.send(403, {'error': 'bad origin'})
        client = self.client()
        if rate_limited('action', client):
            return self.send(429, {'error': 'too many attempts'})
        data = self.body()
        if data.get('action') not in ('approve', 'decline', 'ignore'):
            return self.send(400, {'error': 'bad action'})
        ref, why = approvals.use_token(str(data.get('token') or ''), str(data.get('endpoint') or ''), data['action'])
        if not ref:
            record_fail('action', client)
            core.log('notification action refused:', why)
            return self.send(403, {'error': why})
        kind, ref_id = ref
        if kind == 'issue':
            outcome, title = approvals.issue_decide(ref_id, data['action'])
            return self.send(200, {'outcome': outcome, 'title': title, 'text': approvals.issue_text(outcome)})
        outcome, d = approvals.decide(ref_id, data['action'])
        return self.send(200, {'outcome': outcome, 'title': approvals.label(d) if d else None})

    def login(self):
        if not self.same_origin():
            return self.send(403, {'error': 'bad origin'})
        if not config.get('jellyfin.sign_in.password'):
            return self.send(403, {'error': 'Password sign-in is switched off. Use Quick Connect.'})
        data = self.body()
        username = str(data.get('username') or '').strip()[:100]
        password = str(data.get('password') or '')[:500]
        client = self.client()
        if rate_limited('client', client) or rate_limited('username', username.lower()):
            core.log('sign-in rate-limited for', repr(username))
            return self.send(429, {'error': 'Too many attempts. Try again in 15 minutes.'})
        res = core.jellyfin_login(username, password) if username else None
        if not res:
            record_fail('client', client)
            record_fail('username', username.lower())
            core.log('failed sign-in for', repr(username))
            return self.send(401, {'error': 'Wrong username or password.'})
        return self.signed_in(res, 'password')


def https(url):
    return url if isinstance(url, str) and url.startswith('https://') else None


def links():
    """The links section from the config (public: they're just the public addresses of the services).
    Unset or non-https links are left out."""
    l = config.get('links')
    out = {'request_name': l['request_name']}
    for k in ('watch', 'request', 'get_app'):
        if https(l[k]):
            out[k] = l[k]
    apps = []
    for a in l['recommended_apps']:
        stores = {k: v for k, v in (a.get('stores') or {}).items() if k in config.STORES and https(v)}
        apps.append({'name': a['name'], 'description': config.fill(a.get('description') or ''), 'stores': stores})
    out['apps'] = [a for a in apps if a['stores']]
    out['alternatives'] = [{'name': a['name'], 'url': a['url'], 'text': a.get('text') or 'Prefer something else? {link} work too.'}
                           for a in l['alternative_apps'] if https(a.get('url'))]
    return out


def settings_view():
    """What the Branding & settings page needs: the current settings, the file as written, choices."""
    try:
        text = open(config.CONFIG_FILE, encoding='utf-8').read()
    except OSError:
        text = ''
    return {'config': config.current(), 'defaults': config.DEFAULTS, 'yaml': text,
            'fonts': list(config.FONTS) + branding.custom_fonts(), 'icon_dirs': branding.icon_dirs(),
            'message_vars': {k: sorted(v | config.COMMON_VARS) for k, v in config.MESSAGE_VARS.items()},
            'restart_needed': sorted(k for k in ('server',) if config.get(k) != STARTED.get(k)),
            'jellyfin_libraries': [{k: l[k] for k in ('id', 'name', 'type', 'show', 'label', 'emoji', 'default', 'order')}
                                   for l in (libraries.options(x, i) for i, x in enumerate(libraries.all_libraries()))],
            'secrets': {k: bool(ENV.get(k)) for k in config.SECRETS}}


STARTED = {}      # settings that only apply at start-up, as they were then


def admin_users():
    out = []
    for u in core.db().execute('select * from users order by lower(name)'):
        subs = core.db().execute('select count(*) n, max(last_ok) ok from subs where jf_id=?', (u['jf_id'],)).fetchone()
        out.append({'id': u['jf_id'], 'name': u['name'], 'signed_in': bool(u['signed_in']),
                    'last_login': u['last_login'], 'devices': subs['n'], 'last_push_ok': subs['ok'],
                    'prefs': core.prefs_of(u), 'mutes': len(json.loads(u['mutes'] or '{}')),
                    'approver': core.is_approver(u)})
    return out


def main():
    problems = config.load()
    if not os.path.exists(config.CONFIG_FILE):
        problems.insert(0, f'{config.CONFIG_FILE} not found (copy config.example.yaml there)')
    problems += config.check_secrets()
    if notifier.webhooks_wanted() and not (ENV.get('WEBHOOK_USER') and ENV.get('WEBHOOK_PASSWORD')):
        problems.append('WEBHOOK_USER and WEBHOOK_PASSWORD are needed for the Sonarr / Radarr / Seerr webhooks')
    if problems:
        sys.exit('Pusharr cannot start:\n  ' + '\n  '.join(problems))
    STARTED['server'] = config.get('server')
    core.init_db()
    approvals.init_db()
    notifier.start()        # Sonarr / Radarr / Seerr webhooks (internal only), issue flow, contact sync
    threading.Thread(target=core.background, daemon=True).start()
    internal = ThreadingHTTPServer(('0.0.0.0', config.get('server.internal_port')), Internal)
    threading.Thread(target=internal.serve_forever, daemon=True).start()
    port = config.get('server.public_port')
    core.log(f'{config.get("branding.app_name")} (Pusharr) on :{port} (internal :{internal.server_port}), '
             f'public URL {config.get("public_url") or "-"}')
    ThreadingHTTPServer(('0.0.0.0', port), Public).serve_forever()


if __name__ == '__main__':
    main()
