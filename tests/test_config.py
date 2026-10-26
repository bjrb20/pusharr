"""Config loading, validation and templating (app/config.py) and what branding.py makes from it.

   docker run --rm ghcr.io/bjrb20/pusharr python tests/test_config.py
"""
import json, os, sys, tempfile

tmp = tempfile.mkdtemp()
env_file = os.path.join(tmp, 'secrets.env')
open(env_file, 'w').write('# comment\nJELLYFIN_API_KEY="from-file"\nINTERNAL_TOKEN=tok\n')
os.environ.update(CONFIG_DIR=tmp, DATA_DIR=tmp, ENV_FILE=env_file)
os.environ.pop('JELLYFIN_API_KEY', None)
sys.path.insert(0, 'app')
import config, branding  # noqa: E402

ok_count = 0


def check(cond, what):
    global ok_count
    if not cond:
        raise AssertionError(what)
    ok_count += 1
    print('ok  ', what)


def write(text):
    open(config.CONFIG_FILE, 'w').write(text)


def problems_for(over):
    return config.validate(config.merge(config.DEFAULTS, over))


# ---------- secrets ----------
check(config.secret('JELLYFIN_API_KEY') == 'from-file' and config.secret('INTERNAL_TOKEN') == 'tok',
      'secrets read from ENV_FILE (quotes and comments handled)')
check(config.check_secrets() == ['VAPID_PUBLIC_KEY is not set (see .env.example)', 'VAPID_PRIVATE_KEY is not set (see .env.example)'],
      'missing required secrets are named')

# ---------- loading ----------
check(config.load() == [] and config.get('branding.app_name') == 'Pusharr' and config.get('timezone') == 'UTC',
      'no file: neutral defaults')
check(config.validate(config.merge(config.DEFAULTS, {})) == [], 'the defaults are valid')
import yaml  # noqa: E402
example = yaml.safe_load(open('config.example.yaml'))
check(problems_for(example) == [], 'config.example.yaml is valid')
write('branding:\n  server_name: Moviebox\n  theme: {accent: "#ff0000"}\nnotifications: {digest_hour: 8}\n')
v1 = config.version()
check(config.load() == [] and config.get('branding.server_name') == 'Moviebox' and config.get('branding.theme.accent') == '#ff0000'
      and config.get('branding.theme.bg') == config.DEFAULTS['branding']['theme']['bg'] and config.get('notifications.digest_hour') == 8,
      'file merged over the defaults, key by key')
check(config.version() != v1, 'version changes with the settings')
write('branding: {server_name: Moviebox}\nlinks: {recommended_apps: [{name: A}]}\n')
config.load()
check(config.get('links.recommended_apps') == [{'name': 'A'}], 'lists replace, not merge')
write('branding: {theme: {accent: nope}}\n')
check(config.load() == ['branding.theme.accent: not a colour like #1a2b3c'] and config.get('branding.server_name') == 'Moviebox',
      'a bad file is reported and not applied; the previous settings stay')
write('branding: [oops\n')
check(config.load()[0].startswith('could not read the config file') and config.get('branding.server_name') == 'Moviebox',
      'unreadable YAML reported')
write('- just a list\n')
check('top level must be a mapping' in config.load()[0], 'top level must be a mapping')

# ---------- validation ----------
cases = [
    ({'bogus': 1}, 'bogus: unknown setting'),
    ({'branding': {'thme': {}}}, 'branding.thme: unknown setting'),
    ({'session': {'days': 'thirty'}}, 'session.days: should be a whole number'),
    ({'session': {'days': True}}, 'session.days: should be a whole number'),
    ({'jellyfin': {'sign_in': {'password': 'yes'}}}, 'jellyfin.sign_in.password: should be true or false'),
    ({'public_url': 'http://alerts.example.org'}, 'public_url: an https:// address'),
    ({'jellyfin': {'url': ''}}, 'jellyfin.url: the address this server reaches Jellyfin at (http:// or https://)'),
    ({'jellyfin': {'sign_in': {'password': False, 'quick_connect': False}}}, 'jellyfin.sign_in: turn on password or Quick Connect (or both)'),
    ({'timezone': 'Mars/Olympus'}, "timezone: unknown time zone 'Mars/Olympus'"),
    ({'session': {'cookie_name': 'session'}}, 'session.cookie_name: must start with __Host- (letters, digits, _)'),
    ({'branding': {'fonts': {'body': 'comic-sans'}}}, 'branding.fonts.body: one of inter, bebas-neue, system or custom:<font file>'),
    ({'branding': {'fonts': {'body': 'custom:../../etc/passwd'}}}, 'branding.fonts.body: one of inter, bebas-neue, system or custom:<font file>'),
    ({'branding': {'icon_dir': '../secrets'}}, 'branding.icon_dir: a plain path inside branding/'),
    ({'branding': {'wordmark': {'style': 'image'}}}, 'branding.wordmark.image: upload an image, or use the text style'),
    ({'branding': {'app_name': '  '}}, 'branding.app_name: must not be empty'),
    ({'links': {'watch': 'javascript:alert(1)'}}, 'links.watch: an https:// address (or empty)'),
    ({'links': {'recommended_apps': [{'name': 'X', 'stores': {'ios': 'http://x'}}]}},
     'links.recommended_apps[0].stores.ios: an https:// address'),
    ({'links': {'recommended_apps': [{'name': 'X', 'stores': {'amiga': 'https://x'}}]}},
     'links.recommended_apps[0].stores.amiga: one of ios, android, windows, android_tv, other'),
    ({'links': {'recommended_apps': [{'name': 'X', 'play': {'movie': 'x://{imdb}'}}]}},
     'links.recommended_apps[0].play.movie: unknown variable(s) imdb'),
    ({'links': {'recommended_apps': [{'description': 'no name'}]}}, 'links.recommended_apps[0]: needs at least a name'),
    ({'links': {'alternative_apps': [{'name': 'X', 'url': 'ftp://x'}]}}, 'links.alternative_apps[0]: needs a name and an https:// url'),
    ({'links': {'watch_item': '{jellyfin}/{item}'}}, 'links.watch_item: unknown variable(s) item'),
    ({'notifications': {'digest_hour': 24}}, 'notifications.digest_hour: 0 to 23'),
    ({'approvals': {'approvers': 'everyone'}}, "approvals.approvers: 'admins' or a list of Jellyfin usernames"),
    ({'approvals': {'picker': {'dub_tags': ['(unclosed']}}}, "approvals.picker.dub_tags: '(unclosed' is not a valid pattern"),
    ({'approvals': {'picker': {'languages': []}}}, 'approvals.picker.languages: at least one language'),
    ({'messages': {'nope': 'x'}}, 'messages.nope: unknown message'),
    ({'messages': {'test': 'just text'}}, 'messages.test: should be title + body'),
    ({'messages': {'progress_next': {'title': 'x'}}}, 'messages.progress_next: should be one piece of text'),
    ({'messages': {'test': {'title': 'Hi {name}'}}}, 'messages.test.title: unknown variable(s) {name}'),
    ({'messages': {'test': {'title': 'Hi {user'}}}, 'messages.test.title: bad template'),
    ({'server': {'public_port': 70000}}, 'server.public_port: not a port number'),
    ({'links': {'recommended_apps': [{'name': 'X', 'play': {'item': 'x://play?tmdb={tmdb}'}}]}},
     'links.recommended_apps[0].play.item: unknown variable(s) tmdb'),
    ({'notifications': {'wait_for_ready': {'check_every': 5}}}, 'notifications.wait_for_ready: check_every at least 10 s'),
    ({'notifications': {'wait_for_ready': {'max_wait_hours': 100}}}, 'notifications.wait_for_ready: refresh_after not negative'),
]
for over, want in cases:
    got = problems_for(over)
    check(any(p.startswith(want) for p in got), f'refused: {want}' + ('' if any(p.startswith(want) for p in got) else f' (got {got})'))
check(problems_for({'approvals': {'approvers': ['alex', 'sam']}}) == [], 'approvers can be a list of usernames')
check(problems_for({'links': {'recommended_apps': [{'name': 'X', 'play': {'item': 'x://play?server={server_id}&id={item_id}'}}]}}) == [],
      'exact-item app links take {server_id} and {item_id}')
check(problems_for({'branding': {'fonts': {'wordmark': 'custom:My-Font.woff2'}}}) == [], 'custom fonts allowed')

# ---------- saving ----------
write('branding: {server_name: Moviebox}\n')
config.load()
check(config.save({'branding': {'theme': {'accent': 'bad'}}}) != [] and not os.path.exists(config.CONFIG_FILE + '.bak'),
      'save refuses bad settings and writes nothing')
conf = config.merge(config.current(), {'branding': {'server_name': 'Cinema'}, 'links': {'request_name': 'Jellyseerr'}})
check(config.save(conf) == [] and config.get('branding.server_name') == 'Cinema', 'save applies straight away')
saved = yaml.safe_load(open(config.CONFIG_FILE))
check(saved == {'branding': {'server_name': 'Cinema'}, 'links': {'request_name': 'Jellyseerr'}}
      and os.path.exists(config.CONFIG_FILE + '.bak'), 'only differences from the defaults are written; old file kept')
check('Moviebox' not in open(config.CONFIG_FILE).read(), 'nothing stale left in the file')

# ---------- templates ----------
check(config.msg('episode_one', series='Tracker', episode='S4E4', progress="You're up to S4E3.")
      == ('📺 Tracker S4E4 is ready on Cinema', "You're up to S4E3."), 'message template filled in')
check(config.fill('{server} / {app} / {request_name} / {missing} / {x}', x=None) == 'Cinema / Pusharr / Jellyseerr / {missing} / ',
      'common variables; unknown placeholders left alone; None becomes empty')
check(config.fill('{0} {x.y} {', x='a') in ('{0} {x.y} {',), 'odd templates never raise')
config.current()['messages']['test'] = {'title': '👋 {user} on {server}', 'body': 'Ping'}
check(config.msg('test', user='Alex') == ('👋 Alex on Cinema', 'Ping'), 'edited template used')
check(config.link('watch_item', item_id='abc', server_id='s1') is None, 'item link needs jellyfin.public_url')
config.current()['jellyfin']['public_url'] = 'https://jf.example.org/'
check(config.link('watch_item', item_id='abc', server_id='s1') == 'https://jf.example.org/web/#/details?id=abc&serverId=s1'
      and config.link('watch_home') == 'https://jf.example.org/web/', 'item and library links')
check(config.link('request_item', media='tv', tmdb=1) is None, 'request link needs links.request')
config.current()['links']['request'] = 'https://req.example.org'
check(config.link('request_item', media='tv', tmdb=1399) == 'https://req.example.org/tv/1399', 'request page link')

# ---------- branding ----------
css = branding.theme_css()
check('--accent: #3b82f6;' in css and '--panel-2:' in css and '--font-body: "Inter"' in css and '--wordmark-glow: none;' in css,
      'theme CSS from the config')
config.current()['branding']['wordmark']['glow'] = True
config.current()['branding']['theme']['accent'] = '#E50914'
config.current()['branding']['fonts']['wordmark'] = 'bebas-neue'
css = branding.theme_css()
check('--wordmark-glow: 0 2px 12px rgba(229, 9, 20, .35);' in css and css.count('@font-face') == 2
      and '--font-wordmark: "Bebas Neue"' in css, 'glow from the accent colour; second font loaded')
m = branding.manifest()
check(m['name'] == 'Pusharr' and m['description'] == 'Notifications for new episodes, films and requests on Cinema.'
      and m['icons'][0]['src'] == '/icons/icon-192.png' and 'related_applications' not in m, 'manifest from the config')
config.current()['branding']['icon_dir'] = 'icons-abc123'
check(branding.manifest()['icons'][3]['src'] == '/brand/icons-abc123/maskable-512.png', 'uploaded icon set used in the manifest')
config.current()['branding']['app_name'] = 'Tom & Jerry <TV>'
v = branding.page_vars()
check(v['app'] == 'Tom &amp; Jerry &lt;TV&gt;' and '<h1 class="wordmark">PUSHARR</h1>' in v['wordmark'], 'page text is HTML-escaped')
check(branding.fill_page("const APP = '{{app}}'; x = '{{nope}}';", js=True) == "const APP = 'Tom & Jerry <TV>'; x = '{{nope}}';",
      'JS placeholders get raw (JSON-escaped) text; unknown ones are left')
check([branding.hour_label(h) for h in (0, 7, 12, 19)] == ['midnight', '7am', 'noon', '7pm'], 'digest hour labels')
check(branding.follow_text() == '"New episodes" covers shows you\'ve watched in the last 60 days, are caught up on, '
      'have on your Jellyseerr watchlist or requested.', 'follow rules described')
config.current()['integrations']['seerr']['enabled'] = False
config.current()['notifications']['follow']['caught_up'] = False
check(branding.follow_text() == '"New episodes" covers shows you\'ve watched in the last 60 days.', '… only the rules in use')
check(branding.brand_file('../config.yaml') is None and branding.brand_file('x/../../etc/passwd') is None
      and branding.brand_file('missing.png') is None, 'branding files: no escaping the folder')

print(f'\nall {ok_count} config checks passed')
