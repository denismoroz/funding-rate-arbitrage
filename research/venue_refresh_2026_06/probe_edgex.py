"""edgeX XSMOM venue probe. StarkEx-based orderbook perp DEX."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from _common import (UNIVERSE, DEPTH_PROBE, get_json, book_metrics, now_iso)

BASE = "https://pro.edgex.exchange/api/v1"
VENUE = "edgex"
TAKER_FEE_BPS = 3.8  # defaultTakerFeeRate 0.00038


def coin_from_name(name):
    # contractName like BTCUSD, 1000PEPEUSD etc. Strip trailing USD/USDT.
    for suf in ("USDT", "USD"):
        if name.endswith(suf):
            return name[:-len(suf)]
    return name


def main():
    rows = []
    meta = get_json(BASE + "/public/meta/getMetaData")["data"]
    contracts = meta.get("contractList", [])
    by_coin = {}
    for c in contracts:
        if not c.get("enableTrade"):
            continue
        base = coin_from_name(c.get("contractName", ""))
        by_coin[base] = c

    for coin in UNIVERSE:
        c = by_coin.get(coin)
        listed = c is not None
        cid = c["contractId"] if listed else None
        vol = 0.0; maxlev = 0; mark = None; taker = TAKER_FEE_BPS
        if listed:
            try:
                taker = float(c.get("defaultTakerFeeRate") or 0) * 1e4
            except Exception:
                pass
            tiers = c.get("riskTierList", [])
            if tiers:
                maxlev = int(float(tiers[0].get("maxLeverage") or 0))
            try:
                t = get_json(BASE + f"/public/quote/getTicker?contractId={cid}")["data"][0]
                vol = float(t.get("value") or 0)  # 24h quote volume USD
                mark = float(t.get("close") or 0) or None
            except Exception as e:
                print(f"  ticker fail {coin}: {e}", file=sys.stderr)
        spread = None; depth = 0.0
        if listed and coin in DEPTH_PROBE:
            try:
                d = get_json(BASE + f"/public/quote/getDepth?contractId={cid}&level=200")["data"][0]
                bids = [(b["price"], b["size"]) for b in d.get("bids", [])]
                asks = [(a["price"], a["size"]) for a in d.get("asks", [])]
                spread, depth = book_metrics(bids, asks, mark)
            except Exception as e:
                print(f"  depth fail {coin}: {e}", file=sys.stderr)
        rows.append({
            "venue": VENUE, "coin": coin, "listed": int(listed),
            "volume_24h_usd": round(vol, 2),
            "top_of_book_spread_bps": round(spread, 3) if spread is not None else "",
            "depth_1pct_usd": round(depth, 2),
            "max_leverage": maxlev,
            "taker_fee_bps": round(taker, 2),
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
