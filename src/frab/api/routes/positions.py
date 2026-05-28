"""Position routes — stubbed in Step 3 (new schema; FarbRepo in Step 5)."""
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("")
async def list_positions() -> list:
    raise HTTPException(status_code=503, detail="Engine not configured")


@router.get("/{position_id}/funding-history")
async def get_position_funding_history(position_id: int) -> list:
    raise HTTPException(status_code=503, detail="Engine not configured")
