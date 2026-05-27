"""HyperliquidAdapter: composes HLMarketData + LiveHLExecutor + AtomicExecutor
into a single ExchangeAdapter-conforming aggregate.

This module is a transitional file for F2.4.  F2.6 will rename
hyperliquid.py → frab/exchanges/hyperliquid/_market.py and move this file
to frab/exchanges/hyperliquid/adapter.py once the namespace is free.

All HL-specific quirks (margin transfers, spot-first paired execution,
token validation) are hidden behind the ExchangeAdapter interface.
"""
from __future__ import annotations

from typing import Literal

from frab.domain.exchange import Exchange
from frab.domain.exchange_profile import ExchangeProfile
from frab.domain.market_spec import MarketSpec as DomainMarketSpec
from frab.domain.position import ClosedPosition, Position
from frab.domain.wallet import WalletInfo
from frab.exchanges._hl_tokens import validate_spot_pairs
from frab.exchanges.atomic import AtomicExecutor
from frab.exchanges.base import (
    FundingPayment,
    FundingTick,
    Leg,
    OrderRequest,
    Quote,
    Side,
    UserFill,
)
from frab.exchanges.hyperliquid import HLMarketData
from frab.exchanges.hyperliquid_live import LiveHLExecutor


class HyperliquidAdapter:
    """Universal adapter for Hyperliquid.

    Composes HLMarketData (reads), LiveHLExecutor (wallet / transfers),
    and AtomicExecutor (spot-first paired open/close) into a single
    ExchangeAdapter-conforming object.

    The ``paired_router`` property exposes the underlying AtomicExecutor
    so that existing strategies (which still take an AtomicExecutor-shaped
    executor) can be wired directly during the F2/F3 transition period.
    """

    exchange = Exchange.HYPERLIQUID

    _PROFILE = ExchangeProfile(
        exchange=Exchange.HYPERLIQUID,
        funding_interval_hours=1.0,
        periods_per_year=24 * 365,
        default_spot_taker_bps=7.0,
        default_perp_taker_bps=3.5,
    )

    def __init__(
        self,
        *,
        market_data: HLMarketData,
        live_executor: LiveHLExecutor,
        atomic: AtomicExecutor,
        network: Literal["testnet", "mainnet"],
        user_address: str | None = None,
    ) -> None:
        self._market = market_data
        self._live = live_executor
        self._atomic = atomic
        self._network = network

        # Resolve user address: prefer live_executor.account_address (if the
        # real LiveHLExecutor exposes it) or the explicit kwarg.
        addr = getattr(live_executor, "account_address", None)
        if isinstance(addr, str):
            self._user_address: str = addr
        elif user_address is not None:
            self._user_address = user_address
        else:
            self._user_address = ""

    # ------------------------------------------------------------------
    # F2/F3 transition helper
    # ------------------------------------------------------------------

    @property
    def paired_router(self) -> AtomicExecutor:
        """Expose the underlying AtomicExecutor for the F2/F3 transition
        period — strategies still take an AtomicExecutor-shaped executor
        until F3 migrates them to ExchangeAdapter directly."""
        return self._atomic

    # ------------------------------------------------------------------
    # reads — static profile
    # ------------------------------------------------------------------

    async def get_exchange_profile(self) -> ExchangeProfile:
        return self._PROFILE

    # ------------------------------------------------------------------
    # reads — market data (delegate to HLMarketData)
    # ------------------------------------------------------------------

    async def fetch_quote(self, coin: str) -> Quote:
        return await self._market.fetch_quote(coin)

    async def fetch_funding(self, coin: str) -> FundingTick:
        return await self._market.fetch_funding(coin)

    async def fetch_funding_history(self, coin: str, since_ms: int) -> list[FundingTick]:
        return await self._market.fetch_funding_history(coin, since_ms)

    async def fetch_user_fills(self, since_ms: int) -> list[UserFill]:
        return await self._market.fetch_user_fills(self._user_address, since_ms)

    async def fetch_user_funding(self, since_ms: int) -> list[FundingPayment]:
        return await self._market.fetch_user_funding(self._user_address, since_ms)

    # ------------------------------------------------------------------
    # reads — composed wallet / market specs
    # ------------------------------------------------------------------

    async def get_wallet(self) -> WalletInfo:
        """Fetch live wallet state from HL; package into WalletInfo."""
        state = await self._live.fetch_wallet_state()
        return WalletInfo(
            exchange=self.exchange,
            available_usdc=float(state.get("withdrawable", 0.0)),
            reserved_usdc=float(state.get("perp_equity", 0.0)),
            total_value_usd=float(state.get("account_value", 0.0)),
        )

    async def get_open_positions(self) -> list[Position]:
        """Return venue-side open positions.

        For the F2.4 baseline this returns an empty list — no caller wires
        this yet (F1.4 uses portfolio_service for position queries).  Full
        venue-read implementation is deferred to a later chunk so this
        chunk stays bounded.
        """
        return []

    async def get_market_specs(self) -> dict[str, DomainMarketSpec]:
        """Fetch HL meta and convert to domain MarketSpec objects.

        Falls back to hardcoded defaults for max_leverage and maint_ratio;
        HL perp meta does not expose these fields in the current shape.
        F2.6+ can replace the defaults with per-coin lookups.
        """
        base_specs = await self._market.fetch_meta()
        out: dict[str, DomainMarketSpec] = {}
        for s in base_specs:
            out[s.coin] = DomainMarketSpec(
                coin=s.coin,
                has_spot=s.has_spot,
                has_perp=s.has_perp,
                max_leverage=10,
                maint_ratio=0.01,
                min_size=s.min_size,
                tick_size=s.tick_size,
            )
        return out

    # ------------------------------------------------------------------
    # startup validation
    # ------------------------------------------------------------------

    async def startup_validate(self, coins: tuple[str, ...]) -> None:
        """Mainnet only: ensure every coin's wrapped-token spot pair exists."""
        if self._network == "mainnet":
            await validate_spot_pairs(self._market, coins)

    # ------------------------------------------------------------------
    # writes — open / close / adjust
    # ------------------------------------------------------------------

    async def open_position(
        self,
        coin: str,
        *,
        notional_usd: float,
        margin_reserve_usd: float,
        client_ref: str | None = None,
    ) -> Position:
        """Open a delta-neutral spot+perp position on HL.

        Sequence:
          1. Transfer margin_reserve_usd from spot wallet to perp wallet.
          2. Compute spot quantity from notional / ask price.
          3. Submit spot-first paired open via AtomicExecutor.
          4. Return a domain Position built from real fills.

        Rolls back the margin transfer (best-effort) if the paired open fails.
        """
        if margin_reserve_usd > 0:
            await self._live.transfer_spot_to_perp(margin_reserve_usd)

        quote = await self._market.fetch_quote(coin)
        if quote.ask <= 0:
            raise ValueError(f"invalid ask price for {coin}: {quote.ask}")
        spot_qty = notional_usd / quote.ask

        perp_req = OrderRequest(
            coin=coin,
            leg=Leg.PERP,
            side=Side.SELL,
            qty=spot_qty,
            client_ref=client_ref,
        )
        spot_req = OrderRequest(
            coin=coin,
            leg=Leg.SPOT,
            side=Side.BUY,
            qty=spot_qty,
            client_ref=client_ref,
        )

        result = await self._atomic.open_paired(perp_req, spot_req)
        if result.status != "ok" or result.perp_fill is None or result.spot_fill is None:
            if margin_reserve_usd > 0:
                try:
                    await self._live.transfer_perp_to_spot(margin_reserve_usd)
                except Exception:
                    pass
            raise RuntimeError(
                f"open_position failed for {coin}: errors={result.errors}"
            )

        return Position(
            exchange=self.exchange,
            coin=coin,
            spot_qty=result.spot_fill.qty,
            perp_qty=result.perp_fill.qty,
            notional_usd=notional_usd,
            margin_reserve_usd=margin_reserve_usd,
            entry_spot_price=result.spot_fill.price,
            entry_perp_price=result.perp_fill.price,
            opened_at=result.spot_fill.ts,
            funding_collected=0.0,
            fees_paid=result.spot_fill.fee + result.perp_fill.fee,
            state={},
        )

    async def close_position(self, coin: str) -> ClosedPosition:
        """Close both legs of an open delta-neutral position on HL.

        Reads current venue state to determine sizes, then submits a
        spot-first paired close via AtomicExecutor.  Realized PnL is
        approximated from venue avg-entry prices; PortfolioService can
        refine via apply_close.
        """
        venue_pos = await self._live.get_position(coin)
        if venue_pos is None:
            raise RuntimeError(f"close_position: no venue position for {coin}")

        perp_req = OrderRequest(
            coin=coin,
            leg=Leg.PERP,
            side=Side.BUY,
            qty=abs(venue_pos.perp_units),
        )
        spot_req = OrderRequest(
            coin=coin,
            leg=Leg.SPOT,
            side=Side.SELL,
            qty=venue_pos.spot_units,
        )

        result = await self._atomic.close_paired(perp_req, spot_req)
        if result.status != "ok" or result.perp_fill is None or result.spot_fill is None:
            raise RuntimeError(
                f"close_position failed for {coin}: errors={result.errors}"
            )

        entry_perp = venue_pos.avg_entry_perp or 0.0
        entry_spot = venue_pos.avg_entry_spot or 0.0
        perp_realized = (entry_perp - result.perp_fill.price) * abs(venue_pos.perp_units)
        spot_realized = (result.spot_fill.price - entry_spot) * venue_pos.spot_units
        realized_pnl = perp_realized + spot_realized

        return ClosedPosition(
            exchange=self.exchange,
            coin=coin,
            closed_at=result.spot_fill.ts,
            realized_pnl=realized_pnl,
            fees_paid_total=result.spot_fill.fee + result.perp_fill.fee,
            funding_collected_total=0.0,
            released_margin_usd=0.0,
        )

    async def adjust_margin(self, coin: str, delta_usd: float) -> None:
        """Top-up or release margin for a position.

        Positive delta: transfer from spot wallet to perp wallet.
        Negative delta: transfer from perp wallet to spot wallet.
        Zero: no-op.
        """
        if delta_usd > 0:
            await self._live.transfer_spot_to_perp(delta_usd)
        elif delta_usd < 0:
            await self._live.transfer_perp_to_spot(-delta_usd)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Release the underlying HTTP client."""
        await self._market.aclose()
