"""Markets, their bins, the calendar of report minutes, and live market data."""
from datetime import datetime, timezone
import math
import re
import numpy as np

from .exchange import public

# station, UTC hour the climate day starts, minute of the hourly report, summary message times (UTC)
MARKETS = {'KXHIGHNY': dict(station='NYC', start=5, minute=51, summaries=((20, 15), (21, 15)))}
EDGES = np.array([.05, .2, .8, .95])   # fair-value buckets for the fill-rate estimates
LATENCY = 2                            # seconds from a decision to its quotes
MARGIN = 5.                            # seconds before a window that quotes are withdrawn


def rules_bounds(text):
    """Bin bounds from a market's rules text, or None."""
    if m := re.search(r'is between (-?\d+)-(-?\d+)°', text):return int(m[1]), int(m[2])
    if m := re.search(r'is greater than (-?\d+)°', text):return int(m[1]) + 1, None
    if m := re.search(r'is less than (-?\d+)°', text):return None, int(m[1]) - 1
    return None


def integer_bounds(market):
    """A bin's bounds (low, high); None for an open end."""
    strike = market['strike_type']; floor = market.get('floor_strike'); cap = market.get('cap_strike')
    # some early-2025 winning bins lack strike fields; use the rules text
    if strike is None and (found := rules_bounds(market.get('rules_primary') or '')):return found
    if strike == 'greater':return math.floor(floor) + 1, None
    if strike == 'greater_or_equal':return math.ceil(floor), None
    if strike == 'less':return None, math.ceil(cap) - 1
    if strike == 'less_or_equal':return None, math.floor(cap)
    if strike == 'between':return math.ceil(floor), math.floor(cap)
    raise ValueError('Unsupported settlement strike type')


def scheduled(t, start, spec, hourly=(0, 180), summary=(0, 150)):
    """Which times fall in a calendar window: all windows, and hourly reports only."""
    sec = t % 3600; hour = t % 86400 // 3600; live = t >= start; m = spec['minute'] * 60; only = live & (sec >= m + hourly[0]) & (sec < m + hourly[1]); pull = only.copy()
    for h, minute in spec['summaries']:pull |= live & (hour == h) & (sec >= minute * 60 + summary[0]) & (sec < minute * 60 + summary[1])
    return pull, only


def climate_start(event_ticker, series):
    """UTC start of the event's climate day, from its ticker."""
    day = datetime.strptime(event_ticker.split('-')[1], '%y%b%d').replace(tzinfo=timezone.utc)
    return day.timestamp() + MARKETS[series]['start'] * 3600


def in_window(t, start, series, margin=0.):
    """Whether t (plus margin) falls in a calendar window."""
    times = np.array([t, t + margin]); pull, _ = scheduled(times, start, MARKETS[series]); return bool(pull.any())


def next_window(t, start, series, horizon=7200):
    """Start of the next calendar window after t, or None."""
    times = np.arange(math.floor(t) + 1, math.floor(t) + horizon); pull, _ = scheduled(times, start, MARKETS[series])
    opens = np.flatnonzero(pull[1:] & ~pull[:-1]) + 1
    return float(times[opens[0]]) if len(opens) else None


def stamp(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()


def order_bins(markets):
    """Markets sorted lowest bin first."""
    return sorted(markets, key=lambda m: (integer_bounds(m)[0] is not None, integer_bounds(m)[0] or 0))


def current_event(series, now):
    """The open event whose climate day is under way, or None."""
    markets = []; cursor = None
    while True:
        body = public('/markets', series_ticker=series, status='open', limit=200, **({'cursor': cursor} if cursor else {})); markets += body.get('markets', []); cursor = body.get('cursor')
        if not cursor or not body.get('markets'):break
    by = {}
    for m in markets:by.setdefault(m['event_ticker'], []).append(m)
    for event, ms in by.items():
        start = climate_start(event, series); close = stamp(ms[0]['close_time'])
        if start <= now < close:
            ms = order_bins(ms)
            return dict(event=event, tickers=[m['ticker'] for m in ms], bounds=[integer_bounds(m) for m in ms], start=start, close=close)
    return None


def books(tickers):
    """Best bid and ask in cents, with sizes, per ticker."""
    body = public('/markets/orderbooks', tickers=list(tickers)); out = {}
    for b in body.get('orderbooks', []):
        yes = b['orderbook_fp'].get('yes_dollars') or []; no = b['orderbook_fp'].get('no_dollars') or []
        top_yes = max(([round(float(p) * 100), float(c)] for p, c in yes), default=[0, 0.]); top_no = max(([round(float(p) * 100), float(c)] for p, c in no), default=[0, 0.])
        out[b['ticker']] = dict(bid=top_yes[0], bid_size=top_yes[1], ask=100 - top_no[0] if top_no[0] else 100, ask_size=top_no[1])
    return out
