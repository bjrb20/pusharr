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
notifications: {batching: {import_wait: 1}}
integrations:
  seerr: {enabled: true, url: 'http://127.0.0.1:9'}
  wizarr: {enabled: true, database: /nonexistent}
""")
os.environ.update(DATA_DIR=tmp, CONFIG_DIR=tmp, ENV_FILE=env_file, JELLYFIN_API_KEY='k', INTERNAL_TOKEN='internal-secret',
                  WEBHOOK_USER='sonarr', WEBHOOK_PASSWORD='hook-secret', SEERR_API_KEY='x')
sys.path.insert(0, 'app')

import config, core, notifier, issues, approvals, server  # noqa: E402
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
      and calls[-1]['ids'] == {'type': 'show', 'tvdb': 428433, 'tmdb': 61818}
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
check(post(hook + '/sonarr', {'eventType': 'Download', 'isUpgrade': True, 'series': {'title': 'Tracker'}})[1] == 'ignored upgrade',
      'upgrades ignored')
calls.clear()
st, msg = post(hook + '/sonarr', {'eventType': 'Download', 'series': {'id': 5, 'title': 'Tracker', 'tvdbId': 428433,
                                                                      'tmdbId': 61818, 'type': 'standard', 'path': '/x'},
                                  'episodes': [{'seasonNumber': 4, 'episodeNumber': 5}]})
check(st == 200 and msg.startswith('queued Tracker'), 'import queued for batching')
notifier.find_series = lambda tvdb, tmdb: None
for _ in range(80):        # the "new show" alert for this series can arrive first; wait for the episode one
    if any(c.get('title') == '📺 Tracker S4E5 is ready on Flix' for c in calls):
        break
    time.sleep(0.1)
check(any(c['title'] == '📺 Tracker S4E5 is ready on Flix' for c in calls), 'batched import sent after the window')
calls.clear()
notifier.find_movie = lambda tmdb: {'Id': 'm' * 32}
check(post(hook + '/radarr', {'eventType': 'Download', 'isUpgrade': False,
                              'movie': {'title': 'Dune: Part Two', 'year': 2024, 'tmdbId': 693134}})[0] == 200,
      'first-time film import accepted')
for _ in range(50):
    if calls:
        break
    time.sleep(0.1)
check(calls and calls[-1] == {'category': 'library', 'kind': 'movies', 'title': 'Dune: Part Two (2024)', 'item_id': 'm' * 32,
                              'ids': {'type': 'movie', 'tmdb': 693134}}, 'new film announced')
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
