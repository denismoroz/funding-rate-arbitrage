"""Paradex XSMOM venue probe (Starknet appchain, perp)."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from _common import (UNIVERSE, DEPTH_PROBE, get_json, book_metrics, now_iso)

BASE = "https://api.prod.paradex.trade/v1"
VENUE = "paradex"
TAKER_FEE_BPS = 3.0  # 0.03% taker (verify)


def main():
    rows = []
    mk = get_json(BASE + "/markets")
    results = mk.get("results", mk if isinstance(mk, list) else [])
    by_coin = {}
    for m in results:
        sym = m.get("symbol", "")
        if not sym.endswith("-USD-PERP"):
            continue
        base = sym.split("-")[0]
        by_coin[base] = m
    # summary (volume / funding)
    summ = {}
    try:
        s = get_json(BASE + "/markets/summary?market=ALL")
        for row in s.get("results", []):
            summ[row.get("symbol")] = row
    except Exception as e:
        print(f"summary fail: {e}", file=sys.stderr)

    for coin in UNIVERSE:
        m = by_coin.get(coin)
        listed = m is not None
        sym = m["symbol"] if listed else None
        vol = 0.0; maxlev = 0; mark = None
        if listed:
            srow = summ.get(sym, {})
            vol = float(srow.get("volume_24h") or 0)
            mark = float(srow.get("mark_price") or srow.get("last_traded_price") or 0) or None
            ml = m.get("max_leverage") or m.get("leverage")
            if ml:
                maxlev = int(float(ml))
        spread = None; depth = 0.0
        if listed and coin in DEPTH_PROBE:
            try:
                ob = get_json(BASE + f"/orderbook/{sym}?depth=100")
                bids = ob.get("bids", [])
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
