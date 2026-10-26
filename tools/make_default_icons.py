"""Render Pusharr's bundled icons (static/icons) from tools/pusharr-mark.svg, with the same code the
settings page uses for uploaded icons (app/icons.py). Needs rsvg-convert and Pillow, so run it in the image:

   docker run --rm -u 0 -v "$PWD":/w -w /w ghcr.io/bjrb20/pusharr python tools/make_default_icons.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))
import icons  # noqa: E402

svg = open('tools/pusharr-mark.svg', 'rb').read()
print(icons.generate(svg, 'static/icons', mode='mark', background='#3b82f6', letter='P'))
