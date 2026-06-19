"""Fresh cross-venue funding scout — trailing 90d & 365d through 2026-06-19.

LOCAL research only (public APIs). Pulls per-settlement funding for the FRAB coin
set across 5 perp venues and annualizes CADENCE-AGNOSTICALLY (rate spread over the
hours each settlement covers, ×8760) so a venue's interval change never mis-scales.

Venues & semantics (verified 2026-06-19):
  HL       POST /info {"type":"fundingHistory"} — hourly, fundingRate = per-hour fraction (signed).
  Aster    /fapi/v1/fundingRate (Binance-style) — 8h, fundingRate = per-8h fraction (signed).
  dYdX v4  /historicalFunding/{T}-USD — hourly, rate = per-hour fraction (signed).
  Lighter  /api/v1/fundings?market_id=&resolution=1h — hourly, rate = per-hour PERCENT,
           sign carried by `direction` ('long'=longs pay=+, 'short'=-).
  Paradex  /v1/funding/data?market=-PERP — 8h, funding_rate = per-8h fraction (signed),
           funding_period_hours confirms interval; paginated via `next` cursor.

Writes per-venue raw CSV under raw/ and the summary frab_funding_current.csv.
"""
import time
import json
import datetime as dt
from pathlib import Path
from urllib import request, parse, error

import pandas as pd

HERE = Path(__file__).parent
RAW = HERE / "raw"
RAW.mkdir(exist_ok=True)

COINS = ["BTC", "ETH", "SOL", "HYPE", "AVAX", "LINK", "DOGE", "ZEC", "XPL"]

NOW = dt.datetime(2026, 6, 19, tzinfo=dt.timezone.utc)
W90 = NOW - dt.timedelta(days=90)
W365 = NOW - dt.timedelta(days=365)
START_MS = int(W365.timestamp() * 1000)
UA = "Mozilla/5.0 (research-funding-scout)"


def http_get(url, headers=None, timeout=20):
    req = request.Request(url, headers=headers or {"User-Agent": UA})
    with request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def http_post(url, payload, timeout=20, retries=5):
    data = json.dumps(payload).encode()
    for attempt in range(retries):
        try:
            req = request.Request(url, data=data, headers={
                "Content-Type": "application/json", "User-Agent": UA})
            with request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except error.HTTPError as e:
            if e.code in (429, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt)  # exp backoff: 1,2,4,8s
                continue
            raise


def annualize(rate: pd.Series) -> pd.Series:
    """Cadence-agnostic hourly annualized-% series from signed per-settlement FRACTIONS.

    Each settlement -> per-hour equivalent (rate/interval_h), spread over its hours,
    ×8760×100. interval_h = gap to NEXT settlement, clipped [1,8]. Correct for any cadence.
    """
    rate = rate.sort_index()
    gap_h = rate.index.to_series().diff().shift(-1).dt.total_seconds() / 3600.0
    gap_h = gap_h.fillna(gap_h.median() if len(rate) > 1 else 1.0).clip(lower=1.0, upper=8.0)
    hourly_equiv = rate / gap_h.values
    hourly = hourly_equiv.resample("1h").ffill().dropna()
    return hourly * 8760 * 100.0


# ---------------- fetchers: return signed per-settlement FRACTION series (UTC idx) ----

def fetch_hl(coin):
    recs, start = [], START_MS
    while True:
        data = http_post("https://api.hyperliquid.xyz/info",
                         {"type": "fundingHistory", "coin": coin, "startTime": start})
        if not data:
            break
        recs.extend(data)
        last = data[-1]["time"]
        if len(data) < 500 or last >= int(NOW.timestamp() * 1000):
            break
        if last <= start:
            break
        start = last + 1
        time.sleep(0.5)  # HL pages 500 rows; ~18 pages/coin — be gentle to avoid 429
    if not recs:
        return pd.Series(dtype=float)
    df = pd.DataFrame(recs).drop_duplicates("time")
    t = pd.to_datetime(df["time"], unit="ms", utc=True)
    s = pd.Series(df["fundingRate"].astype(float).values, index=t).sort_index()
    return s[~s.index.duplicated()]


def fetch_aster(coin):
    sym = f"{coin}USDT"
    recs, start = [], START_MS
    while True:
        url = f"https://fapi.asterdex.com/fapi/v1/fundingRate?{parse.urlencode({'symbol': sym, 'startTime': start, 'limit': 1000})}"
        try:
            data = http_get(url)
        except error.HTTPError as e:
            if e.code == 400:
                return pd.Series(dtype=float)  # symbol not listed
            raise
        if not data:
            break
        recs.extend(data)
        last = data[-1]["fundingTime"]
        if len(data) < 1000:
            break
        start = last + 1
        time.sleep(0.15)
    if not recs:
        return pd.Series(dtype=float)
    df = pd.DataFrame(recs).drop_duplicates("fundingTime")
    t = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    s = pd.Series(df["fundingRate"].astype(float).values, index=t).sort_index()
    return s[~s.index.duplicated()]


def fetch_dydx(coin):
    ticker = f"{coin}-USD"
    recs = {}
    cursor = NOW
    for _ in range(120):  # 120*1000h >> 365d
        iso = cursor.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        url = f"https://indexer.dydx.trade/v4/historicalFunding/{ticker}?{parse.urlencode({'limit': 1000, 'effectiveBeforeOrAt': iso})}"
        try:
            data = http_get(url).get("historicalFunding", [])
        except error.HTTPError as e:
            if e.code in (400, 404):
                return pd.Series(dtype=float)
            raise
        if not data:
            break
        for r in data:
            recs[r["effectiveAt"]] = float(r["rate"])
        last = pd.to_datetime(data[-1]["effectiveAt"])
        if last.tz is None:
            last = last.tz_localize("UTC")
        if last <= W365 or len(data) < 1000:
            break
        cursor = (last - dt.timedelta(seconds=1)).to_pydatetime()
        time.sleep(0.3)
    if not recs:
        return pd.Series(dtype=float)
    idx = pd.to_datetime(list(recs.keys()), utc=True)
    s = pd.Series(list(recs.values()), index=idx).sort_index()
    return s[~s.index.duplicated()]


LIGHTER_IDS = {"BTC": 1, "ETH": 0, "SOL": 2, "HYPE": 24, "AVAX": 9,
               "LINK": 8, "DOGE": 3, "ZEC": 90, "XPL": 71}


def fetch_lighter(coin):
    mid = LIGHTER_IDS.get(coin)
    if mid is None:
        return pd.Series(dtype=float)
    recs = {}
    end = int(NOW.timestamp())
    floor = int(W365.timestamp())
    # page backwards in ~40-day chunks (>1000 hourly points/req risk); use count_back
    step = 40 * 24 * 3600
    while end > floor:
        start = max(floor, end - step)
        url = f"https://mainnet.zklighter.elliot.ai/api/v1/fundings?{parse.urlencode({'market_id': mid, 'resolution': '1h', 'start_timestamp': start, 'end_timestamp': end, 'count_back': 1000})}"
        data = http_get(url).get("fundings", [])
        if not data:
            break
        for f in data:
            pct = float(f["rate"])
            if f.get("direction") == "short":
                pct = -pct
            recs[int(f["timestamp"])] = pct / 100.0  # PERCENT/h -> fraction/h
        end = start - 1
        time.sleep(0.2)
    if not recs:
        return pd.Series(dtype=float)
    idx = pd.to_datetime(sorted(recs.keys()), unit="s", utc=True)
    s = pd.Series([recs[int(t.timestamp())] for t in idx], index=idx).sort_index()
    return s[~s.index.duplicated()]


def fetch_paradex_history(coin):
    """Paradex trailing history by HOURLY SAMPLING of its 5s funding-index stream.

    /funding/data emits an instantaneous 8h-equivalent `funding_rate` every ~5s — a
    spot reading, NOT a per-settlement charge, so we can't sum it. Instead we step
    backward day-by-day and within each day request a tiny 60s window per hour, taking
    one sample. Result: ~hourly snapshots of the 8h-rate, whose mean ≈ realized funding.
    Capped to last ~120d to stay within the time budget (still > the 90d window).
    """
    market = f"{coin}-USD-PERP"
    samples = {}
    # sample every 4h over the last 90 days -> ~540 tiny requests/coin; bounded.
    horizon_days = 90
    step_h = 4
    cur = int(NOW.timestamp())
    floor = cur - horizon_days * 24 * 3600
    misses = 0
    while cur > floor:
        a = (cur - 60) * 1000
        b = cur * 1000
        url = f"https://api.prod.paradex.trade/v1/funding/data?{parse.urlencode({'market': market, 'start_at': a, 'end_at': b, 'page_size': 1})}"
        try:
            res = http_get(url).get("results", [])
        except error.HTTPError as e:
            if e.code in (400, 404):
                return pd.Series(dtype=float)
            res = []
        if res:
            r = res[0]
            samples[int(r["created_at"])] = float(r["funding_rate"])  # instantaneous 8h-equiv fraction
            misses = 0
        else:
            misses += 1
            if misses > 30:  # market not listed that far back / no data
                break
        cur -= step_h * 3600
        time.sleep(0.05)
    if not samples:
        return pd.Series(dtype=float)
    idx = pd.to_datetime(sorted(samples.keys()), unit="ms", utc=True)
    # treat each sample as an 8h-rate observation spaced step_h apart; annualize() will
    # divide by the 8h interval cap and spread, giving a time-weighted mean. We feed the
    # fraction directly with a synthetic 8h spacing by relabeling — simpler: convert to
    # per-hour equiv here (rate/8) and resample, matching annualize()'s contract on a
    # uniform series. We keep the raw fraction and let annualize() handle interval clip.
    s = pd.Series([samples[int(t.timestamp() * 1000)] for t in idx], index=idx).sort_index()
    return s[~s.index.duplicated()]


FETCH = {"HL": fetch_hl, "Aster": fetch_aster, "dYdX": fetch_dydx,
         "Lighter": fetch_lighter, "Paradex": fetch_paradex_history}


def window_stats(ann: pd.Series, since):
    w = ann[ann.index >= since]
    if len(w) == 0:
        return None
    return {
        "annualized_pct": round(float(w.mean()), 2),
        "neg_hours_pct": round(float((w < 0).mean() * 100), 1),
        "n_points": int(len(w)),
        "interval_h": None,  # filled by caller from raw cadence
    }


def median_interval_h(rate_idx):
    if len(rate_idx) < 2:
        return None
    diffs = pd.Series(rate_idx).diff().dropna().dt.total_seconds() / 3600.0
    return round(float(diffs.median()), 2)


def main():
    rows = []
    fetched_at = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    for venue, fn in FETCH.items():
        print(f"\n=== {venue} ===")
        for coin in COINS:
            try:
                raw = fn(coin)
            except Exception as e:
                print(f"  {coin}: ERROR {type(e).__name__}: {e}")
                continue
            if raw is None or len(raw) == 0:
                print(f"  {coin}: (none/not listed)")
                continue
            raw = raw[raw.index >= W365]
            if len(raw) == 0:
                print(f"  {coin}: no data in 365d window")
                continue
            iv = median_interval_h(raw.index)
            raw.to_frame("rate_fraction").to_csv(RAW / f"{venue}_{coin}.csv")
            if venue == "Paradex":
                # raw = instantaneous 8h-equiv rate snapshots (sampled ~2h). Each is an
                # 8h-rate, so per-hour equiv = rate/8; annualize the sample series directly.
                ann = (raw / 8.0) * 8760 * 100.0
                iv = 8.0  # nominal settlement interval, not the sampling cadence
            else:
                ann = annualize(raw)
            first = raw.index.min().date()
            # Paradex history is sampled, only ~120d deep -> 90d window only (no 365d).
            windows = [("90d", W90)] if venue == "Paradex" else [("90d", W90), ("365d", W365)]
            for win, since in windows:
                st = window_stats(ann, since)
                if st is None:
                    continue
                st["interval_h"] = iv
                rows.append({"venue": venue, "coin": coin, "window": win,
                             "fetched_at": fetched_at, **st})
            s90 = window_stats(ann, W90)
            s365 = window_stats(ann, W365)
            print(f"  {coin}: n={len(raw)} iv={iv}h from {first} "
                  f"90d={s90['annualized_pct'] if s90 else 'NA'}% "
                  f"365d={s365['annualized_pct'] if s365 else 'NA'}%")
    df = pd.DataFrame(rows, columns=["venue", "coin", "window", "annualized_pct",
                                     "neg_hours_pct", "n_points", "interval_h", "fetched_at"])
    df.to_csv(HERE / "frab_funding_current.csv", index=False)
    print(f"\nwrote {HERE/'frab_funding_current.csv'} ({len(df)} rows)")


if __name__ == "__main__":
    main()
