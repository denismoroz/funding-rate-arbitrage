"""Hyperliquid XSMOM venue probe."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from _common import (UNIVERSE, DEPTH_PROBE, post_json, book_metrics, now_iso)

BASE = "https://api.hyperliquid.xyz/info"
VENUE = "hyperliquid"
TAKER_FEE_BPS = 4.5  # base taker 0.045% (lower with volume tiers)


def main():
    rows = []
    meta_ctx = post_json(BASE, {"type": "metaAndAssetCtxs"})
    universe = meta_ctx[0]["universe"]
    ctxs = meta_ctx[1]
    by_coin = {}
    for u, c in zip(universe, ctxs):
        by_coin[u["name"]] = (u, c)

    for coin in UNIVERSE:
        listed = coin in by_coin
        vol = 0.0; maxlev = 0; mark = None
        if listed:
            u, c = by_coin[coin]
            maxlev = u.get("maxLeverage", 0)
            mark = float(c.get("markPx") or 0) or None
            # dayNtlVlm = 24h notional volume in USD
            vol = float(c.get("dayNtlVlm") or 0)
        spread = None; depth = 0.0
        if listed and coin in DEPTH_PROBE:
            try:
                ob = post_json(BASE, {"type": "l2Book", "coin": coin})
                levels = ob.get("levels", [[], []])
                bids = [(l["px"], l["sz"]) for l in levels[0]]
                asks = [(l["px"], l["sz"]) for l in levels[1]]
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
    print(f"{VENUE}: {n}/32 listed", file=sys.stderr)
    return rows


if __name__ == "__main__":
    import csv
    rows = main()
    out = os.path.join(os.path.dirname(__file__), f"_{VENUE}.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    print(f"wrote {out}")
