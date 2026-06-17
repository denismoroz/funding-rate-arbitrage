"""HLSymbols: HL coin/symbol normalization, perp sz_decimals cache, spot pair cache, qty rounding.

Lives between HLClient (transport) and the business actions. Owns the cross-cutting
caches that any action touching coins needs: perp meta szDecimals and spot pair-index → symbolic name.
"""
from __future__ import annotations

import logging
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from frab.exchanges.hyperliquid.client import HLClient

logger = logging.getLogger(__name__)
from frab.exchanges.hyperliquid.tokens import BRIDGE_TOKEN_BLACKLIST


class HLSymbols:
    def __init__(
        self,
        *,
        client: HLClient,
        spot_token_map: dict[str, str] | None = None,
        spot_token_inverse: dict[str, str] | None = None,
        spot_quote_token: str = "USDC",
    ) -> None:
        self._client = client
        self._spot_token_map: dict[str, str] = spot_token_map if spot_token_map is not None else {}
        # Registry-derived inverse map (HL wrapped token → canonical perp coin).
        # Empty dict when no registry is wired (e.g. test without spot legs).
        self._spot_token_inverse_dict: dict[str, str] = (
            spot_token_inverse if spot_token_inverse is not None else {}
        )
        self._spot_quote_token = spot_quote_token
        self._sz_decimals_cache: dict[str, int] | None = None
        self._spot_idx_to_name: dict[int, str] | None = None

    # --- Synchronous (no I/O) ---

    def make_spot_name(self, coin: str) -> str:
        """Canonical coin → HL spot pair symbol. E.g. 'BTC' → 'UBTC/USDC'."""
        base = self._spot_token_map.get(coin, coin)
        return f"{base}/{self._spot_quote_token}"

    def normalize_spot_coin(self, hl_coin: str) -> str:
        """HL wrapped/quote-token name → canonical coin. E.g. 'UBTC' → 'BTC'.

        Uses the inverse of spot_token_map (user config). Unknown names pass through.
        """
        inverse = {v: k for k, v in self._spot_token_map.items()}
        return inverse.get(hl_coin, hl_coin)

    @property
    def spot_token_inverse(self) -> dict[str, str]:
        """The registry-derived HL wrapped-token → canonical-coin map (read-only)."""
        return self._spot_token_inverse_dict

    @property
    def spot_token_map(self) -> dict[str, str]:
        """User-config canonical → wrapped map (read-only access)."""
        return self._spot_token_map

    @property
    def spot_quote_token(self) -> str:
        return self._spot_quote_token

    # --- Async (cached fetches) ---

    async def sz_decimals(self, coin: str) -> int:
        """Perp asset szDecimals (e.g. BTC=5). Caches perp meta on first call.

        Raises ValueError if coin not in perp universe.
        """
        if self._sz_decimals_cache is None:
            specs = await self._client.perp_meta()
            self._sz_decimals_cache = {s.name: s.sz_decimals for s in specs}
        sz_dec = self._sz_decimals_cache.get(coin)
        if sz_dec is None:
            raise ValueError(f"unknown coin {coin!r} (not in perp meta)")
        return sz_dec

    async def round_qty(self, coin: str, qty: float) -> float:
        """Floor qty to asset's szDecimals (conservative for initial sizing)."""
        sz_dec = await self.sz_decimals(coin)
        quant = Decimal(10) ** -sz_dec
        return float(Decimal(str(qty)).quantize(quant, rounding=ROUND_DOWN))

    async def round_qty_to_nearest(self, coin: str, qty: float) -> float:
        """Round qty to asset's szDecimals with ROUND_HALF_UP."""
        sz_dec = await self.sz_decimals(coin)
        quant = Decimal(10) ** -sz_dec
        return float(Decimal(str(qty)).quantize(quant, rounding=ROUND_HALF_UP))

    async def spot_mids_by_coin(self) -> dict[str, float]:
        """Return {canonical_coin: spot_mid_USDC} for spot pairs we support."""
        try:
            mids = await self._client.all_mids()
        except Exception as exc:
            logger.warning("get_spot_mids_by_coin: allMids failed: %s", exc)
            return {}
        out: dict[str, float] = {}
        for key, val in mids.items():
            # HL exposes most spot pairs under @<index>, but some early pairs
            # (notably PURR/USDC at @0) are keyed by their symbolic name instead.
            name: str | None = None
            if key.startswith("@"):
                try:
                    idx = int(key[1:])
                except ValueError:
                    continue
                name = await self.resolve_spot_pair(idx)
            elif "/" in key:
                name = key
            if not name or "/" not in name:
                continue
            wrapped, quote = name.split("/", 1)
            if quote != self.spot_quote_token:
                continue
            canonical = self._spot_token_inverse_dict.get(wrapped)
            if canonical is None:
                continue
            out[canonical] = val
        return out

    async def resolve_spot_pair(self, idx: int) -> str | None:
        """Map HL spot pair index (the @N) → symbolic name 'UBTC/USDC'.

        Returns None if the index is unknown or the resolved name lacks a slash.
        Caches the spot_meta universe on first call.
        """
        if self._spot_idx_to_name is None:
            await self._load_spot_idx_map()
        return (self._spot_idx_to_name or {}).get(idx)

    async def normalize_hl_coin(self, hl_coin: str) -> tuple[str, str]:
        """HL coin field → (canonical_coin, leg) where leg ∈ {'spot', 'perp'}.

        Inputs accepted:
          - '@142' (spot pair index): resolves to canonical coin + 'spot'.
          - 'UBTC/USDC' (spot pair symbol): canonical coin + 'spot'.
          - 'BTC' (perp ticker): pass through as ('BTC', 'perp').

        Raises ValueError if the resolved wrapped token is in BRIDGE_TOKEN_BLACKLIST
        (e.g. AVAX0/USDC — independent price discovery, not safe to map).

        Unknown @N indexes or non-numeric @-strings fall back to ('input', 'perp').
        """
        if hl_coin.startswith("@"):
            try:
                idx = int(hl_coin[1:])
            except ValueError:
                return hl_coin, "perp"
            name = await self.resolve_spot_pair(idx)
            if name and "/" in name:
                wrapped = name.split("/")[0]
                if wrapped in BRIDGE_TOKEN_BLACKLIST:
                    raise ValueError(
                        f"HL spot token {wrapped!r} is in BRIDGE_TOKEN_BLACKLIST "
                        f"(independent price discovery — not safe to map to perp coin)"
                    )
                return self._spot_token_inverse_dict.get(wrapped, wrapped), "spot"
            return hl_coin, "perp"
        if "/" in hl_coin:
            wrapped = hl_coin.split("/")[0]
            if wrapped in BRIDGE_TOKEN_BLACKLIST:
                raise ValueError(
                    f"HL spot token {wrapped!r} is in BRIDGE_TOKEN_BLACKLIST "
                    f"(independent price discovery — not safe to map to perp coin)"
                )
            coin = self._spot_token_inverse_dict.get(wrapped, wrapped)
            return coin, "spot"
        return hl_coin, "perp"

    # --- Internal ---

    async def _load_spot_idx_map(self) -> None:
        spot_meta = await self._client.spot_meta()
        mapping: dict[int, str] = {}
        for pair in spot_meta.pairs:
            if "/" in pair.name:
                mapping[pair.index] = pair.name
        self._spot_idx_to_name = mapping
