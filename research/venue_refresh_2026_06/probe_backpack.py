"""Backpack XSMOM venue probe. CEX-style Solana-team orderbook venue."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from _common import (UNIVERSE, DEPTH_PROBE, get_json, book_metrics, now_iso)

BASE = "https://api.backpack.exchange/api/v1"
VENUE = "backpack"
TAKER_FEE_BPS = 7.0  # 0.07% base taker (verify; tiered)


def main():
    rows = []
    markets = get_json(BASE + "/markets")
    by_coin = {}
    for m in markets:
        if m.get("marketType") != "PERP":
            continue
        if m.get("orderBookState") != "Open":
            continue
        by_coin[m["baseSymbol"]] = m
    tickers = {}
    try:
        for t in get_json(BASE + "/tickers"):
            tickers[t["symbol"]] = t
    except Exception as e:
        print(f"ticker fail: {e}", file=sys.stderr)

    for coin in UNIVERSE:
        m = by_coin.get(coin)
        listed = m is not None
        sym = m["symbol"] if listed else None
        vol = 0.0; maxlev = 0; mark = None
        if listed:
            t = tickers.get(sym)
            if t:
                vol = float(t.get("quoteVolume") or 0)
                mark = float(t.get("lastPrice") or 0) or None
            imf_base = float(m.get("imfFunction", {}).get("base") or 0)
            if imf_base > 0:
                maxlev = round(1.0 / imf_base)
        spread = None; depth = 0.0
        if listed and coin in DEPTH_PROBE:
            try:
                ob = get_json(BASE + f"/depth?symbol={sym}")
                # bids ascending -> reverse so best (highest) first
                bids = list(reversed(ob.get("bids", [])))
                asks = ob.get("asks", [])
                spread, depth = book_metrics(bids, asks, mark)
            except Exception as e:
                print(f"  depth fail {coin}: {e}", file=sys.stderr)
        rows.append({
            "venue": VENUE, "coin": coin, "listed": int(listed),
            "volume_24h_usd": round(vol, 2),
            "top_of_book_spread_bps": round(spread, 3) if spread is not None else "",
            "depth_1pct_usd": round(depth, 2),
            "max_leverage": maxlev, "taker_fee_bps": TAKER_FEE_BPS,
            "fetched_at": now_iso(),
        })
    n = sum(1 for c in UNIVERSE if c in by_coin)
    print(f"{VENUE}: {n}/32 listed; missing: {[c for c in UNIVERSE if c not in by_coin]}", file=sys.stderr)
    return rows


if __name__ == "__main__":
    import csv
    rows = main()
    out = os.path.join(os.path.dirname(__file__), f"_{VENUE}.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    print(f"wrote {out}")
