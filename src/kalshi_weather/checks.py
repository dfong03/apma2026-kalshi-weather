"""Two checks on the market itself, behind the talk's slides 13 and 38: is the rescaled mid calibrated, and in which minutes of the hour
does its variance arrive. Both read only Kalshi's quote history (`kalshi-weather download quotes`)."""
from pathlib import Path
import math
import numpy as np

from .markets import MARKETS
from .replay import complete_events, grid, stamp


def mids(event, times):
    """Rescaled mids (summing to one) at each time, from whole-cent quotes; NaN rows where a bin lacks a quote."""
    bid, ask = grid(event, times); m = (np.round(bid * 100) + np.round(ask * 100)) / 200
    return m / m.sum(1, keepdims=True)


def events(series, first, last, root):
    spec = MARKETS[series]
    for day, e in complete_events(Path(root) / f'kalshi_quotes_{series}', first, last).items():
        yield day, e, stamp(day + 'T00:00') + spec['start'] * 3600


def calibration(series, first, last, root='data', draws=1000, seed=20261015):
    """Every 30 minutes from 9 hours before the climate day to 10 minutes before the close: each bin's rescaled mid and whether it won.
    Fits P(win) = sigma(a + b logit p) by maximum likelihood, with 95% intervals from resampling days."""
    rows = []
    for day, e, start in events(series, first, last, root):
        times = np.arange(start - 9 * 3600, e['close'] - 600, 1800.)
        for p in mids(e, times):
            if not np.isfinite(p).all():continue
            rows += [(day, float(pk), float(k == e['winner'])) for k, pk in enumerate(p) if .01 < pk < .99]
    if not rows:return 'no events: download the quotes first'
    days = np.array([r[0] for r in rows]); p = np.array([r[1] for r in rows]); won = np.array([r[2] for r in rows]); x = np.log(p / (1 - p))

    def fit(weight):   # Newton's method on the weighted log-likelihood
        w = np.array([0., 1.]); X = np.c_[np.ones_like(x), x]
        for _ in range(50):
            q = 1 / (1 + np.exp(-(X @ w))); step = np.linalg.solve((X * (weight * q * (1 - q))[:, None]).T @ X, X.T @ (weight * (won - q))); w = w + step
            if np.abs(step).max() < 1e-10:break
        return w

    a, b = fit(np.ones_like(x)); names, index = np.unique(days, return_inverse=True); rng = np.random.default_rng(seed)
    boots = np.array([fit(np.bincount(rng.integers(0, len(names), len(names)), minlength=len(names))[index].astype(float)) for _ in range(draws)])
    lo, hi = np.quantile(boots, [.025, .975], axis=0)
    table = []   # the slide's dots: per price bucket, the average over days of the share of bins that won
    for left, right in zip([.01, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, .95], [.05, .1, .2, .3, .4, .5, .6, .7, .8, .9, .95, .99]):
        m = (p >= left) & (p < right); ds = np.unique(days[m])
        price = np.mean([p[m & (days == d)].mean() for d in ds]); share = np.array([won[m & (days == d)].mean() for d in ds])
        table.append(dict(bucket=f'{left:.2f}-{right:.2f}', price=round(float(price), 4), won=round(float(share.mean()), 4), se=round(float(share.std() / math.sqrt(len(ds))), 4), days=len(ds)))
    return dict(series=series, first=first, last=last, days=len(names), rows=len(x), a=round(float(a), 3), a_ci95=[round(float(lo[0]), 3), round(float(hi[0]), 3)],
                b=round(float(b), 3), b_ci95=[round(float(lo[1]), 3), round(float(hi[1]), 3)], crosses_at=round(float(1 / (1 + math.exp(a / (b - 1)))), 4), buckets=table)


def variance(series, first, last, root='data'):
    """Share of the rescaled mids' squared one-minute changes, summed over bins, by the minute of the hour (UTC) in which the change happened."""
    share = np.zeros(60); days = 0
    for day, e, start in events(series, first, last, root):
        every = np.arange(start, e['close'], 60.); p = mids(e, every); d = ((p[1:] - p[:-1]) ** 2).sum(1); ok = np.isfinite(d)
        np.add.at(share, ((every[:-1][ok] // 60) % 60).astype(int), d[ok]); days += 1
    if not days:return 'no events: download the quotes first'
    share = share / share.sum(); spec = MARKETS[series]   # the price moves in the two minutes after a report's or a message's issue minute
    report = [spec['minute'] + 1, spec['minute'] + 2]; summary = sorted({m + i for _, m in spec['summaries'] for i in (1, 2)})
    return dict(series=series, first=first, last=last, days=days, report_minutes=report, report_share=round(float(share[report].sum()), 4),
                summary_minutes=summary, summary_share=round(float(share[summary].sum()), 4), those_minutes_share=round(float(share[report + summary].sum()), 4),
                by_minute=[round(float(s), 5) for s in share])
