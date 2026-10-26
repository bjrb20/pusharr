"""Pusharr settings: one YAML file (CONFIG_DIR/config.yaml) for everything except secrets, which come
from the environment (or an env file named by ENV_FILE, so they stay out of `docker inspect`).

The file is merged over DEFAULTS (neutral "Pusharr" branding), so it only needs what differs. The admin
"Branding & settings" page edits the same file through validate() and save(); most changes apply at once
(ports and the webhook listener need a restart).

  get('branding.app_name')            one setting
  msg('episode_one', series=..., ...)  a message template, filled in -> (title, body)
  secret('JELLYFIN_API_KEY')           a secret from the environment
"""
import copy
import hashlib
import os
import re
import string
import threading
from zoneinfo import ZoneInfo

import yaml

ENV = os.environ
if ENV.get('ENV_FILE') and os.path.exists(ENV['ENV_FILE']):
    for _line in open(ENV['ENV_FILE']):
        _line = _line.strip()
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _v = _line.split('=', 1)
            ENV.setdefault(_k.strip(), _v.strip().strip('"\''))

CONFIG_DIR = ENV.get('CONFIG_DIR', '/config')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.yaml')
BRAND_DIR = os.path.join(CONFIG_DIR, 'branding')       # uploaded icons, fonts and wordmark images

# Secrets: environment only, never in config.yaml.
SECRETS = ('JELLYFIN_API_KEY', 'SEERR_API_KEY', 'SONARR_API_KEY', 'RADARR_API_KEY', 'BAZARR_API_KEY',
           'VAPID_PUBLIC_KEY', 'VAPID_PRIVATE_KEY', 'INTERNAL_TOKEN', 'WEBHOOK_USER', 'WEBHOOK_PASSWORD',
           'ACTION_SECRET')
REQUIRED_SECRETS = ('JELLYFIN_API_KEY', 'VAPID_PUBLIC_KEY', 'VAPID_PRIVATE_KEY', 'INTERNAL_TOKEN')

FONTS = {   # bundled fonts (static/fonts, SIL Open Font License): key -> (CSS family, file, weights, TTF for icons, fallbacks)
    'inter': ('Inter', 'inter-latin-wght-normal.woff2', '100 900', None, '"Helvetica Neue", Arial, system-ui, sans-serif'),
    'bebas-neue': ('Bebas Neue', 'bebas-neue-latin-400-normal.woff2', '400', 'BebasNeue-Regular.ttf',
                   'Impact, "Arial Narrow", sans-serif'),
    'system': (None, None, None, None, None),
}
STORES = ('ios', 'android', 'windows', 'android_tv', 'other')      # app-store link slots per recommended app

DEFAULTS = {
    'public_url': '',               # where people open the app (https, behind your reverse proxy)
    'vapid_subject': '',            # contact for push services (mailto: or https:); default public_url
    'extra_origins': [],            # other allowed Origins, e.g. http://localhost:8095 through an SSH tunnel
    'timezone': 'UTC',
    'server': {'public_port': 8080, 'internal_port': 8001, 'webhook_port': 8000},
    'session': {'days': 30, 'cookie_name': '__Host-pusharr'},
    'branding': {
        'app_name': 'Pusharr',
        'short_name': 'Pusharr',
        'server_name': 'Jellyfin',      # the media server as people know it: "... is ready on Jellyfin"
        'description': 'Notifications for new episodes, films and requests on {server}.',
        'theme': {
            'bg': '#0f1115', 'panel': '#161a20', 'panel_2': '#1c2129', 'line': '#262c36',
            'text': '#e6e8eb', 'text_2': '#cfd3d9', 'muted': '#9aa3ad', 'heading': '#ffffff',
            'accent': '#3b82f6', 'accent_dark': '#2563eb', 'accent_text': '#ffffff', 'ok': '#34d399',
            'header_top': '#0b0d10', 'header_line': '#1f242c', 'secondary': '#2a313b', 'secondary_hover': '#353d49',
            'field': '#1a1f26', 'field_line': '#2a313b', 'link': '#60a5fa',
            'theme_color': '#0f1115', 'background_color': '#0f1115',
        },
        'fonts': {'body': 'inter', 'wordmark': 'inter'},    # a bundled font, 'system', or 'custom:<file in branding/fonts>'
        'wordmark': {
            'style': 'text',            # text or image
            'text': 'PUSHARR', 'subtitle': 'Alerts',
            'weight': 800, 'letter_spacing': '2px', 'glow': False,
            'image': '',                # image style: a file in branding/ (uploaded on the settings page)
        },
        'icon_dir': '',                 # '' = the bundled icons; else a folder in branding/ (made from an uploaded SVG)
    },
    'jellyfin': {
        'url': 'http://jellyfin:8096',  # as this server reaches it
        'public_url': '',               # as people reach it (links and posters); https
        'sign_in': {'password': True, 'quick_connect': True},
    },
    'links': {
        'watch': '',                    # "Watch" card and footer link (https)
        'watch_item': '{jellyfin}/web/#/details?id={item_id}&serverId={server_id}',
        'watch_home': '{jellyfin}/web/',
        'request': '',                  # "Request" card and footer link (https)
        'request_name': 'Seerr',
        'request_item': '{request}/{media}/{tmdb}',
        'get_app': '',                  # footer "Get the app"; default: the first recommended app's store for the device
        'recommended_apps': [],         # [{name, description, stores: {ios, android, windows, android_tv, other},
                                        #   play: {movie, show, show_tmdb}, play_on: [android, ios, mac]}]
        'alternative_apps': [],         # [{name, url, text}]  text: "... {link} ..."
    },
    'notifications': {
        'types': {
            'episodes': {'enabled': True, 'default': True},
            'requests': {'enabled': True, 'default': True},
            'issues': {'enabled': True, 'default': True},
            'announcements': {'enabled': True, 'default': True},
            'movies': {'enabled': True, 'default': False},
            'shows': {'enabled': True, 'default': False},
            'anime': {'enabled': True, 'default': False},
            'approvals': {'enabled': True, 'default': True},
            'auto_approved': {'enabled': True, 'default': False},
        },
        'digest_hour': 19,
        'batching': {'import_wait': 180, 'library_wait': 60, 'library_burst_limit': 5, 'library_burst_window': 600},
        'follow': {'recent_watch_days': 60, 'caught_up': True, 'watchlist': True, 'requested': True},
    },
    'integrations': {
        'seerr': {'enabled': True, 'url': 'http://seerr:5055'},
        'sonarr': {'enabled': True, 'url': 'http://sonarr:8989'},
        'radarr': {'enabled': True, 'url': 'http://radarr:7878'},
        'bazarr': {'enabled': False, 'url': 'http://bazarr:6767', 'language': 'en', 'language_name': 'English'},
        'wizarr': {'enabled': False, 'database': '/wizarr/database.db', 'sync_minutes': 15},
    },
    'approvals': {
        'approvers': 'admins',          # 'admins' (Jellyfin administrators) or a list of Jellyfin usernames
        'token_hours': 12,
        'issue_expire_hours': 24,
        'picker': {
            'languages': ['English'],
            'allow_original_language': True,
            'dub_tags': ['lat', 'latino', 'latam', 'dual[ ._-]?lat', 'castellano', 'espa[nñ]ol', 'spanish', 'esp', 'spa',
                         'dubbed', 'dub', 'doblado', 'dublado', 'pt[ ._-]?br', 'portugues'],
            'language_tags': ['eng', 'english'],
            'other_language_tags': ['ita', 'italian', 'french', 'truefrench', 'vff', 'vf2', 'vfq', 'vfi', 'vostfr', 'ger',
                                    'german', 'rus', 'russian', 'hindi', 'tamil', 'telugu', 'kor', 'korean', 'jap',
                                    'multi', 'dual'],
            'hardsub_tags': ['hc', 'hardsub', 'hardcoded', 'esub', 'korsub', 'hcsub'],
            'camera_tags': ['cam', 'camrip', 'hdcam', 'ts', 'hdts', 'telesync', 'tc', 'hdtc', 'telecine', 'pdvd', 'predvd',
                            'scr', 'screener', 'dvdscr', 'bdscr', 'workprint', 'r5'],
            'min_size_mb': {'movie': 700, 'episode': 100},
        },
    },
    'messages': {
        'episode_one': {'title': '📺 {series} {episode} is ready on {server}', 'body': '{progress}'},
        'episode_many': {'title': '📺 {count} new episodes of {series} on {server}', 'body': '{first}–{last}. {progress}'},
        'progress_not_started': "You haven't started it yet.",
        'progress_next': "You're up to {furthest}.",
        'progress_behind': "You're up to {furthest}, {behind} episodes behind.",
        'request_ready': {'title': '🍿 {what} is ready to watch on {server}', 'body': 'The thing you requested is ready to watch.'},
        'request_declined': {'title': 'Request declined: {what}', 'body': '{reason}'},
        'request_declined_no_reason': 'Ask an admin if you want to know why.',
        'request_pending': {'title': '🎬 New request from {user}: {what}', 'body': 'Tap to see pending requests.'},
        'request_auto': {'title': '✅ Auto-approved: {user} requested: {what}', 'body': 'No action needed.'},
        'library_one': {'title': '🎬 New {kind} on {server}', 'body': '{title}'},
        'library_many': {'title': '🎬 {count} new {kinds} on {server}', 'body': '{titles}'},
        'digest': {'title': '🍿 Today on {server}', 'body': '{summary}'},
        'issue_approval': {'title': '⚠️ {kind} problem on {server}: {title}',
                           'body': '{said}\nApprove to get a different copy (the current one stays until it arrives).'},
        'issue_unsupported': {'title': '⚠️ {kind} problem on {server}: {title}',
                              'body': "{said}\nCan't replace it automatically ({why}). Have a look in {request_name}."},
        'issue_subtitles': {'title': '💬 Subtitle problem on {server}: {title}', 'body': '{said}\n{result}'},
        'issue_other': {'title': '⚠️ Problem reported on {server}: {title}', 'body': '{said}'},
        'issue_fixed': {'title': '📺 {title} has been replaced with a new copy on {server}',
                        'body': 'Thanks for reporting the problem.'},
        'subtitles_fixed': {'title': '💬 New subtitles for {title}', 'body': 'Thanks for reporting it.'},
        'issue_replaced': {'title': '✅ Replaced on {server}: {title}', 'body': 'New copy: {release}'},
        'issue_no_copy': {'title': 'No better copy available yet: {title}',
                          'body': "Nothing acceptable on the indexers right now ({rules}). The current copy stays and the "
                                  "report stays open; I'll look again every {hours} h for {days} days."},
        'issue_failed': {'title': '❌ Replacement failed: {title}',
                         'body': 'Something went wrong; the current file is unchanged. Details are in the {app} log.'},
        'issue_stalled': {'title': '❌ Replacement stalled: {title}',
                          'body': 'No new copy imported after {days} days. The current file is unchanged; check the '
                                  'Radarr/Sonarr queue.'},
        'issue_expired': {'title': 'Expired: {title}', 'body': 'No answer within {hours} h; nothing was changed.'},
        'test': {'title': '🔔 {app} test', 'body': 'Hi {user}, notifications work on this device.'},
        'announcement': {'title': '📣 {title}', 'body': '{body}'},
    },
}

# Variables each message may use (besides {server}, {app} and {request_name}, which all may use).
MESSAGE_VARS = {
    'episode_one': {'series', 'episode', 'progress', 'user'},
    'episode_many': {'series', 'count', 'first', 'last', 'progress', 'user'},
    'progress_not_started': set(), 'progress_next': {'furthest'}, 'progress_behind': {'furthest', 'behind'},
    'request_ready': {'what', 'title', 'user'}, 'request_declined': {'what', 'reason'},
    'request_declined_no_reason': set(), 'request_pending': {'what', 'user'}, 'request_auto': {'what', 'user'},
    'library_one': {'kind', 'title'}, 'library_many': {'count', 'kinds', 'titles'}, 'digest': {'summary'},
    'issue_approval': {'kind', 'title', 'said', 'user'}, 'issue_unsupported': {'kind', 'title', 'said', 'why', 'user'},
    'issue_subtitles': {'title', 'said', 'result', 'user'}, 'issue_other': {'title', 'said', 'user'},
    'issue_fixed': {'title'}, 'subtitles_fixed': {'title'}, 'issue_replaced': {'title', 'release'},
    'issue_no_copy': {'title', 'rules', 'hours', 'days'}, 'issue_failed': {'title'}, 'issue_stalled': {'title', 'days'},
    'issue_expired': {'title', 'hours'}, 'test': {'user'}, 'announcement': {'title', 'body'},
}
COMMON_VARS = {'server', 'app', 'request_name'}

_lock = threading.RLock()
_conf = None
_version = ''


# ---------- loading ----------

def merge(base, over):
    """Deep merge: dicts merge key by key, anything else (lists included) replaces."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = merge(out[k], v) if isinstance(out.get(k), dict) and isinstance(v, dict) else copy.deepcopy(v)
    return out


def read_file(path=None):
    path = path or CONFIG_FILE
    if not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f'{path}: the top level must be a mapping')
    return data


def load(path=None):
    """(Re)load the config file. Returns the list of problems (empty when it's fine); a bad file is
    not applied, the previous settings stay."""
    global _conf, _version
    try:
        raw = read_file(path)
    except (OSError, ValueError, yaml.YAMLError) as e:
        return [f'could not read the config file: {e}']
    conf = merge(DEFAULTS, raw)
    problems = validate(conf)
    if problems and _conf is not None:
        return problems
    with _lock:
        _conf = conf
        _version = hashlib.sha256(yaml.safe_dump(conf, sort_keys=True, allow_unicode=True).encode()).hexdigest()[:10]
    return problems


def current():
    if _conf is None:
        load()
    return _conf


def version():
    """Changes whenever the settings change (cache-busting for the service worker and theme)."""
    current()
    return _version


def get(path, default=None):
    node = current()
    for part in path.split('.'):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def secret(name, default=None):
    return ENV.get(name) or default


def tz():
    return ZoneInfo(get('timezone') or 'UTC')


def save(conf):
    """Validate and write the whole config (keeping the previous file as config.yaml.bak), then apply it.
    Returns the problems; nothing is written if there are any."""
    conf = merge(DEFAULTS, conf)
    problems = validate(conf)
    if problems:
        return problems
    os.makedirs(CONFIG_DIR, exist_ok=True)
    text = ('# Pusharr settings (written by the Branding & settings page; secrets live in the env file)\n'
            + yaml.safe_dump(strip_defaults(conf, DEFAULTS), sort_keys=False, allow_unicode=True, width=120))
    tmp = CONFIG_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    if os.path.exists(CONFIG_FILE):
        os.replace(CONFIG_FILE, CONFIG_FILE + '.bak')
    os.replace(tmp, CONFIG_FILE)
    return load()


def strip_defaults(conf, defaults):
    """Only what differs from DEFAULTS, so the file stays short and new defaults reach old installs."""
    out = {}
    for k, v in conf.items():
        if isinstance(v, dict) and isinstance(defaults.get(k), dict):
            sub = strip_defaults(v, defaults[k])
            if sub:
                out[k] = sub
        elif k not in defaults or defaults[k] != v:
            out[k] = v
    return out


# ---------- validation ----------

HEX = re.compile(r'#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?([0-9a-fA-F]{2})?$')
FREE_KEYS = {'messages'}     # checked separately


def _template_fields(text):
    try:
        return {f.split('.')[0].split('[')[0] for _, f, _, _ in string.Formatter().parse(text) if f is not None}
    except ValueError as e:
        raise ValueError(f'bad template ({e})')


def _check_types(conf, defaults, path, problems):
    for k, v in conf.items():
        p = f'{path}{k}'
        if k not in defaults:
            problems.append(f'{p}: unknown setting')
            continue
        d = defaults[k]
        if isinstance(d, dict):
            if not isinstance(v, dict):
                problems.append(f'{p}: should be a mapping')
            elif p not in FREE_KEYS:
                _check_types(v, d, p + '.', problems)
        elif isinstance(d, bool):
            if not isinstance(v, bool):
                problems.append(f'{p}: should be true or false')
        elif isinstance(d, int):
            if isinstance(v, bool) or not isinstance(v, int):
                problems.append(f'{p}: should be a whole number')
        elif isinstance(d, str) and k != 'approvers':
            if not isinstance(v, str):
                problems.append(f'{p}: should be text')
        elif isinstance(d, list):
            if not isinstance(v, list):
                problems.append(f'{p}: should be a list')


def _url(v, https=True):
    return v == '' or (isinstance(v, str) and re.match(r'https://' if https else r'https?://', v) is not None)


def validate(conf):
    """Problems with a merged config, as readable strings (empty list = fine)."""
    problems = []
    _check_types(conf, DEFAULTS, '', problems)
    if problems:
        return problems
    b, links = conf['branding'], conf['links']
    for k, v in b['theme'].items():
        if not HEX.match(v):
            problems.append(f'branding.theme.{k}: not a colour like #1a2b3c')
    for k in ('body', 'wordmark'):
        f = b['fonts'][k]
        if f not in FONTS and not (f.startswith('custom:') and re.fullmatch(r'custom:[A-Za-z0-9_.-]+\.(woff2|ttf|otf)', f)):
            problems.append(f'branding.fonts.{k}: one of {", ".join(FONTS)} or custom:<font file>')
    if b['wordmark']['style'] not in ('text', 'image'):
        problems.append('branding.wordmark.style: text or image')
    if b['wordmark']['style'] == 'image' and not b['wordmark']['image']:
        problems.append('branding.wordmark.image: upload an image, or use the text style')
    for k in ('image', 'icon_dir'):
        v = b['wordmark']['image'] if k == 'image' else b['icon_dir']
        if v and not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_./-]*', v) or '..' in v:
            problems.append(f'branding.{k}: a plain path inside branding/')
    if not re.fullmatch(r'-?\d+(\.\d+)?(px|em)', b['wordmark']['letter_spacing']):
        problems.append('branding.wordmark.letter_spacing: like 2px or 0.1em')
    if not (100 <= b['wordmark']['weight'] <= 900):
        problems.append('branding.wordmark.weight: 100 to 900')
    for k in ('app_name', 'short_name', 'server_name'):
        if not b[k].strip():
            problems.append(f'branding.{k}: must not be empty')
    if not _url(conf['public_url']):
        problems.append('public_url: an https:// address')
    if conf['vapid_subject'] and not re.match(r'(mailto:|https://)', conf['vapid_subject']):
        problems.append('vapid_subject: a mailto: or https:// address')
    if not _url(conf['jellyfin']['public_url']):
        problems.append('jellyfin.public_url: an https:// address')
    if not _url(conf['jellyfin']['url'], https=False) or not conf['jellyfin']['url']:
        problems.append('jellyfin.url: the address this server reaches Jellyfin at (http:// or https://)')
    if not (conf['jellyfin']['sign_in']['password'] or conf['jellyfin']['sign_in']['quick_connect']):
        problems.append('jellyfin.sign_in: turn on password or Quick Connect (or both)')
    for o in conf['extra_origins']:
        if not _url(o, https=False) or not o:
            problems.append(f'extra_origins: {o!r} is not an http(s):// origin')
    try:
        ZoneInfo(conf['timezone'])
    except Exception:
        problems.append(f"timezone: unknown time zone {conf['timezone']!r}")
    for k, v in conf['server'].items():
        if not (0 <= v < 65536):
            problems.append(f'server.{k}: not a port number')
    if not re.fullmatch(r'__Host-[A-Za-z0-9_]+', conf['session']['cookie_name']):
        problems.append('session.cookie_name: must start with __Host- (letters, digits, _)')
    if not (1 <= conf['session']['days'] <= 365):
        problems.append('session.days: 1 to 365')
    for k in ('watch', 'request', 'get_app'):
        if not _url(links[k]):
            problems.append(f'links.{k}: an https:// address (or empty)')
    for i, app in enumerate(links['recommended_apps']):
        p = f'links.recommended_apps[{i}]'
        if not isinstance(app, dict) or not str(app.get('name', '')).strip():
            problems.append(f'{p}: needs at least a name')
            continue
        unknown = set(app) - {'name', 'description', 'stores', 'play', 'play_on'}
        if unknown:
            problems.append(f'{p}: unknown key(s) {", ".join(sorted(unknown))}')
        for store, url in (app.get('stores') or {}).items():
            if store not in STORES:
                problems.append(f'{p}.stores.{store}: one of {", ".join(STORES)}')
            elif not _url(url or ''):
                problems.append(f'{p}.stores.{store}: an https:// address')
        for kind, tpl in (app.get('play') or {}).items():
            if kind not in ('movie', 'show', 'show_tmdb'):
                problems.append(f'{p}.play.{kind}: movie, show or show_tmdb')
                continue
            try:
                bad = _template_fields(str(tpl)) - {'tmdb', 'tvdb', 'item_id'}
                if bad:
                    problems.append(f'{p}.play.{kind}: unknown variable(s) {", ".join(sorted(bad))}')
            except ValueError as e:
                problems.append(f'{p}.play.{kind}: {e}')
        if not set(app.get('play_on') or []) <= {'android', 'ios', 'mac', 'windows', 'linux', 'android-tv', 'other'}:
            problems.append(f'{p}.play_on: from android, ios, mac, windows, linux, android-tv, other')
    for i, alt in enumerate(links['alternative_apps']):
        if not isinstance(alt, dict) or not alt.get('name') or not _url(alt.get('url', '')) or not alt.get('url'):
            problems.append(f'links.alternative_apps[{i}]: needs a name and an https:// url')
    for k, allowed in (('watch_item', {'jellyfin', 'item_id', 'server_id'}), ('watch_home', {'jellyfin'}),
                       ('request_item', {'request', 'media', 'tmdb'})):
        try:
            bad = _template_fields(links[k]) - allowed
            if bad:
                problems.append(f'links.{k}: unknown variable(s) {", ".join(sorted(bad))}')
        except ValueError as e:
            problems.append(f'links.{k}: {e}')
    n = conf['notifications']
    if not (0 <= n['digest_hour'] <= 23):
        problems.append('notifications.digest_hour: 0 to 23')
    for k, v in n['batching'].items():
        if v < 0:
            problems.append(f'notifications.batching.{k}: must not be negative')
    a = conf['approvals']
    if not (a['approvers'] == 'admins' or (isinstance(a['approvers'], list)
                                           and all(isinstance(x, str) and x for x in a['approvers']))):
        problems.append("approvals.approvers: 'admins' or a list of Jellyfin usernames")
    for k in ('dub_tags', 'language_tags', 'other_language_tags', 'hardsub_tags', 'camera_tags'):
        for tag in a['picker'][k]:
            try:
                re.compile(str(tag))
            except re.error as e:
                problems.append(f'approvals.picker.{k}: {tag!r} is not a valid pattern ({e})')
    if not a['picker']['languages']:
        problems.append('approvals.picker.languages: at least one language')
    for key, tpl in conf['messages'].items():
        if key not in DEFAULTS['messages']:
            problems.append(f'messages.{key}: unknown message')
            continue
        parts = tpl.items() if isinstance(tpl, dict) else [('', tpl)]
        if isinstance(DEFAULTS['messages'][key], dict) != isinstance(tpl, dict):
            problems.append(f'messages.{key}: should be ' + ('title + body' if isinstance(DEFAULTS['messages'][key], dict)
                                                              else 'one piece of text'))
            continue
        for part, text in parts:
            p = f'messages.{key}' + (f'.{part}' if part else '')
            if part not in ('', 'title', 'body') or not isinstance(text, str):
                problems.append(f'{p}: should be text (title / body)')
                continue
            try:
                bad = _template_fields(text) - MESSAGE_VARS[key] - COMMON_VARS
                if bad:
                    problems.append(f'{p}: unknown variable(s) {", ".join("{" + x + "}" for x in sorted(bad))}; '
                                    f'can use {", ".join("{" + x + "}" for x in sorted(MESSAGE_VARS[key] | COMMON_VARS))}')
            except ValueError as e:
                problems.append(f'{p}: {e}')
    return problems


def check_secrets():
    return [f'{k} is not set (see .env.example)' for k in REQUIRED_SECRETS if not ENV.get(k)]


# ---------- templates ----------

class _Vars(dict):
    def __missing__(self, key):
        return '{' + key + '}'


def fill(text, **values):
    """str.format with the common variables, leaving unknown {placeholders} as they are."""
    v = _Vars(server=get('branding.server_name'), app=get('branding.app_name'),
              request_name=get('links.request_name'))
    v.update({k: '' if x is None else x for k, x in values.items()})
    try:
        return string.Formatter().vformat(text, (), v)
    except (ValueError, IndexError, AttributeError, KeyError):
        return text


def msg(key, **values):
    """A message template filled in: (title, body) for title/body messages, else the text."""
    t = get('messages.' + key) or DEFAULTS['messages'][key]
    if isinstance(t, dict):
        return fill(t.get('title', ''), **values), fill(t.get('body', ''), **values)
    return fill(t, **values)


def link(key, **values):
    """A link template from links.* filled in, or None if a base it needs isn't set."""
    bases = {'jellyfin': (get('jellyfin.public_url') or '').rstrip('/'), 'request': (get('links.request') or '').rstrip('/')}
    tpl = get('links.' + key) or ''
    needs = _template_fields(tpl) & set(bases)
    if not tpl or any(not bases[b] for b in needs):
        return None
    return string.Formatter().vformat(tpl, (), _Vars(**bases, **{k: '' if v is None else v for k, v in values.items()}))


def type_enabled(kind):
    return bool(get(f'notifications.types.{kind}.enabled', True))


def approvers_rule():
    return get('approvals.approvers')


def css_rgba(hex_colour, alpha):
    h = hex_colour.lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f'rgba({r}, {g}, {b}, {alpha})'
