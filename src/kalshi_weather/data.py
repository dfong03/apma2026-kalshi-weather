"""Download the replay's inputs from public sources (no account needed).

Writes kalshi_quotes_<SERIES>/, kalshi_trades_<SERIES>/, asos_reports/ and daily_summary_messages/ under the data folder.
Files already on disk are skipped.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import local
import json
import time
import httpx

from .exchange import HOSTS, PREFIX

BASE = HOSTS['prod'] + PREFIX
KEEP = ['ticker', 'event_ticker', 'strike_type', 'floor_strike', 'cap_strike', 'result', 'open_time', 'close_time', 'rules_primary', 'expiration_value']
SOURCES = [('/historical/markets', '/historical/markets/{ticker}/candlesticks', '/historical/trades'),      # settled events move to the historical endpoints
           ('/markets', '/series/{series}/markets/{ticker}/candlesticks', '/markets/trades')]
ASOS = 'https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py'
AFOS = 'https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py'
THREAD = local()
stamp = lambda text: datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()


def client():
    if not hasattr(THREAD, 'client'):THREAD.client = httpx.Client(timeout=120)
    return THREAD.client


def get(url, params, timeout=120):
    """Paced GET that retries errors and waits out rate limits."""
    for attempt in range(8):
        time.sleep(.12)
        try:
            response = client().get(url, params=params, timeout=timeout)
            if response.status_code != 429 and response.status_code < 500:
                response.raise_for_status(); return response
        except httpx.TransportError:
            pass
        time.sleep(3 * (attempt + 1))
    raise RuntimeError('Request failed repeatedly: ' + url)


def price(side, field):
    value = side.get(field) if side.get(field) is not None else side.get(field + '_dollars')
    return None if value is None else float(value)


def collect(series, kind, day, root):
    path = Path(root) / f'kalshi_{kind}_{series}' / f'{day}.json'
    if path.exists():return 'cached'
    event = f'{series}-' + date.fromisoformat(day).strftime('%y%b%d').upper()
    for listing, candles, trades in SOURCES:
        markets = get(BASE + listing, {'event_ticker': event, 'limit': 100}).json().get('markets', [])
        if markets:break
    else:
        path.write_text(json.dumps(dict(target_date=day, event_ticker=event, markets=[], error='event not found'))); return 'missing'
    out = []
    for market in markets:
        entry = {k: market.get(k) for k in KEEP}
        if kind == 'quotes':
            response = get(BASE + candles.format(series=series, ticker=market['ticker']),
                           {'start_ts': int(stamp(market['open_time'])), 'end_ts': int(stamp(market['close_time'])), 'period_interval': 1})
            entry['quotes'] = [[c['end_period_ts'], price(c['yes_bid'], 'close'), price(c['yes_ask'], 'close')] for c in response.json().get('candlesticks', [])]
        else:
            rows = []; cursor = None
            while True:
                body = get(BASE + trades, {'ticker': market['ticker'], 'limit': 1000, **({'cursor': cursor} if cursor else {})}).json()
                rows += [[stamp(t['created_time']), float(t['yes_price_dollars']), float(t['count_fp']), int(t['taker_side'] == 'yes')] for t in body.get('trades', [])]
                cursor = body.get('cursor')
                if not cursor or not body.get('trades'):break
            entry['trades'] = sorted(rows)
        out.append(entry)
    path.write_text(json.dumps(dict(target_date=day, event_ticker=event, markets=out, fetched_at=datetime.now(timezone.utc).isoformat()))); return 'ok'


def kalshi(series, kind, first, last, root='data', workers=2):
    """Download one-minute quotes or trades for a series between two dates."""
    if kind not in ('quotes', 'trades'):raise ValueError('kind must be quotes or trades')
    (Path(root) / f'kalshi_{kind}_{series}').mkdir(parents=True, exist_ok=True); start, end = date.fromisoformat(first), date.fromisoformat(last)
    days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]; counts = {}
    with ThreadPoolExecutor(workers) as pool:
        for n, status in enumerate(pool.map(lambda day: collect(series, kind, day, root), days), 1):
            counts[status] = counts.get(status, 0) + 1
            if n % 25 == 0 or n == len(days):print(series, kind, n, len(days), counts, flush=True)
    return counts


def station_reports(first, last, station='NYC', root='data'):
    """Download a station's reports, one CSV per year."""
    folder = Path(root) / 'asos_reports'; folder.mkdir(parents=True, exist_ok=True)
    for year in range(first, last + 1):
        path = folder / f'{station}_{year}.csv'
        if path.exists():continue
        params = [('station', station), ('data', 'tmpf'), ('data', 'metar'), ('year1', year), ('month1', 1), ('day1', 1), ('year2', year + 1), ('month2', 1), ('day2', 1),
                  ('tz', 'Etc/UTC'), ('format', 'onlycomma'), ('latlon', 'no'), ('missing', 'M'), ('trace', 'T'), ('direct', 'no'), ('report_type', 3), ('report_type', 4)]
        response = get(ASOS, params, timeout=300)
        if not response.text.startswith('station,valid,tmpf,metar'):raise ValueError(f'Unexpected response for {year}: {response.text[:120]!r}')
        path.write_text(response.text); print(station, year, response.text.count('\n') - 1, 'rows', flush=True); time.sleep(2)


def summary_messages(first, last, station='NYC', root='data'):
    """Download a station's daily summary messages, one file per year."""
    folder = Path(root) / 'daily_summary_messages'; folder.mkdir(parents=True, exist_ok=True)
    for year in range(first, last + 1):
        path = folder / f'{station}_{year}.txt'
        if path.exists() and year < date.today().year:continue
        response = get(AFOS, {'pil': f'DSM{station}', 'sdate': f'{year}-01-01', 'edate': f'{year + 1}-01-01', 'limit': 9999, 'fmt': 'text'})
        path.write_text(response.text); print(station, year, response.text.count('DSM' + station), 'messages', flush=True); time.sleep(2)
