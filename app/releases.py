"""Telling requesters what happened to their Seerr request: approved (notification type `approved`),
declined (`declined`, with the reason given on Pusharr's Pending page), and, for an approved film that
isn't out yet, when to expect it (folded into the approved message, so it's one alert).

Nobody is told that a request they approved themselves was approved (admins requesting for themselves,
or requests auto-approved by the requester's own permission); they still hear a film's release date.
Seerr has no decline reason of its own, so declines made in Seerr come without one.

Approved films that aren't out yet: tell the requester when to expect them.

When Seerr approves a film request (by an approver, or by itself) and Radarr says the film isn't out to
download yet (still in cinemas, or not released at all), the requester gets e.g.
"🎬 The Odyssey is still in cinemas / It's out digitally on 17 Nov, and Benflix will grab it then. We'll let
you know when it's ready." The date is Radarr's digitalRelease, else its physicalRelease; without either,
the message says it isn't out digitally yet. The film is then watched: if Radarr's date changes, the
requester gets a short update. The usual "ready to watch" alert still comes when the film lands.

Seerr's webhook needs Request Approved and Request Automatically Approved ticked.
"""
import datetime as dt
import json
import threading
import time
import traceback

import config
import core

N = None                    # the notifier module, set by notifier.py
FIND_TRIES, FIND_GAP = 10, 60   # Seerr adds the film to Radarr just after approving: look for it for ~10 min
WATCH_EVERY = 6 * 3600


def init_db():
    core.db().execute('''create table if not exists release_watch (
        request_id integer primary key, tmdb integer, jf_id text, title text, date text, how text,
        created integer, done integer)''')
    core.db().execute('''create table if not exists request_alerts (
        request_id integer, kind text, ts integer, primary key (request_id, kind))''')


def claim(rid, kind):
    """True the first time (request, approved / declined) is claimed: each is told once."""
    init_db()
    return core.db().execute('insert or ignore into request_alerts(request_id, kind, ts) values(?,?,?)',
                             (int(rid), kind, core.now())).rowcount == 1


def what_of(r, rid=None):
    """The film's title, or the show with its seasons."""
    import approvals
    d = approvals.describe(dict(r, id=r.get('id', rid)))
    if r.get('type') == 'movie':
        return d['title']
    seasons = sorted(s['seasonNumber'] for s in r.get('seasons') or [])
    return d['title'] + (f" season{'s' if len(seasons) > 1 else ''} {', '.join(map(str, seasons))}" if seasons else '')


def self_approved(r):
    """The requester approved it themselves (or it was auto-approved by their own permission)."""
    by, who = r.get('modifiedBy') or {}, r.get('requestedBy') or {}
    return bool(by.get('id')) and by.get('id') == who.get('id')


def notifier():
    global N
    if N is None:           # called from the web server's Pending page before the notifier is set up
        import notifier as n
        N = n
    return N


def seerr_request(rid):
    notifier()
    return N.http('GET', N.url('seerr') + f'/api/v1/request/{int(rid)}',
                  headers={'X-Api-Key': config.secret('SEERR_API_KEY', '')})


def radarr_movie(tmdb):
    found = N.http('GET', N.url('radarr') + f'/api/v3/movie?tmdbId={int(tmdb)}',
                   headers={'X-Api-Key': config.secret('RADARR_API_KEY', '')})
    return found[0] if found else None


def uk_date(d, today=None):
    """17 Nov (this year) or 17 Nov 2027."""
    today = today or dt.datetime.now(config.tz()).date()
    s = f'{d.day} {d.strftime("%b")}'
    return s if d.year == today.year else f'{s} {d.year}'


def release_date(m):
    """(date, how) from Radarr: digital release, else physical; how is 'digitally' or 'on disc'."""
    for field, how in (('digitalRelease', 'digitally'), ('physicalRelease', 'on disc')):
        if m.get(field):
            return dt.date.fromisoformat(m[field][:10]), how
    return None, None


def not_out(m, today=None):
    """True while the film can't be downloaded yet: no file, and no digital / disc release that has passed."""
    today = today or dt.datetime.now(config.tz()).date()
    if m.get('hasFile'):
        return False
    d, _ = release_date(m)
    return d is None or d > today


def message(m, today=None):
    today = today or dt.datetime.now(config.tz()).date()
    title = m['title']
    d, how = release_date(m)
    cinemas = m.get('status') == 'inCinemas' or (m.get('inCinemas') and m['inCinemas'][:10] <= today.isoformat())
    key = ('release_cinemas' if cinemas else 'release_upcoming') + ('' if d else '_nodate')
    return config.msg(key, title=title, date=uk_date(d, today) if d else '', how=how or 'digitally'), d, how


def on_approved(payload):
    """Seerr approved a request (MEDIA_APPROVED / MEDIA_AUTO_APPROVED): queue the check (in a thread)."""
    rid = payload.get('request_id')
    if not rid:
        return 'no request id'
    threading.Thread(target=guard, args=(check_request, int(rid)), daemon=True).start()
    return 'release check queued'


def on_declined(payload):
    rid = payload.get('request_id')
    if not rid:
        return 'no request id'
    threading.Thread(target=guard, args=(declined, int(rid)), daemon=True).start()
    return 'decline queued'


def guard(fn, *a):
    try:
        fn(*a)
    except Exception:
        core.log('ERROR', fn.__name__, a, traceback.format_exc()[-400:])


def check_request(rid, tries=FIND_TRIES, gap=FIND_GAP):
    """After approval: tell the requester it's approved (unless they approved it themselves); for a film
    that isn't out yet, the same message says when to expect it, and the film is watched for date changes."""
    if core.db().execute('select 1 from release_watch where request_id=?', (rid,)).fetchone():
        return 'already told'
    r = seerr_request(rid)
    media, who = r.get('media') or {}, r.get('requestedBy') or {}
    jf_id, mine = who.get('jellyfinUserId'), self_approved(r)
    kind = 'movie' if r.get('type') == 'movie' else 'show'
    ids = {'type': kind, 'tmdb': media.get('tmdbId'), 'tvdb': media.get('tvdbId')}
    m = None
    if kind == 'movie' and media.get('tmdbId') and config.get('integrations.radarr.enabled'):
        for i in range(tries):
            m = radarr_movie(media['tmdbId'])
            if m:
                break
            time.sleep(gap)
        if not m:
            core.log(f'request #{rid}: film not in Radarr after {tries} looks; no release date')
    if m and not_out(m):
        (title, body), d, how = message(m)
        if not mine:
            title = config.msg('request_approved_release', title=m['title'])[0]
        if not mine and not claim(rid, 'approved'):
            return 'already told'
        core.db().execute('insert or ignore into release_watch(request_id, tmdb, jf_id, title, date, how, created, done) '
                          'values(?,?,?,?,?,?,?,0)', (rid, media['tmdbId'], jf_id, m['title'],
                                                       d.isoformat() if d else None, how, core.now()))
        res = N.send(jf_id, title, body, f'request-{rid}', category='requests' if mine else 'approved',
                     person=who.get('displayName'), ids=ids) if jf_id else 'no Jellyfin user'
        core.log(f'request #{rid}: {m["title"]} approved, not out yet ({d or "no date"}) ->', json.dumps(res))
        return 'told'
    if mine:
        return 'approved by the requester'
    if not claim(rid, 'approved'):
        return 'already told'
    by = (r.get('modifiedBy') or {}).get('displayName')
    what = m['title'] if m else what_of(r, rid)
    title, body = config.msg('request_approved_by', approver=by, what=what) if by else config.msg('request_approved', what=what)
    res = N.send(jf_id, title, body, f'request-{rid}', category='approved', person=who.get('displayName'),
                 ids=ids) if jf_id else 'no Jellyfin user'
    core.log(f'request #{rid}: approved ->', json.dumps(res))
    return 'told'


def declined(rid, reason=''):
    """Tell the requester their request wasn't approved, with the reason if one was given (once)."""
    notifier()
    r = seerr_request(rid)
    who = r.get('requestedBy') or {}
    by = r.get('modifiedBy') or {}
    if by and by.get('id') == who.get('id'):
        return 'declined by the requester'
    if not claim(rid, 'declined'):
        return 'already told'
    media = r.get('media') or {}
    title, body = config.msg('request_declined', what=what_of(r, rid),
                             reason=(reason or '').strip()[:200] or config.msg('request_declined_no_reason'))
    res = N.send(who.get('jellyfinUserId'), title, body, f'request-{rid}', category='declined',
                 person=who.get('displayName'),
                 ids={'type': 'movie' if r.get('type') == 'movie' else 'show', 'tmdb': media.get('tmdbId'),
                      'tvdb': media.get('tvdbId')}) if who.get('jellyfinUserId') else 'no Jellyfin user'
    core.log(f'request #{rid}: declined ->', json.dumps(res))
    return 'told'


def watch_tick(today=None):
    """For films still waiting: if Radarr's date changed, send a short update; stop once it's out."""
    today = today or dt.datetime.now(config.tz()).date()
    for w in core.db().execute('select * from release_watch where done=0').fetchall():
        try:
            m = radarr_movie(w['tmdb'])
        except Exception as e:
            core.log('release watch: Radarr failed for', w['title'], repr(e)[:120])
            continue
        if not m or not not_out(m, today):
            core.db().execute('update release_watch set done=1 where request_id=?', (w['request_id'],))
            continue
        d, how = release_date(m)
        new = d.isoformat() if d else None
        if new == w['date']:
            continue
        core.db().execute('update release_watch set date=?, how=? where request_id=?', (new, how, w['request_id']))
        title = m['title']
        old = uk_date(dt.date.fromisoformat(w['date']), today) if w['date'] else ''
        key = 'release_date_changed' if d else 'release_date_removed'
        t, b = config.msg(key, title=title, date=uk_date(d, today) if d else '', old=old, how=how or 'digitally')
        res = N.send(w['jf_id'], t, b, f"request-{w['request_id']}", category='requests',
                     ids={'type': 'movie', 'tmdb': w['tmdb']}) if w['jf_id'] else None
        core.log(f"release watch: {m['title']} date {w['date']} -> {new}:", json.dumps(res))


def loop():
    while True:
        time.sleep(WATCH_EVERY)
        guard(watch_tick)


def start():
    init_db()
    threading.Thread(target=loop, daemon=True).start()
