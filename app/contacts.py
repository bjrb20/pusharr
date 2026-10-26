"""Wizarr -> Seerr contact sync (optional: integrations.wizarr). Loaded by notifier.py. New-episode
alerts work on Seerr users, so this brings people who joined through Wizarr invites into Seerr.

Every integrations.wizarr.sync_minutes (15):
  - read Wizarr's users (username, email) from a copy of its SQLite DB (integrations.wizarr.database,
    mounted read-only; Wizarr's API needs a key made in its admin UI, so the DB is used instead and
    never written to),
  - match them to Seerr users by Jellyfin username,
  - if the Seerr user's email is empty, set it to Wizarr's. Seerr fills the email of a Jellyfin user
    with their username when Jellyfin has no email, so anything without an "@" counts as empty.
    A real address is never overwritten,
  - if the Wizarr user has no Seerr account, import them from Jellyfin into Seerr (Seerr's default
    permissions).
Changes are logged by username only, never the addresses.
"""
import os, shutil, sqlite3, tempfile, threading, time, traceback

import config

N = None  # the notifier module, set by notifier.py


def wizarr_db():
    return config.get('integrations.wizarr.database')


def real_email(e):
    return bool(e) and '@' in e


def api(method, path, body=None):
    return N.http(method, N.url('seerr') + '/api/v1' + path, body, headers={'X-Api-Key': config.secret('SEERR_API_KEY', '')})


def wizarr_users():
    """[(username, email or None)] from a private copy of Wizarr's DB (+ its WAL), so the original is never opened."""
    with tempfile.TemporaryDirectory() as d:
        for suffix in ('', '-wal', '-shm'):
            if os.path.exists(wizarr_db() + suffix):
                shutil.copyfile(wizarr_db() + suffix, os.path.join(d, 'w.db' + suffix))
        c = sqlite3.connect(os.path.join(d, 'w.db'))
        try:
            rows = c.execute('select username, email from user where coalesce(is_disabled, 0) = 0').fetchall()
        finally:
            c.close()
    return [(u, e if real_email(e) else None) for u, e in rows if u]


def seerr_all_users():
    return api('GET', '/user?take=1000')['results']


def set_email(u, email, dry):
    """Set a Seerr user's email, sending their other main settings back unchanged (Seerr overwrites them)."""
    if dry:
        return 'would set email'
    main = api('GET', f"/user/{u['id']}/settings/main")
    main['email'] = email
    api('POST', f"/user/{u['id']}/settings/main", main)
    return 'set email'


def sync(dry=False):
    changes = []
    wiz = wizarr_users()
    seerr = {(u.get('jellyfinUsername') or '').lower(): u for u in seerr_all_users() if u.get('jellyfinUsername')}
    jf_ids = None
    for name, email in wiz:
        u = seerr.get(name.lower())
        if not u:
            if jf_ids is None:
                jf_ids = {x['Name'].lower(): x['Id'] for x in N.jf('/Users')}
            jid = jf_ids.get(name.lower())
            if not jid:
                changes.append(f'{name}: in Wizarr but not in Jellyfin, skipped')
                continue
            if dry:
                changes.append(f'{name}: would import from Jellyfin into Seerr')
                continue
            api('POST', '/user/import-from-jellyfin', {'jellyfinUserIds': [jid]})
            changes.append(f'{name}: imported from Jellyfin into Seerr (default permissions)')
            u = next((x for x in seerr_all_users() if (x.get('jellyfinUsername') or '').lower() == name.lower()), None)
            if not u:
                continue
        if email and not real_email(u.get('email')):
            try:
                changes.append(f'{name}: {set_email(u, email, dry)} in Seerr from Wizarr')
            except Exception as e:
                changes.append(f'{name}: could not set email ({repr(e)[:120]})')
    return changes, len(wiz)


def loop():
    while True:
        try:
            changes, n = sync()
            for c in changes:
                N.log('contact sync:', c)
        except Exception:
            N.log('ERROR contact sync', traceback.format_exc()[-400:])
        time.sleep(config.get('integrations.wizarr.sync_minutes') * 60)


def start():
    if not (config.get('integrations.wizarr.enabled') and config.get('integrations.seerr.enabled')):
        return
    if not os.path.exists(wizarr_db()):
        return N.log(f'{wizarr_db()} not mounted: Wizarr -> Seerr contact sync is off')
    N.log(f"Wizarr -> Seerr contact sync every {config.get('integrations.wizarr.sync_minutes')} min")
    threading.Thread(target=loop, daemon=True).start()
