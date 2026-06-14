"""Unit tests for CloseState (XsmomState.CLOSE handler)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from frab.domain import Side, XsmomPosition, XsmomState
from frab.strategy.xsmom.params import XsmomParams
from frab.strategy.xsmom.states._base import XsmomContext
from frab.strategy.xsmom.states.close import CloseState

_NOW_MS = 1_704_067_200_000


def _make_fp(
    *,
    perp_position_id: int | None = 20,
    collateral_position_id: int | None = 10,
    state_data: dict | None = None,
) -> XsmomPosition:
    return XsmomPosition(
        id=55,
        strategy_id=1,
        coin="ETH",
        side=Side.SHORT,
        state=XsmomState.CLOSE,
        state_data=state_data if state_data is not None else {},
        perp_position_id=perp_position_id,
        collateral_position_id=collateral_position_id,
        target_qty=0.5,
        opened_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        closed_at=None,
    )


def _make_params() -> XsmomParams:
    return XsmomParams(budget_cap=1000.0, universe=("ETH",))


def _make_ctx(mocker, *, exchange=None, xsmom_repo=None, event_bus=None) -> XsmomContext:
    settings = mocker.MagicMock()
    return XsmomContext(
        exchange=exchange or mocker.AsyncMock(),
        xsmom_repo=xsmom_repo or mocker.AsyncMock(),
        params=_make_params(),
        session_factory=mocker.MagicMock(),
        settings=settings,
        event_bus=event_bus,
    )


@pytest.mark.asyncio
async def test_close_happy_path(mocker):
    """CLOSE: closes perp leg, closes collateral row, mark_closed called, returns None."""
    perp_pos = mocker.MagicMock()
    coll_pos = mocker.MagicMock()

    def _load(sf, pos_id):
        return perp_pos if pos_id == 20 else coll_pos

    mocker.patch(
        "frab.strategy.xsmom.states.close.load_position",
        new_callable=mocker.AsyncMock,
        side_effect=_load,
    )

    exchange = mocker.AsyncMock()
    exchange.close_position = mocker.AsyncMock()

    repo = mocker.AsyncMock()
    repo.mark_closed = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, xsmom_repo=repo)
    state = CloseState(ctx)
    fp = _make_fp()

    result = await state.execute(fp)

    assert result is None
    # Both legs closed
    assert exchange.close_position.await_count == 2
    # mark_closed called
    repo.mark_closed.assert_awaited_once_with(fp.id)


@pytest.mark.asyncio
async def test_close_publishes_event(mocker):
    """On CLOSE success, xsmom.closed INFO event is published."""
    mocker.patch(
        "frab.strategy.xsmom.states.close.load_position",
        new_callable=mocker.AsyncMock,
        return_value=mocker.MagicMock(),
    )

    exchange = mocker.AsyncMock()
    repo = mocker.AsyncMock()

    bus = mocker.AsyncMock()
    bus.publish = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, xsmom_repo=repo, event_bus=bus)
    state = CloseState(ctx)
    fp = _make_fp()

    await state.execute(fp)

    bus.publish.assert_awaited_once()
    published = bus.publish.await_args.args[0]
    assert published.level == "INFO"
    assert published.kind == "xsmom.closed"
    assert published.payload_json["xsmom_position_id"] == fp.id


@pytest.mark.asyncio
async def test_close_no_perp_position_id(mocker):
    """perp_position_id=None: log warning, still closes collateral + mark_closed."""
    coll_pos = mocker.MagicMock()

    mocker.patch(
        "frab.strategy.xsmom.states.close.load_position",
        new_callable=mocker.AsyncMock,
        return_value=coll_pos,
    )

    exchange = mocker.AsyncMock()
    repo = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, xsmom_repo=repo)
    state = CloseState(ctx)
    fp = _make_fp(perp_position_id=None)

    result = await state.execute(fp)

    assert result is None
    # Only collateral close
    assert exchange.close_position.await_count == 1
    repo.mark_closed.assert_awaited_once_with(fp.id)


@pytest.mark.asyncio
async def test_close_no_collateral_position_id(mocker):
    """collateral_position_id=None: closes perp only, mark_closed still called."""
    perp_pos = mocker.MagicMock()

    mocker.patch(
        "frab.strategy.xsmom.states.close.load_position",
        new_callable=mocker.AsyncMock,
        return_value=perp_pos,
    )

    exchange = mocker.AsyncMock()
    repo = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, xsmom_repo=repo)
    state = CloseState(ctx)
    fp = _make_fp(collateral_position_id=None)

    result = await state.execute(fp)

    assert result is None
    assert exchange.close_position.await_count == 1
    repo.mark_closed.assert_awaited_once_with(fp.id)


@pytest.mark.asyncio
async def test_close_no_event_bus(mocker):
    """event_bus=None → no exception, mark_closed still called."""
    mocker.patch(
        "frab.strategy.xsmom.states.close.load_position",
        new_callable=mocker.AsyncMock,
        return_value=mocker.MagicMock(),
    )

    exchange = mocker.AsyncMock()
    repo = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, xsmom_repo=repo, event_bus=None)
    state = CloseState(ctx)
    fp = _make_fp()

    result = await state.execute(fp)

    assert result is None
    repo.mark_closed.assert_awaited_once()
