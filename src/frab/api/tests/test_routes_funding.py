"""Tests for GET /api/funding/{coin} — including default exchange resolution."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from frab.db.models import Exchange, FundingRate
from frab.db.session import session_scope


def _ms(offset_hours: int = 0) -> int:
    base = int(datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
    return base + offset_hours * 3600 * 1000


# ── test_funding_defaults_to_hyperliquid_exchange ────────────────────────────


async def test_funding_defaults_to_hyperliquid_exchange(api_client, session_factory):
    """Seed HL exchange + funding rates; request without exchange_id → get the rates."""
    async with session_scope(session_factory) as s:
        exc = Exchange(
            name="hyperliquid",
            funding_interval_h=1,
            spot_taker_bps=2.5,
            perp_taker_bps=2.5,
        )
        s.add(exc)
        await s.flush()
        exc_id = exc.id

        for i in range(3):
            s.add(FundingRate(
                exchange_id=exc_id,
                coin="SOL",
                ts_ms=_ms(i),
                rate=0.001 * (i + 1),
                premium=None,
                annualized_pct=8.76 * (i + 1),
            ))

    # No exchange_id → should default to "hyperliquid"
    resp = await api_client.get("/api/funding/SOL")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    for row in data:
        assert row["coin"] == "SOL"
        assert row["exchange_id"] == exc_id


async def test_funding_returns_empty_when_no_exchange_row(api_client, session_factory):
    """No exchange row named 'hyperliquid' and no exchange_id → empty list."""
    resp = await api_client.get("/api/funding/BTC")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_funding_explicit_exchange_id_still_works(api_client, session_factory):
    """Explicit exchange_id bypasses the default resolution."""
    async with session_scope(session_factory) as s:
        exc = Exchange(
            name="binance",
            funding_interval_h=8,
            spot_taker_bps=1.0,
            perp_taker_bps=1.0,
        )
        s.add(exc)
        await s.flush()
        exc_id = exc.id
        s.add(FundingRate(
            exchange_id=exc_id,
            coin="ETH",
            ts_ms=_ms(0),
            rate=0.002,
            premium=None,
            annualized_pct=5.0,
        ))

    resp = await api_client.get(f"/api/funding/ETH?exchange_id={exc_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["exchange_id"] == exc_id
