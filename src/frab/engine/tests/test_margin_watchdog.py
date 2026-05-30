"""Unit tests for MarginWatchdog.

All async deps mocked via mocker.AsyncMock / mocker.MagicMock.
Real MarginManager used (pure logic, no I/O).

Math for target virtual ratios
--------------------------------
virtual_equity  = required_margin + unrealized_pnl + funding_accrued - fees_paid
virtual_maint   = short_size * current_mark * maint_ratio

Fixture uses: short_size=1.0, mark=100.0, maint_ratio=0.025
  → virtual_maint = 2.5
  → required_margin = target_ratio * 2.5  (all other terms zero)
Account equity is set so that account_ratio = desired_account_ratio * total_maint.
"""
from __future__ import annotations

import pytest

from frab.domain import FarbPosition, FarbState
from frab.engine.margin_manager import MarginManager, MarginStatus
from frab.engine.margin_watchdog import MarginWatchdog, WatchdogReport
from frab.repo.farb_repo import StateConflict

# ── Constants ──────────────────────────────────────────────────────────────────

NOW_MS = 1_700_000_000_000
STRATEGY_ID = 1

# Per-FP fixture parameters
_SHORT_SIZE = 1.0
_MARK = 100.0
_MAINT_RATIO = 0.025
_VIRTUAL_MAINT = _SHORT_SIZE * _MARK * _MAINT_RATIO  # = 2.5

_CREDS = dict(
    hl_private_key="0x" + "a" * 64,
    hl_account_address="0x" + "b" * 40,
    _env_file=None,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_mgr() -> MarginManager:
    """Real MarginManager with standard thresholds."""
    return MarginManager(
        top_up_trigger=2.0,
        forced_close_trigger=1.5,
        healthy_ratio=3.0,
    )


def _make_fp(fp_id: int, coin: str, state_data: dict | None = None) -> FarbPosition:
    """Minimal FarbPosition domain object."""
    return FarbPosition(
        id=fp_id,
        strategy_id=STRATEGY_ID,
        coin=coin,
        state=FarbState.OPEN,
        state_data=state_data or {},
        spot_position_id=None,
        perp_position_id=None,
        margin_position_id=None,
        opened_at=__import__("datetime").datetime(2024, 1, 1, tzinfo=__import__("datetime").timezone.utc),
        closed_at=None,
    )


def _make_ap(coin: str, *, szi: float = -_SHORT_SIZE, mark: float = _MARK,
             unrealized_pnl: float = 0.0) -> object:
    """Mock asset position object."""
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


def _required_margin_for_ratio(target_ratio: float) -> float:
    """required_margin so that virtual_ratio == target_ratio (other terms zero)."""
    return target_ratio * _VIRTUAL_MAINT


def _make_watchdog(mocker, *, fps, account_value, coin_spec_maint=_MAINT_RATIO):
    """
    Build a fully wired MarginWatchdog with mocked exchange, farb_repo, settings, event_bus.

    Returns (watchdog, mock_farb_repo, mock_event_bus).
    """
    mgr = _make_mgr()

    # Exchange mock
    mock_exchange = mocker.MagicMock()
    asset_positions = [
        _make_ap(fp.coin, unrealized_pnl=0.0)
        for fp in fps
    ]
    perp_state = mocker.MagicMock()
    perp_state.account_value = account_value
    perp_state.asset_positions = asset_positions
    mock_exchange.get_account_snapshot = mocker.AsyncMock(
        return_value=(perp_state, mocker.MagicMock())
    )

    # FarbRepo mock
    mock_farb_repo = mocker.MagicMock()
    mock_farb_repo.list_open = mocker.AsyncMock(return_value=fps)
    mock_farb_repo.get = mocker.AsyncMock(return_value=fps[0] if fps else None)
    mock_farb_repo.transition = mocker.AsyncMock(return_value=None)

    # Settings mock
    mock_settings = mocker.MagicMock()
    mock_settings.get_coin_spec.return_value = _make_coin_spec(coin_spec_maint)

    # EventBus mock
    mock_bus = mocker.MagicMock()
    mock_bus.publish = mocker.AsyncMock()

    watchdog = MarginWatchdog(
        strategy_id=STRATEGY_ID,
        exchange=mock_exchange,
        farb_repo=mock_farb_repo,
        margin_manager=mgr,
        settings=mock_settings,
        event_bus=mock_bus,
    )
    return watchdog, mock_farb_repo, mock_bus


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_healthy_no_actions(mocker):
    """High account_value → status=HEALTHY → no events, no transitions, actions_taken==[]."""
    # required_margin=10.0 per FP → virtual_ratio=10/2.5=4.0 (HEALTHY)
    fps = [_make_fp(1, "BTC", state_data={"required_margin": 10.0})]
    # account_value=1000 vs virtual_maint=2.5 → account_ratio=400 (HEALTHY)
    watchdog, mock_farb_repo, mock_bus = _make_watchdog(mocker, fps=fps, account_value=1000.0)

    report = await watchdog.run_check(now_ms=NOW_MS)

    assert isinstance(report, WatchdogReport)
    assert report.assessment.account_status == MarginStatus.HEALTHY
    assert report.actions_taken == []
    mock_bus.publish.assert_not_called()
    mock_farb_repo.transition.assert_not_called()


@pytest.mark.asyncio
async def test_warning_publishes_event_no_close(mocker):
    """account_ratio in (forced_close_trigger, top_up_trigger) → WARNING event, no transition."""
    # We need account_ratio in (1.5, 2.0).
    # Use 2 FPs each with virtual_maint=2.5 → total_maint=5.0
    # account_ratio = account_value / 5.0 → set account_value=8.75 → ratio=1.75 (WARNING)
    fps = [
        _make_fp(1, "BTC", state_data={"required_margin": _required_margin_for_ratio(3.0)}),
        _make_fp(2, "ETH", state_data={"required_margin": _required_margin_for_ratio(3.0)}),
    ]

    mgr = _make_mgr()
    mock_exchange = mocker.MagicMock()
    asset_positions = [_make_ap("BTC"), _make_ap("ETH")]
    perp_state = mocker.MagicMock()
    total_maint = 2 * _VIRTUAL_MAINT  # 5.0
    # account_ratio = 1.75 → WARNING (> 1.5, < 2.0)
    perp_state.account_value = 1.75 * total_maint
    perp_state.asset_positions = asset_positions
    mock_exchange.get_account_snapshot = mocker.AsyncMock(
        return_value=(perp_state, mocker.MagicMock())
    )

    mock_farb_repo = mocker.MagicMock()
    mock_farb_repo.list_open = mocker.AsyncMock(return_value=fps)
    mock_farb_repo.transition = mocker.AsyncMock()

    mock_settings = mocker.MagicMock()
    mock_settings.get_coin_spec.return_value = _make_coin_spec()

    mock_bus = mocker.MagicMock()
    mock_bus.publish = mocker.AsyncMock()

    watchdog = MarginWatchdog(
        strategy_id=STRATEGY_ID,
        exchange=mock_exchange,
        farb_repo=mock_farb_repo,
        margin_manager=mgr,
        settings=mock_settings,
        event_bus=mock_bus,
    )

    report = await watchdog.run_check(now_ms=NOW_MS)

    assert report.assessment.account_status == MarginStatus.WARNING
    assert "event:margin.warning" in report.actions_taken
    # WARNING → no weakest_fp_id, no transition
    assert report.assessment.weakest_fp_id is None
    mock_farb_repo.transition.assert_not_called()
    mock_bus.publish.assert_awaited_once()
    # verify published event level
    published_event = mock_bus.publish.call_args[0][0]
    assert published_event.level == "WARNING"
    assert published_event.kind == "margin.warning"


@pytest.mark.asyncio
async def test_forced_close_picks_weakest_virtual(mocker):
    """3 FPs with virtual ratios [3.0, 1.4, 2.5], account_ratio=1.2 → transition for FP with ratio=1.4."""
    # virtual_maint = 2.5 per FP, total=7.5
    # account_value = 1.2 * 7.5 = 9.0
    # FP1: required=3.0*2.5=7.5  ratio=3.0
    # FP2: required=1.4*2.5=3.5  ratio=1.4  ← weakest
    # FP3: required=2.5*2.5=6.25 ratio=2.5
    fp1 = _make_fp(1, "BTC", state_data={"required_margin": _required_margin_for_ratio(3.0)})
    fp2 = _make_fp(2, "ETH", state_data={"required_margin": _required_margin_for_ratio(1.4)})
    fp3 = _make_fp(3, "SOL", state_data={"required_margin": _required_margin_for_ratio(2.5)})
    fps = [fp1, fp2, fp3]

    mgr = _make_mgr()
    mock_exchange = mocker.MagicMock()
    asset_positions = [_make_ap("BTC"), _make_ap("ETH"), _make_ap("SOL")]
    perp_state = mocker.MagicMock()
    total_maint = 3 * _VIRTUAL_MAINT  # 7.5
    perp_state.account_value = 1.2 * total_maint  # 9.0 → ratio=1.2 (FORCED_CLOSE)
    perp_state.asset_positions = asset_positions
    mock_exchange.get_account_snapshot = mocker.AsyncMock(
        return_value=(perp_state, mocker.MagicMock())
    )

    mock_farb_repo = mocker.MagicMock()
    mock_farb_repo.list_open = mocker.AsyncMock(return_value=fps)
    # get returns fp2 (the weakest, id=2)
    mock_farb_repo.get = mocker.AsyncMock(return_value=fp2)
    mock_farb_repo.transition = mocker.AsyncMock(return_value=None)

    mock_settings = mocker.MagicMock()
    mock_settings.get_coin_spec.return_value = _make_coin_spec()

    mock_bus = mocker.MagicMock()
    mock_bus.publish = mocker.AsyncMock()

    watchdog = MarginWatchdog(
        strategy_id=STRATEGY_ID,
        exchange=mock_exchange,
        farb_repo=mock_farb_repo,
        margin_manager=mgr,
        settings=mock_settings,
        event_bus=mock_bus,
    )

    report = await watchdog.run_check(now_ms=NOW_MS)

    assert report.assessment.account_status == MarginStatus.FORCED_CLOSE
    assert report.assessment.weakest_fp_id == 2

    # transition called with fp2 (weakest)
    mock_farb_repo.transition.assert_awaited_once()
    call_kwargs = mock_farb_repo.transition.call_args
    assert call_kwargs[0][0] == 2  # positional: farb_position_id
    assert call_kwargs[1]["from_state"] == FarbState.OPEN
    assert call_kwargs[1]["to_state"] == FarbState.CLOSING_SHORT

    assert f"forced_close:fp=2" in report.actions_taken


@pytest.mark.asyncio
async def test_liquidation_imminent_critical_event_and_close(mocker):
    """account_ratio=0.9 → CRITICAL event + transition called."""
    fp = _make_fp(1, "BTC", state_data={"required_margin": _required_margin_for_ratio(2.0)})
    fps = [fp]

    mgr = _make_mgr()
    mock_exchange = mocker.MagicMock()
    perp_state = mocker.MagicMock()
    total_maint = _VIRTUAL_MAINT  # 2.5
    perp_state.account_value = 0.9 * total_maint  # ratio=0.9 (LIQUIDATION_IMMINENT)
    perp_state.asset_positions = [_make_ap("BTC")]
    mock_exchange.get_account_snapshot = mocker.AsyncMock(
        return_value=(perp_state, mocker.MagicMock())
    )

    mock_farb_repo = mocker.MagicMock()
    mock_farb_repo.list_open = mocker.AsyncMock(return_value=fps)
    mock_farb_repo.get = mocker.AsyncMock(return_value=fp)
    mock_farb_repo.transition = mocker.AsyncMock(return_value=None)

    mock_settings = mocker.MagicMock()
    mock_settings.get_coin_spec.return_value = _make_coin_spec()

    mock_bus = mocker.MagicMock()
    mock_bus.publish = mocker.AsyncMock()

    watchdog = MarginWatchdog(
        strategy_id=STRATEGY_ID,
        exchange=mock_exchange,
        farb_repo=mock_farb_repo,
        margin_manager=mgr,
        settings=mock_settings,
        event_bus=mock_bus,
    )

    report = await watchdog.run_check(now_ms=NOW_MS)

    assert report.assessment.account_status == MarginStatus.LIQUIDATION_IMMINENT
    mock_bus.publish.assert_awaited_once()
    published_event = mock_bus.publish.call_args[0][0]
    assert published_event.level == "CRITICAL"
    assert published_event.kind == "margin.liquidation_imminent"
    mock_farb_repo.transition.assert_awaited_once()
    assert "event:margin.liquidation_imminent" in report.actions_taken
    assert "forced_close:fp=1" in report.actions_taken


@pytest.mark.asyncio
async def test_no_open_fps_no_actions(mocker):
    """list_open returns [] → HEALTHY (inf ratio), no actions."""
    mgr = _make_mgr()
    mock_exchange = mocker.MagicMock()
    perp_state = mocker.MagicMock()
    perp_state.account_value = 1000.0
    perp_state.asset_positions = []
    mock_exchange.get_account_snapshot = mocker.AsyncMock(
        return_value=(perp_state, mocker.MagicMock())
    )

    mock_farb_repo = mocker.MagicMock()
    mock_farb_repo.list_open = mocker.AsyncMock(return_value=[])
    mock_farb_repo.transition = mocker.AsyncMock()

    mock_settings = mocker.MagicMock()
    mock_bus = mocker.MagicMock()
    mock_bus.publish = mocker.AsyncMock()

    watchdog = MarginWatchdog(
        strategy_id=STRATEGY_ID,
        exchange=mock_exchange,
        farb_repo=mock_farb_repo,
        margin_manager=mgr,
        settings=mock_settings,
        event_bus=mock_bus,
    )

    report = await watchdog.run_check(now_ms=NOW_MS)

    assert report.assessment.account_status == MarginStatus.HEALTHY
    assert report.actions_taken == []
    mock_bus.publish.assert_not_called()
    mock_farb_repo.transition.assert_not_called()


@pytest.mark.asyncio
async def test_missing_asset_position_excluded(mocker):
    """Open FP whose coin isn't in perp_state.asset_positions → logged warning, FP excluded."""
    # Two FPs: BTC (present in asset_positions), ETH (missing)
    fp_btc = _make_fp(1, "BTC", state_data={"required_margin": _required_margin_for_ratio(5.0)})
    fp_eth = _make_fp(2, "ETH", state_data={"required_margin": _required_margin_for_ratio(5.0)})
    fps = [fp_btc, fp_eth]

    mgr = _make_mgr()
    mock_exchange = mocker.MagicMock()
    perp_state = mocker.MagicMock()
    perp_state.account_value = 1000.0
    # Only BTC in asset_positions; ETH is missing
    perp_state.asset_positions = [_make_ap("BTC")]
    mock_exchange.get_account_snapshot = mocker.AsyncMock(
        return_value=(perp_state, mocker.MagicMock())
    )

    mock_farb_repo = mocker.MagicMock()
    mock_farb_repo.list_open = mocker.AsyncMock(return_value=fps)
    mock_farb_repo.transition = mocker.AsyncMock()

    mock_settings = mocker.MagicMock()
    mock_settings.get_coin_spec.return_value = _make_coin_spec()

    mock_bus = mocker.MagicMock()
    mock_bus.publish = mocker.AsyncMock()

    watchdog = MarginWatchdog(
        strategy_id=STRATEGY_ID,
        exchange=mock_exchange,
        farb_repo=mock_farb_repo,
        margin_manager=mgr,
        settings=mock_settings,
        event_bus=mock_bus,
    )

    report = await watchdog.run_check(now_ms=NOW_MS)

    # Only BTC snapshot included → 1 FP in per_fp
    assert len(report.assessment.per_fp) == 1
    assert report.assessment.per_fp[0].coin == "BTC"
    # High account_value vs single BTC maint → HEALTHY
    assert report.assessment.account_status == MarginStatus.HEALTHY


@pytest.mark.asyncio
async def test_state_conflict_does_not_crash(mocker):
    """farb_repo.transition raises StateConflict → logged warning, no re-raise, returns WatchdogReport."""
    fp = _make_fp(1, "BTC", state_data={"required_margin": _required_margin_for_ratio(1.3)})
    fps = [fp]

    mgr = _make_mgr()
    mock_exchange = mocker.MagicMock()
    perp_state = mocker.MagicMock()
    total_maint = _VIRTUAL_MAINT
    # account_ratio=1.2 → FORCED_CLOSE
    perp_state.account_value = 1.2 * total_maint
    perp_state.asset_positions = [_make_ap("BTC")]
    mock_exchange.get_account_snapshot = mocker.AsyncMock(
        return_value=(perp_state, mocker.MagicMock())
    )

    mock_farb_repo = mocker.MagicMock()
    mock_farb_repo.list_open = mocker.AsyncMock(return_value=fps)
    mock_farb_repo.get = mocker.AsyncMock(return_value=fp)
    mock_farb_repo.transition = mocker.AsyncMock(
        side_effect=StateConflict(1, FarbState.OPEN, FarbState.CLOSING_SHORT)
    )

    mock_settings = mocker.MagicMock()
    mock_settings.get_coin_spec.return_value = _make_coin_spec()

    mock_bus = mocker.MagicMock()
    mock_bus.publish = mocker.AsyncMock()

    watchdog = MarginWatchdog(
        strategy_id=STRATEGY_ID,
        exchange=mock_exchange,
        farb_repo=mock_farb_repo,
        margin_manager=mgr,
        settings=mock_settings,
        event_bus=mock_bus,
    )

    # Must not raise
    report = await watchdog.run_check(now_ms=NOW_MS)

    assert isinstance(report, WatchdogReport)
    assert report.assessment.account_status == MarginStatus.FORCED_CLOSE
    # forced_close action NOT appended because transition failed
    assert "forced_close:fp=1" not in report.actions_taken
    # event still emitted
    assert "event:margin.forced_close" in report.actions_taken


@pytest.mark.asyncio
async def test_dry_assess_does_not_emit_or_transition(mocker):
    """dry_assess() returns AccountAssessment but does NOT call event_bus.publish or farb_repo.transition."""
    fp = _make_fp(1, "BTC", state_data={"required_margin": _required_margin_for_ratio(0.8)})
    fps = [fp]

    mgr = _make_mgr()
    mock_exchange = mocker.MagicMock()
    perp_state = mocker.MagicMock()
    total_maint = _VIRTUAL_MAINT
    # account_ratio=0.8 → LIQUIDATION_IMMINENT (would normally emit + transition)
    perp_state.account_value = 0.8 * total_maint
    perp_state.asset_positions = [_make_ap("BTC")]
    mock_exchange.get_account_snapshot = mocker.AsyncMock(
        return_value=(perp_state, mocker.MagicMock())
    )

    mock_farb_repo = mocker.MagicMock()
    mock_farb_repo.list_open = mocker.AsyncMock(return_value=fps)
    mock_farb_repo.transition = mocker.AsyncMock()

    mock_settings = mocker.MagicMock()
    mock_settings.get_coin_spec.return_value = _make_coin_spec()

    mock_bus = mocker.MagicMock()
    mock_bus.publish = mocker.AsyncMock()

    watchdog = MarginWatchdog(
        strategy_id=STRATEGY_ID,
        exchange=mock_exchange,
        farb_repo=mock_farb_repo,
        margin_manager=mgr,
        settings=mock_settings,
        event_bus=mock_bus,
    )

    from frab.engine.margin_manager import AccountAssessment
    result = await watchdog.dry_assess()

    assert isinstance(result, AccountAssessment)
    mock_bus.publish.assert_not_called()
    mock_farb_repo.transition.assert_not_called()


@pytest.mark.asyncio
async def test_report_contains_actions_taken(mocker):
    """WatchdogReport.actions_taken contains exactly expected entries in warning case."""
    fp = _make_fp(1, "BTC", state_data={"required_margin": _required_margin_for_ratio(5.0)})
    fps = [fp]

    mgr = _make_mgr()
    mock_exchange = mocker.MagicMock()
    perp_state = mocker.MagicMock()
    total_maint = _VIRTUAL_MAINT
    # account_ratio=1.75 → WARNING
    perp_state.account_value = 1.75 * total_maint
    perp_state.asset_positions = [_make_ap("BTC")]
    mock_exchange.get_account_snapshot = mocker.AsyncMock(
        return_value=(perp_state, mocker.MagicMock())
    )

    mock_farb_repo = mocker.MagicMock()
    mock_farb_repo.list_open = mocker.AsyncMock(return_value=fps)
    mock_farb_repo.transition = mocker.AsyncMock()

    mock_settings = mocker.MagicMock()
    mock_settings.get_coin_spec.return_value = _make_coin_spec()

    mock_bus = mocker.MagicMock()
    mock_bus.publish = mocker.AsyncMock()

    watchdog = MarginWatchdog(
        strategy_id=STRATEGY_ID,
        exchange=mock_exchange,
        farb_repo=mock_farb_repo,
        margin_manager=mgr,
        settings=mock_settings,
        event_bus=mock_bus,
    )

    report = await watchdog.run_check(now_ms=NOW_MS)

    assert report.actions_taken == ["event:margin.warning"]
    mock_farb_repo.transition.assert_not_called()


@pytest.mark.asyncio
async def test_transition_state_data_merges_existing_keys(mocker):
    """fp.state_data starts with {'foo': 'bar'}; after transition, state_data contains both
    'foo': 'bar' AND 'exit_decision': 'watchdog_forced'."""
    existing_state_data = {"foo": "bar", "required_margin": _required_margin_for_ratio(1.3)}
    fp = _make_fp(1, "BTC", state_data=existing_state_data)
    fps = [fp]

    mgr = _make_mgr()
    mock_exchange = mocker.MagicMock()
    perp_state = mocker.MagicMock()
    total_maint = _VIRTUAL_MAINT
    # account_ratio=1.2 → FORCED_CLOSE
    perp_state.account_value = 1.2 * total_maint
    perp_state.asset_positions = [_make_ap("BTC")]
    mock_exchange.get_account_snapshot = mocker.AsyncMock(
        return_value=(perp_state, mocker.MagicMock())
    )

    mock_farb_repo = mocker.MagicMock()
    mock_farb_repo.list_open = mocker.AsyncMock(return_value=fps)
    mock_farb_repo.get = mocker.AsyncMock(return_value=fp)
    mock_farb_repo.transition = mocker.AsyncMock(return_value=None)

    mock_settings = mocker.MagicMock()
    mock_settings.get_coin_spec.return_value = _make_coin_spec()

    mock_bus = mocker.MagicMock()
    mock_bus.publish = mocker.AsyncMock()

    watchdog = MarginWatchdog(
        strategy_id=STRATEGY_ID,
        exchange=mock_exchange,
        farb_repo=mock_farb_repo,
        margin_manager=mgr,
        settings=mock_settings,
        event_bus=mock_bus,
    )

    await watchdog.run_check(now_ms=NOW_MS)

    mock_farb_repo.transition.assert_awaited_once()
    call_kwargs = mock_farb_repo.transition.call_args[1]
    passed_state_data = call_kwargs["state_data"]

    # Existing key preserved
    assert passed_state_data["foo"] == "bar"
    # New keys added
    assert passed_state_data["exit_decision"] == "watchdog_forced"
    assert passed_state_data["exit_requested_at_ms"] == NOW_MS
    # Required margin also preserved
    assert "required_margin" in passed_state_data
