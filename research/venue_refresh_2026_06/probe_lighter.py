"""Lighter (zkLighter) XSMOM venue probe. zk-rollup orderbook DEX, taker fee 0."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from _common import (UNIVERSE, DEPTH_PROBE, get_json, book_metrics, now_iso)

BASE = "https://mainnet.zklighter.elliot.ai/api/v1"
VENUE = "lighter"
TAKER_FEE_BPS = 0.0  # currently zero taker fee


def main():
    rows = []
    books = get_json(BASE + "/orderBooks")["order_books"]
    by_coin = {}
    for b in books:
        if b.get("market_type") != "perp" or b.get("status") != "active":
            continue
        by_coin[b["symbol"]] = b

    for coin in UNIVERSE:
        b = by_coin.get(coin)
        listed = b is not None
        vol = 0.0; maxlev = 0; mark = None; mid = None
        if listed:
            try:
                d = get_json(BASE + f"/orderBookDetails?market_id={b['market_id']}")["order_book_details"][0]
                vol = float(d.get("daily_quote_token_volume") or 0)
                mark = float(d.get("last_trade_price") or 0) or None
                imf = float(d.get("min_initial_margin_fraction") or 0)  # bps
                if imf > 0:
                    maxlev = round(10000.0 / imf)
            except Exception as e:
                print(f"  detail fail {coin}: {e}", file=sys.stderr)
        spread = None; depth = 0.0
        if listed and coin in DEPTH_PROBE:
            try:
                ob = get_json(BASE + f"/orderBookOrders?market_id={b['market_id']}&limit=200")
                bids = [(o["price"], o["remaining_base_amount"]) for o in ob.get("bids", [])]
                asks = [(o["price"], o["remaining_base_amount"]) for o in ob.get("asks", [])]
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
