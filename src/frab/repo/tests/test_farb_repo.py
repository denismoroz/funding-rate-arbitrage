"""Unit tests for FarbRepo — all 12+ required cases."""
from datetime import datetime, timezone

import pytest

from frab.domain import FarbPosition, FarbState, Instrument
from frab.repo.farb_repo import FarbRepo, StateConflict


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _repo(session_factory) -> FarbRepo:
    return FarbRepo(session_factory)


# ---------------------------------------------------------------------------
# 1. create returns FarbPosition with id populated and initial state
# ---------------------------------------------------------------------------

async def test_create_returns_domain_with_id(session_factory, strategy_id):
    repo = _repo(session_factory)
    fp = await repo.create(strategy_id=strategy_id, coin="BTC")

    assert fp.id is not None
    assert isinstance(fp.id, int)
    assert fp.strategy_id == strategy_id
    assert fp.coin == "BTC"
    assert fp.state == FarbState.CHECK_MARGIN
    assert fp.state_data == {}
    assert isinstance(fp.opened_at, datetime)
    assert fp.opened_at.tzinfo == timezone.utc
    assert fp.closed_at is None


async def test_create_with_custom_initial_state(session_factory, strategy_id):
    repo = _repo(session_factory)
    fp = await repo.create(
        strategy_id=strategy_id,
        coin="ETH",
        initial_state=FarbState.OPENING_MARGIN,
        state_data={"phase": "test"},
    )

    assert fp.state == FarbState.OPENING_MARGIN
    assert fp.state_data == {"phase": "test"}


# ---------------------------------------------------------------------------
# 2. get round-trips correctly (state_data JSON preserved)
# ---------------------------------------------------------------------------

async def test_get_round_trips(session_factory, strategy_id):
    repo = _repo(session_factory)
    created = await repo.create(
        strategy_id=strategy_id,
        coin="SOL",
        state_data={"min_hold_hours": 12, "consec_negative": 0},
    )

    fetched = await repo.get(created.id)

    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.coin == "SOL"
    assert fetched.state == FarbState.CHECK_MARGIN
    assert fetched.state_data == {"min_hold_hours": 12, "consec_negative": 0}
    assert fetched.opened_at == created.opened_at


# ---------------------------------------------------------------------------
# 3. get returns None for unknown id
# ---------------------------------------------------------------------------

async def test_get_returns_none_for_unknown_id(session_factory):
    repo = _repo(session_factory)
    result = await repo.get(999999)
    assert result is None


# ---------------------------------------------------------------------------
# 4. list_non_terminal excludes CLOSED/FAILED; includes PRE/POST/transient
# ---------------------------------------------------------------------------

async def test_list_non_terminal_excludes_terminal_states(session_factory, strategy_id):
    repo = _repo(session_factory)

    # Create one in each terminal state and a few non-terminal states
    fp_check = await repo.create(
        strategy_id=strategy_id, coin="BTC", initial_state=FarbState.CHECK_MARGIN
    )
    fp_opening = await repo.create(
        strategy_id=strategy_id, coin="ETH", initial_state=FarbState.OPENING_LONG
    )
    # Transition one to PRE_BREAKEVEN (active resting state — non-terminal)
    fp_pre = await repo.create(
        strategy_id=strategy_id, coin="SOL", initial_state=FarbState.CHECK_MARGIN
    )
    await repo.transition(
        fp_pre.id,
        from_state=FarbState.CHECK_MARGIN,
        to_state=FarbState.PRE_BREAKEVEN,
    )
    # Mark one as CLOSED
    fp_closed = await repo.create(
        strategy_id=strategy_id, coin="AVAX", initial_state=FarbState.CHECK_MARGIN
    )
    await repo.mark_closed(fp_closed.id)

    # Mark one as FAILED
    fp_failed = await repo.create(
        strategy_id=strategy_id, coin="LINK", initial_state=FarbState.CHECK_MARGIN
    )
    await repo.mark_failed(fp_failed.id, reason="test failure")

    non_terminal = await repo.list_non_terminal(strategy_id)
    non_terminal_ids = {fp.id for fp in non_terminal}

    assert fp_check.id in non_terminal_ids
    assert fp_opening.id in non_terminal_ids
    assert fp_pre.id in non_terminal_ids       # PRE_BREAKEVEN is non-terminal
    assert fp_closed.id not in non_terminal_ids  # CLOSED excluded
    assert fp_failed.id not in non_terminal_ids  # FAILED excluded


# ---------------------------------------------------------------------------
# 5. list_active only returns PRE_BREAKEVEN and POST_BREAKEVEN
# ---------------------------------------------------------------------------

async def test_list_active_only_returns_active_states(session_factory, strategy_id):
    repo = _repo(session_factory)

    fp_pre = await repo.create(
        strategy_id=strategy_id, coin="BTC", initial_state=FarbState.CHECK_MARGIN
    )
    await repo.transition(
        fp_pre.id,
        from_state=FarbState.CHECK_MARGIN,
        to_state=FarbState.PRE_BREAKEVEN,
    )

    fp_post = await repo.create(
        strategy_id=strategy_id, coin="ETH", initial_state=FarbState.CHECK_MARGIN
    )
    await repo.transition(
        fp_post.id,
        from_state=FarbState.CHECK_MARGIN,
        to_state=FarbState.POST_BREAKEVEN,
    )

    # A transient (non-active, non-terminal) position
    fp_other = await repo.create(
        strategy_id=strategy_id, coin="SOL", initial_state=FarbState.OPENING_LONG
    )

    active_list = await repo.list_active(strategy_id)
    active_ids = {fp.id for fp in active_list}

    assert fp_pre.id in active_ids
    assert fp_post.id in active_ids
    assert fp_other.id not in active_ids
    assert all(fp.state in (FarbState.PRE_BREAKEVEN, FarbState.POST_BREAKEVEN) for fp in active_list)


# ---------------------------------------------------------------------------
# 6. transition happy path
# ---------------------------------------------------------------------------

async def test_transition_happy_path(session_factory, strategy_id):
    repo = _repo(session_factory)
    fp = await repo.create(
        strategy_id=strategy_id,
        coin="BTC",
        initial_state=FarbState.CHECK_MARGIN,
    )

    updated = await repo.transition(
        fp.id,
        from_state=FarbState.CHECK_MARGIN,
        to_state=FarbState.OPENING_MARGIN,
        state_data={"attempt": 1},
    )

    assert updated.id == fp.id
    assert updated.state == FarbState.OPENING_MARGIN
    assert updated.state_data == {"attempt": 1}

    # Verify persisted
    fetched = await repo.get(fp.id)
    assert fetched.state == FarbState.OPENING_MARGIN
    assert fetched.state_data == {"attempt": 1}


async def test_transition_without_state_data_preserves_existing(session_factory, strategy_id):
    repo = _repo(session_factory)
    fp = await repo.create(
        strategy_id=strategy_id,
        coin="ETH",
        initial_state=FarbState.CHECK_MARGIN,
        state_data={"existing_key": "value"},
    )

    updated = await repo.transition(
        fp.id,
        from_state=FarbState.CHECK_MARGIN,
        to_state=FarbState.OPENING_MARGIN,
        # no state_data passed
    )

    # state_data unchanged when not supplied
    assert updated.state_data == {"existing_key": "value"}


# ---------------------------------------------------------------------------
# 7. transition StateConflict: wrong from_state, row unchanged
# ---------------------------------------------------------------------------

async def test_transition_state_conflict_wrong_from_state(session_factory, strategy_id):
    repo = _repo(session_factory)
    fp = await repo.create(
        strategy_id=strategy_id,
        coin="BTC",
        initial_state=FarbState.CHECK_MARGIN,
    )

    with pytest.raises(StateConflict) as exc_info:
        await repo.transition(
            fp.id,
            from_state=FarbState.OPENING_LONG,   # wrong — actual is CHECK_MARGIN
            to_state=FarbState.OPENING_SHORT,
        )

    err = exc_info.value
    assert err.farb_position_id == fp.id
    assert err.expected == FarbState.OPENING_LONG
    assert err.actual == FarbState.CHECK_MARGIN

    # Row must be unchanged
    fetched = await repo.get(fp.id)
    assert fetched.state == FarbState.CHECK_MARGIN


# ---------------------------------------------------------------------------
# 8. transition StateConflict for missing id (actual=None)
# ---------------------------------------------------------------------------

async def test_transition_state_conflict_missing_id(session_factory):
    repo = _repo(session_factory)

    with pytest.raises(StateConflict) as exc_info:
        await repo.transition(
            999999,
            from_state=FarbState.CHECK_MARGIN,
            to_state=FarbState.OPENING_MARGIN,
        )

    err = exc_info.value
    assert err.farb_position_id == 999999
    assert err.actual is None


# ---------------------------------------------------------------------------
# 9. set_leg correctly populates spot/perp/margin position_id
# ---------------------------------------------------------------------------

async def test_set_leg_spot(session_factory, strategy_id, real_position_id):
    repo = _repo(session_factory)
    fp = await repo.create(strategy_id=strategy_id, coin="BTC")

    updated = await repo.set_leg(fp.id, instrument=Instrument.SPOT, position_id=real_position_id)

    assert updated.spot_position_id == real_position_id
    assert updated.perp_position_id is None
    assert updated.margin_position_id is None


async def test_set_leg_perp(session_factory, strategy_id, real_position_id):
    repo = _repo(session_factory)
    fp = await repo.create(strategy_id=strategy_id, coin="BTC")

    updated = await repo.set_leg(fp.id, instrument=Instrument.PERP, position_id=real_position_id)

    assert updated.perp_position_id == real_position_id
    assert updated.spot_position_id is None
    assert updated.margin_position_id is None


async def test_set_leg_collateral(session_factory, strategy_id, real_position_id):
    repo = _repo(session_factory)
    fp = await repo.create(strategy_id=strategy_id, coin="BTC")

    updated = await repo.set_leg(fp.id, instrument=Instrument.COLLATERAL, position_id=real_position_id)

    assert updated.margin_position_id == real_position_id
    assert updated.spot_position_id is None
    assert updated.perp_position_id is None


# ---------------------------------------------------------------------------
# 10. update_state_data preserves state, replaces state_data dict
# ---------------------------------------------------------------------------

async def test_update_state_data(session_factory, strategy_id):
    repo = _repo(session_factory)
    fp = await repo.create(
        strategy_id=strategy_id,
        coin="ETH",
        initial_state=FarbState.OPENING_LONG,
        state_data={"old_key": "old_value"},
    )

    updated = await repo.update_state_data(fp.id, {"new_key": 123, "phase": 2})

    assert updated.state == FarbState.OPENING_LONG   # state unchanged
    assert updated.state_data == {"new_key": 123, "phase": 2}
    assert "old_key" not in updated.state_data

    # Verify persisted
    fetched = await repo.get(fp.id)
    assert fetched.state_data == {"new_key": 123, "phase": 2}


# ---------------------------------------------------------------------------
# 11. mark_closed: sets state=CLOSED + closed_at not None
# ---------------------------------------------------------------------------

async def test_mark_closed(session_factory, strategy_id):
    repo = _repo(session_factory)
    fp = await repo.create(
        strategy_id=strategy_id,
        coin="BTC",
        initial_state=FarbState.RELEASING_MARGIN,
    )

    closed = await repo.mark_closed(fp.id)

    assert closed.state == FarbState.CLOSED
    assert closed.closed_at is not None
    assert isinstance(closed.closed_at, datetime)
    assert closed.closed_at.tzinfo == timezone.utc

    # Verify persisted
    fetched = await repo.get(fp.id)
    assert fetched.state == FarbState.CLOSED
    assert fetched.closed_at is not None


async def test_mark_closed_raises_if_already_closed(session_factory, strategy_id):
    repo = _repo(session_factory)
    fp = await repo.create(strategy_id=strategy_id, coin="BTC")
    await repo.mark_closed(fp.id)

    with pytest.raises(StateConflict):
        await repo.mark_closed(fp.id)


# ---------------------------------------------------------------------------
# 12. mark_failed: sets state=FAILED + closed_at + failure_reason in state_data
# ---------------------------------------------------------------------------

async def test_mark_failed(session_factory, strategy_id):
    repo = _repo(session_factory)
    fp = await repo.create(
        strategy_id=strategy_id,
        coin="ETH",
        initial_state=FarbState.OPENING_SHORT,
        state_data={"some_context": "value"},
    )

    failed = await repo.mark_failed(fp.id, reason="order rejected")

    assert failed.state == FarbState.FAILED
    assert failed.closed_at is not None
    assert isinstance(failed.closed_at, datetime)
    assert failed.state_data["failure_reason"] == "order rejected"
    # Existing keys should still be present
    assert failed.state_data["some_context"] == "value"

    # Verify persisted
    fetched = await repo.get(fp.id)
    assert fetched.state == FarbState.FAILED
    assert fetched.closed_at is not None
    assert fetched.state_data["failure_reason"] == "order rejected"


async def test_mark_failed_raises_if_already_failed(session_factory, strategy_id):
    repo = _repo(session_factory)
    fp = await repo.create(strategy_id=strategy_id, coin="BTC")
    await repo.mark_failed(fp.id, reason="first failure")

    with pytest.raises(StateConflict):
        await repo.mark_failed(fp.id, reason="second failure")


async def test_mark_failed_raises_if_already_closed(session_factory, strategy_id):
    repo = _repo(session_factory)
    fp = await repo.create(strategy_id=strategy_id, coin="BTC")
    await repo.mark_closed(fp.id)

    with pytest.raises(StateConflict):
        await repo.mark_failed(fp.id, reason="too late")


# ---------------------------------------------------------------------------
# Extra: list_in_state
# ---------------------------------------------------------------------------

async def test_list_in_state(session_factory, strategy_id):
    repo = _repo(session_factory)

    await repo.create(
        strategy_id=strategy_id, coin="BTC", initial_state=FarbState.OPENING_LONG
    )
    await repo.create(
        strategy_id=strategy_id, coin="ETH", initial_state=FarbState.OPENING_LONG
    )
    await repo.create(
        strategy_id=strategy_id, coin="SOL", initial_state=FarbState.OPENING_SHORT
    )

    result = await repo.list_in_state(strategy_id, FarbState.OPENING_LONG)

    assert len(result) == 2
    assert all(fp.state == FarbState.OPENING_LONG for fp in result)
    coins = {fp.coin for fp in result}
    assert coins == {"BTC", "ETH"}


# ---------------------------------------------------------------------------
# Extra: list_by_coin
# ---------------------------------------------------------------------------

async def test_list_by_coin_excludes_terminal_by_default(session_factory, strategy_id):
    repo = _repo(session_factory)

    fp_active = await repo.create(
        strategy_id=strategy_id, coin="BTC", initial_state=FarbState.OPENING_LONG
    )
    fp_closed = await repo.create(
        strategy_id=strategy_id, coin="BTC", initial_state=FarbState.CHECK_MARGIN
    )
    await repo.mark_closed(fp_closed.id)

    result = await repo.list_by_coin(strategy_id, "BTC")
    result_ids = {fp.id for fp in result}

    assert fp_active.id in result_ids
    assert fp_closed.id not in result_ids


async def test_list_by_coin_include_terminal(session_factory, strategy_id):
    repo = _repo(session_factory)

    fp_active = await repo.create(
        strategy_id=strategy_id, coin="ETH", initial_state=FarbState.OPENING_LONG
    )
    fp_closed = await repo.create(
        strategy_id=strategy_id, coin="ETH", initial_state=FarbState.CHECK_MARGIN
    )
    await repo.mark_closed(fp_closed.id)

    result = await repo.list_by_coin(strategy_id, "ETH", include_terminal=True)
    result_ids = {fp.id for fp in result}

    assert fp_active.id in result_ids
    assert fp_closed.id in result_ids


# ---------------------------------------------------------------------------
# Extra: state_conflict error message
# ---------------------------------------------------------------------------

async def test_state_conflict_error_message(session_factory, strategy_id):
    repo = _repo(session_factory)
    fp = await repo.create(strategy_id=strategy_id, coin="BTC")

    with pytest.raises(StateConflict) as exc_info:
        await repo.transition(
            fp.id,
            from_state=FarbState.PRE_BREAKEVEN,
            to_state=FarbState.CLOSING_SHORT,
        )

    msg = str(exc_info.value)
    assert str(fp.id) in msg
    assert "pre_breakeven" in msg
    assert "check_margin" in msg
