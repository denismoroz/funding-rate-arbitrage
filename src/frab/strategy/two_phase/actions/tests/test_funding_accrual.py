"""Unit tests for FundingAccrual — fully mocked deps, no DB fixtures."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from frab.domain import FarbPosition, FarbState
from frab.strategy.two_phase.actions.funding_accrual import FundingAccrual
from frab.strategy.two_phase.evaluators.signal import SignalComputer


_NOW_MS = 1_704_067_200_000


def _make_fp(*, id: int = 1, coin: str = "BTC", perp_position_id: int | None = 42,
             state_data: dict | None = None) -> FarbPosition:
    return FarbPosition(
        id=id,
        strategy_id=1,
        coin=coin,
        state=FarbState.OPEN,
        state_data=state_data or {},
        spot_position_id=None,
        perp_position_id=perp_position_id,
        margin_position_id=None,
        opened_at=datetime.fromtimestamp(_NOW_MS / 1000, tz=timezone.utc),
        closed_at=None,
    )


def _make_accrual(mocker, *, open_fps=None, accrued_funding=100.0,
                  accrued_side_effect=None, signal_value=0.20):
    exchange = mocker.AsyncMock()
    if accrued_side_effect is not None:
        exchange.get_accrued_funding.side_effect = accrued_side_effect
    else:
        exchange.get_accrued_funding.return_value = accrued_funding

    farb_repo = mocker.AsyncMock()
    farb_repo.list_open.return_value = open_fps or []

    sf = mocker.MagicMock()

    signal_computer = mocker.AsyncMock(spec=SignalComputer)
    signal_computer.compute.return_value = signal_value

    accrual = FundingAccrual(
        strategy_id=1,
        exchange=exchange,
        farb_repo=farb_repo,
        session_factory=sf,
        signal_computer=signal_computer,
    )
    return accrual, exchange, farb_repo, signal_computer


# ─── Tests ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_skips_fp_with_no_perp_position_id(mocker):
    """FP with perp_position_id=None is skipped; exchange not called."""
    fp = _make_fp(perp_position_id=None)
    accrual, exchange, farb_repo, _ = _make_accrual(mocker, open_fps=[fp])

    await accrual.accrue(now_ms=_NOW_MS)

    exchange.get_accrued_funding.assert_not_called()
    farb_repo.update_state_data.assert_not_called()


@pytest.mark.asyncio
async def test_accrues_funding_and_updates_state_data(mocker):
    """For an OPEN FP: calls get_accrued_funding, updates state_data with gross + signal."""
    fp = _make_fp(perp_position_id=42, state_data={"gross_funding_so_far": 0.0})
    gross_value = 7.5
    signal_value = 0.35

    accrual, exchange, farb_repo, signal_computer = _make_accrual(
        mocker,
        open_fps=[fp],
        accrued_funding=gross_value,
        signal_value=signal_value,
    )

    # Patch load_position to return a fake perp Position
    fake_pos = mocker.MagicMock()
    mocker.patch(
        "frab.strategy.two_phase.actions.funding_accrual.load_position",
        new_callable=mocker.AsyncMock,
        return_value=fake_pos,
    )

    await accrual.accrue(now_ms=_NOW_MS)

    exchange.get_accrued_funding.assert_awaited_once_with(fake_pos)
    farb_repo.update_state_data.assert_awaited_once()

    sd_arg = farb_repo.update_state_data.call_args.args[1]
    assert sd_arg["gross_funding_so_far"] == pytest.approx(gross_value)
    assert sd_arg["current_signal_apr"] == pytest.approx(signal_value)


@pytest.mark.asyncio
async def test_exception_from_exchange_is_swallowed_and_continues(mocker):
    """If get_accrued_funding raises, logs warning and continues to next FP."""
    fp1 = _make_fp(id=1, coin="BTC", perp_position_id=10)
    fp2 = _make_fp(id=2, coin="ETH", perp_position_id=20)

    call_count = {"n": 0}

    async def _failing_funding(pos):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("exchange error")
        return 5.0

    accrual, exchange, farb_repo, _ = _make_accrual(
        mocker,
        open_fps=[fp1, fp2],
        accrued_side_effect=_failing_funding,
    )

    mocker.patch(
        "frab.strategy.two_phase.actions.funding_accrual.load_position",
        new_callable=mocker.AsyncMock,
        return_value=mocker.MagicMock(),
    )

    # Should not raise
    await accrual.accrue(now_ms=_NOW_MS)

    # Both FPs were attempted; second one succeeded → update_state_data called once
    assert exchange.get_accrued_funding.call_count == 2
    assert farb_repo.update_state_data.call_count == 1
