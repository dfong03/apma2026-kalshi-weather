# apma2026-kalshi-weather

Market making on Kalshi's daily high-temperature contracts for New York (Central Park): a replay against Kalshi's public trade tape, and the live trader.

Every minute the strategy reads each bin's best bid and ask, takes the rescaled mids as fair values, and posts post-only bids and asks around them:

- **Distance** from fair value: `1/κ + ε` (fill-rate fall-off and adverse selection), plus an inventory cost: one 10-contract quote a side, at `1/κ + ε + Mₖₖ ∓ 2(Mq)ₖ`.
- **Inventory**: the bins are priced together through the settlement covariance `Σ = diag(p) − ppᵀ`. The inventory matrix `M` solves `dM/dτ = φΣ − MDM` in closed form, and a position in one bin moves the quotes in every bin by `2(Mq)ₖ`.
- **Calendar**: no quotes in the minutes when station reports and daily summary messages move the price.
- **Guard**: no bids in bins that Central Park's readings have already ruled out (New York).

`src/kalshi_weather/strategy.json` holds the penalties (chosen) and the fill rates, κ and ε (fitted), all on October 2025 to March 2026.

## Install

Python 3.12 or later, with [uv](https://docs.astral.sh/uv/):

```
uv sync
```

This installs the `kalshi-weather` command into `.venv`; run it as `uv run kalshi-weather ...`.

## Replay

Data comes from Kalshi's public API and the Iowa Environmental Mesonet (no account needed). For April to September 2026:

```
uv run kalshi-weather download quotes 2026-04-01 2026-09-29
uv run kalshi-weather download trades 2026-04-01 2026-09-29
uv run kalshi-weather download stations 2026-01-01 2026-12-31
uv run kalshi-weather download messages 2026-01-01 2026-12-31
uv run kalshi-weather replay --period test
```

`--period` takes `train` (October 2025 to March 2026), `test` (April to September 2026), `early` (January to September 2025) or `FIRST:LAST`. The output reports dollars a day, the daily Sharpe ratio and the worst day.

Fill rule for a historical taker order, capped by our quote size and the position limit:

- through our price and the market's quote: filled in full;
- through our price only because we improved on the market: a quarter;
- exactly at our price: in full if that level was then used up, else nothing (no queue position is archived, so a new order joins the back of the queue).

## Refit

```
uv run kalshi-weather estimate --period train --out strategy.json
uv run kalshi-weather replay --strategy strategy.json
```

This refits the fill rates, κ and ε by same-day flag and fair-value bucket. Cells with fewer than 50,000 quote-minutes keep the values of the strategy given with `--strategy` (by default, the shipped one).

## Record order books

```
uv run kalshi-weather record KXHIGHNY
```

Every level and size of every open market, every 2 seconds, appended as gzip JSON lines under `data/kalshi_books_<SERIES>/`. Kalshi serves no historical books.

## Live trading

Keys stay on disk and are never printed. Put them in `~/.kalshi/` (or `$KALSHI_KEY_DIR`) as `<env>_key.pem` and `<env>_key_id`, readable by you only (`chmod 600`).

Settings come from the environment:

| Variable | Default | Meaning |
| --- | --- | --- |
| `TRADER_ENV` | `prod` | `prod` or `demo` |
| `TRADER_SERIES` | `KXHIGHNY` | the series to quote |
| `TRADER_SIZE` | 10 | contracts a side, per bin |
| `TRADER_QMAX` | 50 | position limit per bin, in contracts |
| `TRADER_LOSS` | 10 | daily loss limit in dollars, marked to fair value (two minutes running) |
| `TRADER_SUBACCOUNT` | none | the subaccount, unless the key is restricted to one |
| `TRADER_STATE` | `data/trader` | state, status and logs |
| `TRADER_STRATEGY` | the shipped one | a strategy file |
| `ALERT_TOPIC_ARN` | none | an AWS SNS topic for watchdog alerts (optional) |

```
uv run kalshi-weather trade run        # the loop, once a minute
uv run kalshi-weather trade kill       # STOP file, trigger the order group, cancel everything
uv run kalshi-weather trade watchdog   # if the loop has written no status for 2 minutes: kill and alert
```

Before the first run, check the key (permissions, PEM layout, a local signature; `--live` adds read-only calls for the balance, the key's subaccount restriction and collateral netting; `--fix` repairs permissions and a mangled PEM). Nothing secret is printed:

```
uv run kalshi-weather key check prod --live
```

A file named `STOP` in the state folder stops all quoting until it is removed. Orders are post-only, placed in an order group that caps fills at 25 contracts in any 15 seconds, and expire 3 minutes after their last extension, or 1 second before the next calendar window opens if that is sooner (so none stand inside a window even if a minute's cycle runs late). If the trader dies, everything is gone within 3 minutes. Positions are held to settlement.

## Dashboard

```
uv run kalshi-weather serve            # http://127.0.0.1:8765/
```

Read-only: it serves the page and `/state` (the trader's latest state and status, the STOP flag and the last two hours of its minute log, from `TRADER_STATE`) and accepts no other requests. It binds to 127.0.0.1; from another machine, use a tunnel (for example `ssh -L 8765:127.0.0.1:8765 <host>`). Per bin: fair value each minute, our best bid and ask, fills, and how the selected bin's quotes are built from fair value, 1/κ, ε, the own-risk term Mₖₖ and inventory.
