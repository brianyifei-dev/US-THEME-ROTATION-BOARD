#!/usr/bin/env python3
"""
Fetch daily OHLC for all theme ETFs and compute the Themes-tab metrics.
Runs headless in GitHub Actions after US close. Output: docs/snapshot.json

Metric definitions (replicating the Excel tab, bugs fixed):
  daily        = close / prev_close - 1
  roll_w       = close / close[5 sessions ago] - 1
  roll_m       = close / close[21 sessions ago] - 1
  ytd          = close / last close of prior year - 1
  w1 / m1 / y1 = calendar 7/30/365-day lookups (nearest prior session)
  off52h       = close / max(close, 252 sessions) - 1        # FIXED sign convention
  vs10/21/50   = close / SMA(n) - 1
  g6_50        = SMA6 > SMA50 ; g21_50 = SMA21 > SMA50
  rs_line      = close / SPY_close
  rs_sts       = percentile rank (inclusive) of today's RS value within
                 trailing 63 sessions of RS values                # FIXED: no #NUM! on short history (needs >=21 obs, else null)
  intraday     = close / open - 1
"""
import json, sys, time, datetime as dt
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
UNIVERSE = json.loads((ROOT / "scripts" / "universe.json").read_text())
OUT = ROOT / "docs" / "snapshot.json"

TICKERS = sorted({u["ticker"] for u in UNIVERSE} | {"SPY"})


def fetch_history() -> pd.DataFrame:
    """Adjusted daily closes + opens, ~420 sessions. yfinance primary, stooq fallback."""
    import yfinance as yf
    df = yf.download(TICKERS, period="2y", interval="1d",
                     auto_adjust=True, progress=False, group_by="ticker", threads=True)
    return df


def stooq_fallback(ticker: str) -> pd.DataFrame | None:
    try:
        from pandas_datareader import data as pdr
        d = pdr.DataReader(f"{ticker}.US", "stooq").sort_index()
        return d
    except Exception:
        return None


def metrics_for(close: pd.Series, open_: pd.Series, spy: pd.Series) -> dict:
    close = close.dropna()
    if len(close) < 60:
        return {}
    c = close.iloc[-1]
    m = {}
    m["price"] = round(float(c), 4)
    m["intraday"] = float(c / open_.dropna().iloc[-1] - 1) if len(open_.dropna()) else None
    m["daily"] = float(c / close.iloc[-2] - 1)
    m["roll_w"] = float(c / close.iloc[-6] - 1)
    m["roll_m"] = float(c / close.iloc[-22] - 1) if len(close) >= 22 else None
    # YTD: last close strictly before Jan 1 of current year
    year = close.index[-1].year
    prior = close[close.index < pd.Timestamp(year, 1, 1)]
    m["ytd"] = float(c / prior.iloc[-1] - 1) if len(prior) else None
    # calendar lookbacks
    for key, days in (("w1", 7), ("m1", 30), ("y1", 365)):
        cutoff = close.index[-1] - pd.Timedelta(days=days)
        ref = close[close.index <= cutoff]
        m[key] = float(c / ref.iloc[-1] - 1) if len(ref) else None
    # 52wk high (fixed sign: at high = 0, below high = negative)
    hi = close.tail(252).max()
    m["off52h"] = float(c / hi - 1)
    # SMAs
    for n in (6, 10, 21, 50):
        m[f"sma{n}"] = float(close.tail(n).mean()) if len(close) >= n else None
    m["vs10"] = c / m["sma10"] - 1
    m["vs21"] = c / m["sma21"] - 1
    m["vs50"] = c / m["sma50"] - 1
    m["g6_50"] = "YES" if m["sma6"] > m["sma50"] else "NO"
    m["g21_50"] = "YES" if m["sma21"] > m["sma50"] else "NO"
    # RS vs SPY + percentile rank over trailing 63 sessions (inclusive -> no #NUM!)
    rs = (close / spy.reindex(close.index)).dropna().tail(63)
    m["rs_sts"] = float((rs <= rs.iloc[-1]).mean()) if len(rs) >= 21 else None
    # 1-Month RS: excess rolling-monthly return vs SPY
    if len(close) >= 22 and len(spy) >= 22:
        m["rs_1m"] = float((c / close.iloc[-22]) / (spy.iloc[-1] / spy.iloc[-22]) - 1)
    else:
        m["rs_1m"] = None
    return m


def main():
    hist = fetch_history()
    spy = hist["SPY"]["Close"].dropna()
    rows = []
    for u in UNIVERSE:
        t = u["ticker"]
        try:
            close, open_ = hist[t]["Close"], hist[t]["Open"]
        except KeyError:
            close = open_ = pd.Series(dtype=float)
        if close.dropna().empty:
            sq = stooq_fallback(t)
            if sq is not None and not sq.empty:
                close, open_ = sq["Close"], sq["Open"]
        m = metrics_for(close, open_, spy)
        rows.append({**u, **m})
        # keys: group, theme, ticker, long, short + metrics

    as_of = str(spy.index[-1].date())
    OUT.write_text(json.dumps({"as_of": as_of,
                               "generated_utc": dt.datetime.utcnow().isoformat(timespec="seconds"),
                               "rows": rows}, indent=1))
    missing = [r["ticker"] for r in rows if "price" not in r]
    print(f"wrote {len(rows)} rows, as_of {as_of}; missing data: {missing or 'none'}")
    if len(missing) > len(rows) * 0.3:
        sys.exit(1)  # fail the workflow loudly rather than publish a mostly-empty board


if __name__ == "__main__":
    main()
