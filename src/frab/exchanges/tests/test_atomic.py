"""Tests for AtomicExecutor."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, call

import pytest

from frab.events.bus import EventBus
from frab.exchanges.atomic import (
    AtomicExecutor,
    PairedCloseResult,
    PairedOpenResult,
    default_is_transient,
)
from frab.exchanges.base import FillReport, Leg, OrderRequest, PositionState, Side

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
    qty: float = 1.0,
    client_ref: str | None = None,
) -> FillReport:
    return FillReport(
        coin=coin,
        leg=leg,
        side=side,
        ts=_FIXED_DT,
        qty=qty,
        price=50_000.0,
        fee=0.5,
        slippage_bps=2.0,
        is_paper=True,
        client_ref=client_ref,
    )


def _make_pos(coin: str = _COIN, spot_units: float = 0.0) -> PositionState:
    return PositionState(
        coin=coin,
        spot_units=spot_units,
        perp_units=0.0,
        avg_entry_spot=None,
        avg_entry_perp=None,
    )


def _perp_open_req(coin: str = _COIN, qty: float = 1.0, client_ref: str | None = "perp-ref") -> OrderRequest:
    """PERP SELL — open short."""
    return OrderRequest(coin=coin, leg=Leg.PERP, side=Side.SELL, qty=qty, client_ref=client_ref)


def _spot_open_req(coin: str = _COIN, qty: float = 1.0, client_ref: str | None = "spot-ref") -> OrderRequest:
    """SPOT BUY — buy spot."""
    return OrderRequest(coin=coin, leg=Leg.SPOT, side=Side.BUY, qty=qty, client_ref=client_ref)


def _perp_close_req(coin: str = _COIN, qty: float = 1.0, client_ref: str | None = "perp-close-ref") -> OrderRequest:
    """PERP BUY — cover short."""
    return OrderRequest(coin=coin, leg=Leg.PERP, side=Side.BUY, qty=qty, client_ref=client_ref)


def _spot_close_req(coin: str = _COIN, qty: float = 1.0, client_ref: str | None = "spot-close-ref") -> OrderRequest:
    """SPOT SELL — sell spot."""
    return OrderRequest(coin=coin, leg=Leg.SPOT, side=Side.SELL, qty=qty, client_ref=client_ref)


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


def _make_underlying(
    submit_side_effect,
    get_position_side_effect=None,
) -> AsyncMock:
    """Build a mock underlying executor."""
    underlying = AsyncMock()
    underlying.submit = AsyncMock(side_effect=submit_side_effect)
    if get_position_side_effect is not None:
        underlying.get_position = AsyncMock(side_effect=get_position_side_effect)
    else:
        # Default: always return zero spot balance
        underlying.get_position = AsyncMock(return_value=_make_pos(spot_units=0.0))
    # round_qty: identity by default (tests using full-precision floats stay green)
    underlying.round_qty = AsyncMock(side_effect=lambda coin, qty: qty)
    return underlying


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

    underlying2 = AsyncMock()
    underlying2.submit = AsyncMock(side_effect=[asyncio.TimeoutError()])
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


async def test_open_paired_calls_spot_first(bus: EventBus) -> None:
    """8. Spot submit is called before perp submit."""
    spot_fill = _make_fill(leg=Leg.SPOT, side=Side.BUY, qty=1.0)
    perp_fill = _make_fill(leg=Leg.PERP, side=Side.SELL, qty=1.0)

    underlying = _make_underlying(
        submit_side_effect=[spot_fill, perp_fill],
        get_position_side_effect=[
            _make_pos(spot_units=0.0),   # pre-snapshot
            _make_pos(spot_units=1.0),   # post-snapshot
        ],
    )
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK)

    await ex.open_paired(_perp_open_req(), _spot_open_req())

    # First submit call is the spot req, second is the perp req
    submit_calls = underlying.submit.call_args_list
    assert len(submit_calls) == 2
    first_req = submit_calls[0].args[0]
    second_req = submit_calls[1].args[0]
    assert first_req.leg == Leg.SPOT
    assert second_req.leg == Leg.PERP


async def test_open_paired_both_succeed(bus: EventBus) -> None:
    """9. Spot fills, then perp fills. status=ok, both fills, errors=()."""
    spot_fill = _make_fill(leg=Leg.SPOT, side=Side.BUY, qty=1.0)
    perp_fill = _make_fill(leg=Leg.PERP, side=Side.SELL, qty=1.0)

    underlying = _make_underlying(
        submit_side_effect=[spot_fill, perp_fill],
        get_position_side_effect=[
            _make_pos(spot_units=0.0),
            _make_pos(spot_units=1.0),
        ],
    )
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK)

    async with bus.subscribe() as q:
        result = await ex.open_paired(_perp_open_req(), _spot_open_req())

    assert isinstance(result, PairedOpenResult)
    assert result.status == "ok"
    assert result.perp_fill is perp_fill
    assert result.spot_fill is not None
    assert result.errors == ()
    assert q.empty()


async def test_open_paired_sizes_perp_to_actual_spot_delta(bus: EventBus) -> None:
    """10. Perp leg is sized to the actual spot balance delta, not the requested qty."""
    requested_qty = 0.00015
    actual_delta = 0.0001499  # HL fee deducted in asset

    spot_fill = _make_fill(leg=Leg.SPOT, side=Side.BUY, qty=requested_qty)
    perp_fill = _make_fill(leg=Leg.PERP, side=Side.SELL, qty=actual_delta)

    underlying = _make_underlying(
        submit_side_effect=[spot_fill, perp_fill],
        get_position_side_effect=[
            _make_pos(spot_units=0.0),
            _make_pos(spot_units=actual_delta),
        ],
    )
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK)

    await ex.open_paired(
        _perp_open_req(qty=requested_qty),
        _spot_open_req(qty=requested_qty),
    )

    # Second submit (perp) should be called with qty == actual_delta
    perp_call_req = underlying.submit.call_args_list[1].args[0]
    assert perp_call_req.leg == Leg.PERP
    assert perp_call_req.qty == pytest.approx(actual_delta)


async def test_open_paired_returns_adjusted_spot_fill(bus: EventBus) -> None:
    """11. spot_fill.qty in the result equals the observed delta, not the gross requested qty."""
    requested_qty = 0.00015
    actual_delta = 0.0001499

    spot_fill = _make_fill(leg=Leg.SPOT, side=Side.BUY, qty=requested_qty)
    perp_fill = _make_fill(leg=Leg.PERP, side=Side.SELL, qty=actual_delta)

    underlying = _make_underlying(
        submit_side_effect=[spot_fill, perp_fill],
        get_position_side_effect=[
            _make_pos(spot_units=0.0),
            _make_pos(spot_units=actual_delta),
        ],
    )
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK)

    result = await ex.open_paired(
        _perp_open_req(qty=requested_qty),
        _spot_open_req(qty=requested_qty),
    )

    assert result.status == "ok"
    assert result.spot_fill is not None
    assert result.spot_fill.qty == pytest.approx(actual_delta)
    # Other fields preserved from the original fill
    assert result.spot_fill.price == spot_fill.price
    assert result.spot_fill.fee == spot_fill.fee


async def test_open_paired_spot_fails_no_perp_attempted(bus: EventBus) -> None:
    """12. Spot raises TimeoutError 3x → perp never called, paired_open_failed published."""
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    underlying = _make_underlying(
        submit_side_effect=[
            asyncio.TimeoutError(), asyncio.TimeoutError(), asyncio.TimeoutError()
        ],
        get_position_side_effect=[_make_pos(spot_units=0.0)],
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
    assert result.perp_attempts == 0
    assert result.spot_attempts == 3
    assert len(result.errors) > 0

    kinds = [e.kind for e in events]
    assert "retry_exhausted" in kinds
    paired_evt = next(e for e in events if e.kind == "paired_open_failed")
    assert "spot leg" in paired_evt.message
    assert underlying.submit.call_count == 3  # perp was never attempted


async def test_open_paired_perp_fails_after_spot_filled_publishes_paired_open_failed(bus: EventBus) -> None:
    """13. Spot fills, perp raises TimeoutError 3x → failed, spot_fill present, events published."""
    spot_fill = _make_fill(leg=Leg.SPOT, side=Side.BUY, qty=1.0, client_ref="spot-ref")
    actual_delta = 1.0
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    underlying = _make_underlying(
        submit_side_effect=[
            spot_fill,
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
        ],
        get_position_side_effect=[
            _make_pos(spot_units=0.0),
            _make_pos(spot_units=actual_delta),
        ],
    )
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK, sleep=fake_sleep)

    async with bus.subscribe() as q:
        result = await ex.open_paired(_perp_open_req(client_ref="perp-ref"), _spot_open_req(client_ref="spot-ref"))

        events = []
        while not q.empty():
            events.append(q.get_nowait())

    assert result.status == "failed"
    assert result.spot_fill is not None
    assert result.spot_fill.qty == pytest.approx(actual_delta)
    assert result.perp_fill is None
    assert result.perp_attempts == 3
    assert result.spot_attempts == 1

    kinds = [e.kind for e in events]
    assert "retry_exhausted" in kinds
    assert "paired_open_failed" in kinds
    assert len(events) == 2

    paired_evt = next(e for e in events if e.kind == "paired_open_failed")
    assert "after spot filled" in paired_evt.message


async def test_open_paired_zero_spot_delta_publishes_failure(bus: EventBus) -> None:
    """14. get_position returns same balance pre/post → delta=0 → fails, no perp call."""
    spot_fill = _make_fill(leg=Leg.SPOT, side=Side.BUY, qty=1.0)

    underlying = _make_underlying(
        submit_side_effect=[spot_fill],
        get_position_side_effect=[
            _make_pos(spot_units=5.0),  # pre-snapshot
            _make_pos(spot_units=5.0),  # post-snapshot: no change
        ],
    )
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK)

    async with bus.subscribe() as q:
        result = await ex.open_paired(_perp_open_req(), _spot_open_req())

        events = []
        while not q.empty():
            events.append(q.get_nowait())

    assert result.status == "failed"
    assert result.perp_fill is None
    assert result.spot_fill is None
    assert result.perp_attempts == 0
    # Only one submit call (spot), perp never attempted
    assert underlying.submit.call_count == 1

    paired_evt = next(e for e in events if e.kind == "paired_open_failed")
    assert "zero delta" in paired_evt.message


async def test_open_paired_validates_perp_side(bus: EventBus) -> None:
    """15. perp_req.side=BUY → ValueError, no underlying calls, no events."""
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
    """16. spot_req.side=SELL → ValueError."""
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
    """17. perp_req.leg=SPOT → ValueError; spot_req.leg=PERP → ValueError."""
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
    """18. perp_req.coin="BTC", spot_req.coin="ETH" → ValueError."""
    underlying = AsyncMock()
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK)

    perp_req = OrderRequest(coin="BTC", leg=Leg.PERP, side=Side.SELL, qty=1.0)
    spot_req = OrderRequest(coin="ETH", leg=Leg.SPOT, side=Side.BUY, qty=1.0)

    with pytest.raises(ValueError, match="coin mismatch"):
        await ex.open_paired(perp_req, spot_req)

    underlying.submit.assert_not_called()


async def test_open_paired_spot_transient_recovers(bus: EventBus) -> None:
    """19. Spot transient once then succeeds; perp succeeds. OK, one sleep."""
    spot_fill = _make_fill(leg=Leg.SPOT, side=Side.BUY, qty=1.0)
    perp_fill = _make_fill(leg=Leg.PERP, side=Side.SELL, qty=1.0)
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    underlying = _make_underlying(
        submit_side_effect=[asyncio.TimeoutError(), spot_fill, perp_fill],
        get_position_side_effect=[
            _make_pos(spot_units=0.0),  # pre-snapshot
            _make_pos(spot_units=1.0),  # post-snapshot
        ],
    )
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK, sleep=fake_sleep)

    async with bus.subscribe() as q:
        result = await ex.open_paired(_perp_open_req(), _spot_open_req())

    assert result.status == "ok"
    assert result.perp_fill is perp_fill
    assert result.spot_fill is not None
    assert sleep_calls == [2.0]  # one sleep for spot retry
    assert q.empty()


# ---------------------------------------------------------------------------
# close_paired tests
# ---------------------------------------------------------------------------


async def test_close_paired_calls_spot_first(bus: EventBus) -> None:
    """20. Spot submit is called before perp submit on close."""
    spot_fill = _make_fill(leg=Leg.SPOT, side=Side.SELL, qty=1.0)
    perp_fill = _make_fill(leg=Leg.PERP, side=Side.BUY, qty=1.0)

    underlying = _make_underlying(
        submit_side_effect=[spot_fill, perp_fill],
        get_position_side_effect=[
            _make_pos(spot_units=1.0),   # pre-snapshot
            _make_pos(spot_units=0.0),   # post-snapshot (sold)
        ],
    )
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK)

    await ex.close_paired(_perp_close_req(), _spot_close_req())

    submit_calls = underlying.submit.call_args_list
    assert len(submit_calls) == 2
    first_req = submit_calls[0].args[0]
    second_req = submit_calls[1].args[0]
    assert first_req.leg == Leg.SPOT
    assert second_req.leg == Leg.PERP


async def test_close_paired_both_succeed(bus: EventBus) -> None:
    """21. Both spot sell and perp cover succeed."""
    spot_fill = _make_fill(leg=Leg.SPOT, side=Side.SELL, qty=1.0)
    perp_fill = _make_fill(leg=Leg.PERP, side=Side.BUY, qty=1.0)

    underlying = _make_underlying(
        submit_side_effect=[spot_fill, perp_fill],
        get_position_side_effect=[
            _make_pos(spot_units=1.0),
            _make_pos(spot_units=0.0),
        ],
    )
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK)

    async with bus.subscribe() as q:
        result = await ex.close_paired(_perp_close_req(), _spot_close_req())

    assert isinstance(result, PairedCloseResult)
    assert result.status == "ok"
    assert result.perp_fill is perp_fill
    assert result.spot_fill is not None
    assert result.errors == ()
    assert q.empty()


async def test_close_paired_sizes_perp_to_abs_spot_delta(bus: EventBus) -> None:
    """22. Perp leg qty equals abs(spot balance delta) on close."""
    requested_qty = 1.0
    actual_sold = 0.9999  # slightly less due to hypothetical rounding

    spot_fill = _make_fill(leg=Leg.SPOT, side=Side.SELL, qty=requested_qty)
    perp_fill = _make_fill(leg=Leg.PERP, side=Side.BUY, qty=actual_sold)

    underlying = _make_underlying(
        submit_side_effect=[spot_fill, perp_fill],
        get_position_side_effect=[
            _make_pos(spot_units=1.0),
            _make_pos(spot_units=1.0 - actual_sold),  # decreased by actual_sold
        ],
    )
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK)

    await ex.close_paired(
        _perp_close_req(qty=requested_qty),
        _spot_close_req(qty=requested_qty),
    )

    perp_call_req = underlying.submit.call_args_list[1].args[0]
    assert perp_call_req.leg == Leg.PERP
    assert perp_call_req.qty == pytest.approx(actual_sold)


async def test_close_paired_spot_fails_no_perp_attempted(bus: EventBus) -> None:
    """23. Spot sell fails → perp not attempted, paired_close_failed published."""
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    underlying = _make_underlying(
        submit_side_effect=[
            asyncio.TimeoutError(), asyncio.TimeoutError(), asyncio.TimeoutError()
        ],
        get_position_side_effect=[_make_pos(spot_units=1.0)],
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
    assert result.perp_attempts == 0
    assert result.spot_attempts == 3

    kinds = [e.kind for e in events]
    assert "paired_close_failed" in kinds
    paired_evt = next(e for e in events if e.kind == "paired_close_failed")
    assert "spot leg" in paired_evt.message
    assert underlying.submit.call_count == 3


async def test_close_paired_perp_fails_after_spot_sold(bus: EventBus) -> None:
    """24. Spot sells, perp cover fails → status=failed, spot_fill present."""
    spot_fill = _make_fill(leg=Leg.SPOT, side=Side.SELL, qty=1.0, client_ref="spot-close-ref")
    sleep_calls: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    underlying = _make_underlying(
        submit_side_effect=[
            spot_fill,
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
        ],
        get_position_side_effect=[
            _make_pos(spot_units=1.0),
            _make_pos(spot_units=0.0),
        ],
    )
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK, sleep=fake_sleep)

    async with bus.subscribe() as q:
        result = await ex.close_paired(
            _perp_close_req(client_ref="perp-close-ref"),
            _spot_close_req(client_ref="spot-close-ref"),
        )

        events = []
        while not q.empty():
            events.append(q.get_nowait())

    assert result.status == "failed"
    assert result.spot_fill is not None
    assert result.perp_fill is None
    assert result.perp_attempts == 3
    assert result.spot_attempts == 1

    kinds = [e.kind for e in events]
    assert "retry_exhausted" in kinds
    assert "paired_close_failed" in kinds

    paired_evt = next(e for e in events if e.kind == "paired_close_failed")
    assert "after spot sold" in paired_evt.message


async def test_close_paired_validates_perp_side(bus: EventBus) -> None:
    """25. perp_req.side=SELL → ValueError."""
    underlying = AsyncMock()
    ex = AtomicExecutor(underlying, bus, clock=_CLOCK)

    bad_perp = OrderRequest(coin=_COIN, leg=Leg.PERP, side=Side.SELL, qty=1.0)

    async with bus.subscribe() as q:
        with pytest.raises(ValueError, match="BUY"):
            await ex.close_paired(bad_perp, _spot_close_req())

    underlying.submit.assert_not_called()
    assert q.empty()


async def test_close_paired_validates_spot_side(bus: EventBus) -> None:
    """26. spot_req.side=BUY → ValueError."""
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
    """27. max_attempts=0 → ValueError."""
    underlying = AsyncMock()
    with pytest.raises(ValueError, match="max_attempts"):
        AtomicExecutor(underlying, bus, max_attempts=0)


def test_init_rejects_short_sleep_array(bus: EventBus) -> None:
    """28. max_attempts=3 with sleep_between_attempts=(2.0,) → ValueError."""
    underlying = AsyncMock()
    with pytest.raises(ValueError):
        AtomicExecutor(underlying, bus, max_attempts=3, sleep_between_attempts=(2.0,))


def test_init_rejects_negative_sleep(bus: EventBus) -> None:
    """29. sleep_between_attempts with negative value → ValueError."""
    underlying = AsyncMock()
    with pytest.raises(ValueError, match=">= 0"):
        AtomicExecutor(underlying, bus, sleep_between_attempts=(2.0, -1.0))


def test_init_accepts_longer_sleep_array_than_needed(bus: EventBus) -> None:
    """30. max_attempts=2 with sleep_between=(2.0, 5.0) is valid (extras ignored)."""
    underlying = AsyncMock()
    # Should not raise
    ex = AtomicExecutor(
        underlying,
        bus,
        max_attempts=2,
        sleep_between_attempts=(2.0, 5.0),
    )
    assert ex._max_attempts == 2
