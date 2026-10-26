"""Pusharr core: storage, preferences, sending (web push), library bursts and the daily digest.
No web handling here; see server.py. Settings come from config.py."""
import datetime as dt
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from pywebpush import WebPushException, webpush
from py_vapid import Vapid02
import urllib3.util.connection

# Docker bridge networks usually have no IPv6 route; without this, urllib3 can fail with "Network is
# unreachable" on push services that publish AAAA records. Set PUSH_IPV6=1 to allow IPv6.
if os.environ.get('PUSH_IPV6') != '1':
    urllib3.util.connection.HAS_IPV6 = False

import config

ENV = config.ENV
DATA_DIR = ENV.get('DATA_DIR', '/data')

LIBRARY_KINDS = {'movies': ('film', 'films'), 'shows': ('TV show', 'TV shows'), 'anime': ('anime', 'anime')}
PREF_TYPES = ('episodes', 'requests', 'issues', 'announcements', 'movies', 'shows', 'anime', 'approvals', 'auto_approved')
CATEGORIES = {'episodes', 'requests', 'issues', 'announcements', 'library', 'test'}


def default_prefs():
    """Each notification type's default (notifications.types.<type>.default); library kinds also get a
    mode (instant or digest). approvals / auto_approved only matter for approvers."""
    p = {}
    for k in PREF_TYPES:
        p[k] = bool(config.get(f'notifications.types.{k}.default', False))
        if k in LIBRARY_KINDS:
            p[f'{k}_mode'] = 'instant'
    return p


def session_days():
    return config.get('session.days')

_local = threading.local()
_lock = threading.RLock()


def log(*a):
    print(dt.datetime.now(config.tz()).strftime('%Y-%m-%d %H:%M:%S'), *a, flush=True)


def now():
    return int(time.time())


# ---------- storage ----------

def db():
    c = getattr(_local, 'db', None)
    if c is None:
        c = sqlite3.connect(os.path.join(DATA_DIR, 'alerts.db'), timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('pragma journal_mode=wal')
        c.execute('pragma foreign_keys=on')
        _local.db = c
    return c


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    db().executescript('''
    create table if not exists users (
        jf_id text primary key, name text not null, is_admin integer not null default 0,
        prefs text not null default '{}', mutes text not null default '{}',
        created integer not null, last_login integer, signed_in integer not null default 0);
    create table if not exists subs (
        id integer primary key, jf_id text not null references users(jf_id) on delete cascade,
        endpoint text not null unique, p256dh text not null, auth text not null, ua text,
        created integer not null, last_ok integer, last_error text, last_error_at integer,
        fails integer not null default 0);
    create table if not exists sessions (
        token_hash text primary key, jf_id text not null references users(jf_id) on delete cascade,
        csrf text not null, created integer not null, expires integer not null, checked integer not null);
    create table if not exists library (
        id integer primary key, kind text not null, title text not null, url text, image text,
        ts integer not null, state text not null default 'pending', sent_ts integer);
    create table if not exists meta (k text primary key, v text);
    create table if not exists sent_log (
        id integer primary key, ts integer not null, jf_id text, category text, title text,
        devices_ok integer, devices_failed integer);
    ''')
    db().executescript('''
    create table if not exists notifier_sent (key text primary key, ts integer);
    create table if not exists seen_series (id integer primary key);
    create table if not exists issue_records (id text primary key, data text not null);
    ''')
    db().execute('''create table if not exists notes (
        id text primary key, jf_id text not null, created integer not null, category text, title text,
        body text, image text, watch_url text, ids text)''')
    if 'ids' not in {r['name'] for r in db().execute('pragma table_info(library)')}:
        db().execute('alter table library add column ids text')
    cols = {r['name'] for r in db().execute('pragma table_info(users)')}
    if 'installed' not in cols:     # platforms (ios/android/desktop) where they've run the installed app
        db().execute("alter table users add column installed text not null default '[]'")


def meta_get(k, default=None):
    r = db().execute('select v from meta where k=?', (k,)).fetchone()
    return r['v'] if r else default


def meta_set(k, v):
    db().execute('insert into meta(k, v) values(?, ?) on conflict(k) do update set v=excluded.v', (k, str(v)))


# ---------- users / prefs ----------

def prefs_of(row):
    p = default_prefs()
    p.update(json.loads(row['prefs'] or '{}'))
    return p


def clean_prefs(incoming):
    out = {}
    defaults = default_prefs()
    for k, v in (incoming or {}).items():
        if k not in defaults:
            continue
        if k.endswith('_mode'):
            if v in ('instant', 'digest'):
                out[k] = v
        else:
            out[k] = bool(v)
    return out


def get_user(jf_id):
    return db().execute('select * from users where jf_id=?', (jf_id,)).fetchone()


def ensure_user(jf_id, name=None, is_admin=None, signed_in=False):
    """Create or update a user row. People can be sent alerts before they ever sign in (they're kept until then)."""
    with _lock:
        u = get_user(jf_id)
        if not u:
            if name is None:
                info = jellyfin_user(jf_id)
                if not info:
                    return None
                name, is_admin = info['Name'], info.get('Policy', {}).get('IsAdministrator', False)
            db().execute('insert into users(jf_id, name, is_admin, created, signed_in) values(?,?,?,?,?)',
                         (jf_id, name, int(bool(is_admin)), now(), int(signed_in)))
        else:
            if name is not None:
                db().execute('update users set name=? where jf_id=?', (name, jf_id))
            if is_admin is not None:
                db().execute('update users set is_admin=? where jf_id=?', (int(bool(is_admin)), jf_id))
            if signed_in:
                db().execute('update users set signed_in=1, last_login=? where jf_id=?', (now(), jf_id))
        return get_user(jf_id)


def is_approver(user):
    """Approvers get request / problem-report approvals: Jellyfin admins, or the listed usernames."""
    rule = config.approvers_rule()
    if rule == 'admins':
        return bool(user['is_admin'])
    return (user['name'] or '').lower() in {x.lower() for x in rule}


def approvers():
    return [u for u in db().execute('select * from users').fetchall() if is_approver(u)]


# ---------- Jellyfin ----------

def jf_request(method, path, body=None, token=None, timeout=20, device_id='pusharr'):
    app = config.get('branding.app_name').replace('"', '')
    auth = (f'MediaBrowser Client="{app}", Device="{app} server", '
            f'DeviceId="{device_id}", Version="1.0"')
    if token:
        auth += f', Token="{token}"'
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(config.get('jellyfin.url').rstrip('/') + path, data=data, method=method,
                                 headers={'Authorization': auth, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def jellyfin_login(username, password):
    """Check a Jellyfin username/password. Returns (id, name, is_admin) or None. The Jellyfin session
    this creates is logged out straight away; neither the password nor the token is kept."""
    try:
        res = jf_request('POST', '/Users/AuthenticateByName', {'Username': username, 'Pw': password})
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            return None
        raise
    return _finish_login(res)


def _finish_login(res, device_id='pusharr'):
    """Log the new Jellyfin session out straight away and return (id, name, is_admin), or None if disabled."""
    user = res.get('User') or {}
    try:
        jf_request('POST', '/Sessions/Logout', token=res.get('AccessToken'), device_id=device_id)
    except Exception:
        pass
    if not user.get('Id') or user.get('Policy', {}).get('IsDisabled'):
        return None
    return user['Id'], user['Name'], bool(user.get('Policy', {}).get('IsAdministrator'))


# Quick Connect: the secret stays on this server; the browser only ever sees the code to type in.

def qc_initiate():
    """Start a Quick Connect request. Returns (secret, code, device_id); a fresh device id per attempt
    so concurrent sign-ins can't log each other out."""
    device_id = 'pusharr-qc-' + secrets.token_hex(6)
    res = jf_request('POST', '/QuickConnect/Initiate', device_id=device_id)
    return res['Secret'], res['Code'], device_id


def qc_poll(secret, device_id):
    """None while waiting; (id, name, is_admin) once approved in Jellyfin; False if it's gone or refused."""
    try:
        st = jf_request('GET', '/QuickConnect/Connect?secret=' + urllib.parse.quote(secret), device_id=device_id)
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403, 404):
            return False
        raise
    if not st.get('Authenticated'):
        return None
    res = jf_request('POST', '/Users/AuthenticateWithQuickConnect', {'Secret': secret}, device_id=device_id)
    return _finish_login(res, device_id) or False


def jellyfin_user(jf_id):
    try:
        return jf_request('GET', f'/Users/{jf_id}', token=config.secret('JELLYFIN_API_KEY'))
    except Exception:
        return None


def jellyfin_shows():
    items = jf_request('GET', '/Items?IncludeItemTypes=Series&Recursive=true&Fields=ProviderIds&enableImages=false'
                              '&SortBy=SortName', token=config.secret('JELLYFIN_API_KEY'))['Items']
    out = []
    for i in items:
        tvdb = (i.get('ProviderIds') or {}).get('Tvdb')
        out.append({'key': f'tvdb:{tvdb}' if tvdb else f"jf:{i['Id']}", 'name': i['Name']})
    return out


# ---------- sessions ----------

def h(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(jf_id):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    t = now()
    db().execute('insert into sessions values(?,?,?,?,?,?)', (h(token), jf_id, csrf, t, t + session_days() * 86400, t))
    return token


def session_user(token):
    """(user row, session row) for a valid session, re-checking with Jellyfin hourly that the account
    still exists and isn't disabled (and refreshing the admin flag)."""
    if not token:
        return None, None
    s = db().execute('select * from sessions where token_hash=?', (h(token),)).fetchone()
    if not s or s['expires'] < now():
        if s:
            db().execute('delete from sessions where token_hash=?', (s['token_hash'],))
        return None, None
    if now() - s['checked'] > 3600:
        info = jellyfin_user(s['jf_id'])
        if not info or info.get('Policy', {}).get('IsDisabled'):
            db().execute('delete from sessions where jf_id=?', (s['jf_id'],))
            return None, None
        ensure_user(s['jf_id'], info['Name'], info.get('Policy', {}).get('IsAdministrator', False))
        db().execute('update sessions set checked=? where token_hash=?', (now(), s['token_hash']))
    return get_user(s['jf_id']), s


def end_session(token):
    db().execute('delete from sessions where token_hash=?', (h(token),))


# ---------- sending ----------

_vapid = None


def vapid():
    global _vapid
    if _vapid is None:
        _vapid = Vapid02.from_raw(config.secret('VAPID_PRIVATE_KEY').encode())
    return _vapid


_server_id = None


def jellyfin_server_id():
    """Jellyfin's server id (for exact web links), from its public info; cached once known."""
    global _server_id
    if _server_id is None:
        try:
            _server_id = jf_request('GET', '/System/Info/Public').get('Id') or ''
        except Exception:
            return ''          # try again next time
    return _server_id


def watch_url(item_id):
    """The Jellyfin web page for one film / episode / series (links.watch_item)."""
    if not item_id:
        return None
    url = config.link('watch_item', item_id=item_id, server_id=jellyfin_server_id())
    return url.replace('&serverId=&', '&').removesuffix('&serverId=') if url else None


def jellyfin_base():
    return (config.get('jellyfin.public_url') or '').rstrip('/')


def payload_for(title, body, item_id=None, url=None, image=None, tag=None):
    base = jellyfin_base()
    if item_id and base:
        url = url or watch_url(item_id)
        image = image or f'{base}/Items/{item_id}/Images/Primary?maxHeight=400&quality=85'
    return {'title': title[:120], 'body': (body or '')[:300], 'url': url or '/', 'image': image, 'tag': tag}


def push_one(sub, payload):
    payload = {k: v for k, v in payload.items() if v is not None}
    """True if delivered. Expired subscriptions (404/410) are deleted."""
    info = {'endpoint': sub['endpoint'], 'keys': {'p256dh': sub['p256dh'], 'auth': sub['auth']}}
    try:
        webpush(info, json.dumps(payload), vapid_private_key=vapid(),
                vapid_claims={'sub': config.get('vapid_subject') or config.get('public_url') or 'https://localhost'},
                ttl=86400, headers={'Urgency': 'high'}, timeout=20)
        db().execute('update subs set last_ok=?, fails=0, last_error=null where id=?', (now(), sub['id']))
        return True
    except WebPushException as e:
        code = e.response.status_code if e.response is not None else None
        if code in (404, 410):
            db().execute('delete from subs where id=?', (sub['id'],))
            log('removed expired subscription', sub['id'], 'of', sub['jf_id'])
        else:
            db().execute('update subs set fails=fails+1, last_error=?, last_error_at=? where id=?',
                         (f'{code} {str(e)[:150]}', now(), sub['id']))
            log('push failed', sub['id'], code)
        return False
    except Exception as e:
        db().execute('update subs set fails=fails+1, last_error=?, last_error_at=? where id=?',
                     (repr(e)[:150], now(), sub['id']))
        log('push failed', sub['id'], repr(e)[:100])
        return False


def make_note(user, category, payload, ids=None):
    """Remember what a notification was about. Tapping it opens /n/<id> inside the app (so an installed
    app opens, not a browser tab); outside links are only on that page's buttons."""
    nid = secrets.token_urlsafe(12)
    url = payload.get('url') or ''
    db().execute('insert into notes(id, jf_id, created, category, title, body, image, watch_url, ids) '
                 'values(?,?,?,?,?,?,?,?,?)',
                 (nid, user['jf_id'], now(), category, payload.get('title'), payload.get('body'), payload.get('image'),
                  url if url.startswith('https://') else None, json.dumps(ids) if ids else None))
    return f'/n/{nid}'


def note_for(nid, user):
    """A note, for its owner (or an admin), with the buttons it should show."""
    n = db().execute('select * from notes where id=?', (nid,)).fetchone()
    if not n or (n['jf_id'] != user['jf_id'] and not user['is_admin']):
        return None
    ids = json.loads(n['ids'] or '{}')
    links = {'apps': app_links(ids)}
    if n['watch_url']:
        links['watch'] = n['watch_url']
    kind = ids.get('type')
    if (config.get('links.request') or '').startswith('https://') and ids.get('tmdb'):
        links['request'] = config.link('request_item', media='movie' if kind == 'movie' else 'tv', tmdb=ids['tmdb'])
    return {'title': n['title'], 'body': n['body'], 'image': n['image'], 'created': n['created'],
            'category': n['category'], 'links': links}


def app_links(ids):
    """Deep links into the recommended apps for a film / show (links.recommended_apps[].play):
    [{name, url, play_on}]. The note page shows them on the platforms in play_on."""
    kind, out = ids.get('type'), []
    if kind not in ('movie', 'show'):
        return out
    for app in config.get('links.recommended_apps') or []:
        play = app.get('play') or {}
        tpl = None
        if kind == 'movie' and ids.get('tmdb'):
            tpl = play.get('movie')
        elif kind == 'show' and ids.get('tvdb') and play.get('show'):
            tpl = play['show']
        elif kind == 'show' and ids.get('tmdb'):
            tpl = play.get('show_tmdb')
        if tpl:
            out.append({'name': app['name'], 'url': config.fill(tpl, tmdb=ids.get('tmdb'), tvdb=ids.get('tvdb')),
                        'play_on': app.get('play_on') or []})
    return out


def deliver(user, category, payload, force=False, ids=None):
    """Send to all of a user's devices. force skips the category toggle."""
    p = prefs_of(user)
    if not force and category in p and (not p[category] or not config.type_enabled(category)):
        return {'skipped': f'{category} off'}
    if not (payload.get('url') or '').startswith('/'):
        payload = dict(payload, url=make_note(user, category, payload, ids))
    subs = db().execute('select * from subs where jf_id=?', (user['jf_id'],)).fetchall()
    ok = sum(push_one(s, payload) for s in subs)
    db().execute('insert into sent_log(ts, jf_id, category, title, devices_ok, devices_failed) values(?,?,?,?,?,?)',
                 (now(), user['jf_id'], category, payload['title'], ok, len(subs) - ok))
    return {'devices': len(subs), 'delivered': ok}


def notify(msg):
    """Internal entry point (the notifier, POST :8001/notify). msg: category, users (Jellyfin ids), title, body,
    item_id/url/image/tag, mute_key; or category=library with kind (movies/shows/anime)."""
    cat = msg.get('category')
    if cat not in CATEGORIES:
        raise ValueError(f'unknown category {cat!r}')
    if cat != 'library' and cat != 'test' and not config.type_enabled(cat):
        return {'skipped': f'{cat} is switched off'}
    if cat == 'library':
        return add_library(msg)
    payload = payload_for(msg['title'], msg.get('body'), msg.get('item_id'), msg.get('url'), msg.get('image'),
                          msg.get('tag'))
    targets = msg.get('users')
    if targets is None and cat == 'announcements':
        targets = [r['jf_id'] for r in db().execute('select jf_id from users where signed_in=1')]
    result = {}
    for jf_id in targets or []:
        u = ensure_user(jf_id)
        if not u:
            result[jf_id] = {'skipped': 'unknown Jellyfin user'}
            continue
        if msg.get('mute_key') and msg['mute_key'] in json.loads(u['mutes'] or '{}'):
            result[u['name']] = {'skipped': 'muted'}
            continue
        result[u['name']] = deliver(u, cat, payload, ids=msg.get('ids'))
    log('notify', cat, repr(payload['title']), json.dumps(result))
    return result


# ---------- new films / shows / anime ----------

def add_library(msg):
    kind = msg.get('kind')
    if kind not in LIBRARY_KINDS:
        raise ValueError(f'unknown library kind {kind!r}')
    if not config.type_enabled(kind):
        return {'skipped': f'{kind} is switched off'}
    p = payload_for(msg['title'], '', msg.get('item_id'), msg.get('url'), msg.get('image'))
    db().execute('insert into library(kind, title, url, image, ts, ids) values(?,?,?,?,?,?)',
                 (kind, msg['title'], p['url'], p['image'], now(), json.dumps(msg['ids']) if msg.get('ids') else None))
    log('library: new', kind, repr(msg['title']))
    return {'queued': kind}


def library_users(kind, mode):
    for u in db().execute('select * from users').fetchall():
        p = prefs_of(u)
        if p[kind] and p[f'{kind}_mode'] == mode:
            yield u


def summary(kind, titles):
    one, many = LIBRARY_KINDS[kind]
    shown = ', '.join(titles[:4]) + (f' and {len(titles) - 4} more' if len(titles) > 4 else '')
    return config.msg('library_many', count=len(titles), kinds=many, titles=shown)


def flush_library():
    """Instant subscribers: items one by one, unless more than library_burst_limit arrive within library_burst_window,
    in which case they're held until the window has passed and sent as one summary."""
    t = now()
    b = config.get('notifications.batching')
    burst_limit, burst_window, wait = b['library_burst_limit'], b['library_burst_window'], b['library_wait']
    for kind in LIBRARY_KINDS:
        pending = db().execute("select * from library where kind=? and state='pending' order by ts", (kind,)).fetchall()
        if not pending or t - pending[0]['ts'] < wait:
            continue
        recent = db().execute("select count(*) from library where kind=? and state in ('sent','summarised') "
                              "and sent_ts > ?", (kind, t - burst_window)).fetchone()[0]
        users = list(library_users(kind, 'instant'))
        one, _ = LIBRARY_KINDS[kind]
        if recent + len(pending) <= burst_limit:
            for item in pending:
                title, body = config.msg('library_one', kind=one, title=item['title'])
                payload = {'title': title, 'body': body, 'url': item['url'],
                           'image': item['image'], 'tag': f'library-{item["id"]}'}
                for u in users:
                    deliver(u, kind, payload, ids=json.loads(item['ids']) if item['ids'] else None)
                db().execute("update library set state='sent', sent_ts=? where id=?", (t, item['id']))
        elif t - pending[0]['ts'] >= burst_window:
            title, body = summary(kind, [i['title'] for i in pending])
            payload = {'title': title, 'body': body, 'url': config.link('watch_home') or '/', 'image': None,
                       'tag': f'library-{kind}-summary'}
            for u in users:
                deliver(u, kind, payload)
            db().executemany("update library set state='summarised', sent_ts=? where id=?",
                             [(t, i['id']) for i in pending])
            log('library: burst of', len(pending), kind, 'sent as one summary to', len(users), 'people')


def run_digest(force=False):
    """Daily at notifications.digest_hour (local time): one message per digest subscriber with everything
    new since the previous digest (at most 24 h)."""
    local = dt.datetime.now(config.tz())
    today = local.strftime('%Y-%m-%d')
    if not force and (local.hour < config.get('notifications.digest_hour') or meta_get('digest_day') == today):
        return
    since = int(meta_get('digest_ts', now() - 86400))
    since = max(since, now() - 86400)
    items = db().execute('select * from library where ts > ? order by ts', (since,)).fetchall()
    sent = 0
    for u in db().execute('select * from users').fetchall():
        p = prefs_of(u)
        parts, first = [], None
        for kind in LIBRARY_KINDS:
            if p[kind] and p[f'{kind}_mode'] == 'digest' and config.type_enabled(kind):
                titles = [i['title'] for i in items if i['kind'] == kind]
                if titles:
                    first = first or next(i for i in items if i['kind'] == kind)
                    one, many = LIBRARY_KINDS[kind]
                    parts.append(f"{len(titles)} {one if len(titles) == 1 else many}: " + ', '.join(titles[:3])
                                 + ('…' if len(titles) > 3 else ''))
        if parts:
            title, body = config.msg('digest', summary=' · '.join(parts))
            deliver(u, 'library', {'title': title, 'body': body,
                                   'url': config.link('watch_home') or '/', 'image': first['image'],
                                   'tag': 'library-digest'}, force=True)
            sent += 1
    meta_set('digest_day', today)
    meta_set('digest_ts', now())
    log(f'digest: {len(items)} new item(s), sent to {sent} people')


def cleanup():
    t = now()
    db().execute('delete from sessions where expires < ?', (t,))
    # subscriptions that have failed for 30 days without a single success
    db().execute('delete from subs where fails >= 10 and coalesce(last_ok, created) < ?', (t - 30 * 86400,))
    db().execute('delete from library where ts < ?', (t - 30 * 86400,))
    db().execute('delete from sent_log where ts < ?', (t - 90 * 86400,))
    db().execute('delete from action_tokens where expires < ?', (t - 86400,))
    db().execute('delete from notes where created < ?', (t - 30 * 86400,))


def background():
    n = 0
    while True:
        for fn in (flush_library, run_digest) + ((cleanup,) if n % 60 == 0 else ()):
            try:
                fn()
            except Exception as e:
                log('ERROR', fn.__name__, repr(e)[:300])
        n += 1
        time.sleep(30)
