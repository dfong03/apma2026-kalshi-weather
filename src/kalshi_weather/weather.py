"""Central Park's running maximum from the National Weather Service, for the guard."""
from datetime import datetime, timezone
import json
import math
import re
import time
import urllib.request

NWS = 'https://api.weather.gov/stations/KNYC/observations'
GUARDED = {'KXHIGHNY'}   # series with a guard (Central Park)
AGENT = 'apma2026-kalshi-weather (Columbia APMA seminar research)'
SIX = re.compile(r'^1([01])(\d{3})$'); TGROUP = re.compile(r'^T([01])(\d{3})([01])(\d{3})$')


def ts(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp() if text else None


whole = lambda f: math.floor(f + .5)


def reading(when, temp_c, metar):
    """Lower bound on the day's maximum from one observation."""
    best = -math.inf; rmk = (metar or '').split(' RMK ')[-1].split() if metar and ' RMK ' in metar else []
    for tok in rmk:
        if (m := TGROUP.fullmatch(tok)):temp_c = (-1 if m[1] == '1' else 1) * int(m[2]) / 10
    if temp_c is not None:best = whole(temp_c * 1.8 + 32)
    if datetime.fromtimestamp(when, timezone.utc).hour != 5:
        for tok in rmk:
            if (m := SIX.fullmatch(tok)):best = max(best, whole((-1 if m[1] == '1' else 1) * int(m[2]) / 10 * 1.8 + 32))
    return best


def floor_since(observations, start):
    """Running maximum (whole degrees F) of the climate day so far."""
    values = [reading(t, c, m) for t, c, m in observations if t >= start]
    return max(values, default=-math.inf)


def ruled_out(bounds, floor):
    """Bins already below the day's maximum: no bids there."""
    return [hi is not None and hi < floor for lo, hi in bounds]


class Weather:
    def __init__(self):self.cache = []; self.fetched = 0.

    def observations(self, start):
        if time.time() - self.fetched > 50:
            req = urllib.request.Request(NWS + '?start=' + datetime.fromtimestamp(start, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), headers={'User-Agent': AGENT, 'Accept': 'application/geo+json'})
            with urllib.request.urlopen(req, timeout=15) as r:body = json.loads(r.read().decode())
            self.cache = [(ts(f['properties']['timestamp']), (f['properties'].get('temperature') or {}).get('value'), f['properties'].get('rawMessage')) for f in body.get('features', [])]
            self.fetched = time.time()
        return self.cache
