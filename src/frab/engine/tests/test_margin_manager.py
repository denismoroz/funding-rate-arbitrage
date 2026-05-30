"""Tests for MarginManager pure-logic class."""
from __future__ import annotations
import math
import pytest

from frab.engine.margin_manager import (
    AccountAssessment,
    FpAssessment,
    FpMarginSnapshot,
    MarginManager,
    MarginStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mgr(
    top_up_trigger: float = 2.0,
    forced_close_trigger: float = 1.5,
    healthy_ratio: float = 3.0,
) -> MarginManager:
    return MarginManager(
        top_up_trigger=top_up_trigger,
        forced_close_trigger=forced_close_trigger,
        healthy_ratio=healthy_ratio,
    )


def _snap(
    fp_id: int = 1,
    coin: str = "BTC",
    short_size: float = 1.0,
    current_mark: float = 100.0,
    required_margin: float = 10.0,
    unrealized_pnl: float = 0.0,
    funding_accrued: float = 0.0,
    fees_paid: float = 0.0,
    signal_apr: float = 0.0,
) -> FpMarginSnapshot:
    return FpMarginSnapshot(
        farb_position_id=fp_id,
        coin=coin,
        short_size=short_size,
        current_mark=current_mark,
        required_margin=required_margin,
        unrealized_pnl=unrealized_pnl,
        funding_accrued=funding_accrued,
        fees_paid=fees_paid,
        signal_apr=signal_apr,
    )


# ---------------------------------------------------------------------------
# test_init_rejects_bad_thresholds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("top_up_trigger,forced_close_trigger,healthy_ratio", [
    (2.0, 1.0, 3.0),   # forced_close_trigger == 1.0, not > 1.0
    (2.0, 2.5, 3.0),   # forced_close_trigger > top_up_trigger
    (5.0, 1.5, 3.0),   # top_up_trigger > healthy_ratio
])
def test_init_rejects_bad_thresholds(top_up_trigger, forced_close_trigger, healthy_ratio):
    with pytest.raises(ValueError):
        MarginManager(
            top_up_trigger=top_up_trigger,
            forced_close_trigger=forced_close_trigger,
            healthy_ratio=healthy_ratio,
        )


# ---------------------------------------------------------------------------
# test_classify_table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ratio,expected_status", [
    (5.0, MarginStatus.HEALTHY),
    (2.0, MarginStatus.HEALTHY),          # boundary: >= top_up_trigger
    (1.7, MarginStatus.WARNING),
    (1.5, MarginStatus.FORCED_CLOSE),     # boundary: not > forced_close_trigger
    (1.2, MarginStatus.FORCED_CLOSE),
    (1.0, MarginStatus.LIQUIDATION_IMMINENT),  # boundary: not > 1.0
    (0.9, MarginStatus.LIQUIDATION_IMMINENT),
])
def test_classify_table(ratio, expected_status):
    mgr = _mgr(top_up_trigger=2.0, forced_close_trigger=1.5, healthy_ratio=3.0)
    assert mgr._classify(ratio) == expected_status


# ---------------------------------------------------------------------------
# test_assess_fp_virtual_equity_includes_all_components
# ---------------------------------------------------------------------------

def test_assess_fp_virtual_equity_includes_all_components():
    mgr = _mgr()
    snap = _snap(
        required_margin=7.20,
        unrealized_pnl=-0.50,
        funding_accrued=0.10,
        fees_paid=0.05,
        short_size=1.0,
        current_mark=100.0,
    )
    result = mgr.assess_fp(snap, maint_ratio=0.01)
    assert math.isclose(result.virtual_equity, 6.75, rel_tol=1e-9)
    assert math.isclose(result.virtual_maintenance, 1.0, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# test_assess_fp_zero_short_size_returns_inf_healthy
# ---------------------------------------------------------------------------

def test_assess_fp_zero_short_size_returns_inf_healthy():
    mgr = _mgr()
    snap = _snap(short_size=0.0, current_mark=100.0, required_margin=10.0)
    result = mgr.assess_fp(snap, maint_ratio=0.05)
    assert result.virtual_maintenance == 0.0
    assert math.isinf(result.virtual_ratio)
    assert result.status == MarginStatus.HEALTHY


# ---------------------------------------------------------------------------
# test_assess_account_aggregates_maintenance
# ---------------------------------------------------------------------------

def test_assess_account_aggregates_maintenance():
    mgr = _mgr()
    # Each FP: short_size=1.0, mark=100.0, maint_ratio=0.05 → virtual_maint=5.0
    snaps = [
        _snap(fp_id=1, coin="BTC", short_size=1.0, current_mark=100.0, required_margin=10.0),
        _snap(fp_id=2, coin="ETH", short_size=1.0, current_mark=100.0, required_margin=10.0),
        _snap(fp_id=3, coin="SOL", short_size=1.0, current_mark=100.0, required_margin=10.0),
    ]
    maint_by_coin = {"BTC": 0.05, "ETH": 0.05, "SOL": 0.05}
    result = mgr.assess_account(
        account_equity_usdc=75.0,
        per_fp_snapshots=snaps,
        maint_ratio_by_coin=maint_by_coin,
    )
    assert math.isclose(result.total_maintenance_usdc, 15.0, rel_tol=1e-9)
    assert math.isclose(result.account_ratio, 5.0, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# test_assess_account_healthy_no_weakest
# ---------------------------------------------------------------------------

def test_assess_account_healthy_no_weakest():
    mgr = _mgr()
    snaps = [_snap(fp_id=1, coin="BTC", short_size=1.0, current_mark=100.0, required_margin=10.0)]
    result = mgr.assess_account(
        account_equity_usdc=75.0,
        per_fp_snapshots=snaps,
        maint_ratio_by_coin={"BTC": 0.05},
    )
    assert result.account_status == MarginStatus.HEALTHY
    assert result.weakest_fp_id is None


# ---------------------------------------------------------------------------
# test_assess_account_forced_close_picks_lowest_virtual_ratio
# ---------------------------------------------------------------------------

def test_assess_account_forced_close_picks_lowest_virtual_ratio():
    mgr = _mgr()
    # FP1: maint=10, equity=30 → virtual_ratio=3.0
    # FP2: maint=10, equity=15 → virtual_ratio=1.5  ← weakest
    # FP3: maint=10, equity=20 → virtual_ratio=2.0
    # total_maint=30, account_equity=36 → account_ratio=1.2 (FORCED_CLOSE)
    # Use short_size=1, mark=1000, maint_ratio=0.01 → virtual_maint=10 per FP
    snaps = [
        _snap(fp_id=1, coin="BTC", short_size=1.0, current_mark=1000.0, required_margin=30.0),
        _snap(fp_id=2, coin="ETH", short_size=1.0, current_mark=1000.0, required_margin=15.0),
        _snap(fp_id=3, coin="SOL", short_size=1.0, current_mark=1000.0, required_margin=20.0),
    ]
    maint_by_coin = {"BTC": 0.01, "ETH": 0.01, "SOL": 0.01}
    result = mgr.assess_account(
        account_equity_usdc=36.0,
        per_fp_snapshots=snaps,
        maint_ratio_by_coin=maint_by_coin,
    )
    assert result.account_status == MarginStatus.FORCED_CLOSE
    assert math.isclose(result.account_ratio, 1.2, rel_tol=1e-9)
    assert result.weakest_fp_id == 2


# ---------------------------------------------------------------------------
# test_assess_account_liquidation_picks_weakest
# ---------------------------------------------------------------------------

def test_assess_account_liquidation_picks_weakest():
    mgr = _mgr()
    # Same FP setup: maint=10 each, total=30
    # account_equity=27 → account_ratio=0.9 (LIQUIDATION_IMMINENT)
    snaps = [
        _snap(fp_id=1, coin="BTC", short_size=1.0, current_mark=1000.0, required_margin=30.0),
        _snap(fp_id=2, coin="ETH", short_size=1.0, current_mark=1000.0, required_margin=15.0),
        _snap(fp_id=3, coin="SOL", short_size=1.0, current_mark=1000.0, required_margin=20.0),
    ]
    maint_by_coin = {"BTC": 0.01, "ETH": 0.01, "SOL": 0.01}
    result = mgr.assess_account(
        account_equity_usdc=27.0,
        per_fp_snapshots=snaps,
        maint_ratio_by_coin=maint_by_coin,
    )
    assert result.account_status == MarginStatus.LIQUIDATION_IMMINENT
    assert result.weakest_fp_id == 2


# ---------------------------------------------------------------------------
# test_assess_account_empty_returns_inf_healthy
# ---------------------------------------------------------------------------

def test_assess_account_empty_returns_inf_healthy():
    mgr = _mgr()
    result = mgr.assess_account(
        account_equity_usdc=1000.0,
        per_fp_snapshots=[],
        maint_ratio_by_coin={},
    )
    assert math.isinf(result.account_ratio)
    assert result.account_status == MarginStatus.HEALTHY
    assert result.weakest_fp_id is None
    assert result.total_maintenance_usdc == 0.0
