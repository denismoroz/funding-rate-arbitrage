"""Unit tests for XsmomRepo — all required cases."""
from datetime import datetime, timezone

import pytest

from frab.domain import Side, XsmomPosition, XsmomState
from frab.repo.xsmom_repo import XsmomRepo, XsmomStateConflict


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _repo(session_factory) -> XsmomRepo:
    return XsmomRepo(session_factory)


# ---------------------------------------------------------------------------
# 1. create → get roundtrip
# ---------------------------------------------------------------------------

async def test_create_returns_domain_with_id(session_factory, strategy_id):
    repo = _repo(session_factory)
    xp = await repo.create(strategy_id=strategy_id, coin="BTC", side=Side.LONG)

    assert xp.id is not None
    assert isinstance(xp.id, int)
    assert xp.strategy_id == strategy_id
    assert xp.coin == "BTC"
    assert xp.side == Side.LONG
    assert xp.state == XsmomState.NEW
    assert xp.state_data == {}
    assert xp.perp_position_id is None
    assert xp.collateral_position_id is None
    assert xp.target_qty is None
    assert isinstance(xp.opened_at, datetime)
    assert xp.opened_at.tzinfo == timezone.utc
    assert xp.closed_at is None


async def test_create_with_all_optional_fields(session_factory, strategy_id):
    repo = _repo(session_factory)
    xp = await repo.create(
        strategy_id=strategy_id,
        coin="ETH",
        side=Side.SHORT,
        target_qty=1.5,
        initial_state=XsmomState.OPENED,
        state_data={"rank": 3},
    )

    assert xp.side == Side.SHORT
    assert xp.state == XsmomState.OPENED
    assert xp.target_qty == 1.5
    assert xp.state_data == {"rank": 3}


async def test_get_round_trips(session_factory, strategy_id):
    repo = _repo(session_factory)
    created = await repo.create(
        strategy_id=strategy_id,
        coin="SOL",
        side=Side.LONG,
        state_data={"rank": 1, "momentum": 0.15},
    )

    fetched = await repo.get(created.id)

    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.coin == "SOL"
    assert fetched.side == Side.LONG
    assert fetched.state == XsmomState.NEW
    assert fetched.state_data == {"rank": 1, "momentum": 0.15}
    assert fetched.opened_at == created.opened_at


async def test_get_returns_none_for_unknown_id(session_factory):
    repo = _repo(session_factory)
    result = await repo.get(999999)
    assert result is None


# ---------------------------------------------------------------------------
# 2. transition happy path
# ---------------------------------------------------------------------------

async def test_transition_happy_path(session_factory, strategy_id):
    repo = _repo(session_factory)
    xp = await repo.create(strategy_id=strategy_id, coin="BTC", side=Side.LONG)

    updated = await repo.transition(
        xp.id,
        from_state=XsmomState.NEW,
        to_state=XsmomState.OPENED,
        state_data={"rank": 2},
    )

    assert updated.id == xp.id
    assert updated.state == XsmomState.OPENED
    assert updated.state_data == {"rank": 2}

    fetched = await repo.get(xp.id)
    assert fetched.state == XsmomState.OPENED
    assert fetched.state_data == {"rank": 2}


async def test_transition_without_state_data_preserves_existing(session_factory, strategy_id):
    repo = _repo(session_factory)
    xp = await repo.create(
        strategy_id=strategy_id,
        coin="ETH",
        side=Side.SHORT,
        state_data={"existing": "value"},
    )

    updated = await repo.transition(
        xp.id,
        from_state=XsmomState.NEW,
        to_state=XsmomState.OPENED,
        # no state_data
    )

    assert updated.state_data == {"existing": "value"}


# ---------------------------------------------------------------------------
# 3. transition StateConflict on wrong from_state
# ---------------------------------------------------------------------------

async def test_transition_state_conflict_wrong_from_state(session_factory, strategy_id):
    repo = _repo(session_factory)
    xp = await repo.create(strategy_id=strategy_id, coin="BTC", side=Side.LONG)

    with pytest.raises(XsmomStateConflict) as exc_info:
        await repo.transition(
            xp.id,
            from_state=XsmomState.OPENED,   # wrong — actual is NEW
            to_state=XsmomState.CLOSE,
        )

    err = exc_info.value
    assert err.xsmom_position_id == xp.id
    assert err.expected == XsmomState.OPENED
    assert err.actual == XsmomState.NEW

    # Row must be unchanged
    fetched = await repo.get(xp.id)
    assert fetched.state == XsmomState.NEW


async def test_transition_state_conflict_missing_id(session_factory):
    repo = _repo(session_factory)

    with pytest.raises(XsmomStateConflict) as exc_info:
        await repo.transition(
            999999,
            from_state=XsmomState.NEW,
            to_state=XsmomState.OPENED,
        )

    err = exc_info.value
    assert err.xsmom_position_id == 999999
    assert err.actual is None


# ---------------------------------------------------------------------------
# 4. mark_closed sets closed_at & state
# ---------------------------------------------------------------------------

async def test_mark_closed(session_factory, strategy_id):
    repo = _repo(session_factory)
    xp = await repo.create(
        strategy_id=strategy_id, coin="BTC", side=Side.LONG,
        initial_state=XsmomState.CLOSE,
    )

    closed = await repo.mark_closed(xp.id)

    assert closed.state == XsmomState.CLOSED
    assert closed.closed_at is not None
    assert isinstance(closed.closed_at, datetime)
    assert closed.closed_at.tzinfo == timezone.utc

    fetched = await repo.get(xp.id)
    assert fetched.state == XsmomState.CLOSED
    assert fetched.closed_at is not None


async def test_mark_closed_raises_if_already_closed(session_factory, strategy_id):
    repo = _repo(session_factory)
    xp = await repo.create(strategy_id=strategy_id, coin="BTC", side=Side.LONG)
    await repo.mark_closed(xp.id)

    with pytest.raises(XsmomStateConflict):
        await repo.mark_closed(xp.id)


async def test_mark_closed_raises_if_failed(session_factory, strategy_id):
    repo = _repo(session_factory)
    xp = await repo.create(strategy_id=strategy_id, coin="BTC", side=Side.LONG)
    await repo.mark_failed(xp.id, reason="oops")

    with pytest.raises(XsmomStateConflict):
        await repo.mark_closed(xp.id)


# ---------------------------------------------------------------------------
# 5. mark_failed records reason in state_data
# ---------------------------------------------------------------------------

async def test_mark_failed(session_factory, strategy_id):
    repo = _repo(session_factory)
    xp = await repo.create(
        strategy_id=strategy_id,
        coin="ETH",
        side=Side.SHORT,
        initial_state=XsmomState.OPENED,
        state_data={"some_ctx": "value"},
    )

    failed = await repo.mark_failed(xp.id, reason="order rejected")

    assert failed.state == XsmomState.FAILED
    assert failed.closed_at is not None
    assert isinstance(failed.closed_at, datetime)
    assert failed.state_data["failure_reason"] == "order rejected"
    assert failed.state_data["some_ctx"] == "value"

    fetched = await repo.get(xp.id)
    assert fetched.state == XsmomState.FAILED
    assert fetched.state_data["failure_reason"] == "order rejected"


async def test_mark_failed_raises_if_already_failed(session_factory, strategy_id):
    repo = _repo(session_factory)
    xp = await repo.create(strategy_id=strategy_id, coin="BTC", side=Side.LONG)
    await repo.mark_failed(xp.id, reason="first")

    with pytest.raises(XsmomStateConflict):
        await repo.mark_failed(xp.id, reason="second")


async def test_mark_failed_raises_if_already_closed(session_factory, strategy_id):
    repo = _repo(session_factory)
    xp = await repo.create(strategy_id=strategy_id, coin="BTC", side=Side.LONG)
    await repo.mark_closed(xp.id)

    with pytest.raises(XsmomStateConflict):
        await repo.mark_failed(xp.id, reason="too late")


# ---------------------------------------------------------------------------
# 6. set_leg sets perp/collateral ids
# ---------------------------------------------------------------------------

async def test_set_leg_perp(session_factory, strategy_id, real_position_id):
    repo = _repo(session_factory)
    xp = await repo.create(strategy_id=strategy_id, coin="BTC", side=Side.LONG)

    updated = await repo.set_leg(xp.id, perp_position_id=real_position_id)

    assert updated.perp_position_id == real_position_id
    assert updated.collateral_position_id is None


async def test_set_leg_collateral(session_factory, strategy_id, real_position_id):
    repo = _repo(session_factory)
    xp = await repo.create(strategy_id=strategy_id, coin="BTC", side=Side.LONG)

    updated = await repo.set_leg(xp.id, collateral_position_id=real_position_id)

    assert updated.collateral_position_id == real_position_id
    assert updated.perp_position_id is None


async def test_set_leg_both(session_factory, strategy_id, real_position_id):
    repo = _repo(session_factory)
    xp = await repo.create(strategy_id=strategy_id, coin="BTC", side=Side.LONG)

    updated = await repo.set_leg(
        xp.id,
        perp_position_id=real_position_id,
        collateral_position_id=real_position_id,
    )

    assert updated.perp_position_id == real_position_id
    assert updated.collateral_position_id == real_position_id


# ---------------------------------------------------------------------------
# 7. list_non_terminal / list_active / list_in_state filtering
# ---------------------------------------------------------------------------

async def test_list_non_terminal(session_factory, strategy_id):
    repo = _repo(session_factory)

    xp_new = await repo.create(strategy_id=strategy_id, coin="BTC", side=Side.LONG)
    xp_opened = await repo.create(
        strategy_id=strategy_id, coin="ETH", side=Side.SHORT,
        initial_state=XsmomState.OPENED,
    )
    xp_close = await repo.create(
        strategy_id=strategy_id, coin="SOL", side=Side.LONG,
        initial_state=XsmomState.CLOSE,
    )
    xp_closed = await repo.create(strategy_id=strategy_id, coin="AVAX", side=Side.SHORT)
    await repo.mark_closed(xp_closed.id)
    xp_failed = await repo.create(strategy_id=strategy_id, coin="LINK", side=Side.LONG)
    await repo.mark_failed(xp_failed.id, reason="err")

    non_terminal = await repo.list_non_terminal(strategy_id)
    ids = {xp.id for xp in non_terminal}

    assert xp_new.id in ids
    assert xp_opened.id in ids
    assert xp_close.id in ids
    assert xp_closed.id not in ids
    assert xp_failed.id not in ids


async def test_list_active(session_factory, strategy_id):
    repo = _repo(session_factory)

    xp_opened = await repo.create(
        strategy_id=strategy_id, coin="BTC", side=Side.LONG,
        initial_state=XsmomState.OPENED,
    )
    xp_new = await repo.create(strategy_id=strategy_id, coin="ETH", side=Side.SHORT)
    xp_close = await repo.create(
        strategy_id=strategy_id, coin="SOL", side=Side.LONG,
        initial_state=XsmomState.CLOSE,
    )

    active = await repo.list_active(strategy_id)
    ids = {xp.id for xp in active}

    assert xp_opened.id in ids
    assert xp_new.id not in ids
    assert xp_close.id not in ids
    assert all(xp.state == XsmomState.OPENED for xp in active)


async def test_list_in_state(session_factory, strategy_id):
    repo = _repo(session_factory)

    await repo.create(strategy_id=strategy_id, coin="BTC", side=Side.LONG, initial_state=XsmomState.OPENED)
    await repo.create(strategy_id=strategy_id, coin="ETH", side=Side.SHORT, initial_state=XsmomState.OPENED)
    await repo.create(strategy_id=strategy_id, coin="SOL", side=Side.LONG, initial_state=XsmomState.CLOSE)

    opened = await repo.list_in_state(strategy_id, XsmomState.OPENED)
    assert len(opened) == 2
    assert all(xp.state == XsmomState.OPENED for xp in opened)

    close_list = await repo.list_in_state(strategy_id, XsmomState.CLOSE)
    assert len(close_list) == 1
    assert close_list[0].coin == "SOL"


async def test_list_by_coin_excludes_terminal_by_default(session_factory, strategy_id):
    repo = _repo(session_factory)

    xp_active = await repo.create(strategy_id=strategy_id, coin="BTC", side=Side.LONG)
    xp_closed = await repo.create(strategy_id=strategy_id, coin="BTC", side=Side.SHORT)
    await repo.mark_closed(xp_closed.id)

    result = await repo.list_by_coin(strategy_id, "BTC")
    result_ids = {xp.id for xp in result}

    assert xp_active.id in result_ids
    assert xp_closed.id not in result_ids


async def test_list_by_coin_include_terminal(session_factory, strategy_id):
    repo = _repo(session_factory)

    xp_active = await repo.create(strategy_id=strategy_id, coin="ETH", side=Side.LONG)
    xp_closed = await repo.create(strategy_id=strategy_id, coin="ETH", side=Side.SHORT)
    await repo.mark_closed(xp_closed.id)

    result = await repo.list_by_coin(strategy_id, "ETH", include_terminal=True)
    result_ids = {xp.id for xp in result}

    assert xp_active.id in result_ids
    assert xp_closed.id in result_ids


# ---------------------------------------------------------------------------
# 8. record_scan + latest_scans ordering
# ---------------------------------------------------------------------------

async def test_record_scan_and_latest_scans(session_factory, strategy_id):
    repo = _repo(session_factory)

    scan_id1 = await repo.record_scan(
        strategy_id=strategy_id,
        ts_ms=1000,
        ranking=["BTC", "ETH"],
        n_long=1,
        n_short=1,
        note="first",
    )
    scan_id2 = await repo.record_scan(
        strategy_id=strategy_id,
        ts_ms=2000,
        ranking=["SOL", "AVAX"],
        n_long=2,
        n_short=2,
        note=None,
    )
    scan_id3 = await repo.record_scan(
        strategy_id=strategy_id,
        ts_ms=3000,
        ranking=["BTC"],
        n_long=1,
        n_short=0,
    )

    assert isinstance(scan_id1, int)
    assert isinstance(scan_id2, int)
    assert scan_id1 != scan_id2

    scans = await repo.latest_scans(strategy_id, limit=50)

    assert len(scans) == 3
    # Most recent first
    assert scans[0]["ts_ms"] == 3000
    assert scans[1]["ts_ms"] == 2000
    assert scans[2]["ts_ms"] == 1000

    assert scans[2]["note"] == "first"
    assert scans[1]["note"] is None


async def test_latest_scans_limit(session_factory, strategy_id):
    repo = _repo(session_factory)

    for i in range(5):
        await repo.record_scan(
            strategy_id=strategy_id,
            ts_ms=i * 1000,
            ranking=[],
            n_long=0,
            n_short=0,
        )

    scans = await repo.latest_scans(strategy_id, limit=3)
    assert len(scans) == 3
    assert scans[0]["ts_ms"] == 4000
    assert scans[1]["ts_ms"] == 3000
    assert scans[2]["ts_ms"] == 2000


# ---------------------------------------------------------------------------
# 9. upsert_daily_prices idempotency + get_daily_closes ordering
# ---------------------------------------------------------------------------

async def test_upsert_daily_prices_idempotent(session_factory, strategy_id):
    repo = _repo(session_factory)

    rows = [
        ("BTC", 1000, 50000.0),
        ("ETH", 1000, 3000.0),
    ]

    await repo.upsert_daily_prices(rows)
    await repo.upsert_daily_prices(rows)  # second call must not raise or duplicate

    closes = await repo.get_daily_closes(["BTC", "ETH"])
    assert len(closes["BTC"]) == 1
    assert closes["BTC"][0] == (1000, 50000.0)
    assert closes["ETH"][0] == (1000, 3000.0)


async def test_upsert_daily_prices_updates_close(session_factory, strategy_id):
    repo = _repo(session_factory)

    await repo.upsert_daily_prices([("BTC", 1000, 50000.0)])
    await repo.upsert_daily_prices([("BTC", 1000, 51000.0)])  # price update

    closes = await repo.get_daily_closes(["BTC"])
    assert len(closes["BTC"]) == 1
    assert closes["BTC"][0] == (1000, 51000.0)


async def test_get_daily_closes_ordering(session_factory, strategy_id):
    repo = _repo(session_factory)

    await repo.upsert_daily_prices([
        ("BTC", 3000, 53000.0),
        ("BTC", 1000, 51000.0),
        ("BTC", 2000, 52000.0),
    ])

    closes = await repo.get_daily_closes(["BTC"])
    btc = closes["BTC"]

    assert len(btc) == 3
    assert btc[0] == (1000, 51000.0)
    assert btc[1] == (2000, 52000.0)
    assert btc[2] == (3000, 53000.0)


async def test_get_daily_closes_since_day_ms(session_factory, strategy_id):
    repo = _repo(session_factory)

    await repo.upsert_daily_prices([
        ("BTC", 1000, 51000.0),
        ("BTC", 2000, 52000.0),
        ("BTC", 3000, 53000.0),
    ])

    closes = await repo.get_daily_closes(["BTC"], since_day_ms=2000)
    btc = closes["BTC"]

    assert len(btc) == 2
    assert btc[0][0] == 2000
    assert btc[1][0] == 3000


async def test_get_daily_closes_empty_coins(session_factory, strategy_id):
    repo = _repo(session_factory)
    result = await repo.get_daily_closes([])
    assert result == {}


async def test_get_daily_closes_per_coin_independent(session_factory, strategy_id):
    repo = _repo(session_factory)

    await repo.upsert_daily_prices([
        ("BTC", 1000, 50000.0),
        ("BTC", 2000, 51000.0),
        ("ETH", 1000, 3000.0),
    ])

    closes = await repo.get_daily_closes(["BTC", "ETH"])

    assert len(closes["BTC"]) == 2
    assert len(closes["ETH"]) == 1


# ---------------------------------------------------------------------------
# 10. update_state_data
# ---------------------------------------------------------------------------

async def test_update_state_data(session_factory, strategy_id):
    repo = _repo(session_factory)
    xp = await repo.create(
        strategy_id=strategy_id,
        coin="BTC",
        side=Side.LONG,
        state_data={"old": "data"},
    )

    updated = await repo.update_state_data(xp.id, {"new_key": 42})

    assert updated.state == XsmomState.NEW  # state unchanged
    assert updated.state_data == {"new_key": 42}
    assert "old" not in updated.state_data

    fetched = await repo.get(xp.id)
    assert fetched.state_data == {"new_key": 42}


# ---------------------------------------------------------------------------
# 11. is_active property
# ---------------------------------------------------------------------------

async def test_is_active_opened(session_factory, strategy_id):
    repo = _repo(session_factory)
    xp = await repo.create(
        strategy_id=strategy_id, coin="BTC", side=Side.LONG,
        initial_state=XsmomState.OPENED,
    )
    assert xp.is_active is True


async def test_is_active_new(session_factory, strategy_id):
    repo = _repo(session_factory)
    xp = await repo.create(strategy_id=strategy_id, coin="BTC", side=Side.LONG)
    assert xp.is_active is False


# ---------------------------------------------------------------------------
# 12. StateConflict error message content
# ---------------------------------------------------------------------------

async def test_state_conflict_error_message(session_factory, strategy_id):
    repo = _repo(session_factory)
    xp = await repo.create(strategy_id=strategy_id, coin="BTC", side=Side.LONG)

    with pytest.raises(XsmomStateConflict) as exc_info:
        await repo.transition(
            xp.id,
            from_state=XsmomState.OPENED,
            to_state=XsmomState.CLOSE,
        )

    msg = str(exc_info.value)
    assert str(xp.id) in msg
    assert "opened" in msg
    assert "new" in msg
