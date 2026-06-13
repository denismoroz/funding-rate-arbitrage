"""
HL Universe Audit for Cross-Sectional Momentum Book (Strategy C / xsmom).

Audits whether the 34-coin backtest universe can be traded live on Hyperliquid perps.

Outputs:
  - Prints a ranked table to stdout + a VERDICT paragraph
  - Writes hl_universe_audit.json (machine-readable per-coin table + summary)

Raw API pulls are cached under data/ (gitignored).

Bridge-token rule: NEVER map digit-suffixed tokens (e.g. AVAX0) to canonical perps.
Exact name match only.

Run:
  python research/cross_sectional/crypto/hl_universe_audit.py
"""

import json
import sys
import time
from pathlib import Path

import requests

# ── paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
DATA_DIR   = SCRIPT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

HL_URL = "https://api.hyperliquid.xyz/info"

META_CACHE    = DATA_DIR / "hl_meta_ctxs_raw.json"
L2_CACHE_DIR  = DATA_DIR / "l2_books"
L2_CACHE_DIR.mkdir(exist_ok=True)

# ── universe ──────────────────────────────────────────────────────────────────
with open(SCRIPT_DIR / "universe.json") as f:
    _uni = json.load(f)
UNIVERSE_COINS: list[str] = _uni["coins"]  # 34 survivors

with open(SCRIPT_DIR / "survivorship.json") as f:
    _surv = json.load(f)
EXTRA_DEAD: list[str] = _surv["extra_dead_coins_included"]

# Bridge-token guard: any name ending in a digit is a bridge token.
def is_bridge_token(name: str) -> bool:
    return bool(name) and name[-1].isdigit()


# ── HL meta + asset contexts ──────────────────────────────────────────────────

def fetch_meta_ctxs(force: bool = False) -> tuple[list[dict], list[dict]]:
    """Return (meta.universe list, assetCtxs list). Caches to disk."""
    if not force and META_CACHE.exists():
        with open(META_CACHE) as f:
            raw = json.load(f)
        print(f"[cache] loaded HL meta from {META_CACHE.name}")
        return raw["meta_universe"], raw["asset_ctxs"]

    print("[fetch] pulling metaAndAssetCtxs from HL…")
    r = requests.post(HL_URL, json={"type": "metaAndAssetCtxs"}, timeout=30)
    r.raise_for_status()
    meta, ctxs = r.json()
    with open(META_CACHE, "w") as f:
        json.dump({"meta_universe": meta["universe"], "asset_ctxs": ctxs}, f)
    print(f"[fetch] cached {len(meta['universe'])} perps -> {META_CACHE.name}")
    return meta["universe"], ctxs


# ── L2 order book ─────────────────────────────────────────────────────────────

def fetch_l2(coin: str, force: bool = False) -> dict | None:
    """Fetch L2 book for coin, cache under data/l2_books/<coin>.json."""
    if is_bridge_token(coin):
        return None  # project policy: skip bridge tokens
    cache = L2_CACHE_DIR / f"{coin}.json"
    if not force and cache.exists():
        with open(cache) as f:
            return json.load(f)
    try:
        r = requests.post(HL_URL, json={"type": "l2Book", "coin": coin}, timeout=10)
        if r.status_code == 422:
            # coin not on HL
            return None
        r.raise_for_status()
        data = r.json()
        with open(cache, "w") as f:
            json.dump(data, f)
        return data
    except Exception as e:
        print(f"  [warn] L2 fetch failed for {coin}: {e}", file=sys.stderr)
        return None


def compute_spread(l2: dict | None) -> tuple[float | None, float | None]:
    """
    Returns (half_spread_bps, top2_depth_usd) from an L2 book response.
    half_spread_bps = 1e4 * (bestAsk - bestBid) / (2 * mid)
    depth = sum of sz*px for top-2 levels on each side.
    """
    if l2 is None:
        return None, None
    try:
        levels = l2.get("levels", [])
        if len(levels) < 2:
            return None, None
        bids = levels[0]  # list of {px, sz, n}
        asks = levels[1]
        if not bids or not asks:
            return None, None
        best_bid = float(bids[0]["px"])
        best_ask = float(asks[0]["px"])
        mid = (best_bid + best_ask) / 2.0
        if mid == 0:
            return None, None
        half_spread_bps = 1e4 * (best_ask - best_bid) / (2.0 * mid)

        # top-2 depth each side
        bid_depth = sum(float(b["px"]) * float(b["sz"]) for b in bids[:2])
        ask_depth = sum(float(a["px"]) * float(a["sz"]) for a in asks[:2])
        top2_depth = bid_depth + ask_depth

        return round(half_spread_bps, 3), round(top2_depth, 2)
    except Exception:
        return None, None


# ── liquidity tier ────────────────────────────────────────────────────────────

def classify_tier(
    present: bool,
    delisted: bool,
    vol_usd: float,
    spread_bps: float | None,
) -> str:
    """
    DEEP:       vol > $50M/day AND spread < 3 bps
    TRADEABLE:  vol > $5M/day  AND spread < 10 bps  (OR spread unknown but vol > $5M)
    THIN:       present but vol < $5M OR spread > 10 bps
    NOT_ON_HL:  not in HL perp universe or isDelisted
    """
    if not present or delisted:
        return "NOT_ON_HL"
    if vol_usd >= 50_000_000 and (spread_bps is None or spread_bps < 3.0):
        return "DEEP"
    if vol_usd >= 5_000_000 and (spread_bps is None or spread_bps < 10.0):
        return "TRADEABLE"
    # vol > $5M but spread >= 10 → THIN
    # vol < $5M → THIN regardless
    return "THIN"


# ── main audit ────────────────────────────────────────────────────────────────

def run_audit(force: bool = False) -> None:
    # 1. Fetch HL metadata
    meta_universe, asset_ctxs = fetch_meta_ctxs(force=force)

    # Build lookup: name -> (meta_entry, ctx_entry)
    hl_lookup: dict[str, dict] = {}
    for u, c in zip(meta_universe, asset_ctxs):
        hl_lookup[u["name"]] = {
            "meta": u,
            "ctx":  c,
        }

    # 2. Fetch L2 books for all universe coins + extra_dead
    all_coins_to_check = UNIVERSE_COINS + EXTRA_DEAD
    print(f"\n[fetch] pulling L2 books for {len(all_coins_to_check)} coins…")
    for coin in all_coins_to_check:
        if is_bridge_token(coin):
            print(f"  [skip] {coin} — bridge token (digit suffix)")
            continue
        fetch_l2(coin, force=force)
        time.sleep(0.15)  # be polite

    # 3. Build per-coin audit table
    print("\n[audit] building per-coin table…")
    rows: list[dict] = []

    def audit_coin(coin: str, is_dead: bool) -> dict:
        # Bridge-token guard
        if is_bridge_token(coin):
            return {
                "coin":            coin,
                "category":        "BRIDGE_TOKEN_EXCLUDED",
                "present_on_hl":   False,
                "isDelisted":      None,
                "dayNtlVlm":       None,
                "oiUsd":           None,
                "maxLeverage":     None,
                "markPx":         None,
                "half_spread_bps": None,
                "top2_depth_usd":  None,
                "tier":            "NOT_ON_HL",
                "in_dead_set":     is_dead,
                "note":            "digit-suffix bridge token — excluded by project policy",
            }

        entry = hl_lookup.get(coin)
        if entry is None:
            return {
                "coin":            coin,
                "category":        "survivor" if not is_dead else "extra_dead",
                "present_on_hl":   False,
                "isDelisted":      None,
                "dayNtlVlm":       0.0,
                "oiUsd":           0.0,
                "maxLeverage":     None,
                "markPx":         None,
                "half_spread_bps": None,
                "top2_depth_usd":  None,
                "tier":            "NOT_ON_HL",
                "in_dead_set":     is_dead,
                "note":            "name not found in HL meta.universe",
            }

        meta_e = entry["meta"]
        ctx_e  = entry["ctx"]
        delisted = bool(meta_e.get("isDelisted", False))
        mark    = float(ctx_e.get("markPx",      0) or 0)
        oi_sz   = float(ctx_e.get("openInterest", 0) or 0)
        vol     = float(ctx_e.get("dayNtlVlm",    0) or 0)
        max_lev = int(meta_e.get("maxLeverage",   0) or 0)
        oi_usd  = oi_sz * mark

        l2_data = fetch_l2(coin)   # from cache
        spread_bps, depth = compute_spread(l2_data)

        tier = classify_tier(
            present=True,
            delisted=delisted,
            vol_usd=vol,
            spread_bps=spread_bps,
        )

        return {
            "coin":            coin,
            "category":        "survivor" if not is_dead else "extra_dead",
            "present_on_hl":   not delisted,
            "isDelisted":      delisted,
            "dayNtlVlm":       round(vol, 0),
            "oiUsd":           round(oi_usd, 0),
            "maxLeverage":     max_lev,
            "markPx":         round(mark, 6) if mark else None,
            "half_spread_bps": spread_bps,
            "top2_depth_usd":  depth,
            "tier":            tier,
            "in_dead_set":     is_dead,
            "note":            "delisted on HL" if delisted else "",
        }

    for coin in UNIVERSE_COINS:
        rows.append(audit_coin(coin, is_dead=False))
    for coin in EXTRA_DEAD:
        rows.append(audit_coin(coin, is_dead=True))

    # 4. Print ranked table (survivor coins only, sorted by volume)
    survivors = [r for r in rows if r["category"] == "survivor"]
    survivors_sorted = sorted(survivors, key=lambda r: r["dayNtlVlm"] or 0, reverse=True)

    COL_W = 10
    HEADER = (
        f"{'Coin':<8}  {'Tier':<12}  {'24h Vol $M':>10}  {'OI $M':>8}  "
        f"{'MaxLev':>6}  {'Spread bps':>10}  {'Depth $':>10}  {'Note'}"
    )
    SEP = "-" * len(HEADER)

    print("\n" + "=" * 80)
    print("HL UNIVERSE AUDIT — Cross-Sectional Momentum Book (2026-06-13 snapshot)")
    print("=" * 80)
    print(HEADER)
    print(SEP)

    tier_order = {"DEEP": 0, "TRADEABLE": 1, "THIN": 2, "NOT_ON_HL": 3}

    for r in survivors_sorted:
        vol_m   = f"{r['dayNtlVlm']/1e6:.1f}"  if r["dayNtlVlm"] else "—"
        oi_m    = f"{r['oiUsd']/1e6:.1f}"       if r["oiUsd"]     else "—"
        lev     = str(r["maxLeverage"])          if r["maxLeverage"] else "—"
        spread  = f"{r['half_spread_bps']:.2f}"  if r["half_spread_bps"] is not None else "—"
        depth   = f"{r['top2_depth_usd']:,.0f}"  if r["top2_depth_usd"] is not None  else "—"
        note    = r["note"] or ""
        print(
            f"{r['coin']:<8}  {r['tier']:<12}  {vol_m:>10}  {oi_m:>8}  "
            f"{lev:>6}  {spread:>10}  {depth:>10}  {note}"
        )

    print(SEP)

    # Extra dead coins — brief summary
    dead_rows = [r for r in rows if r["in_dead_set"]]
    dead_on_hl   = [r for r in dead_rows if r["present_on_hl"]]
    dead_off_hl  = [r for r in dead_rows if not r["present_on_hl"]]

    print(f"\nExtra dead/delisted coins ({len(EXTRA_DEAD)} total):")
    print(f"  Still on HL (not delisted): {len(dead_on_hl)}  — "
          f"{[r['coin'] for r in dead_on_hl]}")
    print(f"  Not on HL / delisted:       {len(dead_off_hl)}  — "
          f"{[r['coin'] for r in dead_off_hl]}")

    # 5. Compute summary stats
    usable = [r for r in survivors if r["tier"] in ("DEEP", "TRADEABLE")]
    thin   = [r for r in survivors if r["tier"] == "THIN"]
    not_hl = [r for r in survivors if r["tier"] == "NOT_ON_HL"]

    N_usable  = len(usable)
    N_thin    = len(thin)
    N_not_hl  = len(not_hl)
    k_usable  = N_usable // 3
    positions = 2 * k_usable

    # Backtest reference: 34 → k=11, 22 positions
    k_bt = 34 // 3  # 11
    pos_bt = 2 * k_bt  # 22

    # Spread analysis vs 8.5 bps/leg assumption
    spreads_deep  = [r["half_spread_bps"] for r in usable
                     if r["tier"] == "DEEP" and r["half_spread_bps"] is not None]
    spreads_trade = [r["half_spread_bps"] for r in usable
                     if r["tier"] == "TRADEABLE" and r["half_spread_bps"] is not None]

    avg_spread_deep  = (sum(spreads_deep)  / len(spreads_deep))  if spreads_deep  else None
    avg_spread_trade = (sum(spreads_trade) / len(spreads_trade)) if spreads_trade else None

    # Min capital: 2k positions × $12 minimum notional
    min_capital_12 = positions * 12
    min_capital_50 = positions * 50  # more practical minimum per position

    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"  Total universe.json coins:    34")
    print(f"  DEEP:                         {len([r for r in survivors if r['tier']=='DEEP'])}")
    print(f"  TRADEABLE:                    {len([r for r in survivors if r['tier']=='TRADEABLE'])}")
    print(f"  THIN:                         {N_thin}  — {[r['coin'] for r in thin]}")
    print(f"  NOT_ON_HL:                    {N_not_hl}  — {[r['coin'] for r in not_hl]}")
    print(f"")
    print(f"  Usable (DEEP+TRADEABLE):      N = {N_usable}")
    print(f"  Tercile size k = N//3:        k = {k_usable}  (backtest: k=11)")
    print(f"  Total positions 2k:           {positions}  (backtest: 22)")
    print(f"")
    print(f"  Min collateral @ $12/pos:     ${min_capital_12:,}")
    print(f"  Practical collateral @ $50:   ${min_capital_50:,}")
    print(f"")
    if avg_spread_deep is not None:
        print(f"  Avg half-spread DEEP coins:   {avg_spread_deep:.2f} bps")
    if avg_spread_trade is not None:
        print(f"  Avg half-spread TRADEABLE:    {avg_spread_trade:.2f} bps")
    print(f"  Backtest cost assumption:     8.5 bps/leg")

    # Spread vs 8.5 bps verdict
    if avg_spread_deep is not None and avg_spread_deep < 3.0:
        spread_verdict_deep = "REALISTIC (spread well below 8.5 bps assumption)"
    else:
        spread_verdict_deep = "UNCLEAR (insufficient DEEP data)"

    if avg_spread_trade is not None:
        if avg_spread_trade < 8.5:
            spread_verdict_trade = "REALISTIC for TRADEABLE coins"
        else:
            spread_verdict_trade = "TIGHT — 8.5 bps assumption may be OPTIMISTIC for TRADEABLE tier"
    else:
        spread_verdict_trade = "INSUFFICIENT DATA"

    print(f"\n  Spread vs 8.5bps:")
    print(f"    DEEP tier:       {spread_verdict_deep}")
    print(f"    TRADEABLE tier:  {spread_verdict_trade}")
    print(f"    THIN tier:       8.5 bps assumption LIKELY OPTIMISTIC (spread > 10 bps)")

    # 6. VERDICT
    tercile_risk = N_usable < 18  # flag if materially below 34
    print("\n" + "=" * 80)
    print("VERDICT")
    print("=" * 80)
    verdict_lines = [
        f"Of the 34 backtest-universe coins, {N_usable} are realistically tradeable on HL today "
        f"({len([r for r in survivors if r['tier']=='DEEP'])} DEEP, "
        f"{len([r for r in survivors if r['tier']=='TRADEABLE'])} TRADEABLE); "
        f"{N_thin} are THIN (risky), {N_not_hl} are absent/delisted.",

        f"With N={N_usable} usable coins, the tercile shrinks to k={k_usable} (vs k=11 in backtest), "
        f"giving only {positions} total positions (vs 22). "
        + ("THIS IS A MATERIAL REDUCTION — the cross-section is dangerously small, "
           "momentum signal becomes noisy at this pool size."
           if tercile_risk else
           "The cross-section is somewhat narrower than the backtest but still workable."),

        f"Spread vs 8.5 bps/leg backtest assumption: DEEP coins are well inside the assumption; "
        f"TRADEABLE coins {'are borderline' if avg_spread_trade and avg_spread_trade > 5.0 else 'are also within it'}. "
        f"THIN coins likely breach the assumption.",

        f"Minimum capital for {positions} positions at $12 notional each = ${min_capital_12:,}; "
        f"a practical $50/position floor implies ${min_capital_50:,} collateral.",

        f"Biggest caveat: this is a ONE-SNAPSHOT liquidity audit (2026-06-13). "
        f"The backtest spans 2023-06 to 2026-06 — many of these coins had far lower liquidity "
        f"in 2023, so the backtest cost assumption may already be too generous for the "
        f"early history. The audit bounds the FORWARD tradeable set, not the historical one. "
        f"Additionally, HL dayNtlVlm is exchange self-reported and L2 spreads are instantaneous, "
        f"not time-averaged effective spreads.",
    ]
    for line in verdict_lines:
        # wrap at ~100 chars
        import textwrap
        for wrapped in textwrap.wrap(line, width=100):
            print(" ", wrapped)
        print()

    # 7. Write JSON output
    summary = {
        "snapshot_date":        "2026-06-13",
        "n_universe":           34,
        "n_deep":               len([r for r in survivors if r["tier"] == "DEEP"]),
        "n_tradeable":          len([r for r in survivors if r["tier"] == "TRADEABLE"]),
        "n_thin":               N_thin,
        "n_not_on_hl":          N_not_hl,
        "n_usable":             N_usable,
        "k_per_side":           k_usable,
        "total_positions":      positions,
        "backtest_k":           k_bt,
        "backtest_positions":   pos_bt,
        "min_capital_12usd":    min_capital_12,
        "min_capital_50usd":    min_capital_50,
        "avg_spread_deep_bps":  round(avg_spread_deep,  3) if avg_spread_deep  else None,
        "avg_spread_trade_bps": round(avg_spread_trade, 3) if avg_spread_trade else None,
        "backtest_cost_bps":    8.5,
        "spread_vs_8p5bps": {
            "deep":      spread_verdict_deep,
            "tradeable": spread_verdict_trade,
            "thin":      "8.5 bps assumption likely OPTIMISTIC (spread > 10 bps)",
        },
        "tercile_shrinkage_flag": tercile_risk,
        "usable_coins":    [r["coin"] for r in survivors if r["tier"] in ("DEEP", "TRADEABLE")],
        "thin_coins":      [r["coin"] for r in survivors if r["tier"] == "THIN"],
        "not_on_hl_coins": [r["coin"] for r in survivors if r["tier"] == "NOT_ON_HL"],
        "extra_dead_on_hl":    [r["coin"] for r in dead_rows if r["present_on_hl"]],
        "extra_dead_not_on_hl":[r["coin"] for r in dead_rows if not r["present_on_hl"]],
        "caveats": [
            "Snapshot audit 2026-06-13; backtest spans 2023-06 to 2026-06 — forward-looking only.",
            "dayNtlVlm is HL self-reported; L2 spreads are instantaneous snapshots, not time-averaged effective spreads.",
            "HL carry-arb viability (separate criterion, ~7 coins) is distinct from momentum price-liquidity criterion.",
            "Some TRADEABLE coins had lower liquidity in 2023; backtest 8.5 bps cost assumption may be optimistic for early history.",
        ],
    }

    out_path = SCRIPT_DIR / "hl_universe_audit.json"
    with open(out_path, "w") as f:
        json.dump({"coins": rows, "summary": summary}, f, indent=2)
    print(f"[output] wrote {out_path}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true", help="Bypass cache, re-fetch from HL API")
    args = p.parse_args()
    run_audit(force=args.force)
