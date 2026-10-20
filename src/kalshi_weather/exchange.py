"""Kalshi API client: signed requests and public market data.

Keys are read from $KALSHI_KEY_DIR (default ~/.kalshi) and never printed.
"""
import base64
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

HOSTS = {'demo': 'https://external-api.demo.kalshi.co', 'prod': 'https://external-api.kalshi.com'}
PREFIX = '/trade-api/v2'


def key_dir():
    return Path(os.environ.get('KALSHI_KEY_DIR', Path.home() / '.kalshi'))


def key_file(folder, env):
    """Path of the environment's private key."""
    for name in (f'{env}_key.pem', f'{env}_ed25519.pem'):
        if (Path(folder) / name).exists():return Path(folder) / name
    raise FileNotFoundError(f'no {env}_key.pem in {folder}')


def signer(key):
    """Signing function for an Ed25519 or RSA key."""
    if isinstance(key, ed25519.Ed25519PrivateKey):return key.sign
    if isinstance(key, rsa.RSAPrivateKey):
        return lambda message: key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    raise ValueError('unsupported key type: Kalshi accepts Ed25519 or RSA')


class Client:
    def __init__(self, env='demo', keys=None):
        if env not in HOSTS:raise ValueError(f'unknown environment {env!r}')
        self.env = env; self.host = HOSTS[env]; folder = Path(keys) if keys else key_dir()
        self._sign = signer(serialization.load_pem_private_key(key_file(folder, env).read_bytes(), password=None))
        self._key_id = (folder / f'{env}_key_id').read_text().strip()

    def __repr__(self):
        return f'Client({self.env!r}, <key loaded>)'

    def request(self, method, path, params=None, body=None, timeout=20):
        full = PREFIX + path; url = self.host + full + ('?' + urllib.parse.urlencode(params) if params else '')
        stamp = str(int(time.time() * 1000)); signature = base64.b64encode(self._sign((stamp + method + full).encode())).decode()
        headers = {'KALSHI-ACCESS-KEY': self._key_id, 'KALSHI-ACCESS-TIMESTAMP': stamp, 'KALSHI-ACCESS-SIGNATURE': signature,
                   'Content-Type': 'application/json', 'Accept': 'application/json'}
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:status, text = r.status, r.read().decode()
        except urllib.error.HTTPError as e:status, text = e.code, e.read().decode()
        except (OSError, http.client.HTTPException) as e:return 0, {'error': type(e).__name__}   # network errors
        try:return status, json.loads(text) if text else {}
        except json.JSONDecodeError:return status, text

    def get(self, path, **params):
        return self.request('GET', path, params=params or None)


SPACING = 0.1; RETRIES = 4; _last = [0.]   # public reads are spaced to stay under the rate limit


def public(path, env='prod', timeout=20, **params):
    """Unauthenticated market data, retrying rate limits with backoff."""
    url = HOSTS[env] + PREFIX + path + ('?' + urllib.parse.urlencode(params, doseq=True) if params else '')
    for attempt in range(RETRIES + 1):
        time.sleep(max(0., _last[0] + SPACING - time.monotonic())); _last[0] = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == RETRIES:raise
            wait = e.headers.get('Retry-After') if e.headers else None
            time.sleep(float(wait) if wait and wait.replace('.', '', 1).isdigit() else 0.5 * 2 ** attempt)
