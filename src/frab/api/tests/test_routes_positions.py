"""Position route tests — unfrozen in Step 8 (reads from DB)."""


async def test_positions_endpoint_returns_empty_list(api_client):
    resp = await api_client.get("/api/positions")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_position_funding_history_returns_empty_list(api_client):
    resp = await api_client.get("/api/positions/1/funding-history")
    assert resp.status_code == 200
    assert resp.json() == []
