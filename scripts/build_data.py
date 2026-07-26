#!/usr/bin/env python3
"""
US Rotation Board — data pipeline.
Fetches daily OHLC for every ticker in universe.json (plus the equal-weight
sector funds needed for the Breadth column and SPY as the RS benchmark),
computes all dashboard metrics, and writes docs/snapshot.json.

Runs headless in GitHub Actions after the US close.

METRIC DEFINITIONS
  daily        close / prev_close - 1
  roll_w       close / close[-6]  - 1          (5 sessions)
  roll_m       close / close[-22] - 1          (21 sessions)
  ytd          close / last close of prior year - 1
  w1/m1/y1     calendar 7/30/365-day lookback (nearest prior session)
  off52h       close / max(close, 252) - 1     (always <= 0; 0 = at 52wk high)
  vs10/21/50   close / SMA(n) - 1
  g6_50        SMA6  > SMA50   ("YES"/"NO")
  g21_50       SMA21 > SMA50
  rs_1m        excess rolling-monthly return vs SPY
  rs_sts       percentile rank of RS line within trailing 63 sessions
  rs_rating    1-99 cross-universe rank, IBD-style weighted 3/6/9/12M excess vs SPY
  breadth_*    equal-weight sector return minus cap-weight return (d / w / m)
  mcap, exp    live AUM + expense ratio (only for tickers with no leveraged pair)
"""
import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
UNIVERSE = json.loads((ROOT / "scripts" / "universe.json").read_text())
OUT = ROOT / "docs" / "snapshot.json"

# Cap-weight sector -> equal-weight counterpart. The EW funds are fetched but
# never emitted as rows; they exist only to compute the Breadth spread.
EW_PAIRS = {
    "XLRE": "RSPR", "XLU": "RSPU", "XLV": "RSPH", "XLF": "RSPF",
    "XLP":  "RSPS", "XLB": "RSPM", "XLE": "RSPG", "XLI": "RSPN",
    "XLY":  "RSPD", "XLC": "RSPC", "XLK": "RSPT",
}

DISPLAY = [u["ticker"] for u in UNIVERSE]
TICKERS = sorted(set(DISPLAY) | set(EW_PAIRS.values()) | {"SPY"})


# ─── fetching ────────────────────────────────────────────────────────────────
def fetch_history() -> pd.DataFrame:
    import yfinance as yf
    return yf.download(TICKERS, period="2y", interval="1d", auto_adjust=True,
                       progress=False, group_by="ticker", threads=True)


def stooq_fallback(ticker: str):
    try:
        from pandas_datareader import data as pdr
        return pdr.DataReader(f"{ticker}.US", "stooq").sort_index()
    except Exception:
        return None


def series_for(t: str, hist):
    """Close/Open for a ticker: batch -> solo yfinance retry -> Stooq."""
    try:
        close, open_ = hist[t]["Close"], hist[t]["Open"]
    except (KeyError, TypeError):
        close = open_ = pd.Series(dtype=float)

    if close.dropna().empty:
        try:
            import yfinance as yf
            solo = yf.download(t, period="2y", interval="1d",
                               auto_adjust=True, progress=False)
            if not solo.empty:
                close, open_ = solo["Close"].squeeze(), solo["Open"].squeeze()
        except Exception:
            pass

    if close.dropna().empty:
        sq = stooq_fallback(t)
        if sq is not None and not sq.empty:
            close, open_ = sq["Close"], sq["Open"]

    return close, open_


# ─── metrics ─────────────────────────────────────────────────────────────────
def metrics_for(close: pd.Series, open_: pd.Series, spy: pd.Series) -> dict:
    close = close.dropna()
    if len(close) < 60:
        return {}
    c = float(close.iloc[-1])
    m = {"price": round(c, 4)}

    o = open_.dropna()
    m["intraday"] = float(c / o.iloc[-1] - 1) if len(o) else None
    m["daily"] = float(c / close.iloc[-2] - 1)
    m["roll_w"] = float(c / close.iloc[-6] - 1)
    m["roll_m"] = float(c / close.iloc[-22] - 1) if len(close) >= 22 else None

    prior = close[close.index < pd.Timestamp(close.index[-1].year, 1, 1)]
    m["ytd"] = float(c / prior.iloc[-1] - 1) if len(prior) else None

    for key, days in (("w1", 7), ("m1", 30), ("y1", 365)):
        ref = close[close.index <= close.index[-1] - pd.Timedelta(days=days)]
        m[key] = float(c / ref.iloc[-1] - 1) if len(ref) else None

    m["off52h"] = float(c / close.tail(252).max() - 1)

    for n in (6, 10, 21, 50):
        m[f"sma{n}"] = float(close.tail(n).mean()) if len(close) >= n else None
    m["vs10"] = c / m["sma10"] - 1
    m["vs21"] = c / m["sma21"] - 1
    m["vs50"] = c / m["sma50"] - 1
    m["g6_50"] = "YES" if m["sma6"] > m["sma50"] else "NO"
    m["g21_50"] = "YES" if m["sma21"] > m["sma50"] else "NO"

    rs = (close / spy.reindex(close.index)).dropna().tail(63)
    m["rs_sts"] = float((rs <= rs.iloc[-1]).mean()) if len(rs) >= 21 else None

    if len(close) >= 22 and len(spy) >= 22:
        m["rs_1m"] = float((c / close.iloc[-22]) / (spy.iloc[-1] / spy.iloc[-22]) - 1)
    else:
        m["rs_1m"] = None

    def xret(n):
        if len(close) < n or len(spy) < n:
            return None
        return float((c / close.iloc[-n]) / (spy.iloc[-1] / spy.iloc[-n]) - 1)

    parts = [(xret(63), .4), (xret(126), .2), (xret(189), .2), (xret(252), .2)]
    valid = [(v, w) for v, w in parts if v is not None]
    m["rs_raw"] = sum(v * w for v, w in valid) / sum(w for _, w in valid) if valid else None
    return m


def fetch_fund_meta(t: str) -> dict:
    """Live AUM + expense ratio. Only called for tickers with no leveraged pair."""
    out = {}
    try:
        import yfinance as yf
        info = yf.Ticker(t).info or {}
        ta = info.get("totalAssets")
        if ta:
            out["mcap"] = float(ta)
        er = (info.get("netExpenseRatio")
              or info.get("annualReportExpenseRatio")
              or info.get("expenseRatio"))
        if er:
            er = float(er)
            # Yahoo returns these fields in PERCENT units (0.09 == 0.09%), verified
            # against published iShares fact sheets. Convert to a fraction.
            frac = er / 100.0
            # Sanity band: real expense ratios span ~0.01% to ~5%. If dividing puts
            # the value outside that band, the source was already a fraction.
            if not (0.00005 <= frac <= 0.05):
                frac = er
            if 0.00005 <= frac <= 0.05:
                out["exp"] = round(frac, 6)
    except Exception:
        pass
    return out


# ─── main ────────────────────────────────────────────────────────────────────
def main():
    hist = fetch_history()
    spy_close, _ = series_for("SPY", hist)
    spy = spy_close.dropna()
    if spy.empty:
        sys.exit("SPY unavailable — cannot compute relative strength")

    # equal-weight sector metrics (fetched, never displayed)
    ew_metrics = {}
    for ewt in set(EW_PAIRS.values()):
        c, o = series_for(ewt, hist)
        ew_metrics[ewt] = metrics_for(c, o, spy)

    rows = []
    for u in UNIVERSE:
        t = u["ticker"]
        close, open_ = series_for(t, hist)
        m = metrics_for(close, open_, spy)

        if not (u.get("long") or u.get("short")):
            m.update(fetch_fund_meta(t))

        row = {**u, **m}

        ewt = EW_PAIRS.get(t)
        if ewt:
            row["ew_ticker"] = ewt
            e = ew_metrics.get(ewt) or {}
            for src, dst in (("daily", "breadth_d"), ("roll_w", "breadth_w"),
                             ("roll_m", "breadth_m")):
                if e.get(src) is not None and m.get(src) is not None:
                    row[dst] = round(e[src] - m[src], 6)

        rows.append(row)

    # cross-universe IBD-style RS Rating (1-99)
    scored = [(i, r["rs_raw"]) for i, r in enumerate(rows) if r.get("rs_raw") is not None]
    if len(scored) > 1:
        ranked = sorted(scored, key=lambda x: x[1])
        n = len(ranked)
        for rank, (i, _) in enumerate(ranked):
            rows[i]["rs_rating"] = max(1, min(99, round(rank / (n - 1) * 98 + 1)))

    OUT.write_text(json.dumps({
        "as_of": str(spy.index[-1].date()),
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", ""),
        "ew_pairs": EW_PAIRS,
        "rows": rows,
    }, indent=1))

    missing = [r["ticker"] for r in rows if "price" not in r]
    print(f"wrote {len(rows)} rows, as_of {spy.index[-1].date()}; "
          f"missing data: {missing or 'none'}")
    if len(missing) > len(rows) * 0.3:
        sys.exit(1)   # fail loudly rather than publish a mostly-empty board


if __name__ == "__main__":
    main()
