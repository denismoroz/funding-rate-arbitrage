"""Unit tests for NewState (XsmomState.NEW handler)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from frab.constants import PERP_TAKER
from frab.domain import Instrument, Side, XsmomPosition, XsmomState
from frab.strategy.xsmom.params import XsmomParams
from frab.strategy.xsmom.states._base import XsmomContext
from frab.strategy.xsmom.states.new import NewState

_NOW_MS = 1_704_067_200_000  # 2024-01-01 00:00:00 UTC


def _make_fp(
    *,
    coin: str = "BTC",
    side: Side = Side.SHORT,
    target_qty: float | None = 0.01,
    state_data: dict | None = None,
    state: XsmomState = XsmomState.NEW,
) -> XsmomPosition:
    return XsmomPosition(
        id=42,
        strategy_id=1,
        coin=coin,
        side=side,
        state=state,
        state_data=state_data if state_data is not None else {},
        perp_position_id=None,
        collateral_position_id=None,
        target_qty=target_qty,
        opened_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        closed_at=None,
    )


def _make_params(**overrides) -> XsmomParams:
    defaults = dict(
        budget_cap=1000.0,
        universe=("BTC", "ETH", "SOL"),
        leverage=1,
        margin_buffer_factor=3.0,
    )
    defaults.update(overrides)
    return XsmomParams(**defaults)


def _make_ctx(
    mocker,
    *,
    exchange=None,
    xsmom_repo=None,
    params=None,
    event_bus=None,
) -> XsmomContext:
    settings = mocker.MagicMock()
    return XsmomContext(
        exchange=exchange or mocker.AsyncMock(),
        xsmom_repo=xsmom_repo or mocker.AsyncMock(),
        params=params or _make_params(),
        session_factory=mocker.MagicMock(),
        settings=settings,
        event_bus=event_bus,
    )


def _make_coll_pos(mocker, pos_id=10):
    pos = mocker.MagicMock()
    pos.id = pos_id
    return pos


def _make_perp_pos(mocker, pos_id=20, entry_price=50000.0, qty=0.01):
    pos = mocker.MagicMock()
    pos.id = pos_id
    pos.entry_price = entry_price
    pos.qty = qty
    return pos


# ── Happy path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_new_happy_path_short(mocker):
    """Sufficient balance → collateral row + perp SHORT opened → OPENED, correct state_data."""
    params = _make_params(leverage=1, margin_buffer_factor=3.0)
    target_qty = 0.01
    mark_price = 50000.0
    notional = target_qty * mark_price   # 500
    required = (notional / params.leverage) * params.margin_buffer_factor  # 1500

    exchange = mocker.AsyncMock()
    quote = mocker.MagicMock()
    quote.mark = mark_price
    quote.spot = None
    exchange.get_quote.return_value = quote
    exchange.get_wallet.return_value = required + 1000.0  # plenty
    exchange.round_qty_to_nearest = mocker.AsyncMock(side_effect=lambda c, q: q)

    coll_pos = _make_coll_pos(mocker, pos_id=10)
    perp_pos = _make_perp_pos(mocker, pos_id=20, entry_price=mark_price, qty=target_qty)

    def _open_position(req):
        if req.instrument == Instrument.COLLATERAL:
            return coll_pos
        return perp_pos

    exchange.open_position = mocker.AsyncMock(side_effect=_open_position)

    repo = mocker.AsyncMock()
    repo.set_leg = mocker.AsyncMock()
    repo.transition = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, xsmom_repo=repo, params=params)
    state = NewState(ctx)
    fp = _make_fp(side=Side.SHORT, target_qty=target_qty)

    result = await state.execute(fp)

    assert result == XsmomState.OPENED

    # Wallet check was called
    exchange.get_wallet.assert_awaited_once_with("USDC", mocker.ANY)

    # Two open_position calls: collateral then perp
    assert exchange.open_position.await_count == 2
    coll_req = exchange.open_position.await_args_list[0].args[0]
    perp_req = exchange.open_position.await_args_list[1].args[0]

    assert coll_req.instrument == Instrument.COLLATERAL
    assert coll_req.side.value == "none"
    assert coll_req.qty == pytest.approx(required)
    assert coll_req.coin == "USDC"

    assert perp_req.instrument == Instrument.PERP
    assert perp_req.side == Side.SHORT
    assert perp_req.qty == pytest.approx(target_qty)
    assert perp_req.coin == "BTC"
    assert perp_req.leverage == params.leverage

    # set_leg called twice
    assert repo.set_leg.await_count == 2
    set_coll = repo.set_leg.await_args_list[0]
    set_perp = repo.set_leg.await_args_list[1]
    assert set_coll.kwargs["collateral_position_id"] == 10
    assert set_perp.kwargs["perp_position_id"] == 20

    # transition to OPENED
    repo.transition.assert_awaited_once()
    trans_kwargs = repo.transition.await_args.kwargs
    assert trans_kwargs["from_state"] == XsmomState.NEW
    assert trans_kwargs["to_state"] == XsmomState.OPENED
    sd = trans_kwargs["state_data"]
    assert sd["required_margin"] == pytest.approx(required)
    assert sd["notional"] == pytest.approx(notional)
    assert sd["leverage"] == params.leverage
    assert sd["gross_funding_so_far"] == 0.0
    assert "opened_at_ms" in sd
    expected_fees = notional * PERP_TAKER * 2
    assert sd["total_fees_paid"] == pytest.approx(expected_fees)


@pytest.mark.asyncio
async def test_new_happy_path_long(mocker):
    """Long-side perp opens with side=LONG."""
    params = _make_params(leverage=2, margin_buffer_factor=2.0)
    target_qty = 1.0

    exchange = mocker.AsyncMock()
    quote = mocker.MagicMock()
    quote.mark = 100.0
    quote.spot = None
    exchange.get_quote.return_value = quote
    exchange.get_wallet.return_value = 9999.0
    exchange.round_qty_to_nearest = mocker.AsyncMock(side_effect=lambda c, q: q)

    coll_pos = _make_coll_pos(mocker)
    perp_pos = _make_perp_pos(mocker, qty=1.0, entry_price=100.0)

    def _open(req):
        return coll_pos if req.instrument == Instrument.COLLATERAL else perp_pos

    exchange.open_position = mocker.AsyncMock(side_effect=_open)

    repo = mocker.AsyncMock()
    ctx = _make_ctx(mocker, exchange=exchange, xsmom_repo=repo, params=params)
    state = NewState(ctx)
    fp = _make_fp(side=Side.LONG, target_qty=target_qty)

    result = await state.execute(fp)

    assert result == XsmomState.OPENED
    perp_req = exchange.open_position.await_args_list[1].args[0]
    assert perp_req.side == Side.LONG


@pytest.mark.asyncio
async def test_new_uses_spot_price_when_available(mocker):
    """If quote.spot is not None, price = spot (not mark)."""
    params = _make_params()
    target_qty = 1.0
    spot_price = 90.0
    mark_price = 95.0  # higher than spot; should use spot

    exchange = mocker.AsyncMock()
    quote = mocker.MagicMock()
    quote.mark = mark_price
    quote.spot = spot_price
    exchange.get_quote.return_value = quote
    exchange.get_wallet.return_value = 9999.0
    exchange.round_qty_to_nearest = mocker.AsyncMock(side_effect=lambda c, q: q)

    coll_pos = _make_coll_pos(mocker)
    perp_pos = _make_perp_pos(mocker, entry_price=spot_price)

    def _open(req):
        return coll_pos if req.instrument == Instrument.COLLATERAL else perp_pos

    exchange.open_position = mocker.AsyncMock(side_effect=_open)

    repo = mocker.AsyncMock()
    ctx = _make_ctx(mocker, exchange=exchange, xsmom_repo=repo, params=params)
    state = NewState(ctx)
    fp = _make_fp(target_qty=target_qty)

    await state.execute(fp)

    trans_kwargs = repo.transition.await_args.kwargs
    expected_notional = target_qty * spot_price
    assert trans_kwargs["state_data"]["notional"] == pytest.approx(expected_notional)


@pytest.mark.asyncio
async def test_new_uses_state_data_notional_if_present(mocker):
    """Phase D may inject notional/required_margin into state_data; must be used as-is."""
    params = _make_params()
    preset_notional = 777.0
    preset_required = 2331.0  # whatever Phase D computed

    exchange = mocker.AsyncMock()
    exchange.get_wallet.return_value = 9999.0
    exchange.round_qty_to_nearest = mocker.AsyncMock(side_effect=lambda c, q: q)

    coll_pos = _make_coll_pos(mocker)
    perp_pos = _make_perp_pos(mocker)

    def _open(req):
        return coll_pos if req.instrument == Instrument.COLLATERAL else perp_pos

    exchange.open_position = mocker.AsyncMock(side_effect=_open)

    repo = mocker.AsyncMock()
    ctx = _make_ctx(mocker, exchange=exchange, xsmom_repo=repo, params=params)
    state = NewState(ctx)
    fp = _make_fp(
        target_qty=0.01,
        state_data={"notional": preset_notional, "required_margin": preset_required},
    )

    await state.execute(fp)

    # Should NOT have called get_quote
    exchange.get_quote.assert_not_awaited()

    sd = repo.transition.await_args.kwargs["state_data"]
    assert sd["notional"] == pytest.approx(preset_notional)
    assert sd["required_margin"] == pytest.approx(preset_required)


@pytest.mark.asyncio
async def test_new_publishes_opened_event(mocker):
    """On success, xsmom.opened INFO event is published."""
    params = _make_params()
    exchange = mocker.AsyncMock()
    quote = mocker.MagicMock()
    quote.mark = 100.0
    quote.spot = None
    exchange.get_quote.return_value = quote
    exchange.get_wallet.return_value = 9999.0
    exchange.round_qty_to_nearest = mocker.AsyncMock(side_effect=lambda c, q: q)

    def _open(req):
        pos = mocker.MagicMock()
        pos.id = 99
        pos.entry_price = 100.0
        pos.qty = 1.0
        return pos

    exchange.open_position = mocker.AsyncMock(side_effect=_open)

    repo = mocker.AsyncMock()
    bus = mocker.AsyncMock()
    bus.publish = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, xsmom_repo=repo, params=params, event_bus=bus)
    state = NewState(ctx)
    fp = _make_fp(target_qty=1.0)

    await state.execute(fp)

    bus.publish.assert_awaited_once()
    published = bus.publish.await_args.args[0]
    assert published.level == "INFO"
    assert published.kind == "xsmom.opened"
    assert published.payload_json["xsmom_position_id"] == fp.id


# ── Insufficient margin ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_new_insufficient_balance_marks_failed(mocker):
    """balance < required → mark_failed, WARNING event, no perp opened, returns None."""
    params = _make_params(leverage=1, margin_buffer_factor=3.0)
    target_qty = 10.0
    mark_price = 100.0
    required = target_qty * mark_price * 3.0  # 3000

    exchange = mocker.AsyncMock()
    quote = mocker.MagicMock()
    quote.mark = mark_price
    quote.spot = None
    exchange.get_quote.return_value = quote
    exchange.get_wallet.return_value = 1.0  # way below required

    repo = mocker.AsyncMock()
    repo.mark_failed = mocker.AsyncMock()

    bus = mocker.AsyncMock()
    bus.publish = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, xsmom_repo=repo, params=params, event_bus=bus)
    state = NewState(ctx)
    fp = _make_fp(target_qty=target_qty)

    result = await state.execute(fp)

    assert result is None
    repo.mark_failed.assert_awaited_once_with(fp.id, reason=mocker.ANY)
    reason_arg = repo.mark_failed.await_args.kwargs["reason"]
    assert "insufficient_margin" in reason_arg
    # No perp opened
    exchange.open_position.assert_not_awaited()
    # WARNING event
    bus.publish.assert_awaited_once()
    published = bus.publish.await_args.args[0]
    assert published.level == "WARNING"
    assert published.kind == "xsmom.failed"


@pytest.mark.asyncio
async def test_new_insufficient_no_event_bus(mocker):
    """event_bus=None on failure path → no exception."""
    params = _make_params(leverage=1, margin_buffer_factor=3.0)
    exchange = mocker.AsyncMock()
    quote = mocker.MagicMock()
    quote.mark = 100.0
    quote.spot = None
    exchange.get_quote.return_value = quote
    exchange.get_wallet.return_value = 0.0

    repo = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, xsmom_repo=repo, params=params, event_bus=None)
    state = NewState(ctx)
    fp = _make_fp(target_qty=1.0)

    result = await state.execute(fp)

    assert result is None
    repo.mark_failed.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_no_target_qty_marks_failed(mocker):
    """target_qty=None and no state_data notional → mark_failed, no exchange calls."""
    params = _make_params()
    exchange = mocker.AsyncMock()
    repo = mocker.AsyncMock()

    ctx = _make_ctx(mocker, exchange=exchange, xsmom_repo=repo, params=params)
    state = NewState(ctx)
    fp = _make_fp(target_qty=None)

    result = await state.execute(fp)

    assert result is None
    repo.mark_failed.assert_awaited_once()
    reason = repo.mark_failed.await_args.kwargs["reason"]
    assert "target_qty" in reason
    exchange.open_position.assert_not_awaited()
