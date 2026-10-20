"""The quoting rule: the inventory matrix in closed form and each bin's quotes."""
from importlib import resources
from pathlib import Path
import json
import math
import numpy as np


def load_strategy(path=None):
    """Load a strategy file (default: the shipped one)."""
    if path:return json.loads(Path(path).read_text())
    return json.loads(resources.files('kalshi_weather').joinpath('strategy.json').read_text())


def skew_matrix(p, rate, kappa, phi, alpha, minutes):
    """Inventory matrix M in closed form; minutes is the time to the close."""
    p, rate, kappa = np.asarray(p, float), np.asarray(rate, float), np.asarray(kappa, float); tau = np.asarray(minutes, float)[..., None]
    d = np.sqrt(4 * rate * kappa / np.e); S = p[..., :, None] * np.eye(p.shape[-1]) - p[..., :, None] * p[..., None, :]
    w, V = np.linalg.eigh(d[..., :, None] * S * d[..., None, :]); w = np.clip(w, 0, None); root = np.sqrt(phi * w); th = np.tanh(root * tau)
    with np.errstate(all='ignore'):n = np.where(root > 0, root * (alpha * w + root * th) / (root + alpha * w * th), alpha * w / (1 + alpha * w * tau))
    return (V * np.nan_to_num(n)[..., None, :]) @ np.swapaxes(V, -1, -2) / d[..., :, None] / d[..., None, :]


def tick(side, p, cost, kappa, market):
    """Best whole-cent price for one side, never crossing the market."""
    target = (p + side * (1 / kappa + cost)) * 100; first = math.floor(target + 1e-9) if side > 0 else math.ceil(target - 1e-9)
    low, high = (max(market + 1, 1), 99) if side > 0 else (1, min(market - 1, 99))
    if low > high:return None
    best = None; value = 0.
    for c in {min(max(first, low), high), min(max(first + side, low), high)}:
        depth = side * (c / 100 - p); v = math.exp(-kappa * depth) * (depth - cost)
        if v > value:best, value = c, v
    return best


def decide(bid, ask, q, minutes, same, est, strategy):
    """Each bin's quotes for one decision, as the replay builds them; None if the market's quotes are invalid."""
    bid = np.asarray(bid, float); ask = np.asarray(ask, float); q = np.asarray(q, float); K = len(bid)
    if not (np.all(bid >= 0) & np.all(ask <= 100) & np.all(bid <= ask)):return None
    mid = (bid + ask) / 200; total = mid.sum()
    if not total > 0:return None
    p = mid / total; cell = np.searchsorted(np.asarray(strategy['edges']), p); row = int(same)
    rate = np.asarray(est['rate'])[row, cell]; kappa = np.asarray(est['kappa'])[row, cell]; extra = np.asarray(est['adverse'])[row, cell]
    lot = strategy['lot']; cap = strategy['cap'] * lot
    if strategy.get('adverse_liquidity'):rate = rate * np.exp(-kappa * extra)   # adverse selection inside the liquidity term
    M = skew_matrix(p, rate, kappa, strategy['phi'], strategy['alpha'], minutes); own = np.diag(M); lean = 2 * M @ q / lot; out = []
    for k in range(K):
        mb, ma = int(bid[k]), int(ask[k]); buy, sell = own[k] + lean[k], own[k] - lean[k]; bids = {}; asks = {}
        if same:
            b = tick(-1, p[k], extra[k] + buy, kappa[k], ma); s = tick(1, p[k], extra[k] + sell, kappa[k], mb)
            if b is not None and b >= 1:bids[b] = lot
            if s is not None and s <= 99:asks[s] = lot
        out.append([[list(x) for x in sorted(bids.items(), reverse=True)], [list(x) for x in sorted(asks.items())], max(0., cap - q[k]), max(0., cap + q[k]), mb, ma])
    return out


def quote_parts(bid, ask, q, minutes, same, est, strategy):
    """What each bin's quotes are built from, in cents, for the dashboard."""
    bid = np.asarray(bid, float); ask = np.asarray(ask, float); q = np.asarray(q, float)
    if not (np.all(bid >= 0) & np.all(ask <= 100) & np.all(bid <= ask)):return None
    mid = (bid + ask) / 200; total = mid.sum()
    if not total > 0:return None
    p = mid / total; cell = np.searchsorted(np.asarray(strategy['edges']), p); row = int(same)
    rate = np.asarray(est['rate'])[row, cell]; kappa = np.asarray(est['kappa'])[row, cell]; extra = np.asarray(est['adverse'])[row, cell]
    if strategy.get('adverse_liquidity'):rate = rate * np.exp(-kappa * extra)
    M = skew_matrix(p, rate, kappa, strategy['phi'], strategy['alpha'], minutes); lean = 2 * M @ q / strategy['lot']
    return [dict(fair=round(100 * p[k], 3), half=round(100 / kappa[k], 3), eps=round(100 * extra[k], 3), own=round(100 * M[k, k], 3),
                 lean=round(100 * lean[k], 3)) for k in range(len(p))]
