"""CoinRegistryRepo — thin DAO for coin_registry rows.

No business logic. Only persistence and guard helpers.
Phase-A: data layer only (no HL calls, no universe-derivation service).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.db.models import CoinRegistry as CoinRegistryRow
from frab.db.models import FarbPosition as FarbPositionRow
from frab.db.session import session_scope
from frab.domain.enums import FarbState


# ── Domain object ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CoinEntry:
    """Immutable snapshot of a coin_registry row."""
    coin: str
    leverage: int
    maint_ratio: float
    position_size_usd: float | None
    active: bool
    spot_token: str | None
    sz_decimals: int | None
    bridge_safe: bool
    validated_at: int | None  # epoch ms; None = not validated


# ── Terminal FarbState values (open = not in terminal set) ────────────────────

_TERMINAL_FARB_STATES: frozenset[str] = frozenset({
    FarbState.CLOSED.value,
    FarbState.FAILED.value,
})


# ── ORM → domain mapper ───────────────────────────────────────────────────────

def _to_domain(row: CoinRegistryRow) -> CoinEntry:
    return CoinEntry(
        coin=row.coin,
        leverage=row.leverage,
        maint_ratio=row.maint_ratio,
        position_size_usd=row.position_size_usd,
        active=row.active,
        spot_token=row.spot_token,
        sz_decimals=row.sz_decimals,
        bridge_safe=row.bridge_safe,
        validated_at=row.validated_at,
    )


# ── CoinRegistryRepo ──────────────────────────────────────────────────────────

class CoinRegistryRepo:
    """Data-access object for coin_registry rows.

    Each public method opens its own session, commits, and closes it.
    No long-lived sessions are held.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # ── CRUD ──────────────────────────────────────────────────────────────────

    async def list(self) -> list[CoinEntry]:
        """Return all rows ordered by coin."""
        async with session_scope(self._sf) as session:
            result = await session.execute(
                select(CoinRegistryRow).order_by(CoinRegistryRow.coin)
            )
            rows = result.scalars().all()
            return [_to_domain(r) for r in rows]

    async def get(self, coin: str) -> CoinEntry | None:
        """Return domain CoinEntry for the given coin, or None if not found."""
        async with session_scope(self._sf) as session:
            row = await session.get(CoinRegistryRow, coin)
            if row is None:
                return None
            return _to_domain(row)

    async def upsert(
        self,
        coin: str,
        *,
        leverage: int,
        maint_ratio: float,
        position_size_usd: float | None = None,
        active: bool,
        spot_token: str | None = None,
        sz_decimals: int | None = None,
        bridge_safe: bool,
        validated_at: int | None = None,
    ) -> CoinEntry:
        """Insert or fully replace a coin_registry row.

        If a row with `coin` already exists it is replaced wholesale.
        Returns the resulting domain CoinEntry.
        """
        async with session_scope(self._sf) as session:
            row = await session.get(CoinRegistryRow, coin)
            if row is None:
                row = CoinRegistryRow(coin=coin)
                session.add(row)
            row.leverage = leverage
            row.maint_ratio = maint_ratio
            row.position_size_usd = position_size_usd
            row.active = active
            row.spot_token = spot_token
            row.sz_decimals = sz_decimals
            row.bridge_safe = bridge_safe
            row.validated_at = validated_at
            await session.flush()
            return _to_domain(row)

    async def set_active(self, coin: str, active: bool) -> CoinEntry:
        """Toggle the active flag for a coin.

        Raises KeyError if the coin is not in the registry.
        """
        async with session_scope(self._sf) as session:
            row = await session.get(CoinRegistryRow, coin)
            if row is None:
                raise KeyError(f"Coin {coin!r} not found in registry")
            row.active = active
            await session.flush()
            return _to_domain(row)

    async def delete(self, coin: str) -> None:
        """Remove a coin from the registry.

        Raises KeyError if the coin is not present.
        Callers should check has_open_position before calling this
        (enforcement is the caller's responsibility in Phase A;
        Phase D adds the API-level guard).
        """
        async with session_scope(self._sf) as session:
            row = await session.get(CoinRegistryRow, coin)
            if row is None:
                raise KeyError(f"Coin {coin!r} not found in registry")
            await session.delete(row)

    # ── Guard helpers ─────────────────────────────────────────────────────────

    async def has_open_position(self, coin: str) -> bool:
        """Return True if any non-terminal FarbPosition exists for this coin.

        A position is considered open when its state is NOT in {CLOSED, FAILED}.
        This covers all transient (opening/closing) and resting
        (PRE_BREAKEVEN, POST_BREAKEVEN) states.

        Used by Phase-D API guards: delete and leverage/maint_ratio edits
        must be blocked while an open position exists on the coin.
        """
        async with session_scope(self._sf) as session:
            result = await session.execute(
                select(FarbPositionRow.id).where(
                    FarbPositionRow.coin == coin,
                    FarbPositionRow.state.not_in(list(_TERMINAL_FARB_STATES)),
                ).limit(1)
            )
            return result.scalar_one_or_none() is not None
