"""The look of the app, from branding.* in the config: theme CSS, web app manifest, icon URLs, the
wordmark and the text placeholders ({{server}}, {{app}}, ...) filled into the static pages.

Uploads from the settings page live in CONFIG_DIR/branding and are served at /brand/<path>:
  icons-<hash>/   a full icon set made from one SVG (icons.py); a new upload gets a new folder, so
                  installed apps fetch the new icons
  fonts/          custom fonts (woff2 / ttf / otf)
  wordmark-<hash>.png  a wordmark image (SVG uploads are rasterised)
"""
import hashlib
import html
import json
import os
import re
import subprocess
import tempfile

import config

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'static')
ICON_FILES = ('icon-192.png', 'icon-512.png', 'maskable-192.png', 'maskable-512.png', 'apple-touch-180.png',
              'favicon-32.png', 'favicon.ico', 'badge-96.png')
BRAND_TYPES = {'.png': 'image/png', '.webp': 'image/webp', '.ico': 'image/x-icon', '.woff2': 'font/woff2',
               '.ttf': 'font/ttf', '.otf': 'font/otf'}
MAX_UPLOAD = 2 * 1024 * 1024


def icon_url(name):
    d = config.get('branding.icon_dir')
    return f'/brand/{d.strip("/")}/{name}' if d else f'/icons/{name}'


def brand_file(rel):
    """Absolute path of a file under CONFIG_DIR/branding, or None if it's outside / missing / not servable."""
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_./-]*', rel or '') or '..' in rel:
        return None
    base = os.path.realpath(config.BRAND_DIR)
    f = os.path.realpath(os.path.join(base, rel))
    if not f.startswith(base + os.sep) or not os.path.isfile(f) or os.path.splitext(f)[1].lower() not in BRAND_TYPES:
        return None
    return f


# ---------- fonts ----------

def font_face(key, css_name):
    """(@font-face rule or '', CSS font-family stack) for a font setting."""
    fallback = '"Helvetica Neue", Arial, system-ui, sans-serif'
    if key.startswith('custom:'):
        f = key.split(':', 1)[1]
        fmt = {'woff2': 'woff2', 'ttf': 'truetype', 'otf': 'opentype'}[f.rsplit('.', 1)[1]]
        return (f'@font-face {{ font-family: "{css_name}"; src: url("/brand/fonts/{f}") format("{fmt}"); '
                f'font-display: swap; }}\n', f'"{css_name}", {fallback}')
    family, file, weights, _, fallbacks = config.FONTS[key]
    if not family:
        return '', 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'
    return (f'@font-face {{ font-family: "{family}"; src: url("/fonts/{file}") format("woff2"); '
            f'font-weight: {weights}; font-style: normal; font-display: swap; }}\n', f'"{family}", {fallbacks}')


def font_preloads():
    out = []
    for key in {config.get('branding.fonts.body'), config.get('branding.fonts.wordmark')}:
        if key.startswith('custom:'):
            f = key.split(':', 1)[1]
            out.append((f'/brand/fonts/{f}', 'font/' + {'woff2': 'woff2', 'ttf': 'ttf', 'otf': 'otf'}[f.rsplit('.', 1)[1]]))
        elif config.FONTS[key][1]:
            out.append((f'/fonts/{config.FONTS[key][1]}', 'font/woff2'))
    return sorted(out)


def theme_css():
    t = config.get('branding.theme')
    w = config.get('branding.wordmark')
    body_face, body_stack = font_face(config.get('branding.fonts.body'), 'Body')
    mark_face, mark_stack = font_face(config.get('branding.fonts.wordmark'), 'Wordmark')
    css = '/* generated from branding.* in config.yaml */\n' + body_face
    if mark_face != body_face:
        css += mark_face
    css += ':root {\n' + ''.join(f'  --{k.replace("_", "-")}: {v};\n' for k, v in t.items())
    css += (f'  --font-body: {body_stack};\n  --font-wordmark: {mark_stack};\n'
            f'  --wordmark-weight: {w["weight"]};\n  --wordmark-spacing: {w["letter_spacing"]};\n'
            f'  --wordmark-glow: {"0 2px 12px " + config.css_rgba(t["accent"], ".35") if w["glow"] else "none"};\n'
            f'  --focus: {t["accent"]};\n}}\n')
    return css


# ---------- manifest + page placeholders ----------

def manifest():
    b = config.get('branding')
    public = (config.get('public_url') or '').rstrip('/')
    m = {
        'name': b['app_name'], 'short_name': b['short_name'],
        'description': config.fill(b['description']),
        'id': '/', 'start_url': '/', 'scope': '/', 'display': 'standalone',
        'background_color': b['theme']['background_color'], 'theme_color': b['theme']['theme_color'],
        'icons': [{'src': icon_url(f'{kind}-{px}.png'), 'sizes': f'{px}x{px}', 'type': 'image/png',
                   'purpose': 'maskable' if kind == 'maskable' else 'any'}
                  for kind in ('icon', 'maskable') for px in (192, 512)],
        'version': config.version(),
    }
    if public:      # lets Chrome's getInstalledRelatedApps() tell a browser tab that the app is installed
        m['related_applications'] = [{'platform': 'webapp', 'url': f'{public}/manifest.webmanifest'}]
    return m


def hour_label(h):
    return {0: 'midnight', 12: 'noon'}.get(h) or f'{h % 12}{"am" if h < 12 else "pm"}'


def follow_text():
    f = config.get('notifications.follow')
    seerr = config.get('integrations.seerr.enabled')
    parts = [f"you've watched in the last {f['recent_watch_days']} days"]
    if f['caught_up']:
        parts.append('are caught up on')
    if f['watchlist'] and seerr:
        parts.append(f"have on your {config.get('links.request_name')} watchlist")
    if f['requested'] and seerr:
        parts.append('requested')
    joined = parts[0] if len(parts) == 1 else ', '.join(parts[:-1]) + ' or ' + parts[-1]
    return f'"New episodes" covers shows {joined}.'


def wordmark_html():
    b = config.get('branding')
    w = b['wordmark']
    if w['style'] == 'image' and brand_file(w['image']):
        inner = f'<img src="/brand/{html.escape(w["image"])}" alt="{html.escape(b["app_name"])}">'
    else:
        inner = html.escape(w['text'])
    sub = f'<div class="sub">{html.escape(w["subtitle"])}</div>' if w['subtitle'] else ''
    return f'<h1 class="wordmark">{inner}</h1>{sub}'


def page_vars():
    """{{name}} -> HTML-safe text for the static pages."""
    b = config.get('branding')
    e = html.escape
    v = {
        'app': e(b['app_name']), 'short': e(b['short_name']), 'server': e(b['server_name']),
        'request_name': e(config.get('links.request_name')),
        'theme_color': e(b['theme']['theme_color']),
        'digest_label': e('Daily at ' + hour_label(config.get('notifications.digest_hour'))),
        'follow_text': e(follow_text()),
        'wordmark': wordmark_html(),
        'version': config.version(),
        'password': '1' if config.get('jellyfin.sign_in.password') else '',
        'quick_connect': '1' if config.get('jellyfin.sign_in.quick_connect') else '',
        'preloads': ''.join(f'<link rel="preload" href="{u}" as="font" type="{t}" crossorigin>\n  '
                            for u, t in font_preloads()).rstrip(),
    }
    for name in ICON_FILES:
        v['icon:' + name] = icon_url(name)
    return v


def fill_page(text, js=False):
    """Fill {{name}} placeholders. In JS the values go in as JSON string bodies (no HTML escaping)."""
    v = page_vars()
    if js:
        b = config.get('branding')
        raw = {'app': b['app_name'], 'server': b['server_name'], 'version': config.version(),
               'request_name': config.get('links.request_name')}
        raw.update({k: v[k] for k in v if k.startswith('icon:')})
        return re.sub(r'\{\{([a-z_:.0-9-]+)\}\}', lambda m: json.dumps(raw.get(m.group(1), m.group(0)))[1:-1], text)
    return re.sub(r'\{\{([a-z_:.0-9-]+)\}\}', lambda m: v.get(m.group(1), m.group(0)), text)


# ---------- uploads (settings page) ----------

def _save(rel, data):
    path = os.path.join(config.BRAND_DIR, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + '.tmp', 'wb') as f:
        f.write(data)
    os.replace(path + '.tmp', path)
    return rel


def upload_font(name, data):
    ext = os.path.splitext(name or '')[1].lower()
    magic = {'.woff2': (b'wOF2',), '.ttf': (b'\x00\x01\x00\x00', b'true'), '.otf': (b'OTTO',)}
    if ext not in magic or not data.startswith(magic[ext]):
        raise ValueError('a .woff2, .ttf or .otf font file')
    base = re.sub(r'[^A-Za-z0-9_-]+', '-', os.path.splitext(os.path.basename(name))[0]).strip('-')[:40] or 'font'
    return 'custom:' + _save(f'fonts/{base}{ext}', data).split('/', 1)[1]


def rasterise_svg(svg, height):
    with tempfile.TemporaryDirectory() as d:
        src, out = os.path.join(d, 'in.svg'), os.path.join(d, 'out.png')
        open(src, 'wb').write(svg)
        subprocess.run(['rsvg-convert', '-h', str(height), '--keep-aspect-ratio', src, '-o', out],
                       check=True, capture_output=True, timeout=60)
        return open(out, 'rb').read()


def upload_wordmark(name, data):
    ext = os.path.splitext(name or '')[1].lower()
    if ext == '.svg' or data.lstrip()[:5] in (b'<?xml', b'<svg ') or data.lstrip().startswith(b'<svg'):
        data = rasterise_svg(data, 192)
    elif not (data.startswith(b'\x89PNG') or data[8:12] == b'WEBP'):
        raise ValueError('an SVG, PNG or WebP image')
    ext = '.webp' if data[8:12] == b'WEBP' else '.png'
    return _save(f'wordmark-{hashlib.sha256(data).hexdigest()[:8]}{ext}', data)


def upload_icon(data, mode='mark', background='#000000', letter=None):
    """Make a full icon set from one SVG into branding/icons-<hash>/; returns that folder (for
    branding.icon_dir)."""
    import icons
    if not (data.lstrip().startswith(b'<svg') or data.lstrip().startswith(b'<?xml')):
        raise ValueError('an SVG file')
    if mode not in ('mark', 'full'):
        raise ValueError('mode: mark or full')
    if not config.HEX.match(background or ''):
        raise ValueError('background: a colour like #000000')
    letter = (letter or config.get('branding.short_name')[:1]).strip()[:1] or 'P'
    folder = f'icons-{hashlib.sha256(data + mode.encode() + background.encode() + letter.encode()).hexdigest()[:8]}'
    out = os.path.join(config.BRAND_DIR, folder)
    icons.generate(data, out, mode=mode, background=background, letter=letter, font=badge_font())
    return folder


def badge_font():
    """A TTF/OTF for the letter badge: the wordmark font if it has one, else the body font, else None."""
    for key in (config.get('branding.fonts.wordmark'), config.get('branding.fonts.body')):
        if key.startswith('custom:') and not key.endswith('.woff2'):
            return os.path.join(config.BRAND_DIR, 'fonts', key.split(':', 1)[1])
        if key in config.FONTS and config.FONTS[key][3]:
            return os.path.join(STATIC, 'fonts', config.FONTS[key][3])
    return None


def custom_fonts():
    d = os.path.join(config.BRAND_DIR, 'fonts')
    return sorted('custom:' + f for f in os.listdir(d) if re.search(r'\.(woff2|ttf|otf)$', f)) if os.path.isdir(d) else []


def icon_dirs():
    d = config.BRAND_DIR
    return sorted(f for f in os.listdir(d) if os.path.isdir(os.path.join(d, f))
                  and os.path.isfile(os.path.join(d, f, 'icon-192.png'))) if os.path.isdir(d) else []
