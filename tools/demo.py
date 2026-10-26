"""Try Pusharr without a media server: a fake Jellyfin (sign in as demo / demo, an admin) and the app with
the neutral example settings, on http://localhost:8095. Nothing is sent anywhere; integrations are off.

   docker run --rm -p 127.0.0.1:8095:8095 ghcr.io/bjrb20/pusharr python tools/demo.py

Used for the README screenshots.
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get('DEMO_PORT', '8095'))
USER = {'Id': 'd' * 32, 'Name': 'demo', 'Policy': {'IsAdministrator': True}}
SHOWS = [('Tracker', '428433'), ('Severance', '371980'), ('The Bear', '403245'), ('Slow Horses', '386479')]


class FakeJellyfin(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b''
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
        if self.path == '/Users/AuthenticateByName':
            ok = data.get('Username', '').lower() == 'demo' and data.get('Pw') == 'demo'
            return self.reply(200, {'AccessToken': 'demo', 'User': USER}) if ok else self.reply(401)
        if self.path == '/QuickConnect/Initiate':
            return self.reply(200, {'Secret': 'demo-secret', 'Code': '424242'})
        self.reply(204)

    def do_GET(self):
        if self.path == '/System/Info/Public':
            return self.reply(200, {'Id': 'demo-server'})
        if self.path.startswith('/QuickConnect/Connect'):
            return self.reply(200, {'Authenticated': False})
        if self.path.startswith('/Users/'):
            return self.reply(200, USER)
        if self.path.startswith('/Items'):
            return self.reply(200, {'Items': [{'Id': f'{i:032d}', 'Name': n, 'ProviderIds': {'Tvdb': t}}
                                              for i, (n, t) in enumerate(SHOWS)]})
        self.reply(404)


def main():
    jf = ThreadingHTTPServer(('127.0.0.1', 0), FakeJellyfin)
    threading.Thread(target=jf.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp()
    here = os.path.dirname(os.path.abspath(__file__))
    env = os.path.join(tmp, 'demo.env')
    os.system(f'{sys.executable} {here}/gen_vapid.py {env} >/dev/null')
    with open(env, 'a') as f:
        f.write('JELLYFIN_API_KEY=demo\nINTERNAL_TOKEN=demo\n')
    import yaml
    conf = yaml.safe_load(open(os.path.join(here, '..', 'config.example.yaml')))
    conf['public_url'] = ''
    conf['extra_origins'] = [f'http://localhost:{PORT}']
    conf['jellyfin']['url'] = f'http://127.0.0.1:{jf.server_port}'
    conf['server'] = {'public_port': PORT, 'internal_port': 0, 'webhook_port': 0}
    for k in conf['integrations']:
        conf['integrations'][k]['enabled'] = False
    conf['links']['recommended_apps'] = [{
        'name': 'Your favourite app', 'description': 'Watch on your phone, tablet, computer or TV.',
        'stores': {'ios': 'https://example.org/ios', 'android': 'https://example.org/android', 'other': 'https://example.org'},
        'play': {'movie': 'yourapp://play?tmdb={tmdb}', 'show': 'yourapp://play?tvdb={tvdb}'},
        'play_on': ['android', 'ios', 'mac', 'windows', 'linux', 'other']}]
    open(os.path.join(tmp, 'config.yaml'), 'w').write(yaml.safe_dump(conf))
    os.environ.update(CONFIG_DIR=tmp, DATA_DIR=tmp, ENV_FILE=env)
    sys.path.insert(0, os.path.join(here, '..', 'app'))
    import core
    import server
    core.init_db()
    core.ensure_user(USER['Id'], 'demo', True)
    core.db().execute('insert or ignore into notes(id, jf_id, created, category, title, body, watch_url, ids) '
                      'values(?,?,?,?,?,?,?,?)',
                      ('demo-note-1', USER['Id'], core.now() - 300, 'episodes', '📺 Tracker S4E4 is ready on Jellyfin',
                       "You're up to S4E3.", 'https://jellyfin.example.org/web/',
                       json.dumps({'type': 'show', 'tvdb': 428433, 'tmdb': 61818})))
    print(f'Pusharr demo on http://localhost:{PORT}  (sign in as demo / demo)', flush=True)
    server.main()


if __name__ == '__main__':
    main()
