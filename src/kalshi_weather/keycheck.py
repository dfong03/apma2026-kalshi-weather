"""Check, and with --fix repair, a Kalshi API key on disk without printing it.

Usage: kalshi-weather key check prod|demo [--dir DIR] [--fix] [--live]
"""
import os
import re
import stat
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

LAYOUT = re.compile(rb'-----BEGIN [A-Z ]+-----\n(?:[A-Za-z0-9+/=]{1,64}\n)+-----END [A-Z ]+-----\n?')
UUID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


def _report(failures, label, ok, detail=''):
    print(f"{'ok  ' if ok else 'FAIL'} {label}{': ' + detail if detail else ''}", flush=True)
    if not ok:failures.append(label)


def write_private(path, data):
    """Write a file atomically with mode 600."""
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as handle:handle.write(data)
    os.replace(tmp, path); os.chmod(path, 0o600)


def candidates(raw):
    """Possible repairs of a pasted key, as PEM bytes, most faithful first."""
    yield raw
    text = raw.decode('utf-8', errors='ignore').replace('\\n', '\n').replace('\r', '').strip().strip('"\'').strip()
    kinds = re.findall(r'-----BEGIN ([A-Z ]+)-----', text) or ['PRIVATE KEY', 'RSA PRIVATE KEY']
    body = re.sub(r'\s+', '', re.sub(r'-----(BEGIN|END) [A-Z ]+-----', '', text))
    for kind in dict.fromkeys(kinds):
        yield (f'-----BEGIN {kind}-----\n' + '\n'.join(body[i:i + 64] for i in range(0, len(body), 64)) + f'\n-----END {kind}-----\n').encode()


def check(env, folder=None, fix=False, live=False):
    """Run the checks; returns 0 if all pass, else 1."""
    folder = Path(folder or os.environ.get('KALSHI_KEY_DIR') or Path.home() / '.kalshi'); failures = []

    def report(*a):_report(failures, *a)

    if not folder.is_dir():report('key folder exists', False, str(folder)); return 1
    mode = stat.S_IMODE(folder.stat().st_mode)
    if mode != 0o700 and fix:os.chmod(folder, 0o700); mode = 0o700
    report('key folder is private (700)', mode == 0o700, oct(mode))

    key_path = next((folder / n for n in (f'{env}_key.pem', f'{env}_ed25519.pem') if (folder / n).exists()), None)
    id_path = folder / f'{env}_key_id'
    if key_path is None:report('private key file present', False, f'expected {env}_key.pem in {folder}'); return 1
    for path in (key_path, id_path):
        if not path.exists():report(f'{path.name} present', False); continue
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o600 and fix:os.chmod(path, 0o600); mode = 0o600
        report(f'{path.name} is private (600)', mode == 0o600, oct(mode))

    raw = key_path.read_bytes(); key = None; used = None
    for attempt, pem in enumerate(candidates(raw)):
        try:key = serialization.load_pem_private_key(pem, password=None); used = attempt; break
        except Exception:continue   # the parser's message may quote the key
    report('private key parses', key is not None, f'{len(raw)} bytes, {raw.count(chr(10).encode())} line breaks as stored')
    if key is None:return 1
    standard = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    if used or not LAYOUT.fullmatch(raw):   # parsed only after repair
        lines = standard.count(b'\n')
        if fix:write_private(key_path, standard); report('rewrote the key file as standard PKCS#8 PEM', True, f'{lines} lines')
        else:report('key file is in standard PEM layout', False, 'it needed repairing to parse; rerun with --fix to rewrite it')
    kind = 'Ed25519' if isinstance(key, ed25519.Ed25519PrivateKey) else f'RSA {key.key_size}' if isinstance(key, rsa.RSAPrivateKey) else type(key).__name__
    report('key type is one Kalshi accepts', kind == 'Ed25519' or kind.startswith('RSA'), kind)

    message = b'1700000000000GET/trade-api/v2/portfolio/balance'
    try:
        if isinstance(key, ed25519.Ed25519PrivateKey):key.public_key().verify(key.sign(message), message)
        else:
            pss = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH)
            key.public_key().verify(key.sign(message, pss, hashes.SHA256()), message, pss, hashes.SHA256())
        report('signs and verifies a sample request locally', True)
    except Exception:report('signs and verifies a sample request locally', False)

    if id_path.exists():
        text = id_path.read_text(errors='ignore'); token = text.strip().strip('"\'').strip()
        shaped = bool(UUID.match(token))
        if shaped and token != text and fix:write_private(id_path, token.encode()); report('cleaned the key ID file', True, 'removed surrounding whitespace or quotes')
        report('key ID is one UUID-shaped token', shaped and (token == text or fix), f'{len(text)} characters stored')

    if live and not failures:
        from .exchange import Client
        client = Client(env, folder); key_id = id_path.read_text().strip()
        status, reply = client.get('/portfolio/balance')
        report('Kalshi accepts the key (balance read)', status == 200, f'HTTP {status}' + (f", balance ${float(reply.get('balance_dollars', reply.get('balance', 0) / 100)):.2f}" if status == 200 else ''))
        status, reply = client.get('/api_keys')
        mine = next((k for k in (reply.get('api_keys') or []) if k.get('api_key_id') == key_id), None) if status == 200 else None
        status, bal = client.get('/portfolio/subaccounts/balances')
        balances = (bal.get('subaccount_balances') or []) if status == 200 else []
        for b in balances:print(f"     balance on shard {b.get('exchange_index')}, subaccount {b.get('subaccount_number')}: ${float(b.get('balance') or 0):.2f}")
        if mine is not None:
            sub = mine.get('subaccount'); others = {b.get('subaccount_number') for b in balances} - {0}
            if sub is not None:report('key is restricted to one subaccount', True, f'subaccount {sub}')
            elif not others:print('note key is not restricted, but the account has no other subaccounts, so it reaches the same money either way')
            else:report('key is restricted to one subaccount', False, f'unrestricted, and the account has subaccounts {sorted(others)}')
            print(f"     scopes: {', '.join(mine.get('scopes') or ['(default: read, write)'])}")
        else:report('key listed under /api_keys', False, f'HTTP {status}' + ('' if status != 200 else '; not found by ID'))
        status, reply = client.get('/portfolio/subaccounts/netting')
        if status == 200:
            configs = reply.get('netting_configs') or []
            want = mine.get('subaccount') if mine else None
            for c in configs:
                if want is None or c.get('subaccount_number') == want:print(f"     netting on shard {c.get('exchange_index')}, subaccount {c.get('subaccount_number')}: {'on' if c.get('enabled') else 'OFF'}")
        else:print(f'     netting setting not readable (HTTP {status})')
    print('all checks passed' if not failures else f'{len(failures)} check(s) failed: ' + '; '.join(failures))
    return 0 if not failures else 1

