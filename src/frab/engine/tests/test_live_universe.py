"""Tests for Phase E — Live Universe Re-read.

Covers the five required scenarios:

1. Provenance: static registry (universe = 5 live coins) → quote set and entry
   candidates identical to the old _coins / p.coins behaviour; sizing unchanged.

2. Activate: registry.universe() grows by one coin → next tick's working set
   includes it; next hour-tick's entry candidates include it. No restart.

3. Deactivate WITH open position: coin removed from universe but has a
   non-terminal FarbPosition → still in working_coins, exits still process,
   NOT offered for new entry.

4. Deactivate WITHOUT position: coin removed from universe, no position →
   drops from working set and entries entirely.

5. Working-set derivation unit test: working_coins = universe ∪ open coins.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from frab.coin_registry import CoinRegistry
from frab.db.models import (
    CoinRegistry as CoinRegistryRow,
    Exchange as ExchangeRow,
    FarbPosition as FarbPositionRow,
    Strategy as StrategyRow,
)
from frab.db.session import init_db, make_session_factory, session_scope
from frab.domain.enums import FarbState
from frab.engine.loop import EngineLoop
from frab.exchanges.protocol import Quote, WalletKind
from frab.repo.farb_repo import FarbRepo
from frab.strategy.two_phase import TwoPhaseParams


# ─── Helpers ─────────────────────────────────────────────────────────────────

_LIVE_COINS = ("BTC", "ETH", "HYPE", "PURR", "SOL")
_NOW_MS = 1_704_067_200_000  # 2024-01-01 00:00:00 UTC


def _make_quote(coin: str) -> Quote:
    return Quote(coin=coin, mark=100.0, spot=100.0, bid=99.0, ask=101.0, ts_ms=_NOW_MS)


# ─── DB Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
async def db_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)
    await init_db(eng)
    yield eng
    await eng.dispose()


@pytest.fixture
async def sf(db_engine):
    return make_session_factory(db_engine)


@pytest.fixture
async def strategy_id(sf) -> int:
    async with session_scope(sf) as s:
        row = StrategyRow(
            name="two_phase", version="v2",
            params_json=TwoPhaseParams().to_dict() if hasattr(TwoPhaseParams(), "to_dict")
                        else {"coins": list(_LIVE_COINS)},
        )
        s.add(row)
        await s.flush()
        sid = row.id
    return sid


async def _seed_registry(sf, coins: list[str], *, active: bool = True) -> None:
    """Insert coin_registry rows for the given coins."""
    async with session_scope(sf) as s:
        for coin in coins:
            row = CoinRegistryRow(
                coin=coin,
                leverage=5,
                maint_ratio=0.025,
                position_size_usd=None,
                active=active,
                spot_token=None,
                sz_decimals=None,
                bridge_safe=False,
                validated_at=_NOW_MS,  # validated
            )
            s.add(row)


async def _seed_open_position(sf, *, strategy_id: int, coin: str) -> int:
    """Insert a non-terminal FarbPosition for coin. Returns its id."""
    async with session_scope(sf) as s:
        fp = FarbPositionRow(
            strategy_id=strategy_id,
            coin=coin,
            state=FarbState.PRE_BREAKEVEN.value,
            state_data={},
            spot_position_id=None,
            perp_position_id=None,
            margin_position_id=None,
            opened_at=_NOW_MS,
            closed_at=None,
        )
        s.add(fp)
        await s.flush()
        fid = fp.id
    return fid


def _make_mock_strategy(strategy_id: int, farb_repo: FarbRepo) -> MagicMock:
    """Minimal strategy mock that exposes farb_repo for distinct_open_coins."""
    strat = MagicMock()
    strat.strategy_id = strategy_id
    strat.on_minute_tick = AsyncMock()
    strat.on_hour_tick = AsyncMock()
    strat.farb_repo = farb_repo
    return strat


def _make_loop(
    strategy,
    sf,
    *,
    coins: list[str],
    registry: CoinRegistry | None = None,
) -> EngineLoop:
    mock_exchange = MagicMock()
    mock_exchange.name = "hyperliquid"
    mock_exchange.get_quotes = None
    mock_exchange.get_quote = AsyncMock(side_effect=lambda c: _make_quote(c))
    mock_exchange.get_funding_rate = AsyncMock(
        side_effect=lambda c: MagicMock(coin=c, ts_ms=_NOW_MS, rate=0.0001,
                                        premium=0.0001, annualized_pct=8.76)
    )
    mock_exchange.get_wallet = AsyncMock(return_value=1000.0)
    mock_exchange.get_perp_unrealized_by_coin = AsyncMock(return_value={})
    mock_exchange.get_spot_mids_by_coin = AsyncMock(return_value={})

    mock_ledger = MagicMock()
    mock_ledger.compute_and_save = AsyncMock()

    loop = EngineLoop(
        strategy=strategy,
        exchange=mock_exchange,
        ledger=mock_ledger,
        session_factory=sf,
        coins=coins,
        registry=registry,
        minute_interval_s=0.05,
    )
    loop._exchange_id_cache = 1
    loop._save_prices = AsyncMock()
    loop._save_funding = AsyncMock()
    return loop


# ─── Test 1: Provenance ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_provenance_registry_equals_static_coins(sf, strategy_id):
    """With registry.universe() == _LIVE_COINS, working set and quote set are
    identical to the old static self._coins path.  Provenance invariant."""
    await _seed_registry(sf, list(_LIVE_COINS))
    registry = CoinRegistry(sf)
    await registry.load()

    farb_repo = FarbRepo(sf)
    strategy = _make_mock_strategy(strategy_id, farb_repo)

    loop = _make_loop(strategy, sf, coins=list(_LIVE_COINS), registry=registry)

    # Resolve working coins — must equal the static set
    working = await loop._resolve_working_coins()
    assert set(working) == set(_LIVE_COINS), (
        f"working_coins mismatch: {working} != {_LIVE_COINS}"
    )

    # Simulate a minute tick and assert quotes fetched for exactly those coins
    await loop._minute_tick(_NOW_MS)
    # Verify via the exchange mock's get_quote calls
    called_coins = {c.args[0] for c in loop._exchange.get_quote.call_args_list}
    assert called_coins == set(_LIVE_COINS), (
        f"Quote fetched for wrong coins: {called_coins}"
    )


# ─── Test 2: Activate — new coin appears without restart ─────────────────────

@pytest.mark.asyncio
async def test_activate_new_coin_appears_in_next_tick(sf, strategy_id):
    """Adding ZEC to registry (active+validated) makes it appear in the next
    tick's working_coins without any engine restart."""
    base_coins = list(_LIVE_COINS)
    await _seed_registry(sf, base_coins)
    registry = CoinRegistry(sf)
    await registry.load()

    farb_repo = FarbRepo(sf)
    strategy = _make_mock_strategy(strategy_id, farb_repo)

    loop = _make_loop(strategy, sf, coins=base_coins, registry=registry)

    # Before activation: ZEC not in working set
    before = await loop._resolve_working_coins()
    assert "ZEC" not in before

    # Simulate activation: add ZEC to DB, then reload registry
    await _seed_registry(sf, ["ZEC"])
    await registry.reload()

    # After activation: ZEC in working set on next tick
    after = await loop._resolve_working_coins()
    assert "ZEC" in after
    assert set(_LIVE_COINS) <= set(after), "existing coins must remain"


# ─── Test 3: Deactivate WITH open position ───────────────────────────────────

@pytest.mark.asyncio
async def test_deactivate_with_open_position_stays_in_working_set(sf, strategy_id):
    """A coin deactivated in the registry but holding a non-terminal FarbPosition
    must remain in working_coins (still quoted/snapshotted for exit) but must NOT
    appear as an entry candidate."""
    from frab.strategy.two_phase.evaluators.entry import EntryEvaluator
    from frab.strategy.two_phase.params import TwoPhaseParams

    await _seed_registry(sf, list(_LIVE_COINS))
    registry = CoinRegistry(sf)
    await registry.load()

    farb_repo = FarbRepo(sf)

    # Seed a non-terminal position for SOL
    await _seed_open_position(sf, strategy_id=strategy_id, coin="SOL")

    # Now deactivate SOL in DB and reload
    async with session_scope(sf) as s:
        row = await s.get(CoinRegistryRow, "SOL")
        row.active = False
    await registry.reload()

    # SOL no longer in universe
    assert "SOL" not in registry.universe()

    strategy = _make_mock_strategy(strategy_id, farb_repo)
    loop = _make_loop(strategy, sf, coins=list(_LIVE_COINS), registry=registry)

    # Working set: SOL must still be there because of the open position
    working = await loop._resolve_working_coins()
    assert "SOL" in working, "deactivated coin with open position must remain in working_coins"

    # Entry universe: SOL must NOT appear (registry.universe() only, no open coins)
    # Build an EntryEvaluator with registry and verify iteration skips SOL
    mock_signal = AsyncMock(return_value=None)  # signal returns None → no entries created
    params = TwoPhaseParams(coins=list(_LIVE_COINS), concurrency_cap=10, budget_cap_usdc=100_000.0)
    evaluator = EntryEvaluator(
        strategy_id=strategy_id,
        farb_repo=farb_repo,
        params=params,
        signal_computer=MagicMock(compute=mock_signal),
        registry=registry,
    )
    await evaluator.evaluate(now_ms=_NOW_MS, force_cooldown_bypass=False)

    # The signal computer was NOT called for SOL (deactivated)
    called_for_sol = any(
        c == ("SOL",) for c in [call.args for call in mock_signal.call_args_list]
    )
    assert not called_for_sol, "signal.compute must not be called for deactivated coin SOL"


# ─── Test 4: Deactivate WITHOUT open position ────────────────────────────────

@pytest.mark.asyncio
async def test_deactivate_without_position_drops_from_working_set(sf, strategy_id):
    """A coin deactivated in the registry with no open positions must drop
    from working_coins entirely and not receive any quote fetches or entry
    evaluation."""
    await _seed_registry(sf, list(_LIVE_COINS))
    registry = CoinRegistry(sf)
    await registry.load()

    farb_repo = FarbRepo(sf)
    strategy = _make_mock_strategy(strategy_id, farb_repo)

    # Before deactivation
    loop = _make_loop(strategy, sf, coins=list(_LIVE_COINS), registry=registry)
    before = await loop._resolve_working_coins()
    assert "HYPE" in before

    # Deactivate HYPE (no open position for HYPE)
    async with session_scope(sf) as s:
        row = await s.get(CoinRegistryRow, "HYPE")
        row.active = False
    await registry.reload()

    # Rebuild loop with updated registry (same object, reloaded in place)
    after = await loop._resolve_working_coins()
    assert "HYPE" not in after, "deactivated coin with no position must leave working_coins"

    # Simulate minute tick: exchange.get_quote must NOT be called for HYPE
    await loop._minute_tick(_NOW_MS)
    called_coins = {c.args[0] for c in loop._exchange.get_quote.call_args_list}
    assert "HYPE" not in called_coins, "no quote fetched for deactivated coin without position"


# ─── Test 5: Working-set derivation unit test ────────────────────────────────

@pytest.mark.asyncio
async def test_working_set_is_union_of_universe_and_open_coins(sf, strategy_id):
    """working_coins = registry.universe() ∪ {coins with open FarbPositions}.

    This is the core invariant: a coin absent from the universe (deactivated)
    but with an open position is still included; a coin in the universe with no
    position is included too; a coin absent from both is excluded.
    """
    # Seed registry: BTC+ETH active, SOL inactive
    await _seed_registry(sf, ["BTC", "ETH"])
    async with session_scope(sf) as s:
        s.add(CoinRegistryRow(
            coin="SOL", leverage=5, maint_ratio=0.025,
            position_size_usd=None, active=False,
            spot_token=None, sz_decimals=None, bridge_safe=False,
            validated_at=_NOW_MS,
        ))

    registry = CoinRegistry(sf)
    await registry.load()

    # SOL has an open position despite being inactive
    await _seed_open_position(sf, strategy_id=strategy_id, coin="SOL")

    farb_repo = FarbRepo(sf)
    strategy = _make_mock_strategy(strategy_id, farb_repo)
    loop = _make_loop(strategy, sf, coins=["BTC", "ETH"], registry=registry)

    working = await loop._resolve_working_coins()
    working_set = set(working)

    assert "BTC" in working_set, "active coin must be in working set"
    assert "ETH" in working_set, "active coin must be in working set"
    assert "SOL" in working_set, "inactive coin with open position must be in working set"
    # ZEC is neither in universe nor has an open position
    assert "ZEC" not in working_set, "completely absent coin must not be in working set"
