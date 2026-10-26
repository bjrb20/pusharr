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

DATA_DIR = os.environ.get('DATA_DIR', '/data')
log = core.log
ENV = core.ENV


def import_wait():
    return config.get('notifications.batching.import_wait')


def url(app):
    return config.get(f'integrations.{app}.url').rstrip('/')


def webhooks_wanted():
    return any(config.get(f'integrations.{a}.enabled') for a in ('seerr', 'sonarr', 'radarr'))


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

def send(jf_id, title, message, tag, category='episodes', item_id=None, ids=None, mute_key=None, person=None):
    """One web push to one person (their own toggles and mutes apply). Returns the delivery result."""
    if not jf_id:
        raise ValueError(f'no Jellyfin user id for {person}')
    res = core.notify({'category': category, 'users': [jf_id], 'title': title, 'body': message, 'item_id': item_id,
                       'mute_key': mute_key, 'tag': tag, 'ids': ids})
    return next(iter(res.values()), {}) if isinstance(res, dict) and 'skipped' not in res else res


def alert_library(kind, title, item_id=None, ids=None):
    """Something new arrived (kind: movies/shows/anime); core fans it out by preference."""
    try:
        core.notify({'category': 'library', 'kind': kind, 'title': title, 'item_id': item_id, 'ids': ids})
        log('library alert:', kind, title)
    except Exception as e:
        log('ERROR library alert', kind, title, repr(e)[:200])


# ---------- deciding ----------

def ep_key(e):
    return (e.get('ParentIndexNumber') or 0, e.get('IndexNumber') or 0)


def fmt(k):
    return f'S{k[0]}E{k[1]}'


def find_series(tvdb, tmdb):
    items = jf('/Items?IncludeItemTypes=Series&Recursive=true&Fields=ProviderIds&enableImages=false')['Items']
    for idk, val in (('Tvdb', tvdb), ('Tmdb', tmdb)):
        if val:
            for i in items:
                if str(i.get('ProviderIds', {}).get(idk, '')) == str(val):
                    return i
    return None


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
        if len(unplayed_new) == 1:      # link straight to the episode
            d['item_id'] = next((e['Id'] for e in eps if ep_key(e) == unplayed_new[0]), d['item_id'])
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


def process(tvdb, tmdb, sonarr_title, keys, dry=False):
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
                    res = send(d['jf_id'], title, body, tag=f'jellyfin-{tvdb}', person=d['user'], category='episodes',
                               item_id=d.get('item_id'), ids={'type': 'show', 'tvdb': tvdb, 'tmdb': tmdb},
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
    with _lock:
        p = _pending.pop(series_id, None)
    if not p:
        return
    if p.get('new_series'):
        s = find_series(p['tvdb'], p['tmdb'])
        alert_library('anime' if p.get('anime') else 'shows', p['title'], s['Id'] if s else None,
                      {'type': 'show', 'tvdb': p['tvdb'], 'tmdb': p['tmdb']})
    try:
        name, report = process(p['tvdb'], p['tmdb'], p['title'], p['keys'])
        for r in report:
            if r.get('sent'):
                log('SENT', r['user'], '|', r['title'], '|', r['message'], '|', '; '.join(r['reasons']),
                    '|', json.dumps(r.get('result', {})))
            elif r.get('error'):
                log('ERROR', r['user'], r['error'])
            elif r['reasons']:
                log('skip', r['user'], '-', r['skip'], '|', '; '.join(r['reasons']))
        log('done', name, sorted(fmt(k) for k in p['keys']),
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
                                               'anime': s.get('type') == 'anime' or '/Anime/' in (s.get('path') or '')})
        p['new_series'] = p.get('new_series') or new_series
        p['keys'] |= keys
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


def find_movie(tmdb):
    items = jf('/Items?IncludeItemTypes=Movie&Recursive=true&Fields=ProviderIds&enableImages=false')['Items']
    return next((i for i in items if str(i.get('ProviderIds', {}).get('Tmdb')) == str(tmdb)), None)


def radarr_import(payload):
    """A film imported for the first time (not an upgrade): wait for Jellyfin to pick it up, then alert."""
    m = payload.get('movie', {})
    title = f"{m.get('title')} ({m.get('year')})" if m.get('year') else m.get('title')

    def later():
        try:
            item = find_movie(m.get('tmdbId'))
        except Exception:
            item = None
        alert_library('movies', title, item['Id'] if item else None, {'type': 'movie', 'tmdb': m.get('tmdbId')})
    t = threading.Timer(import_wait(), later)
    t.daemon = True
    t.start()
    return f'new film {title}: alert in {import_wait()}s'


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
        if self.path not in ('/sonarr', '/seerr', '/radarr'):
            return self.reply(404, 'not found')
        want = 'Basic ' + base64.b64encode(f"{ENV.get('WEBHOOK_USER', '')}:{ENV.get('WEBHOOK_PASSWORD', '')}".encode()).decode()
        if not (ENV.get('WEBHOOK_USER') and ENV.get('WEBHOOK_PASSWORD')):
            return self.reply(503, 'webhook login not set')
        app = self.path[1:]
        if not config.get(f'integrations.{app}.enabled'):
            return self.reply(404, f'{app} is switched off')
        if not hmac.compare_digest(self.headers.get('Authorization', ''), want):
            return self.reply(401, 'unauthorized')
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
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
    issues.N = contacts.N = sys.modules[__name__]
    core.init_db()
    approvals.init_db()


def start():
    """Started by server.py: webhooks on server.webhook_port, the issue worker, contact sync."""
    setup()
    if not webhooks_wanted():
        log('Seerr, Sonarr and Radarr are all switched off: no webhooks')
        return None
    srv = ThreadingHTTPServer(('0.0.0.0', config.get('server.webhook_port')), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f'webhooks on :{srv.server_port} (batch {import_wait()}s, following = '
        f'{config.get("notifications.follow.recent_watch_days")} days)')
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
