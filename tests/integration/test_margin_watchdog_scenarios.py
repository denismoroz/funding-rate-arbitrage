"""Integration tests for MarginWatchdog — end-to-end with in-memory SQLite.

Uses:
- real FarbRepo + in-memory aiosqlite engine
- real MarginManager
- mocker.AsyncMock exchange (get_account_snapshot)
- mocker.MagicMock event_bus (publish as AsyncMock)

Math reference (maint_ratio=0.025, short_size=1.0, mark=100.0):
    virtual_maint per FP = 1.0 * 100.0 * 0.025 = 2.5
    account_ratio = account_value / (N * 2.5)

Thresholds: top_up=2.0, forced_close=1.5, healthy=2.0
    HEALTHY      → account_ratio >= 2.0
    WARNING      → 1.5 < account_ratio < 2.0
    FORCED_CLOSE → 1.0 < account_ratio <= 1.5
    LIQ_IMMINENT → account_ratio <= 1.0
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from frab.constants import CoinMarginSpec
from frab.db.models import Exchange as ExchangeRow, Strategy as StrategyRow
from frab.db.session import init_db, make_session_factory, session_scope
from frab.domain import FarbState
from frab.engine.margin_manager import MarginManager, MarginStatus
from frab.engine.margin_watchdog import MarginWatchdog
from frab.events.bus import EventBus
from frab.repo.farb_repo import FarbRepo

# ── Constants ─────────────────────────────────────────────────────────────────

NOW_MS = 1_704_067_200_000  # 2024-01-01 00:00:00 UTC

# Each FP: short_size=1.0, mark=100.0, maint_ratio=0.025 → virtual_maint=2.5
SHORT_SIZE = 1.0
MARK = 100.0
MAINT_RATIO = 0.025
VIRTUAL_MAINT = SHORT_SIZE * MARK * MAINT_RATIO  # = 2.5

# MarginManager thresholds (satisfy 1.0 < forced < top_up <= healthy)
TOP_UP = 2.0
FORCED_CLOSE = 1.5
HEALTHY = 2.0


# ── DB fixtures ───────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)

    def _enable_fks(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    event.listen(eng.sync_engine, "connect", _enable_fks)
    await init_db(eng)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return make_session_factory(engine)


@pytest_asyncio.fixture
async def strategy_id(session_factory):
    """Seed minimal exchange + strategy rows; return strategy_id."""
    async with session_scope(session_factory) as s:
        exc = ExchangeRow(
            name="mock_hl",
            funding_interval_h=1,
            spot_taker_bps=7.0,
            perp_taker_bps=3.5,
        )
        s.add(exc)
        strat = StrategyRow(
            name="watchdog_test",
            version="v1",
            params_json={},
            status="running",
        )
        s.add(strat)
        await s.flush()
        sid = strat.id
    return sid


# ── Helper builders ───────────────────────────────────────────────────────────


def _make_ap(coin: str, szi: float = -SHORT_SIZE, position_value: float = MARK * SHORT_SIZE, unrealized_pnl: float = 0.0):
    """Build a minimal asset_position-like object the watchdog reads."""
    ap = type("AP", (), {})()
    ap.coin = coin
    ap.szi = szi
    ap.position_value = position_value
    ap.unrealized_pnl = unrealized_pnl
    return ap


def _make_perp_state(account_value: float, asset_positions):
    ps = type("PerpState", (), {})()
    ps.account_value = account_value
    ps.asset_positions = asset_positions
    return ps


def _make_exchange(mocker, account_value: float, asset_positions):
    """AsyncMock exchange whose get_account_snapshot returns (perp_state, spot_state)."""
    exchange = mocker.AsyncMock()
    perp_state = _make_perp_state(account_value, asset_positions)
    spot_state = mocker.MagicMock()
    exchange.get_account_snapshot.return_value = (perp_state, spot_state)
    return exchange


def _make_settings(mocker, maint_ratio: float = MAINT_RATIO):
    settings = mocker.MagicMock()
    spec = CoinMarginSpec(leverage=10, maint_ratio=maint_ratio)
    settings.get_coin_spec.return_value = spec
    return settings


def _make_manager():
    return MarginManager(
        top_up_trigger=TOP_UP,
        forced_close_trigger=FORCED_CLOSE,
        healthy_ratio=HEALTHY,
    )


def _make_watchdog(strategy_id, exchange, farb_repo, mgr, settings, bus):
    return MarginWatchdog(
        strategy_id=strategy_id,
        exchange=exchange,
        farb_repo=farb_repo,
        margin_manager=mgr,
        settings=settings,
        event_bus=bus,
    )


async def _seed_open_fp(farb_repo: FarbRepo, strategy_id: int, coin: str, state_data: dict) -> int:
    """Create FP in CHECK_MARGIN then transition to OPEN. Returns fp.id."""
    fp = await farb_repo.create(
        strategy_id=strategy_id,
        coin=coin,
        initial_state=FarbState.CHECK_MARGIN,
        state_data=state_data,
    )
    fp = await farb_repo.transition(
        fp.id,
        from_state=FarbState.CHECK_MARGIN,
        to_state=FarbState.OPEN,
        state_data=state_data,
    )
    return fp.id


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_e2e_healthy_no_action(session_factory, strategy_id, mocker):
    """High account_value → HEALTHY: no events, no DB state change."""
    farb_repo = FarbRepo(session_factory)

    sd = {"required_margin": 7.5}
    btc_id = await _seed_open_fp(farb_repo, strategy_id, "BTC", sd)
    eth_id = await _seed_open_fp(farb_repo, strategy_id, "ETH", sd)
    sol_id = await _seed_open_fp(farb_repo, strategy_id, "SOL", sd)

    # 3 FPs × virtual_maint=2.5 → total_maint=7.5
    # account_ratio = 100.0 / 7.5 ≈ 13.3 >> 2.0 → HEALTHY
    asset_positions = [
        _make_ap("BTC"),
        _make_ap("ETH"),
        _make_ap("SOL"),
    ]
    exchange = _make_exchange(mocker, account_value=100.0, asset_positions=asset_positions)
    settings = _make_settings(mocker)
    mgr = _make_manager()

    bus = mocker.MagicMock(spec=EventBus)
    bus.publish = mocker.AsyncMock()

    watchdog = _make_watchdog(strategy_id, exchange, farb_repo, mgr, settings, bus)
    report = await watchdog.run_check(now_ms=NOW_MS)

    assert report.assessment.account_status == MarginStatus.HEALTHY
    assert report.actions_taken == []
    bus.publish.assert_not_called()

    # All 3 FPs still OPEN
    for fp_id in [btc_id, eth_id, sol_id]:
        fp = await farb_repo.get(fp_id)
        assert fp.state == FarbState.OPEN, f"FP {fp_id} ({fp.coin}) should be OPEN"


async def test_e2e_warning_publishes_event_no_close(session_factory, strategy_id, mocker):
    """account_ratio ∈ (1.5, 2.0) → WARNING: 1 event, no FP closed."""
    farb_repo = FarbRepo(session_factory)

    sd = {"required_margin": 7.5}
    fp1_id = await _seed_open_fp(farb_repo, strategy_id, "BTC", sd)
    fp2_id = await _seed_open_fp(farb_repo, strategy_id, "ETH", sd)

    # 2 FPs × virtual_maint=2.5 → total_maint=5.0
    # account_ratio = 8.5 / 5.0 = 1.7 → WARNING (1.5 < 1.7 < 2.0)
    asset_positions = [_make_ap("BTC"), _make_ap("ETH")]
    exchange = _make_exchange(mocker, account_value=8.5, asset_positions=asset_positions)
    settings = _make_settings(mocker)
    mgr = _make_manager()

    bus = mocker.MagicMock(spec=EventBus)
    bus.publish = mocker.AsyncMock()

    watchdog = _make_watchdog(strategy_id, exchange, farb_repo, mgr, settings, bus)
    report = await watchdog.run_check(now_ms=NOW_MS)

    assert report.assessment.account_status == MarginStatus.WARNING
    # Exactly one event published
    assert bus.publish.call_count == 1
    published_event = bus.publish.call_args[0][0]
    assert published_event.kind == "margin.warning"
    assert published_event.level == "WARNING"

    # Actions: event published but no forced_close
    assert any("event:margin" in a for a in report.actions_taken)
    assert not any("forced_close" in a for a in report.actions_taken)

    # Both FPs still OPEN
    fp1 = await farb_repo.get(fp1_id)
    fp2 = await farb_repo.get(fp2_id)
    assert fp1.state == FarbState.OPEN
    assert fp2.state == FarbState.OPEN


async def test_e2e_forced_close_weakest_by_virtual_ratio(session_factory, strategy_id, mocker):
    """account_ratio ∈ (1.0, 1.5) → FORCED_CLOSE: weakest FP (ETH) → CLOSING_SHORT."""
    farb_repo = FarbRepo(session_factory)

    # BTC: required_margin=10.0 (no loss)   → virtual_equity = 10.0 + 0 + 0 - 0 = 10.0
    # ETH: required_margin=5.0, unrealized=-3.0 → virtual_equity = 5.0 - 3.0 = 2.0
    # SOL: required_margin=8.0               → virtual_equity = 8.0
    # virtual_maint = 2.5 for all (same size/mark/maint)
    # virtual_ratio: BTC=4.0, ETH=0.8 (lowest!), SOL=3.2
    btc_id = await _seed_open_fp(farb_repo, strategy_id, "BTC", {"required_margin": 10.0})
    eth_id = await _seed_open_fp(farb_repo, strategy_id, "ETH", {"required_margin": 5.0})
    sol_id = await _seed_open_fp(farb_repo, strategy_id, "SOL", {"required_margin": 8.0})

    # 3 FPs × 2.5 = 7.5 total_maint
    # account_ratio = 9.375 / 7.5 = 1.25 → FORCED_CLOSE (1.0 < 1.25 < 1.5)
    asset_positions = [
        _make_ap("BTC", unrealized_pnl=0.0),
        _make_ap("ETH", unrealized_pnl=-3.0),  # ETH is weakest
        _make_ap("SOL", unrealized_pnl=0.0),
    ]
    exchange = _make_exchange(mocker, account_value=9.375, asset_positions=asset_positions)
    settings = _make_settings(mocker)
    mgr = _make_manager()

    bus = mocker.MagicMock(spec=EventBus)
    bus.publish = mocker.AsyncMock()

    watchdog = _make_watchdog(strategy_id, exchange, farb_repo, mgr, settings, bus)
    report = await watchdog.run_check(now_ms=NOW_MS)

    assert report.assessment.account_status == MarginStatus.FORCED_CLOSE
    assert report.assessment.weakest_fp_id == eth_id

    # ETH → CLOSING_SHORT
    eth_fp = await farb_repo.get(eth_id)
    assert eth_fp.state == FarbState.CLOSING_SHORT
    # state_data has exit markers + preserves original key
    assert eth_fp.state_data.get("exit_decision") == "watchdog_forced"
    assert eth_fp.state_data.get("exit_requested_at_ms") == NOW_MS
    assert eth_fp.state_data.get("required_margin") == 5.0

    # BTC and SOL still OPEN
    btc_fp = await farb_repo.get(btc_id)
    sol_fp = await farb_repo.get(sol_id)
    assert btc_fp.state == FarbState.OPEN
    assert sol_fp.state == FarbState.OPEN

    # One event + one forced_close action
    assert any("forced_close" in a for a in report.actions_taken)


async def test_e2e_liquidation_imminent_emits_critical(session_factory, strategy_id, mocker):
    """account_ratio < 1.0 → LIQUIDATION_IMMINENT: CRITICAL event, FP → CLOSING_SHORT."""
    farb_repo = FarbRepo(session_factory)

    fp_id = await _seed_open_fp(farb_repo, strategy_id, "BTC", {"required_margin": 7.5})

    # 1 FP × 2.5 = 2.5 total_maint
    # account_ratio = 2.0 / 2.5 = 0.8 → LIQUIDATION_IMMINENT
    asset_positions = [_make_ap("BTC")]
    exchange = _make_exchange(mocker, account_value=2.0, asset_positions=asset_positions)
    settings = _make_settings(mocker)
    mgr = _make_manager()

    bus = mocker.MagicMock(spec=EventBus)
    bus.publish = mocker.AsyncMock()

    watchdog = _make_watchdog(strategy_id, exchange, farb_repo, mgr, settings, bus)
    report = await watchdog.run_check(now_ms=NOW_MS)

    assert report.assessment.account_status == MarginStatus.LIQUIDATION_IMMINENT

    # Event: CRITICAL level, kind=margin.liquidation_imminent
    assert bus.publish.call_count == 1
    published_event = bus.publish.call_args[0][0]
    assert published_event.level == "CRITICAL"
    assert published_event.kind == "margin.liquidation_imminent"

    # FP should be CLOSING_SHORT
    fp = await farb_repo.get(fp_id)
    assert fp.state == FarbState.CLOSING_SHORT
    assert fp.state_data.get("exit_decision") == "watchdog_forced"


async def test_e2e_watchdog_idempotent_when_fp_already_closing(session_factory, strategy_id, mocker):
    """Second run with same bad account state after ETH already CLOSING_SHORT → no crash."""
    farb_repo = FarbRepo(session_factory)

    # Only one FP to keep it simple: after first run it goes CLOSING_SHORT.
    # On second run list_open returns empty → no snapshots → account_ratio=inf → HEALTHY.
    fp_id = await _seed_open_fp(farb_repo, strategy_id, "BTC", {"required_margin": 7.5})

    # account_ratio = 2.0 / 2.5 = 0.8 → LIQUIDATION_IMMINENT
    asset_positions = [_make_ap("BTC")]
    exchange = _make_exchange(mocker, account_value=2.0, asset_positions=asset_positions)
    settings = _make_settings(mocker)
    mgr = _make_manager()

    bus = mocker.MagicMock(spec=EventBus)
    bus.publish = mocker.AsyncMock()

    watchdog = _make_watchdog(strategy_id, exchange, farb_repo, mgr, settings, bus)

    # First run: FP → CLOSING_SHORT
    report1 = await watchdog.run_check(now_ms=NOW_MS)
    assert report1.assessment.account_status == MarginStatus.LIQUIDATION_IMMINENT
    fp = await farb_repo.get(fp_id)
    assert fp.state == FarbState.CLOSING_SHORT

    # Second run: same bad account_value, but list_open() returns [] (FP no longer OPEN)
    # → no snapshots → total_maint=0 → account_ratio=inf → HEALTHY
    report2 = await watchdog.run_check(now_ms=NOW_MS + 1000)
    # No exception raised — idempotent
    assert report2.assessment.account_status == MarginStatus.HEALTHY

    # FP remains CLOSING_SHORT (not further modified)
    fp_after = await farb_repo.get(fp_id)
    assert fp_after.state == FarbState.CLOSING_SHORT


async def test_e2e_per_fp_includes_funding_and_fees_in_equity(session_factory, strategy_id, mocker):
    """Virtual equity = required_margin + unrealized_pnl + funding - fees flows correctly."""
    farb_repo = FarbRepo(session_factory)

    # state_data drives the arithmetic
    state_data = {
        "required_margin": 7.20,
        "gross_funding_so_far": 0.5,
        "total_fees_paid": 0.3,
    }
    fp_id = await _seed_open_fp(farb_repo, strategy_id, "BTC", state_data)

    # unrealized_pnl = -0.1 from the mocked asset position
    # virtual_equity = 7.20 + (-0.1) + 0.5 - 0.3 = 7.30
    asset_positions = [_make_ap("BTC", unrealized_pnl=-0.1)]

    # account_value very high → assessment will be HEALTHY (no side effects)
    exchange = _make_exchange(mocker, account_value=1000.0, asset_positions=asset_positions)
    settings = _make_settings(mocker)
    mgr = _make_manager()

    bus = mocker.MagicMock(spec=EventBus)
    bus.publish = mocker.AsyncMock()

    watchdog = _make_watchdog(strategy_id, exchange, farb_repo, mgr, settings, bus)

    # Use dry_assess to get the assessment without side effects
    assessment = await watchdog.dry_assess()

    assert len(assessment.per_fp) == 1
    fp_assessment = assessment.per_fp[0]
    assert fp_assessment.farb_position_id == fp_id
    assert fp_assessment.virtual_equity == pytest.approx(7.30, rel=1e-9)

    # No events published (healthy + dry_assess)
    bus.publish.assert_not_called()

    # DB state unchanged: still OPEN
    fp = await farb_repo.get(fp_id)
    assert fp.state == FarbState.OPEN
