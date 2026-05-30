"""Database-related CLI commands."""
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy
import typer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from frab.settings import PROJECT_ROOT, get_settings
from frab.db.models import Exchange, FundingRate, Market
from frab.db.session import session_scope
from frab.exchanges.hyperliquid.exchange import HLExchange as HLExchangeReader
from frab.exchanges.protocol import Exchange as ExchangeDataSource
from alembic import command
from alembic.config import Config

EXCHANGE_NAME = "hyperliquid"

HYPERLIQUID_SPEC = {
    "name": "hyperliquid",
    "funding_interval_h": 1,
    "spot_taker_bps": 7.0,
    "perp_taker_bps": 3.5,
}

# Discovered placeholder values for MVP. Phase 2 will refresh from HL `meta` endpoint.
HYPERLIQUID_MARKETS = [
    # coin, min_size, tick_size — sensible defaults; refined in Phase 2
    ("BTC",  0.0001,  1.0),
    ("ETH",  0.001,   0.1),
    ("SOL",  0.01,    0.01),
    ("AVAX", 0.01,    0.001),
    ("LINK", 0.1,     0.001),
    ("AAVE", 0.01,    0.01),
    ("DOGE", 1.0,     0.00001),
]


def _sync_db_url(async_url: str) -> str:
    return async_url.replace("sqlite+aiosqlite://", "sqlite://")


def init_db() -> None:
    """Initialise the database schema via Alembic (alembic upgrade head)."""
    settings = get_settings()
    # Ensure the data directory exists before Alembic tries to create the DB file.
    db_path = settings.db_url.replace("sqlite+aiosqlite:///", "")
    if db_path and not db_path.startswith(":"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    # env.py creates an async engine; pass the async URL (sqlite+aiosqlite://)
    alembic_cfg.set_main_option("sqlalchemy.url", settings.db_url)
    command.upgrade(alembic_cfg, "head")
    typer.echo(f"Database initialised: {settings.db_url}")


def seed() -> None:
    """Seed the database with Hyperliquid exchange and markets (idempotent)."""
    settings = get_settings()
    sync_url = _sync_db_url(settings.db_url)
    engine = sqlalchemy.create_engine(sync_url, future=True)

    added_exchanges = 0
    skipped_exchanges = 0
    added_markets = 0
    skipped_markets = 0

    with Session(engine) as session:
        # Exchange — insert or retrieve existing
        existing_exchange = session.execute(
            select(Exchange).where(Exchange.name == HYPERLIQUID_SPEC["name"])
        ).scalar_one_or_none()

        if existing_exchange is not None:
            exchange_id = existing_exchange.id
            skipped_exchanges += 1
        else:
            exchange = Exchange(
                name=HYPERLIQUID_SPEC["name"],
                funding_interval_h=HYPERLIQUID_SPEC["funding_interval_h"],
                spot_taker_bps=HYPERLIQUID_SPEC["spot_taker_bps"],
                perp_taker_bps=HYPERLIQUID_SPEC["perp_taker_bps"],
            )
            session.add(exchange)
            session.flush()
            exchange_id = exchange.id
            added_exchanges += 1

        # Markets — insert or skip existing
        for coin, min_size, tick_size in HYPERLIQUID_MARKETS:
            existing_market = session.execute(
                select(Market).where(
                    Market.exchange_id == exchange_id,
                    Market.coin == coin,
                )
            ).scalar_one_or_none()

            if existing_market is not None:
                skipped_markets += 1
            else:
                market = Market(
                    exchange_id=exchange_id,
                    coin=coin,
                    min_size=min_size,
                    tick_size=tick_size,
                )
                session.add(market)
                added_markets += 1

        session.commit()

    typer.echo(
        f"Seed complete — "
        f"exchanges: {added_exchanges} added, {skipped_exchanges} skipped; "
        f"markets: {added_markets} added, {skipped_markets} skipped."
    )


async def _backfill_funding_async(
    session_factory: async_sessionmaker[AsyncSession],
    market_data: ExchangeDataSource,
    coins: tuple[str, ...],
    hours: int,
) -> dict[str, int]:
    """Fetch HL funding history for each coin, write idempotently to DB.

    Returns {coin: ticks_added}.
    """
    now = datetime.now(UTC)
    since_ms = int((now - timedelta(hours=hours)).timestamp() * 1000)

    async with session_scope(session_factory) as s:
        result = await s.execute(select(Exchange).where(Exchange.name == EXCHANGE_NAME))
        exc = result.scalar_one_or_none()
        if exc is None:
            raise RuntimeError(f"Exchange {EXCHANGE_NAME!r} not seeded; run `frab seed` first.")
        exchange_id = exc.id
        seeded_coins_result = await s.execute(select(Market.coin).where(Market.exchange_id == exchange_id))
        seeded_coins = {row for (row,) in seeded_coins_result.all()}

    counts: dict[str, int] = {}
    for coin in coins:
        if coin not in seeded_coins:
            typer.echo(f"  {coin}: unknown coin (not seeded), skipped")
            counts[coin] = 0
            continue
        ticks = await market_data.fetch_funding_history(coin, since_ms)
        added = 0
        async with session_scope(session_factory) as s:
            for tick in ticks:
                tick_ms = tick.ts_ms
                existing = await s.scalar(
                    select(FundingRate.id).where(
                        FundingRate.exchange_id == exchange_id,
                        FundingRate.coin == coin,
                        FundingRate.ts_ms == tick_ms,
                    )
                )
                if existing is not None:
                    continue
                s.add(FundingRate(
                    exchange_id=exchange_id,
                    coin=coin,
                    ts_ms=tick_ms,
                    rate=tick.rate,
                    premium=tick.premium,
                    annualized_pct=tick.annualized_pct,
                ))
                added += 1
        counts[coin] = added
    return counts


def backfill(
    hours: int = 24,
    coins: str = "BTC,ETH,SOL,AVAX,LINK,AAVE,DOGE",
) -> None:
    """Fetch funding history from Hyperliquid and write to DB (idempotent)."""
    settings = get_settings()
    coin_tuple = tuple(c.strip().upper() for c in coins.split(",") if c.strip())

    async def _run() -> dict[str, int]:
        engine = create_async_engine(settings.db_url, future=True)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        market_data = HLExchangeReader(
            api_url=settings.hl_api_url,
            timeout_s=settings.hl_request_timeout_s,
        )
        try:
            return await _backfill_funding_async(session_factory, market_data, coin_tuple, hours)
        finally:
            await market_data.aclose()
            await engine.dispose()

    counts = asyncio.run(_run())
    total = sum(counts.values())
    typer.echo(f"Backfill complete — {total} ticks added across {len(coin_tuple)} coins:")
    for coin, n in counts.items():
        typer.echo(f"  {coin}: {n} added")


def register(app: typer.Typer) -> None:
    app.command()(init_db)
    app.command()(seed)
    app.command()(backfill)
