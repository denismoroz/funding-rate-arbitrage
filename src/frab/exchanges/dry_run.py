"""DryRunAdapterGuard — wraps any ExchangeAdapter and synthesises paper fills.

All read methods are forwarded to the underlying adapter unchanged.
Write methods (open_position, close_position, adjust_margin) never touch the
underlying; they synthesise paper fills with configurable slippage.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from frab.domain.exchange import Exchange
from frab.domain.position import ClosedPosition, Position
from frab.exchanges.adapter import ExchangeAdapter


class DryRunAdapterGuard:
    """Wraps an ExchangeAdapter; intercepts mutating calls for paper trading."""

    def __init__(
        self,
        underlying: ExchangeAdapter,
        *,
        slippage_bps: float = 2.0,
        clock_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._underlying = underlying
        self._slippage_bps = slippage_bps
        self._clock_fn = clock_fn if clock_fn is not None else (lambda: datetime.now(UTC))
        self._paper_positions: dict[str, Position] = {}

    @property
    def exchange(self) -> Exchange:
        return self._underlying.exchange

    # ------------------------------------------------------------------
    # Reads — forward to underlying
    # ------------------------------------------------------------------

    async def get_exchange_profile(self):
        return await self._underlying.get_exchange_profile()

    async def get_wallet(self):
        return await self._underlying.get_wallet()

    async def get_open_positions(self):
        return await self._underlying.get_open_positions()

    async def get_market_specs(self):
        return await self._underlying.get_market_specs()

    async def fetch_quote(self, coin: str):
        return await self._underlying.fetch_quote(coin)

    async def fetch_funding(self, coin: str):
        return await self._underlying.fetch_funding(coin)

    async def fetch_funding_history(self, coin: str, since_ms: int):
        return await self._underlying.fetch_funding_history(coin, since_ms)

    async def fetch_user_fills(self, since_ms: int):
        return await self._underlying.fetch_user_fills(since_ms)

    async def fetch_user_funding(self, since_ms: int):
        return await self._underlying.fetch_user_funding(since_ms)

    async def startup_validate(self, coins: tuple[str, ...]) -> None:
        return await self._underlying.startup_validate(coins)

    async def close(self) -> None:
        return await self._underlying.close()

    # ------------------------------------------------------------------
    # Writes — synthesise paper fills, never call underlying
    # ------------------------------------------------------------------

    async def open_position(
        self,
        coin: str,
        *,
        notional_usd: float,
        margin_reserve_usd: float,
        client_ref: str | None = None,
    ) -> Position:
        """Synthesise a paper open with slippage; cache for later close."""
        quote = await self._underlying.fetch_quote(coin)
        profile = await self._underlying.get_exchange_profile()
        slip = self._slippage_bps / 1e4
        spot_fill_price = quote.ask * (1.0 + slip)
        perp_fill_price = quote.mark - quote.mark * slip * 0.5
        spot_qty = notional_usd / spot_fill_price
        perp_qty = spot_qty
        spot_fee = notional_usd * profile.default_spot_taker_bps / 1e4
        perp_fee = notional_usd * profile.default_perp_taker_bps / 1e4
        total_fees = spot_fee + perp_fee
        pos = Position(
            exchange=self.exchange,
            coin=coin,
            spot_qty=spot_qty,
            perp_qty=perp_qty,
            notional_usd=notional_usd,
            margin_reserve_usd=margin_reserve_usd,
            entry_spot_price=spot_fill_price,
            entry_perp_price=perp_fill_price,
            opened_at=self._clock_fn(),
            funding_collected=0.0,
            fees_paid=total_fees,
            state={},
        )
        self._paper_positions[coin] = pos
        return pos

    async def close_position(self, coin: str) -> ClosedPosition:
        """Synthesise a paper close; raises ValueError if no open paper position."""
        pos = self._paper_positions.get(coin)
        if pos is None:
            raise ValueError(f"no paper position open for {coin}")
        quote = await self._underlying.fetch_quote(coin)
        profile = await self._underlying.get_exchange_profile()
        slip = self._slippage_bps / 1e4
        spot_close_price = quote.bid * (1.0 - slip)
        perp_close_price = quote.mark + quote.mark * slip * 0.5
        perp_realized = (pos.entry_perp_price - perp_close_price) * pos.perp_qty
        spot_realized = (spot_close_price - pos.entry_spot_price) * pos.spot_qty
        realized_pnl = perp_realized + spot_realized
        spot_close_fee = (pos.spot_qty * spot_close_price) * profile.default_spot_taker_bps / 1e4
        perp_close_fee = (pos.perp_qty * perp_close_price) * profile.default_perp_taker_bps / 1e4
        closed = ClosedPosition(
            exchange=self.exchange,
            coin=coin,
            closed_at=self._clock_fn(),
            realized_pnl=realized_pnl,
            fees_paid_total=pos.fees_paid + spot_close_fee + perp_close_fee,
            funding_collected_total=pos.funding_collected,
            released_margin_usd=pos.margin_reserve_usd,
        )
        del self._paper_positions[coin]
        return closed

    async def adjust_margin(self, coin: str, delta_usd: float) -> None:
        """No-op in dry-run; real wallets are never touched."""
        return None
