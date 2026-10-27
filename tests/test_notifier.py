"""Tests for the notifier part (webhooks, episode alerts, state in the DB, issue records, the old-state
import). Jellyfin / Seerr / Sonarr are not contacted: the functions that would are replaced.

   docker run --rm ghcr.io/bjrb20/pusharr python tests/test_notifier.py
"""
import base64, json, os, sys, tempfile, time, urllib.error, urllib.request

tmp = tempfile.mkdtemp()
env_file = os.path.join(tmp, 'pusharr.env')
os.system(f'{sys.executable} tools/gen_vapid.py {env_file} >/dev/null')
open(os.path.join(tmp, 'config.yaml'), 'w').write("""
public_url: https://alerts.test
branding: {app_name: Flix Alerts, server_name: Flix}
jellyfin: {url: 'http://127.0.0.1:9', public_url: 'https://flix.test'}
server: {webhook_port: 0}
notifications: {batching: {import_wait: 1}, wait_for_ready: {enabled: false}}
integrations:
  seerr: {enabled: true, url: 'http://127.0.0.1:9'}
  wizarr: {enabled: true, database: /nonexistent}
""")
os.environ.update(DATA_DIR=tmp, CONFIG_DIR=tmp, ENV_FILE=env_file, JELLYFIN_API_KEY='k', INTERNAL_TOKEN='internal-secret',
                  WEBHOOK_USER='sonarr', WEBHOOK_PASSWORD='hook-secret', SEERR_API_KEY='x')
sys.path.insert(0, 'app')

import config, core, notifier, issues, approvals, server  # noqa: E402
notifier.real_find_series, notifier.real_find_movie = notifier.find_series, notifier.find_movie   # before the tests replace them
assert config.load() == []

ok_count = 0


def check(cond, what):
    global ok_count
    if not cond:
        raise AssertionError(what)
    ok_count += 1
    print('ok  ', what)


# ---------- state in the DB ----------
notifier.setup()
notifier.save_sent({'Alex|428433|S4E1', 'Alex|428433|S4E2', 'Robin|428433|S4E3'})
core.db().executemany('insert into seen_series(id) values(?)', [(1,), (2,), (3,)])
check(notifier.load_sent() == {'Alex|428433|S4E1', 'Alex|428433|S4E2', 'Robin|428433|S4E3'}, 'sent keys kept in the DB')

# ---------- wording (message templates) ----------
d1 = {'new': [(4, 4)], 'furthest': (4, 3), 'behind': 1}
check(notifier.message('Tracker', d1) == ('📺 Tracker S4E4 is ready on Flix', "You're up to S4E3."), 'single-episode wording')
d6 = {'new': [(4, i) for i in range(4, 10)], 'furthest': (4, 3), 'behind': 6}
check(notifier.message('Tracker', d6) == ('📺 6 new episodes of Tracker on Flix', "S4E4–S4E9. You're up to S4E3."), 'batch wording')
check(notifier.message('Tracker', dict(d1, behind=3))[1] == "You're up to S4E3, 3 episodes behind." and
      notifier.message('Tracker', dict(d1, furthest=None))[1] == "You haven't started it yet.", 'progress wording')

# ---------- send once (dedup in the DB) ----------
core.ensure_user('a' * 32, 'Alex', True)
calls = []
notifier.decide = lambda tvdb, tmdb, keys, now=None: ('Tracker', [
    {'user': 'Alex', 'seerr_id': 1, 'jf_id': 'a' * 32, 'reasons': ['caught up'], 'skip': None, 'item_id': 'e' * 32,
     'new': sorted(keys), 'furthest': (4, 3), 'behind': len(keys)}])
real_notify = core.notify
core.notify = lambda msg: calls.append(msg) or {'Alex': {'devices': 0, 'delivered': 0}}
name, rep = notifier.process(428433, 61818, 'Tracker', {(4, 1)})
check(rep[0]['skip'] == 'already notified' and not calls, 'an episode already sent is not sent again')
name, rep = notifier.process(428433, 61818, 'Tracker', {(4, 4)})
check(rep[0].get('sent') and calls[-1]['category'] == 'episodes' and calls[-1]['users'] == ['a' * 32]
      and calls[-1]['ids'] == {'type': 'show', 'tvdb': 428433, 'tmdb': 61818, 'season': 4, 'episode': 4}
      and calls[-1]['title'] == '📺 Tracker S4E4 is ready on Flix', 'new episode goes straight to web push')
name, rep = notifier.process(428433, 61818, 'Tracker', {(4, 4)})
check(rep[0]['skip'] == 'already notified' and len(calls) == 1, 'and only once')

# ---------- "new show" tracking ----------
check(notifier.seen_series_add(2) is False, 'series that already had files is not "new"')
check(notifier.seen_series_add(99) is True and notifier.seen_series_add(99) is False, 'first file of a series is "new", once')
check(99 in {r['id'] for r in core.db().execute('select id from seen_series')}, 'kept in the DB')

# ---------- webhooks on their own port ----------
srv = notifier.ThreadingHTTPServer(('127.0.0.1', 0), notifier.Handler)
notifier.threading.Thread(target=srv.serve_forever, daemon=True).start()
hook = f'http://127.0.0.1:{srv.server_port}'
AUTH = 'Basic ' + base64.b64encode(b'sonarr:hook-secret').decode()


def post(url, body, auth=AUTH):
    h = {'Content-Type': 'application/json'}
    if auth:
        h['Authorization'] = auth
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(), headers=h), timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


check(post(hook + '/sonarr', {'eventType': 'Test'}, auth=None)[0] == 401, 'webhook without auth refused')
check(post(hook + '/sonarr', {'eventType': 'Test'}, auth='Basic ' + base64.b64encode(b'sonarr:wrong').decode())[0] == 401,
      'webhook with the wrong password refused')
check(post(hook + '/sonarr', {'eventType': 'Test'}) == (200, 'ok'), 'Sonarr test button answered')
check(post(hook + '/radarr', {'eventType': 'Test'}) == (200, 'ok'), 'Radarr test button answered')
check(post(hook + '/seerr', {'notification_type': 'TEST_NOTIFICATION'}) == (200, 'ok'), 'Seerr test button answered')
req_ = urllib.request.Request(hook + '/wizarr', data='User newbie has joined your server! 🎉'.encode(),
                              headers={'Authorization': AUTH, 'Title': 'New User'})
with urllib.request.urlopen(req_, timeout=10) as r_:
    check(r_.status == 200 and r_.read() == b'checking', "Wizarr's ntfy agent (plain text) makes the new-account check run at once")
check(post(hook + '/wizarr', {}, auth=None)[0] == 401, '... with the webhook login')
check(post(hook + '/sonarr', {'eventType': 'Download', 'isUpgrade': True, 'series': {'title': 'Tracker'}})[1] == 'ignored upgrade',
      'upgrades ignored')
calls.clear()
st, msg = post(hook + '/sonarr', {'eventType': 'Download', 'series': {'id': 5, 'title': 'Tracker', 'tvdbId': 428433,
                                                                      'tmdbId': 61818, 'type': 'standard', 'path': '/x'},
                                  'episodes': [{'seasonNumber': 4, 'episodeNumber': 5}]})
check(st == 200 and msg.startswith('queued Tracker'), 'import queued for batching')
notifier.find_series = lambda tvdb, tmdb, path=None: None
for _ in range(80):        # the "new show" alert for this series can arrive first; wait for the episode one
    if any(c.get('title') == '📺 Tracker S4E5 is ready on Flix' for c in calls):
        break
    time.sleep(0.1)
check(any(c['title'] == '📺 Tracker S4E5 is ready on Flix' for c in calls), 'batched import sent after the window')
calls.clear()
notifier.find_movie = lambda tmdb, path=None: {'Id': 'm' * 32}
import libraries  # noqa: E402
libraries._cache.update(at=core.now() + 10 ** 8, libs=[      # Jellyfin's libraries, as if just read
    {'id': 'mov' + '0' * 29, 'name': 'Movies', 'type': 'movies', 'paths': ['/data/video/movie']},
    {'id': 'ani' + '0' * 29, 'name': 'Anime', 'type': 'tvshows', 'paths': ['/data/video/Anime']}])
check(post(hook + '/radarr', {'eventType': 'Download', 'isUpgrade': False,
                              'movie': {'title': 'Dune: Part Two', 'year': 2024, 'tmdbId': 693134,
                                        'folderPath': '/data/video/movie/Dune Part Two (2024)'},
                              'movieFile': {'path': '/data/video/movie/Dune Part Two (2024)/Dune.mkv'}})[0] == 200,
      'first-time film import accepted')
for _ in range(50):
    if calls:
        break
    time.sleep(0.1)
check(calls and calls[-1] == {'category': 'library', 'library': 'mov' + '0' * 29, 'title': 'Dune: Part Two (2024)', 'item_id': 'm' * 32,
                              'ids': {'type': 'movie', 'tmdb': 693134, 'item': 'm' * 32}},
      'new film announced in the library its folder belongs to (with its exact item)')
calls.clear()
notifier.send_episodes({'tvdb': 1, 'tmdb': 2, 'title': 'Frieren', 'keys': [[1, 1]], 'new_series': True,
                        'series_path': '/data/video/Anime/Frieren'}, {'series': 's' * 32})
check(calls and calls[0]['category'] == 'library' and calls[0]['library'] == 'ani' + '0' * 29,
      'a new show is classified by its folder (Anime library), not by series type or name')
check(post(hook + '/radarr', {'eventType': 'Download', 'isUpgrade': True, 'movie': {}})[1].startswith('ignored'),
      'film upgrades ignored')
core.notify = real_notify

# the public app port must not take webhooks
pub = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Public)
notifier.threading.Thread(target=pub.serve_forever, daemon=True).start()
check(post(f'http://127.0.0.1:{pub.server_port}/sonarr', {'eventType': 'Test'})[0] in (401, 404),
      'public port does not accept webhooks')

# ---------- issue records in the DB, answered through approvals ----------
db = issues.load()
db['20'] = {'issue_id': 20, 'kind': 'AUDIO', 'state': 'awaiting', 'reporter': 'Sam', 'comment': 'out of sync',
            'created': issues.iso(), 'target': {'title': 'Hercules (1997)', 'jf_item': 'h' * 32}, 'via': 'push', 'log': []}
db['21'] = dict(db['20'], issue_id=21, dry=True)
issues.save(db)
pend = approvals.notifier('GET', '/issues-pending')
check([p['issue_id'] for p in pend] == [20] and pend[0]['comment'] == 'out of sync', 'pending list from the DB (dry runs left out)')
check(approvals.notifier('POST', '/issue-action', {'issue_id': 20, 'action': 'ignore'}) == {'outcome': 'ignored', 'title': 'Hercules (1997)'},
      'Ignore answers the issue in-process')
check(issues.load()['20']['state'] == 'ignored' and approvals.notifier('POST', '/issue-action', {'issue_id': 20, 'action': 'approve'})['outcome']
      == 'already-ignored', 'answer stored; a second answer is refused')
check(approvals.notifier('POST', '/issue-action', {'issue_id': 999, 'action': 'ignore'})['outcome'] == 'unknown', 'unknown issue')

# ---------- the replacement picker ----------
def rel(title, q='WEBDL-1080p', langs=('English',), rej=(), seeders=50, size=4e9, weight=5, cf=0):
    return {'title': title, 'quality': {'quality': {'name': q}}, 'languages': [{'name': l} for l in langs],
            'rejections': list(rej), 'seeders': seeders, 'size': size, 'protocol': 'torrent', 'releaseWeight': weight,
            'customFormatScore': cf, 'guid': title, 'indexerId': 1}


SOFT = ['Existing file and the Quality profile does not allow upgrades',
        'Quality for release in queue already meets cutoff: HDTV-1080p v1']
A = lambda r, cur=(): issues.acceptable(r, 'radarr', set(cur), 'English')
for t in ('Spider-Man.Brand.New.Day.2026.WEB-DL.1080p.seleZen', 'Spider-Man Brand New Day 2026 REPACK 1080p 10bit WEBRip 6CH x265 HEVC-PSA',
          'Despicable.Me.2010.1080p.BluRay', 'Castaway.on.the.Moon.2009.1080p.WEB', 'The.Last.Spartan.2026.1080p.WEB'):
    check(A(rel(t)) == (True, None), f'not mistaken for a dub: {t}')
check(A(rel('Spider-Man.Brand.New.Day.2026.1080p.WEB-DL.DDP5.1.H.264-FLUX', rej=SOFT)) == (True, None),
      'English WEB-DL with only "existing file / in queue" notes is acceptable')
check(A(rel('Spider-man.brand.new.day.2026.1080p-dual-lat-cinecalidad', q='HDTV-1080p', langs=('Latvian',)))[0] is False,
      'the dual-lat cinecalidad release (parsed as Latvian) is rejected')
check(A(rel('Spider-man.brand.new.day.2026.1080p-dual-lat-cinecalidad', q='HDTV-1080p', langs=('English',))) ==
      (False, 'foreign dub (dual-lat)'), '... even when Radarr thinks it is English')
for t in ('Film.2026.1080p.WEB.LATINO.x264', 'Film.2026.1080p.WEB-DL.Castellano', 'Film 2026 1080p Latam WEB',
          'Spider-Man Brand New Day (2026) [Bluray 1080p][Esp]', 'Film.2026.1080p.WEB.SPA.ENG.x264',
          'Film.2026.1080p.WEB-DL.Dublado', 'Film 2026 1080p PT-BR'):
    check(A(rel(t))[1].startswith('foreign dub'), f'foreign dub rejected: {t}')
check(A(rel('Film.2026.MULTi.VF2.2160p.WEB', langs=('French',)))[0] is False, 'multi without English rejected')
check(A(rel('Film.2026.iTA-ENG.1080p.WEB-DL', langs=('Italian', 'English'))) == (True, None), 'Italian + English dual audio allowed')
for t, q in (('Film 2026 1080p HDTS DD2 0 H 264-LMNTRY', 'WEBDL-1080p'), ('Film.2026.HDCAM.x264', 'WEBDL-1080p'),
             ('Film.2026.1080p.TELESYNC', 'WEBDL-1080p'), ('Film.2026.1080p', 'TELESYNC'), ('Film.2026.HDTC.x264', 'WEBDL-1080p')):
    check(A(rel(t, q=q))[1].startswith('camera copy'), f'camera copy rejected: {t} ({q})')
check(A(rel('Film.2026.1080p.WEB.Sample'))[1] == 'sample' and A(rel('Film.2026.1080p.WEB', size=50e6))[1] == 'too small (sample?)',
      'samples rejected')
check(A(rel('Film.2026.1080p.WEB.HC.x264'))[1] == 'hard-coded subtitles' and A(rel('Film 2026 1080p ESub'))[1] == 'hard-coded subtitles',
      'hard-coded subtitles rejected')
check(A(rel('Film.2026.2160p.WEB', rej=['WEBDL-2160p is not wanted in profile']))[1] == 'WEBDL-2160p is not wanted in profile',
      'quality profile respected')
check(A(rel('Film.2026.1080p.WEB', rej=['Custom Formats Bad Group have score -1000 below minimum 0']))[0] is False,
      'custom-format minimum respected')
check(A(rel('Film.2026.1080p.WEB', seeders=0))[1] == 'no seeders', 'unseeded rejected')
check(A(rel('Film.2026.1080p.WEB-DL-GRP'), cur={issues.norm('Film.2026.1080p.WEB-DL-GRP.mkv')})[1] == 'the current release',
      'never the current release')
cands = [rel('B.many.seeders.1080p.HDTV', q='HDTV-1080p', weight=9, seeders=900),
         rel('A.radarr.favourite.1080p.WEB-DL', weight=2, seeders=40),
         rel('C.ita.eng.1080p.WEB-DL', langs=('Italian', 'English'), weight=1, seeders=300)]
check([r['title'] for r in sorted(cands, key=lambda r: issues.rank_key(r, 'English'))] ==
      ['A.radarr.favourite.1080p.WEB-DL', 'B.many.seeders.1080p.HDTV', 'C.ita.eng.1080p.WEB-DL'],
      "ranked by Radarr's own order (English-only first), not by seeders")
check(issues.check_queue_item({}, {'title': 'Spider-man.brand.new.day.2026.1080p-dual-lat-cinecalidad.my.mp4',
                                   'quality': {'quality': {'name': 'HDTV-1080p'}}}) == 'foreign dub (dual-lat)',
      'a grab whose real torrent name is bad gets caught in the queue')

# ---------- finding items Jellyfin hasn't identified yet (by folder) ----------
real_jf = notifier.jf
CAT = [{'Id': 'r' * 32, 'Name': 'Rivals (2024)', 'ProviderIds': {}, 'Path': '/data/video/TV show/Rivals (2024)'},
       {'Id': 'v' * 32, 'Name': 'Film', 'ProviderIds': {}, 'Path': '/data/video/movie/Film (2026)/Film.mkv'}]
notifier.jf = lambda path: {'Items': CAT}
check(notifier.real_find_series(429920, 208921, '/data/video/TV show/Rivals (2024)/')['Id'] == 'r' * 32
      and notifier.real_find_series(429920, 208921) is None, 'unidentified show found by its folder (Sonarr and Jellyfin share paths)')
check(notifier.real_find_movie(5, '/data/video/movie/Film (2026)/Film.mkv')['Id'] == 'v' * 32, 'unidentified film found by its file')
notifier.jf = real_jf

# ---------- holding alerts until Jellyfin has the content ----------
W = config.current()['notifications']['wait_for_ready']
W['enabled'] = True
core.db().execute('delete from pending_alerts')
released, asked = [], []
real_release, real_http = notifier.release, notifier.http
notifier.release = lambda kind, p, info: released.append((kind, p, info))
notifier.http = lambda method, url, body=None, headers=None, timeout=30: asked.append((method, url.split('/', 3)[-1], body)) or {}
JF = {'series': None, 'series_item': None, 'episodes': [], 'movie': None, 'movie_item': None}
notifier.find_series = lambda tvdb, tmdb, path=None: JF['series']
notifier.find_movie = lambda tmdb, path=None: JF['movie']
notifier.jf_item = lambda item_id: JF['series_item'] if JF['series'] and item_id == JF['series']['Id'] else JF['movie_item']
notifier.jf = lambda path: {'Items': JF['episodes']} if '/Episodes' in path else {'Items': []}
full = lambda **kw: dict({'Name': 'x', 'Overview': 'Something happens.', 'ImageTags': {'Primary': 'tag'}}, **kw)

t0 = core.now()
check(notifier.hold('episodes', 'series:7', {'tvdb': 1, 'tmdb': 2, 'title': 'Show', 'keys': [[1, 1]], 'new_series': True,
                                            'paths': ['/data/video/TV show/Show/Season 01/a.mkv']}) == 'held'
      and not released, 'import: the alert is held, nothing sent yet')
check(notifier.hold('episodes', 'series:7', {'tvdb': 1, 'tmdb': 2, 'title': 'Show', 'keys': [[1, 2]], 'paths': []}) == 'merged'
      and notifier.waiting()[0]['data']['keys'] == [[1, 1], [1, 2]], 'more episodes of the same show join the waiting alert')
row = notifier.waiting()[0]
check(row['next_check'] - row['created'] == 120, 'first look after check_every (2 min)')
notifier.ready_tick(t0 + 130)
check(not released and notifier.waiting()[0]['checks'] == 1, 'not in Jellyfin: still waiting')
notifier.ready_tick(t0 + 260)
notifier.ready_tick(t0 + 390)
notifier.ready_tick(t0 + 620)
r = notifier.waiting()[0]
check(r['refreshed'] == 1 and any(a[1] == 'Library/Media/Updated' and a[2]['Updates'][0]['Path'].endswith('a.mkv') for a in asked),
      'after refresh_after: Jellyfin is told about the new file')
check(r['next_check'] - (t0 + 620) == 240, 'and it backs off (2 min, then doubling)')
JF['series'] = {'Id': 's' * 32}
JF['series_item'] = full()
JF['episodes'] = [full(Id='e1' * 16, IndexNumber=1, SeriesId='s' * 32), dict(Id='e2' * 16, IndexNumber=2, Name='Episode 2', SeriesId='s' * 32)]
ok_, info, why = notifier.check('episodes', notifier.waiting()[0]['data'])
check(not ok_ and why == 'waiting for S1E2', 'an episode without overview / still image is not ready')
JF['episodes'][1] = full(Id='e2' * 16, IndexNumber=2, SeriesId='s' * 32)
JF['series_item'] = dict(full(), Overview='')
check(notifier.check('episodes', notifier.waiting()[0]['data'])[2] == 'series has no title / overview / poster yet',
      'a new show also waits for its own overview and poster')
JF['series_item'] = full()
notifier.ready_tick(t0 + 2000)
check(len(released) == 1 and released[0][0] == 'episodes' and released[0][2]['episodes'] == {'S1E1': 'e1' * 16, 'S1E2': 'e2' * 16}
      and not notifier.waiting(), 'ready: both episodes go out as one alert, with their Jellyfin ids')

released.clear(); asked.clear()
notifier.hold('movie', 'movie:9', {'title': 'Film (2026)', 'tmdb': 9, 'paths': []})
JF['movie'] = {'Id': 'f' * 32}
JF['movie_item'] = dict(full(), ImageTags={})
notifier.ready_tick(t0 + 10 ** 6 // 1000)
check(not released, 'film without a poster: waits')
notifier.ready_tick(core.now() + 700)
check(any('Items/' + 'f' * 32 + '/Refresh' in a[1] for a in asked), 'film found but incomplete: Jellyfin is asked to refresh it')
notifier.ready_tick(core.now() + 6 * 3600 + 5)
check(len(released) == 1 and released[0][2] == {'item': 'f' * 32} and not notifier.waiting(),
      'never complete: sent anyway after max_wait_hours (its page still deep-links)')

released.clear()
notifier.hold('request', 'request:5', {'payload': {}, 'title': 'Film', 'media_type': 'movie', 'tmdb': 9})
JF['movie_item'] = full()
notifier.ready_tick(core.now() + 200)
check(released and released[0][0] == 'request' and released[0][2] == {'item': 'f' * 32}, 'request ready waits for the film too')

W['enabled'] = False
released.clear()
notifier.hold('movie', 'movie:10', {'title': 'Other', 'tmdb': 10, 'paths': []})
time.sleep(0.3)
check(released and released[0][1]['title'] == 'Other' and not notifier.waiting(), 'switched off: sent straight away')
notifier.release, notifier.http = real_release, real_http

# ---------- approved films that aren't out yet ----------
import datetime as dt_  # noqa: E402
import releases  # noqa: E402
T = dt_.date(2026, 10, 27)
ODY = {'title': 'The Odyssey', 'year': 2026, 'tmdbId': 1368337, 'status': 'inCinemas', 'hasFile': False,
       'inCinemas': '2026-07-15T00:00:00Z', 'digitalRelease': '2026-11-17T00:00:00Z', 'physicalRelease': '2026-11-17T00:00:00Z'}
check(releases.message(ODY, T)[0] == ('🎬 The Odyssey is still in cinemas',
                                       "It's out digitally on 17 Nov, and Flix will grab it then. We'll let you know when it's ready."),
      'in cinemas, digital date known (UK date, no year when it is this year)')
check(releases.message(dict(ODY, digitalRelease='2027-02-03T00:00:00Z'), T)[0][1].startswith("It's out digitally on 3 Feb 2027"),
      'a date next year shows the year')
check(releases.message(dict(ODY, digitalRelease=None, physicalRelease='2026-12-01T00:00:00Z'), T)[0][1]
      .startswith("It's out on disc on 1 Dec"), 'no digital date: the disc date')
check(releases.message(dict(ODY, digitalRelease=None, physicalRelease=None), T)[0] == (
      '🎬 The Odyssey is still in cinemas', "It isn't out digitally yet. Flix will grab it as soon as it is, and we'll let you "
      "know when it's ready."), 'no date at all: says it is not out digitally yet')
check(releases.message(dict(ODY, status='announced', inCinemas='2026-12-20T00:00:00Z', digitalRelease='2027-03-01T00:00:00Z'), T)[0][0]
      == "🎬 The Odyssey isn't out yet", 'not in cinemas yet either')
check(releases.not_out(ODY, T) and not releases.not_out(dict(ODY, digitalRelease='2026-10-01T00:00:00Z'), T)
      and not releases.not_out(dict(ODY, hasFile=True), T), 'out digitally already, or downloaded: nothing to say')
sent_r, RAD = [], {'movie': None}
releases.seerr_request = lambda rid: {'type': 'movie', 'media': {'tmdbId': 1368337},
                                      'requestedBy': {'displayName': 'Sam', 'jellyfinUserId': 'b' * 32}}
releases.radarr_movie = lambda tmdb: RAD['movie']
real_send = notifier.send
notifier.send = lambda jf_id, title, body, tag, **kw: sent_r.append((jf_id, title, body, kw.get('category'))) or {'delivered': 1}
looks = []
releases.radarr_movie = lambda tmdb: looks.append(1) or (ODY if len(looks) >= 2 else None)
check(releases.check_request(3, tries=3, gap=0) == 'told' and len(looks) == 2 and sent_r[0][0] == 'b' * 32
      and sent_r[0][1:3] == ('✅ The Odyssey approved', "It's out digitally on 17 Nov, and Flix will grab it then. We'll let you "
                             "know when it's ready.") and sent_r[0][3] == 'approved',
      'approved, not out yet: ONE message (approved + when to expect it), after Seerr adds the film to Radarr')
check(releases.check_request(3, tries=1, gap=0) == 'already told' and len(sent_r) == 1, 'only once per request')
releases.seerr_request = lambda rid: {'type': 'movie', 'media': {'tmdbId': 1368337}, 'modifiedBy': {'id': 1, 'displayName': 'Alex'},
                                      'requestedBy': {'id': 1, 'displayName': 'Alex', 'jellyfinUserId': 'a' * 32}}
check(releases.check_request(30, tries=1, gap=0) == 'told' and sent_r[-1][1] == '🎬 The Odyssey is still in cinemas'
      and sent_r[-1][3] == 'requests', 'an admin who approved their own request: just the release date, no "approved"')
releases.seerr_request = lambda rid: {'type': 'movie', 'media': {'tmdbId': 1368337},
                                      'requestedBy': {'id': 2, 'displayName': 'Sam', 'jellyfinUserId': 'b' * 32}}
releases.radarr_movie = lambda tmdb: dict(ODY, digitalRelease='2026-10-01T00:00:00Z')
releases.seerr_request = lambda rid: {'type': 'movie', 'media': {'tmdbId': 1368337}, 'modifiedBy': {'id': 1, 'displayName': 'Ben'},
                                      'requestedBy': {'id': 2, 'displayName': 'Sam', 'jellyfinUserId': 'b' * 32}}
n0 = len(sent_r)
check(releases.check_request(4, tries=1, gap=0) == 'told' and sent_r[-1][1:4] == (
      '✅ Ben approved The Odyssey', "We'll let you know when it's ready on Flix.", 'approved'), 'film out already: "Ben approved The Odyssey"')
releases.seerr_request = lambda rid: {'type': 'tv', 'media': {'tmdbId': 1399, 'tvdbId': 121361}, 'seasons': [{'seasonNumber': 1}, {'seasonNumber': 2}],
                                      'modifiedBy': {'id': 1, 'displayName': 'Ben'}, 'requestedBy': {'id': 2, 'jellyfinUserId': 'b' * 32}}
releases_what = releases.what_of
releases.what_of = lambda r, rid=None: 'Game of Thrones seasons 1, 2'
check(releases.check_request(5, tries=1, gap=0) == 'told' and sent_r[-1][1] == '✅ Ben approved Game of Thrones seasons 1, 2',
      'a show: approved, with its seasons (no Radarr wait)')
check(releases.check_request(5, tries=1, gap=0) == 'already told', '... once')
check(releases.declined(6, 'Already on Netflix') == 'told' and sent_r[-1][1:4] == (
      "❌ Your request for Game of Thrones seasons 1, 2 wasn't approved", 'Already on Netflix', 'declined'), 'declined, with the reason')
check(releases.declined(6) == 'already told', "Seerr's decline webhook after a decline on the Pending page: not told twice")
check(releases.declined(7) == 'told' and sent_r[-1][2] == 'Ask an admin if you want to know why.', 'declined in Seerr: no reason there')
releases.seerr_request = lambda rid: {'type': 'tv', 'media': {}, 'modifiedBy': {'id': 2}, 'requestedBy': {'id': 2, 'jellyfinUserId': 'b' * 32}}
check(releases.check_request(8, tries=1, gap=0) == 'approved by the requester' and releases.declined(8) == 'declined by the requester',
      'their own approval / decline: no alert')
releases.what_of = releases_what
check(len(sent_r) == n0 + 4, 'exactly one alert per outcome')
releases.radarr_movie = lambda tmdb: ODY
del sent_r[1:]
releases.watch_tick(T)
check(len(sent_r) == 1, 'same date: no update')
releases.radarr_movie = lambda tmdb: dict(ODY, digitalRelease='2026-11-24T00:00:00Z')
releases.watch_tick(T)
check(sent_r[-1][1:3] == ('📅 New date for The Odyssey', "It's now out digitally on 24 Nov (was 17 Nov). We'll let you know when it's ready.")
      and {x[0] for x in sent_r[1:]} == {'a' * 32, 'b' * 32}, 'the date moves: a short update to each requester waiting for it')
releases.radarr_movie = lambda tmdb: dict(ODY, hasFile=True)
releases.watch_tick(T)
check(core.db().execute('select done from release_watch where request_id=3').fetchone()[0] == 1 and len(sent_r) == 3,
      'downloaded: the watch stops (the usual "ready" alert takes over)')
notifier.send = real_send
releases.seerr_request = lambda rid: {'type': 'tv', 'media': {'tmdbId': 1}, 'requestedBy': {}}
check(post(hook + '/seerr', {'notification_type': 'MEDIA_APPROVED', 'request_id': '99', 'media': {'media_type': 'movie'}})
      == (200, 'release check queued'), 'Seerr "request approved" webhook starts the check')

# ---------- picker rules come from the config ----------
check(A(rel('Film.2026.1080p.WEB.GERMAN.DL', langs=('German',)))[0] is False, 'German-only rejected while English is wanted')
config.current()['approvals']['picker']['languages'] = ['German']
config.current()['approvals']['picker']['allow_original_language'] = False
check(A(rel('Film.2026.1080p.WEB.GERMAN', langs=('German',))) == (True, None)
      and A(rel('Film.2026.1080p.WEB', langs=('English',)))[1] == 'no wanted audio language (English)',
      'wanted languages are configurable')
config.current()['approvals']['picker']['languages'] = ['English']
config.current()['approvals']['picker']['allow_original_language'] = True
config.current()['approvals']['picker']['dub_tags'] = ['zzdub']
check(A(rel('Film.2026.1080p.WEB.LATINO')) == (True, None) and A(rel('Film.2026.1080p.WEB.ZZDUB'))[1] == 'foreign dub (ZZDUB)',
      'rejected tags are configurable')
config.load()

# ---------- no Home Assistant left ----------
src = ''.join(open(os.path.join('app', f)).read() for f in os.listdir('app') if f.endswith('.py'))
check(not any(w in src for w in ('HA_TOKEN', 'HA_URL', 'HA_WEBHOOK', 'mobile_app', 'websocket', 'ISSUE_NOTIFY_HA')),
      'no Home Assistant code left')

print(f'\nall {ok_count} notifier checks passed')
