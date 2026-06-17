"""CoinDiscovery — Phase C write-path for market-fact fields.

Given a canonical perp ticker, queries the HL public /info endpoint (no
private key), resolves market facts, validates the spot pair, and writes
them into the coin_registry row with ``validated_at`` set.

Design contract (per plan decision #4/#5):
- Market-fact fields (spot_token, sz_decimals, bridge_safe) are NEVER typed
  by a human.  They are DISCOVERED here from HL and written automatically.
- If ANY step fails, NO row is written (atomicity: no half-valid state).
- Bridge-token guard: if the resolved spot token is in BRIDGE_TOKEN_BLACKLIST,
  spot_token is set to None and bridge_safe=False, and the coin is still
  registerable as perp-only (NOT outright rejected, see rule below).
- Price-parity guard: even if a spot token is not blacklisted, its mid price
  must be within SPOT_PERP_PARITY_TOLERANCE of the perp mid price (both from
  allMids).  Tokens like UAVAX ($8) vs the AVAX perp ($13.5) fail this check
  and are treated identically to the bridge-blacklist case.

Bridge guard rule (per discussion with operator):
  If a coin resolves a spot token that is in BRIDGE_TOKEN_BLACKLIST we treat
  it the SAME as "no safe USDC-quoted spot pair":
    - spot_token is NOT written (left None)
    - bridge_safe = False
    - validated_at IS written (perp-only valid, no spot leg)
  Rationale: we cannot safely trade spot for this coin, but the perp is
  perfectly fine.  A deliberately blacklisted token appearing as the spot pair
  for a perp coin is just an HL naming artefact (e.g. a hypothetical coin
  whose only HL spot pair happens to be an EVM bridge token).

perp-only coins (no USDC-quoted spot pair at all) behave identically.

HLClient reuse:
  The service receives an HLClient instance from the caller (typically the
  same client already in use by HLExchange).  All HL I/O goes through:
    client.perp_meta()  — HLClient.perp_meta() → list[HLPerpMarketSpec]
    client.spot_meta()  — HLClient.spot_meta() → HLSpotMeta
    client.all_mids()   — HLClient.all_mids() → dict[str, float] (price-parity check)
    client._post({"type": "spotMeta"}) — for validate_spot_pairs (tokens.py)

  No new HTTP layer; no private key needed.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.tokens import (
    BRIDGE_TOKEN_BLACKLIST,
    validate_spot_pairs,
)
from frab.repo.coin_registry_repo import CoinEntry, CoinRegistryRepo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Parity tolerance
# ---------------------------------------------------------------------------

# Maximum allowed deviation between the spot mid and the perp mid for a token
# to be considered 1:1 with the perp (i.e. truly wrapped, not independent price
# discovery).  Real wrapped tokens (UBTC, UETH, USOL, HYPE, ZEC, XPL) sit within
# ~0.1–1% of the perp.  UAVAX trades ~40% below the AVAX perp — rejected with
# wide margin.  3% gives comfortable room for normal spread without false positives.
SPOT_PERP_PARITY_TOLERANCE = 0.03


# ---------------------------------------------------------------------------
# Discovered facts dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DiscoveredFacts:
    """Market facts resolved from HL for a single canonical perp ticker."""
    coin: str
    sz_decimals: int              # from perp meta (always present for known perps)
    spot_token: str | None        # wrapped HL base token, or None (perp-only / blacklisted)
    bridge_safe: bool             # True iff spot_token is set and NOT blacklisted


# ---------------------------------------------------------------------------
# CoinDiscovery service
# ---------------------------------------------------------------------------

class CoinDiscovery:
    """Resolves HL market facts for a canonical perp ticker and writes them
    into the coin_registry via CoinRegistryRepo.

    Designed for single-use per coin-add operation (Phase D API call).
    A shared HLClient instance can be passed in or created by the caller.

    No private key required; all calls use the public /info endpoint.
    """

    def __init__(
        self,
        client: HLClient,
        repo: CoinRegistryRepo,
    ) -> None:
        self._client = client
        self._repo = repo

    # ── Public API ──────────────────────────────────────────────────────────

    async def discover(self, coin: str) -> DiscoveredFacts:
        """Query HL and resolve market facts for ``coin``.

        Steps:
        1. perp_meta → confirm the perp exists; extract sz_decimals.
        2. spot_meta → find a USDC-quoted spot pair whose base token maps
           to ``coin`` (case-insensitive match: canonical == base or canonical
           matches the HL convention like BTC→UBTC, ETH→UETH, SOL→USOL,
           identity tokens HYPE/PURR/ZEC/XPL where base==coin).
        3. Bridge check: if resolved base is in BRIDGE_TOKEN_BLACKLIST →
           treat as perp-only (spot_token=None, bridge_safe=False).
        4. Price-parity check: fetch allMids; compare spot mid vs perp mid.
           If |spot/perp - 1| > SPOT_PERP_PARITY_TOLERANCE or either price
           is missing/zero → treat as perp-only (fail-safe).

        Raises ``ValueError`` if the perp ticker does not exist in HL meta.
        """
        coin = coin.upper().strip()

        # ── Step 1: perp meta ────────────────────────────────────────────
        perp_specs = await self._client.perp_meta()
        sz_dec: int | None = None
        for spec in perp_specs:
            if spec.name == coin:
                sz_dec = spec.sz_decimals
                break
        if sz_dec is None:
            raise ValueError(
                f"Coin {coin!r} not found in HL perp meta (universe). "
                "Check the ticker spelling or verify the coin is listed on HL."
            )

        # ── Step 2: spot meta → find USDC pair ──────────────────────────
        spot_meta = await self._client.spot_meta()
        # spot_meta.tokens: {index: name}; spot_meta.pairs: list[HLSpotPair(index, name)]
        # We need USDC token index to filter pairs.
        usdc_idx: int | None = None
        for idx, name in spot_meta.tokens.items():
            if name == "USDC":
                usdc_idx = idx
                break

        # Build: base_token_name → (spot pair name, pair index), for USDC-quoted pairs only.
        # HLSpotPair.name is already resolved as "BASE/QUOTE" by client.spot_meta().
        # HLSpotPair.index is the @N index used in allMids to key the spot mid price.
        base_to_spot: dict[str, tuple[str, int]] = {}
        if usdc_idx is not None:
            for pair in spot_meta.pairs:
                if "/" not in pair.name:
                    continue
                base, quote = pair.name.split("/", 1)
                if quote == "USDC":
                    base_to_spot[base] = (pair.name, pair.index)

        # Identify the canonical spot base token for this coin.
        # Convention: base might be "U" + coin (e.g. UBTC for BTC) or identity (coin == base).
        spot_token: str | None = None
        spot_pair_index: int | None = None
        wrapped_candidate = f"U{coin}"          # e.g. BTC → UBTC
        if coin in base_to_spot:
            # Identity match (HYPE, PURR, ZEC, XPL, etc.)
            spot_token = coin
            _, spot_pair_index = base_to_spot[coin]
        elif wrapped_candidate in base_to_spot:
            # Wrapped name (BTC→UBTC, ETH→UETH, SOL→USOL)
            spot_token = wrapped_candidate
            _, spot_pair_index = base_to_spot[wrapped_candidate]

        # ── Step 3: bridge guard ─────────────────────────────────────────
        if spot_token is not None and spot_token in BRIDGE_TOKEN_BLACKLIST:
            logger.warning(
                "CoinDiscovery: resolved spot token %r for coin %r is in "
                "BRIDGE_TOKEN_BLACKLIST — setting spot_token=None, bridge_safe=False "
                "(perp-only; no spot leg).",
                spot_token,
                coin,
            )
            spot_token = None
            spot_pair_index = None

        # ── Step 4: price-parity guard ───────────────────────────────────
        # Only check parity when we have a candidate spot token (not already
        # downgraded by the bridge guard).  Fetch allMids once and look up:
        #   - perp mid: mids.get(coin)           — keyed by canonical ticker
        #   - spot mid: mids.get(f"@{index}")    — keyed by @N pair index
        #              or mids.get(f"{spot_token}/USDC") for symbolic-name pairs
        # Fail-safe: if either price is missing or zero → downgrade to perp-only.
        if spot_token is not None:
            spot_token, spot_pair_index = await self._check_price_parity(
                coin=coin,
                spot_token=spot_token,
                spot_pair_index=spot_pair_index,
            )

        bridge_safe = spot_token is not None

        return DiscoveredFacts(
            coin=coin,
            sz_decimals=sz_dec,
            spot_token=spot_token,
            bridge_safe=bridge_safe,
        )

    async def _check_price_parity(
        self,
        *,
        coin: str,
        spot_token: str,
        spot_pair_index: int | None,
    ) -> tuple[str | None, int | None]:
        """Verify spot mid is within SPOT_PERP_PARITY_TOLERANCE of the perp mid.

        Fetches allMids (one call).  Perp mid is keyed by the canonical coin
        ticker; spot mid is keyed by "@<pair_index>" or by the symbolic pair
        name "<spot_token>/USDC" (for early HL pairs that use symbolic keys).

        Returns (spot_token, spot_pair_index) unchanged if parity holds.
        Returns (None, None) if:
          - either price is absent or zero (fail-safe)
          - |spot_mid / perp_mid - 1| > SPOT_PERP_PARITY_TOLERANCE

        Logs a WARNING in the downgrade cases naming both prices.
        """
        try:
            mids = await self._client.all_mids()
        except Exception as exc:
            logger.warning(
                "CoinDiscovery: allMids call failed for coin %r / spot_token %r — "
                "failing safe: spot_token=None, bridge_safe=False. Error: %s",
                coin, spot_token, exc,
            )
            return None, None

        perp_px = mids.get(coin) or 0.0

        # Spot mid may be keyed by @<index> or by the symbolic pair name.
        spot_mid: float = 0.0
        if spot_pair_index is not None:
            spot_mid = mids.get(f"@{spot_pair_index}") or 0.0
        if not spot_mid:
            # Fallback: symbolic key (e.g. "PURR/USDC") used by some early HL pairs.
            spot_mid = mids.get(f"{spot_token}/USDC") or 0.0

        if not perp_px or not spot_mid:
            logger.warning(
                "CoinDiscovery: cannot verify price parity for coin %r / spot_token %r — "
                "perp_px=%s spot_mid=%s (missing/zero). Failing safe: spot_token=None, "
                "bridge_safe=False.",
                coin, spot_token, perp_px or "missing", spot_mid or "missing",
            )
            return None, None

        deviation = abs(spot_mid / perp_px - 1.0)
        if deviation > SPOT_PERP_PARITY_TOLERANCE:
            logger.warning(
                "CoinDiscovery: spot↔perp price parity FAILED for coin %r "
                "(spot_token=%r): spot_mid=%.6g, perp_px=%.6g, deviation=%.2f%% "
                "(tolerance %.0f%%). Downgrading to perp-only (spot_token=None, "
                "bridge_safe=False).",
                coin, spot_token, spot_mid, perp_px, deviation * 100,
                SPOT_PERP_PARITY_TOLERANCE * 100,
            )
            return None, None

        logger.debug(
            "CoinDiscovery: price parity OK for coin %r / spot_token %r: "
            "spot_mid=%.6g, perp_px=%.6g, deviation=%.3f%%",
            coin, spot_token, spot_mid, perp_px, deviation * 100,
        )
        return spot_token, spot_pair_index

    async def validate_and_register(
        self,
        coin: str,
        *,
        leverage: int,
        maint_ratio: float,
        position_size_usd: float | None = None,
        active: bool = False,
    ) -> CoinEntry:
        """Discover market facts, validate, and write the registry row.

        On any error from discovery or spot-pair validation, raises
        before writing.  No half-valid row is ever written.

        Newly added coins default ``active=False``; the operator enables
        them separately (Phase D).

        Returns the persisted CoinEntry (validated_at is set).
        """
        coin = coin.upper().strip()

        # ── Discovery ────────────────────────────────────────────────────
        facts = await self.discover(coin)
        logger.info(
            "CoinDiscovery.discover(%r): sz_decimals=%d, spot_token=%r, bridge_safe=%r",
            coin, facts.sz_decimals, facts.spot_token, facts.bridge_safe,
        )

        # ── Spot-pair validation (only when spot_token is set) ───────────
        # Use the HL /info endpoint via the existing validate_spot_pairs.
        # The client._post delegate satisfies the duck-typed market_data arg
        # that validate_spot_pairs expects (it only calls _post({"type": "spotMeta"})).
        if facts.spot_token is not None:
            spot_map = {coin: facts.spot_token}
            await validate_spot_pairs(self._client, (coin,), spot_token_map=spot_map)
            logger.info(
                "CoinDiscovery: spot-pair validation passed for %r → %r",
                coin, facts.spot_token,
            )

        # ── Write to registry ────────────────────────────────────────────
        validated_at = int(time.time() * 1000)
        entry = await self._repo.upsert(
            coin,
            leverage=leverage,
            maint_ratio=maint_ratio,
            position_size_usd=position_size_usd,
            active=active,
            spot_token=facts.spot_token,
            sz_decimals=facts.sz_decimals,
            bridge_safe=facts.bridge_safe,
            validated_at=validated_at,
        )
        logger.info(
            "CoinDiscovery: registered coin=%r leverage=%d maint_ratio=%s "
            "spot_token=%r bridge_safe=%r validated_at=%d",
            coin, leverage, maint_ratio, facts.spot_token, facts.bridge_safe, validated_at,
        )
        return entry
