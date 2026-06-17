"""Unit tests for XsmomMarginWatchdog.

Mirrors frab.engine.tests.test_margin_watchdog structure.
Real MarginManager used (pure logic).

Math recap
----------
virtual_equity  = required_margin + unrealized_pnl + funding_accrued - fees_paid
virtual_maint   = short_size * current_mark * maint_ratio

Fixture: abs(szi)=1.0, mark=100.0, maint_ratio=0.025
  → virtual_maint = 2.5
  → required_margin = target_ratio * 2.5  (all other terms zero)
"""
from __future__ import annotations

import datetime

import pytest

from frab.domain import Side, XsmomPosition, XsmomState
from frab.engine.margin_manager import MarginManager, MarginStatus
from frab.repo.xsmom_repo import XsmomStateConflict
from frab.strategy.xsmom.protection.margin_watchdog import WatchdogReport, XsmomMarginWatchdog

NOW_MS = 1_700_000_000_000
STRATEGY_ID = 1

_SZI = 1.0
_MARK = 100.0
_MAINT_RATIO = 0.025
_VIRTUAL_MAINT = _SZI * _MARK * _MAINT_RATIO  # 2.5


def _make_mgr() -> MarginManager:
    return MarginManager(
        top_up_trigger=2.0,
        forced_close_trigger=1.5,
        healthy_ratio=3.0,
    )


def _required_for_ratio(ratio: float) -> float:
    return ratio * _VIRTUAL_MAINT


def _make_fp(
    fp_id: int,
    coin: str,
    state_data: dict | None = None,
    state: XsmomState = XsmomState.OPENED,
) -> XsmomPosition:
    return XsmomPosition(
        id=fp_id,
        strategy_id=STRATEGY_ID,
        coin=coin,
        side=Side.SHORT,
        state=state,
        state_data=state_data or {},
        perp_position_id=None,
        collateral_position_id=None,
        target_qty=_SZI,
        opened_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc),
        closed_at=None,
    )


def _make_ap(coin: str, *, szi: float = -_SZI, mark: float = _MARK, unrealized_pnl: float = 0.0):
    ap = type("AP", (), {})()
    ap.coin = coin
    ap.szi = szi
    ap.position_value = abs(szi) * mark
    ap.unrealized_pnl = unrealized_pnl
    return ap


def _make_coin_spec(maint_ratio: float = _MAINT_RATIO):
    spec = type("CoinSpec", (), {})()
    spec.maint_ratio = maint_ratio
    return spec


def _make_mock_registry(mocker, coin_spec_maint: float = _MAINT_RATIO):
    """Return a mock CoinRegistry whose get_coin_spec always returns the given maint_ratio."""
    mock_registry = mocker.MagicMock()
    mock_registry.get_coin_spec.return_value = _make_coin_spec(coin_spec_maint)
    return mock_registry


def _make_watchdog(mocker, *, fps, account_value, coin_spec_maint=_MAINT_RATIO):
    """Build XsmomMarginWatchdog with mocked deps. Returns (watchdog, xsmom_repo, event_bus)."""
    mgr = _make_mgr()

    mock_exchange = mocker.MagicMock()
    asset_positions = [_make_ap(fp.coin) for fp in fps]
    perp_state = mocker.MagicMock()
    perp_state.account_value = account_value
    perp_state.asset_positions = asset_positions
    mock_exchange.get_account_snapshot = mocker.AsyncMock(
        return_value=(perp_state, mocker.MagicMock())
    )

    mock_repo = mocker.MagicMock()
    mock_repo.list_active = mocker.AsyncMock(return_value=fps)
    mock_repo.get = mocker.AsyncMock(return_value=fps[0] if fps else None)
    mock_repo.transition = mocker.AsyncMock(return_value=None)

    mock_registry = _make_mock_registry(mocker, coin_spec_maint)

    mock_bus = mocker.MagicMock()
    mock_bus.publish = mocker.AsyncMock()

    watchdog = XsmomMarginWatchdog(
        strategy_id=STRATEGY_ID,
        exchange=mock_exchange,
        xsmom_repo=mock_repo,
        margin_manager=mgr,
        registry=mock_registry,
        event_bus=mock_bus,
    )
    return watchdog, mock_repo, mock_bus


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_healthy_no_actions(mocker):
    """High account_value → HEALTHY → no events, no transitions."""
    fps = [_make_fp(1, "BTC", state_data={"required_margin": _required_for_ratio(4.0)})]
    watchdog, repo, bus = _make_watchdog(mocker, fps=fps, account_value=1000.0)

    report = await watchdog.run_check(now_ms=NOW_MS)

    assert isinstance(report, WatchdogReport)
    assert report.assessment.account_status == MarginStatus.HEALTHY
    assert report.actions_taken == []
    bus.publish.assert_not_called()
    repo.transition.assert_not_called()


@pytest.mark.asyncio
async def test_warning_emits_event_no_close(mocker):
    """WARNING ratio → event emitted, no transition."""
    fps = [
        _make_fp(1, "BTC", state_data={"required_margin": _required_for_ratio(3.0)}),
        _make_fp(2, "ETH", state_data={"required_margin": _required_for_ratio(3.0)}),
    ]

    mgr = _make_mgr()
    mock_exchange = mocker.MagicMock()
    perp_state = mocker.MagicMock()
    total_maint = 2 * _VIRTUAL_MAINT  # 5.0
    perp_state.account_value = 1.75 * total_maint   # ratio=1.75 → WARNING
    perp_state.asset_positions = [_make_ap("BTC"), _make_ap("ETH")]
    mock_exchange.get_account_snapshot = mocker.AsyncMock(
        return_value=(perp_state, mocker.MagicMock())
    )

    mock_repo = mocker.MagicMock()
    mock_repo.list_active = mocker.AsyncMock(return_value=fps)
    mock_repo.transition = mocker.AsyncMock()

    mock_registry = _make_mock_registry(mocker)

    mock_bus = mocker.MagicMock()
    mock_bus.publish = mocker.AsyncMock()

    watchdog = XsmomMarginWatchdog(
        strategy_id=STRATEGY_ID,
        exchange=mock_exchange,
        xsmom_repo=mock_repo,
        margin_manager=mgr,
        registry=mock_registry,
        event_bus=mock_bus,
    )

    report = await watchdog.run_check(now_ms=NOW_MS)

    assert report.assessment.account_status == MarginStatus.WARNING
    assert "event:margin.warning" in report.actions_taken
    assert report.assessment.weakest_fp_id is None
    mock_repo.transition.assert_not_called()
    mock_bus.publish.assert_awaited_once()
    published = mock_bus.publish.call_args[0][0]
    assert published.level == "WARNING"
    assert published.source == "xsmom_margin_watchdog"
    assert published.kind == "margin.warning"


@pytest.mark.asyncio
async def test_forced_close_picks_weakest_virtual(mocker):
    """3 FPs with virtual ratios [3.0, 1.4, 2.5] → weakest (1.4) transitioned to CLOSE."""
    fp1 = _make_fp(1, "BTC", state_data={"required_margin": _required_for_ratio(3.0)})
    fp2 = _make_fp(2, "ETH", state_data={"required_margin": _required_for_ratio(1.4)})
    fp3 = _make_fp(3, "SOL", state_data={"required_margin": _required_for_ratio(2.5)})
    fps = [fp1, fp2, fp3]

    mgr = _make_mgr()
    mock_exchange = mocker.MagicMock()
    perp_state = mocker.MagicMock()
    total_maint = 3 * _VIRTUAL_MAINT   # 7.5
    perp_state.account_value = 1.2 * total_maint   # ratio=1.2 → FORCED_CLOSE
    perp_state.asset_positions = [_make_ap("BTC"), _make_ap("ETH"), _make_ap("SOL")]
    mock_exchange.get_account_snapshot = mocker.AsyncMock(
        return_value=(perp_state, mocker.MagicMock())
    )

    mock_repo = mocker.MagicMock()
    mock_repo.list_active = mocker.AsyncMock(return_value=fps)
    mock_repo.get = mocker.AsyncMock(return_value=fp2)  # weakest is fp2
    mock_repo.transition = mocker.AsyncMock(return_value=None)

    mock_registry = _make_mock_registry(mocker)

    mock_bus = mocker.MagicMock()
    mock_bus.publish = mocker.AsyncMock()

    watchdog = XsmomMarginWatchdog(
        strategy_id=STRATEGY_ID,
        exchange=mock_exchange,
        xsmom_repo=mock_repo,
        margin_manager=mgr,
        registry=mock_registry,
        event_bus=mock_bus,
    )

    report = await watchdog.run_check(now_ms=NOW_MS)

    assert report.assessment.account_status == MarginStatus.FORCED_CLOSE
    assert report.assessment.weakest_fp_id == 2

    mock_repo.transition.assert_awaited_once()
    call_kwargs = mock_repo.transition.call_args
    # positional arg: xsmom_position_id
    assert call_kwargs[0][0] == 2
    # keyword args
    assert call_kwargs[1]["from_state"] == XsmomState.OPENED
    assert call_kwargs[1]["to_state"] == XsmomState.CLOSE

    assert "forced_close:fp=2" in report.actions_taken


@pytest.mark.asyncio
async def test_state_conflict_does_not_crash(mocker):
    """XsmomStateConflict during transition → warning logged, report still returned."""
    fp = _make_fp(1, "BTC", state_data={"required_margin": _required_for_ratio(1.3)})
    fps = [fp]

    mgr = _make_mgr()
    mock_exchange = mocker.MagicMock()
    perp_state = mocker.MagicMock()
    perp_state.account_value = 1.2 * _VIRTUAL_MAINT  # ratio=1.2 → FORCED_CLOSE
    perp_state.asset_positions = [_make_ap("BTC")]
    mock_exchange.get_account_snapshot = mocker.AsyncMock(
        return_value=(perp_state, mocker.MagicMock())
    )

    mock_repo = mocker.MagicMock()
    mock_repo.list_active = mocker.AsyncMock(return_value=fps)
    mock_repo.get = mocker.AsyncMock(return_value=fp)
    mock_repo.transition = mocker.AsyncMock(
        side_effect=XsmomStateConflict(1, XsmomState.OPENED, XsmomState.CLOSE)
    )

    mock_registry = _make_mock_registry(mocker)

    mock_bus = mocker.MagicMock()
    mock_bus.publish = mocker.AsyncMock()

    watchdog = XsmomMarginWatchdog(
        strategy_id=STRATEGY_ID,
        exchange=mock_exchange,
        xsmom_repo=mock_repo,
        margin_manager=mgr,
        registry=mock_registry,
        event_bus=mock_bus,
    )

    report = await watchdog.run_check(now_ms=NOW_MS)

    assert isinstance(report, WatchdogReport)
    assert "forced_close:fp=1" not in report.actions_taken
    assert "event:margin.forced_close" in report.actions_taken


@pytest.mark.asyncio
async def test_long_position_uses_abs_szi(mocker):
    """LONG position (szi > 0) → abs(szi) used correctly for short_size."""
    fp = _make_fp(1, "BTC", state_data={"required_margin": _required_for_ratio(4.0)})

    mgr = _make_mgr()
    mock_exchange = mocker.MagicMock()
    perp_state = mocker.MagicMock()
    perp_state.account_value = 1000.0  # HEALTHY
    # szi > 0 = LONG position; abs should be used
    long_ap = _make_ap("BTC", szi=+_SZI)
    perp_state.asset_positions = [long_ap]
    mock_exchange.get_account_snapshot = mocker.AsyncMock(
        return_value=(perp_state, mocker.MagicMock())
    )

    mock_repo = mocker.MagicMock()
    mock_repo.list_active = mocker.AsyncMock(return_value=[fp])
    mock_repo.transition = mocker.AsyncMock()

    mock_registry = _make_mock_registry(mocker)

    mock_bus = mocker.MagicMock()
    mock_bus.publish = mocker.AsyncMock()

    watchdog = XsmomMarginWatchdog(
        strategy_id=STRATEGY_ID,
        exchange=mock_exchange,
        xsmom_repo=mock_repo,
        margin_manager=mgr,
        registry=mock_registry,
        event_bus=mock_bus,
    )

    report = await watchdog.run_check(now_ms=NOW_MS)

    # virtual_maint should be positive (abs used), account HEALTHY
    assert report.assessment.per_fp[0].virtual_maintenance == pytest.approx(_VIRTUAL_MAINT)
    assert report.assessment.account_status == MarginStatus.HEALTHY


@pytest.mark.asyncio
async def test_dry_assess_does_not_emit_or_transition(mocker):
    """dry_assess() is read-only: no event, no transition."""
    fp = _make_fp(1, "BTC", state_data={"required_margin": _required_for_ratio(0.8)})
    fps = [fp]

    watchdog, mock_repo, mock_bus = _make_watchdog(
        mocker, fps=fps, account_value=0.8 * _VIRTUAL_MAINT
    )

    from frab.engine.margin_manager import AccountAssessment
    result = await watchdog.dry_assess()

    assert isinstance(result, AccountAssessment)
    mock_bus.publish.assert_not_called()
    mock_repo.transition.assert_not_called()


@pytest.mark.asyncio
async def test_missing_asset_position_skipped(mocker):
    """FP whose coin is not in HL asset_positions → warning, FP excluded from snapshots."""
    fp_btc = _make_fp(1, "BTC", state_data={"required_margin": _required_for_ratio(5.0)})
    fp_eth = _make_fp(2, "ETH", state_data={"required_margin": _required_for_ratio(5.0)})
    fps = [fp_btc, fp_eth]

    mgr = _make_mgr()
    mock_exchange = mocker.MagicMock()
    perp_state = mocker.MagicMock()
    perp_state.account_value = 1000.0
    perp_state.asset_positions = [_make_ap("BTC")]  # ETH missing
    mock_exchange.get_account_snapshot = mocker.AsyncMock(
        return_value=(perp_state, mocker.MagicMock())
    )

    mock_repo = mocker.MagicMock()
    mock_repo.list_active = mocker.AsyncMock(return_value=fps)
    mock_repo.transition = mocker.AsyncMock()

    mock_registry = _make_mock_registry(mocker)

    mock_bus = mocker.MagicMock()
    mock_bus.publish = mocker.AsyncMock()

    watchdog = XsmomMarginWatchdog(
        strategy_id=STRATEGY_ID,
        exchange=mock_exchange,
        xsmom_repo=mock_repo,
        margin_manager=mgr,
        registry=mock_registry,
        event_bus=mock_bus,
    )

    report = await watchdog.run_check(now_ms=NOW_MS)

    assert len(report.assessment.per_fp) == 1
    assert report.assessment.per_fp[0].coin == "BTC"
    assert report.assessment.account_status == MarginStatus.HEALTHY
