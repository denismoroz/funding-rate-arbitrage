"""Build frab_funding_current.csv from cached raw/ CSVs + fast Paradex snapshot.

Paradex history is impractical here (its /funding/data stream is a 5s instantaneous
8h-rate feed; sampling 90d at 4h steps took ~8min/coin). We keep the one already-fetched
Paradex BTC 90d-sampled row and add a fast CURRENT-SNAPSHOT row per coin from
/markets/summary (window='snapshot'), clearly distinct from trailing windows.
"""
import json
import datetime as dt
from pathlib import Path
from urllib import request, error

import pandas as pd

HERE = Path(__file__).parent
RAW = HERE / "raw"
COINS = ["BTC", "ETH", "SOL", "HYPE", "AVAX", "LINK", "DOGE", "ZEC", "XPL"]
NOW = dt.datetime(2026, 6, 19, tzinfo=dt.timezone.utc)
W90 = NOW - dt.timedelta(days=90)
W365 = NOW - dt.timedelta(days=365)
FETCHED = "2026-06-19T16:00:00Z"
UA = "Mozilla/5.0 (research-funding-scout)"


def annualize(rate: pd.Series) -> pd.Series:
    rate = rate.sort_index()
    gap_h = rate.index.to_series().diff().shift(-1).dt.total_seconds() / 3600.0
    gap_h = gap_h.fillna(gap_h.median() if len(rate) > 1 else 1.0).clip(lower=1.0, upper=8.0)
    hourly_equiv = rate / gap_h.values
    hourly = hourly_equiv.resample("1h").ffill().dropna()
    return hourly * 8760 * 100.0


def median_interval_h(idx):
    if len(idx) < 2:
        return None
    d = pd.Series(idx).diff().dropna().dt.total_seconds() / 3600.0
    return round(float(d.median()), 2)


def wstats(ann, since):
    w = ann[ann.index >= since]
    if len(w) == 0:
        return None
    return {"annualized_pct": round(float(w.mean()), 2),
            "neg_hours_pct": round(float((w < 0).mean() * 100), 1),
            "n_points": int(len(w))}


def paradex_snapshot(coin):
    url = f"https://api.prod.paradex.trade/v1/markets/summary?market={coin}-USD-PERP"
    req = request.Request(url, headers={"User-Agent": UA})
    try:
        with request.urlopen(req, timeout=15) as r:
            j = json.loads(r.read().decode())
    except error.HTTPError:
        return None
    res = j.get("results", [])
    if not res:
        return None
    fr = res[0].get("funding_rate")
    if fr is None:
        return None
    # instantaneous 8h-equiv fraction -> APR
    return round(float(fr) / 8.0 * 8760 * 100.0, 2)


def main():
    rows = []
    for venue in ["HL", "Aster", "dYdX", "Lighter"]:
        for coin in COINS:
            p = RAW / f"{venue}_{coin}.csv"
            if not p.exists():
                continue
            df = pd.read_csv(p)
            idx = pd.to_datetime(df.iloc[:, 0], utc=True, format="ISO8601")
            s = pd.Series(df["rate_fraction"].values, index=idx).sort_index()
            iv = median_interval_h(s.index)
            ann = annualize(s)
            for win, since in [("90d", W90), ("365d", W365)]:
                st = wstats(ann, since)
                if st:
                    rows.append({"venue": venue, "coin": coin, "window": win,
                                 **st, "interval_h": iv, "fetched_at": FETCHED})
    # Paradex BTC 90d-sampled (already fetched)
    pbtc = RAW / "Paradex_BTC.csv"
    if pbtc.exists():
        df = pd.read_csv(pbtc)
        idx = pd.to_datetime(df.iloc[:, 0], utc=True, format="ISO8601")
        s = pd.Series(df["rate_fraction"].values, index=idx).sort_index()
        ann = (s / 8.0) * 8760 * 100.0
        st = wstats(ann, W90)
        if st:
            rows.append({"venue": "Paradex", "coin": "BTC", "window": "90d_sampled",
                         **st, "interval_h": 8.0, "fetched_at": FETCHED})
    # Paradex current snapshot for all coins (fast, 1 req/coin)
    for coin in COINS:
        apr = paradex_snapshot(coin)
        if apr is not None:
            rows.append({"venue": "Paradex", "coin": coin, "window": "snapshot",
                         "annualized_pct": apr, "neg_hours_pct": None, "n_points": 1,
                         "interval_h": 8.0, "fetched_at": FETCHED})

    df = pd.DataFrame(rows, columns=["venue", "coin", "window", "annualized_pct",
                                     "neg_hours_pct", "n_points", "interval_h", "fetched_at"])
    df.to_csv(HERE / "frab_funding_current.csv", index=False)
    print(f"wrote frab_funding_current.csv ({len(df)} rows)")
    # quick best-venue-per-coin (365d trailing, history venues only)
    h = df[(df.window == "365d")]
    print("\nBest 365d per coin (HL/Aster/dYdX/Lighter):")
    for coin in COINS:
        sub = h[h.coin == coin].sort_values("annualized_pct", ascending=False)
        if len(sub):
            b = sub.iloc[0]
            print(f"  {coin}: {b.venue} {b.annualized_pct}%")


if __name__ == "__main__":
    main()
