"""Replay the strategy against Kalshi's public trade tape.

Each minute the strategy posts one bid and one ask per bin around fair value. A historical taker trade fills our quote
if it trades through our price, or at our price when that level was then used up (a new order joins the back of the queue).
No quotes inside the calendar windows, and no bids in bins the day's readings have ruled out (the guard).
"""
from datetime import datetime, timezone
from pathlib import Path
import csv
import json
import re
import numpy as np

from .markets import EDGES, LATENCY, MARKETS, integer_bounds, order_bins, scheduled
from .strategy import skew_matrix, tick

PERIODS = {'train': ('2025-10-01', '2026-03-31'), 'test': ('2026-04-01', '2026-09-29'), 'early': ('2025-01-01', '2025-09-30')}
FILL = (1., .25)                       # fill shares: at our price (level used up), and through it only because we improved
REPORT_DELAY = 190; MESSAGE_DELAY = 120   # delays before a report and a summary message reach us
GUARD = {'KXHIGHNY': 'NYC'}             # markets with a guard
SIX = re.compile(r'^1([01])(\d{3})$')
stamp = lambda text: int(np.datetime64(text).astype('datetime64[s]').astype(int))
whole = lambda x: float(np.floor(x + .5))


# ---- data
def complete_events(folder, first, last):
    """Events with every bin quoted and a single winner, by date."""
    out = {}
    for path in sorted(Path(folder).glob('*.json')):
        record = json.loads(path.read_text()); day = record['target_date']
        if not first <= day <= last or not record['markets']:continue
        try:markets = order_bins(record['markets']); bounds = [integer_bounds(m) for m in markets]
        except (ValueError, TypeError):continue
        if bounds[0][0] is not None or bounds[-1][1] is not None or any(bounds[i][0] != bounds[i - 1][1] + 1 for i in range(1, len(bounds))):continue
        if sum(m['result'] == 'yes' for m in markets) != 1 or any(len(m['quotes']) == 0 for m in markets):continue
        quotes = [np.array([[q[0], np.nan if q[1] is None else q[1], np.nan if q[2] is None else q[2]] for q in m['quotes']]) for m in markets]
        when = lambda text: int(np.datetime64(text.replace('Z', '')).astype('datetime64[s]').astype(int))
        out[day] = dict(bounds=bounds, quotes=quotes, tickers=[m['ticker'] for m in markets], winner=[m['result'] == 'yes' for m in markets].index(True),
                        open=when(markets[0]['open_time']), close=when(markets[0]['close_time']))
    return out


def grid(event, times):
    """Standing bid and ask per bin at each time; NaN where a bin lacks a quote."""
    bid = np.full((len(times), len(event['quotes'])), np.nan); ask = bid.copy()
    for k, q in enumerate(event['quotes']):
        i = np.searchsorted(q[:, 0], times, side='right') - 1; ok = i >= 0
        bid[ok, k] = q[i[ok], 1]; ask[ok, k] = q[i[ok], 2]
    bad = ~(np.isfinite(bid) & np.isfinite(ask) & (bid >= 0) & (ask <= 1) & (bid <= ask)).all(1)
    bid[bad] = np.nan; ask[bad] = np.nan
    return bid, ask


def readings(root, station='NYC'):
    """Station reports as (time, lower bound on the day's maximum)."""
    rows = []
    for path in sorted((Path(root) / 'asos_reports').glob(f'{station}_*.csv')):
        for row in csv.DictReader(path.open()):
            try:temp = float(row['tmpf'])
            except ValueError:continue
            ts = int(datetime.fromisoformat(row['valid']).replace(tzinfo=timezone.utc).timestamp())
            six = [(-1 if m[1] == '1' else 1) * int(m[2]) / 10 * 1.8 + 32 for tok in row['metar'].split(' RMK ')[-1].split() if (m := SIX.fullmatch(tok))]
            six = six[0] if len(six) == 1 else None
            rows.append((ts, max(whole(temp), whole(six) if six is not None and ts % 86400 // 3600 != 5 else -np.inf)))
    return np.array(sorted(rows)).reshape(-1, 2)


def messages(root, station='NYC'):
    """Daily summary messages as (date, hour) -> maximum so far."""
    sent = {}
    for path in sorted((Path(root) / 'daily_summary_messages').glob(f'{station}_*.txt')):
        year = int(path.stem.split('_')[1])
        for product in re.split(r'(?=CXUS4\d \w{4} \d{6})', path.read_text(errors='replace')):
            head = re.match(r'CXUS4\d \w{4} (\d{2})', product)
            for m in re.finditer(rf'K{station} DS (?:COR )?(\d{{4}}) (\d{{2}})/(\d{{2}}) +(-?\d+)/', product):
                hh, dd, mm, field = m.groups()
                if len(field) < 5:continue
                issued = year - 1 if mm == '12' and head and int(head[1]) < int(dd) else year   # a December message issued just after New Year
                sent[(f'{issued}-{mm}-{dd}', int(hh[:2]))] = float(field[:-4])
    return sent


def walking(tr):
    """Trades in time order, each sweep in the order it walks the book."""
    return tr[np.lexsort((np.where(tr[:, 4] > 0, tr[:, 2], -tr[:, 2]), tr[:, 4] < 0, tr[:, 0]))]


def prepare(day, e, book, obs, sent, spec, latency=LATENCY):
    """One event: its trades and what was known at each decision."""
    start = stamp(day + 'T00:00') + spec['start'] * 3600; rows = []
    for k, ticker in enumerate(e['tickers']):
        tr = book.get(ticker)
        if tr is not None and len(tr):rows.append(np.c_[tr[:, 0], np.full(len(tr), k), np.round(tr[:, 1] * 100), tr[:, 2], np.where(tr[:, 3] > 0, 1, -1)])
    if not rows:return None
    tr = walking(np.concatenate(rows)); t = tr[:, 0]; times, index = np.unique(np.floor((t - latency) / 60) * 60, return_inverse=True)
    bid, ask = grid(e, times); later = np.add(*grid(e, np.minimum(times + 360, e['close'] - 60))) / 2
    pull, _ = scheduled(t, start, spec)
    # the running maximum known at each decision
    keep = (obs[:, 0] >= start) & (obs[:, 0] < start + 86400); known = [(ts + REPORT_DELAY, v) for ts, v in obs[keep]]
    known += [(stamp(day + f'T{hh + 5}:15') + MESSAGE_DELAY, sent[(day, hh)]) for hh in (15, 16) if (day, hh) in sent]; known = np.array(sorted(known)).reshape(-1, 2)
    floor = np.r_[-np.inf, np.maximum.accumulate(known[:, 1])][np.searchsorted(known[:, 0], times, side='right')]
    caps = np.array([np.inf if hi is None else hi for _, hi in e['bounds']])
    return dict(day=day, times=times, bid=np.round(bid * 100), ask=np.round(ask * 100), later=later, ruled=caps[None, :] < floor[:, None], same=times >= start, start=start,
                source=e, left=(e['close'] - times) / 60, winner=e['winner'], edge=np.searchsorted(index, np.arange(len(times) + 1)),
                bin=tr[:, 1].astype(int).tolist(), price=tr[:, 2].astype(int).tolist(), size=tr[:, 3].tolist(), side=tr[:, 4].astype(int).tolist(), pull=pull.tolist(), stamp=t, spec=spec)


def load(series, first, last, root='data', latency=LATENCY, obs=None, sent=None):
    """The events of one market between two dates."""
    root = Path(root); spec = MARKETS[series]; station = GUARD.get(series)
    obs = obs if obs is not None else readings(root, station) if station else np.zeros((0, 2)); sent = sent if sent is not None else messages(root, station) if station else {}
    out = []
    for day, e in complete_events(root / f'kalshi_quotes_{series}', first, last).items():
        path = root / f'kalshi_trades_{series}' / f'{day}.json'
        if not path.exists():continue
        book = {m['ticker']: np.array(m['trades'], dtype=float).reshape(-1, 4) for m in json.loads(path.read_text())['markets']}
        event = prepare(day, e, book, obs, sent, spec, latency)
        if event:out.append(event)
    return out


# ---- the replay
def exhausting(event):
    """Whether each trade's price level was then used up."""
    B, P, S = np.asarray(event['bin']), np.asarray(event['price']), np.asarray(event['side']); out = np.zeros(len(B), bool)
    for k in np.unique(B):
        for side in (1, -1):
            idx = np.where((B == k) & (S == side))[0]; after = None; later = None       # the next different price after this trade
            for i in idx[::-1]:
                nxt = later if after is not None and P[after] == P[i] else (P[after] if after is not None else None)
                out[i] = nxt is not None and (nxt > P[i] if side > 0 else nxt < P[i]); after = i; later = nxt
    return out


def fair(event):
    """Fair values at each decision: mids rescaled to sum to one."""
    mid = (event['bid'] + event['ask']) / 200; total = mid.sum(1, keepdims=True)
    with np.errstate(all='ignore'):return np.where(total > 0, mid / total, np.nan)


def simulate(events, strategy, est, fill=FILL, guard=True, calendar=True):
    """Replay the strategy on one market's events; returns one row per fill and per-event results."""
    touch, improve = fill; lot = strategy['lot']; cap = strategy['cap'] * lot
    rates = {k: np.asarray(v) for k, v in est.items()}; fills = []; peaks = []
    for number, ev in enumerate(events):
        p = fair(ev); K = p.shape[1]; valid = np.isfinite(p).all(1).tolist()
        cell = np.searchsorted(EDGES, np.nan_to_num(p)); same = ev['same'].astype(int)[:, None]; rate = rates['rate'][same, cell]; kappa = rates['kappa'][same, cell]
        # adverse selection also lowers the liquidity term
        if strategy.get('adverse_liquidity'):rate = rate * np.exp(-kappa * rates['adverse'][same, cell])
        M = skew_matrix(np.nan_to_num(p, nan=1 / K), rate, kappa, strategy['phi'], strategy['alpha'], ev['left'])
        own = np.diagonal(M, axis1=1, axis2=2).tolist(); kappa = kappa.tolist(); centre = p.tolist(); extra = rates['adverse'][same, cell].tolist()
        bid, ask, ruled, later, edge = ev['bid'].tolist(), ev['ask'].tolist(), ev['ruled'].tolist(), ev['later'].tolist(), ev['edge']
        mids = ((ev['bid'] + ev['ask']) / 200).tolist(); fairs = centre; used = exhausting(ev).tolist()
        q = np.zeros(K); cash = 0.; peak = 0.; most = 0.; locked = 0.; B, price, size, side = ev['bin'], ev['price'], ev['size'], ev['side']
        pull = ev['pull']; win = [float(k == ev['winner']) for k in range(K)]
        for a in range(len(ev['times'])):
            if not valid[a]:continue
            lean = (2 * M[a] @ q / lot).tolist(); quotes = {}

            def post(k):
                """Bin k's bid and ask."""
                mb, ma = bid[a][k], ask[a][k]; buy, sell = own[a][k] + lean[k], own[a][k] - lean[k]; bids = {}; asks = {}
                if ev['same'][a]:
                    b = tick(-1, centre[a][k], extra[a][k] + buy, kappa[a][k], ma); s = tick(1, centre[a][k], extra[a][k] + sell, kappa[a][k], mb)
                    if b is not None and b >= 1 and not (guard and ruled[a][k]):bids[b] = lot
                    if s is not None and s <= 99:asks[s] = lot
                # levels best first; rooms left under the position limit
                return [[list(x) for x in sorted(bids.items(), reverse=True)], [list(x) for x in sorted(asks.items())], max(0., cap - q[k]), max(0., cap + q[k]), mb, ma]

            for r in range(edge[a], edge[a + 1]):
                k = B[r]
                if k not in quotes:quotes[k] = post(k)
                if calendar and pull[r]:continue
                quote = quotes[k]; mb, ma = quote[4], quote[5]; P = price[r]; rest = size[r]; here = touch if used[r] else 0.
                if side[r] > 0:      # taker buys: may lift our asks
                    for level in quote[1]:
                        s = level[0]
                        if P < s:break
                        got = min(level[1], quote[3], rest * (here if P == s else 1. if P > ma else improve))
                        if got <= 0:continue
                        level[1] -= got; quote[3] -= got; rest -= got; q[k] -= got; cash += got * s / 100
                        fills.append((number, ev['times'][a], k, 1, got, s, mids[a][k], later[a][k], win[k], fairs[a][k], ev['same'][a], pull[r], s - ma, ev['left'][a]))
                else:                # taker sells: may hit our bids
                    for level in quote[0]:
                        b = level[0]
                        if P > b:break
                        got = min(level[1], quote[2], rest * (here if P == b else 1. if P < mb else improve))
                        if got <= 0:continue
                        level[1] -= got; quote[2] -= got; rest -= got; q[k] += got; cash -= got * b / 100
                        fills.append((number, ev['times'][a], k, -1, got, b, mids[a][k], later[a][k], win[k], fairs[a][k], ev['same'][a], pull[r], mb - b, ev['left'][a]))
                peak = max(peak, -cash - q.min()); most = max(most, float(np.abs(q).max())); locked = max(locked, float(np.where(q > 0, q * p[a], -q * (1 - p[a])).sum()))
        peaks.append((peak, most, cash + q[ev['winner']], locked))
    return np.array(fills, dtype=float).reshape(-1, 14), np.array(peaks).reshape(-1, 4)


def summarise(fills, peaks):
    """Dollars a day and its spread, from simulate's output."""
    days = len(peaks); n = fills[:, 4]; s = fills[:, 3]; price = fills[:, 5] / 100; gain = s * (price - fills[:, 8]) * n
    daily = np.bincount(fills[:, 0].astype(int), gain, minlength=days)
    assert np.allclose(daily, peaks[:, 2])
    sd = daily.std(ddof=1) if days > 1 else 0.
    return dict(days=days, dollars_per_day=round(float(daily.mean()), 2), daily_sharpe=round(float(daily.mean() / sd), 3) if sd > 0 else None,
                worst_day=round(float(daily.min()), 2), share_of_days_negative=round(float((daily < 0).mean()), 3), contracts_per_day=round(float(n.sum() / days)),
                peak_cash_mean=round(float(peaks[:, 0].mean()), 2), peak_collateral_without_netting_max=round(float(peaks[:, 3].max()), 2)), daily
