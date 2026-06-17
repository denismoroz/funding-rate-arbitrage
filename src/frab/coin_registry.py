"""CoinRegistry service — single async load, sync accessors.

Loads all coin_registry rows from the DB once at engine startup into an
immutable in-memory snapshot, then exposes sync accessors that every deep
call-site can use without needing to await.

Usage (server.py lifespan):

    registry = CoinRegistry(session_factory)
    await registry.load()
    # then pass registry / derived dicts to components
    registry_settings = RegistryAwareSettings(settings, registry)
    # use registry_settings wherever settings.get_coin_spec() was called

Design notes:
- ``load()`` is async (DB hit); all accessors are sync (snapshot lookup).
- ``reload()`` re-runs the same DB query and atomically replaces the snapshot.
  Phase D's settings API calls this after any mutation.
- No fallback for unknown coins: callers that need a CoinMarginSpec for a
  coin not in the registry must handle KeyError / raise themselves.
  FALLBACK_LEVERAGE is deliberately NOT consulted (per plan decision 3).
- ``RegistryAwareSettings`` is a thin adapter that wraps a ``Settings`` object
  and overrides ``get_coin_spec`` to delegate to the registry.  All other
  attribute accesses fall through to the underlying Settings so no other
  behaviour changes.  Phase F replaces this with the single-source cleanup.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.constants import CoinMarginSpec
from frab.db.models import CoinRegistry as CoinRegistryRow
from frab.exchanges.hyperliquid.tokens import BRIDGE_TOKEN_BLACKLIST

if TYPE_CHECKING:
    from frab.settings import Settings

logger = logging.getLogger(__name__)


class CoinRegistry:
    """In-memory snapshot of the coin_registry table with sync accessors.

    Call ``await load()`` exactly once before using the accessors.
    Subsequent calls to ``await reload()`` swap the snapshot atomically.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory
        # Snapshot storage — populated by load() / reload()
        self._rows: list[CoinRegistryRow] | None = None
        # Derived caches — rebuilt on every load
        self._universe: tuple[str, ...] = ()
        self._spot_token_map: dict[str, str] = {}
        self._spot_token_inverse: dict[str, str] = {}
        self._specs: dict[str, CoinMarginSpec] = {}
        self._sz_decimals: dict[str, int] = {}
        self._bridge_safe: dict[str, bool] = {}

    # ── Async load / reload ───────────────────────────────────────────────────

    async def load(self) -> None:
        """Load snapshot from DB. Must be called once before any accessor."""
        await self._fetch_and_build()
        logger.info(
            "CoinRegistry loaded: %d rows, universe=%s",
            len(self._specs),
            self._universe,
        )

    async def reload(self) -> None:
        """Reload snapshot from DB (Phase D: called after any mutation)."""
        await self._fetch_and_build()
        logger.info("CoinRegistry reloaded: universe=%s", self._universe)

    async def _fetch_and_build(self) -> None:
        async with self._sf() as session:
            result = await session.execute(
                select(CoinRegistryRow).order_by(CoinRegistryRow.coin)
            )
            rows = list(result.scalars().all())

        # Rebuild derived caches from fresh rows
        specs: dict[str, CoinMarginSpec] = {}
        spot_token_map: dict[str, str] = {}
        sz_decimals: dict[str, int] = {}
        bridge_safe_map: dict[str, bool] = {}
        universe_coins: list[str] = []

        for row in rows:
            specs[row.coin] = CoinMarginSpec(
                leverage=row.leverage,
                maint_ratio=row.maint_ratio,
            )
            if row.spot_token is not None:
                spot_token_map[row.coin] = row.spot_token
            if row.sz_decimals is not None:
                sz_decimals[row.coin] = row.sz_decimals
            bridge_safe_map[row.coin] = row.bridge_safe
            if row.active and row.validated_at is not None:
                universe_coins.append(row.coin)

        # Build spot_token_inverse from spot_token_map, excluding bridge-blacklisted names
        spot_token_inverse: dict[str, str] = {
            spot_tok: coin
            for coin, spot_tok in spot_token_map.items()
            if spot_tok not in BRIDGE_TOKEN_BLACKLIST
        }

        # Atomically swap all caches (no lock needed — single async event loop)
        self._specs = specs
        self._spot_token_map = spot_token_map
        self._spot_token_inverse = spot_token_inverse
        self._sz_decimals = sz_decimals
        self._bridge_safe = bridge_safe_map
        self._universe = tuple(sorted(universe_coins))

    # ── Sync accessors ────────────────────────────────────────────────────────

    def _require_loaded(self) -> None:
        if not self._specs and self._universe == ():
            # allow empty registry (e.g. test with no rows); only block if
            # load() was never called at all (specs dict and universe both empty
            # but we can't distinguish "never loaded" from "loaded empty DB").
            # We intentionally don't raise here — callers get back empty results
            # which is the correct answer for an empty registry.
            pass

    def get_coin_spec(self, coin: str) -> CoinMarginSpec:
        """Return CoinMarginSpec for the given coin.

        Raises KeyError if the coin is not in the registry.
        No fallback — per design decision 3 (no FALLBACK_LEVERAGE).
        """
        spec = self._specs.get(coin)
        if spec is None:
            raise KeyError(
                f"Coin {coin!r} is not in the coin_registry. "
                "Add it via the settings API (Phase D)."
            )
        return spec

    def universe(self) -> tuple[str, ...]:
        """Active AND validated coins, sorted deterministically."""
        return self._universe

    def spot_token_map(self) -> dict[str, str]:
        """Canonical coin → HL spot token, for rows where spot_token is set."""
        return dict(self._spot_token_map)

    def spot_token_inverse(self) -> dict[str, str]:
        """HL spot token → canonical coin (inverse of spot_token_map).

        Bridge-blacklisted names are excluded (they have independent price
        discovery and must never be mapped to the canonical perp coin).
        Computed once at load time — map and inverse can never desync.
        """
        return dict(self._spot_token_inverse)

    def bridge_safe(self, coin: str) -> bool:
        """Return True if the coin's spot token is 1:1 with its perp (bridge-safe).

        Returns False for unknown coins.
        """
        return self._bridge_safe.get(coin, False)

    def sz_decimals(self, coin: str) -> int | None:
        """Return sz_decimals for the given coin, or None if not set."""
        return self._sz_decimals.get(coin)


class RegistryAwareSettings:
    """Thin adapter: wraps a Settings object and overrides get_coin_spec to use the registry.

    Every attribute read other than ``get_coin_spec`` is delegated to the
    underlying ``Settings`` object, so the adapter is a transparent replacement
    in every call-site that receives a settings argument.

    Phase F will replace this with the final single-source cleanup once
    constants.py is deleted.
    """

    def __init__(self, settings: "Settings", registry: CoinRegistry) -> None:
        # Stored under mangled names to avoid conflicting with passthrough
        object.__setattr__(self, "_settings", settings)
        object.__setattr__(self, "_registry", registry)

    # Override get_coin_spec to delegate to registry (primary source, no fallback)
    def get_coin_spec(self, coin: str) -> CoinMarginSpec:
        return object.__getattribute__(self, "_registry").get_coin_spec(coin)

    # Transparent passthrough for everything else
    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_settings"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_settings"), name, value)
