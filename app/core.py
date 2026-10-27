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

PREF_TYPES = ('episodes', 'requests', 'issues', 'announcements', 'approvals', 'auto_approved', 'joins')
CATEGORIES = {'episodes', 'requests', 'issues', 'announcements', 'library', 'test'}


def default_prefs():
    """Each notification type's default (notifications.types.<type>.default), plus "New in <library>" for
    each shown Jellyfin library (lib_<id>, with a lib_<id>_mode of instant or digest; libraries.py).
    approvals / auto_approved only matter for approvers."""
    import libraries
    p = {}
    for k in PREF_TYPES:
        p[k] = bool(config.get(f'notifications.types.{k}.default', False))
    for lib in libraries.visible():
        p[libraries.key(lib['id'])] = lib['default']
        p[libraries.key(lib['id']) + '_mode'] = 'instant'
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
    if 'libraries' not in cols:     # the Jellyfin libraries they can see: JSON list of ids, or "*" (libraries.py)
        db().execute('alter table users add column libraries text')


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
    # url None: deliver() gives the push its own note page (/n/<id>) with the item's buttons
    return {'title': title[:120], 'body': (body or '')[:300], 'url': url, 'image': image, 'tag': tag}


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
    watch, image, ids, resolving = resolve_note(n)
    links = {'apps': app_links(ids)}
    if watch:
        links['watch'] = watch
    kind = ids.get('type')
    if (config.get('links.request') or '').startswith('https://') and ids.get('tmdb'):
        links['request'] = config.link('request_item', media='movie' if kind == 'movie' else 'tv', tmdb=ids['tmdb'])
    return {'title': n['title'], 'body': n['body'], 'image': image, 'created': n['created'],
            'category': n['category'], 'links': links, 'resolving': resolving}


def resolve_note(n):
    """(watch url, image, ids, still resolving) for a stored note, filling in what's now known."""
    ids = json.loads(n['ids'] or '{}')
    watch, image, resolving = n['watch_url'], n['image'], False
    if ids.get('type') in ('movie', 'show') and not (watch and ids.get('item')):
        # the item wasn't in Jellyfin yet when the push went out (import not scanned): look it up now.
        # ids.item is what plays (the film, the episode, the first new episode of a batch, or a new show).
        if not ids.get('item'):
            ids['item'] = find_item(ids)
        if not watch:
            page = find_item(series_ids(ids)) if ids.get('batch') else ids.get('item')
            watch = watch_url(page) if page else None
            image = image or poster_url(page)
        if watch or ids.get('item'):
            db().execute('update notes set watch_url=?, image=?, ids=? where id=?',
                         (watch, image, json.dumps({k: v for k, v in ids.items() if v is not None}), n['id']))
        resolving = not (watch and ids.get('item')) and now() - n['created'] < 6 * 3600   # the page tries again
    return watch, image, ids, resolving


def series_ids(ids):
    """The show itself, without the episode."""
    return {k: v for k, v in ids.items() if k not in ('season', 'episode', 'item', 'batch')}


def poster_url(item_id):
    base = jellyfin_base()
    return f'{base}/Items/{item_id}/Images/Primary?maxHeight=400&quality=85' if base and item_id else None


_catalogue = {}     # 'Movie' / 'Series' -> (fetched at, items with ProviderIds)


def _jellyfin_items(kind):
    at, items = _catalogue.get(kind, (0, None))
    if items is None or now() - at > 60:
        items = jf_request('GET', f'/Items?IncludeItemTypes={kind}&Recursive=true&Fields=ProviderIds'
                                  '&enableImages=false', token=config.secret('JELLYFIN_API_KEY'))['Items']
        _catalogue[kind] = (now(), items)
    return items


def find_item(ids):
    """Jellyfin item id for {'type': movie|show, tmdb, tvdb, season?, episode?}: the film, the episode
    (when season and episode are given and Jellyfin has it), else the series. None if not there (yet)."""
    try:
        if ids.get('type') == 'movie':
            want = [('Tmdb', ids.get('tmdb'))]
            items = _jellyfin_items('Movie')
        else:
            want = [('Tvdb', ids.get('tvdb')), ('Tmdb', ids.get('tmdb'))]
            items = _jellyfin_items('Series')
        match = None
        for key, val in want:
            if val:
                match = next((i for i in items if str((i.get('ProviderIds') or {}).get(key, '')) == str(val)), None)
                if match:
                    break
        if not match:
            _catalogue.pop('Movie' if ids.get('type') == 'movie' else 'Series', None)   # look again next time
            return None
        if ids.get('type') == 'show' and ids.get('season') is not None and ids.get('episode') is not None:
            eps = jf_request('GET', f"/Shows/{match['Id']}/Episodes?season={int(ids['season'])}&enableImages=false",
                             token=config.secret('JELLYFIN_API_KEY')).get('Items', [])
            ep = next((e for e in eps if e.get('IndexNumber') == int(ids['episode'])), None)
            if ep:
                return ep['Id']
        return match['Id']
    except Exception as e:
        log('Jellyfin lookup failed', json.dumps(ids), repr(e)[:150])
        return None


def backfill_notes(days=30):
    """Fill in the Jellyfin link and the playable item of stored notes (items not found when they were sent)."""
    fixed = 0
    for n in db().execute('select * from notes where ids is not null and created > ?', (now() - days * 86400,)).fetchall():
        before = (n['watch_url'], json.loads(n['ids']).get('item'))
        watch, _, ids, _ = resolve_note(n)
        if (watch, ids.get('item')) != before:
            fixed += 1
    return fixed


def app_links(ids):
    """Deep links into the recommended apps for a film / show (links.recommended_apps[].play):
    [{name, url, play_on}]. The note page shows them on the platforms in play_on."""
    kind, out = ids.get('type'), []
    if kind not in ('movie', 'show'):
        return out
    server_id = jellyfin_server_id() if ids.get('item') else ''
    for app in config.get('links.recommended_apps') or []:
        play = app.get('play') or {}
        tpl = None
        if ids.get('item') and server_id and play.get('item'):
            # the exact film / episode on this server (e.g. plezy://play?server=<id>&id=<item>)
            out.append({'name': app['name'], 'url': config.fill(play['item'], server_id=server_id, item_id=ids['item']),
                        'play_on': app.get('play_on') or []})
            continue
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
    item_id/url/image/tag, mute_key, ids, library (Jellyfin library id) or path; or category=library with
    library (or path / item_id) and title. Content in a library only goes to people who can see that library."""
    import libraries
    cat = msg.get('category')
    if cat not in CATEGORIES:
        raise ValueError(f'unknown category {cat!r}')
    if cat != 'library' and cat != 'test' and not config.type_enabled(cat):
        return {'skipped': f'{cat} is switched off'}
    if cat == 'library':
        return add_library(msg)
    payload = payload_for(msg['title'], msg.get('body'), msg.get('item_id'), msg.get('url'), msg.get('image'),
                          msg.get('tag'))
    ids = dict(msg['ids']) if msg.get('ids') else None
    if ids and msg.get('item_id') and not ids.get('item') and not ids.get('batch'):
        ids['item'] = msg['item_id']        # what Play opens: the same film / episode / show as Watch
    targets = msg.get('users')
    if targets is None and cat == 'announcements':
        targets = [r['jf_id'] for r in db().execute('select jf_id from users where signed_in=1')]
    lib = None
    if cat not in ('announcements', 'test'):
        lib = msg.get('library') or libraries.classify(msg.get('path'), msg.get('item_id') or (ids or {}).get('item'))
    result = {}
    for jf_id in targets or []:
        u = ensure_user(jf_id)
        if not u:
            result[jf_id] = {'skipped': 'unknown Jellyfin user'}
            continue
        if not libraries.can_see(u, lib):
            result[u['name']] = {'skipped': 'no access to that library'}
            continue
        if msg.get('mute_key') and msg['mute_key'] in json.loads(u['mutes'] or '{}'):
            result[u['name']] = {'skipped': 'muted'}
            continue
        result[u['name']] = deliver(u, cat, payload, ids=ids)
    log('notify', cat, repr(payload['title']), json.dumps(result))
    return result


# ---------- new in a library ----------

def add_library(msg):
    """Queue a "New in <library>" alert. msg: title, library (Jellyfin library id) or path / item_id to
    find it, item_id, ids; the old kind (movies / shows / anime) is still understood."""
    import libraries
    lib = msg.get('library') or (libraries.legacy(msg['kind']) if msg.get('kind') in libraries.LEGACY else None) \
        or libraries.classify(msg.get('path'), msg.get('item_id'))
    info = libraries.by_id(lib) if lib else None
    if not info:
        log('library: no Jellyfin library for', repr(msg.get('title')), '- not announced')
        return {'skipped': 'library not found'}
    if not info['show']:
        return {'skipped': f"{info['name']} is not a notification type"}
    p = payload_for(msg['title'], '', msg.get('item_id'), msg.get('url'), msg.get('image'))
    ids = dict(msg['ids']) if msg.get('ids') else None
    if ids and msg.get('item_id') and not ids.get('item'):
        ids['item'] = msg['item_id']
    # no Jellyfin item yet: url stays empty, so the push gets a note page that finds the item later
    db().execute('insert into library(kind, title, url, image, ts, ids) values(?,?,?,?,?,?)',
                 (lib, msg['title'], p['url'], p['image'], now(), json.dumps(ids) if ids else None))
    log('library: new in', info['name'], repr(msg['title']))
    return {'queued': info['name']}


def library_users(lib, mode):
    import libraries
    k = libraries.key(lib)
    for u in db().execute('select * from users').fetchall():
        p = prefs_of(u)
        if p.get(k) and p.get(k + '_mode') == mode and libraries.can_see(u, lib):
            yield u


def summary(info, titles):
    shown = ', '.join(titles[:4]) + (f' and {len(titles) - 4} more' if len(titles) > 4 else '')
    return config.msg('library_many', count=len(titles), library=info['label'], emoji=info['emoji'], titles=shown)


def flush_library():
    """Instant subscribers: items one by one, unless more than library_burst_limit arrive within library_burst_window,
    in which case they're held until the window has passed and sent as one summary."""
    import libraries
    t = now()
    b = config.get('notifications.batching')
    burst_limit, burst_window, wait = b['library_burst_limit'], b['library_burst_window'], b['library_wait']
    for lib in [r['kind'] for r in db().execute("select distinct kind from library where state='pending'")]:
        if lib in libraries.LEGACY:
            continue                    # queued before the move to libraries: libraries.migrate() handles it
        info = libraries.by_id(lib)
        pending = db().execute("select * from library where kind=? and state='pending' order by ts", (lib,)).fetchall()
        if not info or not info['show']:
            db().execute("update library set state='dropped' where kind=? and state='pending'", (lib,))
            continue
        if not pending or t - pending[0]['ts'] < wait:
            continue
        recent = db().execute("select count(*) from library where kind=? and state in ('sent','summarised') "
                              "and sent_ts > ?", (lib, t - burst_window)).fetchone()[0]
        users = list(library_users(lib, 'instant'))
        cat = libraries.key(lib)
        if recent + len(pending) <= burst_limit:
            for item in pending:
                title, body = config.msg('library_one', library=info['label'], emoji=info['emoji'], title=item['title'])
                payload = {'title': title, 'body': body, 'url': item['url'],
                           'image': item['image'], 'tag': f'library-{item["id"]}'}
                for u in users:
                    deliver(u, cat, payload, ids=json.loads(item['ids']) if item['ids'] else None)
                db().execute("update library set state='sent', sent_ts=? where id=?", (t, item['id']))
        elif t - pending[0]['ts'] >= burst_window:
            title, body = summary(info, [i['title'] for i in pending])
            payload = {'title': title, 'body': body, 'url': config.link('watch_home'), 'image': None,
                       'tag': f'library-{lib}-summary'}
            for u in users:
                deliver(u, cat, payload)
            db().executemany("update library set state='summarised', sent_ts=? where id=?",
                             [(t, i['id']) for i in pending])
            log('library: burst of', len(pending), 'in', info['name'], 'sent as one summary to', len(users), 'people')


def run_digest(force=False):
    """Daily at notifications.digest_hour (local time): one message per digest subscriber with everything
    new since the previous digest (at most 24 h), from the libraries they chose and can see."""
    import libraries
    local = dt.datetime.now(config.tz())
    today = local.strftime('%Y-%m-%d')
    if not force and (local.hour < config.get('notifications.digest_hour') or meta_get('digest_day') == today):
        return
    since = int(meta_get('digest_ts', now() - 86400))
    since = max(since, now() - 86400)
    items = db().execute("select * from library where ts > ? and state != 'dropped' order by ts", (since,)).fetchall()
    sent = 0
    for u in db().execute('select * from users').fetchall():
        p = prefs_of(u)
        parts, first = [], None
        for info in libraries.visible_for(u):
            k = libraries.key(info['id'])
            if p.get(k) and p.get(k + '_mode') == 'digest':
                titles = [i['title'] for i in items if i['kind'] == info['id']]
                if titles:
                    first = first or next(i for i in items if i['kind'] == info['id'])
                    parts.append(f"{info['emoji']} {info['label']} ({len(titles)}): " + ', '.join(titles[:3])
                                 + ('…' if len(titles) > 3 else ''))
        if parts:
            title, body = config.msg('digest', summary=' · '.join(parts))
            deliver(u, 'library', {'title': title, 'body': body,
                                   'url': config.link('watch_home'), 'image': first['image'],
                                   'tag': 'library-digest'}, force=True)
            sent += 1
    meta_set('digest_day', today)
    meta_set('digest_ts', now())
    log(f'digest: {len(items)} new item(s), sent to {sent} people')


def library_upkeep():
    """Move the old films/shows/anime choices over (once) and re-read everyone's library access."""
    import libraries
    libraries.migrate()
    libraries.all_libraries()
    libraries.refresh_access()


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
        for fn in ((library_upkeep,) if n % 20 == 0 else ()) + (flush_library, run_digest) + \
                ((cleanup,) if n % 60 == 0 else ()):
            try:
                fn()
            except Exception as e:
                log('ERROR', fn.__name__, repr(e)[:300])
        n += 1
        time.sleep(30)
