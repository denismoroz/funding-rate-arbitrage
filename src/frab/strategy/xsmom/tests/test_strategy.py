"""Unit tests for XsmomStrategy orchestrator (advance_one, on_*_tick, manual controls)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from frab.domain import Side, XsmomPosition, XsmomState
from frab.repo.xsmom_repo import XsmomStateConflict
from frab.strategy.xsmom.params import XsmomParams
from frab.strategy.xsmom.strategy import XsmomStrategy

_NOW_MS = 1_704_067_200_000  # 2024-01-01 UTC


def _make_params() -> XsmomParams:
    return XsmomParams(budget_cap=1000.0, universe=("BTC", "ETH", "SOL"))


def _make_fp(
    fp_id: int = 1,
    coin: str = "BTC",
    state: XsmomState = XsmomState.NEW,
    side: Side = Side.SHORT,
    state_data: dict | None = None,
    perp_position_id: int | None = None,
    collateral_position_id: int | None = None,
    target_qty: float | None = 0.01,
) -> XsmomPosition:
    return XsmomPosition(
        id=fp_id,
        strategy_id=1,
        coin=coin,
        side=side,
        state=state,
        state_data=state_data or {},
        perp_position_id=perp_position_id,
        collateral_position_id=collateral_position_id,
        target_qty=target_qty,
        opened_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        closed_at=None,
    )


def _make_strategy(mocker, *, params=None, strategy_id=1) -> tuple[XsmomStrategy, object, object]:
    """Returns (strategy, mock_repo, mock_exchange)."""
    exchange = mocker.AsyncMock()
    xsmom_repo = mocker.AsyncMock()
    # on_hour_tick now runs a scan; give the shared mock a clean empty panel.
    xsmom_repo.get_daily_closes = mocker.AsyncMock(return_value={})
    settings = mocker.MagicMock()
    sf = mocker.MagicMock()

    strategy = XsmomStrategy(
        strategy_id=strategy_id,
        exchange=exchange,
        xsmom_repo=xsmom_repo,
        session_factory=sf,
        params=params or _make_params(),
        settings=settings,
    )
    return strategy, xsmom_repo, exchange


# ── advance_all_pending / _advance_one ───────────────────────────────────────

@pytest.mark.asyncio
async def test_advance_one_drives_new_to_opened(mocker):
    """NEW → (step) → OPENED in one advance_all_pending call."""
    fp_new = _make_fp(fp_id=1, state=XsmomState.NEW)
    fp_opened = _make_fp(fp_id=1, state=XsmomState.OPENED)

    strategy, xsmom_repo, _ = _make_strategy(mocker)
    xsmom_repo.list_non_terminal = mocker.AsyncMock(return_value=[fp_new])
    # After the dispatch, refetch returns OPENED
    xsmom_repo.get = mocker.AsyncMock(return_value=fp_opened)

    # Make the state machine's NewState.execute return OPENED
    mock_step = mocker.AsyncMock(return_value=XsmomState.OPENED)
    strategy._state_machine.step = mock_step

    await strategy.advance_all_pending()

    mock_step.assert_awaited_once_with(fp_new)
    # After refetch we see OPENED which is non-transient → loop stops
    xsmom_repo.get.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_advance_one_drives_close_to_closed(mocker):
    """CLOSE → (step) → None (mark_closed sets CLOSED) → loop stops."""
    fp_close = _make_fp(fp_id=2, state=XsmomState.CLOSE)
    fp_closed = _make_fp(fp_id=2, state=XsmomState.CLOSED)

    strategy, xsmom_repo, _ = _make_strategy(mocker)
    xsmom_repo.list_non_terminal = mocker.AsyncMock(return_value=[fp_close])
    xsmom_repo.get = mocker.AsyncMock(return_value=fp_closed)

    # CloseState.execute returns None (mark_closed → CLOSED is terminal)
    mock_step = mocker.AsyncMock(return_value=None)
    strategy._state_machine.step = mock_step

    await strategy.advance_all_pending()

    mock_step.assert_awaited_once_with(fp_close)


@pytest.mark.asyncio
async def test_advance_one_state_conflict_breaks(mocker):
    """XsmomStateConflict during dispatch → log warning + break (no mark_failed)."""
    fp = _make_fp(fp_id=3, state=XsmomState.NEW)

    strategy, xsmom_repo, _ = _make_strategy(mocker)
    xsmom_repo.list_non_terminal = mocker.AsyncMock(return_value=[fp])
    xsmom_repo.get = mocker.AsyncMock()

    strategy._state_machine.step = mocker.AsyncMock(
        side_effect=XsmomStateConflict(3, XsmomState.NEW, XsmomState.OPENED)
    )

    await strategy.advance_all_pending()  # must not raise

    # get should NOT be called (break before refetch)
    xsmom_repo.get.assert_not_awaited()
    xsmom_repo.mark_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_advance_one_generic_exception_marks_failed(mocker):
    """Generic exception during dispatch → mark_failed + publish xsmom.failed + break."""
    fp = _make_fp(fp_id=4, state=XsmomState.NEW)

    bus = mocker.AsyncMock()
    bus.publish = mocker.AsyncMock()

    exchange = mocker.AsyncMock()
    xsmom_repo = mocker.AsyncMock()
    settings = mocker.MagicMock()

    strategy = XsmomStrategy(
        strategy_id=1,
        exchange=exchange,
        xsmom_repo=xsmom_repo,
        session_factory=mocker.MagicMock(),
        params=_make_params(),
        settings=settings,
        event_bus=bus,
    )

    xsmom_repo.list_non_terminal = mocker.AsyncMock(return_value=[fp])
    strategy._state_machine.step = mocker.AsyncMock(
        side_effect=RuntimeError("simulated exchange error")
    )

    await strategy.advance_all_pending()  # must not raise

    xsmom_repo.mark_failed.assert_awaited_once_with(fp.id, reason=mocker.ANY)
    bus.publish.assert_awaited_once()
    published = bus.publish.await_args.args[0]
    assert published.level == "ERROR"
    assert published.kind == "xsmom.failed"


@pytest.mark.asyncio
async def test_advance_one_resting_state_stops_immediately(mocker):
    """OPENED is non-transient → _advance_one returns immediately, step not called."""
    fp = _make_fp(fp_id=5, state=XsmomState.OPENED)

    strategy, xsmom_repo, _ = _make_strategy(mocker)
    xsmom_repo.list_non_terminal = mocker.AsyncMock(return_value=[fp])

    mock_step = mocker.AsyncMock()
    strategy._state_machine.step = mock_step

    await strategy.advance_all_pending()

    mock_step.assert_not_awaited()


# ── on_minute_tick ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_minute_tick_calls_advance_all(mocker):
    """on_minute_tick delegates to advance_all_pending."""
    strategy, xsmom_repo, _ = _make_strategy(mocker)
    xsmom_repo.list_non_terminal = mocker.AsyncMock(return_value=[])

    await strategy.on_minute_tick(now_ms=_NOW_MS)

    xsmom_repo.list_non_terminal.assert_awaited_once_with(1)


# ── on_hour_tick ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_hour_tick_paused_accrues_only(mocker):
    """Paused strategy → accrue only, watchdog not called."""
    strategy, xsmom_repo, exchange = _make_strategy(mocker)
    xsmom_repo.list_active = mocker.AsyncMock(return_value=[])

    watchdog = mocker.AsyncMock()
    strategy._margin_watchdog = watchdog

    # Fake the Strategy DB row with status="paused"
    strat_row = mocker.MagicMock()
    strat_row.status = "paused"
    mock_session = mocker.AsyncMock()
    mock_session.get.return_value = strat_row
    mock_session.__aenter__ = mocker.AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = mocker.AsyncMock(return_value=False)

    sf = mocker.MagicMock()
    sf.return_value = mock_session
    strategy._sf = sf

    mocker.patch(
        "frab.strategy.xsmom.strategy.session_scope",
        return_value=mock_session,
    )

    await strategy.on_hour_tick(now_ms=_NOW_MS)

    # Watchdog NOT called when paused
    watchdog.run_check.assert_not_awaited()
    # Accrue was called (list_active fetch)
    xsmom_repo.list_active.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_hour_tick_active_calls_watchdog(mocker):
    """Active strategy → accrue + watchdog run_check called."""
    watchdog = mocker.AsyncMock()
    report = mocker.MagicMock()
    report.actions_taken = []
    watchdog.run_check = mocker.AsyncMock(return_value=report)

    strategy, xsmom_repo, exchange = _make_strategy(mocker)
    xsmom_repo.list_active = mocker.AsyncMock(return_value=[])
    strategy._margin_watchdog = watchdog

    strat_row = mocker.MagicMock()
    strat_row.status = "active"
    mock_session = mocker.AsyncMock()
    mock_session.get.return_value = strat_row
    mock_session.__aenter__ = mocker.AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = mocker.AsyncMock(return_value=False)

    mocker.patch(
        "frab.strategy.xsmom.strategy.session_scope",
        return_value=mock_session,
    )

    await strategy.on_hour_tick(now_ms=_NOW_MS)

    watchdog.run_check.assert_awaited_once_with(now_ms=_NOW_MS)


@pytest.mark.asyncio
async def test_on_hour_tick_watchdog_crash_does_not_propagate(mocker):
    """Watchdog crash → logged, hour_tick continues without raising."""
    watchdog = mocker.AsyncMock()
    watchdog.run_check = mocker.AsyncMock(side_effect=RuntimeError("watchdog boom"))

    strategy, xsmom_repo, exchange = _make_strategy(mocker)
    xsmom_repo.list_active = mocker.AsyncMock(return_value=[])
    strategy._margin_watchdog = watchdog

    strat_row = mocker.MagicMock()
    strat_row.status = "active"
    mock_session = mocker.AsyncMock()
    mock_session.get.return_value = strat_row
    mock_session.__aenter__ = mocker.AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = mocker.AsyncMock(return_value=False)

    mocker.patch(
        "frab.strategy.xsmom.strategy.session_scope",
        return_value=mock_session,
    )

    # Must not raise
    await strategy.on_hour_tick(now_ms=_NOW_MS)


# ── manual_close / close_all ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_manual_close_transitions_opened_to_close(mocker):
    """manual_close(id) on OPENED position → transition OPENED→CLOSE."""
    fp_opened = _make_fp(fp_id=10, state=XsmomState.OPENED)
    fp_close = _make_fp(fp_id=10, state=XsmomState.CLOSE)

    strategy, xsmom_repo, _ = _make_strategy(mocker)
    xsmom_repo.get = mocker.AsyncMock(return_value=fp_opened)
    xsmom_repo.transition = mocker.AsyncMock(return_value=fp_close)

    result = await strategy.manual_close(10)

    xsmom_repo.transition.assert_awaited_once()
    call_kwargs = xsmom_repo.transition.await_args.kwargs
    assert call_kwargs["from_state"] == XsmomState.OPENED
    assert call_kwargs["to_state"] == XsmomState.CLOSE
    assert result.state == XsmomState.CLOSE


@pytest.mark.asyncio
async def test_manual_close_raises_if_not_found(mocker):
    """manual_close on missing id → KeyError."""
    strategy, xsmom_repo, _ = _make_strategy(mocker)
    xsmom_repo.get = mocker.AsyncMock(return_value=None)

    with pytest.raises(KeyError):
        await strategy.manual_close(999)


@pytest.mark.asyncio
async def test_close_all_transitions_all_opened(mocker):
    """close_all → transitions every OPENED position to CLOSE."""
    fp1 = _make_fp(fp_id=1, state=XsmomState.OPENED, coin="BTC")
    fp2 = _make_fp(fp_id=2, state=XsmomState.OPENED, coin="ETH")

    strategy, xsmom_repo, _ = _make_strategy(mocker)
    xsmom_repo.list_in_state = mocker.AsyncMock(return_value=[fp1, fp2])
    xsmom_repo.transition = mocker.AsyncMock(
        side_effect=lambda id, **kw: _make_fp(fp_id=id, state=XsmomState.CLOSE)
    )

    results = await strategy.close_all()

    assert len(results) == 2
    assert xsmom_repo.transition.await_count == 2
    # Verify both transitions were OPENED→CLOSE
    for call in xsmom_repo.transition.await_args_list:
        assert call.kwargs["from_state"] == XsmomState.OPENED
        assert call.kwargs["to_state"] == XsmomState.CLOSE


@pytest.mark.asyncio
async def test_close_all_skips_state_conflict(mocker):
    """close_all: XsmomStateConflict on one position is swallowed; others proceed."""
    fp1 = _make_fp(fp_id=1, state=XsmomState.OPENED, coin="BTC")
    fp2 = _make_fp(fp_id=2, state=XsmomState.OPENED, coin="ETH")

    strategy, xsmom_repo, _ = _make_strategy(mocker)
    xsmom_repo.list_in_state = mocker.AsyncMock(return_value=[fp1, fp2])

    call_count = {"n": 0}

    async def _transition(fp_id, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise XsmomStateConflict(fp_id, XsmomState.OPENED, XsmomState.CLOSE)
        return _make_fp(fp_id=fp_id, state=XsmomState.CLOSE)

    xsmom_repo.transition = mocker.AsyncMock(side_effect=_transition)

    results = await strategy.close_all()

    # Only fp2 succeeded
    assert len(results) == 1
    assert results[0].state == XsmomState.CLOSE


# ── reload_params ─────────────────────────────────────────────────────────────

def test_reload_params_same_params_is_noop(mocker):
    """reload_params with identical params → _build_internals not called again."""
    strategy, _, _ = _make_strategy(mocker)
    original_sm = strategy._state_machine
    params = strategy.params

    strategy.reload_params(params)  # same object, structural ==

    assert strategy._state_machine is original_sm  # no rebuild


def test_reload_params_different_params_rebuilds(mocker):
    """reload_params with different params → state machine is rebuilt."""
    strategy, _, _ = _make_strategy(mocker)
    original_sm = strategy._state_machine

    new_params = XsmomParams(
        budget_cap=2000.0,
        universe=("BTC", "ETH"),
        leverage=2,
    )

    strategy.reload_params(new_params)

    assert strategy.params == new_params
    assert strategy._state_machine is not original_sm
