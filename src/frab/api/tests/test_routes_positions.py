"""Position route tests — stubbed in Step 3.

The old tests used Position with spot_units/perp_units/market_id/strategy_id
which are gone in the new schema. The /api/positions endpoint is now a 503
stub until Step 5 (FarbRepo) rewrites it.
"""

async def test_positions_endpoint_is_stubbed(api_client):
    resp = await api_client.get("/api/positions")
    assert resp.status_code == 503


async def test_position_funding_history_is_stubbed(api_client):
    resp = await api_client.get("/api/positions/1/funding-history")
    assert resp.status_code == 503
