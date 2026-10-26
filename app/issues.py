"""Seerr "Report an Issue" handling, loaded by notifier.py (same service, same env file).

Seerr (Settings -> Notifications -> Webhook, type Issue Created) POSTs to /seerr. Then:

  VIDEO / AUDIO  -> ask the approvers (web push with Approve / Ignore, and the Pending page), valid
                    approvals.issue_expire_hours (24 h).
                    Approve = replace the copy with no gap: the current file stays until the
                    new one is imported over it.
  SUBTITLES      -> Bazarr: blocklist a bad Bazarr subtitle (it then fetches another), or search
                    for one; FYI to the approvers.
  OTHER          -> just tell the approvers.

Replacement (Radarr film, Sonarr episode or a whole season):
  1. If the current file came from a Radarr/Sonarr grab, mark that grab failed (history ->
     failed): the release is blocklisted and the file is left alone.
  2. Interactive search, then grab the best acceptable release (approvals.picker: Radarr/Sonarr's own
     rejections count except "a file exists"; wanted audio languages only; no dubs, camera copies,
     hard-coded subtitles or samples), in Radarr/Sonarr's own preference order, then seeders. The
     real torrent name is checked again once it's in the queue.
  3. Watch the queue. Radarr/Sonarr import a same-or-better-quality copy over the old file by
     themselves. If the import is blocked as "not an upgrade", run a Manual Import of just that
     download for just that film/episode, which replaces the old file.
  4. When the file id has changed: comment "Replaced with a new copy" on the Seerr issue, resolve
     it, and tell the reporter.

We never touch files ourselves: only Radarr/Sonarr/Bazarr do, and only inside the library. Every
step is logged and kept in the app's DB (issue_records).

An issue whose comment contains "[dry-run]" goes through the whole flow but only reports what it
would mark failed and grab.
"""
import datetime as dt, functools, json, os, re, secrets, threading, time, traceback
import urllib.parse, urllib.request

import approvals
import config
import core

N = None  # the notifier module (http, log, send), set by notifier.py

ISSUE_TYPES = {1: 'VIDEO', 2: 'AUDIO', 3: 'SUBTITLES', 4: 'OTHER'}
GIVE_UP_DAYS = 3
BLOCKED_GRACE_MIN = 5
# Interactive-search rejections that only say "you already have a file". Anything else is a real no.
SOFT_REJECTION = re.compile(r'existing file|cutoff|not an upgrade|does not allow upgrades|'
                            r'not a (custom format|quality revision) upgrade|upgrade', re.I)

_lock = threading.RLock()


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t=None):
    return (t or now()).isoformat(timespec='seconds')


def parse_iso(s):
    return dt.datetime.fromisoformat(s)


# ---------- clients ----------

def expire_hours():
    return config.get('approvals.issue_expire_hours')


def api(app, method, path, body=None):
    prefix = '/api/v1' if app == 'seerr' else '/api/v3'
    return N.http(method, N.url(app) + prefix + path, body,
                  headers={'X-Api-Key': config.secret(f'{app.upper()}_API_KEY', '')}, timeout=180)


def bazarr(method, path, form=None):
    data = urllib.parse.urlencode(form, doseq=True).encode() if form is not None else None
    req = urllib.request.Request(N.url('bazarr') + '/api' + path, data=data, method=method,
                                 headers={'X-API-KEY': config.secret('BAZARR_API_KEY', '')})
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read()
        return json.loads(raw) if raw and 'json' in r.headers.get('Content-Type', '') else raw


# ---------- state ----------

def load():
    """All issue records, {issue id: record}, from the app's DB."""
    return {r['id']: json.loads(r['data']) for r in core.db().execute('select id, data from issue_records')}


def save(db):
    core.db().executemany('insert into issue_records(id, data) values(?, ?) on conflict(id) do update set data=excluded.data',
                          [(str(k), json.dumps(v)) for k, v in db.items()])


def note(rec, *a):
    """Log a step for this issue, to the container log and to its record."""
    line = ' '.join(str(x) for x in a)
    N.log(f"issue #{rec['issue_id']}:", line)
    rec.setdefault('log', []).append(f'{iso()} {line}')


def update(_id, **kv):
    with _lock:
        db = load()
        db[str(_id)].update(kv)
        save(db)
        return db[str(_id)]


# ---------- notifications ----------

def tell_approver(title, message, tag, dry=False, event='info', rec=None):
    """Tell the approvers by web push (event: approval = Approve / Ignore buttons and the Pending page,
    info = plain, resolved = replaces the earlier one)."""
    issue_id = int(tag.rsplit('-', 1)[1])
    approvals.notify_issue({'issue_id': issue_id, 'event': event, 'push_title': title, 'message': message,
                            'dry': dry, 'kind': (rec or {}).get('kind'),
                            'title': ((rec or {}).get('target') or {}).get('title'),
                            'jf_item': ((rec or {}).get('target') or {}).get('jf_item')})
    return 'push'


def target_ids(t):
    """Catalogue ids of a Seerr issue/request target, for the note page's app and request buttons."""
    return {'type': 'movie' if t.get('media_type') == 'movie' else 'show', 'tmdb': t.get('tmdb'), 'tvdb': t.get('tvdb')}


def tell_person(name, title, message, tag, jf_id=None, category='issues', item_id=None, ids=None):
    res = N.send(jf_id, title, message, tag, category=category, item_id=item_id, ids=ids, person=name)
    return f'told {name}' + (f' {json.dumps(res)}' if isinstance(res, dict) and res else '')


def seerr_comment(rec, text):
    if rec.get('dry'):
        note(rec, 'would comment on the Seerr issue:', repr(text))
        return
    api('seerr', 'POST', f"/issue/{rec['issue_id']}/comment", {'message': text})
    note(rec, 'commented on the Seerr issue:', repr(text))


def seerr_resolve(rec):
    if rec.get('dry'):
        note(rec, 'would resolve the Seerr issue')
        return
    api('seerr', 'POST', f"/issue/{rec['issue_id']}/resolved")
    note(rec, 'resolved the Seerr issue')


# ---------- what the issue is about ----------

def describe(issue):
    """Work out the Radarr/Sonarr target of a Seerr issue."""
    media = issue.get('media') or {}
    t = {'media_type': media.get('mediaType'), 'tmdb': media.get('tmdbId'), 'tvdb': media.get('tvdbId'),
         'jf_item': media.get('jellyfinMediaId'),
         'season': issue.get('problemSeason') or 0, 'episode': issue.get('problemEpisode') or 0}
    if t['media_type'] == 'movie':
        m = api('radarr', 'GET', f"/movie?tmdbId={t['tmdb']}")
        if m:
            m = m[0]
            t.update(app='radarr', movie_id=m['id'], title=f"{m['title']} ({m['year']})", label=m['title'],
                     files={str(m['id']): m['movieFile']['id']} if m.get('movieFile') else {})
        return t
    s = api('sonarr', 'GET', f"/series?tvdbId={t['tvdb']}") if t['tvdb'] else []
    if not s and t['tmdb']:
        s = [x for x in api('sonarr', 'GET', '/series') if x.get('tmdbId') == t['tmdb']]
    if not s:
        return t
    s = s[0]
    t.update(app='sonarr', series_id=s['id'], label=s['title'], title=s['title'])
    eps = api('sonarr', 'GET', f"/episode?seriesId={s['id']}")
    if t['season'] and t['episode']:
        eps = [e for e in eps if e['seasonNumber'] == t['season'] and e['episodeNumber'] == t['episode']]
        t['title'] = f"{s['title']} S{t['season']}E{t['episode']}"
    elif t['season']:
        eps = [e for e in eps if e['seasonNumber'] == t['season']]
        t['title'] = f"{s['title']} season {t['season']}"
    else:
        eps = []        # whole series: too big to replace in one go
    t['episodes'] = [e['id'] for e in eps if e.get('hasFile')]
    t['files'] = {str(e['id']): e['episodeFileId'] for e in eps if e.get('hasFile')}
    return t


# ---------- webhook from Seerr ----------

def on_seerr(payload):
    kind = payload.get('notification_type')
    if kind == 'TEST_NOTIFICATION':
        N.log('Seerr test webhook received')
        return 'ok'
    if kind in ('MEDIA_PENDING', 'MEDIA_AUTO_APPROVED'):
        threading.Thread(target=guard, args=(request_event, payload, kind), daemon=True).start()
        return 'request event queued'
    if kind == 'MEDIA_AVAILABLE':
        media = payload.get('media') or {}
        rid = payload.get('request_id') or payload.get('request', {}).get('request_id')
        return N.hold('request', f'request:{rid}', {
            'payload': payload, 'title': payload.get('subject'), 'media_type': media.get('media_type'),
            'tmdb': media.get('tmdbId'), 'tvdb': media.get('tvdbId')})
    if kind != 'ISSUE_CREATED':
        return f'ignored {kind}'
    issue_id = int(payload.get('issue_id') or 0)
    threading.Thread(target=guard, args=(new_issue, issue_id), daemon=True).start()
    return f'issue {issue_id} queued'


def request_event(payload, kind):
    """New Seerr request: push it to admins (Approve / Decline when it's pending)."""
    rid = payload.get('request_id')
    if not rid:
        return
    res = approvals.notify_pending(int(rid), auto=kind == 'MEDIA_AUTO_APPROVED')
    N.log(f'request #{rid} {kind.lower()} ->', json.dumps(res))


def request_ready(payload, item_id=None):
    """Seerr says requested media is available (and Jellyfin has it): tell whoever requested it."""
    rid = payload.get('request_id')
    if not rid:
        return N.log('request ready without a request id, ignored')
    r = api('seerr', 'GET', f'/request/{rid}')
    media, who = r.get('media') or {}, r.get('requestedBy') or {}
    info = api('seerr', 'GET', f"/{'movie' if media.get('mediaType') == 'movie' else 'tv'}/{media.get('tmdbId')}")
    title = info.get('title') or info.get('name') or payload.get('subject') or 'Your request'
    year = (info.get('releaseDate') or info.get('firstAirDate') or '')[:4]
    seasons = [s['seasonNumber'] for s in r.get('seasons') or []]
    what = f"{title} ({year})" if year else title
    if r.get('type') == 'tv' and seasons:
        what += f" season{'s' if len(seasons) > 1 else ''} {', '.join(map(str, seasons))}"
    name = who.get('displayName') or who.get('jellyfinUsername') or '?'
    msg_title, msg_body = config.msg('request_ready', what=what, title=title, user=name)
    res = tell_person(name, msg_title, msg_body, f'request-{rid}', jf_id=who.get('jellyfinUserId'), category='requests',
                      ids={'type': 'movie' if media.get('mediaType') == 'movie' else 'show',
                           'tmdb': media.get('tmdbId'), 'tvdb': media.get('tvdbId'),
                           'item': item_id or media.get('jellyfinMediaId')},
                      item_id=item_id or media.get('jellyfinMediaId'))
    N.log(f'request #{rid} ready ({what}):', res)


def guard(fn, *a):
    try:
        fn(*a)
    except Exception:
        N.log('ERROR', fn.__name__, a, traceback.format_exc())


def new_issue(issue_id):
    issue = api('seerr', 'GET', f'/issue/{issue_id}')
    kind = ISSUE_TYPES.get(issue.get('issueType'), 'OTHER')
    who = (issue.get('createdBy') or {}).get('displayName') or '?'
    comments = issue.get('comments') or []
    comment = comments[0]['message'] if comments else ''
    t = describe(issue)
    with _lock:
        db = load()
        if str(issue_id) in db:
            return
        rec = db[str(issue_id)] = {'issue_id': issue_id, 'kind': kind, 'reporter': who, 'comment': comment,
                                   'reporter_jf': (issue.get('createdBy') or {}).get('jellyfinUserId'),
                                   'target': t, 'created': iso(), 'state': 'new',
                                   'dry': '[dry-run]' in comment.lower(), 'nonce': secrets.token_hex(8)}
        save(db)
    title = t.get('title') or f"tmdb {t['tmdb']}"
    test = '[TEST] ' if rec['dry'] else ''
    note(rec, f'{kind} issue on {title} from {who}: {comment!r}' + (' (dry run)' if rec['dry'] else ''))
    tag = f'arr-issue-{issue_id}'
    said = f'{who}: “{comment}”' if comment else f'reported by {who}'

    if kind in ('VIDEO', 'AUDIO'):
        if not t.get('app') or not t.get('files'):
            why = ('whole-series issues need a season or episode' if t.get('app') == 'sonarr' and not t['season']
                   else 'not in Radarr/Sonarr with a file')
            note(rec, 'cannot replace:', why)
            update(issue_id, state='unsupported', log=rec['log'])
            t_, b_ = config.msg('issue_unsupported', kind=kind.title(), title=title, said=said, why=why, user=who)
            tell_approver(test + t_, b_, tag, dry=rec['dry'], rec=rec)
            return
        t_, b_ = config.msg('issue_approval', kind=kind.title(), title=title, said=said, user=who)
        tell_approver(test + t_, b_, tag, dry=rec['dry'], event='approval', rec=rec)
        note(rec, 'asked the approvers (expires in', expire_hours(), 'h)')
        update(issue_id, state='awaiting', via='push', log=rec['log'])
    elif kind == 'SUBTITLES':
        update(issue_id, state='subtitles', log=rec['log'])
        result = subtitles(rec)
        t_, b_ = config.msg('issue_subtitles', title=title, said=said, result=result, user=who)
        tell_approver(test + t_, b_, tag, dry=rec['dry'], rec=rec)
    else:
        update(issue_id, state='told', log=rec['log'])
        t_, b_ = config.msg('issue_other', title=title, said=said, user=who)
        tell_approver(test + t_, b_, tag, dry=rec['dry'], rec=rec)
        note(rec, 'told the approvers (OTHER: no automatic action)')


# ---------- approvals ----------

def answer(issue_id, verb, where):
    """Approve / Ignore an issue. Returns (outcome, title): approved, ignored, expired, unknown or
    already-<state>."""
    with _lock:
        db = load()
        rec = db.get(str(issue_id))
        if not rec:
            N.log(f'{verb} for unknown issue #{issue_id} from {where}, ignored')
            return 'unknown', None
        title = (rec.get('target') or {}).get('title')
        if rec['state'] != 'awaiting':
            note(rec, f'{verb} from {where} but state is {rec["state"]}, ignored')
            save(db)
            return f"already-{rec['state']}", title
        if now() - parse_iso(rec['created']) > dt.timedelta(hours=expire_hours()):
            rec['state'] = 'expired'
            note(rec, f'{verb} from {where} after {expire_hours()} h, ignored')
            save(db)
            return 'expired', title
        rec['state'] = 'approved' if verb == 'approve' else 'ignored'
        note(rec, f'approver chose {verb.title()} ({where})')
        save(db)
    if verb == 'approve':
        threading.Thread(target=guard, args=(start_replacement, rec['issue_id']), daemon=True).start()
    return rec['state'], title


def pending_list():
    """Issues waiting for Approve / Ignore, for the Pending page."""
    out = []
    for r in load().values():
        if r['state'] == 'awaiting' and not r.get('dry') and \
                now() - parse_iso(r['created']) <= dt.timedelta(hours=expire_hours()):
            t = r.get('target') or {}
            out.append({'issue_id': r['issue_id'], 'kind': r['kind'], 'title': t.get('title'),
                        'reporter': r['reporter'], 'comment': r.get('comment'), 'created': r['created'],
                        'jf_item': t.get('jf_item')})
    return sorted(out, key=lambda x: x['created'], reverse=True)


# ---------- replacing ----------

def history(app, t):
    if app == 'radarr':
        return api('radarr', 'GET', f"/history/movie?movieId={t['movie_id']}")
    return api('sonarr', 'GET', f"/history/series?seriesId={t['series_id']}")


def current_files(t):
    if t['app'] == 'radarr':
        m = api('radarr', 'GET', f"/movie/{t['movie_id']}")
        f = m.get('movieFile')
        return {str(m['id']): f['id']} if f else {}, ([f] if f else [])
    out, files = {}, {}
    for eid in t['episodes']:
        e = api('sonarr', 'GET', f'/episode/{eid}')
        if e.get('hasFile'):
            out[str(eid)] = e['episodeFileId']
    for fid in set(out.values()):
        files[fid] = api('sonarr', 'GET', f'/episodefile/{fid}')
    return out, list(files.values())


def norm(s):
    """Release/file name reduced for comparison. Only real video extensions are dropped: release titles
    are full of dots (Film.2026.1080p.WEB-DL-GRP), so os.path.splitext would cut them short."""
    s = os.path.basename(s or '')
    s = re.sub(r'\.(mkv|mp4|avi|m4v|ts|wmv|mov)$', '', s, flags=re.I)
    s = re.sub(r'\[[^\]]*\]$', '', s)
    return re.sub(r'[^a-z0-9]', '', s.lower())


# Release-name rules from approvals.picker: tags that must never replace a copy, whatever Radarr/Sonarr
# parsed. Each tag is a regex matched as a whole word.
_W = r'(?<![a-z0-9])({})(?![a-z0-9])'
CAM_QUALITIES = {'CAM', 'TELESYNC', 'TELECINE', 'WORKPRINT', 'DVDSCR', 'REGIONAL'}


@functools.lru_cache(maxsize=32)
def _tags(tags):
    return re.compile(_W.format('|'.join(tags)), re.I) if tags else re.compile(r'(?!x)x')


def tags(kind):
    """Compiled pattern for approvals.picker.<kind>: dub_tags (foreign dubs), other_language_tags (fine
    only with a wanted language too), language_tags (a wanted language), hardsub_tags, camera_tags."""
    return _tags(tuple(str(t) for t in config.get(f'approvals.picker.{kind}')))


def wanted_languages(orig_lang=None):
    p = config.get('approvals.picker')
    return set(p['languages']) | ({orig_lang} if orig_lang and p['allow_original_language'] else set())


def min_size(app):
    return config.get('approvals.picker.min_size_mb')['movie' if app == 'radarr' else 'episode'] * 2**20


def bad_name(name, langs=(), orig_lang=None):
    """Why a release name is unacceptable regardless of what Radarr/Sonarr parsed, or None."""
    n = name or ''
    if re.search(r'(?<![a-z])sample(?![a-z])', n, re.I):
        return 'sample'
    if tags('dub_tags').search(n):
        return f"foreign dub ({tags('dub_tags').search(n).group(1)})"
    if tags('hardsub_tags').search(n):
        return 'hard-coded subtitles'
    if tags('camera_tags').search(n):
        return f"camera copy ({tags('camera_tags').search(n).group(1)})"
    other = tags('other_language_tags').search(n)
    if other and not (tags('language_tags').search(n) or set(langs) & wanted_languages(orig_lang)):
        return f'no wanted audio language ({other.group(1)})'
    return None


def acceptable(r, app, cur_names=(), orig_lang=None):
    """(True, None) if release `r` (from Radarr/Sonarr's interactive search) may replace the current copy,
    else (False, reason). Radarr/Sonarr's own rejections count (quality profile, custom formats,
    language, size...) except the ones that only say a file exists / is in the queue."""
    hard = [x for x in r.get('rejections', []) if not SOFT_REJECTION.search(x)]
    if hard:
        return False, hard[0]
    if r.get('downloadAllowed') is False:
        return False, 'download not allowed'
    if norm(r.get('title')) in cur_names:
        return False, 'the current release'
    if r.get('protocol') == 'torrent' and not (r.get('seeders') or 0):
        return False, 'no seeders'
    if (r.get('size') or 0) and r['size'] < min_size(app):
        return False, 'too small (sample?)'
    q = (r.get('quality') or {}).get('quality', {}).get('name', '')
    if q in CAM_QUALITIES:
        return False, f'camera copy ({q})'
    langs = {x.get('name') for x in r.get('languages') or []}
    wanted = wanted_languages(orig_lang)
    if langs - wanted - {'Unknown'} and not langs & wanted:
        return False, f"no wanted audio language ({', '.join(sorted(langs))})"
    why = bad_name(r.get('title'), langs, orig_lang)
    if why:
        return False, why
    return True, None


def rank_key(r, orig_lang=None):
    """Radarr/Sonarr's own preference order (releaseWeight: quality, custom formats, ...), releases with
    only wanted languages ahead of multi-language ones, then the best-seeded."""
    langs = {x.get('name') for x in r.get('languages') or []}
    pure = langs <= wanted_languages(orig_lang) | {'Unknown'}
    return (0 if pure else 1, r.get('releaseWeight', 999), -(r.get('seeders') or 0))


def pick_release(rec, files):
    t = rec['target']
    if t['app'] == 'radarr':
        rels = api('radarr', 'GET', f"/release?movieId={t['movie_id']}")
    elif t['episode']:
        rels = api('sonarr', 'GET', f"/release?episodeId={t['episodes'][0]}")
        rels = [r for r in rels if not r.get('fullSeason') and (r.get('mappedEpisodeNumbers') or r.get('episodeNumbers')) == [t['episode']]]
    else:
        rels = api('sonarr', 'GET', f"/release?seriesId={t['series_id']}&seasonNumber={t['season']}")
        rels = [r for r in rels if r.get('fullSeason')]
    cur_names = {norm(f.get('sceneName')) for f in files} | {norm(f.get('relativePath') or f.get('path')) for f in files}
    cur_names.discard('')
    item = api(t['app'], 'GET', f"/movie/{t['movie_id']}" if t['app'] == 'radarr' else f"/series/{t['series_id']}")
    orig_lang = (item.get('originalLanguage') or {}).get('name')
    ok, why = [], {}
    for r in rels:
        good, reason = acceptable(r, t['app'], cur_names, orig_lang)
        if good:
            ok.append(r)
        else:
            why[reason] = why.get(reason, 0) + 1
    ok.sort(key=lambda r: rank_key(r, orig_lang))
    rec['_rejected'] = sorted(why.items(), key=lambda x: -x[1])[:6]
    return ok, len(rels)


_qw_cache = {}


def quality_weight(rec, quality_id):
    """Weight of the current file's quality in the item's quality profile (same scale as qualityWeight)."""
    t = rec['target']
    if t['app'] == 'radarr':
        prof = api('radarr', 'GET', f"/movie/{t['movie_id']}")['qualityProfileId']
    else:
        prof = api('sonarr', 'GET', f"/series/{t['series_id']}")['qualityProfileId']
    key = (t['app'], prof)
    if key not in _qw_cache:
        # qualityWeight = index of the quality's top-level item in the profile * 100 (+ revision bits)
        items = api(t['app'], 'GET', f'/qualityprofile/{prof}')['items']
        _qw_cache[key] = {q['quality']['id']: i * 100 for i, it in enumerate(items) for q in (it.get('items') or [it])}
    return _qw_cache[key].get(quality_id, 0)


def mark_grab_failed(rec, files):
    """Blocklist the release(s) that produced the current file(s), if Radarr/Sonarr grabbed them."""
    t = rec['target']
    h = history(t['app'], t)
    paths = {f['path'] for f in files if f.get('path')}
    dl_ids = set()
    for e in h:
        if e['eventType'] in ('downloadFolderImported', 'movieFolderImported') and e.get('downloadId'):
            if e.get('data', {}).get('importedPath') in paths:
                dl_ids.add(e['downloadId'])
    grabs = {e['downloadId']: e for e in h if e['eventType'] == 'grabbed' and e.get('downloadId') in dl_ids}
    if not grabs:
        note(rec, 'current file was not grabbed by', t['app'], '(e.g. library import): nothing to blocklist; '
             'its release will be excluded by name instead')
        return []
    for d, g in grabs.items():
        if rec.get('dry'):
            note(rec, f"would mark grab failed (blocklist, file kept): {g['sourceTitle']} [history {g['id']}]")
        else:
            api(t['app'], 'POST', f"/history/failed/{g['id']}")
            note(rec, f"marked grab failed (blocklisted, file kept): {g['sourceTitle']} [history {g['id']}]")
    return list(grabs)


def queue_for(t):
    if t['app'] == 'radarr':
        q = api('radarr', 'GET', f"/queue?movieIds={t['movie_id']}&pageSize=50")
    else:
        q = api('sonarr', 'GET', f"/queue?seriesIds={t['series_id']}&pageSize=200&includeEpisode=false")
    recs = q.get('records', []) if isinstance(q, dict) else q
    if t['app'] == 'sonarr':
        recs = [r for r in recs if r.get('episodeId') in t['episodes']]
    return recs


def start_replacement(issue_id):
    rec = load()[str(issue_id)]
    t = rec['target']
    try:
        ids, files = current_files(t)
        if not files:
            note(rec, 'no current file any more; nothing to replace')
            update(issue_id, state='failed', log=rec['log'])
            return
        rec['orig_files'] = ids
        note(rec, 'current file(s):', '; '.join(f.get('relativePath', '?') for f in files))
        if not rec.get('retry_of'):
            mark_grab_failed(rec, files)
        if not rec.get('dry'):
            time.sleep(20)          # a failed-grab may make Radarr/Sonarr search and grab by themselves
            q = [x for x in queue_for(t) if not check_queue_item(rec, x)]
            if q:
                note(rec, 'Radarr/Sonarr already grabbed a replacement:', q[0].get('title'))
                rec.update(state='downloading', download_ids=[x.get('downloadId') for x in q],
                           grabbed=iso(), release=q[0].get('title'))
                update(issue_id, **rec)
                return
        ok, total = pick_release(rec, files)
        note(rec, f'interactive search: {total} releases, {len(ok)} acceptable')
        for reason, n in rec.pop('_rejected', []):
            note(rec, f'  rejected {n}: {reason}')
        for r in ok[:3]:
            note(rec, f"  candidate: {r['title']} | {r['quality']['quality']['name']} | "
                      f"{r.get('seeders')} seeders | {r.get('indexer')}")
        if not ok:
            no_copy_yet(rec)
            return
        best = ok[0]
        if rec.get('dry'):
            note(rec, f"would grab: {best['title']} (current file would stay until it's imported)")
            rec['state'] = 'dry-run-done'
            update(issue_id, **rec)
            seerr_comment(rec, 'Replaced with a new copy')
            seerr_resolve(rec)
            return
        for best in ok[:3]:         # the real torrent name can differ from the indexer's title: check it
            before = {q.get('id') for q in queue_for(t)}
            api(t['app'], 'POST', '/release', {'guid': best['guid'], 'indexerId': best['indexerId']})
            note(rec, f"grabbed: {best['title']}")
            if grab_checks_out(rec, before):
                rec.update(state='downloading', grabbed=iso(), release=best['title'], download_ids=[])
                update(issue_id, **rec)
                return
        no_copy_yet(rec)
    except Exception:
        note(rec, 'ERROR', traceback.format_exc()[-500:])
        update(issue_id, state='failed', log=rec['log'])
        tell_approver(*config.msg('issue_failed', title=t.get('title')), f'arr-issue-{issue_id}', rec=rec)


RETRY_HOURS, RETRY_DAYS = 6, 14


def no_copy_yet(rec):
    """Nothing acceptable to grab: leave the current file and the issue as they are, tell the approver,
    and look again every RETRY_HOURS for up to RETRY_DAYS."""
    t = rec['target']
    first = rec.get('no_copy_since') or iso()
    gave_up = now() - parse_iso(first) > dt.timedelta(days=RETRY_DAYS)
    note(rec, 'no acceptable copy to grab' + (f'; giving up after {RETRY_DAYS} days' if gave_up else
                                              f'; looking again in {RETRY_HOURS} h'))
    if rec.get('dry'):
        rec['state'] = 'dry-run-done'
    else:
        rec.update(state='failed' if gave_up else 'no-copy', no_copy_since=first,
                   retry_at=(now() + dt.timedelta(hours=RETRY_HOURS)).isoformat(timespec='seconds'))
    update(rec['issue_id'], **rec)
    if not rec.get('dry') and not rec.get('retry_of'):
        langs = ' or '.join(sorted(config.get('approvals.picker.languages')))
        rules = f'{langs} audio, no camera copies, within the quality profile'
        tell_approver(*config.msg('issue_no_copy', title=t['title'], rules=rules, hours=RETRY_HOURS, days=RETRY_DAYS),
                      f"arr-issue-{rec['issue_id']}", rec=rec)


def check_queue_item(rec, q):
    """Why a queued download is unacceptable (by its real name), or None."""
    item_langs = {x.get('name') for x in q.get('languages') or []}
    return bad_name(q.get('title'), item_langs, None) or (
        f"camera copy ({q['quality']['quality']['name']})"
        if (q.get('quality') or {}).get('quality', {}).get('name') in CAM_QUALITIES else None)


def remove_from_queue(rec, q, why):
    """Remove a bad download: out of the download client (with its data) and onto the blocklist."""
    t = rec['target']
    api(t['app'], 'DELETE', f"/queue/{q['id']}?removeFromClient=true&blocklist=true&skipRedownload=true")
    note(rec, f"removed and blocklisted {q.get('title')!r} ({why})")


def grab_checks_out(rec, before, wait=60):
    """After a grab, find the new download in the queue (ids not in `before`) and check the torrent's
    real name. A bad one is removed and blocklisted; returns whether the grab is fine."""
    t = rec['target']
    for _ in range(wait // 5):
        time.sleep(5)
        new = [q for q in queue_for(t) if q.get('id') not in before]
        if new:
            for q in new:
                why = check_queue_item(rec, q)
                if why:
                    remove_from_queue(rec, q, why)
                    return False
            return True
    note(rec, f"grab not seen in the queue after {wait}s; keeping it")
    return True


def retry_no_copy():
    for rec in list(load().values()):
        if rec.get('state') == 'no-copy' and rec.get('retry_at') and now() >= parse_iso(rec['retry_at']):
            note(rec, 'looking again for a better copy')
            update(rec['issue_id'], state='approved', retry_of=rec.get('retry_of', 0) + 1, log=rec['log'])
            guard(start_replacement, rec['issue_id'])


def manual_import(rec, q):
    """Import a download that was blocked as 'not an upgrade', for just this item."""
    t = rec['target']
    did = q.get('downloadId')
    items = api(t['app'], 'GET', f'/manualimport?downloadId={did}&filterExistingFiles=false')
    files = []
    for it in items:
        hard = [x for x in (it.get('rejections') or [])
                if not SOFT_REJECTION.search(x if isinstance(x, str) else x.get('reason', ''))]
        if hard:
            continue
        f = {'path': it['path'], 'quality': it['quality'], 'languages': it.get('languages', []),
             'releaseGroup': it.get('releaseGroup'), 'downloadId': did, 'indexerFlags': it.get('indexerFlags', 0)}
        if t['app'] == 'radarr':
            if (it.get('movie') or {}).get('id') != t['movie_id']:
                continue
            f['movieId'] = t['movie_id']
        else:
            eids = [e['id'] for e in it.get('episodes', []) if e['id'] in t['episodes']]
            if not eids or (it.get('series') or {}).get('id') != t['series_id']:
                continue
            f.update(seriesId=t['series_id'], episodeIds=eids, releaseType=it.get('releaseType', 'unknown'))
        files.append(f)
    if not files:
        note(rec, 'manual import: nothing importable in', q.get('title'), [it.get('rejections') for it in items][:3])
        return False
    api(t['app'], 'POST', '/command', {'name': 'ManualImport', 'importMode': 'auto', 'files': files})
    note(rec, f'manual import of {len(files)} file(s) from {q.get("title")} (replaces the old file)')
    return True


def check_progress(rec):
    t = rec['target']
    ids, _ = current_files(t)
    orig = rec.get('orig_files', {})
    replaced = [k for k in orig if ids.get(k) and ids[k] != orig[k]]
    if orig and len(replaced) == len(orig):
        note(rec, 'new copy imported (file id changed)')
        rec['state'] = 'done'
        update(rec['issue_id'], **rec)
        seerr_comment(rec, 'Replaced with a new copy')
        seerr_resolve(rec)
        note(rec, tell_person(rec['reporter'], *config.msg('issue_fixed', title=t['title']), f"arr-issue-{rec['issue_id']}",
                              jf_id=rec.get('reporter_jf'), item_id=t.get('jf_item'), ids=target_ids(t)))
        reporter = core.get_user(rec.get('reporter_jf') or '')
        if not (reporter and core.is_approver(reporter)):
            tell_approver(*config.msg('issue_replaced', title=t['title'], release=rec.get('release')),
                          f"arr-issue-{rec['issue_id']}", rec=rec)
        update(rec['issue_id'], **rec)
        return
    q = queue_for(t)
    for item in q:
        state = item.get('trackedDownloadState')
        msgs = ' '.join(m for s in item.get('statusMessages', []) for m in s.get('messages', []))
        if state in ('importBlocked', 'importPending') or 'upgrade' in msgs.lower():
            first = rec.setdefault('blocked_since', iso())
            if (now() - parse_iso(first) > dt.timedelta(minutes=BLOCKED_GRACE_MIN)
                    and item.get('downloadId') not in rec.setdefault('manual_imported', [])):
                note(rec, 'import blocked:', msgs[:200] or state)
                if manual_import(rec, item):
                    rec['manual_imported'].append(item.get('downloadId'))
        elif state == 'failed' or item.get('status') == 'failed':
            note(rec, 'download failed:', item.get('title'), msgs[:200])
    if now() - parse_iso(rec['grabbed']) > dt.timedelta(days=GIVE_UP_DAYS):
        note(rec, f'no new copy after {GIVE_UP_DAYS} days, giving up (current file unchanged)')
        rec['state'] = 'failed'
        tell_approver(*config.msg('issue_stalled', title=t['title'], days=GIVE_UP_DAYS), f"arr-issue-{rec['issue_id']}",
                      rec=rec)
    update(rec['issue_id'], **rec)


def catch_up():
    """Pick up issues whose webhook was missed (e.g. the service was restarting)."""
    known = load()
    for i in api('seerr', 'GET', '/issue?take=50&skip=0&sort=added&filter=open').get('results', []):
        age = now() - dt.datetime.fromisoformat(i['createdAt'].replace('Z', '+00:00'))
        if str(i['id']) not in known and age < dt.timedelta(hours=expire_hours()) and age > dt.timedelta(seconds=60):
            N.log(f"issue #{i['id']}: webhook was missed, picking it up now")
            guard(new_issue, i['id'])


def worker():
    n = 0
    while True:
        if n % 5 == 0:
            guard(catch_up)
        n += 1
        try:
            for rec in list(load().values()):
                if rec['state'] == 'awaiting' and now() - parse_iso(rec['created']) > dt.timedelta(hours=expire_hours()):
                    note(rec, f'no answer within {expire_hours()} h: expired')
                    update(rec['issue_id'], state='expired', log=rec['log'])
                    tell_approver(*config.msg('issue_expired', title=(rec.get('target') or {}).get('title'),
                                              hours=expire_hours()), f"arr-issue-{rec['issue_id']}",
                                  event='resolved', dry=rec.get('dry'), rec=rec)
                elif rec['state'] == 'downloading':
                    guard(check_progress, rec)
            if n % 10 == 0:
                guard(retry_no_copy)
        except Exception:
            N.log('ERROR issue worker', traceback.format_exc()[-500:])
        time.sleep(60)


# ---------- subtitles (Bazarr) ----------

def subtitles(rec):
    t = rec['target']
    dry = rec.get('dry')
    b = config.get('integrations.bazarr')
    lang, lname = b['language'], b['language_name']
    if not b['enabled']:
        note(rec, 'subtitles: Bazarr is switched off')
        return 'Bazarr is switched off; have a look yourself.'
    try:
        if t.get('app') == 'radarr':
            item = bazarr('GET', f"/movies?radarrid[]={t['movie_id']}")['data']
            base = {'radarrid': t['movie_id']}
            kind, hist = 'movies', f"/movies/history?radarrid={t['movie_id']}"
        elif t.get('app') == 'sonarr' and t['episode'] and t['episodes']:
            item = bazarr('GET', f"/episodes?episodeid[]={t['episodes'][0]}")['data']
            base = {'seriesid': t['series_id'], 'episodeid': t['episodes'][0]}
            kind, hist = 'episodes', f"/episodes/history?episodeid={t['episodes'][0]}"
        else:
            note(rec, 'subtitles: needs a film or a single episode that Bazarr knows')
            return "Couldn't hand it to Bazarr (needs a film or one episode). Have a look in Bazarr."
        if not item:
            note(rec, 'subtitles: Bazarr does not know this item yet')
            return 'Bazarr hasn\'t synced this one yet; nothing done.'
        item = item[0]
        missing = [s for s in item.get('missing_subtitles', []) if s.get('code2') == lang]
        if missing:
            if not dry:
                bazarr('PATCH', f"/{kind}?" + urllib.parse.urlencode({**base, 'action': 'search-missing'}), {})
            note(rec, ('would ask' if dry else 'asked'), f'Bazarr to search for missing {lname} subtitles')
            return f'{lname} subtitles were missing; Bazarr is searching for them.'
        # Subtitles in that language exist. If Bazarr downloaded them, blocklist that one: Bazarr deletes it and
        # fetches the next best. Otherwise (embedded or from the release) download the best external one.
        h = bazarr('GET', hist).get('data', [])
        mine = [x for x in h if x.get('provider') and x.get('subs_id') and x.get('language', {}).get('code2') == lang
                and x.get('subtitles_path') and not x.get('blacklisted')]
        if mine:
            x = mine[0]
            form = {**base, 'provider': x['provider'], 'subs_id': x['subs_id'], 'language': lang,
                    'subtitles_path': x['subtitles_path']}
            if not dry:
                bazarr('POST', f"/{kind}/blacklist?" + urllib.parse.urlencode(base), form)
            note(rec, ('would blocklist' if dry else 'blocklisted'), f"Bazarr subtitle from {x['provider']}; Bazarr "
                 'deletes it and fetches the next best')
            return f"Blocklisted the {x['provider']} subtitle; Bazarr is fetching another."
        found = bazarr('GET', f"/providers/{kind}?" + urllib.parse.urlencode(base)).get('data', [])
        # Bazarr sends its booleans as "True"/"False" strings. Skip embeddedsubtitles: the embedded
        # track is what's there already and may be the bad one.
        found = [s for s in found if s.get('language') == lang and str(s.get('forced')).lower() != 'true'
                 and s.get('provider') != 'embeddedsubtitles']
        found.sort(key=lambda s: -float(s.get('score') or 0))
        if not found:
            note(rec, f'subtitles: no {lname} subtitles found by the providers')
            return f'No other {lname} subtitles found by the free providers.'
        s = found[0]
        if not dry:
            bazarr('POST', f"/providers/{kind}?" + urllib.parse.urlencode(base),
                   {**base, 'hi': str(s.get('hearing_impaired', 'False')), 'forced': 'False',
                    'original_format': str(s.get('original_format', 'False')), 'provider': s['provider'],
                    'subtitle': s['subtitle']})
        note(rec, ('would download' if dry else 'downloaded'),
             f"{lname} subtitle from {s['provider']} (score {s.get('score')}): {s.get('release_info')}"[:300])
        if not dry:
            seerr_comment(rec, f"Downloaded new {lname} subtitles ({s['provider']}).")
            seerr_resolve(rec)
            note(rec, tell_person(rec['reporter'], *config.msg('subtitles_fixed', title=t['title']),
                                  f"arr-issue-{rec['issue_id']}", jf_id=rec.get('reporter_jf'), item_id=t.get('jf_item'),
                                  ids=target_ids(t)))
        return f"Downloaded new {lname} subtitles from {s['provider']}."
    except Exception:
        note(rec, 'subtitles ERROR', traceback.format_exc()[-400:])
        return f"Handing it to Bazarr failed; see the {config.get('branding.app_name')} log."


def start():
    threading.Thread(target=worker, daemon=True).start()
