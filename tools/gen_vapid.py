"""Create VAPID keys for web push and append them to an env file (created chmod 600).
Prints only the public key, never the private one.

   python tools/gen_vapid.py /path/to/alerts.env
"""
import base64, os, sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

path = sys.argv[1]
existing = open(path).read() if os.path.exists(path) else ''
if 'VAPID_PRIVATE_KEY=' in existing:
    sys.exit(f'{path} already has VAPID keys; not replacing them (every device would have to re-subscribe)')
key = ec.generate_private_key(ec.SECP256R1())
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b'=').decode()
private = b64(key.private_numbers().private_value.to_bytes(32, 'big'))
public = b64(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
os.umask(0o077)
with open(path, 'a') as f:
    f.write(('' if existing.endswith('\n') or not existing else '\n') +
            f'VAPID_PUBLIC_KEY={public}\nVAPID_PRIVATE_KEY={private}\n')
os.chmod(path, 0o600)
print('VAPID keys added to', path, '- public key:', public)
