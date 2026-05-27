"""Top-level app builder: wires DB + engine + event bus into the FastAPI app."""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import AsyncIterator

from fastapi import FastAPI
from hyperliquid.utils import constants
from sqlalchemy import select

from frab.api.app import create_app
from frab.application.portfolio_service import PortfolioService
from frab.db.models import (
    EquitySnapshot,
    Exchange,
    FundingRate,
    Market,
    Position,
    PositionMode,
    PositionStatus,
    Price,
    Strategy,
)
from frab.db.recorder import DbRecorder
from frab.db.session import create_engine, make_session_factory, session_scope
from frab.engine.fee_reconciler import FeeReconciler
from frab.engine.funding_reconciler import FundingReconciler
from frab.engine.loop import Engine
from frab.engine.margin_manager import MarginManager, PerCoinSpec
from frab.engine.reconcile import scan as reconcile_scan
from frab.events.bus import EventBus, EventDbSink
from frab.exchanges.atomic import AtomicExecutor
from frab.exchanges.dry_run import DryRunAdapterGuard
from frab.exchanges.hyperliquid_adapter import HyperliquidAdapter
from frab.exchanges.base import Executor, FundingTick
from frab.exchanges.hyperliquid import HLExchangeReader
from frab.exchanges.hyperliquid_live import LiveHLExecutor
from frab.settings import Settings, get_settings
from frab.strategies.base import Strategy as StrategyBase
from frab.strategies.registry import get_strategy_spec, parse_params_override
from frab.strategies.strategy_a import (
    AccumulatorsSnapshot,
    OpenPositionSnapshot,
)

logger = logging.getLogger(__name__)


DEFAULT_COINS: tuple[str, ...] = ("BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE")
EXCHANGE_NAME = "hyperliquid"

# Wrapped-token map for HL mainnet spot. Used when hl_network == "mainnet".
#
# CRITICAL: spot leg only safe to use when the wrapped token is BOTH
#   (a) priced 1:1 with the HL canonical perp coin (real bridge, not separate
#       price discovery), AND
#   (b) has enough liquidity that a market order can match within slippage.
#
# Live audit 2026-05-19:
#   UBTC, UETH, USOL — tight spreads, deep books, 1:1 with perp → SAFE.
#   UAVAX — exists but UAVAX trades $8-9 while AVAX perp ~$13.5 (not 1:1) AND
#     top-of-book spread is ~9% → market orders fail. EXCLUDED.
#   LINK0, AAVE0, AVAX0 — HL's EVM bridges, independent price discovery,
#     break delta-neutrality (LINK0 incident: -$3 on supposedly hedged pos).
#     EXCLUDED.
#   DOGE, etc. — no spot pair on HL mainnet at all.
# Moved to frab.exchanges._hl_tokens in F2.3; re-export until F2.6
# moves the module under the hyperliquid/ package proper.
from frab.exchanges._hl_tokens import (  # noqa: E402
    MAINNET_SPOT_TOKEN_MAP,
    select_spot_token_map as _select_spot_token_map,
    validate_spot_pairs as _validate_spot_pairs,
)


def _select_coins(settings: Settings, default: tuple[str, ...]) -> tuple[str, ...]:
    """Universe from settings.hl_universe override, else `default`."""
    override = settings.universe_tuple()
    return override if override else default


def _position_mode(settings: Settings) -> PositionMode:
    return PositionMode.LIVE


def _build_params_override(settings: Settings) -> dict:
    """Merge strategy_params_json env override with HL-driven risk caps.

    position_size_usdc and concurrency_cap come from env-level risk knobs
    (FRAB_HL_POSITION_SIZE_USD, FRAB_HL_MAX_OPEN_POSITIONS) — keeps live
    caps in one place.
    """
    params_override = parse_params_override(settings.strategy_params_json) or {}
    params_override["position_size_usdc"] = settings.hl_position_size_usd
    params_override["concurrency_cap"] = settings.hl_max_open_positions
    return params_override


def _compute_auto_sizes(
    per_coin: dict[str, dict],
    *,
    budget_cap_usd: float,
    concurrency_cap: int,
    margin_buffer_x: float,
) -> dict[str, float]:
    """Auto-derive position_size_usd per coin from budget/K/leverage/buffer.

    Each open slot consumes `position_size * (1 + buffer/leverage)` of capital
    (spot leg + locked perp margin). Allocating equal $-per-slot:

        size_i = (budget_cap / K) / (1 + buffer / leverage_i)

    Coins with lower leverage get smaller position_size because they need
    a larger perp margin reserve per dollar of notional.
    """
    if concurrency_cap <= 0:
        raise ValueError("concurrency_cap must be > 0 for auto-sizing")
    per_slot = budget_cap_usd / concurrency_cap
    return {
        coin: per_slot / (1.0 + margin_buffer_x / p["leverage"])
        for coin, p in per_coin.items()
    }


def _build_margin_manager(settings: Settings) -> MarginManager | None:
    """Construct MarginManager from settings, or None for legacy uniform mode.

    Returns None when FRAB_PER_COIN_PARAMS_JSON is empty — strategy then runs
    without margin pre-flight or watchdog (backwards compat).

    When `position_size_usd` is omitted for ALL coins in the JSON, sizes are
    auto-derived from budget_cap / K / footprint(leverage, buffer).
    """
    per_coin = settings.per_coin_params()
    if per_coin is None:
        return None

    auto_sized = all("position_size_usd" not in p for p in per_coin.values())
    if auto_sized:
        sizes = _compute_auto_sizes(
            per_coin,
            budget_cap_usd=settings.budget_cap_usd,
            concurrency_cap=settings.hl_max_open_positions,
            margin_buffer_x=settings.margin_buffer_x,
        )
        logger.info(
            "auto-sized per-coin position sizes from budget=$%.2f K=%d buf=%.2fx: %s",
            settings.budget_cap_usd,
            settings.hl_max_open_positions,
            settings.margin_buffer_x,
            {c: round(s, 2) for c, s in sizes.items()},
        )
    else:
        sizes = {coin: p["position_size_usd"] for coin, p in per_coin.items()}

    specs = {
        coin: PerCoinSpec(
            position_size_usd=sizes[coin],
            leverage=p["leverage"],
            maint_ratio=p["maint_ratio"],
        )
        for coin, p in per_coin.items()
    }
    return MarginManager(
        per_coin_params=specs,
        margin_buffer_x=settings.margin_buffer_x,
        top_up_trigger=settings.top_up_trigger,
        healthy_ratio=settings.healthy_ratio,
        budget_cap_usd=settings.budget_cap_usd,
    )


def _hl_info_url(settings: Settings) -> str:
    """Return the /info endpoint URL for HLExchangeReader based on network."""
    if settings.hl_network == "testnet":
        return f"{constants.TESTNET_API_URL}/info"
    return f"{constants.MAINNET_API_URL}/info"


def _build_executor(
    settings: Settings,
    *,
    market_data,
) -> Executor:
    """Return a LiveHLExecutor for testnet/mainnet."""
    return LiveHLExecutor(
        private_key=settings.hl_private_key.get_secret_value(),
        account_address=settings.hl_account_address,
        network=settings.hl_network,
        spot_token_map=_select_spot_token_map(settings.hl_network),
        slippage=settings.hl_live_slippage,
    )


def _build_fee_reconciler(
    settings: Settings,
    *,
    session_factory,
    market_data,
    bus: EventBus,
    portfolio_service: "PortfolioService | None" = None,
    strategy_id: int | None = None,
) -> "FeeReconciler | None":
    """Return a FeeReconciler for live trading."""
    return FeeReconciler(
        session_factory=session_factory,
        market_data=market_data,
        user_address=settings.hl_account_address,
        bus=bus,
        portfolio_service=portfolio_service,
        strategy_id=strategy_id,
    )


def _build_wallet_snapshotter(
    settings: Settings,
    *,
    session_factory,
    executor,
    recorder: "DbRecorder",
):
    """Return an async callable that records a WalletSnapshot, or None for testnet."""
    if settings.hl_network != "mainnet":
        return None

    async def _snapshot() -> None:
        now = datetime.now(UTC)
        # Fetch latest mark per coin from DB to price spot holdings
        mark_prices: dict[str, float] = {}
        async with session_scope(session_factory) as s:
            rows = await s.execute(
                select(Market.coin, Price.mark, Price.ts)
                .join(Price, Price.market_id == Market.id)
                .order_by(Price.ts.desc())
                .limit(500)
            )
            for coin, mark, _price_ts in rows.all():
                if coin not in mark_prices:
                    mark_prices[coin] = float(mark)

        raw = await executor.fetch_wallet_state(mark_prices=mark_prices)
        spot_equity = sum(b["usd_value"] for b in raw["spot_balances"])
        await recorder.record_wallet_snapshot(
            ts=now,
            account_value=raw["total_usd"],
            perp_equity=raw["perp_account_value"],
            spot_equity=spot_equity,
            withdrawable=raw["usdc_spot"],
        )

    return _snapshot


def _build_funding_reconciler(
    settings: Settings,
    *,
    session_factory,
    market_data,
    bus: EventBus,
    portfolio_service: "PortfolioService | None" = None,
    strategy_id: int | None = None,
) -> "FundingReconciler | None":
    """Return a FundingReconciler for live trading."""
    return FundingReconciler(
        session_factory=session_factory,
        market_data=market_data,
        user_address=settings.hl_account_address,
        bus=bus,
        portfolio_service=portfolio_service,
        strategy_id=strategy_id,
    )


async def _ensure_strategy(session_factory, params_json: dict, *, name: str, version: str, instance_token: str) -> int:
    async with session_scope(session_factory) as s:
        # Defensive cleanup: mark any leftover 'running' strategies as 'stopped'
        # (handles crash recovery when previous process exited uncleanly).
        leftover = await s.execute(
            select(Strategy).where(Strategy.status == "running")
        )
        now_utc = datetime.now(UTC)
        for st in leftover.scalars().all():
            st.status = "stopped"
            st.stopped_at = now_utc
            st.instance_token = None

        # Find or create the strategy row.
        result = await s.execute(
            select(Strategy).where(
                Strategy.name == name,
                Strategy.version == version,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            existing = Strategy(
                name=name,
                version=version,
                params_json=params_json,
                status="idle",
            )
            s.add(existing)
            await s.flush()
        else:
            # Refresh params_json on each start so DB reflects current config
            # (env-driven coins/sizing/cap can change between runs).
            existing.params_json = params_json

        # Mark this strategy as running.
        existing.status = "running"
        existing.started_at = now_utc
        existing.stopped_at = None
        existing.instance_token = instance_token
        return existing.id


async def _mark_stopped_if_owner(session_factory, strategy_id: int, instance_token: str) -> bool:
    """Mark strategy stopped only if its instance_token matches ours.

    Returns True if we updated the row (we were the live owner), False if
    a newer process has already taken ownership (different token).
    """
    async with session_scope(session_factory) as s:
        row = await s.get(Strategy, strategy_id)
        if row is None:
            return False
        if row.instance_token != instance_token:
            return False
        row.status = "stopped"
        row.stopped_at = datetime.now(UTC)
        row.instance_token = None
        return True


async def _load_funding_from_db(
    session_factory,
    strategy: StrategyBase,
    coins: tuple[str, ...],
    window_hours: int,
) -> int:
    """Prime the strategy's MarketState from the latest funding rows in DB.

    Pulls up to `window_hours` rows per coin (most recent first, then reversed
    to ascending order). No network calls — historical data must already have
    been written by `frab backfill` or accumulated by a prior `frab serve` run.
    """
    ticks_by_coin: dict[str, list[FundingTick]] = {}
    async with session_scope(session_factory) as s:
        for coin in coins:
            stmt = (
                select(FundingRate)
                .join(Market, FundingRate.market_id == Market.id)
                .where(Market.coin == coin)
                .order_by(FundingRate.ts.desc())
                .limit(window_hours)
            )
            result = await s.execute(stmt)
            rows = result.scalars().all()
            ticks_by_coin[coin] = [
                FundingTick(
                    coin=coin,
                    ts=fr.ts if fr.ts.tzinfo is not None else fr.ts.replace(tzinfo=UTC),
                    rate=fr.rate,
                    premium=fr.premium,
                    annualized_pct=fr.annualized_pct,
                )
                for fr in reversed(rows)
            ]

    applied = strategy.warmup_from_history(ticks_by_coin)
    per_coin = {c: len(ts) for c, ts in ticks_by_coin.items()}
    logger.info("load_funding_from_db: applied=%d, per_coin=%s", applied, per_coin)
    return applied


async def _rehydrate_strategy_from_db(
    session_factory,
    strategy: StrategyBase,
    strategy_id: int,
) -> None:
    """Restore in-memory positions + cash/accumulators after engine restart.

    Reads OPEN positions and the latest equity snapshot for this strategy.
    Leaves construction defaults in place if nothing persisted (fresh start).
    """
    async with session_scope(session_factory) as s:
        pos_rows = (await s.execute(
            select(Position, Market.coin)
            .join(Market, Position.market_id == Market.id)
            .where(
                Position.strategy_id == strategy_id,
                Position.status == PositionStatus.OPEN,
            )
        )).all()

        last_eq = (await s.execute(
            select(EquitySnapshot)
            .where(EquitySnapshot.strategy_id == strategy_id)
            .order_by(EquitySnapshot.ts.desc())
            .limit(1)
        )).scalar_one_or_none()

    snapshots = [
        OpenPositionSnapshot(
            coin=coin,
            opened_at=pos.opened_at if pos.opened_at.tzinfo is not None
                      else pos.opened_at.replace(tzinfo=UTC),
            spot_qty=pos.spot_units,
            perp_qty=abs(pos.perp_units),
            entry_spot_price=pos.entry_spot_price,
            entry_perp_price=pos.entry_perp_price,
            funding_collected=pos.funding_collected,
            fees_paid=pos.fees_paid,
            position_min_hold_hours=pos.position_min_hold_hours,
            consec_negative_hours=pos.consec_negative_hours,
        )
        for pos, coin in pos_rows
    ]

    accumulators = None
    if last_eq is not None:
        accumulators = AccumulatorsSnapshot(
            cash=last_eq.cash,
            realized_pnl_cum=last_eq.perp_realized_cum,
            funding_cum=last_eq.funding_cum,
            fees_cum=last_eq.fees_cum,
        )

    if snapshots or accumulators is not None:
        strategy.rehydrate(positions=snapshots, accumulators=accumulators)
        logger.info(
            "rehydrate_strategy: positions=%d, cash=%s, funding_cum=%s",
            len(snapshots),
            accumulators.cash if accumulators else "default",
            accumulators.funding_cum if accumulators else "default",
        )


async def _resolve_exchange(session_factory) -> int:
    async with session_scope(session_factory) as s:
        result = await s.execute(select(Exchange).where(Exchange.name == EXCHANGE_NAME))
        exc = result.scalar_one_or_none()
        if exc is None:
            raise RuntimeError(
                f"Exchange {EXCHANGE_NAME!r} not seeded; run `frab seed` first."
            )
        return exc.id


def build_app(coins: tuple[str, ...] = DEFAULT_COINS, *, dry_run: bool = False) -> FastAPI:
    settings = get_settings()
    db_engine = create_engine(settings.db_url)
    session_factory = make_session_factory(db_engine)
    bus = EventBus()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # settings-driven universe takes precedence over the explicit `coins` arg
        resolved_coins = _select_coins(settings, coins)

        exchange_id = await _resolve_exchange(session_factory)

        market_data = HLExchangeReader(
            api_url=_hl_info_url(settings),
            timeout_s=settings.hl_request_timeout_s,
        )

        # Validate spot pairs on mainnet: every coin in the universe must have a
        # spot/USDC pair on HL whose base token matches MAINNET_SPOT_TOKEN_MAP.
        # Prevents trading the canonical perp against an unrelated wrapped token
        # (e.g. "LINK" perp vs "LINK0/USDC" spot — not 1:1, broke hedge once).
        if settings.hl_network == "mainnet":
            await _validate_spot_pairs(market_data, resolved_coins)
        executor = _build_executor(
            settings,
            market_data=market_data,
        )
        atomic = AtomicExecutor(
            executor,
            bus,
            max_attempts=3,
            sleep_between_attempts=(2.0, 5.0),
        )

        # F2.5: build HyperliquidAdapter by composing the primitives we just
        # built. In dry-run, wrap with DryRunAdapterGuard so write methods
        # are intercepted at the infrastructure level (defence in depth).
        # F2.7 wires consumers (wallet route, future strategy migration)
        # to read from app.state.adapter; F2.8 retires _build_executor.
        adapter = HyperliquidAdapter(
            market_data=market_data,
            live_executor=executor,
            atomic=atomic,
            network=settings.hl_network,
            user_address=settings.hl_account_address,
        )
        if dry_run:
            adapter = DryRunAdapterGuard(adapter)
        app.state.adapter = adapter

        spec = get_strategy_spec(settings.strategy_name)
        params_override = _build_params_override(settings)
        margin_manager = _build_margin_manager(settings)
        if margin_manager is not None:
            logger.info(
                "margin_manager enabled: %d coins, buffer=%.1fx, "
                "trigger=%.2f, healthy=%.2f, budget=$%.0f",
                len(margin_manager._params),
                margin_manager.margin_buffer_x,
                margin_manager.top_up_trigger,
                margin_manager.healthy_ratio,
                margin_manager.budget_cap_usd,
            )
        strategy, params_json = spec.build(
            coins=resolved_coins,
            params_override=params_override,
            executor=atomic,
            dry_run=dry_run,
            margin_manager=margin_manager,
        )

        instance_token = uuid.uuid4().hex
        strategy_id = await _ensure_strategy(
            session_factory, params_json, name=spec.name, version=spec.version, instance_token=instance_token
        )
        recorder = DbRecorder(
            session_factory,
            strategy_id=strategy_id,
            exchange_id=exchange_id,
            mode=_position_mode(settings),
        )
        await recorder.prime()

        # Derive signal_window_hours from strategy params for DB warmup
        signal_window_hours = params_json.get("signal_window_hours", 12)
        await _load_funding_from_db(
            session_factory, strategy, resolved_coins, signal_window_hours
        )
        await _rehydrate_strategy_from_db(session_factory, strategy, strategy_id)
        await reconcile_scan(session_factory, strategy_id, bus)   # ← new

        # Build PortfolioService — seeded from current strategy state so that
        # the initial cash is consistent with what strategy already tracks.
        from frab.domain.exchange import Exchange as DomainExchange
        from sqlalchemy import func
        strat_cash = float(getattr(strategy, "cash", 0.0))
        strat_perp_cash = float(getattr(strategy, "perp_cash", 0.0))
        async with session_scope(session_factory) as _ps_session:
            committed = (await _ps_session.execute(
                select(func.coalesce(
                    func.sum(Position.notional_usd + Position.margin_reserve_usd), 0.0
                ))
                .where(Position.strategy_id == strategy_id)
                .where(Position.status == PositionStatus.OPEN)
            )).scalar() or 0.0
        initial_cash = strat_cash + strat_perp_cash + float(committed)
        portfolio_service = PortfolioService(
            session_factory=session_factory,
            strategy_id=strategy_id,
            initial_cash_per_exchange={DomainExchange.HYPERLIQUID: initial_cash},
        )
        await portfolio_service.rehydrate_from_db()
        app.state.portfolio_service = portfolio_service
        # Late-bind PortfolioService into the strategy so dual-track mirror calls
        # (set_fees_cum / set_funding_cum) actually fire. F1.4 will move this
        # into spec.build so the wiring is no longer a separate step.
        if hasattr(strategy, "set_portfolio_service"):
            strategy.set_portfolio_service(portfolio_service)

        # Wire fee reconciler for live mode; paper mode has no real fills to reconcile.
        fee_reconciler = _build_fee_reconciler(
            settings,
            session_factory=session_factory,
            market_data=market_data,
            bus=bus,
            portfolio_service=portfolio_service,
            strategy_id=strategy_id,
        )

        # Wire funding reconciler for live mode; paper mode skips HL userFunding.
        funding_reconciler = _build_funding_reconciler(
            settings,
            session_factory=session_factory,
            market_data=market_data,
            bus=bus,
            portfolio_service=portfolio_service,
            strategy_id=strategy_id,
        )

        # Wire wallet snapshotter for live mode; paper mode has no wallet to snapshot.
        wallet_snapshotter = _build_wallet_snapshotter(
            settings,
            session_factory=session_factory,
            executor=executor,
            recorder=recorder,
        )

        engine = Engine(
            market_data=market_data,
            strategy=strategy,
            portfolio_service=portfolio_service,
            coins=resolved_coins,
            recorder=recorder,
            event_bus=bus,
            fee_reconciler=fee_reconciler,
            funding_reconciler=funding_reconciler,
            wallet_snapshotter=wallet_snapshotter,
        )
        sink = EventDbSink(session_factory, bus)

        def _on_task_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.error("Background task %s failed", task.get_name(), exc_info=exc)

        sink_task = asyncio.create_task(sink.run(), name="event-sink")
        sink_task.add_done_callback(_on_task_done)
        await sink.wait_until_subscribed()
        engine_task = asyncio.create_task(engine.run(), name="engine")
        engine_task.add_done_callback(_on_task_done)
        logger.info(
            "frab serve: started (strategy=%s/%s, strategy_id=%d, network=%s, mode=%s, coins=%s)",
            spec.name, spec.version, strategy_id, settings.hl_network, _position_mode(settings).value, resolved_coins,
        )

        app.state.strategy = strategy
        app.state.strategy_id = strategy_id
        app.state.engine = engine
        app.state.executor = executor

        try:
            yield
        finally:
            try:
                updated = await _mark_stopped_if_owner(session_factory, app.state.strategy_id, instance_token)
                if not updated:
                    logger.info(
                        "skipped marking strategy stopped: another process now owns id=%d",
                        app.state.strategy_id,
                    )
            except Exception:
                logger.exception("failed to mark strategy as stopped on shutdown")

            app.state.strategy = None
            app.state.strategy_id = None
            app.state.engine = None
            app.state.portfolio_service = None
            app.state.adapter = None
            engine.stop()
            await asyncio.gather(engine_task, return_exceptions=True)
            await sink.stop()
            await asyncio.gather(sink_task, return_exceptions=True)
            await market_data.aclose()
            await db_engine.dispose()
            logger.info("frab serve: shutdown complete")

    app = create_app(session_factory, event_bus=bus)
    app.router.lifespan_context = lifespan
    return app
