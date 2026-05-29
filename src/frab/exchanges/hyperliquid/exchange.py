"""HLExchange: stateless Hyperliquid exchange implementation.

Satisfies the Exchange Protocol. Read methods and write methods are
delegated to HLClient (transport + typed wire layer). DB session is
opened per-method (short-lived), committed, and closed — no in-memory
caches of positions or wallet state.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Callable, Literal

import httpx
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession


from frab.db.models import Exchange as DBExchange
from frab.domain import Position
from frab.exchanges.hyperliquid.actions.account_snapshot import AccountSnapshotAction
from frab.exchanges.hyperliquid.actions.backfill_fees import BackfillFeesAction
from frab.exchanges.hyperliquid.actions.close_position import ClosePositionAction
from frab.exchanges.hyperliquid.actions.get_wallet import GetWalletAction
from frab.exchanges.hyperliquid.actions.load_funding import LoadAccruedFundingAction
from frab.exchanges.hyperliquid.actions.load_positions import LoadOpenPositionsAction
from frab.exchanges.hyperliquid.actions.open_position import OpenPositionAction, PartialFillError
from frab.exchanges.hyperliquid.actions.transfer import TransferAction
from frab.exchanges.hyperliquid.client import HLClient, HLTransferError
from frab.exchanges.hyperliquid.symbols import HLSymbols, SPOT_TOKEN_INVERSE
from frab.exchanges.hyperliquid.tokens import BRIDGE_TOKEN_BLACKLIST
from frab.exchanges.hyperliquid.wire import HLPerpState, HLSpotState, HLUserFill
from frab.exchanges.protocol import (
    FundingTick,
    MarketSpec,
    OpenRequest,
    Quote,
    WalletKind,
)

logger = logging.getLogger(__name__)

# Re-export so existing importers of HLTransferError / PartialFillError from exchange.py still work.
__all__ = ["HLExchange", "HLTransferError", "PartialFillError", "BRIDGE_TOKEN_BLACKLIST", "SPOT_TOKEN_INVERSE"]

_PERIODS_PER_YEAR = 24 * 365  # HL funds hourly



def _base_url(network: Literal["testnet", "mainnet"]) -> str:
    if network == "testnet":
        return constants.TESTNET_API_URL
    if network == "mainnet":
        return constants.MAINNET_API_URL
    raise ValueError(f"unsupported network: {network!r}")


class HLExchange:
    """Unified Hyperliquid exchange class: read (httpx) + write (SDK) + DB writes."""

    name = "hyperliquid"

    def __init__(
        self,
        *,
        # --- Read-side (httpx) ---
        api_url: str = "https://api.hyperliquid.xyz/info",
        timeout_s: float = 10.0,
        client: httpx.AsyncClient | None = None,
        # --- Write-side (SDK) ---
        private_key: str | None = None,
        account_address: str | None = None,
        network: Literal["testnet", "mainnet"] = "testnet",
        info: Info | None = None,
        exchange: Exchange | None = None,
        # --- DB ---
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        # --- Shared order settings ---
        spot_token_map: dict[str, str] | None = None,
        spot_quote_token: str = "USDC",
        slippage: float = 0.01,
        partial_fill_tolerance: float = 0.01,
        clock_fn: Callable[[], datetime] | None = None,
    ) -> None:
        # --- SDK objects for write methods ---
        if info is None:
            info = Info(_base_url(network), skip_ws=True)
        if exchange is None and (private_key is not None and account_address is not None):
            wallet = Account.from_key(private_key)
            exchange = Exchange(
                wallet=wallet,
                base_url=_base_url(network),
                account_address=account_address,
            )

        # Build the transport client
        self._hl_client = HLClient(
            api_url=api_url,
            timeout_s=timeout_s,
            client=client,
            info=info,
            exchange=exchange,
        )

        # Keep references for legacy attribute access (e.g., test_tokens.py accesses _info)
        self._info = info
        self._exchange = exchange
        # Legacy: expose _api_url for registry test / external callers
        self._api_url = api_url

        if account_address is not None:
            self._address: str | None = account_address
        else:
            candidate = getattr(exchange, "account_address", None) if exchange is not None else None
            self._address = candidate if isinstance(candidate, str) else None

        self._symbols = HLSymbols(
            client=self._hl_client,
            spot_token_map=spot_token_map,
            spot_quote_token=spot_quote_token,
        )

        self._session_factory = session_factory
        self._slippage = slippage
        self._partial_fill_tolerance = partial_fill_tolerance
        self._clock_fn = clock_fn if clock_fn is not None else lambda: datetime.now(UTC)

        if session_factory is not None:
            self._open_action: OpenPositionAction | None = OpenPositionAction(
                client=self._hl_client,
                symbols=self._symbols,
                session_factory=session_factory,
                exchange_name=self.name,
                address=self._address,
                slippage=self._slippage,
                partial_fill_tolerance=self._partial_fill_tolerance,
                clock_fn=self._clock_fn,
            )
            self._close_action: ClosePositionAction | None = ClosePositionAction(
                client=self._hl_client,
                symbols=self._symbols,
                session_factory=session_factory,
                exchange_name=self.name,
                address=self._address,
                slippage=self._slippage,
                clock_fn=self._clock_fn,
            )
            self._load_positions_action: LoadOpenPositionsAction | None = LoadOpenPositionsAction(
                client=self._hl_client,
                symbols=self._symbols,
                session_factory=session_factory,
                exchange_name=self.name,
                address=self._address,
            )
            self._load_funding_action: LoadAccruedFundingAction | None = LoadAccruedFundingAction(
                client=self._hl_client,
                session_factory=session_factory,
                address=self._address,
            )
            self._get_wallet_action: GetWalletAction | None = GetWalletAction(
                client=self._hl_client,
                symbols=self._symbols,
                session_factory=session_factory,
                exchange_name=self.name,
                address=self._address,
                clock_fn=self._clock_fn,
            )
            self._transfer_action: TransferAction | None = TransferAction(
                client=self._hl_client,
                symbols=self._symbols,
                session_factory=session_factory,
                exchange_name=self.name,
                address=self._address,
                clock_fn=self._clock_fn,
            )
            self._backfill_fees_action: BackfillFeesAction | None = BackfillFeesAction(
                client=self._hl_client,
                session_factory=session_factory,
                address=self._address,
            )
        else:
            self._open_action = None
            self._close_action = None
            self._load_positions_action = None
            self._load_funding_action = None
            self._get_wallet_action = None
            self._transfer_action = None
            self._backfill_fees_action = None

        self._account_snapshot_action = AccountSnapshotAction(
            client=self._hl_client,
            symbols=self._symbols,
            address=self._address,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        await self._hl_client.aclose()

    async def __aenter__(self) -> "HLExchange":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    def _record_to_tick(self, coin: str, record: HLUserFill | Any) -> FundingTick:
        # Works with both typed HLFundingRecord and raw dict (for get_funding_rate)
        if hasattr(record, "rate"):
            rate = record.rate
            premium = record.premium
            ts_ms = record.ts_ms
        else:
            rate = float(record["fundingRate"])
            premium = float(record["premium"])
            ts_ms = int(record["time"])
        return FundingTick(
            coin=coin,
            ts_ms=ts_ms,
            rate=rate,
            premium=premium,
            annualized_pct=rate * _PERIODS_PER_YEAR * 100,
        )

    # ------------------------------------------------------------------
    # Internal: DB helpers
    # ------------------------------------------------------------------

    async def _get_exchange_id(self, session: AsyncSession) -> int:
        result = await session.execute(
            select(DBExchange).where(DBExchange.name == self.name)
        )
        exc = result.scalar_one_or_none()
        if exc is None:
            raise RuntimeError(
                f"Exchange {self.name!r} not found in DB; run `frab seed` first."
            )
        return exc.id

    # ------------------------------------------------------------------
    # Protocol: Read methods
    # ------------------------------------------------------------------

    async def get_quote(self, coin: str) -> Quote:
        mids_data, snap = await asyncio.gather(
            self._hl_client.all_mids(),
            self._hl_client.l2_book(coin),
        )
        mark = mids_data.get(coin, 0.0)
        bid = snap.bid if snap.bid else mark
        ask = snap.ask if snap.ask else mark
        ts_ms = snap.ts_ms
        return Quote(coin=coin, mark=mark, spot=None, bid=bid, ask=ask, ts_ms=ts_ms)

    async def get_funding_rate(self, coin: str) -> FundingTick:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        records = await self._hl_client.funding_history(coin, since_ms=now_ms - 2 * 3600 * 1000)
        if not records:
            raise ValueError(f"no recent funding for {coin}")
        last = records[-1]
        return FundingTick(
            coin=coin,
            ts_ms=last.ts_ms,
            rate=last.rate,
            premium=last.premium,
            annualized_pct=last.rate * _PERIODS_PER_YEAR * 100,
        )

    async def get_meta(self) -> list[MarketSpec]:
        specs_raw = await self._hl_client.perp_meta()
        specs: list[MarketSpec] = []
        for entry in specs_raw:
            sz = entry.sz_decimals
            min_size = 10 ** -sz
            exp = 6 - sz
            tick_size = 10 ** -exp if exp >= 0 else 1.0
            specs.append(MarketSpec(
                coin=entry.name,
                has_spot=False,
                has_perp=True,
                min_size=min_size,
                tick_size=tick_size,
                sz_decimals=sz,
            ))
        return specs

    # ------------------------------------------------------------------
    # Protocol: open_position
    # ------------------------------------------------------------------

    async def open_position(self, req: OpenRequest) -> Position:
        """Open a position on HL and write it to DB. Returns domain Position."""
        if self._open_action is None:
            raise RuntimeError("session_factory required for open_position")
        return await self._open_action.execute(req)

    # ------------------------------------------------------------------
    # Protocol: close_position
    # ------------------------------------------------------------------

    async def close_position(self, pos: Position) -> Position:
        """Close a position on HL and update DB. Returns closed Position."""
        if self._close_action is None:
            raise RuntimeError("session_factory required for close_position")
        return await self._close_action.execute(pos)

    # ------------------------------------------------------------------
    # Protocol: get_open_positions
    # ------------------------------------------------------------------

    async def get_open_positions(self) -> list[Position]:
        """Fetch open positions from HL, reconcile with DB, return canonical set.

        Out-of-band positions are logged at WARN but not auto-corrected.
        """
        if self._load_positions_action is None:
            raise RuntimeError("session_factory required for get_open_positions")
        return await self._load_positions_action.execute()

    # ------------------------------------------------------------------
    # Protocol: get_accrued_funding
    # ------------------------------------------------------------------

    async def get_accrued_funding(self, pos: Position) -> float:
        """Fetch HL funding history since pos.opened_at, write accruals to DB.

        Idempotent: skips rows that already exist by (position_id, ts_ms).
        """
        if self._load_funding_action is None:
            raise RuntimeError("session_factory required for get_accrued_funding")
        return await self._load_funding_action.execute(pos)

    async def get_spot_mids_by_coin(self) -> dict[str, float]:
        """Return {canonical_coin: spot_mid_USDC} for spot pairs we support."""
        return await self._symbols.spot_mids_by_coin()

    async def get_perp_unrealized_by_coin(self) -> dict[str, float]:
        """Return {coin: unrealizedPnl_USDC} from HL assetPositions."""
        return await self._account_snapshot_action.get_perp_unrealized_by_coin()

    # ------------------------------------------------------------------
    # Fees from HL userFills (authoritative)
    # ------------------------------------------------------------------

    async def backfill_fill_fees(self, strategy_id: int) -> int:
        """Update DB fills whose fee == 0 with the real fee from HL userFills."""
        if self._backfill_fees_action is None:
            raise RuntimeError("session_factory required for backfill_fill_fees")
        return await self._backfill_fees_action.execute(strategy_id)

    # ------------------------------------------------------------------
    # Protocol: get_wallet
    # ------------------------------------------------------------------

    async def get_wallet(self, coin: str, kind: WalletKind) -> float:
        """Get free balance for (coin, kind), write wallet_snapshot, return balance."""
        if self._get_wallet_action is None:
            raise RuntimeError("session_factory required for get_wallet")
        return await self._get_wallet_action.execute(coin, kind)

    # ------------------------------------------------------------------
    # Protocol: transfer
    # ------------------------------------------------------------------

    async def transfer(
        self,
        coin: str,
        amount: float,
        from_wallet: WalletKind,
        to_wallet: WalletKind,
    ) -> None:
        """Transfer funds between wallets. Writes wallet_snapshots after transfer."""
        self._require_exchange()
        # session_factory may be None: TransferAction handles that internally (skips snapshot)
        if self._transfer_action is None:
            # No session_factory at all: still must call SDK; build a transient action without DB
            action = TransferAction(
                client=self._hl_client, symbols=self._symbols,
                session_factory=None, exchange_name=self.name,
                address=self._address, clock_fn=self._clock_fn,
            )
            await action.execute(coin, amount, from_wallet, to_wallet)
            return
        await self._transfer_action.execute(coin, amount, from_wallet, to_wallet)

    # ------------------------------------------------------------------
    # Internal: SDK helpers
    # ------------------------------------------------------------------

    def _require_exchange(self) -> Exchange:
        if self._exchange is None:
            raise RuntimeError(
                "HLExchange write methods require private_key + account_address "
                "(or an injected exchange object)"
            )
        return self._exchange

    def _require_address(self) -> str:
        if self._address is None:
            raise RuntimeError("account_address required")
        return self._address

    async def round_qty(self, coin: str, qty: float) -> float:
        """Floor qty to asset's szDecimals (conservative; used for initial sizing)."""
        return await self._symbols.round_qty(coin, qty)

    async def round_qty_to_nearest(self, coin: str, qty: float) -> float:
        """Round qty to asset's szDecimals with ROUND_HALF_UP."""
        return await self._symbols.round_qty_to_nearest(coin, qty)

    # ------------------------------------------------------------------
    # Additional read helpers (non-Protocol, for CLI/internal use)
    # ------------------------------------------------------------------

    async def fetch_funding_history(self, coin: str, since_ms: int) -> list[FundingTick]:
        records = await self._hl_client.funding_history(coin, since_ms)
        return [
            FundingTick(
                coin=coin,
                ts_ms=r.ts_ms,
                rate=r.rate,
                premium=r.premium,
                annualized_pct=r.rate * _PERIODS_PER_YEAR * 100,
            )
            for r in records
        ]

    async def get_account_snapshot(self) -> tuple[HLPerpState, HLSpotState]:
        """Return typed perp + spot account state in one round-trip pair."""
        return await self._account_snapshot_action.get_snapshot()

    async def fetch_wallet_state(
        self,
        mark_prices: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Return normalized wallet snapshot suitable for the /api/equity/wallet endpoint."""
        return await self._account_snapshot_action.get_wallet_state(mark_prices)
