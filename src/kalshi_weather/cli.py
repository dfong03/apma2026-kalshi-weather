"""Command line: download data, replay, refit, check the market, record books, trade, serve the dashboard, check keys."""
import argparse
import json
import numpy as np

SERIES = ['KXHIGHNY']


def period(text):
    """'train', 'test', 'early', or FIRST:LAST dates."""
    from .replay import PERIODS
    return PERIODS[text] if text in PERIODS else tuple(text.split(':'))


def replay(args):
    from .replay import load, simulate, summarise
    from .strategy import load_strategy
    strategy = load_strategy(args.strategy); first, last = period(args.period); total = None; out = {}
    for series in args.series:
        events = load(series, first, last, args.data)
        if not events:out[series] = 'no events: download the data first'; continue
        fills, peaks = simulate(events, strategy, strategy['estimates'][series], guard=not args.no_guard, calendar=not args.no_calendar)
        summary, daily = summarise(fills, peaks); out[series] = summary
        days = [e['day'] for e in events]; series_daily = dict(zip(days, daily))
        total = series_daily if total is None else {d: total[d] + series_daily[d] for d in total if d in series_daily}
    if total and len(args.series) > 1:
        daily = np.array(list(total.values())); out['together'] = dict(days=len(daily), dollars_per_day=round(float(daily.mean()), 2),
                                                                       daily_sharpe=round(float(daily.mean() / daily.std(ddof=1)), 3), worst_day=round(float(daily.min()), 2))
    print(json.dumps(out, indent=1))


def estimate(args):
    from .estimate import fit
    from .replay import load
    from .strategy import load_strategy
    strategy = load_strategy(args.strategy); first, last = period(args.period); kept = {}
    for series in args.series:
        strategy['estimates'][series], kept[series] = fit(load(series, first, last, args.data), strategy['estimates'][series])
    strategy['fitted'] = dict(first=first, last=last, cells_kept_from_prior=kept)
    with open(args.out, 'w') as handle:json.dump(strategy, handle, indent=1)
    print(f'wrote {args.out}; cells kept from the prior: {kept}')


def download(args):
    from . import data
    if args.source in ('quotes', 'trades'):
        for series in args.series:data.kalshi(series, args.source, args.first, args.last, args.data, args.workers)
    elif args.source == 'stations':data.station_reports(int(args.first[:4]), int(args.last[:4]), 'NYC', args.data)
    else:data.summary_messages(int(args.first[:4]), int(args.last[:4]), 'NYC', args.data)


def check(args):
    from . import checks
    first, last = period(args.period)
    print(json.dumps({series: getattr(checks, args.what)(series, first, last, args.data) for series in args.series}, indent=1))


def record(args):
    from .recorder import record as run
    print('recording', args.series, 'every', args.interval, 'seconds to', args.data, flush=True)
    print(run(args.series, args.interval, args.data, args.seconds), 'lines written', flush=True)


def trade(args):
    from .trader import Config, kill, run, watchdog
    cfg = Config.from_env()
    if args.command == 'run':run(cfg, args.once)
    elif args.command == 'kill':print(f'stopped; {kill(cfg)} orders left resting')
    else:print(watchdog(cfg))


def serve(args):
    from .serve import serve as run
    from .trader import Config
    run(Config.from_env().state, args.port)


def key(args):
    from .keycheck import check
    raise SystemExit(check(args.env, args.dir, args.fix, args.live))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='kalshi-weather', description=__doc__); sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('download', help='download quotes, trades, station reports or summary messages')
    p.add_argument('source', choices=['quotes', 'trades', 'stations', 'messages']); p.add_argument('first', help='YYYY-MM-DD'); p.add_argument('last', help='YYYY-MM-DD')
    p.add_argument('--series', nargs='*', default=SERIES); p.add_argument('--data', default='data'); p.add_argument('--workers', type=int, default=2); p.set_defaults(run=download)
    p = sub.add_parser('replay', help='replay the strategy on the trade tape')
    p.add_argument('--series', nargs='*', default=SERIES); p.add_argument('--period', default='test', help='train, test, early, or FIRST:LAST')
    p.add_argument('--data', default='data'); p.add_argument('--strategy', default=None, help='a strategy file (default: the shipped one)')
    p.add_argument('--no-guard', action='store_true'); p.add_argument('--no-calendar', action='store_true'); p.set_defaults(run=replay)
    p = sub.add_parser('estimate', help='refit fill rates and the adverse-selection term')
    p.add_argument('--series', nargs='*', default=SERIES); p.add_argument('--period', default='train'); p.add_argument('--data', default='data')
    p.add_argument('--strategy', default=None, help='the prior for thin cells (default: the shipped strategy)'); p.add_argument('--out', default='strategy.json'); p.set_defaults(run=estimate)
    p = sub.add_parser('check', help="check the market: the price's calibration, or the minutes in which its variance arrives")
    p.add_argument('what', choices=['calibration', 'variance']); p.add_argument('--series', nargs='*', default=SERIES)
    p.add_argument('--period', default='2025-10-01:2026-09-29', help='train, test, early, or FIRST:LAST'); p.add_argument('--data', default='data'); p.set_defaults(run=check)
    p = sub.add_parser('record', help='record live order books'); p.add_argument('series', nargs='*', default=SERIES)
    p.add_argument('--interval', type=float, default=2.); p.add_argument('--data', default='data'); p.add_argument('--seconds', type=float, default=None); p.set_defaults(run=record)
    p = sub.add_parser('trade', help='the live trader (settings from the environment)'); p.add_argument('command', choices=['run', 'kill', 'watchdog'])
    p.add_argument('--once', action='store_true'); p.set_defaults(run=trade)
    p = sub.add_parser('serve', help="the trader's dashboard, read-only, on 127.0.0.1 (state folder from TRADER_STATE)")
    p.add_argument('--port', type=int, default=8765); p.set_defaults(run=serve)
    p = sub.add_parser('key', help='check (and repair) an API key on disk without showing it'); p.add_argument('command', choices=['check'])
    p.add_argument('env', choices=['prod', 'demo']); p.add_argument('--dir', default=None, help='default: $KALSHI_KEY_DIR or ~/.kalshi')
    p.add_argument('--fix', action='store_true', help='repair permissions and the PEM layout'); p.add_argument('--live', action='store_true', help='read-only calls to Kalshi: balance, key scope, subaccount, netting')
    p.set_defaults(run=key)
    args = parser.parse_args(argv); args.run(args)


if __name__ == '__main__':
    main()
