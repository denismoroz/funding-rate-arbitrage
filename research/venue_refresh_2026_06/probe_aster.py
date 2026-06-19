"""Aster (asterdex) XSMOM venue probe. Binance-fapi-like."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from _common import (UNIVERSE, DEPTH_PROBE, get_json, book_metrics, now_iso)

BASE = "https://fapi.asterdex.com"
VENUE = "aster"
TAKER_FEE_BPS = 3.5  # 0.035% taker (verify)


def main():
    rows = []
    info = get_json(BASE + "/fapi/v1/exchangeInfo")
    symbols = {}  # COIN -> symbol info
    for s in info.get("symbols", []):
        if s.get("contractType") != "PERPETUAL" and s.get("contractType"):
            continue
        if s.get("quoteAsset") not in ("USDT", "USD", "USDC"):
            continue
        base = s.get("baseAsset")
        symbols[base] = s
    # 24h tickers
    tickers = {}
    try:
        for t in get_json(BASE + "/fapi/v1/ticker/24hr"):
            tickers[t["symbol"]] = t
    except Exception as e:
        print(f"ticker fail: {e}", file=sys.stderr)

    for coin in UNIVERSE:
        info_s = symbols.get(coin)
        listed = info_s is not None
        sym = info_s["symbol"] if listed else None
        vol = 0.0; maxlev = 0; mark = None
        if listed:
            t = tickers.get(sym)
            if t:
                vol = float(t.get("quoteVolume") or 0)  # quote = USDT notional
                mark = float(t.get("lastPrice") or 0) or None
        spread = None; depth = 0.0
        if listed and coin in DEPTH_PROBE:
            try:
                ob = get_json(BASE + f"/fapi/v1/depth?symbol={sym}&limit=500")
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
    n = sum(1 for c in UNIVERSE if c in symbols)
    print(f"{VENUE}: {n}/32 listed; missing: {[c for c in UNIVERSE if c not in symbols]}", file=sys.stderr)
    return rows


if __name__ == "__main__":
    import csv
    rows = main()
    out = os.path.join(os.path.dirname(__file__), f"_{VENUE}.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    print(f"wrote {out}")
