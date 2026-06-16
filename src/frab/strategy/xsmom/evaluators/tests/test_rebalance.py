"""Reconcile tests for XsmomRebalance — KEEP / ADD / DROP / FLIP against a real DB.

Uses the in-memory SQLite engine + a real XsmomRepo so the keep-no-churn guarantee
is exercised end-to-end at the persistence layer. The exchange is a stub that needs
get_quote (for ADD sizing) and get_wallet (for envelope sizing).
Scores are injected directly so no daily-price history is required.
"""
from __future__ import annotations

import pytest

from frab.domain import Side, XsmomState
from frab.repo.xsmom_repo import XsmomRepo
from frab.strategy.xsmom.evaluators.rebalance import XsmomRebalance, is_rebalance_due
from frab.strategy.xsmom.params import XsmomParams

_NOW_MS = 1_704_067_200_000  # 2024-01-01 00:00 UTC

# 6-coin universe → auto tercile k = 6 // 3 = 2 per side.
_UNIVERSE = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")
# Scores descending: top-2 (AAA,BBB) → LONG, bottom-2 (EEE,FFF) → SHORT, CCC/DDD idle.
_SCORES = {"AAA": 5.0, "BBB": 4.0, "CCC": 3.0, "DDD": 2.0, "EEE": 1.0, "FFF": 0.0}

# Wallet large enough that effective = budget_cap (wallet not the binding constraint).
_WALLET = 5000.0


def _params() -> XsmomParams:
    return XsmomParams(budget_cap=1000.0, universe=_UNIVERSE)


def _expected_breakdown() -> dict:
    """Expected sizing for _params() + wallet=_WALLET."""
    return _params().sizing_breakdown(len(_UNIVERSE), _WALLET)


class _StubQuote:
    def __init__(self, mark: float) -> None:
        self.mark = mark
        self.spot = None


class _StubExchange:
    """Stub for reconcile: get_quote for ADD sizing, get_wallet for envelope calc."""

    name = "stub"

    async def get_quote(self, coin: str):
        return _StubQuote(mark=100.0)

    async def get_wallet(self, coin: str, kind) -> float:  # noqa: ANN001
        return _WALLET


def _rebalance(strategy_id: int, repo: XsmomRepo) -> XsmomRebalance:
    return XsmomRebalance(
        strategy_id=strategy_id,
        exchange=_StubExchange(),
        xsmom_repo=repo,
        params=_params(),
        session_factory=None,  # unused: reconcile only touches repo + exchange
        event_bus=None,
    )


# ── KEEP ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reconcile_keeps_same_coin_same_side(session_factory, strategy_id):
    repo = XsmomRepo(session_factory)
    # AAA already OPENED LONG and stays top-k long → KEEP, no transition.
    kept_fp = await repo.create(
        strategy_id=strategy_id, coin="AAA", side=Side.LONG,
        target_qty=1.0, initial_state=XsmomState.OPENED,
    )

    summary = await _rebalance(strategy_id, repo).reconcile(now_ms=_NOW_MS, scores=_SCORES)

    assert "AAA" in summary["kept"]
    # Still OPENED — untouched.
    after = await repo.get(kept_fp.id)
    assert after.state == XsmomState.OPENED


# ── ADD ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reconcile_adds_new_target_coin(session_factory, strategy_id):
    repo = XsmomRepo(session_factory)
    # Nothing held; BBB is a target LONG → a NEW row must be created.
    summary = await _rebalance(strategy_id, repo).reconcile(now_ms=_NOW_MS, scores=_SCORES)

    bd = _expected_breakdown()
    # k=2, per_leg = (budget - reserve) / 2 / k
    expected_notional = bd["per_leg"]
    expected_qty = expected_notional / 100.0  # stub mark = 100.0
    expected_margin = _params().compute_required_margin(expected_notional)

    new_rows = await repo.list_in_state(strategy_id, XsmomState.NEW)
    bbb = next((r for r in new_rows if r.coin == "BBB"), None)
    assert bbb is not None
    assert bbb.side == Side.LONG
    assert bbb.target_qty == pytest.approx(expected_qty)
    assert bbb.state_data["notional"] == pytest.approx(expected_notional)
    assert bbb.state_data["required_margin"] == pytest.approx(expected_margin)
    assert bbb.id in summary["opened"]


# ── DROP ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reconcile_drops_coin_not_in_target(session_factory, strategy_id):
    repo = XsmomRepo(session_factory)
    # CCC OPENED LONG but CCC is not in the target book (mid-tercile) → DROP to CLOSE.
    ccc = await repo.create(
        strategy_id=strategy_id, coin="CCC", side=Side.LONG,
        target_qty=1.0, initial_state=XsmomState.OPENED,
    )

    summary = await _rebalance(strategy_id, repo).reconcile(now_ms=_NOW_MS, scores=_SCORES)

    after = await repo.get(ccc.id)
    assert after.state == XsmomState.CLOSE
    assert after.state_data["exit_decision"] == "rebalance_drop"
    assert ccc.id in summary["dropped"]


# ── FLIP ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reconcile_flips_opposite_side(session_factory, strategy_id):
    repo = XsmomRepo(session_factory)
    # EEE OPENED LONG but target says EEE SHORT → old to CLOSE + new SHORT row.
    old = await repo.create(
        strategy_id=strategy_id, coin="EEE", side=Side.LONG,
        target_qty=1.0, initial_state=XsmomState.OPENED,
    )

    summary = await _rebalance(strategy_id, repo).reconcile(now_ms=_NOW_MS, scores=_SCORES)

    after_old = await repo.get(old.id)
    assert after_old.state == XsmomState.CLOSE
    assert after_old.state_data["exit_decision"] == "rebalance_flip"
    assert "EEE" in summary["flipped"]

    new_rows = await repo.list_in_state(strategy_id, XsmomState.NEW)
    eee_new = next((r for r in new_rows if r.coin == "EEE"), None)
    assert eee_new is not None
    assert eee_new.side == Side.SHORT


# ── in-flight NEW counts as held (no duplicate) ───────────────────────────────

@pytest.mark.asyncio
async def test_reconcile_in_flight_new_not_duplicated(session_factory, strategy_id):
    repo = XsmomRepo(session_factory)
    # AAA is already a NEW LONG (in-flight) and is a target LONG → must NOT duplicate.
    await repo.create(
        strategy_id=strategy_id, coin="AAA", side=Side.LONG,
        target_qty=1.0, initial_state=XsmomState.NEW,
    )

    await _rebalance(strategy_id, repo).reconcile(now_ms=_NOW_MS, scores=_SCORES)

    new_aaa = [r for r in await repo.list_in_state(strategy_id, XsmomState.NEW) if r.coin == "AAA"]
    assert len(new_aaa) == 1  # not duplicated


# ── is_rebalance_due ──────────────────────────────────────────────────────────

def test_is_rebalance_due_never_rebalanced():
    assert is_rebalance_due(_NOW_MS, None, _params()) is True


def test_is_rebalance_due_too_soon():
    # 1 day after last rebalance (< rebalance_days=7) → not due.
    one_day_later = _NOW_MS + 86_400_000
    assert is_rebalance_due(one_day_later, _NOW_MS, _params()) is False


def test_is_rebalance_due_after_window_on_anchor():
    # 2024-01-01 is a Monday. Add 10 days → 2024-01-11 is a Thursday (weekday 3).
    ten_days_later = _NOW_MS + 10 * 86_400_000
    assert is_rebalance_due(ten_days_later, _NOW_MS, _params()) is True


def test_is_rebalance_due_after_window_off_anchor():
    # 8 days later → 2024-01-09 is a Tuesday (weekday 1) → not the anchor day.
    eight_days_later = _NOW_MS + 8 * 86_400_000
    assert is_rebalance_due(eight_days_later, _NOW_MS, _params()) is False
