"""Approvals for approvers (approvals.approvers: Jellyfin admins or a list): Seerr requests (Approve /
Decline) and problem reports from the issue-replacement flow (Approve / Ignore), as pushes with buttons
and on the Pending page.

Action buttons carry a token that is HMAC-signed, single-use, short-lived and bound to the one
push subscription it was sent to (the service worker sends its own endpoint back with it). The
server then approves or declines in Seerr (`POST /api/v1/request/<id>/approve|decline`).
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.request

import config
import core

ENV = core.ENV
STATUS = {1: 'pending', 2: 'approved', 3: 'declined', 4: 'failed', 5: 'completed'}


def init_db():
    core.db().executescript('''
    create table if not exists action_tokens (
        nonce text primary key, sub_id integer not null, request_id integer not null,
        expires integer not null, used integer);
    ''')
    cols = {r['name'] for r in core.db().execute('pragma table_info(action_tokens)')}
    if 'kind' not in cols:      # 'request' (Seerr request id) or 'issue' (Seerr issue id)
        core.db().execute("alter table action_tokens add column kind text not null default 'request'")


# ---------- Seerr ----------

def seerr(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(config.get('integrations.seerr.url').rstrip('/') + '/api/v1' + path, data=data,
                                 method=method, headers={'X-Api-Key': config.secret('SEERR_API_KEY', ''),
                                                         'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def describe(req):
    """Title, subtitle bits and poster for a Seerr request."""
    media = req.get('media') or {}
    kind = 'movie' if req.get('type') == 'movie' else 'tv'
    try:
        info = seerr('GET', f"/{kind}/{media.get('tmdbId')}")
    except Exception:
        info = {}
    title = info.get('title') or info.get('name') or f"TMDB {media.get('tmdbId')}"
    year = (info.get('releaseDate') or info.get('firstAirDate') or '')[:4]
    if kind == 'movie':
        what = 'film'
    else:
        seasons = sorted(s['seasonNumber'] for s in req.get('seasons') or [])
        what = 'series' + (f", season{'s' if len(seasons) > 1 else ''} {', '.join(map(str, seasons))}" if seasons else '')
    who = (req.get('requestedBy') or {})
    poster = f"https://image.tmdb.org/t/p/w500{info['posterPath']}" if info.get('posterPath') else None
    return {'id': req['id'], 'title': title, 'year': year, 'what': what, 'poster': poster,
            'by': who.get('displayName') or who.get('jellyfinUsername') or '?',
            'by_jf': who.get('jellyfinUserId'), 'status': STATUS.get(req.get('status'), '?'),
            'jf_item': media.get('jellyfinMediaId'), 'created': req.get('createdAt')}


def label(d):
    return f"{d['title']} ({', '.join(x for x in (d['year'], d['what']) if x)})"


def pending():
    if not config.get('integrations.seerr.enabled'):
        return []
    res = seerr('GET', '/request?take=50&skip=0&filter=pending&sort=added')
    return [describe(r) for r in res.get('results', [])]


def decide(request_id, action, reason=''):
    """Approve or decline in Seerr. Returns (outcome, description) where outcome is approved,
    declined, already-<status> or gone."""
    try:
        req = seerr('GET', f'/request/{int(request_id)}')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 'gone', None
        raise
    d = describe(req)
    if req.get('status') != 1:
        return f"already-{d['status']}", d
    try:
        seerr('POST', f"/request/{int(request_id)}/{'approve' if action == 'approve' else 'decline'}")
    except urllib.error.HTTPError as e:
        if e.code == 409:           # someone handled it in Seerr in the meantime
            d = describe(seerr('GET', f'/request/{int(request_id)}'))
            return f"already-{d['status']}", d
        raise
    outcome = 'approved' if action == 'approve' else 'declined'
    core.log(f'request #{request_id} {outcome}:', label(d), 'from', d['by'])
    if outcome == 'declined':
        # Seerr has no decline reason, so the requester hears it from us now (Seerr's own decline webhook
        # that follows finds it already told)
        try:
            import releases
            releases.declined(int(request_id), reason)
        except Exception as e:
            core.log(f'request #{request_id}: could not tell the requester', repr(e)[:150])
    replace_notifications(request_id, outcome, d)
    return outcome, d


# ---------- notifications ----------

def secret():
    s = ENV.get('ACTION_SECRET') or core.meta_get('action_secret')
    if not s:
        s = secrets.token_urlsafe(32)
        core.meta_set('action_secret', s)
    return s.encode()


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()


def make_token(sub_id, ref_id, kind='request'):
    nonce = secrets.token_urlsafe(16)
    exp = core.now() + config.get('approvals.token_hours') * 3600
    body = b64(json.dumps({'n': nonce, 's': sub_id, 'r': ref_id, 'k': kind, 'e': exp}).encode())
    sig = b64(hmac.new(secret(), body.encode(), hashlib.sha256).digest())
    core.db().execute('insert into action_tokens(nonce, sub_id, request_id, expires, kind) values(?,?,?,?,?)',
                      (nonce, sub_id, ref_id, exp, kind))
    return f'{body}.{sig}'


ACTIONS = {'request': ('approve', 'decline'), 'issue': ('approve', 'ignore')}


def use_token(token, endpoint, action):
    """Check and burn a token. Returns ((kind, id), None) or (None, reason)."""
    try:
        body, sig = token.split('.')
        want = b64(hmac.new(secret(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, want):
            return None, 'bad signature'
        p = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
    except Exception:
        return None, 'malformed'
    if p['e'] < core.now():
        return None, 'expired'
    if action not in ACTIONS.get(p.get('k', 'request'), ()):
        return None, 'bad action'           # checked before the token is used up
    sub = core.db().execute('select * from subs where id=?', (p['s'],)).fetchone()
    if not sub or not endpoint or not hmac.compare_digest(sub['endpoint'], endpoint):
        return None, 'not for this device'
    owner = core.get_user(sub['jf_id'])
    if not owner or not core.is_approver(owner):
        return None, 'not an approver'
    # single use: only the first request flips used from NULL
    cur = core.db().execute('update action_tokens set used=? where nonce=? and used is null and expires>=?',
                            (core.now(), p['n'], core.now()))
    if cur.rowcount != 1:
        return None, 'already used'
    return (p.get('k', 'request'), p['r']), None


def notify_pending(request_id, auto=False):
    """Push a new request to approvers: with Approve/Decline (pending) or info only (auto-approved)."""
    d = describe(seerr('GET', f'/request/{int(request_id)}'))
    pref = 'auto_approved' if auto else 'approvals'
    title, body = config.msg('request_auto' if auto else 'request_pending', user=d['by'], what=label(d))
    sent = {}
    if not config.type_enabled(pref):
        return {'skipped': f'{pref} is switched off'}
    for u in core.approvers():
        if not core.prefs_of(u)[pref]:
            sent[u['name']] = {'skipped': f'{pref} off'}
            continue
        ok = 0
        subs = core.db().execute('select * from subs where jf_id=?', (u['jf_id'],)).fetchall()
        for s in subs:
            payload = {'title': title[:120], 'body': body,
                       'image': d['poster'], 'url': '/#pending' if not auto else '/', 'tag': f'req-{request_id}'}
            if not auto and d['status'] == 'pending':
                payload['actions'] = [{'action': 'approve', 'title': 'Approve'}, {'action': 'decline', 'title': 'Decline'}]
                payload['token'] = make_token(s['id'], int(request_id))
            ok += core.push_one(s, payload)
        sent[u['name']] = {'devices': len(subs), 'delivered': ok}
    core.log('request', '#' + str(request_id), 'auto-approved' if auto else 'pending', repr(label(d)), json.dumps(sent))
    return sent


def replace_notifications(request_id, outcome, d):
    """Replace the request notification on approvers' devices with the result (same tag)."""
    text = {'approved': 'Approved ✓', 'declined': 'Declined'}.get(outcome, outcome)
    for u in core.approvers():
        for s in core.db().execute('select * from subs where jf_id=?', (u['jf_id'],)).fetchall():
            core.push_one(s, {'title': f"{text}: {label(d)}", 'body': f"Requested by {d['by']}", 'image': None,
                              'url': '/#pending', 'tag': f'req-{request_id}', 'silent': True})


# ---------- problem reports (issue replacement; decided in the notifier) ----------

ISSUE_RESULT = {'approved': 'Approved ✓, getting a new copy', 'ignored': 'Ignored', 'expired': 'Expired',
                'unknown': 'No longer waiting'}


def notifier(method, path, body=None):
    """The issue-replacement flow lives in this app now (issues.py); same interface as the old HTTP API."""
    import issues
    if path == '/issues-pending':
        return issues.pending_list()
    if path == '/issue-action':
        outcome, title = issues.answer(int(body['issue_id']), body['action'], config.get('branding.app_name'))
        return {'outcome': outcome, 'title': title}
    raise ValueError(path)


def issue_label(e):
    return config.msg('issue_approval', kind=(e.get('kind') or 'Problem').title(), title=e.get('title'))[0]


def notify_issue(e):
    """From the notifier. event: approval (Approve / Ignore buttons), info (plain) or resolved
    (replaces the earlier notification)."""
    iid, kind = int(e['issue_id']), e.get('event', 'info')
    if e.get('dry'):
        core.log(f'issue #{iid} {kind} (dry run, not pushed):', e.get('push_title') or issue_label(e))
        return {'dry_run': True}
    sent = {}
    for u in core.approvers():
        if kind != 'resolved' and not core.prefs_of(u)['approvals']:
            sent[u['name']] = {'skipped': 'approvals off'}
            continue
        subs = core.db().execute('select * from subs where jf_id=?', (u['jf_id'],)).fetchall()
        ok = 0
        for s in subs:
            payload = core.payload_for(e.get('push_title') or issue_label(e), e.get('message') or '',
                                       e.get('jf_item'), tag=f'issue-{iid}')
            if kind == 'approval':
                payload.update(url='/#pending', token=make_token(s['id'], iid, 'issue'),
                               actions=[{'action': 'approve', 'title': 'Approve'},
                                        {'action': 'ignore', 'title': 'Ignore'}])
            elif kind == 'resolved':
                payload.update(url='/#pending', image=None, silent=True)
            ok += core.push_one(s, payload)
        sent[u['name']] = {'devices': len(subs), 'delivered': ok}
    core.log(f'issue #{iid} {kind}:', repr(e.get('push_title') or issue_label(e)), json.dumps(sent))
    return sent


def pending_issues():
    base = core.jellyfin_base()
    out = []
    for i in notifier('GET', '/issues-pending') or []:
        i['poster'] = (f"{base}/Items/{i['jf_item']}/Images/Primary?maxHeight=300&quality=85"
                       if i.get('jf_item') and base else None)
        out.append(i)
    return out


def issue_text(outcome):
    return ISSUE_RESULT.get(outcome) or (f'Already {outcome[8:]}' if outcome.startswith('already-') else outcome)


def issue_decide(issue_id, action):
    """Approve / Ignore a problem report through the notifier, then replace the notification."""
    res = notifier('POST', '/issue-action', {'issue_id': int(issue_id), 'action': action})
    outcome = res.get('outcome', 'unknown')
    text = issue_text(outcome)
    core.log(f'issue #{issue_id} {action} ->', outcome)
    notify_issue({'issue_id': issue_id, 'event': 'resolved',
                  'push_title': f"{text}: {res.get('title')}" if res.get('title') else text})
    return outcome, res.get('title')
