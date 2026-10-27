"""New accounts: tell admins (notification type `joins`) when someone joins, e.g. through a Wizarr invite.

Wizarr has no webhook with the invite in it (its notification agents send only "User X has joined"), so
new people are noticed in Jellyfin itself: the user list is checked every minute, and anyone not seen
before is new. Who they are and which invite they used come from Wizarr's database (the read-only copy
contacts.py also reads: user.code -> invitation). Wizarr's ntfy agent can also be pointed at
http://pusharr:8000/wizarr (webhook login) to make the check run at once.

The first check only records who's there, so existing accounts never cause an alert. A new account
whose Wizarr record isn't written yet is looked at again for up to 2 minutes before the alert goes.
"""
import datetime as dt
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import traceback

import config
import core

N = None            # the notifier module, set by notifier.py
CHECK_EVERY = 60
WIZARR_GRACE = 120  # seconds to wait for Wizarr's record of a new account

_lock = threading.Lock()
_first_seen = {}    # jellyfin id -> when it was first noticed (while waiting for Wizarr)
_wake = threading.Event()


def init_db():
    core.db().execute('create table if not exists known_accounts (jf_id text primary key, name text, seen integer)')


def wizarr_on():
    return config.get('integrations.wizarr.enabled') and os.path.exists(config.get('integrations.wizarr.database'))


def wizarr_record(username):
    """{'code', 'created', 'invite_created', 'invite_expires', 'unlimited', 'uses'} for a Wizarr user, from a
    private copy of Wizarr's DB (the original is never opened), or None."""
    db = config.get('integrations.wizarr.database')
    with tempfile.TemporaryDirectory() as d:
        for suffix in ('', '-wal', '-shm'):
            if os.path.exists(db + suffix):
                shutil.copyfile(db + suffix, os.path.join(d, 'w.db' + suffix))
        c = sqlite3.connect(os.path.join(d, 'w.db'))
        c.row_factory = sqlite3.Row
        try:
            u = c.execute('select * from user where lower(username)=lower(?) order by id desc limit 1', (username,)).fetchone()
            if not u:
                return None
            out = {'code': u['code'], 'created': u['created_at']}
            inv = c.execute('select * from invitation where code=? order by id desc limit 1', (u['code'],)).fetchone() \
                if u['code'] else None
            if inv:
                uses = c.execute('select count(*) from invitation_user where invite_id=?', (inv['id'],)).fetchone()[0]
                out.update(invite_created=inv['created'], invite_expires=inv['expires'], unlimited=bool(inv['unlimited']),
                           uses=uses)
            return out
        finally:
            c.close()


def how(rec):
    """The alert's text: which invite, if any."""
    if not rec:
        return 'New account in Jellyfin (not through a Wizarr invite).'
    if not rec.get('code'):
        return 'Joined through Wizarr.'
    text = f"Used Wizarr invite {rec['code']}"
    if rec.get('invite_created'):
        text += f" (made {str(rec['invite_created'])[:10]}"
        text += ', unlimited' if rec.get('unlimited') else ''
        text += f", used {rec['uses']}×)" if rec.get('uses') and rec.get('unlimited') else ')'
    return text + '.'


def eligible(user):
    """Whether this Pusharr user can have the joins alert (notifications.types.joins.who)."""
    who = config.get('notifications.types.joins.who', 'admins')
    if who == 'everyone':
        return True
    if who == 'approvers':
        return core.is_approver(user)
    if isinstance(who, list):
        return (user['name'] or '').lower() in {x.lower() for x in who}
    return bool(user['is_admin'])


def announce(account, rec):
    title, body = config.msg('user_joined', user=account['Name'], how=how(rec), invite=(rec or {}).get('code') or '')
    payload = core.payload_for(title, body, tag=f"joined-{account['Id']}")
    payload['url'] = '/#admin'
    result = {}
    for u in core.db().execute('select * from users').fetchall():
        if eligible(u):
            result[u['name']] = core.deliver(u, 'joins', payload)
    core.log('joined:', account['Name'], '|', body, '|', result)
    return result


def check(t=None):
    """Look for new Jellyfin accounts; alert about the ones that are ready. Returns the names announced."""
    t = t or core.now()
    with _lock:
        accounts = core.jf_request('GET', '/Users', token=config.secret('JELLYFIN_API_KEY')) or []
        known = {r['jf_id'] for r in core.db().execute('select jf_id from known_accounts')}
        if not known:       # first look: remember everyone, alert about nobody
            core.db().executemany('insert or ignore into known_accounts(jf_id, name, seen) values(?,?,?)',
                                  [(a['Id'], a['Name'], t) for a in accounts])
            core.log(f'new-account alerts: {len(accounts)} existing accounts noted (no alerts for them)')
            return []
        announced = []
        for a in accounts:
            if a['Id'] in known:
                continue
            rec = None
            if wizarr_on():
                try:
                    rec = wizarr_record(a['Name'])
                except Exception as e:
                    core.log('could not read Wizarr for', a['Name'], repr(e)[:120])
                first = _first_seen.setdefault(a['Id'], t)
                if rec is None and t - first < WIZARR_GRACE:
                    continue        # Wizarr may still be writing its record: look again next time
            _first_seen.pop(a['Id'], None)
            core.db().execute('insert or ignore into known_accounts(jf_id, name, seen) values(?,?,?)', (a['Id'], a['Name'], t))
            if config.type_enabled('joins'):
                announce(a, rec)
            announced.append(a['Name'])
        return announced


def on_wizarr(text):
    """Wizarr's ntfy agent ("User X has joined your server!"): check now instead of within the minute."""
    _wake.set()
    return 'checking'


def loop():
    while True:
        try:
            check()
        except Exception:
            core.log('ERROR new-account check', traceback.format_exc()[-300:])
        _wake.wait(CHECK_EVERY)
        if _wake.is_set():
            _wake.clear()
            time.sleep(5)       # let Wizarr finish writing its record


def start():
    init_db()
    threading.Thread(target=loop, daemon=True).start()
