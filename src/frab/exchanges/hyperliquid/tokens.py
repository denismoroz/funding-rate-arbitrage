"""Hyperliquid token map + spot-pair validation.

Centralizes wrapped-token names and the fail-fast startup check that
ensures every coin in the universe maps to a real HL USDC-quoted spot
pair. Lives under exchanges/ (not hyperliquid/) for F2.3 because the
hyperliquid/ package doesn't exist yet — F2.6 will consolidate
hyperliquid.py + hyperliquid_live.py + atomic.py into hyperliquid/ and
this file will move there too.

CRITICAL: spot leg only safe to use when the wrapped token is BOTH
  (a) priced 1:1 with the HL canonical perp coin (real bridge, not separate
      price discovery), AND
  (b) has enough liquidity that a market order can match within slippage.

Live audit 2026-05-19:
  UBTC, UETH, USOL — tight spreads, deep books, 1:1 with perp -> SAFE.
  UAVAX — exists but UAVAX trades $8-9 while AVAX perp ~$13.5 (not 1:1) AND
    top-of-book spread is ~9% -> market orders fail. EXCLUDED.
  LINK0, AAVE0, AVAX0 — HL's EVM bridges, independent price discovery,
    break delta-neutrality (LINK0 incident: -$3 on supposedly hedged pos).
    EXCLUDED.
  DOGE, etc. — no spot pair on HL mainnet at all.
"""
from __future__ import annotations

# EVM bridge tokens: independent price discovery from the canonical perp — mapping one
# to the other silently breaks delta-neutrality and cost real money in testing (-$3 incident).
BRIDGE_TOKEN_BLACKLIST: frozenset[str] = frozenset({"AVAX0", "LINK0", "AAVE0"})

MAINNET_SPOT_TOKEN_MAP: dict[str, str] = {
    "BTC": "UBTC",
    "ETH": "UETH",
    "SOL": "USOL",
    # Native HL spot tokens — wrapped name == canonical perp coin
    # (HL-first-class, not bridged), so identity mapping is safe.
    "HYPE": "HYPE",
    "PURR": "PURR",
    "ZEC": "ZEC",
    "XPL": "XPL",
}


def select_spot_token_map(network: str) -> dict[str, str]:
    """Spot base-token map; only mainnet uses wrapped names."""
    return MAINNET_SPOT_TOKEN_MAP if network == "mainnet" else {}


async def validate_spot_pairs(market_data, coins: tuple[str, ...]) -> None:
    """Verify every coin's spot/USDC pair exists on HL with the expected base token.

    Fail-fast at engine startup if MAINNET_SPOT_TOKEN_MAP entry doesn't resolve
    to a real HL spot pair quoted in USDC. Prevents accidentally trading the
    canonical perp against a non-1:1 wrapped token (breaks delta-neutrality).
    """
    meta = await market_data._post({"type": "spotMeta"})
    tokens = {
        t["index"]: t.get("name", "")
        for t in meta.get("tokens", [])
        if isinstance(t.get("index"), int)
    }
    usdc_idx: int | None = next(
        (i for i, n in tokens.items() if n == "USDC"), None
    )
    if usdc_idx is None:
        raise RuntimeError("HL spotMeta: USDC token not found")

    base_to_pair: dict[str, str] = {}
    for u in meta.get("universe", []):
        toks = u.get("tokens") or []
        if len(toks) != 2 or toks[1] != usdc_idx:
            continue
        base_name = tokens.get(toks[0])
        if base_name:
            base_to_pair[base_name] = u.get("name", "")

    missing: list[str] = []
    mismatched: list[str] = []
    for coin in coins:
        expected_base = MAINNET_SPOT_TOKEN_MAP.get(coin)
        if expected_base is None:
            missing.append(f"{coin} (no map entry)")
            continue
        if expected_base not in base_to_pair:
            mismatched.append(f"{coin} -> {expected_base}/USDC (not on HL)")

    if missing or mismatched:
        raise RuntimeError(
            "Spot-pair validation failed for HL mainnet. "
            f"missing_map: {missing}; not_on_hl: {mismatched}. "
            "Either remove these coins from the universe or fix "
            "MAINNET_SPOT_TOKEN_MAP."
        )
