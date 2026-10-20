"""Fit the fill-rate curve and the adverse-selection term from the trade tape.

Hypothetical quotes 1 to 10 cents from fair value are filled by the replay's fill rule, and the curve is fitted by
maximum likelihood for each same-day flag and fair-value bucket. Thin cells keep the prior estimates.
"""
import numpy as np
from scipy.optimize import minimize_scalar

from .markets import EDGES, scheduled
from .replay import FILL, exhausting, grid

DEPTHS = 10; OFFSETS = 5; LOT = 100.; THIN = 50000


def tallies(ev, touch, improve):
    """One event: fills, quote-minutes and depths of the hypothetical quotes, and how their fills settled."""
    K = ev['bid'].shape[1]; index = np.repeat(np.arange(len(ev['times'])), np.diff(ev['edge'])); k = np.array(ev['bin']); P = np.array(ev['price']); n = np.array(ev['size']); s = np.array(ev['side']); used = exhausting(ev)
    every = np.arange(ev['source']['open'] // 60 * 60, ev['source']['close'], 60.); gb, ga = (np.round(g * 100) for g in grid(ev['source'], every)); pm = (gb + ga) / 200; pm = pm / pm.sum(1, keepdims=True); good = np.isfinite(pm).all(1)
    same = (every >= ev['start']).astype(int); good &= ~scheduled(every, ev['start'], ev['spec'], summary=(0, 180))[0]
    row = np.clip(np.searchsorted(every, ev['times'][index]), 0, len(every) - 1); inside = (every[row] == ev['times'][index]) & ~np.array(ev['pull']); cell = np.searchsorted(EDGES, np.nan_to_num(pm))
    filled = np.zeros((2, 5, OFFSETS, DEPTHS)); count = filled.copy(); depth = filled.copy(); lost = np.zeros((2, 5)); win = np.array([float(i == ev['winner']) for i in range(K)])
    for i in range(K):
        m = (k == i) & inside
        for sign in (1, -1):
            first = (np.floor(pm[:, i] * 100 + 1e-9) + 1) if sign > 0 else (np.ceil(pm[:, i] * 100 - 1e-9) - 1); market = ga[:, i] if sign > 0 else gb[:, i]; take = m & (s == sign); r = row[take]
            with np.errstate(invalid='ignore'):offset = np.clip(np.ceil(np.nan_to_num(sign * (first - pm[:, i] * 100)) * OFFSETS) - 1, 0, OFFSETS - 1).astype(int)
            for j in range(DEPTHS):
                quote = first + sign * j; through = sign * (P[take] - quote[r]); share = np.where(through == 0, touch * used[take], np.where(sign * (P[take] - market[r]) > 0, 1., improve)) * (through >= 0)
                got = np.minimum(np.bincount(r, share * n[take], minlength=len(every)), LOT) / LOT; live = good & (quote >= 1) & (quote <= 99); where = (same[live], cell[live, i], offset[live])
                np.add.at(filled[..., j], where, got[live]); np.add.at(lost, where[:2], got[live] * sign * (win[i] - pm[live, i])); np.add.at(count[..., j], where, 1.); np.add.at(depth[..., j], where, sign * (quote[live] / 100 - pm[live, i]))
    return filled, count, depth, lost


def volume_mle(filled, count, depth):
    """Maximum-likelihood fit of the fill curve for one cell."""
    use = count > 0; F, N, d = filled[use], count[use], depth[use]
    if F.sum() <= 0:return None
    rate = lambda kappa: F.sum() / (N * np.exp(-kappa * d)).sum(); best = minimize_scalar(lambda kappa: -(F * (np.log(rate(kappa)) - kappa * d)).sum() + F.sum(), bounds=(1., 400.), method='bounded')
    return float(rate(best.x)), float(best.x)


def fit(events, prior, fill=FILL):
    """Fit one market's estimates; returns them and the cells kept from the prior."""
    touch, improve = fill; out = {k: np.asarray(v, float).tolist() for k, v in prior.items() if k in ('rate', 'kappa', 'adverse')}; kept = []
    F, N, G, L = [np.sum(x, 0) for x in zip(*[tallies(ev, touch, improve) for ev in events])]
    for d in (0, 1):
        for c in range(5):
            if N[d, c, :, 0].sum() < THIN or F[d, c].sum() <= 0:kept.append([d, c]); continue
            out['rate'][d][c], out['kappa'][d][c] = volume_mle(F[d, c], N[d, c], np.nan_to_num(G[d, c] / np.maximum(N[d, c], 1))); out['adverse'][d][c] = min(max(float(L[d, c] / F[d, c].sum()), 0.), .05)
    return out, kept
