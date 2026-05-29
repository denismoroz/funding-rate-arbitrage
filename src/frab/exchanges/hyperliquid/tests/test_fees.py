"""Unit tests for fetch_real_fee_usdc in actions._fees."""
from __future__ import annotations

import asyncio
from datetime import datetime, UTC

import pytest

from frab.exchanges.hyperliquid.actions._fees import fetch_real_fee_usdc
from frab.exchanges.hyperliquid.client import HLClient
from frab.exchanges.hyperliquid.wire import HLUserFill


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fill(oid: int, fee_token: str = "USDC", fee_raw: float = 0.05, px: float = 50000.0) -> HLUserFill:
    return HLUserFill(
        oid=oid,
        side="B",
        sz=0.001,
        px=px,
        ts_ms=1_000_000,
        fee_raw=fee_raw,
        fee_token=fee_token,
        coin="BTC",
    )


FIXED_CLOCK = datetime(2026, 5, 29, 16, 0, tzinfo=UTC)
FIXED_END_MS = int(FIXED_CLOCK.timestamp() * 1000) + 60_000


@pytest.fixture
def client(mocker):
    return mocker.AsyncMock(spec=HLClient)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_returns_fee_raw_for_usdc_token(client):
    fill = _make_fill(oid=1, fee_token="USDC", fee_raw=0.05)
    client.user_fills_by_time.return_value = [fill]

    result = await fetch_real_fee_usdc(
        client=client, address="0xabc", oid=1, since_ms=0,
        clock_fn=lambda: FIXED_CLOCK, attempts=1,
    )

    assert result == 0.05


async def test_converts_wrapped_token_fee_to_usdc(client):
    fill = _make_fill(oid=2, fee_token="UBTC", fee_raw=0.001, px=50000.0)
    client.user_fills_by_time.return_value = [fill]

    result = await fetch_real_fee_usdc(
        client=client, address="0xabc", oid=2, since_ms=0,
        clock_fn=lambda: FIXED_CLOCK, attempts=1,
    )

    assert result == pytest.approx(50.0)


async def test_unknown_fee_token_logs_and_returns_raw(client, caplog):
    fill = _make_fill(oid=3, fee_token="XYZ", fee_raw=0.07, px=100.0)
    client.user_fills_by_time.return_value = [fill]

    import logging
    with caplog.at_level(logging.WARNING):
        result = await fetch_real_fee_usdc(
            client=client, address="0xabc", oid=3, since_ms=0,
            clock_fn=lambda: FIXED_CLOCK, attempts=1,
        )

    assert result == 0.07
    assert "unknown feeToken" in caplog.text


async def test_oid_mismatch_skips_fill(client):
    fill = _make_fill(oid=99, fee_token="USDC", fee_raw=0.05)
    client.user_fills_by_time.return_value = [fill]

    result = await fetch_real_fee_usdc(
        client=client, address="0xabc", oid=42, since_ms=0,
        clock_fn=lambda: FIXED_CLOCK, attempts=1,
    )

    assert result is None


async def test_retries_on_empty_fills(client, mocker):
    mocker.patch("asyncio.sleep", new_callable=mocker.AsyncMock)
    fill = _make_fill(oid=5, fee_token="USDC", fee_raw=0.03)
    client.user_fills_by_time.side_effect = [[], [fill]]

    result = await fetch_real_fee_usdc(
        client=client, address="0xabc", oid=5, since_ms=0,
        clock_fn=lambda: FIXED_CLOCK, attempts=2,
    )

    assert result == 0.03
    assert client.user_fills_by_time.call_count == 2


async def test_returns_none_after_all_attempts_exhausted(client, mocker):
    mocker.patch("asyncio.sleep", new_callable=mocker.AsyncMock)
    client.user_fills_by_time.return_value = []

    result = await fetch_real_fee_usdc(
        client=client, address="0xabc", oid=7, since_ms=0,
        clock_fn=lambda: FIXED_CLOCK, attempts=3,
    )

    assert result is None
    assert client.user_fills_by_time.call_count == 3


async def test_handles_user_fills_by_time_exception(client, mocker):
    mocker.patch("asyncio.sleep", new_callable=mocker.AsyncMock)
    fill = _make_fill(oid=8, fee_token="USDC", fee_raw=0.02)
    client.user_fills_by_time.side_effect = [RuntimeError("network error"), [fill]]

    result = await fetch_real_fee_usdc(
        client=client, address="0xabc", oid=8, since_ms=0,
        clock_fn=lambda: FIXED_CLOCK, attempts=2,
    )

    assert result == 0.02


async def test_uses_clock_fn_for_end_ms(client, mocker):
    fill = _make_fill(oid=9, fee_token="USDC", fee_raw=0.01)
    client.user_fills_by_time.return_value = [fill]

    await fetch_real_fee_usdc(
        client=client, address="0xabc", oid=9, since_ms=0,
        clock_fn=lambda: FIXED_CLOCK, attempts=1,
    )

    _addr, since, end = client.user_fills_by_time.call_args.args
    assert end == FIXED_END_MS
