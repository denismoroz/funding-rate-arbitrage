import json, urllib.request
FRAB = ["BTC","ETH","SOL","HYPE","AVAX","LINK","DOGE","ZEC","XPL"]
def get(url,data=None,headers=None,method=None):
    req=urllib.request.Request(url,data=data,headers=headers or {},method=method)
    return urllib.request.urlopen(req,timeout=15).read()

print("Spot availability for FRAB coins (long-leg / unified delta-neutral):\n")

# HL spot (Unit bridge tokens)
try:
    d=json.loads(get("https://api.hyperliquid.xyz/info",data=b'{"type":"spotMeta"}',headers={"Content-Type":"application/json"},method="POST"))
    tokens={t["name"] for t in d["tokens"]}
    # HL majors are uBTC/uETH... plus native HYPE/PURR
    def hl_has(c):
        return c in tokens or ("U"+c) in tokens or ("u"+c) in tokens
    res={c:("YES" if hl_has(c) else "no") for c in FRAB}
    print("HL spot   :", res, f"(total spot tokens={len(tokens)})")
except Exception as e: print("HL err",e)

# Aster spot
try:
    d=json.loads(get("https://sapi.asterdex.com/api/v1/exchangeInfo"))
    bases={s["baseAsset"] for s in d.get("symbols",[])}
    print("Aster spot:", {c:("YES" if c in bases else "no") for c in FRAB}, f"(total spot pairs={len(d.get('symbols',[]))})")
except Exception as e: print("Aster spot err",e)

# Backpack spot
try:
    d=json.loads(get("https://api.backpack.exchange/api/v1/markets"))
    spot_bases={m["baseSymbol"] for m in d if m.get("marketType")=="SPOT"}
    print("Backpack  :", {c:("YES" if c in spot_bases else "no") for c in FRAB}, f"(total spot={len(spot_bases)})")
except Exception as e: print("Backpack err",e)

print("\nPerp-only (NO spot leg possible — FRAB needs external/decoupled spot):")
print("  dYdX v4, Lighter, Paradex, edgeX, Vertex(perp), Drift(has spot on Solana)")
