"""Fixtures for XSMOM evaluator tests — reuses the in-memory SQLite engine from
frab/conftest.py and adds a Strategy row fixture."""
import pytest_asyncio

from frab.db.models import Strategy
from frab.db.session import session_scope


@pytest_asyncio.fixture
async def strategy_id(session_factory) -> int:
    """Insert a Strategy row (params_json empty) and return its id."""
    async with session_scope(session_factory) as s:
        strat = Strategy(name="xsmom_test", version="v1", params_json={})
        s.add(strat)
        await s.flush()
        sid = strat.id
    return sid
