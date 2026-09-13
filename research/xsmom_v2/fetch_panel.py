"""Point-in-time Binance USDT-M perp panel for the XSMOM rework, 2020-01 .. 2026-09 (daily).

Why Binance: the HL-era panel has ~3 years, too little to tell a Sharpe-0.5 edge from luck, and it was
assembled from coins alive in 2026. Binance still serves full history for delisted perps (LUNA, SRM, FTT,
MIR, ANC ...), so the universe at every date can be the coins that were actually tradable then.

Symbol discovery: base assets from spot exchangeInfo (TRADING + BREAK) and futures exchangeInfo
(incl. SETTLING); a symbol is kept if fapi daily klines exist. Excluded: stablecoins, fiat, leveraged
tokens, index perps. Output (scratchpad, not committed): klines/<SYM>.csv (open_ms, close, quote_volume),
funding/<SYM>.csv (fundingTime, fundingRate).
"""
import re, sys, time
from pathlib import Path
import pandas as pd, requests

OUT = Path("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/xsmom_binance")
START = int(pd.Timestamp("2019-12-01", tz="UTC").timestamp() * 1000)
END = int(pd.Timestamp("2026-09-13", tz="UTC").timestamp() * 1000)
STABLE = {"USDC", "BUSD", "TUSD", "USDP", "PAX", "DAI", "FDUSD", "USDSB", "USDS", "SUSD", "EUR", "GBP", "AUD", "TRY",
          "BRL", "RUB", "UST", "USTC", "AEUR", "EURI", "USDE", "BFUSD", "XUSD", "PAXG", "WBTC", "WBETH", "BTCST",
          "BIDR", "IDRT", "NGN", "UAH", "ZAR", "PLN", "RON", "ARS", "COP", "JPY", "MXN", "CZK", "USD1", "RLUSD"}
INDEX = {"DEFI", "BTCDOM", "FOOTBALL", "BLUEBIRD", "USDCUSDT"}
S = requests.Session()


def get(url, params, pause):
    for attempt in range(6):
        try:
            r = S.get(url, params=params, timeout=30)
        except requests.RequestException:
            time.sleep(5 * (attempt + 1)); continue
        if r.status_code in (418, 429):
            time.sleep(int(r.headers.get("Retry-After", 30))); continue
        if r.status_code == 400:
            return None                       # unknown symbol
        r.raise_for_status(); time.sleep(pause)
        return r.json()
    raise RuntimeError(f"failed {url} {params}")


def symbols():
    spot = get("https://api.binance.com/api/v3/exchangeInfo", {}, 1)["symbols"]
    fut = get("https://fapi.binance.com/fapi/v1/exchangeInfo", {}, 1)["symbols"]
    bases = {s["baseAsset"] for s in spot if s["quoteAsset"] == "USDT"}
    syms = {s["symbol"] for s in fut if s["quoteAsset"] == "USDT" and s["contractType"] == "PERPETUAL"}
    for b in bases:
        syms.add(f"{b}USDT")
    keep = []
    for s in sorted(syms):
        base = s[:-4]
        if base in STABLE or base in INDEX or re.search(r"(UP|DOWN|BULL|BEAR)$", base) and len(base) > 4:
            continue
        keep.append(s)
    return keep


def klines(sym):
    rows, s = [], START
    while s < END:
        d = get("https://fapi.binance.com/fapi/v1/klines",
                {"symbol": sym, "interval": "1d", "startTime": s, "endTime": END, "limit": 1000}, 0.1)
        if not d: break
        rows += d; s = d[-1][0] + 86_400_000
        if len(d) < 1000: break
    return rows


def funding(sym):
    rows, s = [], START
    while s < END:
        d = get("https://fapi.binance.com/fapi/v1/fundingRate",
                {"symbol": sym, "startTime": s, "endTime": END, "limit": 1000}, 0.12)
        if not d: break
        rows += d; s = int(d[-1]["fundingTime"]) + 1
        if len(d) < 1000: break
    return rows


def liquid_union(top=60, min_days=90):
    """Symbols that were ever in the top-`top` by trailing 30d median quote volume (>= min_days listed)."""
    vols = {}
    for f in (OUT / "klines").glob("*.csv"):
        if f.stat().st_size < 40:
            continue
        k = pd.read_csv(f)
        idx = pd.to_datetime(k["open_ms"], unit="ms", utc=True)
        v = pd.Series(k["quote_volume"].values, index=idx)
        v = v.rolling(30, min_periods=20).median().where(pd.Series(range(len(v)), index=idx) >= min_days)
        vols[f.stem] = v
    panel = pd.DataFrame(vols)
    ranks = panel.rank(axis=1, ascending=False)
    return sorted(ranks.columns[(ranks <= top).any()])


def main():
    (OUT / "klines").mkdir(parents=True, exist_ok=True); (OUT / "funding").mkdir(parents=True, exist_ok=True)
    syms = symbols()
    print("candidate symbols", len(syms), flush=True)
    for i, sym in enumerate(syms):                      # phase 1: daily klines for every symbol
        kf = OUT / "klines" / f"{sym}.csv"
        if kf.exists():
            continue
        k = klines(sym)
        if not k:
            kf.write_text("open_ms,close,quote_volume\n"); continue
        pd.DataFrame([(r[0], float(r[4]), float(r[7])) for r in k],
                     columns=["open_ms", "close", "quote_volume"]).to_csv(kf, index=False)
        if i % 50 == 0:
            print(f"klines {i}/{len(syms)}", flush=True)
    liquid = liquid_union()
    print("ever in top-60 by volume:", len(liquid), flush=True)
    for i, sym in enumerate(liquid):                    # phase 2: funding for the liquid ones only
        ff = OUT / "funding" / f"{sym}.csv"
        if ff.exists():
            continue
        f = funding(sym)
        pd.DataFrame([(int(r["fundingTime"]), float(r["fundingRate"])) for r in f],
                     columns=["fundingTime", "fundingRate"]).to_csv(ff, index=False)
        if i % 20 == 0:
            print(f"funding {i}/{len(liquid)}", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
