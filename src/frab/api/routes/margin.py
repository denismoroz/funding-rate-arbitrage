"""GET /api/equity/margin — stubbed in Step 1 (engine not wired)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from frab.api.deps import get_session

router = APIRouter()


@router.get("/margin")
async def get_margin_status(
    strategy_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Margin watchdog state — engine not configured."""
    raise HTTPException(status_code=503, detail="Engine not configured")
