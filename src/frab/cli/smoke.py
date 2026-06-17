"""Live-smoke subcommands — HL testnet API smoke (read + tiny round-trip orders)."""
import asyncio
from datetime import UTC, datetime

import typer

from frab.settings import get_settings
from frab.exchanges.hyperliquid.exchange import HLExchange as HLExchangeReader
from frab.exchanges.hyperliquid.exchange import HLExchange as LiveHLExecutor
from frab.server import _hl_info_url

live_smoke = typer.Typer(help="HL testnet API smoke (read + tiny round-trip orders)")


# ---------------------------------------------------------------------------
# live-smoke helpers
# ---------------------------------------------------------------------------

def _smoke_check_network(settings) -> None:
    """Verify credentials are present before running smoke."""
    if settings.hl_private_key is None or settings.hl_account_address is None:
        typer.echo("ERROR: hl_private_key and hl_account_address are required for live-smoke")
        raise typer.Exit(code=2)


def _build_smoke_clients(settings, slippage: float | None = None):
    """Return (market_data, executor). If slippage is None, use settings.hl_live_slippage."""
    resolved_slippage = slippage if slippage is not None else settings.hl_live_slippage
    market_data = HLExchangeReader(
        api_url=_hl_info_url(settings),
        timeout_s=settings.hl_request_timeout_s,
    )
    executor = LiveHLExecutor(
        private_key=settings.hl_private_key.get_secret_value(),
        account_address=settings.hl_account_address,
        network=settings.hl_network,
        slippage=resolved_slippage,
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
        meta = await market_data.get_meta()
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
        tick = await market_data.get_funding_rate("PURR")
        typer.echo(f"  rate: {tick.rate:.8f}   annualized: {tick.annualized_pct:.2f}%   ts_ms: {tick.ts_ms}")

        # 5. funding history (bulk)
        _hdr("=== funding_history/PURR (last 12 h) ===")
        since_ms = now_ms - 12 * 3600 * 1000
        ticks = await market_data.fetch_funding_history("PURR", since_ms)
        typer.echo(f"  ticks: {len(ticks)}")
        if ticks:
            typer.echo(f"  first ts_ms: {ticks[0].ts_ms}   last ts_ms: {ticks[-1].ts_ms}")

        # 6. l2Book perp (HYPE — testnet has more depth than PURR perp)
        _hdr("=== l2Book perp/HYPE ===")
        quote_perp = await market_data.get_quote("HYPE")
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
    from frab.domain import Instrument, Side
    from frab.exchanges.protocol import OpenRequest
    market_data, executor = _build_smoke_clients(settings)

    try:
        _hdr(f"=== spot smoke: {coin}/USDC ===")
        quote_before = await market_data.get_quote(coin)
        typer.echo(f"  mark before: {quote_before.mark}")

        buy_req = OpenRequest(coin=coin, instrument=Instrument.SPOT, side=Side.LONG, qty=qty)
        typer.echo(f"  submitting BUY {qty} {coin}/USDC ...")
        pos = await executor.open_position(buy_req)
        typer.echo(f"  BUY fill — price: {pos.entry_price}  qty: {pos.qty}")

        await asyncio.sleep(1.0)

        typer.echo(f"  submitting SELL {qty} {coin}/USDC ...")
        closed_pos = await executor.close_position(pos)
        typer.echo(f"  SELL done — status: {closed_pos.status}")

    finally:
        await market_data.aclose()

    _hdr("=== spot smoke complete ===")


async def _smoke_perp_impl(settings, coin: str, qty: float, slippage: float) -> None:
    from frab.domain import Instrument, Side
    from frab.exchanges.protocol import OpenRequest
    market_data, executor = _build_smoke_clients(settings, slippage)

    try:
        _hdr(f"=== perp smoke: {coin} ===")
        quote_before = await market_data.get_quote(coin)
        typer.echo(f"  mark before: {quote_before.mark}")

        typer.echo(f"  submitting SHORT {qty} {coin} perp ...")
        sell_req = OpenRequest(coin=coin, instrument=Instrument.PERP, side=Side.SHORT, qty=qty)
        pos = await executor.open_position(sell_req)
        typer.echo(f"  SHORT fill — price: {pos.entry_price}  qty: {pos.qty}")

        await asyncio.sleep(1.0)

        typer.echo(f"  submitting COVER {qty} {coin} perp ...")
        closed_pos = await executor.close_position(pos)
        typer.echo(f"  COVER done — status: {closed_pos.status}")

    finally:
        await market_data.aclose()

    _hdr("=== perp smoke complete ===")


# ---------------------------------------------------------------------------
# live-smoke subcommands
# ---------------------------------------------------------------------------

@live_smoke.command("read")
def smoke_read() -> None:
    """Exercise all read endpoints (no signing)."""
    settings = get_settings()
    _smoke_check_network(settings)
    asyncio.run(_smoke_read_impl(settings))


@live_smoke.command("spot")
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


@live_smoke.command("perp")
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


@live_smoke.command("all")
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
    """Open spot+perp pair, wait, close both legs."""
    from frab.domain import Instrument, Side
    from frab.exchanges.protocol import OpenRequest

    _, executor = _build_smoke_clients(settings)

    _hdr("=== open spot leg ===")
    typer.echo(f"  requested {qty} {coin}")
    spot_req = OpenRequest(coin=coin, instrument=Instrument.SPOT, side=Side.LONG, qty=qty)
    spot_pos = await executor.open_position(spot_req)
    typer.echo(f"  spot fill — price: {spot_pos.entry_price}  qty: {spot_pos.qty}")

    _hdr("=== open perp leg ===")
    perp_req = OpenRequest(coin=coin, instrument=Instrument.PERP, side=Side.SHORT, qty=spot_pos.qty)
    perp_pos = await executor.open_position(perp_req)
    typer.echo(f"  perp fill — price: {perp_pos.entry_price}  qty: {perp_pos.qty}")

    typer.echo(f"  waiting {wait_sec}s ...")
    await asyncio.sleep(wait_sec)

    open_positions = await executor.get_open_positions()
    _hdr(f"=== open positions ({len(open_positions)}) ===")
    for p in open_positions:
        typer.echo(f"  {p.coin} {p.instrument} {p.side} qty={p.qty}")

    _hdr("=== close spot leg ===")
    closed_spot = await executor.close_position(spot_pos)
    typer.echo(f"  spot close — status: {closed_spot.status}")

    _hdr("=== close perp leg ===")
    closed_perp = await executor.close_position(perp_pos)
    typer.echo(f"  perp close — status: {closed_perp.status}")


@live_smoke.command("paired")
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


def register(app: typer.Typer) -> None:
    app.add_typer(live_smoke, name="live-smoke")
