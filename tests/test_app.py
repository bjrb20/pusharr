"""End-to-end test with a fake Jellyfin and a fake push service (which checks the VAPID signature and
decrypts each message like a browser would). No real accounts or push services are touched.

   docker run --rm ghcr.io/bjrb20/pusharr python tests/test_app.py
"""
import base64, json, os, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import http_ece
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b'=').decode()
unb64 = lambda s: base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))

# ---------- fakes ----------
USERS = {'alex': ('a' * 32, 'secret', True), 'sam': ('b' * 32, 'pw2', False)}
logouts, pushes, FORMATS = [], [], set()
QC = {}   # secret -> {'approved': bool, 'device': str}


class FakeJellyfin(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def reply(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b''
        self.send_response(code); self.send_header('Content-Type', 'application/json'); self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
        if self.path == '/Users/AuthenticateByName':
            u = USERS.get(data.get('Username', '').lower())
            if not u or u[1] != data.get('Pw'):
                return self.reply(401)
            return self.reply(200, {'AccessToken': 'tok-' + u[0], 'User': {
                'Id': u[0], 'Name': data['Username'].title(), 'Policy': {'IsAdministrator': u[2]}}})
        if self.path == '/QuickConnect/Initiate':
            dev = self.headers['Authorization'].split('DeviceId="')[1].split('"')[0]
            secret = f'qc-secret-{len(QC)}'
            QC[secret] = {'approved': False, 'device': dev}
            return self.reply(200, {'Secret': secret, 'Code': f'{100000 + len(QC)}', 'Authenticated': False})
        if self.path == '/Users/AuthenticateWithQuickConnect':
            q = QC.get(data.get('Secret'))
            if not q or not q['approved']:
                return self.reply(401)
            q['used'] = True
            return self.reply(200, {'AccessToken': 'tok-qc', 'User': {'Id': 'a' * 32, 'Name': 'Alex',
                                                                      'Policy': {'IsAdministrator': True}}})
        if self.path == '/Sessions/Logout':
            logouts.append(self.headers.get('Authorization'))
            return self.reply(204)
        self.reply(404)

    def do_GET(self):
        if self.path == '/System/Info/Public':
            return self.reply(200, {'Id': 'srv123', 'ServerName': 'Flix'})
        if self.path.startswith('/QuickConnect/Connect?secret='):
            q = QC.get(self.path.split('=', 1)[1])
            return self.reply(200, {'Authenticated': q['approved']}) if q and not q.get('used') else self.reply(404)
        if self.path == '/Library/VirtualFolders':
            return self.reply(200, LIBS)
        if self.path == '/Users':
            return self.reply(200, [jf_user(i, name, adm) for name, (i, _, adm) in USERS.items()])
        if self.path.startswith('/Users/'):
            uid = self.path.split('/')[2]
            for name, (i, _, adm) in USERS.items():
                if i == uid:
                    return self.reply(200, jf_user(i, name, adm))
            return self.reply(404)
        if self.path.startswith('/Items/') and self.path.endswith('/Ancestors'):
            lib = ITEM_LIB.get(self.path.split('/')[2])
            return self.reply(200, [{'Id': 'parent' + '0' * 26}] + ([{'Id': lib}] if lib else []))
        if self.path.startswith('/Items') and 'IncludeItemTypes=Movie' in self.path:
            return self.reply(200, {'Items': MOVIES})
        if self.path.startswith('/Items'):
            return self.reply(200, {'Items': SERIES})
        if self.path.startswith('/Shows/') and '/Episodes' in self.path:
            sid = self.path.split('/')[2]
            season = int(self.path.split('season=')[1].split('&')[0])
            return self.reply(200, {'Items': [e for e in EPISODES.get(sid, []) if e['ParentIndexNumber'] == season]})
        self.reply(404)


MOV, SHO, ANI, HOME, MUS = ('11aa' * 8, '22bb' * 8, '33cc' * 8, '44dd' * 8, '55ee' * 8)
LIBS = [{'Name': 'Movies', 'ItemId': MOV, 'CollectionType': 'movies', 'Locations': ['/data/video/movie']},
        {'Name': 'Shows', 'ItemId': SHO, 'CollectionType': 'tvshows', 'Locations': ['/data/video/TV show', '/data/video/Animation']},
        {'Name': 'Anime', 'ItemId': ANI, 'CollectionType': 'tvshows', 'Locations': ['/data/video/Anime']},
        {'Name': 'Home Videos', 'ItemId': HOME, 'CollectionType': 'homevideos', 'Locations': ['/data/video/home video']},
        {'Name': 'Music', 'ItemId': MUS, 'CollectionType': 'music', 'Locations': ['/data/music']}]
ACCESS = {'b' * 32: [MOV, SHO]}         # Sam can't see Anime (or home videos, music); Alex is an admin
ITEM_LIB = {'c' * 32: SHO, '4' * 32: SHO, 'e' * 32: SHO, '7' * 32: MOV, 'f' * 32: MOV, 'z' * 32: ANI}
K = lambda lib: 'lib_' + lib


def jf_user(i, name, adm):
    pol = {'IsAdministrator': adm, 'EnableAllFolders': adm, 'EnabledFolders': [] if adm else ACCESS.get(i, [])}
    return {'Id': i, 'Name': name.title(), 'Policy': pol}


SERIES = [{'Id': 'c' * 32, 'Name': 'Tracker', 'ProviderIds': {'Tvdb': '428433'}},
          {'Id': 'd' * 32, 'Name': 'Bleach', 'ProviderIds': {}}]
MOVIES = [{'Id': '7' * 32, 'Name': 'Dune: Part Two', 'ProviderIds': {'Tmdb': '693134'}}]
EPISODES = {'c' * 32: [{'Id': '4' * 32, 'ParentIndexNumber': 4, 'IndexNumber': 4}]}


SEERR = {   # request id -> request
    7: {'id': 7, 'type': 'movie', 'status': 1, 'media': {'tmdbId': 11970, 'jellyfinMediaId': None},
        'requestedBy': {'displayName': 'Sam', 'jellyfinUserId': 'b' * 32}},
    8: {'id': 8, 'type': 'tv', 'status': 1, 'media': {'tmdbId': 1399}, 'seasons': [{'seasonNumber': 1}, {'seasonNumber': 2}],
        'requestedBy': {'displayName': 'Sam', 'jellyfinUserId': 'b' * 32}},
    9: {'id': 9, 'type': 'movie', 'status': 1, 'media': {'tmdbId': 11970}, 'requestedBy': {'displayName': 'Sam', 'jellyfinUserId': 'b' * 32}},
}
seerr_calls = []


class FakeSeerr(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def reply(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b''
        self.send_response(code); self.send_header('Content-Type', 'application/json'); self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        assert self.headers['X-Api-Key'] == 'seerr-key'
        p = self.path.split('?')[0]
        if p == '/api/v1/request':
            return self.reply(200, {'results': [r for r in SEERR.values() if r['status'] == 1]})
        if p.startswith('/api/v1/request/'):
            r = SEERR.get(int(p.rsplit('/', 1)[1]))
            return self.reply(200, r) if r else self.reply(404)
        if p.startswith('/api/v1/movie/'):
            return self.reply(200, {'title': 'Hercules', 'releaseDate': '1997-06-27', 'posterPath': '/herc.jpg'})
        if p.startswith('/api/v1/tv/'):
            return self.reply(200, {'name': 'Game of Thrones', 'firstAirDate': '2011-04-17'})
        self.reply(404)

    def do_POST(self):
        parts = self.path.split('/')       # /api/v1/request/<id>/<approve|decline>
        r = SEERR.get(int(parts[4]))
        seerr_calls.append((int(parts[4]), parts[5]))
        if not r:
            return self.reply(404)
        if r['status'] != 1:
            return self.reply(409)
        r['status'] = 2 if parts[5] == 'approve' else 3
        self.reply(200, r)


CLIENT_KEY = ec.generate_private_key(ec.SECP256R1())
CLIENT_PUB = b64(CLIENT_KEY.public_key().public_bytes(serialization.Encoding.X962,
                                                      serialization.PublicFormat.UncompressedPoint))
AUTH = os.urandom(16)


class FakePush(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        if self.path == '/gone':
            self.send_response(410); self.end_headers(); return
        # VAPID: "vapid t=<jwt>, k=<public key>"; check the ES256 signature with the server's key
        auth = self.headers['Authorization']
        if auth.lower().startswith('vapid '):          # RFC 8292: "vapid t=<jwt>, k=<key>"
            parts = dict(p.strip().split('=', 1) for p in auth[6:].split(','))
        else:                                          # older draft: "WebPush <jwt>" + Crypto-Key: p256ecdsa=<key>
            ck = dict(p.strip().split('=', 1) for p in self.headers['Crypto-Key'].split(';') if '=' in p)
            parts = {'t': auth.split(' ', 1)[1], 'k': ck['p256ecdsa']}
        FORMATS.add(auth.split(' ', 1)[0])
        head, claims, sig = parts['t'].split('.')
        pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(parts['k']))
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        raw = unb64(sig)
        pub.verify(encode_dss_signature(int.from_bytes(raw[:32], 'big'), int.from_bytes(raw[32:], 'big')),
                   f'{head}.{claims}'.encode(), ec.ECDSA(hashes.SHA256()))
        assert parts['k'] == os.environ['VAPID_PUBLIC_KEY']
        msg = json.loads(http_ece.decrypt(body, private_key=CLIENT_KEY, auth_secret=AUTH, version='aes128gcm'))
        pushes.append({'msg': msg, 'urgency': self.headers.get('Urgency'), 'aud': json.loads(unb64(claims))['aud']})
        self.send_response(201); self.end_headers()


def serve(handler):
    s = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s.server_port


# ---------- set-up ----------
jf_port, push_port, seerr_port = serve(FakeJellyfin), serve(FakePush), serve(FakeSeerr)
tmp = tempfile.mkdtemp()
env_file = os.path.join(tmp, 'pusharr.env')
os.system(f'{sys.executable} tools/gen_vapid.py {env_file} >/dev/null')
open(os.path.join(tmp, 'config.yaml'), 'w').write(f"""
public_url: https://alerts.test
branding: {{app_name: Flix Alerts, short_name: Flix Alerts, server_name: Flix}}
jellyfin: {{url: 'http://127.0.0.1:{jf_port}', public_url: 'https://flix.test'}}
links:
  watch: https://flix.test
  recommended_apps:
    - name: Zapp
      stores: {{ios: 'https://apps.test/zapp'}}
      play: {{item: 'zapp://play?server={{server_id}}&id={{item_id}}', movie: 'zapp://play?type=movie&tmdb={{tmdb}}',
             show: 'zapp://play?type=show&tvdb={{tvdb}}&mode=next'}}
      play_on: [android, ios, mac]
integrations:
  seerr: {{enabled: true, url: 'http://127.0.0.1:{seerr_port}'}}
""")
os.environ.update(DATA_DIR=tmp, CONFIG_DIR=tmp, ENV_FILE=env_file, JELLYFIN_API_KEY='k', INTERNAL_TOKEN='internal-secret',
                  SEERR_API_KEY='seerr-key')
sys.path.insert(0, 'app')
import approvals, config, core, issues, notifier, server  # noqa: E402
problems = config.load()
assert problems == [], problems
core.init_db()
approvals.init_db()
pub_port = serve(server.Public)
int_port = serve(server.Internal)


def set_conf(path, value):
    """Change one setting in the running config (as a saved settings change would)."""
    node = config.current()
    *parents, last = path.split('.')
    for k in parents:
        node = node[k]
    node[last] = value

ok_count = 0


def check(cond, what):
    global ok_count
    if not cond:
        raise AssertionError(what)
    ok_count += 1
    print('ok  ', what)


def req(method, path, body=None, cookie=None, csrf=None, origin='https://alerts.test', port=None, auth=None):
    h = {'Content-Type': 'application/json'}
    if cookie: h['Cookie'] = cookie
    if csrf: h['X-CSRF-Token'] = csrf
    if origin: h['Origin'] = origin
    if auth: h['Authorization'] = auth
    r = urllib.request.Request(f'http://127.0.0.1:{port or pub_port}{path}', method=method, headers=h,
                               data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if resp.headers.get('Content-Type') == 'application/json' else raw), resp.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw[:1] == b'{' else raw), e.headers


# ---------- tests ----------
st, body, hd = req('GET', '/')
check(st == 200 and b'<title>Flix Alerts</title>' in body and b'Sign in with your <b>Flix</b> account' in body
      and b'{{' not in body and "frame-ancestors 'none'" in hd['Content-Security-Policy'], 'app page served with CSP, names filled in')
check(req('GET', '/manifest.webmanifest')[2]['Content-Type'] == 'application/manifest+json', 'manifest type')
check(req('GET', '/../app/core.py')[0] == 404 and req('GET', '/%2e%2e/app/core.py')[0] == 404, 'no path traversal')
check(req('GET', '/api/me')[0] == 401, 'signed out by default')

for _ in range(5):
    check(req('POST', '/api/login', {'username': 'mallory', 'password': 'x'})[0] == 401, 'wrong password refused')
st, body, _ = req('POST', '/api/login', {'username': 'mallory', 'password': 'x'})
check(st == 429, 'sign-in rate-limited after 5 failures for a username')
check(req('POST', '/api/login', {'username': 'alex', 'password': 'secret'}, origin='https://evil.test')[0] == 403,
      'sign-in from another origin refused')

st, me, hd = req('POST', '/api/login', {'username': 'alex', 'password': 'secret'})
cookie_hdr = hd['Set-Cookie']
check(st == 200 and me['user'] == {'name': 'Alex', 'is_admin': True, 'is_approver': True}, 'sign-in works')
check(all(f in cookie_hdr for f in ('__Host-pusharr=', 'Secure', 'HttpOnly', 'SameSite=Lax', 'Max-Age=2592000')),
      'session cookie is __Host-, Secure, HttpOnly, SameSite, expiring')
check(logouts == ['MediaBrowser Client="Flix Alerts", Device="Flix Alerts server", DeviceId="pusharr", '
                  'Version="1.0", Token="tok-' + 'a' * 32 + '"'], 'Jellyfin session logged out straight after sign-in')
cookie = cookie_hdr.split(';')[0]
dbtext = open(os.path.join(tmp, 'alerts.db'), 'rb').read()
check(b'secret' not in dbtext and b'tok-' not in dbtext and cookie.split('=', 1)[1].encode() not in dbtext,
      'password, Jellyfin token and raw session token are not stored')
csrf = me['csrf']
check(me['prefs']['episodes'] and not me['prefs'][K(MOV)] and 'ha' not in me['prefs'] and 'movies' not in me['types']
      and [l['label'] for l in me['libraries']] == ['Movies', 'Shows', 'Anime'] and me['libraries'][0]['emoji'] == '🎬',
      'default preferences, every type offered')

check(req('POST', '/api/prefs', {'prefs': {K(MOV): True}}, cookie)[0] == 403, 'POST without CSRF token refused')
check(req('POST', '/api/prefs', {'prefs': {K(MOV): True}}, cookie, csrf, origin='https://evil.test')[0] == 403,
      'POST from another origin refused')
st, r, _ = req('POST', '/api/prefs', {'prefs': {K(MOV): True, K(SHO): True, K(SHO) + '_mode': 'digest', 'bogus': 1,
                                                K(ANI) + '_mode': 'hourly', K(HOME): True}}, cookie, csrf)
check(st == 200 and r['prefs'][K(MOV)] and r['prefs'][K(SHO) + '_mode'] == 'digest' and 'bogus' not in r['prefs']
      and r['prefs'][K(ANI) + '_mode'] == 'instant' and K(HOME) not in r['prefs'], 'prefs saved and validated (hidden libraries refused)')
st, shows, _ = req('GET', '/api/shows', cookie=cookie)
check(shows[0] == {'key': 'tvdb:428433', 'name': 'Tracker'} and shows[1]['key'].startswith('jf:'), 'show list for mutes')
st, r, _ = req('POST', '/api/mutes', {'add': {'key': 'tvdb:428433', 'name': 'Tracker'}}, cookie, csrf)
check(r['mutes'] == {'tvdb:428433': 'Tracker'}, 'mute a show')
check(req('POST', '/api/mutes', {'add': {'key': 'x; drop', 'name': 'x'}}, cookie, csrf)[1]['mutes'] == r['mutes'],
      'bad mute keys ignored')

st, r, _ = req('POST', '/api/subscribe', {'endpoint': 'http://insecure.test/x', 'keys': {'p256dh': CLIENT_PUB, 'auth': b64(AUTH)}}, cookie, csrf)
check(st == 400, 'non-https push endpoint refused')
st, r, _ = req('POST', '/api/subscribe', {'endpoint': 'https://push.test/abc', 'keys': {'p256dh': CLIENT_PUB, 'auth': b64(AUTH)}}, cookie, csrf)
check(st == 200 and r['devices'] == 1, 'device subscribed')
# point that subscription at the local fake push service (the real API only accepts https endpoints)
core.db().execute('update subs set endpoint=? where endpoint=?', (f'http://127.0.0.1:{push_port}/abc', 'https://push.test/abc'))
core.db().execute('insert into subs(jf_id, endpoint, p256dh, auth, created) values(?,?,?,?,?)',
                  ('a' * 32, f'http://127.0.0.1:{push_port}/gone', CLIENT_PUB, b64(AUTH), core.now()))

st, r, _ = req('POST', '/api/test', {}, cookie, csrf)
check(r['devices'] == 2 and r['delivered'] == 1 and pushes[-1]['msg']['title'] == '🔔 Flix Alerts test'
      and pushes[-1]['urgency'] == 'high', 'test push: VAPID-signed, encrypted, decrypted by the fake browser')
check(core.db().execute('select count(*) from subs').fetchone()[0] == 1, 'expired (410) subscription removed')

# internal API
check(req('POST', '/notify', {'category': 'episodes'}, port=int_port)[0] == 401, 'internal API needs the token')
check(req('POST', '/notify', {'category': 'episodes'}, cookie, csrf)[0] == 404, 'internal API not on the public port')
A = 'Bearer internal-secret'
pushes.clear()
st, r, _ = req('POST', '/notify', {'category': 'episodes', 'users': ['a' * 32], 'title': '📺 Bleach S2E3 is ready',
                                   'body': "You're up to S2E2.", 'item_id': 'e' * 32, 'mute_key': 'tvdb:1',
                                   'ids': {'type': 'show', 'tvdb': 74796, 'tmdb': 30984}},
               port=int_port, auth=A)
m = pushes[0]['msg']
check(r['Alex']['delivered'] == 1 and m['url'].startswith('/n/')
      and m['image'].startswith('https://flix.test/Items/' + 'e' * 32), 'episode alert opens an in-app page, with poster')
note_url = m['url']
st, n, _ = req('GET', '/api/note/' + note_url[3:], cookie=cookie)
check(st == 200 and n['title'] == '📺 Bleach S2E3 is ready' and n['body'] == "You're up to S2E2."
      and n['links'] == {'watch': 'https://flix.test/web/#/details?id=' + 'e' * 32 + '&serverId=srv123',
                         'apps': [{'name': 'Zapp', 'url': 'zapp://play?server=srv123&id=' + 'e' * 32,
                                   'play_on': ['android', 'ios', 'mac']}]},
      'note page: exact Jellyfin item (with serverId) + the app\'s exact-item link (server id = Jellyfin\'s Id)')
st, body, _ = req('GET', note_url)
check(st == 200 and b'id="note"' in body, '/n/<id> serves the app (in scope, opens in the installed app)')
check(req('GET', '/n/../../app/core.py')[0] == 404, 'note paths are strict')
st, r, _ = req('POST', '/notify', {'category': 'episodes', 'users': ['a' * 32], 'title': 'x', 'mute_key': 'tvdb:428433'},
               port=int_port, auth=A)
check(r['Alex'] == {'skipped': 'muted'}, 'muted show skipped')
st, r, _ = req('POST', '/notify', {'category': 'requests', 'users': ['b' * 32], 'title': 'x'}, port=int_port, auth=A)
check(r['Sam']['devices'] == 0, 'unknown-but-real Jellyfin user created on the fly')

# library: 3 films one by one; then a burst of 8 shows -> one summary after the window
pushes.clear()
for t in ('Film A', 'Film B', 'Film C'):
    req('POST', '/notify', {'category': 'library', 'kind': 'movies', 'title': t, 'item_id': 'f' * 32}, port=int_port, auth=A)
core.db().execute('update library set ts=ts-120')
core.flush_library()
check([p['msg']['body'] for p in pushes if 'msg' in p] == ['Film A', 'Film B', 'Film C'], '3 new films sent one by one')
req('POST', '/api/prefs', {'prefs': {K(SHO) + '_mode': 'instant'}}, cookie, csrf)
pushes.clear()
for i in range(8):
    req('POST', '/notify', {'category': 'library', 'kind': 'shows', 'title': f'Show {i}'}, port=int_port, auth=A)
core.db().execute("update library set ts=ts-120 where kind=?", (SHO,))
core.flush_library()
check(not pushes, 'burst of 8 shows held back')
core.db().execute("update library set ts=ts-600 where kind=?", (SHO,))
core.flush_library()
msgs = [p['msg'] for p in pushes if 'msg' in p]
check(len(msgs) == 1 and msgs[0]['title'] == '📺 8 new in Shows' and 'and 4 more' in msgs[0]['body'],
      'burst sent as one summary')

# digest
req('POST', '/api/prefs', {'prefs': {K(MOV) + '_mode': 'digest'}}, cookie, csrf)
pushes.clear()
core.meta_set('digest_ts', core.now() - 3600)
core.run_digest(force=True)
msgs = [p['msg'] for p in pushes if 'msg' in p]
check(len(msgs) == 1 and msgs[0]['title'] == '🍿 Today on Flix' and msgs[0]['body'].startswith('🎬 Movies (3): Film A'),
      'daily digest')

# admin
st, users, _ = req('GET', '/api/admin/users', cookie=cookie)
check(st == 200 and {u['name'] for u in users} == {'Alex', 'Sam'} and users[0]['devices'] == 1, 'admin user list')
pushes.clear()
st, r, _ = req('POST', '/api/admin/announce', {'title': 'Maintenance', 'body': 'Down at 9pm'}, cookie, csrf)
check(r['Alex']['delivered'] == 1 and 'Sam' not in r and pushes[0]['msg']['title'] == '📣 Maintenance',
      'announcement to everyone signed in')
st, me2, hd2 = req('POST', '/api/login', {'username': 'sam', 'password': 'pw2'})
c2 = hd2['Set-Cookie'].split(';')[0]
check(req('GET', '/api/admin/users', cookie=c2)[0] == 403, 'non-admin gets no admin list')
check(req('POST', '/api/admin/announce', {'title': 'x'}, c2, me2['csrf'])[0] == 403, 'non-admin cannot announce')

# links (the settings page refuses non-https links; the API leaves them out anyway)
set_conf('links.request', 'http://insecure.test')
config.current()['links']['recommended_apps'][0]['stores']['android'] = 'http://insecure.test/zapp'
st, links, _ = req('GET', '/api/links')
check(st == 200 and links == {'request_name': 'Seerr', 'watch': 'https://flix.test', 'alternatives': [],
                              'apps': [{'name': 'Zapp', 'description': '', 'stores': {'ios': 'https://apps.test/zapp'}}]},
      'links from config; unset and non-https ones left out')

# note pages: owner only; Seerr button for requests
st, me_sam, hd_sam = req('POST', '/api/login', {'username': 'sam', 'password': 'pw2'})
c_sam = hd_sam['Set-Cookie'].split(';')[0]
check(req('GET', '/api/note/' + note_url[3:], cookie=c_sam)[0] == 404, "someone else's note is not shown")
check(req('GET', '/api/note/' + note_url[3:])[0] == 401, 'note needs sign-in')
set_conf('links.request', 'https://requests.test')
core.db().execute('insert into subs(jf_id, endpoint, p256dh, auth, created) values(?,?,?,?,?)',
                  ('b' * 32, f'http://127.0.0.1:{push_port}/sam-phone', CLIENT_PUB, b64(AUTH), core.now()))
pushes.clear()
req('POST', '/notify', {'category': 'requests', 'users': ['b' * 32], 'title': '🍿 Dune: Part Two (2024) is ready to watch on Flix',
                        'item_id': 'd' * 32, 'ids': {'type': 'movie', 'tmdb': 693134}}, port=int_port, auth=A)
nu = pushes[0]['msg']['url']
st, n, _ = req('GET', '/api/note/' + nu[3:], cookie=c_sam)
check(n['links'] == {'watch': 'https://flix.test/web/#/details?id=' + 'd' * 32 + '&serverId=srv123',
                     'apps': [{'name': 'Zapp', 'url': 'zapp://play?server=srv123&id=' + 'd' * 32, 'play_on': ['android', 'ios', 'mac']}],
                     'request': 'https://requests.test/movie/693134'},
      'request note: exact Jellyfin item, exact app link (server + item id), request page')
pushes.clear()
req('POST', '/notify', {'category': 'episodes', 'users': ['b' * 32], 'title': '📺 Unknown S1E1 is ready on Flix',
                        'ids': {'type': 'show', 'tvdb': 999999, 'tmdb': 61818}}, port=int_port, auth=A)
st, n, _ = req('GET', '/api/note/' + pushes[0]['msg']['url'][3:], cookie=c_sam)
check(n['links']['request'] == 'https://requests.test/tv/61818' and n['links']['apps'][0]['url'] == 'zapp://play?type=show&tvdb=999999&mode=next'
      and n['resolving'], 'item not in Jellyfin: the app link falls back to the TVDB id (next episode); request page')
check(req('GET', '/api/note/' + nu[3:], cookie=cookie)[0] == 200, 'admins can open any note')

# every kind of notification opens a note page with the item's buttons, even when the item wasn't in
# Jellyfin yet at send time (found by provider id when the page opens)
W = 'https://flix.test/web/#/details?id='


def push_note(msg):
    pushes.clear()
    st, r, _ = req('POST', '/notify', dict(msg, users=['b' * 32]), port=int_port, auth=A)
    url = pushes[0]['msg']['url'] if pushes else ''
    return url, (req('GET', '/api/note/' + url[3:], cookie=c_sam)[1] if url.startswith('/n/') else None)


url, n = push_note({'category': 'episodes', 'title': '📺 Tracker S4E4 is ready on Flix', 'body': "You're up to S4E3.",
                    'ids': {'type': 'show', 'tvdb': 428433, 'tmdb': 61818, 'season': 4, 'episode': 4}})
check(url.startswith('/n/') and n['links']['watch'] == W + '4' * 32 + '&serverId=srv123' and not n['resolving']
      and n['image'].startswith('https://flix.test/Items/' + '4' * 32) and n['links']['request']
      and n['links']['apps'][0]['url'] == 'zapp://play?server=srv123&id=' + '4' * 32,
      'new episode sent before Jellyfin had it: the page finds the episode itself (Watch, app, request)')
check(core.db().execute('select watch_url from notes where id=?', (url[3:],)).fetchone()[0].endswith('4' * 32 + '&serverId=srv123'),
      '... and remembers it')
url, n = push_note({'category': 'episodes', 'title': '📺 3 new episodes of Tracker on Flix', 'item_id': 'c' * 32,
                    'ids': {'type': 'show', 'tvdb': 428433, 'tmdb': 61818, 'season': 4, 'episode': 4, 'batch': True}})
check(n['links']['watch'] == W + 'c' * 32 + '&serverId=srv123' and n['links']['apps'][0]['url'] == 'zapp://play?server=srv123&id=' + '4' * 32
      and not n['resolving'], 'batch of episodes: Watch opens the series, Play the first new episode (exact item)')
url, n = push_note({'category': 'requests', 'title': '🍿 Dune: Part Two (2024) is ready to watch on Flix',
                    'ids': {'type': 'movie', 'tmdb': 693134}})
check(url.startswith('/n/') and n['links']['watch'] == W + '7' * 32 + '&serverId=srv123'
      and n['links']['request'] == 'https://requests.test/movie/693134'
      and n['links']['apps'][0]['url'] == 'zapp://play?server=srv123&id=' + '7' * 32,
      'request ready without a Jellyfin id from Seerr: found by TMDB id (Watch and the exact app link)')
url, n = push_note({'category': 'issues', 'title': '📺 Tracker S4E4 has been replaced with a new copy on Flix',
                    'item_id': '4' * 32, 'ids': {'type': 'show', 'tvdb': 428433, 'tmdb': 61818}})
check(n['links']['watch'] == W + '4' * 32 + '&serverId=srv123' and n['links']['request'], 'problem fixed: item buttons')
req('POST', '/api/prefs', {'prefs': {K(SHO): True, K(SHO) + '_mode': 'instant', K(MOV): True, K(MOV) + '_mode': 'instant'}},
    c_sam, me_sam['csrf'])
core.db().execute("update library set state='sent', sent_ts=0")
pushes.clear()
req('POST', '/notify', {'category': 'library', 'kind': 'shows', 'title': 'Rivals (2024)',
                        'ids': {'type': 'show', 'tvdb': 429920, 'tmdb': 208921}}, port=int_port, auth=A)
core.db().execute("update library set ts=ts-120 where state='pending'")
core.flush_library()
sam_push = next(p['msg'] for p in pushes if p.get('msg', {}).get('body') == 'Rivals (2024)')
st, n, _ = req('GET', '/api/note/' + sam_push['url'][3:], cookie=cookie)
check(sam_push['url'].startswith('/n/') and 'watch' not in n['links'] and n['resolving']
      and n['links']['request'] == 'https://requests.test/tv/208921' and n['links']['apps'],
      'new show not in Jellyfin yet: still a note page (app and request buttons), marked as still looking')
SERIES.append({'Id': 'e' * 32, 'Name': 'Rivals', 'ProviderIds': {'Tvdb': '429920', 'Tmdb': '208921'}})
st, n, _ = req('GET', '/api/note/' + sam_push['url'][3:], cookie=cookie)
check(n['links']['watch'] == W + 'e' * 32 + '&serverId=srv123' and not n['resolving'],
      'once Jellyfin has scanned it, the same page shows Watch')
pushes.clear()
req('POST', '/notify', {'category': 'library', 'kind': 'movies', 'title': 'Dune: Part Two (2024)', 'item_id': '7' * 32,
                        'ids': {'type': 'movie', 'tmdb': 693134}}, port=int_port, auth=A)
core.db().execute("update library set ts=ts-120 where state='pending'")
core.flush_library()
film = next(p['msg'] for p in pushes if p.get('msg', {}).get('body') == 'Dune: Part Two (2024)')
st, n, _ = req('GET', '/api/note/' + film['url'][3:], cookie=cookie)
check(n['links']['watch'] == W + '7' * 32 + '&serverId=srv123' and film['image'], 'new film: note page with Watch and poster')
core.db().execute("insert into notes(id, jf_id, created, category, title, ids) values('oldnote123', ?, ?, 'library', 'x', ?)",
                  ('b' * 32, core.now() - 3600, json.dumps({'type': 'show', 'tvdb': 428433})))
check(core.backfill_notes() >= 1 and core.db().execute("select watch_url from notes where id='oldnote123'").fetchone()[0]
      == W + 'c' * 32 + '&serverId=srv123', 'stored notes without a link are filled in')
url, n = push_note({'category': 'announcements', 'title': '📣 Maintenance', 'body': 'Down at 9pm'})
check(url.startswith('/n/') and n['links'] == {'apps': []} and not n['resolving'], 'announcement: a plain note page')
req('POST', '/api/prefs', {'prefs': {K(SHO): False, K(MOV): False}}, c_sam, me_sam['csrf'])

# libraries: classification by folder / by item, and only to people who can see that library
import libraries  # noqa: E402
check(libraries.for_path('/data/video/Anime/Frieren/Season 01/x.mkv') == ANI and libraries.for_path('/data/video/Animation/Bluey') == SHO
      and libraries.for_path('/data/video/Anime') == ANI and libraries.for_path('/data/video/Animex/y') is None,
      'content belongs to the library whose folder holds it (no name guessing)')
check(libraries.for_item('c' * 32) == SHO and libraries.for_item('nope' * 8) is None, '... or where Jellyfin put the item')
check([l['label'] for l in req('GET', '/api/me', cookie=c_sam)[1]['libraries']] == ['Movies', 'Shows'],
      "Sam is only offered the libraries Sam can see")
pushes.clear()
st, r, _ = req('POST', '/notify', {'category': 'episodes', 'users': ['a' * 32, 'b' * 32], 'title': '📺 Frieren S2E1 is ready',
                                   'path': '/data/video/Anime/Frieren/Season 02/e1.mkv'}, port=int_port, auth=A)
check(r['Sam'] == {'skipped': 'no access to that library'} and r['Alex']['delivered'] == 1, 'anime episode: not sent to Sam (no access to Anime)')
st, r, _ = req('POST', '/notify', {'category': 'requests', 'users': ['b' * 32], 'title': 'x', 'item_id': 'z' * 32}, port=int_port, auth=A)
check(r['Sam'] == {'skipped': 'no access to that library'}, 'request ready for an Anime item: not sent to Sam either')
st, r, _ = req('POST', '/notify', {'category': 'issues', 'users': ['b' * 32], 'title': 'x', 'item_id': 'c' * 32}, port=int_port, auth=A)
check(r['Sam']['delivered'] == 1, 'problem fixed in Shows: sent to Sam')
st, r, _ = req('POST', '/notify', {'category': 'announcements', 'users': ['b' * 32], 'title': 'x', 'path': '/data/video/Anime/x'},
               port=int_port, auth=A)
check(r['Sam']['delivered'] == 1, 'announcements are not about a library')
ACCESS['b' * 32] = [MOV, SHO, ANI]
check(libraries.refresh_access() == 2 and [l['label'] for l in req('GET', '/api/me', cookie=c_sam)[1]['libraries']]
      == ['Movies', 'Shows', 'Anime'], 'access re-read from Jellyfin (every 10 min and at sign-in)')
ACCESS['b' * 32] = [MOV, SHO]
libraries.refresh_access()
check(req('POST', '/notify', {'category': 'library', 'library': ANI, 'title': 'Frieren', 'item_id': 'z' * 32}, port=int_port,
          auth=A)[1] == {'queued': 'Anime'}, 'new in Anime: queued')
core.db().execute("update library set state='sent' where kind=?", (ANI,))
check(req('POST', '/notify', {'category': 'library', 'library': HOME, 'title': 'Birthday'}, port=int_port, auth=A)[1]
      == {'skipped': 'Home Videos is not a notification type'}, 'home videos are hidden unless shown in the config')
set_conf('libraries', {'Home Videos': {'show': True, 'label': 'Family', 'emoji': '🏠', 'default': True, 'order': -1},
                       'Anime': {'show': False}})
me_a = req('GET', '/api/me', cookie=cookie)[1]
check([(l['label'], l['emoji']) for l in me_a['libraries']] == [('Family', '🏠'), ('Movies', '🎬'), ('Shows', '📺')]
      and me_a['prefs'][K(HOME)] is True and K(ANI) not in me_a['prefs'], 'per-library options: show/hide, label, emoji, default, order')
set_conf('libraries', {})

# moving the old movies / shows / anime choices to the libraries (once)
core.db().execute("update users set prefs=? where jf_id=?", (json.dumps({'movies': True, 'anime': True, 'anime_mode': 'digest',
                                                                          'episodes': False}), 'b' * 32))
core.db().execute("insert into library(kind, title, ts, state) values('shows', 'Queued show', ?, 'pending')", (core.now(),))
core.db().execute("delete from meta where k='libraries_migrated'")
core.flush_library()
check(core.db().execute("select count(*) from library where kind='shows' and state='pending'").fetchone()[0] == 1,
      'queued alerts of the old kinds are kept until they are moved')
mapping = libraries.migrate()
p_sam = json.loads(core.get_user('b' * 32)['prefs'])
check(mapping == {'movies': MOV, 'shows': SHO, 'anime': ANI} and p_sam == {K(MOV): True, K(ANI): True, K(ANI) + '_mode': 'digest',
                                                                          'episodes': False},
      'films / TV shows / anime choices moved to Movies / Shows / Anime (instant or digest kept)')
check(core.db().execute("select count(*) from library where kind=? and title='Queued show'", (SHO,)).fetchone()[0] == 1,
      'the queued alert moved too (sent once, not lost or doubled)')
check(libraries.migrate() is None and json.loads(core.get_user('b' * 32)['prefs']) == p_sam, 'and it only happens once')
core.db().execute("delete from library where title='Queued show'")
core.db().execute("update users set prefs='{}' where jf_id=?", ('b' * 32,))
set_conf('links.request', '')
del config.current()['links']['recommended_apps'][0]['stores']['android']

# installed app
m = json.loads(req('GET', '/manifest.webmanifest')[1])
check(m['id'] == '/' and m['start_url'] == '/' and m['scope'] == '/', 'manifest id, start_url and scope are /')
for icon in m['icons']:
    st, body, hd = req('GET', icon['src'])
    check(st == 200 and hd['Content-Type'] == 'image/png' and body[:8] == b'\x89PNG\r\n\x1a\n', f"manifest icon {icon['src']} served")
check({i['purpose'] for i in m['icons']} == {'any', 'maskable'} and {i['sizes'] for i in m['icons']} == {'192x192', '512x512'},
      'manifest has any + maskable icons at 192 and 512')
for path, ctype in (('/favicon.ico', 'image/x-icon'), ('/icons/apple-touch-180.png', 'image/png'),
                    ('/icons/badge-96.png', 'image/png'), ('/fonts/inter-latin-wght-normal.woff2', 'font/woff2'),
                    ('/fonts/bebas-neue-latin-400-normal.woff2', 'font/woff2'), ('/theme.css', 'text/css')):
    st, body, hd = req('GET', path)
    check(st == 200 and hd['Content-Type'] == ctype, f'{path} served as {ctype}')
sw = req('GET', '/sw.js')[1]
check(b"const APP = 'Flix Alerts'" in sw and b"'/icons/icon-192.png'" in sw and b"'/icons/badge-96.png'" in sw
      and config.version().encode() in sw and b'{{' not in sw, 'service worker: app name, icons and config version filled in')
check(m['name'] == 'Flix Alerts' and m['display'] == 'standalone' and m['related_applications'] == [
    {'platform': 'webapp', 'url': 'https://alerts.test/manifest.webmanifest'}], 'manifest: name, standalone + related app from public_url')
check(me['installed_platforms'] == [], 'nothing installed yet')
check(req('POST', '/api/installed', {'platform': 'ios'}, cookie)[0] == 403, 'installed flag needs CSRF')
check(req('POST', '/api/installed', {'platform': 'toaster'}, cookie, csrf)[0] == 400, 'bad platform refused')
st, r, _ = req('POST', '/api/installed', {'platform': 'ios'}, cookie, csrf)
check(st == 200 and r['installed_platforms'] == ['ios'], 'installed app on iOS remembered')
check(req('GET', '/api/me', cookie=cookie)[1]['installed_platforms'] == ['ios'], 'shown to the browser tab via /api/me')
st, body, _ = req('GET', '/')
check(b'id="install"' in body and b'/detect.js' in body, 'install screen and detection script in the page')

# Quick Connect
st, r, _ = req('POST', '/api/qc/start', {})
check(st == 200 and r['code'] == '100001' and 'qc-secret' not in json.dumps(r), 'Quick Connect code shown; secret stays on the server')
check(list(QC.values())[0]['device'].startswith('pusharr-qc-'), 'Quick Connect uses its own device id per attempt')
check(req('POST', '/api/qc/poll', {'id': r['id']})[1] == {'pending': True}, 'pending until approved in Jellyfin')
check(req('POST', '/api/qc/poll', {'id': 'made-up'})[0] == 410, 'unknown attempt refused')
check(req('POST', '/api/qc/poll', {'id': r['id']}, origin='https://evil.test')[0] == 403, 'Quick Connect poll from another origin refused')
QC['qc-secret-0']['approved'] = True
n_logouts = len(logouts)
st, me3, hd3 = req('POST', '/api/qc/poll', {'id': r['id']})
check(st == 200 and me3['user']['name'] == 'Alex' and '__Host-pusharr=' in hd3['Set-Cookie'],
      'approved Quick Connect signs in with our own session')
check(len(logouts) == n_logouts + 1 and logouts[-1].endswith('Token="tok-qc"'), 'Quick Connect Jellyfin session logged out straight away')
check(req('POST', '/api/qc/poll', {'id': r['id']})[0] == 410, 'a Quick Connect attempt can only be used once')
r2 = req('POST', '/api/qc/start', {})[1]
server._qc[r2['id']]['created'] -= 301
check(req('POST', '/api/qc/poll', {'id': r2['id']})[0] == 410, 'Quick Connect code expires after 5 minutes')
for _ in range(8):
    req('POST', '/api/qc/start', {})
check(req('POST', '/api/qc/start', {})[0] == 429, 'Quick Connect codes rate-limited per client')
check(b'qc-secret' not in open(os.path.join(tmp, 'alerts.db'), 'rb').read(), 'Quick Connect secret not stored')

# request approvals
alex_sub = core.db().execute('select * from subs where jf_id=?', ('a' * 32,)).fetchone()
alex_ep = alex_sub['endpoint']
core.db().execute('insert into subs(jf_id, endpoint, p256dh, auth, created) values(?,?,?,?,?)',
                  ('b' * 32, f'http://127.0.0.1:{push_port}/sam', CLIENT_PUB, b64(AUTH), core.now()))
pushes.clear()
st, r, _ = req('POST', '/request-event', {'request_id': 7, 'kind': 'pending'}, port=int_port, auth=A)
m = pushes[0]['msg']
check(st == 200 and r == {'Alex': {'devices': 1, 'delivered': 1}} and len(pushes) == 1,
      'pending request pushed to the admin only')
check(m['title'] == '🎬 New request from Sam: Hercules (1997, film)' and m['image'] == 'https://image.tmdb.org/t/p/w500/herc.jpg'
      and [a['action'] for a in m['actions']] == ['approve', 'decline'] and m['url'] == '/#pending' and m['token'],
      'request push: title, poster, Approve/Decline buttons, opens Pending')
token = m['token']
check(req('POST', '/api/action', {'token': token, 'action': 'approve', 'endpoint': 'https://someone-else'})[1]['error']
      == 'not for this device', 'action token bound to its subscription')
body_, sig_ = token.split('.')
check(req('POST', '/api/action', {'token': body_ + '.' + sig_[::-1], 'action': 'approve', 'endpoint': alex_ep})[1]['error']
      == 'bad signature', 'tampered action token refused')
check(req('POST', '/api/action', {'token': token, 'action': 'approve', 'endpoint': alex_ep}, origin='https://evil.test')[0] == 403,
      'action from another origin refused')
pushes.clear()
st, r, _ = req('POST', '/api/action', {'token': token, 'action': 'approve', 'endpoint': alex_ep})
check(st == 200 and r == {'outcome': 'approved', 'title': 'Hercules (1997, film)'} and SEERR[7]['status'] == 2
      and seerr_calls[-1] == (7, 'approve'), 'Approve button approves in Seerr')
check(any(p.get('msg', {}).get('title') == 'Approved ✓: Hercules (1997, film)' and p['msg']['tag'] == 'req-7' for p in pushes),
      'notification replaced with "Approved ✓"')
check(req('POST', '/api/action', {'token': token, 'action': 'decline', 'endpoint': alex_ep})[1]['error'] == 'already used'
      and SEERR[7]['status'] == 2, 'replayed action token refused')
pushes.clear()
req('POST', '/request-event', {'request_id': 9, 'kind': 'pending'}, port=int_port, auth=A)
t9 = pushes[0]['msg']['token']
SEERR[9]['status'] = 2           # approved in Seerr meanwhile
check(req('POST', '/api/action', {'token': t9, 'action': 'decline', 'endpoint': alex_ep})[1]['outcome'] == 'already-approved'
      and SEERR[9]['status'] == 2, 'already handled in Seerr is reported, not changed')
pushes.clear()
req('POST', '/request-event', {'request_id': 8, 'kind': 'pending'}, port=int_port, auth=A)
t8 = pushes[0]['msg']['token']
check(pushes[0]['msg']['title'] == '🎬 New request from Sam: Game of Thrones (2011, series, seasons 1, 2)', 'series request title with seasons')
core.db().execute('update action_tokens set expires=?', (core.now() - 1,))
check(req('POST', '/api/action', {'token': t8, 'action': 'approve', 'endpoint': alex_ep})[0] == 403
      and SEERR[8]['status'] == 1, 'expired action token refused')
st, plist, _ = req('GET', '/api/admin/requests', cookie=cookie)
check(st == 200 and [x['id'] for x in plist] == [8], 'Pending page lists what still waits')
check(req('GET', '/api/admin/requests', cookie=c2)[0] == 403, 'non-admin gets no Pending page')
check(req('POST', '/api/admin/requests/decide', {'id': 8, 'action': 'decline'}, c2, me2['csrf'])[0] == 403, 'non-admin cannot decide')
check(req('POST', '/api/admin/requests/decide', {'id': 8, 'action': 'decline'}, cookie)[0] == 403, 'deciding needs CSRF')
pushes.clear()
st, r, _ = req('POST', '/api/admin/requests/decide', {'id': 8, 'action': 'decline', 'reason': 'Already on Netflix'}, cookie, csrf)
sam_msgs = [p['msg'] for p in pushes if 'msg' in p and p['msg']['title'].startswith('Request declined')]
check(r['outcome'] == 'declined' and SEERR[8]['status'] == 3 and sam_msgs and sam_msgs[0]['body'] == 'Already on Netflix',
      'decline from the page, reason sent to the requester')
pushes.clear()
st, r, _ = req('POST', '/request-event', {'request_id': 9, 'kind': 'auto'}, port=int_port, auth=A)
check(r == {'Alex': {'skipped': 'auto_approved off'}} and not pushes, 'auto-approved info is off by default')
req('POST', '/api/prefs', {'prefs': {'auto_approved': True}}, cookie, csrf)
req('POST', '/request-event', {'request_id': 9, 'kind': 'auto'}, port=int_port, auth=A)
check(pushes and 'actions' not in pushes[0]['msg'] and pushes[0]['msg']['title'].startswith('✅ Auto-approved'),
      'auto-approved info (when on) has no buttons')

# problem reports (issue replacement approvals; the issue flow runs in this app)
notifier.setup()
recs = issues.load()
recs['12'] = {'issue_id': 12, 'kind': 'VIDEO', 'state': 'awaiting', 'reporter': 'Sam', 'comment': 'Picture freezes at 10 min',
              'created': issues.iso(), 'target': {'title': 'Tracker (2024) S4E1', 'jf_item': 'f' * 32}, 'via': 'push', 'log': []}
issues.save(recs)
pushes.clear()
ev = {'issue_id': 12, 'event': 'approval', 'kind': 'VIDEO', 'title': 'Tracker (2024) S4E1', 'jf_item': 'f' * 32,
      'push_title': '⚠️ Video problem on Flix: Tracker (2024) S4E1',
      'message': 'Sam: “Picture freezes at 10 min”\nApprove to get a different copy.'}
st, r, _ = req('POST', '/issue-event', ev, port=int_port, auth=A)
m = pushes[0]['msg']
check(st == 200 and r == {'Alex': {'devices': 1, 'delivered': 1}} and len(pushes) == 1, 'problem report pushed to the admin only')
check(m['title'] == '⚠️ Video problem on Flix: Tracker (2024) S4E1' and 'Picture freezes' in m['body']
      and [a['action'] for a in m['actions']] == ['approve', 'ignore'] and m['url'] == '/#pending'
      and m['image'].startswith('https://flix.test/Items/' + 'f' * 32), 'problem report push: title, comment, poster, Approve/Ignore')
ti = m['token']
check(req('POST', '/api/action', {'token': ti, 'action': 'decline', 'endpoint': alex_ep})[1]['error'] == 'bad action',
      'Decline is not an action for problem reports')
st, plist, _ = req('GET', '/api/admin/issues', cookie=cookie)
check(st == 200 and [(i['issue_id'], i['reporter'], i['comment']) for i in plist] == [(12, 'Sam', 'Picture freezes at 10 min')]
      and plist[0]['poster'].startswith('https://flix.test/Items/'), 'Pending page lists problem reports')
check(req('GET', '/api/admin/issues', cookie=c2)[0] == 403, 'non-admin gets no problem reports')
pushes.clear()
st, r, _ = req('POST', '/api/action', {'token': ti, 'action': 'ignore', 'endpoint': alex_ep})
check(st == 200 and r['outcome'] == 'ignored' and r['text'] == 'Ignored' and issues.load()['12']['state'] == 'ignored',
      'Ignore button (token was still valid after the refused Decline) answers the issue')
check(any(p.get('msg', {}).get('title') == 'Ignored: Tracker (2024) S4E1' and p['msg']['tag'] == 'issue-12' for p in pushes),
      'problem report notification replaced with the result')
check(req('POST', '/api/action', {'token': ti, 'action': 'approve', 'endpoint': alex_ep})[1]['error'] == 'already used',
      'replayed problem-report token refused')
recs = issues.load()
recs['13'] = dict(recs['12'], issue_id=13, state='awaiting', dry=True, log=[])     # dry run: approving grabs nothing
issues.save(recs)
check(req('POST', '/api/admin/issues/decide', {'id': 13, 'action': 'approve'}, cookie)[0] == 403, 'deciding a report needs CSRF')
st, r, _ = req('POST', '/api/admin/issues/decide', {'id': 13, 'action': 'approve'}, cookie, csrf)
check(r['outcome'] == 'approved', 'approve a problem report from the page')
st, r, _ = req('POST', '/api/admin/issues/decide', {'id': 13, 'action': 'ignore'}, cookie, csrf)
check(r['outcome'].startswith('already-'), 'already-answered report is reported, not changed')
pushes.clear()
st, r, _ = req('POST', '/issue-event', dict(ev, issue_id=14, dry=True), port=int_port, auth=A)
check(r == {'dry_run': True} and not pushes, 'dry-run problem reports are not pushed')
st, r, _ = req('POST', '/issue-event', {'issue_id': 15, 'event': 'info', 'push_title': '💬 Subtitle problem on Flix: X',
                                        'message': 'Bazarr is fetching others.'}, port=int_port, auth=A)
check(pushes and 'actions' not in pushes[0]['msg'] and pushes[0]['msg']['title'].startswith('💬 Subtitle problem on Flix'),
      'info notices have no buttons')

# Branding & settings page
SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="50" cy="50" r="40" fill="#fff"/></svg>')


def upload(kind, data, name, ck=None, cs=None, **q):
    h = {'Content-Type': 'application/octet-stream', 'X-Filename': name, 'Origin': 'https://alerts.test'}
    if ck: h['Cookie'] = ck
    if cs: h['X-CSRF-Token'] = cs
    qs = urllib.parse.urlencode({'kind': kind, **q})
    r = urllib.request.Request(f'http://127.0.0.1:{pub_port}/api/admin/upload?{qs}', method='POST', headers=h, data=data)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}')


check(req('GET', '/api/admin/settings', cookie=c2)[0] == 403, 'non-admin gets no settings')
check(req('POST', '/api/admin/settings', {'config': {}}, c2, me2['csrf'])[0] == 403, 'non-admin cannot save settings')
check(req('POST', '/api/admin/settings', {'config': {}}, cookie)[0] == 403, 'saving settings needs CSRF')
st, view, _ = req('GET', '/api/admin/settings', cookie=cookie)
check(st == 200 and view['config']['branding']['server_name'] == 'Flix' and 'server_name: Flix' in view['yaml']
      and view['secrets']['JELLYFIN_API_KEY'] and not view['secrets']['SONARR_API_KEY']
      and all(isinstance(v, bool) for v in view['secrets'].values()), 'settings view: config, file, which secrets are set (not their values)')
conf = view['config']
bad = json.loads(json.dumps(conf))
bad['branding']['theme']['accent'] = 'red'
bad['messages']['episode_one']['title'] = '{series} {colour}'
bad['links']['recommended_apps'] = {'__yaml__': '- name: [unclosed'}
st, r, _ = req('POST', '/api/admin/settings', {'config': bad}, cookie, csrf)
check(r['ok'] is False and any('links.recommended_apps' in p for p in r['problems']), 'bad YAML in a form field is reported')
bad['links']['recommended_apps'] = {'__yaml__': '[]'}
st, r, _ = req('POST', '/api/admin/settings', {'config': bad}, cookie, csrf)
check(r['ok'] is False and any(p.startswith('branding.theme.accent') for p in r['problems'])
      and any('{colour}' in p for p in r['problems']), 'bad colour and unknown template variable refused')
check(config.get('branding.theme.accent') == '#3b82f6', 'nothing applied when there are problems')
good = json.loads(json.dumps(conf))
good['branding']['server_name'] = 'Moviebox'
good['branding']['theme']['accent'] = '#ff8800'
good['messages']['episode_one']['title'] = '🍿 {series} {episode} has landed on {server}'
good['approvals']['approvers'] = {'__yaml__': '- sam'}
st, r, _ = req('POST', '/api/admin/settings', {'config': good}, cookie, csrf)
saved = open(os.path.join(tmp, 'config.yaml')).read()
check(r['ok'] and config.get('branding.server_name') == 'Moviebox' and 'server_name: Moviebox' in saved
      and os.path.exists(os.path.join(tmp, 'config.yaml.bak')) and 'messages:' in saved and 'notifications:' not in saved,
      'settings saved (only what differs from the defaults), previous file kept as .bak')
check('--accent: #ff8800;' in req('GET', '/theme.css')[1].decode() and b'Sign in with your <b>Moviebox</b>' in req('GET', '/')[1],
      'new colour and name used straight away')
check(notifier.message('Tracker', {'new': [(4, 4)], 'furthest': (4, 3), 'behind': 1})[0]
      == '🍿 Tracker S4E4 has landed on Moviebox', 'edited message template used')
check(req('GET', '/api/me', cookie=cookie)[1]['user']['is_approver'] is False
      and req('GET', '/api/admin/requests', cookie=cookie)[0] == 403 and req('GET', '/api/admin/requests', cookie=c2)[0] == 200,
      'approvers as a list: Sam approves, Alex (admin) no longer does')
st, r, _ = req('POST', '/api/admin/settings', {'yaml': 'branding: {server_name: Flix}\nbogus: 1\n'}, cookie, csrf)
check(r['ok'] is False and r['problems'] == ['bogus: unknown setting'], 'YAML editor: unknown settings refused')
st, r, _ = req('POST', '/api/admin/settings', {'yaml': view['yaml'], 'check': True}, cookie, csrf)
check(r['ok'] and config.get('branding.server_name') == 'Moviebox', '"Check" validates without saving')
st, r, _ = req('POST', '/api/admin/settings', {'yaml': view['yaml']}, cookie, csrf)
check(r['ok'] and config.get('branding.server_name') == 'Flix' and config.get('approvals.approvers') == 'admins',
      'YAML editor saves the whole file')

check(upload('icon', SVG, 'logo.svg', cookie)[0] == 403, 'upload needs CSRF')
check(upload('icon', SVG, 'logo.svg', c2, me2['csrf'])[0] == 403, 'non-admin cannot upload')
check(upload('icon', b'<html>', 'x.svg', cookie, csrf)[0] == 400, 'non-SVG icon refused')
st, r = upload('icon', SVG, 'logo.svg', cookie, csrf, mode='mark', background='#112233')
icon_dir = r.get('value', '')
made = sorted(os.listdir(os.path.join(tmp, 'branding', icon_dir))) if icon_dir else []
check(st == 200 and icon_dir.startswith('icons-') and made == sorted(
    ['apple-touch-180.png', 'badge-96.png', 'favicon-32.png', 'favicon.ico', 'icon-192.png', 'icon-512.png',
     'maskable-192.png', 'maskable-512.png', 'source.svg']), 'one SVG makes the whole icon set')
from PIL import Image  # noqa: E402
badge = Image.open(os.path.join(tmp, 'branding', icon_dir, 'badge-96.png'))
apple = Image.open(os.path.join(tmp, 'branding', icon_dir, 'apple-touch-180.png'))
check(badge.size == (96, 96) and badge.mode == 'RGBA' and {p[:3] for p in badge.getdata() if p[3]} == {(255, 255, 255)}
      and apple.mode == 'RGB' and apple.getpixel((2, 2)) == (0x11, 0x22, 0x33), 'badge is white-only, Apple icon is opaque on the background')
st, r = upload('icon', SVG, 'logo.svg', cookie, csrf, mode='full', background='#000000', letter='F')
check(st == 200 and r['value'] != icon_dir, 'whole-icon mode (letter badge) makes its own folder')
set_conf('branding.icon_dir', icon_dir)
m = json.loads(req('GET', '/manifest.webmanifest')[1])
check(all(i['src'].startswith(f'/brand/{icon_dir}/') for i in m['icons'])
      and req('GET', m['icons'][0]['src'])[2]['Content-Type'] == 'image/png'
      and f'/brand/{icon_dir}/badge-96.png'.encode() in req('GET', '/sw.js')[1], 'manifest, page and worker use the uploaded icons')
check(req('GET', f'/brand/{icon_dir}/source.svg')[0] == 404 and req('GET', '/brand/../config.yaml')[0] == 404,
      'only images and fonts are served from the branding folder')
set_conf('branding.icon_dir', '')
check(upload('font', b'not a font', 'x.woff2', cookie, csrf)[0] == 400, 'non-font refused')
st, r = upload('font', open('static/fonts/bebas-neue-latin-400-normal.woff2', 'rb').read(), 'My Font!.woff2', cookie, csrf)
check(st == 200 and r['value'] == 'custom:My-Font.woff2', 'font uploaded with a safe name')
set_conf('branding.fonts.wordmark', r['value'])
check('src: url("/brand/fonts/My-Font.woff2") format("woff2")' in req('GET', '/theme.css')[1].decode()
      and req('GET', '/brand/fonts/My-Font.woff2')[2]['Content-Type'] == 'font/woff2', 'custom wordmark font served and used')
set_conf('branding.fonts.wordmark', 'inter')
st, r = upload('wordmark', SVG, 'mark.svg', cookie, csrf)
check(st == 200 and r['value'].startswith('wordmark-') and r['value'].endswith('.png'), 'SVG wordmark rasterised to PNG')
set_conf('branding.wordmark.style', 'image')
set_conf('branding.wordmark.image', r['value'])
check(f'<img src="/brand/{r["value"]}" alt="Flix Alerts">'.encode() in req('GET', '/')[1], 'image wordmark in the header')
set_conf('branding.wordmark.style', 'text')

# switches in the config
set_conf('notifications.types.announcements.enabled', False)
check('announcements' not in req('GET', '/api/me', cookie=cookie)[1]['types'], 'a switched-off type is not offered')
check(req('POST', '/notify', {'category': 'announcements', 'title': 'X'}, port=int_port, auth=A)[1]
      == {'skipped': 'announcements is switched off'}, 'and nothing is sent for it')
set_conf('notifications.types.announcements.enabled', True)
set_conf('jellyfin.sign_in.password', False)
check(req('POST', '/api/login', {'username': 'alex', 'password': 'secret'})[0] == 403 and b'data-password=""' in req('GET', '/')[1],
      'password sign-in can be switched off')
set_conf('jellyfin.sign_in.password', True)
set_conf('jellyfin.sign_in.quick_connect', False)
check(req('POST', '/api/qc/start', {})[0] == 403, 'Quick Connect can be switched off')
set_conf('jellyfin.sign_in.quick_connect', True)

# sessions
st, _, hd = req('POST', '/api/logout', {}, cookie, csrf)
check('Max-Age=0' in hd['Set-Cookie'] and req('GET', '/api/me', cookie=cookie)[0] == 401, 'sign-out ends the session')
core.db().execute('update sessions set expires=?', (core.now() - 1,))
check(req('GET', '/api/me', cookie=c2)[0] == 401, 'expired session refused')

check(FORMATS == {'vapid'}, 'RFC 8292 VAPID header (vapid t=..., k=...), as Apple requires')
print(f'\nall {ok_count} checks passed')
