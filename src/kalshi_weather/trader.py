"""Live quoting on Kalshi for one series (New York by default).

Each minute: honor STOP, read orders, positions, fills and the market, stop on the loss limit or a position mismatch,
pull quotes in calendar windows, then send only the changes needed to reach the strategy's quotes.
Usage: kalshi-weather trade run [--once] | kill | watchdog
Settings: TRADER_ENV, TRADER_SERIES, TRADER_SIZE, TRADER_QMAX, TRADER_LOSS, TRADER_SUBACCOUNT, TRADER_STATE, TRADER_STRATEGY.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import gzip
import http.client
import json
import math
import os
import signal
import subprocess
import time
import urllib.error
import uuid

from . import exchange as kalshi_api
from .markets import MARGIN, books, current_event, in_window, next_window
from .strategy import decide, load_strategy, quote_parts
from .weather import GUARDED, Weather, floor_since, ruled_out


@dataclass
class Config:
    env: str = 'prod'; series: str = 'KXHIGHNY'; subaccount: int | None = None; state: str = 'data/trader'
    size: float = 10.; q_max: float = 50.; loss: float = 10.; loss_minutes: int = 2
    max_orders: int = 60; max_contracts: float = 120.; expiry: int = 180; extend_below: int = 120; min_life: int = 15
    group_limit: float = 25.; grace: float = 10.; write_rate: float = 80.   # write tokens a second (basic tier)
    strategy: str | None = None

    @classmethod
    def from_env(cls, environ=os.environ):
        get = lambda name, cast, default: cast(environ[name]) if environ.get(name, '') != '' else default
        return cls(env=get('TRADER_ENV', str, 'prod'), series=get('TRADER_SERIES', str, 'KXHIGHNY'), subaccount=get('TRADER_SUBACCOUNT', int, None),
                   state=get('TRADER_STATE', str, 'data/trader'), size=get('TRADER_SIZE', float, 10.),
                   q_max=get('TRADER_QMAX', float, 50.), loss=get('TRADER_LOSS', float, 10.), strategy=get('TRADER_STRATEGY', str, None))


@dataclass
class Order:
    id: str; ticker: str; side: int; cents: int; remaining: float; filled: float = 0.; expires: float | None = None; created: float = 0.


def ts(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp() if text else None


# ---- the exchange
class Exchange:
    """Kalshi's order API. Side +1 sells YES, -1 buys YES; calls return (result, error or None)."""

    def __init__(self, env, subaccount=None, client=None, write_rate=80.):
        self.client = client or kalshi_api.Client(env, os.environ.get('KALSHI_KEY_DIR') or None)
        self.sub = subaccount; self.rate = write_rate; self.next_write = 0.; self.writes = 0

    def _pace(self, cost=10.):
        now = time.monotonic(); wait = self.next_write - now
        if wait > 0:time.sleep(wait)
        self.next_write = max(now, self.next_write) + cost / self.rate; self.writes += 1

    def _with_sub(self, d):
        if self.sub is not None:d['subaccount'] = self.sub
        return d

    @staticmethod
    def _error(status, reply):
        if 200 <= status < 300:return None
        e = reply.get('error', {}) if isinstance(reply, dict) else {}
        return (e.get('details') or e.get('code') or f'http {status}') if isinstance(e, dict) else f'http {status}'

    def _pages(self, path, key, **params):
        rows, cursor = [], None
        while True:
            status, reply = self.client.get(path, **self._with_sub(dict(limit=1000, **params, **({'cursor': cursor} if cursor else {}))))
            if status != 200:raise RuntimeError(f'{path}: http {status}')
            rows += reply.get(key) or []; cursor = reply.get('cursor')
            if not cursor or not reply.get(key):return rows

    def resting(self):
        out = []
        for o in self._pages('/portfolio/orders', 'orders', status='resting'):
            out.append(Order(o['order_id'], o['ticker'], 1 if o.get('book_side') == 'ask' else -1, round(float(o['yes_price_dollars']) * 100),
                             float(o.get('remaining_count_fp') or 0), float(o.get('fill_count_fp') or 0), ts(o.get('expiration_time')), ts(o.get('created_time')) or 0.))
        return out

    def balance(self):
        """Cash balance in dollars, or None."""
        status, reply = self.client.get('/portfolio/balance', **self._with_sub({}))
        if status != 200 or not isinstance(reply, dict):return None
        return float(reply['balance_dollars']) if reply.get('balance_dollars') is not None else float(reply.get('balance', 0)) / 100

    def positions(self):
        return {p['ticker']: float(p.get('position_fp') or 0) for p in self._pages('/portfolio/positions', 'market_positions')}

    def fills(self, since):
        return [dict(id=f.get('fill_id'), ticker=f['ticker'], side=1 if f.get('book_side') == 'ask' else -1, cents=round(float(f['yes_price_dollars']) * 100),
                     n=float(f['count_fp']), fee=float(f.get('fee_cost') or 0), t=float(f.get('ts') or ts(f.get('created_time')) or 0), order=f.get('order_id'))
                for f in self._pages('/portfolio/fills', 'fills', min_ts=int(since))]

    def create(self, ticker, side, cents, n, expires, group=None):
        self._pace()
        body = self._with_sub(dict(ticker=ticker, client_order_id=str(uuid.uuid4()), side='ask' if side > 0 else 'bid', count=f'{n:.2f}', price=f'{cents / 100:.4f}',
                                   time_in_force='good_till_canceled', expiration_time=int(expires), post_only=True, self_trade_prevention_type='taker_at_cross',
                                   cancel_order_on_pause=True, **({'order_group_id': group} if group else {})))
        status, reply = self.client.request('POST', '/portfolio/events/orders', body=body)
        return (reply.get('order_id') if status == 201 else None), self._error(status, reply)

    def amend(self, o, cents, total, expires):
        self._pace()
        status, reply = self.client.request('POST', f'/portfolio/events/orders/{o.id}/amend', params=self._with_sub({}) or None,
                                            body=dict(ticker=o.ticker, side='ask' if o.side > 0 else 'bid', price=f'{cents / 100:.4f}', count=f'{total:.2f}', expiration_time=int(expires)))
        return status == 200, self._error(status, reply)

    def decrease(self, o, to):
        self._pace()
        status, reply = self.client.request('POST', f'/portfolio/events/orders/{o.id}/decrease', params=self._with_sub({}) or None, body=dict(reduce_to=f'{to:.2f}'))
        return status == 200, self._error(status, reply)

    def cancel(self, orders):
        done = 0
        for i in range(0, len(orders), 20):
            chunk = orders[i:i + 20]; self._pace(2. * len(chunk))
            status, reply = self.client.request('DELETE', '/portfolio/events/orders/batched', body=dict(orders=[self._with_sub(dict(order_id=o.id, market_ticker=o.ticker)) for o in chunk]))
            done += len(reply.get('orders', [])) if status == 200 and isinstance(reply, dict) else 0
        return done, None

    def cancel_all(self):
        self._pace()
        status, reply = self.client.request('DELETE', '/portfolio/events/orders', params=self._with_sub({}) or None)
        return status in (200, 204), self._error(status, reply)

    def group_create(self, limit):
        self._pace()
        status, reply = self.client.request('POST', '/portfolio/order_groups/create', body=self._with_sub(dict(contracts_limit=int(limit))))
        return (reply.get('order_group_id') if status in (200, 201) else None), self._error(status, reply)

    def group_trigger(self, group):
        self._pace(); status, reply = self.client.request('PUT', f'/portfolio/order_groups/{group}/trigger'); return status == 200, self._error(status, reply)

    def group_reset(self, group):
        self._pace(); status, reply = self.client.request('PUT', f'/portfolio/order_groups/{group}/reset'); return status == 200, self._error(status, reply)


# ---- what to post, and what to change
def target_book(quotes, ruled, q, unit, cfg):
    """Orders to post: the strategy's quotes after the guard, the position limit and the order caps."""
    entries = []
    for k, (bids, asks, *_rest) in enumerate(quotes):
        for side, levels in ((-1, bids), (1, asks)):
            if side < 0 and ruled[k]:continue
            room = cfg.q_max - q[k] if side < 0 else cfg.q_max + q[k]          # room left before the position limit
            left = min(cfg.size, max(0., room)); depth = 0
            for cents, n in levels:                                           # one level a side
                if left <= 1e-9:break
                post = min(round(n / unit) * cfg.size, left)
                if post > 1e-9:entries.append((depth, (k, side, int(cents)), post)); left -= post; depth += 1
    entries.sort(key=lambda e: e[0]); book = {}
    for depth, key, n in entries:
        if len(book) < cfg.max_orders and sum(book.values()) + n <= cfg.max_contracts + 1e-9:book[key] = n
    return book


def plan(target, resting, index, now, cfg, expires=None):
    """Actions that turn the resting orders into the target, keeping queue places where possible."""
    expires = now + cfg.expiry if expires is None else expires
    def moved(o):return o.expires is None or o.expires > expires + .5 or (o.expires - now < cfg.extend_below and o.expires < expires - .5)
    groups = {}
    for o in sorted(resting, key=lambda o: o.created):
        if o.ticker in index:groups.setdefault((index[o.ticker], o.side, o.cents), []).append(o)
    keep, spare, missing = [], [], {}
    for key, orders in groups.items():
        want, kept = target.get(key, 0.), 0.
        for o in orders:
            if kept >= want - 1e-9:spare.append(o); continue
            take = min(o.remaining, want - kept); kept += take
            if take < o.remaining - 1e-9:keep.append(('decrease', o, take))
            if moved(o):keep.append(('extend', o, take))
        if kept < want - 1e-9:missing[key] = want - kept
    for key, n in target.items():
        if key not in groups:missing[key] = n
    actions, creates = [a for a in keep if a and a[0] == 'decrease'], []
    for key, n in sorted(missing.items()):
        reuse = next((o for o in spare if (index[o.ticker], o.side) == key[:2] and o.filled == 0), None)
        if reuse:spare.remove(reuse); actions.append(('reprice', reuse, key, n))
        else:creates.append(('create', key, n))
    stray = [o for o in resting if o.ticker not in index]                     # not this event's orders
    return [('cancel', o) for o in spare + stray] + actions + [a for a in keep if a and a[0] == 'extend'] + creates


def loss_stop(marks, cfg):
    """Stop when the last cfg.loss_minutes marks are all at or below minus the loss limit."""
    return len(marks) >= cfg.loss_minutes and all(m <= -cfg.loss for m in marks[-cfg.loss_minutes:])


def from_fills(fills, tickers):
    """Positions and cash implied by our fills in this event."""
    q = {t: 0. for t in tickers}; cash = 0.
    for f in fills:
        if f['ticker'] in q:q[f['ticker']] -= f['side'] * f['n']; cash += f['side'] * f['n'] * f['cents'] / 100 - f['fee']
    return q, cash


# ---- the loop
class Market:
    def event(self, series, now):return current_event(series, now)
    def books(self, tickers):return books(tickers)


@dataclass
class Trader:
    cfg: Config; ex: object; market: object = field(default_factory=Market); weather: object = field(default_factory=Weather)
    strategy: dict = None; marks: list = field(default_factory=list); mismatches: int = 0; group: str = None; errors: int = 0

    def __post_init__(self):
        self.dir = Path(self.cfg.state); self.dir.mkdir(parents=True, exist_ok=True)
        self.strategy = self.strategy or load_strategy(self.cfg.strategy)
        saved = self._load('trader.json'); self.group = saved.get('group'); self.marks = saved.get('marks', [])[-10:]; self.marked_event = saved.get('event')
        self.unit = self.strategy['lot']; self.event = None; self.looked = 0.

    def _load(self, name):
        try:return json.loads((self.dir / name).read_text())
        except (OSError, ValueError):return {}

    def _save(self, name, data):
        tmp = self.dir / (name + '.tmp'); tmp.write_text(json.dumps(data)); tmp.replace(self.dir / name)

    def stopped(self):return (self.dir / 'STOP').exists()

    def halt(self, why):
        """STOP file, trigger the order group, cancel everything; positions are held."""
        (self.dir / 'STOP').write_text(why + '\n')
        if self.group:self.ex.group_trigger(self.group)
        self.ex.cancel_all(); self.group = None   # a triggered group refuses orders: make a fresh one

    def cycle(self, now):
        cfg = self.cfg; row = dict(t=now, env=cfg.env, series=cfg.series)
        saved = self._load('trader.json')
        if 'group' in saved and saved['group'] != self.group:self.group = saved['group']   # kill triggered and cleared our group
        if self.stopped():
            resting = self.ex.resting()
            if resting or self.group:self.halt((self.dir / 'STOP').read_text().strip() or 'stopped')   # trigger our group, cancel all
            self.group = None
            return self._finish(row, 'STOPPED', 'All orders cancelled; positions held to settlement.')
        if self.event is None or now - self.looked > 300 or now >= self.event['close']:self.event = self.market.event(cfg.series, now); self.looked = now
        ev = self.event
        if ev is None or not ev['start'] <= now < ev['close']:
            resting = self.ex.resting()
            if resting:self.ex.cancel(resting)
            return self._finish(row, 'IDLE', 'No climate day under way.')
        tickers = ev['tickers']; index = {t: k for k, t in enumerate(tickers)}
        row.update(event=ev['event'], tickers=tickers, bounds=ev['bounds'], close=ev['close'], next_window=next_window(now, ev['start'], cfg.series),
                   limits=dict(q_max=cfg.q_max, loss=cfg.loss, size=cfg.size))
        top = self.market.books(tickers); bid = [top[t]['bid'] for t in tickers]; ask = [top[t]['ask'] for t in tickers]
        resting = self.ex.resting(); exchange_q = self.ex.positions(); fills = self.ex.fills(ev['start'] - 86400)
        balance = getattr(self.ex, 'balance', lambda: None)()
        if balance is not None:row['balance'] = round(balance, 2)
        mine = [f for f in fills if f['ticker'] in index]; local_q, cash = from_fills(mine, tickers)
        q = [exchange_q.get(t, 0.) for t in tickers]
        mids = [(b + a) / 200 for b, a in zip(bid, ask)]; total = sum(mids) or 1.; fair = [m / total for m in mids]
        mark = cash + sum(qk * pk for qk, pk in zip(q, fair)); worst = min((cash + qk for qk in q), default=cash)
        if self.marked_event != ev['event']:self.marks, self.marked_event = [], ev['event']   # the loss limit is per climate day
        self.marks = (self.marks + [round(mark, 4)])[-10:]
        row.update(bid=bid, ask=ask, fair=[round(f, 4) for f in fair], q=q, cash=round(cash, 4), mark=round(mark, 4), worst=round(worst, 4), fills_today=len(mine),
                   last_fills=[dict(t=f['t'], k=index[f['ticker']], side=f['side'], cents=f['cents'], n=f['n'])   # the last two minutes, at least six
                               for i, f in enumerate(sorted(mine, key=lambda f: -f['t'])) if i < 6 or f['t'] >= now - 120])
        if cfg.series not in GUARDED:
            floor = -math.inf; row['guard'] = 'off: no station feed for this series'
        else:
            try:
                floor = floor_since(self.weather.observations(ev['start']), ev['start']); row['guard'] = 'ok'
            except (OSError, http.client.HTTPException, ValueError, KeyError) as problem:   # keep this event's last known high
                saved = self._load('trader.json'); floor = saved.get('floor', -math.inf) if saved.get('event') == ev['event'] else -math.inf
                row['guard'] = f'stale: {type(problem).__name__}'
        ruled = ruled_out(ev['bounds'], floor); row.update(high_so_far=None if floor == -math.inf else floor, ruled=ruled)

        if loss_stop(self.marks, cfg):
            self.halt(f'daily loss limit: marked {mark:.2f} for {cfg.loss_minutes} minutes'); return self._finish(row, 'STOPPED', f'Daily loss limit of ${cfg.loss:.0f} reached.')
        recent = any(now - f['t'] < cfg.grace for f in mine)
        differs = [t for t in tickers if abs(local_q[t] - exchange_q.get(t, 0.)) > 1e-6]
        self.mismatches = 0 if recent or not differs else self.mismatches + 1
        if self.mismatches >= 2:
            if resting:self.ex.cancel(resting)
            row['mismatch'] = {t: [local_q[t], exchange_q.get(t, 0.)] for t in differs}
            return self._finish(row, 'PAUSED', "Our positions differ from Kalshi's; no quotes until they agree.")
        if in_window(now, ev['start'], cfg.series, MARGIN):
            if resting:self.ex.cancel(resting)
            return self._finish(row, 'PULLED', '')
        # expire just before the next calendar window
        expires = now + cfg.expiry
        if row['next_window'] is not None:expires = min(expires, row['next_window'] - 1)
        if expires - now < cfg.min_life:
            if resting:self.ex.cancel(resting)
            return self._finish(row, 'PULLED', 'A calendar window opens within seconds.')

        quotes = decide(bid, ask, q, (ev['close'] - now) / 60, now >= ev['start'], self.strategy['estimates'][cfg.series], self.strategy)
        if quotes is None:
            if resting:self.ex.cancel(resting)
            return self._finish(row, 'PULLED', 'Books incomplete; not quoting.')
        target = target_book(quotes, ruled, q, self.unit, cfg)
        row['parts'] = quote_parts(bid, ask, q, (ev['close'] - now) / 60, now >= ev['start'], self.strategy['estimates'][cfg.series], self.strategy)
        row.update(best_bid=[max((c for (k, s, c) in target if k == i and s < 0), default=None) for i in range(len(tickers))],
                   best_ask=[min((c for (k, s, c) in target if k == i and s > 0), default=None) for i in range(len(tickers))])
        if not self.group:
            self.group, err = self.ex.group_create(cfg.group_limit)
            if err:row['group_error'] = err
        problems = self._execute(plan(target, resting, index, now, cfg, expires), tickers, expires)
        if 'group' in problems:
            self.halt('order group refused orders (fill cap hit or triggered)'); return self._finish(row, 'STOPPED', 'Order group refused orders: fills came too fast.')
        row.update(orders=len(target), contracts=sum(target.values()), write_errors=problems.get('other', 0), post_only_skips=problems.get('cross', 0))
        return self._finish(row, 'QUOTING', '', floor=floor)

    def _execute(self, actions, tickers, expires):
        problems = {}
        for a in actions:
            kind = a[0]
            if kind == 'cancel':self.ex.cancel([a[1]]); continue
            if kind == 'decrease':ok, err = self.ex.decrease(a[1], a[2])
            elif kind == 'extend':ok, err = self.ex.amend(a[1], a[1].cents, a[1].filled + a[2], expires)   # after any decrease
            elif kind == 'reprice':ok, err = self.ex.amend(a[1], a[2][2], a[3], expires)
            else:
                (k, side, cents), n = a[1], a[2]; ok, err = self.ex.create(tickers[k], side, cents, n, expires, self.group)
            if err:
                tag = 'cross' if 'post only' in err else 'group' if 'order_group' in err or 'order group' in err else 'other'
                problems[tag] = problems.get(tag, 0) + 1; self.errors += tag == 'other'
        return problems

    def _finish(self, row, status, why, floor=None):
        row.update(status=status, why=why, errors=self.errors); now = row['t']
        if floor is None or floor == -math.inf:   # no reading this minute: keep the last known high
            prev = self._load('trader.json'); floor = prev.get('floor') if prev.get('event') == self.marked_event else None
        saved = dict(group=self.group, marks=self.marks, event=self.marked_event, **({'floor': floor} if floor is not None else {}))
        self._save('trader.json', saved); self._save('status.json', dict(t=time.time(), decision=now, status=status, why=why, env=self.cfg.env, errors=self.errors))
        self._save('state.json', row)
        log = self.dir / 'log' / f"{datetime.fromtimestamp(now, timezone.utc):%Y-%m-%d}.jsonl.gz"; log.parent.mkdir(exist_ok=True)
        with gzip.open(log, 'at') as handle:handle.write(json.dumps(row, separators=(',', ':')) + '\n')
        return row


# ---- commands
def alert(subject, message):
    topic = os.environ.get('ALERT_TOPIC_ARN')
    if topic:subprocess.run(['aws', 'sns', 'publish', '--topic-arn', topic, '--subject', subject[:99], '--message', message], capture_output=True, timeout=30, check=False)


def kill(cfg, ex=None, why='kill switch'):
    """Stop now: STOP file, trigger the order group, cancel everything."""
    ex = ex or Exchange(cfg.env, cfg.subaccount, write_rate=cfg.write_rate); d = Path(cfg.state); d.mkdir(parents=True, exist_ok=True)
    (d / 'STOP').write_text(why + '\n')
    saved = json.loads((d / 'trader.json').read_text()) if (d / 'trader.json').exists() else {}
    if saved.get('group'):   # a triggered group is dead: clear it
        ex.group_trigger(saved['group']); saved['group'] = None
        tmp = d / 'trader.json.tmp'; tmp.write_text(json.dumps(saved)); tmp.replace(d / 'trader.json')
    ex.cancel_all(); time.sleep(2); left = ex.resting()
    if left:ex.cancel(left); time.sleep(2); left = ex.resting()
    (d / 'status.json').write_text(json.dumps(dict(t=time.time(), status='STOPPED', why=why, env=cfg.env, left_resting=len(left))))
    return len(left)


def watchdog(cfg, max_age=120.):
    """Stop everything and alert if the trader's status is stale."""
    d = Path(cfg.state)
    if (d / 'STOP').exists() or not (d / 'status.json').exists():return 'nothing to do'
    age = time.time() - json.loads((d / 'status.json').read_text())['t']
    if age < max_age:return f'ok ({age:.0f} s)'
    left = kill(cfg, why=f'watchdog: no status for {age:.0f} s')
    alert('kalshi-weather WATCHDOG', f'The trader wrote no status for {age:.0f} s. Orders cancelled ({left} left resting), STOP set. Positions are held.')
    return f'killed ({left} left)'


def run(cfg, once=False):
    trader = Trader(cfg, Exchange(cfg.env, cfg.subaccount, write_rate=cfg.write_rate)); running = [True]
    for name in (signal.SIGINT, signal.SIGTERM):signal.signal(name, lambda *_: running.__setitem__(0, False))
    print(f'trading {cfg.series} on {cfg.env}: {cfg.size:g} contracts a side, Q {cfg.q_max:g}, loss limit ${cfg.loss:g}; state in {cfg.state}', flush=True)
    while running[0]:
        decision = math.floor(time.time() / 60) * 60
        try:row = trader.cycle(decision); print(f"{datetime.now(timezone.utc):%H:%M:%S} {row['status']} {row.get('why', '')}", flush=True)
        except (OSError, http.client.HTTPException, RuntimeError, KeyError, ValueError) as problem:   # one bad minute; expiries cover it
            trader.errors += 1; print(f'{datetime.now(timezone.utc):%H:%M:%S} error: {type(problem).__name__}: {str(problem)[:160]}', flush=True)
        if once:return
        time.sleep(max(1., decision + 61 - time.time()))
