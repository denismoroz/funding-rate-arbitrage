"""Tests for AtomicExecutor."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from frab.events.bus import EventBus
from frab.exchanges.atomic import (
    AtomicExecutor,
    PairedCloseResult,
    PairedOpenResult,
    default_is_transient,
)
from frab.exchanges.base import FillReport, Leg, OrderRequest, Side

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXED_DT = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
_CLOCK = lambda: _FIXED_DT  # noqa: E731
_COIN = "BTC"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fill(
    coin: str = _COIN,
    leg: Leg = Leg.PERP,
    side: Side = Side.SELL,
    client_ref: str | None = None,
) -> FillReport:
    return FillReport(
        coin=coin,
        leg=leg,
        side=side,
        ts=_FIXED_DT,
        qty=1.0,
        price=50_000.0,
        fee=0.5,
        slippage_bps=2.0,
        is_paper=True,
        client_ref=client_ref,
    )


def _perp_open_req(coin: str = _COIN, client_ref: str | None = "perp-ref") -> OrderRequest:
    """PERP SELL — open short."""
    return OrderRequest(coin=coin, leg=Leg.PERP, side=Side.SELL, qty=1.0, client_ref=client_ref)


def _spot_open_req(coin: str = _COIN, client_ref: str | None = "spot-ref") -> OrderRequest:
    """SPOT BUY — buy spot."""
    return OrderRequest(coin=coin, leg=Leg.SPOT, side=Side.BUY, qty=1.0, client_ref=client_ref)


def _perp_close_req(coin: str = _COIN, client_ref: str | None = "perp-close-ref") -> OrderRequest:
    """PERP BUY — cover short."""
    return OrderRequest(coin=coin, leg=Leg.PERP, side=Side.BUY, qty=1.0, client_ref=client_ref)


def _spot_close_req(coin: str = _COIN, client_ref: str | None = "spot-close-ref") -> OrderRequest:
    """SPOT SELL — sell spot."""
    return OrderRequest(coin=coin, leg=Leg.SPOT, side=Side.SELL, qty=1.0, client_ref=client_ref)


def _sleep_recorder() -> tuple[list[float], Any]:
    """Return (calls_list, async_sleep_fn)."""
    calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    return calls, fake_sleep


async def _drain_events(bus: EventBus) -> list[Any]:
    """Subscribe and collect any events already published (synchronous put_nowait)."""
    # We can't drain retroactively — events must be captured via subscribe BEFORE publish.
    # This helper is used for post-hoc draining when we hold a queue reference.
    raise RuntimeError("Use 'async with bus.subscribe() as q:' before the operation")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


# ---------------------------------------------------------------------------
# submit_with_retry tests
# ---------------------------------------------------------------------------


async def test_submit_succeeds_first_try(bus: EventBus) -> None:
    """1. underlying returns FillReport on attempt 1."""
    fill = _make_fill()
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    underlying.submit = AsyncMock(return_value=fill)
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK, sleep=fake_sleep)

    async with bus.subscribe() as q:
        result = await ex.submit_with_retry(_perp_open_req())

    assert result is fill
    assert sleep_calls == []
    assert q.empty()


async def test_submit_retries_on_transient_then_succeeds(bus: EventBus) -> None:
    """2. TimeoutError once, then fill. One sleep of 2.0s recorded, no event published."""
    fill = _make_fill()
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    underlying.submit = AsyncMock(side_effect=[asyncio.TimeoutError(), fill])
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK, sleep=fake_sleep)

    async with bus.subscribe() as q:
        result = await ex.submit_with_retry(_perp_open_req())

    assert result is fill
    assert sleep_calls == [2.0]
    assert q.empty()


async def test_submit_exhausts_retries_publishes_event_and_raises(bus: EventBus) -> None:
    """3. TimeoutError 3 times → raises, publishes retry_exhausted event."""
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    underlying.submit = AsyncMock(
        side_effect=[asyncio.TimeoutError(), asyncio.TimeoutError(), asyncio.TimeoutError()]
    )
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK, sleep=fake_sleep)

    req = _perp_open_req(client_ref="test-ref")

    async with bus.subscribe() as q:
        with pytest.raises(asyncio.TimeoutError):
            await ex.submit_with_retry(req)

        events = []
        while not q.empty():
            events.append(q.get_nowait())

    assert sleep_calls == [2.0, 5.0]
    assert len(events) == 1
    evt = events[0]
    assert evt.kind == "retry_exhausted"
    assert evt.level == "ERROR"
    assert evt.source == "atomic_executor"
    payload = evt.payload_json
    assert payload["coin"] == _COIN
    assert payload["leg"] == Leg.PERP.value
    assert payload["side"] == Side.SELL.value
    assert payload["client_ref"] == "test-ref"
    assert payload["attempts"] == 3
    assert len(payload["errors"]) == 3


async def test_submit_non_transient_no_retry(bus: EventBus) -> None:
    """4. ValueError on attempt 1 → raises immediately, no sleep, no event."""
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    underlying.submit = AsyncMock(side_effect=[ValueError("bad qty")])
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK, sleep=fake_sleep)

    async with bus.subscribe() as q:
        with pytest.raises(ValueError, match="bad qty"):
            await ex.submit_with_retry(_perp_open_req())

    assert sleep_calls == []
    assert q.empty()


async def test_submit_transient_then_non_transient(bus: EventBus) -> None:
    """5. TimeoutError on attempt 1, ValueError on attempt 2 → raises ValueError. One sleep, no event."""
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    underlying.submit = AsyncMock(
        side_effect=[asyncio.TimeoutError(), ValueError("bad")]
    )
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK, sleep=fake_sleep)

    async with bus.subscribe() as q:
        with pytest.raises(ValueError, match="bad"):
            await ex.submit_with_retry(_perp_open_req())

    assert sleep_calls == [2.0]
    assert q.empty()


def test_default_is_transient_classifier() -> None:
    """6. Verify default_is_transient on known types."""
    assert default_is_transient(asyncio.TimeoutError()) is True
    assert default_is_transient(ConnectionError()) is True
    assert default_is_transient(ValueError()) is False
    assert default_is_transient(RuntimeError()) is False


async def test_custom_is_transient_predicate(bus: EventBus) -> None:
    """7. Custom predicate: KeyError is retried, TimeoutError is not."""
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    from unittest.mock import AsyncMock

    fill = _make_fill()

    # KeyError should be retried (2 KeyErrors then success)
    underlying = AsyncMock()
    underlying.submit = AsyncMock(side_effect=[KeyError("k"), KeyError("k"), fill])
    ex = AtomicExecutor(
        underlying,
        bus,
        is_transient=lambda e: isinstance(e, KeyError),
        clock=_CLOCK,
        sleep=fake_sleep,
    )

    async with bus.subscribe() as q:
        result = await ex.submit_with_retry(_perp_open_req())

    assert result is fill
    assert len(sleep_calls) == 2  # between attempt 1→2 and 2→3

    # TimeoutError should NOT be retried with custom predicate
    sleep_calls.clear()
    from unittest.mock import AsyncMock as AM2

    underlying2 = AM2()
    underlying2.submit = AM2(side_effect=[asyncio.TimeoutError()])
    ex2 = AtomicExecutor(
        underlying2,
        bus,
        is_transient=lambda e: isinstance(e, KeyError),
        clock=_CLOCK,
        sleep=fake_sleep,
    )

    with pytest.raises(asyncio.TimeoutError):
        await ex2.submit_with_retry(_perp_open_req())

    assert sleep_calls == []  # no retry for non-transient TimeoutError


# ---------------------------------------------------------------------------
# open_paired tests
# ---------------------------------------------------------------------------


async def test_open_paired_both_succeed(bus: EventBus) -> None:
    """8. Perp fills, then spot fills. status=ok, both fills, errors=()."""
    perp_fill = _make_fill(leg=Leg.PERP, side=Side.SELL)
    spot_fill = _make_fill(leg=Leg.SPOT, side=Side.BUY)
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    underlying.submit = AsyncMock(side_effect=[perp_fill, spot_fill])
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK, sleep=fake_sleep)

    async with bus.subscribe() as q:
        result = await ex.open_paired(_perp_open_req(), _spot_open_req())

    assert isinstance(result, PairedOpenResult)
    assert result.status == "ok"
    assert result.perp_fill is perp_fill
    assert result.spot_fill is spot_fill
    assert result.errors == ()
    assert q.empty()


async def test_open_paired_perp_fails_no_spot_attempted(bus: EventBus) -> None:
    """9. Perp raises TimeoutError 3x → spot not attempted, paired_open_failed published."""
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    underlying.submit = AsyncMock(
        side_effect=[asyncio.TimeoutError(), asyncio.TimeoutError(), asyncio.TimeoutError()]
    )
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK, sleep=fake_sleep)

    async with bus.subscribe() as q:
        result = await ex.open_paired(_perp_open_req(), _spot_open_req())

        events = []
        while not q.empty():
            events.append(q.get_nowait())

    assert result.status == "failed"
    assert result.perp_fill is None
    assert result.spot_fill is None
    assert result.spot_attempts == 0
    assert result.perp_attempts == 3
    assert len(result.errors) > 0

    # Events: retry_exhausted + paired_open_failed
    kinds = [e.kind for e in events]
    assert "retry_exhausted" in kinds
    paired_evt = next(e for e in events if e.kind == "paired_open_failed")
    assert "perp leg" in paired_evt.message
    assert underlying.submit.call_count == 3  # spot was never attempted


async def test_open_paired_spot_fails_after_perp_filled(bus: EventBus) -> None:
    """10. Perp fills, spot raises TimeoutError 3x → failed, perp_fill present, 2 events."""
    perp_fill = _make_fill(leg=Leg.PERP, side=Side.SELL, client_ref="perp-ref")
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    underlying.submit = AsyncMock(
        side_effect=[
            perp_fill,
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
        ]
    )
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK, sleep=fake_sleep)

    async with bus.subscribe() as q:
        result = await ex.open_paired(_perp_open_req(client_ref="perp-ref"), _spot_open_req())

        events = []
        while not q.empty():
            events.append(q.get_nowait())

    assert result.status == "failed"
    assert result.perp_fill is perp_fill
    assert result.spot_fill is None
    assert result.spot_attempts == 3
    assert result.perp_attempts == 1

    kinds = [e.kind for e in events]
    # Both retry_exhausted (for spot) and paired_open_failed
    assert "retry_exhausted" in kinds
    assert "paired_open_failed" in kinds
    assert len(events) == 2

    paired_evt = next(e for e in events if e.kind == "paired_open_failed")
    assert "after perp filled" in paired_evt.message


async def test_open_paired_validates_perp_side(bus: EventBus) -> None:
    """11. perp_req.side=BUY → ValueError, no underlying calls, no events."""
    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    underlying.submit = AsyncMock()
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK)

    bad_perp = OrderRequest(coin=_COIN, leg=Leg.PERP, side=Side.BUY, qty=1.0)

    async with bus.subscribe() as q:
        with pytest.raises(ValueError, match="SELL"):
            await ex.open_paired(bad_perp, _spot_open_req())

    underlying.submit.assert_not_called()
    assert q.empty()


async def test_open_paired_validates_spot_side(bus: EventBus) -> None:
    """12. spot_req.side=SELL → ValueError."""
    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    underlying.submit = AsyncMock()
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK)

    bad_spot = OrderRequest(coin=_COIN, leg=Leg.SPOT, side=Side.SELL, qty=1.0)

    async with bus.subscribe() as q:
        with pytest.raises(ValueError, match="BUY"):
            await ex.open_paired(_perp_open_req(), bad_spot)

    underlying.submit.assert_not_called()
    assert q.empty()


async def test_open_paired_validates_legs(bus: EventBus) -> None:
    """13. perp_req.leg=SPOT → ValueError; spot_req.leg=PERP → ValueError."""
    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK)

    # perp req with SPOT leg
    bad_perp = OrderRequest(coin=_COIN, leg=Leg.SPOT, side=Side.SELL, qty=1.0)
    with pytest.raises(ValueError, match="PERP"):
        await ex.open_paired(bad_perp, _spot_open_req())

    # spot req with PERP leg
    bad_spot = OrderRequest(coin=_COIN, leg=Leg.PERP, side=Side.BUY, qty=1.0)
    with pytest.raises(ValueError, match="SPOT"):
        await ex.open_paired(_perp_open_req(), bad_spot)

    underlying.submit.assert_not_called()


async def test_open_paired_validates_coin_match(bus: EventBus) -> None:
    """14. perp_req.coin="BTC", spot_req.coin="ETH" → ValueError."""
    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK)

    perp_req = OrderRequest(coin="BTC", leg=Leg.PERP, side=Side.SELL, qty=1.0)
    spot_req = OrderRequest(coin="ETH", leg=Leg.SPOT, side=Side.BUY, qty=1.0)

    with pytest.raises(ValueError, match="coin mismatch"):
        await ex.open_paired(perp_req, spot_req)

    underlying.submit.assert_not_called()


async def test_open_paired_perp_transient_recovers(bus: EventBus) -> None:
    """15. Perp transient once then succeeds; spot succeeds. OK, sleeps only for perp."""
    perp_fill = _make_fill(leg=Leg.PERP, side=Side.SELL)
    spot_fill = _make_fill(leg=Leg.SPOT, side=Side.BUY)
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    underlying.submit = AsyncMock(
        side_effect=[asyncio.TimeoutError(), perp_fill, spot_fill]
    )
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK, sleep=fake_sleep)

    async with bus.subscribe() as q:
        result = await ex.open_paired(_perp_open_req(), _spot_open_req())

    assert result.status == "ok"
    assert result.perp_fill is perp_fill
    assert result.spot_fill is spot_fill
    assert sleep_calls == [2.0]  # one sleep for perp retry
    assert q.empty()


# ---------------------------------------------------------------------------
# close_paired tests
# ---------------------------------------------------------------------------


async def test_close_paired_both_succeed(bus: EventBus) -> None:
    """16. Both perp cover and spot sell succeed."""
    perp_fill = _make_fill(leg=Leg.PERP, side=Side.BUY)
    spot_fill = _make_fill(leg=Leg.SPOT, side=Side.SELL)

    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    underlying.submit = AsyncMock(side_effect=[perp_fill, spot_fill])
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK)

    async with bus.subscribe() as q:
        result = await ex.close_paired(_perp_close_req(), _spot_close_req())

    assert isinstance(result, PairedCloseResult)
    assert result.status == "ok"
    assert result.perp_fill is perp_fill
    assert result.spot_fill is spot_fill
    assert result.errors == ()
    assert q.empty()


async def test_close_paired_perp_fails_no_spot_attempted(bus: EventBus) -> None:
    """17. Perp cover fails → spot not attempted, paired_close_failed published."""
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    underlying.submit = AsyncMock(
        side_effect=[asyncio.TimeoutError(), asyncio.TimeoutError(), asyncio.TimeoutError()]
    )
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK, sleep=fake_sleep)

    async with bus.subscribe() as q:
        result = await ex.close_paired(_perp_close_req(), _spot_close_req())

        events = []
        while not q.empty():
            events.append(q.get_nowait())

    assert result.status == "failed"
    assert result.perp_fill is None
    assert result.spot_fill is None
    assert result.spot_attempts == 0
    assert result.perp_attempts == 3

    kinds = [e.kind for e in events]
    assert "paired_close_failed" in kinds
    paired_evt = next(e for e in events if e.kind == "paired_close_failed")
    assert "perp leg" in paired_evt.message
    assert underlying.submit.call_count == 3


async def test_close_paired_spot_fails_after_perp_covered(bus: EventBus) -> None:
    """18. Perp covered, then spot sell fails → status=failed, perp_fill present."""
    perp_fill = _make_fill(leg=Leg.PERP, side=Side.BUY, client_ref="perp-close-ref")
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    underlying.submit = AsyncMock(
        side_effect=[
            perp_fill,
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
        ]
    )
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK, sleep=fake_sleep)

    async with bus.subscribe() as q:
        result = await ex.close_paired(
            _perp_close_req(client_ref="perp-close-ref"),
            _spot_close_req(),
        )

        events = []
        while not q.empty():
            events.append(q.get_nowait())

    assert result.status == "failed"
    assert result.perp_fill is perp_fill
    assert result.spot_fill is None
    assert result.spot_attempts == 3

    kinds = [e.kind for e in events]
    assert "retry_exhausted" in kinds
    assert "paired_close_failed" in kinds

    paired_evt = next(e for e in events if e.kind == "paired_close_failed")
    assert "after perp covered" in paired_evt.message


async def test_close_paired_validates_perp_side(bus: EventBus) -> None:
    """19. perp_req.side=SELL → ValueError."""
    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK)

    bad_perp = OrderRequest(coin=_COIN, leg=Leg.PERP, side=Side.SELL, qty=1.0)

    async with bus.subscribe() as q:
        with pytest.raises(ValueError, match="BUY"):
            await ex.close_paired(bad_perp, _spot_close_req())

    underlying.submit.assert_not_called()
    assert q.empty()


async def test_close_paired_validates_spot_side(bus: EventBus) -> None:
    """20. spot_req.side=BUY → ValueError."""
    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK)

    bad_spot = OrderRequest(coin=_COIN, leg=Leg.SPOT, side=Side.BUY, qty=1.0)

    async with bus.subscribe() as q:
        with pytest.raises(ValueError, match="SELL"):
            await ex.close_paired(_perp_close_req(), bad_spot)

    underlying.submit.assert_not_called()
    assert q.empty()


# ---------------------------------------------------------------------------
# __init__ validation tests
# ---------------------------------------------------------------------------


def test_init_rejects_max_attempts_zero(bus: EventBus) -> None:
    """21. max_attempts=0 → ValueError."""
    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    with pytest.raises(ValueError, match="max_attempts"):
        AtomicExecutor(underlying, bus, max_attempts=0)


def test_init_rejects_short_sleep_array(bus: EventBus) -> None:
    """22. max_attempts=3 with sleep_between_attempts=(2.0,) → ValueError."""
    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    with pytest.raises(ValueError):
        AtomicExecutor(underlying, bus, max_attempts=3, sleep_between_attempts=(2.0,))


def test_init_rejects_negative_sleep(bus: EventBus) -> None:
    """23. sleep_between_attempts with negative value → ValueError."""
    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    with pytest.raises(ValueError, match=">= 0"):
        AtomicExecutor(underlying, bus, sleep_between_attempts=(2.0, -1.0))


def test_init_accepts_longer_sleep_array_than_needed(bus: EventBus) -> None:
    """24. max_attempts=2 with sleep_between=(2.0, 5.0) is valid (extras ignored)."""
    from unittest.mock import AsyncMock

    underlying = AsyncMock()
    # Should not raise
    ex = AtomicExecutor(
        underlying,
        bus,
        max_attempts=2,
        sleep_between_attempts=(2.0, 5.0),
    )
    assert ex._max_attempts == 2
