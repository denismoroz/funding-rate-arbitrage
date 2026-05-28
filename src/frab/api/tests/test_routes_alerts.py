"""Alerts route tests — stubbed in Step 3.

The alerts query used Position.strategy_id/market_id and Event.ts (datetime),
both gone in the new schema. The /api/alerts endpoint is now a stub that
returns [].
"""


async def test_alerts_returns_empty_list(api_client):
    resp = await api_client.get("/api/alerts?strategy_id=1")
    assert resp.status_code == 200
    assert resp.json() == []
