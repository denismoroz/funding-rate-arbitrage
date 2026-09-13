"""Strategy B v2 with honest margin — the production CoinBook stepped over history.

Question 1: what does B v2 earn on the capital it really ties up, once the shorts need
initial margin, pumps need top-ups and a spike can liquidate? (research treated margin as free)
Question 2: is timing the SPOT itself (sell when the hedge would switch on, buy back when it
switches off) different from hedging with a perp short?

Same rules as the paper engine (src/frab/strategy/b2): sticky 12h, ratchet 0.5, carry 60%.
One continuous run 2023-06 .. 2026-09 per variant, reported by the pre-registered windows.
Forward bars (2026-06-01 ..) come from HL candles incl. highs, fetched to the scratchpad.
"""
import sys, time, json
from dataclasses import replace
from pathlib import Path
import numpy as np, pandas as pd, requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "research"))
import frab.strategy.b2.book as B
from frab.strategy.b2.book import CoinBook, start_book, step
from frab.strategy.b2.params import B2Params
from engine import load_data

COINS = ["BTC", "ETH", "SOL", "AVAX"]
WIN = {"train": ("2023-06-01", "2025-05-31 23:00"), "test": ("2025-06-01", "2026-05-31 23:00"),
       "forward": ("2026-06-01", "2026-09-13 23:00")}
SCRATCH = Path("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/b_forward_hi")
URL = "https://api.hyperliquid.xyz/info"
CAPITAL = 1000.0
OUT = Path(__file__).with_name("margin_check.json")


def fetch_forward(coin):
    f = SCRATCH / f"{coin}.csv"
    if f.exists():
        return pd.read_csv(f, parse_dates=["time"]).set_index("time")
    SCRATCH.mkdir(parents=True, exist_ok=True)
    start = int(pd.Timestamp("2026-05-01", tz="UTC").timestamp() * 1000); now = int(time.time() * 1000)
    c = requests.post(URL, json={"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h",
                      "startTime": start, "endTime": now}}, timeout=60).json()
    px = pd.DataFrame(c)
    px["time"] = pd.to_datetime(px["t"].astype("int64"), unit="ms", utc=True)
    px = px.set_index("time")[["c", "h"]].astype(float).rename(columns={"c": "close", "h": "high"})
    rows, s = [], start
    while s < now:
        d = requests.post(URL, json={"type": "fundingHistory", "coin": coin, "startTime": s}, timeout=30).json()
        if not d: break
        rows += d; last = int(d[-1]["time"])
        if len(d) < 500 or last <= s: break
        s = last + 1; time.sleep(0.25)
    fr = pd.DataFrame(rows); fr["time"] = pd.to_datetime(fr["time"].astype("int64"), unit="ms", utc=True).dt.floor("h")
    fr = fr.drop_duplicates("time").set_index("time")["fundingRate"].astype(float)
    df = px.join(fr, how="left"); df["fundingRate"] = df["fundingRate"].fillna(0.0)
    df = df[df.index < pd.Timestamp(now - now % 3_600_000 - 3_600_000, unit="ms", tz="UTC")]  # closed bars only
    df.to_csv(f)
    return df


def series(coin):
    hist = load_data(coin)[["close", "fundingRate"]]
    hi = pd.read_csv(ROOT / "research" / "data" / f"{coin}_1h.csv")
    hi["time"] = pd.to_datetime(hi["time"], format="ISO8601", utc=True).dt.floor("h")
    hist = hist.join(hi.set_index("time")["high"], how="left")
    hist = hist[hist.index < pd.Timestamp("2026-06-01", tz="UTC")]
    fwd = fetch_forward(coin)
    fwd = fwd[fwd.index >= pd.Timestamp("2026-06-01", tz="UTC")]
    df = pd.concat([hist, fwd[["close", "fundingRate", "high"]]]).sort_index()
    df["fundingRate"] = df["fundingRate"].astype(float).fillna(0.0)
    df["high"] = df["high"].fillna(df["close"])
    return df


COUNTERS = ["trades", "margin_rebals", "liquidations", "hedge_limited", "carry_blocked_hours", "carry_trades"]


WARM = 800   # bars of history before a window start: 30d momentum + the sticky state


def run_book(df, params, coin, i0, i1, zero_funding=False):
    """Fresh book started at bar i0 with signals warmed on the bars before it, stepped to i1."""
    px = df["close"].astype(float).tolist(); hi = df["high"].astype(float).tolist()
    fr = [0.0] * len(px) if zero_funding else df["fundingRate"].astype(float).tolist()
    book = CoinBook.new(coin, params)
    for i in range(max(0, i0 - WARM), i0):
        B.advance_signals(book, px[max(0, i - 730):i + 1], fr[max(0, i - 8):i + 1], params)
    start_book(book, bar_ms=i0, price=px[i0], params=params)
    n = i1 - i0 + 1
    eq = np.empty(n); fees = np.empty(n); cnt = np.empty((n, len(COUNTERS))); minfree = np.empty(n)
    for k, i in enumerate(range(i0, i1 + 1)):
        step(book, bar_ms=i, price=px[i], funding_rate=fr[i], closes=px[max(0, i - 730):i + 1],
             funding_hist=fr[max(0, i - 8):i + 1], params=params, high=hi[i])
        eq[k] = book.equity(px[i])
        fees[k] = book.perp_fees + book.spot_fees + book.carry_fees + book.margin_fees
        cnt[k] = [getattr(book, c) for c in COUNTERS]
        liq = book.liquidation_price()
        minfree[k] = (liq / hi[i] - 1) if liq else np.nan
    return book, eq, fees, cnt, minfree


def variant(data, name, params, zero_funding=False, perp_as_spot=False):
    """Each window is a separate $CAPITAL start (like the paper engine), not one long run:
    B sizes spot in fixed dollars, so a run started years earlier dilutes later % returns."""
    saved = B.PERP_TAKER
    if perp_as_spot:
        B.PERP_TAKER = B.SPOT_TAKER            # hedge trades pay the spot fee: it IS a spot sale
    idx = data[COINS[0]].index
    res = {"variant": name}
    try:
        for w, (lo, hi) in WIN.items():
            pos = np.flatnonzero((idx >= pd.Timestamp(lo, tz="UTC")) & (idx <= pd.Timestamp(hi, tz="UTC")))
            a, b = pos[0], pos[-1]
            per = {c: run_book(data[c], params, c, a, b, zero_funding) for c in COINS}
            eq = sum(per[c][1] for c in COINS)
            years = len(eq) / 8760
            ret = eq[-1] / CAPITAL - 1
            path = np.concatenate([[CAPITAL], eq])
            dd = -(path / np.maximum.accumulate(path) - 1).min()
            cnt = sum(per[c][3][-1] for c in COINS)
            dist = np.nanmin(np.stack([per[c][4] for c in COINS])) if any(np.isfinite(per[c][4]).any() for c in COINS) else np.nan
            books = {c: per[c][0] for c in COINS}
            res[w] = dict(ret=ret * 100, ann=((1 + ret) ** (1 / years) - 1) * 100, dd=dd * 100,
                          fees=sum(per[c][2][-1] for c in COINS) / CAPITAL * 100,
                          hedge_funding=sum(b_.funding_total for b_ in books.values()) / CAPITAL * 100,
                          carry_funding=sum(b_.carry_funding for b_ in books.values()) / CAPITAL * 100,
                          min_liq_distance=float(dist * 100) if np.isfinite(dist) else None,
                          **dict(zip(COUNTERS, cnt.tolist())))
            res["hl_pool"] = sum(b_.reserve for b_ in books.values())
            res["spot_target"] = sum(b_.position_size for b_ in books.values())
            res["carry_target"] = sum(b_.carry_target for b_ in books.values())
    finally:
        B.PERP_TAKER = saved
    return res


def hold(data):
    idx = data[COINS[0]].index
    eq = pd.Series(sum(CAPITAL / len(COINS) * data[c]["close"].values / data[c]["close"].values[0] for c in COINS), index=idx)
    res = {"variant": "hold_100_spot"}
    for w, (lo, hi) in WIN.items():
        seg = eq[(idx >= pd.Timestamp(lo, tz="UTC")) & (idx <= pd.Timestamp(hi, tz="UTC"))]
        years = len(seg) / 8760; ret = seg.iloc[-1] / seg.iloc[0] - 1
        res[w] = dict(ret=ret * 100, ann=((1 + ret) ** (1 / years) - 1) * 100, dd=-(seg / seg.cummax() - 1).min() * 100)
    return res


def main():
    data = {c: series(c) for c in COINS}
    common = data[COINS[0]].index
    for c in COINS[1:]:
        common = common.intersection(data[c].index)
    data = {c: data[c].loc[common] for c in COINS}
    print("bars", len(common), str(common[0])[:16], "..", str(common[-1])[:16])

    base = B2Params(coins=tuple(COINS), capital_usd=CAPITAL, min_order_usd=10.0 * CAPITAL / 257.0)
    caps = {"BTC": 3.0, "ETH": 2.0, "SOL": 1.5, "AVAX": 1.5}
    runs = [
        ("research_no_margin_min0", replace(base, margin_enabled=False, min_order_usd=0.0), {}),
        ("research_no_margin", replace(base, margin_enabled=False), {}),
        ("margin_1x", replace(base, short_leverage={c: 1.0 for c in COINS}), {}),
        ("margin_caps_3_2_1.5_1.5", replace(base, short_leverage=caps), {}),
        ("margin_max_5_3_1.9_2.5", replace(base, short_leverage={"BTC": 5.0, "ETH": 3.0, "SOL": 1.9, "AVAX": 2.5}), {}),
        ("hedge_no_carry_caps", replace(base, short_leverage=caps, carry_enabled=False), {}),
        ("sell_spot_no_carry", replace(base, margin_enabled=False, carry_enabled=False, spot_share=1 / 1.1),
         dict(zero_funding=True, perp_as_spot=True)),
        ("hedge_no_carry_research", replace(base, margin_enabled=False, carry_enabled=False, spot_share=1 / 1.1), {}),
    ]
    out = []
    for name, p, kw in runs:
        t = time.time()
        r = variant(data, name, p, **kw)
        out.append(r)
        print(f"{name:<28} pool {r['hl_pool']:7.1f} spot {r['spot_target']:6.1f}  "
              + "  ".join(f"{w} {r[w]['ann']:+6.1f}%/y ({r[w]['ret']:+.1f}) dd {r[w]['dd']:4.1f} reb {r[w]['margin_rebals']:.0f} "
                          f"liq {r[w]['liquidations']:.0f} lim {r[w]['hedge_limited']:.0f} liqd {r[w]['min_liq_distance'] or 0:.0f}" for w in WIN)
              + f"  ({time.time() - t:.0f}s)")
    out.append(hold(data))
    OUT.write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
