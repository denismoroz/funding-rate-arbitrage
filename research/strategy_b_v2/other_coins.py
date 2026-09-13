"""Strategy B v2 "cold wallet" on other large Hyperliquid perps — does it carry beyond BTC/ETH/SOL/AVAX?

PRE-REGISTERED 2026-09-13, before any result of this script was seen.

Universe      : HL perps listed today with maxLeverage >= 5 and open interest >= $15M, plus 2023
                large caps still listed on HL (DOT ATOM BCH TRX XLM APT INJ TON) so the list is not
                only today's winners. BTC/ETH/SOL/AVAX excluded (validated before) but run as reference.
Config        : exactly the paper "cold wallet" test — spot + trend hedge, no carry, sticky 12h,
                a = 0, ratchet 0.5, pool reserves hedge margin for 1.5x spot, buffer 10%.
                No per-coin tuning. Short leverage 1.5x for every coin outside the four
                (BTC 3x, ETH 2x, SOL/AVAX 1.5x); maintenance margin 1 / (2 x HL max leverage).
                One $1000 book per coin, min order $10, taker + 5 bps slippage.
Windows       : TRAIN 2023-06..2025-05, TEST 2025-06..2026-05, FORWARD 2026-06-01..2026-09-13.
                A window counts only if the coin was shortable on HL (funding history exists) for
                >= 75% of it; the book then starts at the first shortable hour, signals warmed on
                up to 800 hours before (fewer at the very start of the data: the 30-day signal then
                simply stays off until it has history, as in the original research).
Data          : cached Binance 1h candles + HL funding (research/cross_sectional/crypto/data) up to
                2026-04-30, rescaled to HL price on the overlap; fresh HL 1h candles (incl. highs) +
                funding from 2026-04-15. Coins whose cached Binance ticker is a different asset
                (LIT = Litentry, not Lighter) or missing use HL data only -> forward window only.
PASS (per coin): TEST and FORWARD both evaluated, return > 0 and max DD <= 15% in both, 0 liquidations.
Also reported : TRAIN, hold-spot return / DD, whether the strategy's TEST DD <= half of holding's.
"""
import json, sys, time
from dataclasses import replace
from pathlib import Path
import numpy as np, pandas as pd, requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import frab.strategy.b2.book as B
from frab.strategy.b2.book import CoinBook, start_book, step
from frab.strategy.b2.params import B2Params

CACHE = ROOT / "research/cross_sectional/crypto/data"
SCRATCH = Path("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/b2_other_hl")
URL = "https://api.hyperliquid.xyz/info"
OUT = Path(__file__).with_name("other_coins.json")
WIN = {"train": ("2023-06-08", "2025-05-31 23:00"), "test": ("2025-06-01", "2026-05-31 23:00"),
       "forward": ("2026-06-01", "2026-09-13 12:00")}
MAJORS = {"BTC": 3.0, "ETH": 2.0, "SOL": 1.5, "AVAX": 1.5}
EXTRA_2023 = ["DOT", "ATOM", "BCH", "TRX", "XLM", "APT", "INJ", "TON"]
HL_ONLY = {"LIT"}                    # cached Binance "LIT" is Litentry, HL LIT is Lighter
WARM, CAP = 800, 1000.0
HL_START = pd.Timestamp("2026-04-15", tz="UTC")
JUNCTION = pd.Timestamp("2026-05-01", tz="UTC")


def post(body):
    for attempt in range(5):
        r = requests.post(URL, json=body, timeout=60)
        if r.status_code == 429:
            time.sleep(5 * (attempt + 1)); continue
        r.raise_for_status(); time.sleep(1.0)
        return r.json()
    raise RuntimeError("HL rate limit")


def universe():
    meta, ctxs = post({"type": "metaAndAssetCtxs"})
    rows = {}
    for a, c in zip(meta["universe"], ctxs):
        if a.get("isDelisted"):
            continue
        oi = float(c.get("openInterest") or 0) * float(c.get("markPx") or 0)
        rows[a["name"]] = dict(max_lev=int(a["maxLeverage"]), oi=oi)
    picked = [n for n, r in rows.items() if r["max_lev"] >= 5 and r["oi"] >= 15e6 and n not in MAJORS]
    picked += [n for n in EXTRA_2023 if n in rows and n not in picked]
    return sorted(picked, key=lambda n: -rows[n]["oi"]), rows


def hl_data(coin):
    f = SCRATCH / f"{coin}.csv"
    if f.exists():
        return pd.read_csv(f, parse_dates=["time"]).set_index("time")
    SCRATCH.mkdir(parents=True, exist_ok=True)
    start = int(HL_START.timestamp() * 1000); now = int(time.time() * 1000)
    c = post({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": start, "endTime": now}})
    px = pd.DataFrame(c)
    px["time"] = pd.to_datetime(px["t"].astype("int64"), unit="ms", utc=True)
    px = px.set_index("time")[["c", "h"]].astype(float).rename(columns={"c": "close", "h": "high"})
    rows, s = [], start
    while s < now:
        d = post({"type": "fundingHistory", "coin": coin, "startTime": s})
        if not d: break
        rows += d; last = int(d[-1]["time"])
        if len(d) < 500 or last <= s: break
        s = last + 1
    fr = pd.DataFrame(rows)
    fr["time"] = pd.to_datetime(fr["time"].astype("int64"), unit="ms", utc=True).dt.floor("h")
    fr = fr.drop_duplicates("time").set_index("time")["fundingRate"].astype(float)
    df = px.join(fr, how="left")
    df = df[df.index < pd.Timestamp(now - now % 3_600_000 - 3_600_000, unit="ms", tz="UTC")]
    df.to_csv(f)
    return df


def cached(coin):
    p1, pf = CACHE / f"{coin}_1h.csv", CACHE / f"{coin}.csv"
    if coin in HL_ONLY or not p1.exists() or not pf.exists():
        return None
    px = pd.read_csv(p1)
    px["time"] = pd.to_datetime(px["time"], format="ISO8601", utc=True).dt.floor("h")
    px = px.drop_duplicates("time").set_index("time")[["close", "high"]].astype(float)
    fr = pd.read_csv(pf)
    fr["time"] = pd.to_datetime(fr["time"], format="ISO8601", utc=True).dt.floor("h")
    fr = fr.drop_duplicates("time").set_index("time")["fundingRate"].astype(float)
    return px.join(fr, how="left"), fr.index.min()


def series(coin):
    """Hourly close/high/funding + first hour the coin was shortable on HL + a note."""
    hl = hl_data(coin)
    note = ""
    c = cached(coin)
    if c is None:
        df, shortable = hl, hl["fundingRate"].first_valid_index()
        note = "HL data only"
    else:
        cdf, shortable = c
        ov = cdf.index.intersection(hl.index)
        ov = ov[ov < JUNCTION + pd.Timedelta(days=40)]
        ratio = float(np.median(hl.loc[ov, "close"] / cdf.loc[ov, "close"])) if len(ov) > 100 else np.nan
        if not np.isfinite(ratio) or not (0.2 < ratio < 5000):
            df, shortable, note = hl, hl["fundingRate"].first_valid_index(), "no usable overlap -> HL only"
        else:
            if abs(ratio - 1) > 0.02:
                cdf[["close", "high"]] *= ratio
                note = f"cached prices x{ratio:.4g}"
            df = pd.concat([cdf[cdf.index < JUNCTION], hl[hl.index >= JUNCTION]])
    full = pd.date_range(df.index.min(), df.index.max(), freq="h")
    df = df[~df.index.duplicated()].reindex(full)
    gaps = df["close"].isna().sum()
    df[["close", "high"]] = df[["close", "high"]].ffill(limit=6)
    df["fundingRate"] = df["fundingRate"].fillna(0.0)
    df = df.dropna(subset=["close"])
    df["high"] = df[["high", "close"]].max(axis=1)
    return df, shortable, note + (f"; {gaps} missing hours filled" if gaps > 24 else "")


def run(df, coin, params, lo, hi, shortable):
    idx = df.index
    lo, hi = pd.Timestamp(lo, tz="UTC"), pd.Timestamp(hi, tz="UTC")
    span = (hi - lo).total_seconds() / 3600
    start = max(lo, shortable.ceil("h")) if shortable is not None else None
    if start is None or start > hi or (hi - start).total_seconds() / 3600 < 0.75 * span:
        return None
    pos = np.flatnonzero((idx >= start) & (idx <= hi))
    if len(pos) < 0.75 * span:
        return None
    a, b = pos[0], pos[-1]
    px = df["close"].tolist(); hi_ = df["high"].tolist(); fr = df["fundingRate"].tolist()
    book = CoinBook.new(coin, params)
    for i in range(max(0, a - WARM), a):
        B.advance_signals(book, px[max(0, i - 730):i + 1], fr[max(0, i - 8):i + 1], params)
    start_book(book, bar_ms=a, price=px[a], params=params)
    eq, dist = [], []
    for i in range(a, b + 1):
        step(book, bar_ms=i, price=px[i], funding_rate=fr[i], closes=px[max(0, i - 730):i + 1],
             funding_hist=fr[max(0, i - 8):i + 1], params=params, high=hi_[i])
        eq.append(book.equity(px[i]))
        liq = book.liquidation_price()
        if liq:
            dist.append(liq / hi_[i] - 1)
    eq = np.array(eq); path = np.concatenate([[CAP], eq]); years = len(eq) / 8760
    hold = np.array(px[a:b + 1]) / px[a]
    ret = eq[-1] / CAP - 1
    return dict(start=str(idx[a])[:10], ret=ret * 100, ann=((1 + ret) ** (1 / years) - 1) * 100,
                dd=-(path / np.maximum.accumulate(path) - 1).min() * 100,
                hold_ret=(hold[-1] - 1) * 100, hold_dd=-(hold / np.maximum.accumulate(hold) - 1).min() * 100,
                hedges=book.trades, top_ups=book.margin_rebals, liquidations=book.liquidations,
                limited=book.hedge_limited, min_liq_dist=min(dist) * 100 if dist else None,
                hedge_funding=book.funding_total / CAP * 100)


def main():
    coins, meta = universe()
    print("universe:", " ".join(coins))
    base = B2Params(capital_usd=CAP, carry_enabled=False, hedge_margin_headroom=1.5, min_order_usd=10.0)
    out = []
    for coin in list(MAJORS) + coins:
        lev = MAJORS.get(coin, 1.5)
        params = replace(base, coins=(coin,), short_leverage={coin: lev},
                         maint_margin_rate={coin: 1 / (2 * meta[coin]["max_lev"])})
        try:
            df, shortable, note = series(coin)
        except Exception as exc:  # noqa: BLE001
            print(f"{coin:<9} data error: {exc}"); out.append(dict(coin=coin, error=str(exc))); continue
        r = dict(coin=coin, reference=coin in MAJORS, leverage=lev, max_lev=meta[coin]["max_lev"],
                 oi_musd=meta[coin]["oi"] / 1e6, shortable_from=str(shortable)[:10], note=note)
        for w, (lo, hi) in WIN.items():
            r[w] = run(df, coin, params, lo, hi, shortable)
        t, f = r["test"], r["forward"]
        r["pass"] = bool(t and f and t["ret"] > 0 and f["ret"] > 0 and t["dd"] <= 15 and f["dd"] <= 15
                         and t["liquidations"] == 0 and f["liquidations"] == 0)
        out.append(r)
        cell = lambda x: "      —       " if x is None else f"{x['ann']:+6.1f}%/y dd{x['dd']:4.1f}"
        print(f"{coin:<9} {'REF ' if coin in MAJORS else ''}{'PASS' if r['pass'] else '    '}  "
              f"train {cell(r['train'])}  test {cell(r['test'])}  fwd {cell(r['forward'])}  {note}")
    OUT.write_text(json.dumps(out, indent=1, default=float))


if __name__ == "__main__":
    main()
