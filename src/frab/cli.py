from pathlib import Path

import typer
import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session
import sqlalchemy

from frab.settings import PROJECT_ROOT, get_settings
from frab.db.models import Exchange, Market

app = typer.Typer(no_args_is_help=True, add_completion=False)

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


@app.command()
def init_db() -> None:
    """Apply Alembic migrations to head on the configured database."""
    settings = get_settings()
    cfg = Config()
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "src/frab/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.db_url)
    command.upgrade(cfg, "head")
    typer.echo(f"Database initialised: {settings.db_url}")


@app.command()
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


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    coins: str = "BTC,ETH,SOL,AVAX,LINK,AAVE,DOGE",
) -> None:
    """Run shadow-trading engine + FastAPI server on the configured DB."""
    from frab.server import build_app

    coin_tuple = tuple(c.strip().upper() for c in coins.split(",") if c.strip())
    asgi_app = build_app(coin_tuple)
    uvicorn.run(asgi_app, host=host, port=port)
