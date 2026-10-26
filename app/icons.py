"""Make every icon a PWA needs from one SVG (rsvg-convert renders it, Pillow composes the sizes).

mode 'mark': the SVG is a symbol on a transparent background. It's placed on `background`:
             rounded square (any-purpose icons), full bleed with the symbol inside the 80 % safe zone
             (maskable), opaque square (Apple touch icon) and a small rounded square (favicon). The
             monochrome badge (Android's status bar) is the symbol's silhouette in white.
mode 'full': the SVG is the whole icon, background included. It's used as it is; the maskable icons
             put it, scaled down, on `background`; the badge is a white letter (the app's initial).

   generate(svg_bytes, out_dir, mode='mark', background='#000000', letter='P', font=None)
Writes icon-192/512.png, maskable-192/512.png, apple-touch-180.png, favicon-32.png, favicon.ico,
badge-96.png and source.svg into out_dir.
"""
import io
import os
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFont

MASTER = 1024


def render(svg, px=MASTER):
    """The SVG rasterised into a px x px RGBA image (aspect kept, centred)."""
    with tempfile.TemporaryDirectory() as d:
        src, out = os.path.join(d, 'in.svg'), os.path.join(d, 'out.png')
        open(src, 'wb').write(svg)
        subprocess.run(['rsvg-convert', '-w', str(px), '-h', str(px), '--keep-aspect-ratio', src, '-o', out],
                       check=True, capture_output=True, timeout=60)
        img = Image.open(out).convert('RGBA')
    canvas = Image.new('RGBA', (px, px), (0, 0, 0, 0))
    canvas.paste(img, ((px - img.width) // 2, (px - img.height) // 2), img)
    return canvas


def rgb(hex_colour):
    h = hex_colour.lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def tile(size, background, radius):
    """A background square with rounded corners (radius as a fraction of the size)."""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * radius),
                                          fill=rgb(background) + (255,))
    return img


def place(base, art, scale):
    """Paste `art` (square RGBA) onto `base`, scaled to `scale` of its width and centred."""
    s = int(base.width * scale)
    a = art.resize((s, s), Image.LANCZOS)
    out = base.copy()
    out.alpha_composite(a, ((base.width - s) // 2, (base.height - s) // 2))
    return out


def silhouette(art, size=96, fill=0.82):
    """White shape from the art's alpha, trimmed and centred: the monochrome badge."""
    alpha = art.getchannel('A')
    box = alpha.getbbox() or (0, 0, art.width, art.height)
    alpha = alpha.crop(box)
    s = max(alpha.size)
    sq = Image.new('L', (s, s), 0)
    sq.paste(alpha, ((s - alpha.width) // 2, (s - alpha.height) // 2))
    inner = int(size * fill)
    sq = sq.resize((inner, inner), Image.LANCZOS)
    out = Image.new('RGBA', (size, size), (255, 255, 255, 0))
    mask = Image.new('L', (size, size), 0)
    mask.paste(sq, ((size - inner) // 2, (size - inner) // 2))
    out.putalpha(mask)
    return out


def letter_badge(letter, font=None, size=96, glyph_h=0.74):
    """A flat white letter on transparent, centred on its ink."""
    ss = 4
    big = size * ss
    img = Image.new('RGBA', (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def face(px):
        if font and os.path.exists(font):
            return ImageFont.truetype(font, px)
        return ImageFont.load_default(size=px)
    px = int(big * glyph_h)
    for _ in range(4):
        x0, y0, x1, y1 = d.textbbox((0, 0), letter, font=face(px), anchor='ls')
        px = max(8, int(px * big * glyph_h / max(1, y1 - y0)))
    f = face(px)
    x0, y0, x1, y1 = d.textbbox((0, 0), letter, font=f, anchor='ls')
    d.text(((big - (x1 - x0)) / 2 - x0, (big - (y1 - y0)) / 2 - y0), letter, font=f, fill=(255, 255, 255, 255),
           anchor='ls')
    return img.resize((size, size), Image.LANCZOS)


def generate(svg, out_dir, mode='mark', background='#000000', letter='P', font=None):
    art = render(svg)
    os.makedirs(out_dir, exist_ok=True)
    out = {}
    if mode == 'mark':
        for px in (192, 512):
            out[f'icon-{px}.png'] = place(tile(px, background, 0.22), art, 0.70)
            out[f'maskable-{px}.png'] = place(tile(px, background, 0), art, 0.56)
        out['apple-touch-180.png'] = place(tile(180, background, 0), art, 0.70).convert('RGB')
        fav = place(tile(256, background, 0.18), art, 0.80)
        out['badge-96.png'] = silhouette(art)
    else:
        for px in (192, 512):
            out[f'icon-{px}.png'] = art.resize((px, px), Image.LANCZOS)
            out[f'maskable-{px}.png'] = place(tile(px, background, 0), art, 0.80)
        apple = tile(180, background, 0)
        apple.alpha_composite(art.resize((180, 180), Image.LANCZOS))
        out['apple-touch-180.png'] = apple.convert('RGB')
        fav = art.resize((256, 256), Image.LANCZOS)
        out['badge-96.png'] = letter_badge(letter, font)
    out['favicon-32.png'] = fav.resize((32, 32), Image.LANCZOS)
    for name, img in out.items():
        img.save(os.path.join(out_dir, name))
    buf = io.BytesIO()
    fav.save(buf, format='ICO', sizes=[(16, 16), (32, 32), (48, 48)])
    open(os.path.join(out_dir, 'favicon.ico'), 'wb').write(buf.getvalue())
    open(os.path.join(out_dir, 'source.svg'), 'wb').write(svg)
    return sorted(os.listdir(out_dir))
