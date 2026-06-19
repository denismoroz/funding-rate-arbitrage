"""dYdX v4 XSMOM venue probe."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from _common import (UNIVERSE, DEPTH_PROBE, get_json, book_metrics, now_iso)

BASE = "https://indexer.dydx.trade/v4"
VENUE = "dydx"
TAKER_FEE_BPS = 5.0  # 0.05% taker default tier (verify)


def main():
    rows = []
    data = get_json(BASE + "/perpetualMarkets")
    markets = data.get("markets", {})
    # ticker convention COIN-USD
    by_coin = {}
    for tk, m in markets.items():
        base = tk.split("-")[0]
        by_coin[base] = (tk, m)

    for coin in UNIVERSE:
        listed = coin in by_coin
        vol = 0.0; maxlev = 0; mark = None
        tk = None
        if listed:
            tk, m = by_coin[coin]
            vol = float(m.get("volume24H") or 0)  # already USD
            mark = float(m.get("oraclePrice") or 0) or None
            imf = float(m.get("initialMarginFraction") or 0)
            if imf > 0:
                maxlev = round(1.0 / imf)
        spread = None; depth = 0.0
        if listed and coin in DEPTH_PROBE:
            try:
                ob = get_json(BASE + f"/orderbooks/perpetualMarket/{tk}")
                bids = [(b["price"], b["size"]) for b in ob.get("bids", [])]
                asks = [(a["price"], a["size"]) for a in ob.get("asks", [])]
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
    print(f"{VENUE}: {n}/32 listed (total markets {len(markets)}); missing: {[c for c in UNIVERSE if c not in by_coin]}", file=sys.stderr)
    return rows


if __name__ == "__main__":
    import csv
    rows = main()
    out = os.path.join(os.path.dirname(__file__), f"_{VENUE}.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    print(f"wrote {out}")
