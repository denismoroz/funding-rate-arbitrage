"""Alerts route — stubbed in Step 3 (new schema; alerts query rewrite in Step 5)."""
from fastapi import APIRouter

router = APIRouter()


@router.get("")
async def list_alerts(strategy_id: int) -> list:
    return []
