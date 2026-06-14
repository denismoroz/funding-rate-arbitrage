"""Unit tests for XsmomFundingAccrual."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from frab.domain import Side, XsmomPosition, XsmomState
from frab.strategy.xsmom.actions.funding_accrual import XsmomFundingAccrual

_NOW_MS = 1_704_067_200_000


def _make_fp(
    *,
    fp_id: int = 1,
    coin: str = "BTC",
    perp_position_id: int | None = 42,
    state_data: dict | None = None,
) -> XsmomPosition:
    return XsmomPosition(
        id=fp_id,
        strategy_id=1,
        coin=coin,
        side=Side.SHORT,
        state=XsmomState.OPENED,
        state_data=state_data or {},
        perp_position_id=perp_position_id,
        collateral_position_id=10,
        target_qty=0.01,
        opened_at=datetime.fromtimestamp(_NOW_MS / 1000, tz=timezone.utc),
        closed_at=None,
    )


def _make_accrual(mocker, *, open_fps=None, accrued_funding=100.0, accrued_side_effect=None):
    exchange = mocker.AsyncMock()
    if accrued_side_effect is not None:
        exchange.get_accrued_funding.side_effect = accrued_side_effect
    else:
        exchange.get_accrued_funding.return_value = accrued_funding

    xsmom_repo = mocker.AsyncMock()
    xsmom_repo.list_active.return_value = open_fps or []

    sf = mocker.MagicMock()

    accrual = XsmomFundingAccrual(
        strategy_id=1,
        exchange=exchange,
        xsmom_repo=xsmom_repo,
        session_factory=sf,
    )
    return accrual, exchange, xsmom_repo


@pytest.mark.asyncio
async def test_skips_fp_with_no_perp_position_id(mocker):
    """FP with perp_position_id=None is skipped; exchange not called."""
    fp = _make_fp(perp_position_id=None)
    accrual, exchange, xsmom_repo = _make_accrual(mocker, open_fps=[fp])

    await accrual.accrue(now_ms=_NOW_MS)

    exchange.get_accrued_funding.assert_not_called()
    xsmom_repo.update_state_data.assert_not_called()


@pytest.mark.asyncio
async def test_accrues_funding_and_updates_state_data(mocker):
    """For OPENED FP: calls get_accrued_funding, updates gross_funding_so_far."""
    fp = _make_fp(perp_position_id=42, state_data={"gross_funding_so_far": 0.0})
    gross_value = 12.5

    accrual, exchange, xsmom_repo = _make_accrual(
        mocker, open_fps=[fp], accrued_funding=gross_value
    )

    fake_pos = mocker.MagicMock()
    mocker.patch(
        "frab.strategy.xsmom.actions.funding_accrual.load_position",
        new_callable=mocker.AsyncMock,
        return_value=fake_pos,
    )

    await accrual.accrue(now_ms=_NOW_MS)

    exchange.get_accrued_funding.assert_awaited_once_with(fake_pos, full=True)
    xsmom_repo.update_state_data.assert_awaited_once()

    sd_arg = xsmom_repo.update_state_data.call_args.args[1]
    assert sd_arg["gross_funding_so_far"] == pytest.approx(gross_value)


@pytest.mark.asyncio
async def test_exception_from_exchange_is_swallowed(mocker):
    """get_accrued_funding raises → logs warning, continues to next FP."""
    fp1 = _make_fp(fp_id=1, coin="BTC", perp_position_id=10)
    fp2 = _make_fp(fp_id=2, coin="ETH", perp_position_id=20)

    call_count = {"n": 0}

    async def _failing(pos, *, full: bool = False):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("exchange error")
        return 5.0

    accrual, exchange, xsmom_repo = _make_accrual(
        mocker, open_fps=[fp1, fp2], accrued_side_effect=_failing
    )

    mocker.patch(
        "frab.strategy.xsmom.actions.funding_accrual.load_position",
        new_callable=mocker.AsyncMock,
        return_value=mocker.MagicMock(),
    )

    await accrual.accrue(now_ms=_NOW_MS)

    assert exchange.get_accrued_funding.call_count == 2
    assert xsmom_repo.update_state_data.call_count == 1


@pytest.mark.asyncio
async def test_first_accrue_is_full_sweep(mocker):
    """First call → full=True, _last_full_sweep_ms set."""
    fp = _make_fp(perp_position_id=42)
    accrual, exchange, _ = _make_accrual(mocker, open_fps=[fp])

    mocker.patch(
        "frab.strategy.xsmom.actions.funding_accrual.load_position",
        new_callable=mocker.AsyncMock,
        return_value=mocker.MagicMock(),
    )

    assert accrual._last_full_sweep_ms is None
    await accrual.accrue(now_ms=_NOW_MS)

    _, kwargs = exchange.get_accrued_funding.call_args
    assert kwargs.get("full") is True
    assert accrual._last_full_sweep_ms == _NOW_MS


@pytest.mark.asyncio
async def test_second_accrue_within_24h_is_incremental(mocker):
    """Second call within 24 h → full=False."""
    fp = _make_fp(perp_position_id=42)
    accrual, exchange, _ = _make_accrual(mocker, open_fps=[fp])

    mocker.patch(
        "frab.strategy.xsmom.actions.funding_accrual.load_position",
        new_callable=mocker.AsyncMock,
        return_value=mocker.MagicMock(),
    )

    await accrual.accrue(now_ms=_NOW_MS)
    one_hour_ms = 60 * 60 * 1000
    await accrual.accrue(now_ms=_NOW_MS + one_hour_ms)

    calls = exchange.get_accrued_funding.call_args_list
    assert len(calls) == 2
    _, second_kwargs = calls[1]
    assert second_kwargs.get("full") is False
    assert accrual._last_full_sweep_ms == _NOW_MS


@pytest.mark.asyncio
async def test_accrue_after_24h_is_full_sweep_again(mocker):
    """Call >= 24 h after last full sweep → full=True again."""
    fp = _make_fp(perp_position_id=42)
    accrual, exchange, _ = _make_accrual(mocker, open_fps=[fp])

    mocker.patch(
        "frab.strategy.xsmom.actions.funding_accrual.load_position",
        new_callable=mocker.AsyncMock,
        return_value=mocker.MagicMock(),
    )

    await accrual.accrue(now_ms=_NOW_MS)
    twenty_four_h_ms = 24 * 60 * 60 * 1000
    second_now = _NOW_MS + twenty_four_h_ms
    await accrual.accrue(now_ms=second_now)

    calls = exchange.get_accrued_funding.call_args_list
    _, second_kwargs = calls[1]
    assert second_kwargs.get("full") is True
    assert accrual._last_full_sweep_ms == second_now
