"""Download clean-up: tell admins (notification type `cleanup`) when Cleanuparr removes a download.

Cleanuparr has no webhook of its own, but its ntfy provider POSTs JSON to whatever server URL it's given:
{"topic", "title", "message", "priority", "tags"}. Point it at http://pusharr:8000/cleanuparr with the webhook
login (Basic auth). The message is Cleanuparr's description plus "Key: value" lines (instance, download,
reason...). Only the title and the first lines are kept; which events it sends is chosen in Cleanuparr.
"""
import hashlib

import config
import core


def eligible(user):
    """Whether this Pusharr user can have the clean-up alert (notifications.types.cleanup.who)."""
    return core.who_allows(user, config.get('notifications.types.cleanup.who', 'admins'), 'admins')


def on_cleanuparr(payload):
    """One ntfy-style message from Cleanuparr -> a push to the people allowed the `cleanup` type."""
    title = str(payload.get('title') or '').strip()
    body = str(payload.get('message') or '').strip()
    if not (title or body):
        return 'empty'
    # one push per removal: the tag keeps two removals in the same second from replacing each other
    tag = 'cleanup-' + hashlib.sha1(f'{title}\n{body}'.encode()).hexdigest()[:12]
    push = core.payload_for(f"🧹 {title or 'Cleanuparr'}", body, tag=tag)
    result = {}
    for u in core.db().execute('select * from users').fetchall():
        if eligible(u):
            result[u['name']] = core.deliver(u, 'cleanup', push)
    core.log('cleanuparr:', title, '|', body.replace('\n', ' / ')[:200], '|', result)
    return 'sent' if result else 'nobody to tell'
