# US Theme Rotation Board

Replaces the Excel "Themes" tab with an auto-refreshing web dashboard.

## Setup (once)
1. Create a GitHub repo, push this folder.
2. Settings → Pages → Source: `main` branch, `/docs` folder.
3. Actions run automatically on schedule (Tue–Sat UTC = after each US session);
   dashboard URL: `https://<user>.github.io/<repo>/`

## How it works
- `.github/workflows/refresh.yml` — cron at 21:15 & 22:15 UTC (~08:15 Sydney year-round)
- `scripts/build_data.py` — fetches 2y daily OHLC (yfinance, Stooq fallback),
  computes all Themes-tab metrics, writes `docs/snapshot.json`
- `docs/index.html` — static page, reads snapshot.json, renders leaderboards + group tables

## Fixes vs. the Excel tab
- `%52H`: corrected sign convention (0 = at 52wk high, negative below)
- `RS_STS`: percentile-inclusive over 63 sessions — no more #NUM! on young ETFs
- NLR duplicate-of-IBAT row bug does not carry over (each ticker fetched independently)

## Edit the universe
`scripts/universe.json` — add/remove `{group, theme, ticker, long, short}` entries.
