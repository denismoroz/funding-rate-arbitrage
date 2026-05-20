from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from frab.api.deps import get_session
from frab.api.schemas import EquityOut, WalletSnapshotOut
from frab.db.models import EquitySnapshot, WalletSnapshot

router = APIRouter()


@router.get("", response_model=list[EquityOut])
async def list_equity(
    strategy_id: int,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 1000,
    session: AsyncSession = Depends(get_session),
) -> list[EquityOut]:
    stmt = select(EquitySnapshot).where(EquitySnapshot.strategy_id == strategy_id)
    if since is not None:
        stmt = stmt.where(EquitySnapshot.ts >= since)
    if until is not None:
        stmt = stmt.where(EquitySnapshot.ts <= until)
    stmt = stmt.order_by(EquitySnapshot.ts.asc()).limit(limit)
    result = await session.execute(stmt)
    snapshots = result.scalars().all()
    return [EquityOut.model_validate(s) for s in snapshots]


@router.get("/wallet-history", response_model=list[WalletSnapshotOut])
async def list_wallet_history(
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 2000,
    session: AsyncSession = Depends(get_session),
) -> list[WalletSnapshotOut]:
    stmt = select(WalletSnapshot)
    if since is not None:
        stmt = stmt.where(WalletSnapshot.ts >= since)
    if until is not None:
        stmt = stmt.where(WalletSnapshot.ts <= until)
    stmt = stmt.order_by(WalletSnapshot.ts.asc()).limit(limit)
    result = await session.execute(stmt)
    snapshots = result.scalars().all()
    return [WalletSnapshotOut.model_validate(s) for s in snapshots]
