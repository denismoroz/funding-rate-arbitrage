import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
import sqlalchemy

from frab.settings import PROJECT_ROOT, get_settings
from frab.db.models import Exchange, FundingRate, Market, Strategy
from frab.db.session import session_scope
from frab.events.bus import EventBus
from frab.exchanges.atomic import AtomicExecutor
from frab.exchanges.base import Leg, ExchangeDataSource, OrderRequest, Side
from frab.exchanges.hyperliquid.exchange import HLExchange as HLExchangeReader
from frab.exchanges.hyperliquid.exchange import HLExchange as LiveHLExecutor
from frab.server import _hl_info_url, _select_spot_token_map

app = typer.Typer(no_args_is_help=True, add_completion=False)

live_smoke_app = typer.Typer(help="HL testnet API smoke (read + tiny round-trip orders)")
app.add_typer(live_smoke_app, name="live-smoke")

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


@app.command()
def init_db() -> None:
    """Initialise the database schema via Alembic (alembic upgrade head)."""
    settings = get_settings()
    # Ensure the data directory exists before Alembic tries to create the DB file.
    from pathlib import Path
    db_path = settings.db_url.replace("sqlite+aiosqlite:///", "")
    if db_path and not db_path.startswith(":"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    # env.py creates an async engine; pass the async URL (sqlite+aiosqlite://)
    alembic_cfg.set_main_option("sqlalchemy.url", settings.db_url)
    command.upgrade(alembic_cfg, "head")
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
                tick_ms = int(tick.ts.timestamp() * 1000)
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


@app.command()
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


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    coins: str = "BTC,ETH,SOL,AVAX,LINK,AAVE,DOGE",
    log_level: str = "INFO",
    dry_run: bool | None = typer.Option(
        None,
        "--dry-run/--no-dry-run",
        help="Skip executor calls (no real orders). Overrides FRAB_DRY_RUN env var.",
    ),
) -> None:
    """Run shadow-trading engine + FastAPI server on the configured DB."""
    import logging

    from frab.server import build_app

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    settings = get_settings()
    resolved_dry_run = dry_run if dry_run is not None else settings.dry_run
    logging.getLogger(__name__).info(
        "serve: dry_run=%s (source=%s)",
        resolved_dry_run,
        "cli flag" if dry_run is not None else "settings/env",
    )

    coin_tuple = tuple(c.strip().upper() for c in coins.split(",") if c.strip())
    asgi_app = build_app(coin_tuple, dry_run=resolved_dry_run)
    uvicorn.run(asgi_app, host=host, port=port, log_level=log_level.lower())


# ---------------------------------------------------------------------------
# live-smoke helpers
# ---------------------------------------------------------------------------

def _smoke_check_network(settings) -> None:
    """Verify credentials are present before running smoke."""
    if settings.hl_private_key is None or settings.hl_account_address is None:
        typer.echo("ERROR: hl_private_key and hl_account_address are required for live-smoke")
        raise typer.Exit(code=2)


def _build_smoke_clients(settings):
    """Return (market_data, executor) from settings."""
    market_data = HLExchangeReader(
        api_url=_hl_info_url(settings),
        timeout_s=settings.hl_request_timeout_s,
    )
    executor = LiveHLExecutor(
        private_key=settings.hl_private_key.get_secret_value(),
        account_address=settings.hl_account_address,
        network=settings.hl_network,
        spot_token_map=_select_spot_token_map(settings.hl_network),
        slippage=settings.hl_live_slippage,
    )
    return market_data, executor


def _build_smoke_clients_with_slippage(settings, slippage: float):
    """Return (market_data, executor) with a custom slippage override."""
    market_data = HLExchangeReader(
        api_url=_hl_info_url(settings),
        timeout_s=settings.hl_request_timeout_s,
    )
    executor = LiveHLExecutor(
        private_key=settings.hl_private_key.get_secret_value(),
        account_address=settings.hl_account_address,
        network=settings.hl_network,
        spot_token_map=_select_spot_token_map(settings.hl_network),
        slippage=slippage,
    )
    return market_data, executor


def _hdr(text: str) -> None:
    typer.echo(typer.style(text, bold=True))


async def _smoke_read_impl(settings) -> None:
    market_data, executor = _build_smoke_clients(settings)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    try:
        # 1. meta
        _hdr("=== meta ===")
        meta = await market_data.fetch_meta()
        typer.echo(f"  perp coins: {len(meta)}")
        sample_names = [s.coin for s in meta[:5]]
        typer.echo(f"  first 5: {sample_names}")

        # 2. spot_meta — via _info SDK directly (no HLExchangeReader method for this)
        _hdr("=== spot_meta ===")
        spot_meta = await asyncio.to_thread(executor._info.spot_meta)
        tokens = spot_meta.get("tokens", [])
        pairs = spot_meta.get("universe", [])
        typer.echo(f"  tokens: {len(tokens)}   pairs: {len(pairs)}")

        # 3. allMids — via _info SDK directly
        _hdr("=== allMids ===")
        mids = await asyncio.to_thread(executor._info.all_mids)
        mid_items = list(mids.items())
        typer.echo(f"  coins with mids: {len(mid_items)}")
        for coin, mid in mid_items[:3]:
            typer.echo(f"    {coin}: {mid}")

        # 4. funding (latest tick)
        _hdr("=== funding/PURR (latest) ===")
        tick = await market_data.fetch_funding("PURR")
        typer.echo(f"  rate: {tick.rate:.8f}   annualized: {tick.annualized_pct:.2f}%   ts: {tick.ts}")

        # 5. funding history (bulk)
        _hdr("=== funding_history/PURR (last 12 h) ===")
        since_ms = now_ms - 12 * 3600 * 1000
        ticks = await market_data.fetch_funding_history("PURR", since_ms)
        typer.echo(f"  ticks: {len(ticks)}")
        if ticks:
            typer.echo(f"  first ts: {ticks[0].ts}   last ts: {ticks[-1].ts}")

        # 6. l2Book perp (HYPE — testnet has more depth than PURR perp)
        _hdr("=== l2Book perp/HYPE ===")
        quote_perp = await market_data.fetch_quote("HYPE")
        typer.echo(f"  bid: {quote_perp.bid}   ask: {quote_perp.ask}   mark: {quote_perp.mark}")

        # 7. l2Book spot — via _info SDK (pair name format)
        _hdr("=== l2Book spot/PURR-USDC ===")
        book = await asyncio.to_thread(executor._info.l2_snapshot, "PURR/USDC")
        levels = book.get("levels") or []
        bids = levels[0] if len(levels) >= 1 else []
        asks = levels[1] if len(levels) >= 2 else []
        top_bid = bids[0]["px"] if bids else "N/A"
        top_ask = asks[0]["px"] if asks else "N/A"
        typer.echo(f"  top bid: {top_bid}   top ask: {top_ask}")

        # 8. user_state (perp)
        _hdr("=== user_state (perp) ===")
        state = await asyncio.to_thread(executor._info.user_state, settings.hl_account_address)
        margin = state.get("marginSummary", {})
        typer.echo(f"  accountValue: {margin.get('accountValue', 'N/A')}")
        typer.echo(f"  withdrawable: {state.get('withdrawable', 'N/A')}")
        typer.echo(f"  open positions: {len(state.get('assetPositions', []))}")

        # 9. spot_user_state
        _hdr("=== spot_user_state ===")
        spot_state = await asyncio.to_thread(executor._info.spot_user_state, settings.hl_account_address)
        balances = spot_state.get("balances", [])
        typer.echo(f"  balances ({len(balances)}):")
        for bal in balances:
            typer.echo(f"    {bal.get('coin', '?')}: total={bal.get('total', '?')}  hold={bal.get('hold', '?')}")

    finally:
        await market_data.aclose()

    _hdr("=== OK — all read endpoints responded ===")


async def _smoke_spot_impl(settings, coin: str, qty: float) -> None:
    market_data, executor = _build_smoke_clients(settings)

    try:
        _hdr(f"=== spot smoke: {coin}/USDC ===")
        quote_before = await market_data.fetch_quote(coin)
        typer.echo(f"  mark before: {quote_before.mark}")

        buy_ref = f"smoke-spot-buy-{int(time.time() * 1000)}"
        buy_req = OrderRequest(
            coin=coin,
            leg=Leg.SPOT,
            side=Side.BUY,
            qty=qty,
            client_ref=buy_ref,
        )
        typer.echo(f"  submitting BUY {qty} {coin}/USDC ...")
        buy_fill = await executor.submit(buy_req)
        typer.echo(
            f"  BUY fill — price: {buy_fill.price}  qty: {buy_fill.qty}  fee: {buy_fill.fee}  ref: {buy_fill.client_ref}"
        )

        await asyncio.sleep(1.0)

        sell_ref = f"smoke-spot-sell-{int(time.time() * 1000)}"
        sell_req = OrderRequest(
            coin=coin,
            leg=Leg.SPOT,
            side=Side.SELL,
            qty=qty,
            client_ref=sell_ref,
        )
        typer.echo(f"  submitting SELL {qty} {coin}/USDC ...")
        sell_fill = await executor.submit(sell_req)
        typer.echo(
            f"  SELL fill — price: {sell_fill.price}  qty: {sell_fill.qty}  fee: {sell_fill.fee}  ref: {sell_fill.client_ref}"
        )

        net_pnl = (sell_fill.price - buy_fill.price) * qty - buy_fill.fee - sell_fill.fee
        typer.echo(f"  net PnL (cost of smoke): {net_pnl:.6f} USDC")

    finally:
        await market_data.aclose()

    _hdr("=== spot smoke complete ===")


async def _smoke_perp_impl(settings, coin: str, qty: float, slippage: float) -> None:
    market_data, executor = _build_smoke_clients_with_slippage(settings, slippage)

    try:
        _hdr(f"=== perp smoke: {coin} ===")
        quote_before = await market_data.fetch_quote(coin)
        typer.echo(f"  mark before: {quote_before.mark}")

        sell_ref = f"smoke-perp-sell-{int(time.time() * 1000)}"
        sell_req = OrderRequest(
            coin=coin,
            leg=Leg.PERP,
            side=Side.SELL,
            qty=qty,
            client_ref=sell_ref,
        )
        typer.echo(f"  submitting SELL (short) {qty} {coin} perp ...")
        sell_fill = await executor.submit(sell_req)
        typer.echo(
            f"  SELL fill — price: {sell_fill.price}  qty: {sell_fill.qty}  fee: {sell_fill.fee}  ref: {sell_fill.client_ref}"
        )

        await asyncio.sleep(1.0)

        buy_ref = f"smoke-perp-buy-{int(time.time() * 1000)}"
        buy_req = OrderRequest(
            coin=coin,
            leg=Leg.PERP,
            side=Side.BUY,
            qty=qty,
            client_ref=buy_ref,
        )
        typer.echo(f"  submitting BUY (cover) {qty} {coin} perp ...")
        buy_fill = await executor.submit(buy_req)
        typer.echo(
            f"  BUY fill — price: {buy_fill.price}  qty: {buy_fill.qty}  fee: {buy_fill.fee}  ref: {buy_fill.client_ref}"
        )

        # Short P&L: opened at sell_fill.price, closed at buy_fill.price
        net_pnl = (sell_fill.price - buy_fill.price) * qty - sell_fill.fee - buy_fill.fee
        typer.echo(f"  net PnL (cost of smoke): {net_pnl:.6f} USDC")

    finally:
        await market_data.aclose()

    _hdr("=== perp smoke complete ===")


# ---------------------------------------------------------------------------
# live-smoke subcommands
# ---------------------------------------------------------------------------

@live_smoke_app.command("read")
def smoke_read() -> None:
    """Exercise all read endpoints (no signing)."""
    settings = get_settings()
    _smoke_check_network(settings)
    asyncio.run(_smoke_read_impl(settings))


@live_smoke_app.command("spot")
def smoke_spot(
    coin: str = "PURR",
    qty: float = 1.0,
) -> None:
    """Open + close a tiny spot position (round-trip) to validate signed orders."""
    settings = get_settings()
    _smoke_check_network(settings)
    typer.echo(
        f"spot smoke: BUY then SELL {qty} {coin}/USDC on {settings.hl_network} — proceed?"
    )
    typer.confirm("Proceed?", abort=True)
    asyncio.run(_smoke_spot_impl(settings, coin, qty))


@live_smoke_app.command("perp")
def smoke_perp(
    coin: str = "HYPE",
    qty: float = 0.5,
    slippage: float = 0.30,
) -> None:
    """Open + close a tiny perp short (round-trip) to validate signed orders."""
    settings = get_settings()
    _smoke_check_network(settings)
    typer.echo(
        f"perp smoke: SHORT then COVER {qty} {coin} perp on {settings.hl_network} — proceed?"
    )
    typer.confirm("Proceed?", abort=True)
    asyncio.run(_smoke_perp_impl(settings, coin, qty, slippage))


@live_smoke_app.command("all")
def smoke_all(
    spot_coin: str = "PURR",
    spot_qty: float = 1.0,
    perp_coin: str = "HYPE",
    perp_qty: float = 0.5,
    perp_slippage: float = 0.30,
) -> None:
    """Run read + spot + perp smoke in sequence (one confirmation prompt)."""
    settings = get_settings()
    _smoke_check_network(settings)
    typer.echo(
        f"Full smoke on {settings.hl_network}: "
        f"8 read calls + spot round-trip ({spot_qty} {spot_coin}/USDC) "
        f"+ perp round-trip ({perp_qty} {perp_coin})."
    )
    typer.confirm("Run full smoke — 2 round-trip orders + 8 read calls — proceed?", abort=True)

    async def _run_all() -> None:
        await _smoke_read_impl(settings)
        await _smoke_spot_impl(settings, spot_coin, spot_qty)
        await _smoke_perp_impl(settings, perp_coin, perp_qty, perp_slippage)

    asyncio.run(_run_all())


async def _smoke_paired_impl(settings, coin: str, qty: float, wait_sec: float) -> None:
    _, executor = _build_smoke_clients(settings)
    bus = EventBus()
    atomic = AtomicExecutor(executor, bus, max_attempts=1, sleep_between_attempts=())

    ts = int(time.time() * 1000)
    perp_open = OrderRequest(
        coin=coin, leg=Leg.PERP, side=Side.SELL, qty=qty,
        client_ref=f"smoke-paired-open-perp-{ts}",
    )
    spot_open = OrderRequest(
        coin=coin, leg=Leg.SPOT, side=Side.BUY, qty=qty,
        client_ref=f"smoke-paired-open-spot-{ts}",
    )

    _hdr("=== open_paired ===")
    typer.echo(f"  requested {qty} {coin}")
    open_result = await atomic.open_paired(perp_open, spot_open)
    typer.echo(f"  status: {open_result.status}")
    if open_result.spot_fill is not None:
        typer.echo(f"  spot fill: qty={open_result.spot_fill.qty}  px={open_result.spot_fill.price}")
    if open_result.perp_fill is not None:
        typer.echo(f"  perp fill: qty={open_result.perp_fill.qty}  px={open_result.perp_fill.price}")
    if open_result.status != "ok":
        typer.echo(f"  errors: {open_result.errors}")
        return

    typer.echo(f"  waiting {wait_sec}s ...")
    await asyncio.sleep(wait_sec)

    pos = await executor.get_position(coin)
    _hdr("=== position after open ===")
    if pos is not None:
        typer.echo(f"  pos: {pos}")
    else:
        typer.echo("  (zero)")

    actual_qty = open_result.spot_fill.qty
    ts2 = int(time.time() * 1000)
    perp_close = OrderRequest(
        coin=coin, leg=Leg.PERP, side=Side.BUY, qty=actual_qty,
        client_ref=f"smoke-paired-close-perp-{ts2}",
    )
    spot_close = OrderRequest(
        coin=coin, leg=Leg.SPOT, side=Side.SELL, qty=actual_qty,
        client_ref=f"smoke-paired-close-spot-{ts2}",
    )

    _hdr("=== close_paired ===")
    typer.echo(f"  closing {actual_qty} {coin}")
    close_result = await atomic.close_paired(perp_close, spot_close)
    typer.echo(f"  status: {close_result.status}")
    if close_result.spot_fill is not None:
        typer.echo(f"  spot fill: qty={close_result.spot_fill.qty}  px={close_result.spot_fill.price}")
    if close_result.perp_fill is not None:
        typer.echo(f"  perp fill: qty={close_result.perp_fill.qty}  px={close_result.perp_fill.price}")
    if close_result.status != "ok":
        typer.echo(f"  errors: {close_result.errors}")

    pos2 = await executor.get_position(coin)
    _hdr("=== position after close ===")
    if pos2 is not None:
        typer.echo(f"  pos: {pos2}")
    else:
        typer.echo("  (zero)")


@live_smoke_app.command("paired")
def smoke_paired(
    coin: str = "BTC",
    qty: float = 0.00015,
    wait_sec: float = 5.0,
) -> None:
    """Atomic open_paired + close_paired round-trip (validates spot-first flow)."""
    settings = get_settings()
    _smoke_check_network(settings)
    typer.echo(
        f"paired smoke: open + close {qty} {coin} (spot+perp) on {settings.hl_network} — proceed?"
    )
    typer.confirm("Proceed?", abort=True)
    asyncio.run(_smoke_paired_impl(settings, coin, qty, wait_sec))
