"""The notifier part of Pusharr: Sonarr / Radarr / Seerr webhooks on their own internal port
(server.webhook_port, 8000; never published), new-episode decisions, new films/shows, request events and
the issue-replacement flow (issues.py), plus the optional Wizarr -> Seerr contact sync (contacts.py).

Sonarr (Connect -> Webhook, On Import) POSTs to /sonarr. Imports of the same series are batched
for notifications.batching.import_wait seconds, then for every Seerr user with a linked Jellyfin account
we decide whether they care about the series, and send them one push (their own toggles and mutes apply).

Someone qualifies for a series if any of these hold (each can be switched off in notifications.follow):
  following  - played any episode of it in the last recent_watch_days days
  caught-up  - played the latest episode that existed before this import
  watchlist  - it's on their Seerr watchlist
  requested  - they requested it in Seerr
and they haven't already played the new episode(s).

State (what's been sent, issue records, series that already had files) is in the app's SQLite DB.

  python app/notifier.py dry-run --tvdb 428433 --episodes S04E01,S04E02   # who would be told, and why
  python app/notifier.py dry-run --payload sonarr.json
  python app/notifier.py send-test <name>        # one real test message to that person
  python app/notifier.py waiting                 # alerts held until Jellyfin has the item with its metadata
  python app/notifier.py issues                  # Seerr issue replacements and their state (see issues.py)
  python app/notifier.py approve <seerr issue id>   # same as tapping Approve
  python app/notifier.py email-sync [--dry]      # Wizarr -> Seerr contact sync now (see contacts.py)
"""
import base64, datetime as dt, hmac, json, os, re, sys, threading, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import approvals
import config
import contacts
import core
import issues
import joins
import libraries

DATA_DIR = os.environ.get('DATA_DIR', '/data')
log = core.log
ENV = core.ENV


def import_wait():
    return config.get('notifications.batching.import_wait')


def url(app):
    return config.get(f'integrations.{app}.url').rstrip('/')


def webhooks_wanted():
    return any(config.get(f'integrations.{a}.enabled') for a in ('seerr', 'sonarr', 'radarr', 'wizarr'))


def http(method, url, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    h = {'Content-Type': 'application/json', **(headers or {})}
    with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=h), timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw and 'json' in r.headers.get('Content-Type', '') else raw


def jf(path):
    return http('GET', config.get('jellyfin.url').rstrip('/') + path,
                headers={'Authorization': f'MediaBrowser Token="{config.secret("JELLYFIN_API_KEY")}"'})


def seerr(path):
    return http('GET', url('seerr') + '/api/v1' + path, headers={'X-Api-Key': config.secret('SEERR_API_KEY', '')})


def sonarr(path):
    return http('GET', url('sonarr') + '/api/v3' + path, headers={'X-Api-Key': config.secret('SONARR_API_KEY', '')})


# ---------- sending ----------

def send(jf_id, title, message, tag, category='episodes', item_id=None, ids=None, mute_key=None, person=None,
         library=None):
    """One web push to one person (their own toggles, mutes and library access apply). Returns the result."""
    if not jf_id:
        raise ValueError(f'no Jellyfin user id for {person}')
    res = core.notify({'category': category, 'users': [jf_id], 'title': title, 'body': message, 'item_id': item_id,
                       'mute_key': mute_key, 'tag': tag, 'ids': ids, 'library': library})
    return next(iter(res.values()), {}) if isinstance(res, dict) and 'skipped' not in res else res


def alert_library(library, title, item_id=None, ids=None):
    """Something new arrived in a Jellyfin library ("New in <library>"); core fans it out by preference
    and library access."""
    try:
        res = core.notify({'category': 'library', 'library': library, 'title': title, 'item_id': item_id, 'ids': ids})
        log('library alert:', title, json.dumps(res))
    except Exception as e:
        log('ERROR library alert', title, repr(e)[:200])


# ---------- deciding ----------

def ep_key(e):
    return (e.get('ParentIndexNumber') or 0, e.get('IndexNumber') or 0)


def fmt(k):
    return f'S{k[0]}E{k[1]}'


def same_path(a, b):
    return bool(a and b) and a.rstrip('/') == b.rstrip('/')


def find_series(tvdb, tmdb, path=None):
    """The Jellyfin series by TVDB / TMDB id, else by folder (Sonarr's paths are Jellyfin's): a show Jellyfin
    hasn't identified yet has no provider ids."""
    items = jf('/Items?IncludeItemTypes=Series&Recursive=true&Fields=ProviderIds,Path&enableImages=false')['Items']
    for idk, val in (('Tvdb', tvdb), ('Tmdb', tmdb)):
        if val:
            for i in items:
                if str(i.get('ProviderIds', {}).get(idk, '')) == str(val):
                    return i
    return next((i for i in items if same_path(i.get('Path'), path)), None) if path else None


def seerr_users():
    res = seerr('/user?take=500')['results']
    return [u for u in res if u.get('jellyfinUserId')]


def seerr_pages(path):
    out, page = [], 1
    while True:
        sep = '&' if '?' in path else '?'
        r = seerr(f'{path}{sep}page={page}')
        out += r.get('results', [])
        if page >= r.get('totalPages', 1):
            return out
        page += 1


def seerr_requests(uid):
    out, skip = [], 0
    while True:
        r = seerr(f'/user/{uid}/requests?take=100&skip={skip}')
        out += r.get('results', [])
        skip += 100
        if skip >= r.get('pageInfo', {}).get('results', 0):
            return out


def decide(tvdb, tmdb, new_keys, now=None):
    """Return (series_name, [decision dicts]) for a batch of new episode keys {(season, episode)}."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rules = config.get('notifications.follow')
    series = find_series(tvdb, tmdb)
    name = series['Name'] if series else None
    out = []
    for u in seerr_users():
        uname = u.get('displayName') or u.get('jellyfinUsername') or u.get('username') or f'user{u["id"]}'
        d = {'user': uname, 'seerr_id': u['id'], 'jf_id': u['jellyfinUserId'], 'reasons': [], 'skip': None,
             'item_id': series['Id'] if series else None}
        eps = []
        if series:
            eps = [e for e in jf(f'/Shows/{series["Id"]}/Episodes?userId={u["jellyfinUserId"]}&Fields=UserData'
                                 f'&enableImages=false')['Items'] if ep_key(e)[0] > 0]
        played = {ep_key(e) for e in eps if e.get('UserData', {}).get('Played')}
        unplayed_new = sorted(k for k in new_keys if k not in played)
        if not unplayed_new:
            d['skip'] = 'already played the new episode(s)'
            out.append(d)
            continue
        cutoff = now - dt.timedelta(days=rules['recent_watch_days'])
        recent = [e for e in eps if (e.get('UserData', {}).get('LastPlayedDate') or '') >= cutoff.strftime('%Y-%m-%d')]
        if recent:
            last = max(e['UserData']['LastPlayedDate'] for e in recent)[:10]
            d['reasons'].append(f'following (last watched {last})')
        before = sorted(ep_key(e) for e in eps if ep_key(e) not in new_keys)
        if rules['caught_up'] and before and before[-1] in played:
            d['reasons'].append(f'caught up (had watched {fmt(before[-1])}, the latest before this)')
        if rules['watchlist'] and tmdb and any(str(w.get('tmdbId')) == str(tmdb)
                                               for w in seerr_pages(f'/user/{u["id"]}/watchlist')):
            d['reasons'].append('on their Seerr watchlist')
        for r in seerr_requests(u['id']) if rules['requested'] else []:
            m = r.get('media') or {}
            if r.get('type') == 'tv' and ((tvdb and str(m.get('tvdbId')) == str(tvdb)) or
                                          (tmdb and str(m.get('tmdbId')) == str(tmdb))):
                d['reasons'].append('requested it in Seerr')
                break
        furthest = max(played) if played else None
        allk = sorted(set(before) | set(new_keys))
        behind = len([k for k in allk if furthest is None or k > furthest])
        d.update(new=unplayed_new, furthest=furthest, behind=behind)
        first_ep = next((e['Id'] for e in eps if ep_key(e) == unplayed_new[0]), None)
        d['first_item_id'] = first_ep       # what "Play" starts: the (first) new episode
        if len(unplayed_new) == 1:      # link straight to the episode
            d['item_id'] = first_ep or d['item_id']
        out.append(d)
    return name, out


def message(series_name, d):
    new, furthest, behind = d['new'], d['furthest'], d['behind']
    if furthest is None:
        where = config.msg('progress_not_started')
    elif behind == len(new):        # the new episode(s) are exactly the next ones for them
        where = config.msg('progress_next', furthest=fmt(furthest))
    else:
        where = config.msg('progress_behind', furthest=fmt(furthest), behind=behind)
    if len(new) == 1:
        return config.msg('episode_one', series=series_name, episode=fmt(new[0]), progress=where, user=d.get('user'))
    return config.msg('episode_many', series=series_name, count=len(new), first=fmt(new[0]), last=fmt(new[-1]),
                      progress=where, user=d.get('user'))


# ---------- batching + sending ----------

_lock = threading.Lock()
_pending = {}  # sonarr series id -> {'tvdb', 'tmdb', 'title', 'keys': set, 'timer'}


def load_sent():
    """Keys (person|tvdb|SxEy) of episode alerts already sent, so nothing is sent twice."""
    return {r['key'] for r in core.db().execute('select key from notifier_sent')}


def save_sent(s):
    core.db().executemany('insert or ignore into notifier_sent(key, ts) values(?, ?)', [(k, core.now()) for k in s])


def process(tvdb, tmdb, sonarr_title, keys, dry=False, library=None):
    name, decisions = decide(tvdb, tmdb, set(keys))
    name = name or sonarr_title
    sent = load_sent()
    report = []
    for d in decisions:
        line = {'user': d['user'], 'reasons': d['reasons'], 'skip': d['skip']}
        if not d['skip'] and not d['reasons']:
            line['skip'] = "doesn't follow it"
        if not line['skip']:
            title, body = message(name, d)
            line.update(title=title, message=body)
            ids = [f"{d['user']}|{tvdb}|{fmt(k)}" for k in d['new']]
            if all(i in sent for i in ids):
                line['skip'] = 'already notified'
            elif not dry:
                try:
                    # the (first) new episode: what Play starts; its page can find it later if it's not known yet
                    ids_ = {'type': 'show', 'tvdb': tvdb, 'tmdb': tmdb, 'season': d['new'][0][0], 'episode': d['new'][0][1]}
                    if len(d['new']) > 1:
                        ids_['batch'] = True       # Watch opens the series, Play the first new episode
                    if d.get('first_item_id'):
                        ids_['item'] = d['first_item_id']
                    res = send(d['jf_id'], title, body, tag=f'jellyfin-{tvdb}', person=d['user'], category='episodes',
                               item_id=d.get('item_id'), ids=ids_, library=library,
                               mute_key=f'tvdb:{tvdb}' if tvdb else None)
                    sent.update(ids)
                    line['sent'] = True
                    if isinstance(res, dict) and res:
                        line['result'] = res
                except Exception as e:
                    line['error'] = repr(e)[:200]
        report.append(line)
    if not dry:
        save_sent(sent)
    return name, report


def flush(series_id):
    """The batch window for a series is over: hold its alert until Jellyfin has the episodes."""
    with _lock:
        p = _pending.pop(series_id, None)
    if not p:
        return
    hold('episodes', f'series:{series_id}', {'tvdb': p['tvdb'], 'tmdb': p['tmdb'], 'title': p['title'],
                                             'series_path': p.get('series_path'),
                                             'keys': sorted(p['keys']), 'new_series': bool(p.get('new_series')),
                                             'library': libraries.for_path(p.get('series_path')),
                                             'paths': sorted(p.get('paths', ()))})


def send_episodes(p, info=None):
    """Send a (ready, or waited long enough) batch: the new-show alert, then each person's episode alert."""
    keys = {tuple(k) for k in p['keys']}
    sid = (info or {}).get('series')
    if not sid:
        s = find_series(p['tvdb'], p['tmdb'], p.get('series_path'))
        sid = s['Id'] if s else None
    # which library: its folder, else where Jellyfin put the series
    library = p.get('library') or libraries.for_path(p.get('series_path')) or libraries.for_item(sid)
    if p.get('new_series'):
        alert_library(library, p['title'], sid, {'type': 'show', 'tvdb': p['tvdb'], 'tmdb': p['tmdb'], 'item': sid})
    try:
        name, report = process(p['tvdb'], p['tmdb'], p['title'], keys, library=library)
        for r in report:
            if r.get('sent'):
                log('SENT', r['user'], '|', r['title'], '|', r['message'], '|', '; '.join(r['reasons']),
                    '|', json.dumps(r.get('result', {})))
            elif r.get('error'):
                log('ERROR', r['user'], r['error'])
            elif r['reasons']:
                log('skip', r['user'], '-', r['skip'], '|', '; '.join(r['reasons']))
        log('done', name, sorted(fmt(k) for k in keys),
            'sent to', [r['user'] for r in report if r.get('sent')] or 'nobody')
    except Exception as e:
        log('ERROR processing', p['title'], repr(e))


def queue_import(payload):
    s = payload.get('series', {})
    keys = {(e['seasonNumber'], e['episodeNumber']) for e in payload.get('episodes', [])}
    if not keys:
        return 'no episodes'
    with _lock:
        new_series = seen_series_add(s.get('id'))
        p = _pending.setdefault(s.get('id'), {'tvdb': s.get('tvdbId'), 'tmdb': s.get('tmdbId'),
                                               'title': s.get('title'), 'keys': set(), 'timer': None,
                                               'series_path': s.get('path')})
        p['new_series'] = p.get('new_series') or new_series
        p['keys'] |= keys
        files = payload.get('episodeFiles') or ([payload['episodeFile']] if payload.get('episodeFile') else [])
        p.setdefault('paths', set()).update(f['path'] for f in files if f.get('path'))
        if p['timer'] is None:      # first import of this series opens a batch window
            p['timer'] = threading.Timer(import_wait(), flush, [s.get('id')])
            p['timer'].daemon = True
            p['timer'].start()
    return f"queued {s.get('title')} {sorted(fmt(k) for k in keys)}"


# ---------- new series / new films ----------

_seen = None


def load_seen():
    """Series that already have files. Seeded from Sonarr the first time the service runs."""
    global _seen
    _seen = {r['id'] for r in core.db().execute('select id from seen_series')}
    if not _seen:
        _seen = {x['id'] for x in sonarr('/series') if (x.get('statistics') or {}).get('episodeFileCount')}
        core.db().executemany('insert or ignore into seen_series(id) values(?)', [(i,) for i in _seen])
        log(f'seeded {len(_seen)} existing series (no "new show" alerts for these)')


def seen_series_add(series_id):
    """True the first time a Sonarr series gets a file, i.e. a new show."""
    if _seen is None:
        load_seen()
    if series_id in _seen:
        return False
    _seen.add(series_id)
    core.db().execute('insert or ignore into seen_series(id) values(?)', (series_id,))
    return True


def find_movie(tmdb, path=None):
    """The Jellyfin film by TMDB id, else by its file (Radarr's paths are Jellyfin's)."""
    items = jf('/Items?IncludeItemTypes=Movie&Recursive=true&Fields=ProviderIds,Path&enableImages=false')['Items']
    return (next((i for i in items if str(i.get('ProviderIds', {}).get('Tmdb')) == str(tmdb)), None)
            or (next((i for i in items if same_path(i.get('Path'), path)), None) if path else None))


def radarr_import(payload):
    """A film imported for the first time (not an upgrade): alert once Jellyfin has it, with its metadata."""
    m = payload.get('movie', {})
    title = f"{m.get('title')} ({m.get('year')})" if m.get('year') else m.get('title')
    path = (payload.get('movieFile') or {}).get('path')
    if path and not path.startswith('/'):
        path = (m.get('folderPath') or '').rstrip('/') + '/' + path
    hold('movie', f"movie:{m.get('tmdbId')}", {'title': title, 'tmdb': m.get('tmdbId'), 'paths': [path] if path else [],
                                               'library': libraries.for_path(path or m.get('folderPath'))})
    return f'new film {title}: alert once Jellyfin has it'


def send_movie(p, info=None):
    item = (info or {}).get('item')
    if not item:
        f = find_movie(p['tmdb'], (p.get('paths') or [None])[0])
        item = f['Id'] if f else None
    library = p.get('library') or libraries.for_path((p.get('paths') or [None])[0]) or libraries.for_item(item)
    alert_library(library, p['title'], item, {'type': 'movie', 'tmdb': p['tmdb'], 'item': item})


# ---------- holding alerts until the content is really ready ----------
#
# Sonarr / Radarr say "imported" before Jellyfin has scanned the file and fetched its title, overview and
# poster. Alerts wait in the DB (so a restart doesn't lose them) until Jellyfin has all of that: checked
# every check_every s, backing off to check_every_max; after refresh_after s Jellyfin is asked to look at
# the new files / refresh the item; after max_wait_hours the alert goes anyway (its page still finds the
# item later). Batches of the same series keep merging while they wait.

def wait_conf():
    return config.get('notifications.wait_for_ready')


def hold(kind, key, data):
    """Queue an alert (kind: episodes / movie / request) until its content is ready."""
    if not wait_conf()['enabled']:
        threading.Thread(target=release, args=(kind, data, None), daemon=True).start()
        return 'sending now'
    t = core.now()
    with _lock:
        row = core.db().execute('select * from pending_alerts where key=?', (key,)).fetchone()
        if row and kind == 'episodes':
            d = json.loads(row['data'])
            d['keys'] = sorted({tuple(k) for k in d['keys']} | {tuple(k) for k in data['keys']})
            d['new_series'] = d.get('new_series') or data.get('new_series')
            d['series_path'] = d.get('series_path') or data.get('series_path')
            d['paths'] = sorted(set(d.get('paths', [])) | set(data.get('paths', [])))
            core.db().execute('update pending_alerts set data=? where id=?', (json.dumps(d), row['id']))
            log('holding', data.get('title'), [fmt(k) for k in d['keys']], '(merged into the waiting alert)')
            return 'merged'
        if row:
            return 'already waiting'
        core.db().execute('insert into pending_alerts(kind, key, data, created, next_check, checks, refreshed) '
                          'values(?,?,?,?,?,0,0)', (kind, key, json.dumps(data), t, t + wait_conf()['check_every']))
    log('holding', kind, data.get('title'), 'until Jellyfin has it with its metadata')
    return 'held'


def complete(item):
    """A Jellyfin item with its title, overview and poster."""
    return bool(item and item.get('Name') and (item.get('Overview') or '').strip()
                and (item.get('ImageTags') or {}).get('Primary'))


def jf_item(item_id):
    items = jf(f'/Items?Ids={item_id}&Fields=Overview,ProviderIds').get('Items') or []
    return items[0] if items else None


def check(kind, p):
    """(ready, info, what's missing). info: Jellyfin ids found so far."""
    if kind == 'movie' or (kind == 'request' and p.get('media_type') == 'movie'):
        m = find_movie(p['tmdb'], (p.get('paths') or [None])[0])
        if not m:
            return False, {}, 'film not in Jellyfin yet'
        item = jf_item(m['Id'])
        return complete(item), {'item': m['Id']}, '' if complete(item) else 'film has no title / overview / poster yet'
    series = find_series(p.get('tvdb'), p.get('tmdb'), p.get('series_path'))
    if not series:
        return False, {}, 'series not in Jellyfin yet'
    info = {'series': series['Id'], 'episodes': {}}
    if kind == 'request' or p.get('new_series'):
        if not complete(jf_item(series['Id'])):
            return False, info, 'series has no title / overview / poster yet'
    if kind == 'request':
        return True, info, ''
    missing = []
    for season in sorted({k[0] for k in p['keys']}):
        eps = jf(f"/Shows/{series['Id']}/Episodes?season={season}&Fields=Overview").get('Items') or []
        by_num = {e.get('IndexNumber'): e for e in eps}
        for s_, e_ in [k for k in p['keys'] if k[0] == season]:
            ep = by_num.get(e_)
            if ep:
                info['episodes'][fmt((s_, e_))] = ep['Id']
            if not (ep and complete(ep) and ep.get('SeriesId')):
                missing.append(fmt((s_, e_)))
    return not missing, info, f"waiting for {', '.join(missing)}" if missing else ''


def nudge(kind, p, info):
    """Ask Jellyfin to look: at the new files if it doesn't have the item, else refresh the item."""
    key = config.secret('JELLYFIN_API_KEY')
    hdr = {'Authorization': f'MediaBrowser Token="{key}"'}
    base = config.get('jellyfin.url').rstrip('/')
    done = []
    try:
        target = info.get('item') or (info.get('series') if kind == 'request' or p.get('new_series') else None)
        episodes = [i for i in (info.get('episodes') or {}).values()]
        if kind == 'episodes' and info.get('series') and len(episodes) < len(p['keys']):
            http('POST', f"{base}/Items/{info['series']}/Refresh?Recursive=true&MetadataRefreshMode=Default"
                         '&ImageRefreshMode=Default', headers=hdr)
            done.append('series refresh')
        for item in ([target] if target else []) + episodes:
            http('POST', f'{base}/Items/{item}/Refresh?MetadataRefreshMode=FullRefresh&ImageRefreshMode=FullRefresh'
                         '&ReplaceAllMetadata=false&ReplaceAllImages=false', headers=hdr)
            done.append(f'refresh {item[:8]}')
        if not info.get('series') and not info.get('item') and p.get('paths'):
            http('POST', f'{base}/Library/Media/Updated',
                 {'Updates': [{'Path': path, 'UpdateType': 'Created'} for path in p['paths']]}, headers=hdr)
            done.append(f"scan {len(p['paths'])} new file(s)")
    except Exception as e:
        done.append(f'failed: {repr(e)[:120]}')
    log('asked Jellyfin to look at', p.get('title'), '-', ', '.join(done) or 'nothing to ask')


def release(kind, p, info):
    if kind == 'episodes':
        send_episodes(p, info)
    elif kind == 'movie':
        send_movie(p, info)
    elif kind == 'request':
        issues.guard(issues.request_ready, p['payload'], (info or {}).get('item') or (info or {}).get('series'))


def ready_tick(t=None):
    """Check the waiting alerts that are due; send the ready (or overdue) ones."""
    t = t or core.now()
    w = wait_conf()
    for row in core.db().execute('select * from pending_alerts where next_check <= ? order by created', (t,)).fetchall():
        p = json.loads(row['data'])
        age = t - row['created']
        try:
            ready, info, why = check(row['kind'], p)
        except Exception as e:
            ready, info, why = False, {}, f'check failed: {repr(e)[:120]}'
        overdue = age >= w['max_wait_hours'] * 3600
        if ready or overdue:
            core.db().execute('delete from pending_alerts where id=?', (row['id'],))
            log('ready:' if ready else f"sending anyway after {w['max_wait_hours']} h ({why}):", row['kind'], p.get('title'),
                f'after {age // 60} min')
            guard_release(row['kind'], p, info)
            continue
        refreshed = row['refreshed']
        if age >= w['refresh_after'] and not refreshed:
            nudge(row['kind'], p, info)
            refreshed = 1
        checks = row['checks'] + 1
        interval = min(w['check_every'] * 2 ** max(0, checks - 3), w['check_every_max'])
        core.db().execute('update pending_alerts set checks=?, refreshed=?, next_check=? where id=?',
                          (checks, refreshed, t + interval, row['id']))
        log('still waiting:', p.get('title'), '-', why, f'(next look in {interval // 60} min)')


def guard_release(kind, p, info):
    try:
        release(kind, p, info)
    except Exception as e:
        log('ERROR sending', kind, p.get('title'), repr(e)[:200])


def ready_worker():
    while True:
        try:
            ready_tick()
        except Exception as e:
            log('ERROR ready worker', repr(e)[:200])
        time.sleep(30)


def waiting():
    return [dict(r, data=json.loads(r['data'])) for r in core.db().execute('select * from pending_alerts order by created')]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, text):
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(text.encode())

    def do_GET(self):
        self.reply(200, 'ok') if self.path == '/health' else self.reply(404, 'not found')

    def do_POST(self):
        if self.path not in ('/sonarr', '/seerr', '/radarr', '/wizarr'):
            return self.reply(404, 'not found')
        want = 'Basic ' + base64.b64encode(f"{ENV.get('WEBHOOK_USER', '')}:{ENV.get('WEBHOOK_PASSWORD', '')}".encode()).decode()
        if not (ENV.get('WEBHOOK_USER') and ENV.get('WEBHOOK_PASSWORD')):
            return self.reply(503, 'webhook login not set')
        app = self.path[1:]
        if not config.get(f'integrations.{app}.enabled'):
            return self.reply(404, f'{app} is switched off')
        if not hmac.compare_digest(self.headers.get('Authorization', ''), want):
            return self.reply(401, 'unauthorized')
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.path == '/wizarr':      # Wizarr's ntfy agent: plain text "User X has joined your server!"
            log('Wizarr says:', raw.decode('utf-8', 'replace')[:120])
            return self.reply(200, joins.on_wizarr(raw.decode('utf-8', 'replace')))
        try:
            payload = json.loads(raw or b'{}')
        except ValueError:
            return self.reply(400, 'bad json')
        if self.path == '/seerr':
            return self.reply(200, issues.on_seerr(payload))
        ev = payload.get('eventType')
        if ev == 'Test':
            log(f'{self.path[1:].title()} test webhook received')
            return self.reply(200, 'ok')
        if self.path == '/radarr':
            if ev != 'Download' or payload.get('isUpgrade') or payload.get('deletedFiles'):
                return self.reply(200, f'ignored {ev}')
            msg = radarr_import(payload)
            log(msg)
            return self.reply(200, msg)
        if ev != 'Download':
            return self.reply(200, f'ignored {ev}')
        if payload.get('isUpgrade') or payload.get('deletedFiles'):
            log('ignored upgrade/re-import', payload.get('series', {}).get('title'))
            return self.reply(200, 'ignored upgrade')
        msg = queue_import(payload)
        log(msg)
        self.reply(200, msg)


# ---------- CLI ----------

def parse_eps(s):
    return {tuple(int(x) for x in re.match(r'S(\d+)E(\d+)$', e.strip(), re.I).groups()) for e in s.split(',')}


def setup():
    """Wire the notifier modules together (shared by the server and the CLI)."""
    issues.N = contacts.N = joins.N = sys.modules[__name__]
    core.init_db()
    approvals.init_db()
    joins.init_db()
    core.db().execute('''create table if not exists pending_alerts (
        id integer primary key, kind text not null, key text unique, data text not null, created integer not null,
        next_check integer not null, checks integer not null default 0, refreshed integer not null default 0)''')


def start():
    """Started by server.py: webhooks on server.webhook_port, the issue worker, contact sync."""
    setup()
    joins.start()               # new Jellyfin accounts (Wizarr invites) -> admins
    if not webhooks_wanted():
        log('Seerr, Sonarr and Radarr are all switched off: no webhooks')
        return None
    srv = ThreadingHTTPServer(('0.0.0.0', config.get('server.webhook_port')), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f'webhooks on :{srv.server_port} (batch {import_wait()}s, following = '
        f'{config.get("notifications.follow.recent_watch_days")} days)')
    threading.Thread(target=ready_worker, daemon=True).start()
    if config.get('integrations.seerr.enabled'):
        issues.start()
    contacts.start()
    if config.get('integrations.sonarr.enabled'):
        try:
            load_seen()
        except Exception as e:
            log('could not read Sonarr series yet:', repr(e)[:200])
    return srv


def main(argv):
    if __name__ == '__main__':      # running as a script: make the module importable as "notifier" too
        sys.modules['notifier'] = sys.modules[__name__]
    setup()
    cmd = argv[1] if len(argv) > 1 else 'help'
    if cmd == 'dry-run':
        a = dict(zip(argv[2::2], argv[3::2]))
        if '--payload' in a:
            p = json.load(open(a['--payload']))
            s = p['series']; tvdb, tmdb, title = s.get('tvdbId'), s.get('tmdbId'), s.get('title')
            keys = {(e['seasonNumber'], e['episodeNumber']) for e in p['episodes']}
        else:
            tvdb = a.get('--tvdb'); tmdb = a.get('--tmdb'); title = None
            if tvdb and not tmdb:
                s = sonarr(f'/series?tvdbId={tvdb}')
                tmdb = s[0].get('tmdbId') if s else None
                title = s[0]['title'] if s else None
            keys = parse_eps(a['--episodes'])
        name, report = process(tvdb, tmdb, title, keys, dry=True)
        print(f'{name} {sorted(fmt(k) for k in keys)}')
        for r in report:
            if r['reasons'] or r.get('title'):
                print(f"  {r['user']:10} {'WOULD SEND' if not r['skip'] else 'skip: ' + r['skip']}")
                print(f"             why: {'; '.join(r['reasons']) or '-'}")
                if r.get('title'):
                    print(f"             msg: {r['title']} / {r['message']}")
            else:
                print(f"  {r['user']:10} skip: {r['skip']}")
    elif cmd == 'send-test':
        u = next((x for x in core.db().execute('select * from users') if x['name'].lower() == argv[2].lower()), None)
        if not u:
            sys.exit(f'{argv[2]}: not a known user (they need to have signed in, or been sent something)')
        title, body = config.msg('test', user=u['name'])
        print(send(u['jf_id'], title, body, tag='test', category='test', person=u['name']))
    elif cmd == 'email-sync':        # email-sync [--dry]: one Wizarr -> Seerr contact sync now
        changes, n = contacts.sync(dry='--dry' in argv)
        print(f'{n} Wizarr users;', f'{len(changes)} change(s)' if changes else 'nothing to change')
        for c in changes:
            print(' ', c)
    elif cmd == 'waiting':          # alerts held until Jellyfin has the content
        for r in waiting():
            d = r['data']
            print(f"{r['kind']:8} {d.get('title')} {[fmt(k) for k in d.get('keys', [])] or ''} | waiting "
                  f"{(core.now() - r['created']) // 60} min, {r['checks']} checks, refreshed: {bool(r['refreshed'])}")
    elif cmd == 'issues':
        for r in issues.load().values():
            print(f"#{r['issue_id']} {r['kind']} {r['target'].get('title')} from {r['reporter']}: {r['state']}"
                  + (' (dry run)' if r.get('dry') else ''))
            for line in r.get('log', [])[-12:]:
                print('   ', line)
    elif cmd == 'approve':
        print(issues.answer(int(argv[2]), 'approve', 'the command line'))
        while issues.load()[argv[2]]['state'] == 'approved':
            time.sleep(2)
        print('state:', issues.load()[argv[2]]['state'])
    else:
        print(__doc__)


if __name__ == '__main__':
    main(sys.argv)
