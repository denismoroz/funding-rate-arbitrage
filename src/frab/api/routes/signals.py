"""Signals routes — stubbed in Step 3 (Signal table removed from new schema)."""
from fastapi import APIRouter

router = APIRouter()


@router.get("")
async def list_signals() -> list:
    return []
