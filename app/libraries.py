"""Jellyfin libraries as notification types ("New in <library>"), and which libraries each person can see.

- Libraries come from Jellyfin (/Library/VirtualFolders: name, item id, collection type, folders), cached
  for 10 minutes. Each shown library is a notification type; its preference keys are lib_<id> (on/off)
  and lib_<id>_mode (instant / digest).
- config.yaml `libraries:` sets per-library options, keyed by library name (or id): show, label, emoji,
  default (on for new people), order, who (admins, approvers, everyone or a list of usernames: only they
  get the type at all). Home videos, music, collections and the like are hidden unless shown there.
- New content belongs to the library whose folder holds its file (Sonarr's and Radarr's paths are
  Jellyfin's), else to the library its Jellyfin item sits in.
- Access: Jellyfin admins and "all libraries" users see everything; others their enabled libraries.
  Refreshed every 10 minutes and at sign-in; every alert about content in a library is only sent to
  people who can see that library.
"""
import json
import threading

import config
import core

HIDDEN_TYPES = {'homevideos', 'music', 'boxsets', 'photos', 'books', 'playlists', 'livetv', 'folders', 'musicvideos'}
EMOJI = {'movies': '🎬', 'tvshows': '📺', 'music': '🎵'}
LEGACY = ('movies', 'shows', 'anime')      # the old fixed kinds, migrated to libraries
REFRESH = 600

_lock = threading.Lock()
_cache = {'at': 0, 'libs': None}
_item_lib = {}


def fetch():
    out = []
    for f in core.jf_request('GET', '/Library/VirtualFolders', token=config.secret('JELLYFIN_API_KEY')) or []:
        paths = []
        for p in f.get('Locations') or []:
            p = p.rstrip('/')
            if p and p not in paths:
                paths.append(p)
        out.append({'id': f['ItemId'], 'name': f['Name'], 'type': f.get('CollectionType') or 'mixed', 'paths': paths})
    return out


def all_libraries(refresh=False):
    """Every Jellyfin library (shown or not). Keeps the last good list if Jellyfin doesn't answer."""
    with _lock:
        if refresh or _cache['libs'] is None or core.now() - _cache['at'] > REFRESH:
            try:
                _cache['libs'] = fetch()
                _cache['at'] = core.now()
            except Exception as e:
                core.log('could not read the Jellyfin libraries:', repr(e)[:150])
                _cache['at'] = core.now() - REFRESH + 60       # try again in a minute
        return list(_cache['libs'] or [])


def options(lib, index=0):
    conf = config.get('libraries') or {}
    o = conf.get(lib['name']) or conf.get(lib['id']) or {}
    return {'id': lib['id'], 'name': lib['name'], 'type': lib['type'], 'paths': lib['paths'],
            'show': o.get('show', lib['type'] not in HIDDEN_TYPES),
            'label': o.get('label') or lib['name'],
            'emoji': o.get('emoji') or EMOJI.get(lib['type'], '🎞️'),
            'default': bool(o.get('default', False)),
            'order': o.get('order', index),
            'who': o.get('who')}


def visible():
    """The libraries that are notification types, in display order."""
    libs = [options(lib, i) for i, lib in enumerate(all_libraries())]
    return sorted((x for x in libs if x['show']), key=lambda x: (x['order'], x['name'].lower()))


def by_id(lib_id):
    return next((options(lib, i) for i, lib in enumerate(all_libraries()) if lib['id'] == lib_id), None)


def key(lib_id):
    return f'lib_{lib_id}'


def for_path(path):
    """The library whose folder holds this path (longest match), or None."""
    if not path:
        return None
    path = path.rstrip('/')
    best = None
    for lib in all_libraries():
        for p in lib['paths']:
            if (path == p or path.startswith(p + '/')) and (best is None or len(p) > best[1]):
                best = (lib['id'], len(p))
    return best[0] if best else None


def for_item(item_id):
    """The library a Jellyfin item sits in (its ancestors include the library folder), or None."""
    if not item_id:
        return None
    if item_id in _item_lib:
        return _item_lib[item_id]
    ids = {lib['id'] for lib in all_libraries()}
    try:
        anc = core.jf_request('GET', f'/Items/{item_id}/Ancestors', token=config.secret('JELLYFIN_API_KEY')) or []
    except Exception:
        return None
    found = next((a['Id'] for a in anc if a.get('Id') in ids), None)
    if found:
        _item_lib[item_id] = found
    return found


def classify(path=None, item_id=None):
    return for_path(path) or for_item(item_id)


def legacy(kind):
    """The library for one of the old fixed kinds (movies / shows / anime), or None."""
    libs = all_libraries()
    named = lambda *names: next((l['id'] for l in libs if l['name'].lower() in names), None)
    if kind == 'movies':
        return named('movies', 'films') or next((l['id'] for l in libs if l['type'] == 'movies'), None)
    if kind == 'anime':
        return named('anime') or next((l['id'] for l in libs if 'anime' in l['name'].lower()), None)
    if kind == 'shows':
        return named('shows', 'tv shows', 'tv') or next(
            (l['id'] for l in libs if l['type'] == 'tvshows' and 'anime' not in l['name'].lower()), None)
    return None


# ---------- who can see what ----------

def access_from(user_json):
    """'*' (everything) or the list of library ids a Jellyfin user can see."""
    p = (user_json or {}).get('Policy') or {}
    if p.get('IsAdministrator') or p.get('EnableAllFolders'):
        return '*'
    return list(p.get('EnabledFolders') or [])


def store_access(jf_id, access):
    core.db().execute('update users set libraries=? where jf_id=?', (json.dumps(access), jf_id))


def refresh_access():
    """Re-read everyone's library access from Jellyfin. Returns how many people were updated."""
    users = core.jf_request('GET', '/Users', token=config.secret('JELLYFIN_API_KEY')) or []
    known = {r['jf_id'] for r in core.db().execute('select jf_id from users')}
    n = 0
    for u in users:
        if u.get('Id') in known:
            store_access(u['Id'], access_from(u))
            n += 1
    return n


def refresh_user(jf_id):
    info = core.jellyfin_user(jf_id)
    if info:
        store_access(jf_id, access_from(info))


def access_of(user):
    raw = user['libraries'] if 'libraries' in user.keys() else None
    if raw is None:
        refresh_user(user['jf_id'])
        row = core.get_user(user['jf_id'])
        raw = row['libraries'] if row else None
    return json.loads(raw) if raw else '*'


def can_see(user, lib_id):
    """Whether this person can see content in that library (unknown library: yes), and its `who` lets them."""
    if not lib_id:
        return True
    info = by_id(lib_id)
    if info and not core.who_allows(user, info['who']):
        return False
    access = access_of(user)
    return access == '*' or lib_id in access


def visible_for(user):
    access = access_of(user)
    return [l for l in visible() if (access == '*' or l['id'] in access) and core.who_allows(user, l['who'])]


# ---------- moving the old movies / shows / anime choices to libraries ----------

def migrate():
    """Once: people's movies / shows / anime choices (and queued library alerts) become their libraries'."""
    if core.meta_get('libraries_migrated'):
        return None
    if not all_libraries():
        return None                     # Jellyfin not reachable yet: try again later
    mapping = {k: legacy(k) for k in LEGACY}
    moved = 0
    for u in core.db().execute('select jf_id, prefs from users').fetchall():
        prefs = json.loads(u['prefs'] or '{}')
        changed = False
        for old, lib in mapping.items():
            for suffix in ('', '_mode'):
                if old + suffix in prefs:
                    value = prefs.pop(old + suffix)
                    if lib:
                        prefs[key(lib) + suffix] = value
                    changed = True
        if changed:
            core.db().execute('update users set prefs=? where jf_id=?', (json.dumps(prefs), u['jf_id']))
            moved += 1
    rows = 0
    for old, lib in mapping.items():
        if lib:
            rows += core.db().execute('update library set kind=? where kind=?', (lib, old)).rowcount
        else:
            core.db().execute("update library set state='dropped' where kind=? and state='pending'", (old,))
    core.meta_set('libraries_migrated', core.now())
    core.log('library alerts: moved the films/shows/anime choices of', moved, 'people and', rows, 'queued alerts to',
             {k: (by_id(v) or {}).get('name') for k, v in mapping.items()})
    return mapping
