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


from frab.db.models import (
    Exchange as DBExchange,
    Fill as DBFill,
    FundingAccrual as DBFundingAccrual,
    Position as DBPosition,
    WalletSnapshot as DBWalletSnapshot,
)
from frab.db.session import session_scope
from frab.domain import Instrument, Position, PositionStatus, Side
from frab.exchanges.hyperliquid.actions.close_position import ClosePositionAction
from frab.exchanges.hyperliquid.actions.open_position import OpenPositionAction, PartialFillError
from frab.exchanges.hyperliquid.client import HLClient, HLTransferError
from frab.exchanges.hyperliquid.symbols import HLSymbols, SPOT_TOKEN_INVERSE
from frab.exchanges.hyperliquid.tokens import BRIDGE_TOKEN_BLACKLIST
from frab.exchanges.hyperliquid.wire import HLUserFill
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

# Backward-compat alias: code that imported _SPOT_TOKEN_INVERSE from exchange.py keeps working.
_SPOT_TOKEN_INVERSE = SPOT_TOKEN_INVERSE

_PERIODS_PER_YEAR = 24 * 365  # HL funds hourly



def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


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
        else:
            self._open_action = None
            self._close_action = None

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

    def _db_pos_to_domain(self, row: DBPosition) -> Position:
        return Position(
            id=row.id,
            exchange_name=self.name,
            coin=row.coin,
            instrument=Instrument(row.instrument),
            side=Side(row.side),
            qty=row.qty,
            entry_price=row.entry_price,
            opened_at=_ms_to_dt(row.opened_at),
            closed_at=_ms_to_dt(row.closed_at) if row.closed_at is not None else None,
            status=PositionStatus(row.status),
            farb_position_id=row.farb_position_id,
        )

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
        address = self._require_address()
        if self._session_factory is None:
            raise RuntimeError("session_factory required for get_open_positions")

        perp_state, spot_state = await asyncio.gather(
            self._hl_client.user_state(address),
            self._hl_client.spot_user_state(address),
        )

        # Build set of coins with open positions on HL
        hl_perp_coins: set[str] = set()
        for ap in perp_state.asset_positions:
            if abs(ap.szi) > 1e-12:
                hl_perp_coins.add(ap.coin)

        hl_spot_coins: set[str] = set()
        for bal in spot_state.balances:
            if bal.total > 1e-12:
                hl_spot_coins.add(bal.coin)

        async with session_scope(self._session_factory) as s:
            exchange_id = await self._get_exchange_id(s)
            result = await s.execute(
                select(DBPosition).where(
                    DBPosition.exchange_id == exchange_id,
                    DBPosition.status == PositionStatus.OPEN.value,
                )
            )
            db_rows = result.scalars().all()

        positions = []
        for row in db_rows:
            # Check reconciliation for PERP positions
            if row.instrument == Instrument.PERP.value and row.coin not in hl_perp_coins:
                logger.warning(
                    "get_open_positions: DB has OPEN PERP %s but HL reports no position",
                    row.coin,
                )
            # Check reconciliation for SPOT positions
            spot_hl_coin = self._symbols.spot_token_map.get(row.coin, row.coin)
            if row.instrument == Instrument.SPOT.value and spot_hl_coin not in hl_spot_coins:
                logger.warning(
                    "get_open_positions: DB has OPEN SPOT %s but HL reports no balance",
                    row.coin,
                )
            positions.append(self._db_pos_to_domain(row))

        return positions

    # ------------------------------------------------------------------
    # Protocol: get_accrued_funding
    # ------------------------------------------------------------------

    async def get_accrued_funding(self, pos: Position) -> float:
        """Fetch HL funding history since pos.opened_at, write accruals to DB.

        Idempotent: skips rows that already exist by (position_id, ts_ms).
        """
        address = self._require_address()
        if pos.id is None:
            raise ValueError("Position must have a DB id to fetch accrued funding")
        if self._session_factory is None:
            raise RuntimeError("session_factory required for get_accrued_funding")

        since_ms = _dt_to_ms(pos.opened_at)
        deltas = await self._hl_client.user_funding(address, since_ms)

        # Filter to matching coin
        new_accruals: list[tuple[int, float]] = []
        for delta in deltas:
            if delta.coin != pos.coin:
                continue
            new_accruals.append((delta.ts_ms, delta.amount_usdc))

        async with session_scope(self._session_factory) as s:
            # Load existing accrual ts_ms values for this position to avoid duplicates
            result = await s.execute(
                select(DBFundingAccrual.ts_ms).where(
                    DBFundingAccrual.position_id == pos.id
                )
            )
            existing_ts = {row for (row,) in result.all()}

            for ts_ms, amount in new_accruals:
                if ts_ms not in existing_ts:
                    s.add(DBFundingAccrual(
                        position_id=pos.id,
                        ts_ms=ts_ms,
                        amount=amount,
                    ))

        # Return cumulative sum from DB (source of truth)
        async with session_scope(self._session_factory) as s:
            result = await s.execute(
                select(DBFundingAccrual.amount).where(
                    DBFundingAccrual.position_id == pos.id
                )
            )
            total = sum(row for (row,) in result.all())

        return total

    # ------------------------------------------------------------------
    # Spot mids from HL (authoritative spot prices in USDC)
    # ------------------------------------------------------------------

    async def get_spot_mids_by_coin(self) -> dict[str, float]:
        """Return {canonical_coin: spot_mid_USDC} for spot pairs we support."""
        try:
            mids = await self._hl_client.all_mids()
        except Exception as exc:
            logger.warning("get_spot_mids_by_coin: allMids failed: %s", exc)
            return {}
        out: dict[str, float] = {}
        for key, val in mids.items():
            if not key.startswith("@"):
                continue
            try:
                idx = int(key[1:])
            except ValueError:
                continue
            name = await self._symbols.resolve_spot_pair(idx)
            if not name or "/" not in name:
                continue
            wrapped, quote = name.split("/", 1)
            if quote != self._symbols.spot_quote_token:
                continue
            canonical = SPOT_TOKEN_INVERSE.get(wrapped)
            if canonical is None:
                continue
            out[canonical] = val
        return out

    # ------------------------------------------------------------------
    # Perp unrealized PnL from HL (authoritative)
    # ------------------------------------------------------------------

    async def get_perp_unrealized_by_coin(self) -> dict[str, float]:
        """Return {coin: unrealizedPnl_USDC} from HL assetPositions."""
        address = self._require_address()
        try:
            state = await self._hl_client.user_state(address)
        except Exception as exc:
            logger.warning("get_perp_unrealized_by_coin: user_state failed: %s", exc)
            return {}
        return {ap.coin: ap.unrealized_pnl for ap in state.asset_positions if ap.coin}

    # ------------------------------------------------------------------
    # Fees from HL userFills (authoritative)
    # ------------------------------------------------------------------

    async def _fetch_real_fee_usdc(
        self,
        oid: int,
        *,
        since_ms: int,
        attempts: int = 3,
        sleep_s: float = 0.5,
    ) -> float | None:
        """Delegate to the stateless fee-lookup helper in actions._fees."""
        from frab.exchanges.hyperliquid.actions._fees import fetch_real_fee_usdc
        return await fetch_real_fee_usdc(
            client=self._hl_client,
            address=self._require_address(),
            oid=oid,
            since_ms=since_ms,
            clock_fn=self._clock_fn,
            attempts=attempts,
            sleep_s=sleep_s,
        )

    async def backfill_fill_fees(self, strategy_id: int) -> int:
        """Update DB fills whose fee == 0 with the real fee from HL userFills."""
        from frab.db.models import FarbPosition as DBFarbPosition

        if self._session_factory is None:
            raise RuntimeError("session_factory required for backfill_fill_fees")
        address = self._require_address()

        async with session_scope(self._session_factory) as s:
            result = await s.execute(
                select(DBFill, DBPosition).join(
                    DBPosition, DBFill.position_id == DBPosition.id
                ).join(
                    DBFarbPosition, DBPosition.farb_position_id == DBFarbPosition.id
                ).where(
                    DBFarbPosition.strategy_id == strategy_id,
                    DBFill.fee == 0.0,
                )
            )
            zero_fee_fills = result.all()

        if not zero_fee_fills:
            return 0

        min_ts = min(f.ts_ms for f, _ in zero_fee_fills) - 60_000
        try:
            hl_fills = await self._hl_client.user_fills_by_time(address, min_ts)
        except Exception as exc:
            logger.warning("backfill_fill_fees: user_fills_by_time failed: %s", exc)
            return 0

        updated = 0
        for fill_row, pos_row in zero_fee_fills:
            match = self._match_hl_fill(hl_fills, fill_row, pos_row)
            if match is None:
                continue
            fee_raw = match.fee_raw
            fee_token = match.fee_token
            if fee_token in ("USDC", "USD"):
                fee_usdc = fee_raw
            elif fee_token in SPOT_TOKEN_INVERSE:
                px = match.px
                fee_usdc = fee_raw * px
            else:
                fee_usdc = fee_raw
            if fee_usdc <= 0:
                continue
            async with session_scope(self._session_factory) as s:
                row = await s.get(DBFill, fill_row.id)
                if row is not None:
                    row.fee = fee_usdc
                    updated += 1
            logger.info(
                "backfill_fill_fees: fill_id=%d coin=%s qty=%s → fee=%.6f USDC",
                fill_row.id, pos_row.coin, fill_row.qty, fee_usdc,
            )
        return updated

    @staticmethod
    def _match_hl_fill(hl_fills: list[HLUserFill], db_fill: Any, db_pos: Any) -> HLUserFill | None:
        """Find an HLUserFill matching a DB (fill, position) row by side/qty/time."""
        want_side = "B" if db_fill.side == Side.LONG.value else "A"
        for f in hl_fills or []:
            if f.side != want_side:
                continue
            if abs(f.sz - db_fill.qty) > max(db_fill.qty * 0.01, 1e-9):
                continue
            if abs(f.ts_ms - db_fill.ts_ms) > 30_000:
                continue
            # Coin matching: PERP fills carry the coin name; SPOT fills carry pair symbol
            instrument = db_pos.instrument
            if instrument == Instrument.PERP.value:
                if f.coin != db_pos.coin:
                    continue
            else:  # SPOT — accept either the symbolic or @-prefixed form
                if not (f.coin.startswith("@") or "/" in f.coin):
                    continue
            return f
        return None

    # ------------------------------------------------------------------
    # Protocol: get_wallet
    # ------------------------------------------------------------------

    async def get_wallet(self, coin: str, kind: WalletKind) -> float:
        """Get free balance for (coin, kind), write wallet_snapshot, return balance."""
        address = self._require_address()
        if self._session_factory is None:
            raise RuntimeError("session_factory required for get_wallet")

        perp_state, spot_state = await asyncio.gather(
            self._hl_client.user_state(address),
            self._hl_client.spot_user_state(address),
        )

        now_ms = _dt_to_ms(self._clock_fn())

        # Per-kind balance (returned to caller; state-machine code uses this)
        if kind == WalletKind.PERP:
            if coin in ("USDC", "USD"):
                balance = perp_state.account_value
            else:
                balance = 0.0
        else:  # SPOT
            spot_coin = self._symbols.spot_token_map.get(coin, coin)
            balance = 0.0
            for bal in spot_state.balances:
                if bal.coin == spot_coin or bal.coin == coin:
                    balance = bal.total
                    break

        # Total balance across BOTH sub-wallets (equity-relevant cash).
        if coin in ("USDC", "USD"):
            account_value = perp_state.account_value
            unrealized_total = sum(ap.unrealized_pnl for ap in perp_state.asset_positions)
            # HL sign convention: cumFunding.sinceOpen is negative when received (a credit).
            # Flip to "received" semantics.
            cum_funding_received = sum(
                -ap.cum_funding_since_open for ap in perp_state.asset_positions
            )
            spot_coin = self._symbols.spot_token_map.get(coin, coin)
            spot_total = 0.0
            spot_hold = 0.0
            for bal in spot_state.balances:
                if bal.coin == spot_coin or bal.coin == coin:
                    spot_total = bal.total
                    spot_hold = bal.hold
                    break
            perp_standalone = (
                account_value - spot_hold - unrealized_total - cum_funding_received
            )
            total_balance = spot_total + perp_standalone
        else:
            # Non-USDC: only spot balance matters for this strategy
            spot_coin = self._symbols.spot_token_map.get(coin, coin)
            total_balance = 0.0
            for bal in spot_state.balances:
                if bal.coin == spot_coin or bal.coin == coin:
                    total_balance = bal.total
                    break

        async with session_scope(self._session_factory) as s:
            exchange_id = await self._get_exchange_id(s)
            s.add(DBWalletSnapshot(
                exchange_id=exchange_id,
                coin=coin,
                ts_ms=now_ms,
                balance=total_balance,
                source="hl_account_total",
            ))

        return balance

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
        if amount <= 0:
            raise ValueError(f"amount must be positive, got {amount!r}")

        if from_wallet == WalletKind.SPOT and to_wallet == WalletKind.PERP:
            to_perp = True
        elif from_wallet == WalletKind.PERP and to_wallet == WalletKind.SPOT:
            to_perp = False
        else:
            raise ValueError(
                f"unsupported transfer direction: {from_wallet} → {to_wallet}"
            )

        await self._hl_client.usd_class_transfer(amount, to_perp)
        logger.info(
            "transfer coin=%s amount=%.4f %s→%s ok",
            coin, amount, from_wallet, to_wallet,
        )

        # Write wallet_snapshots after transfer if session_factory available
        if self._session_factory is not None:
            now_ms = _dt_to_ms(self._clock_fn())
            address = self._address
            if address is not None:
                # Capture post-transfer balances
                perp_state, spot_state = await asyncio.gather(
                    self._hl_client.user_state(address),
                    self._hl_client.spot_user_state(address),
                )
                perp_balance = perp_state.account_value
                spot_coin = self._symbols.spot_token_map.get(coin, coin)
                spot_balance = 0.0
                for bal in spot_state.balances:
                    if bal.coin == spot_coin or bal.coin == coin:
                        spot_balance = bal.total
                        break

                if coin in ("USDC", "USD"):
                    total_balance = perp_balance + spot_balance
                else:
                    total_balance = spot_balance

                async with session_scope(self._session_factory) as s:
                    exchange_id = await self._get_exchange_id(s)
                    s.add(DBWalletSnapshot(
                        exchange_id=exchange_id,
                        coin=coin,
                        ts_ms=now_ms,
                        balance=total_balance,
                        source="hl_account_total",
                    ))

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

    async def _set_leverage(self, coin: str, leverage: int) -> None:
        """Set cross-margin leverage for a perp asset before opening a position."""
        if leverage <= 0:
            raise ValueError(f"leverage must be > 0, got {leverage!r}")
        self._require_exchange()
        await self._hl_client.update_leverage(coin, leverage)

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

    async def fetch_account_state(self) -> dict[str, Any]:
        """Return raw perp + spot account state dicts."""
        address = self._require_address()
        perp_state, spot_state = await asyncio.gather(
            self._hl_client.user_state(address),
            self._hl_client.spot_user_state(address),
        )
        # Re-serialize to match the dict shape callers (API routes) expect
        perp_dict = {
            "marginSummary": {"accountValue": str(perp_state.account_value)},
            "assetPositions": [
                {
                    "position": {
                        "coin": ap.coin,
                        "szi": str(ap.szi),
                        "unrealizedPnl": str(ap.unrealized_pnl),
                        "cumFunding": {"sinceOpen": str(ap.cum_funding_since_open)},
                    }
                }
                for ap in perp_state.asset_positions
            ],
        }
        spot_dict = {
            "balances": [
                {"coin": b.coin, "total": str(b.total), "hold": str(b.hold)}
                for b in spot_state.balances
            ]
        }
        return {"perp": perp_dict, "spot": spot_dict}


    async def fetch_wallet_state(
        self,
        mark_prices: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Return normalized wallet snapshot suitable for the /api/equity/wallet endpoint."""
        address = self._require_address()  # noqa: F841

        raw = await self.fetch_account_state()
        perp_state = raw["perp"]
        spot_state = raw["spot"]

        margin_summary = perp_state.get("marginSummary", {})
        perp_account_value = float(margin_summary.get("accountValue", 0.0))

        perp_unrealized_pnl = sum(
            float(entry.get("position", {}).get("unrealizedPnl", 0.0))
            for entry in perp_state.get("assetPositions", [])
        )

        prices = mark_prices or {}
        spot_balances: list[dict[str, Any]] = []
        usdc_spot = 0.0

        for balance in spot_state.get("balances", []):
            hl_coin: str = balance.get("coin", "")
            total = float(balance.get("total", 0.0))
            if total <= 0:
                continue

            canonical = self._symbols.normalize_spot_coin(hl_coin)

            if canonical in ("USDC", "USD"):
                usdc_spot += total
                continue

            mark = prices.get(canonical, 0.0)
            usd_value = total * mark
            spot_balances.append({
                "coin": canonical,
                "qty": total,
                "mark": mark,
                "usd_value": usd_value,
            })

        spot_tokens_usd = sum(b["usd_value"] for b in spot_balances)
        total_usd = perp_account_value + spot_tokens_usd + usdc_spot

        return {
            "perp_account_value": perp_account_value,
            "perp_unrealized_pnl": perp_unrealized_pnl,
            "spot_balances": spot_balances,
            "usdc_spot": usdc_spot,
            "total_usd": total_usd,
        }
