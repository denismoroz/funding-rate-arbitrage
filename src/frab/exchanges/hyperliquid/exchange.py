"""HLExchange: stateless Hyperliquid exchange implementation.

Satisfies the Exchange Protocol. Read methods use httpx against the HL /info
REST endpoint. Write methods use the hyperliquid-python-sdk (sync, run via
asyncio.to_thread). DB session is opened per-method (short-lived), committed,
and closed — no in-memory caches of positions or wallet state.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Callable, Literal

import httpx
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from frab.constants import PERP_TAKER, SPOT_TAKER
from frab.db.models import (
    Exchange as DBExchange,
    Fill as DBFill,
    FundingAccrual as DBFundingAccrual,
    Position as DBPosition,
    WalletSnapshot as DBWalletSnapshot,
)
from frab.db.session import session_scope
from frab.domain import Instrument, Position, PositionStatus, Side
from frab.exchanges.hyperliquid.tokens import BRIDGE_TOKEN_BLACKLIST
from frab.exchanges.protocol import (
    FundingTick,
    MarketSpec,
    OpenRequest,
    Quote,
    WalletKind,
)

logger = logging.getLogger(__name__)

_PERIODS_PER_YEAR = 24 * 365  # HL funds hourly

# Inverse of MAINNET_SPOT_TOKEN_MAP: HL wrapped token → canonical perp coin.
# BRIDGE_TOKEN_BLACKLIST names are explicitly excluded (independent price discovery).
_SPOT_TOKEN_INVERSE: dict[str, str] = {
    "UBTC": "BTC",
    "UETH": "ETH",
    "USOL": "SOL",
}


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


class _RetryableHTTPError(Exception):
    pass


_RETRYABLE = retry_if_exception_type(
    (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, _RetryableHTTPError)
)
_WAIT = wait_exponential(multiplier=0.3, min=0.3, max=4)
_STOP = stop_after_attempt(4)


class HLTransferError(RuntimeError):
    """Raised when a usdClassTransfer action is rejected by Hyperliquid."""


class PartialFillError(RuntimeError):
    """Raised when HL filled less than the requested qty beyond tolerance."""

    def __init__(self, requested_qty: float, filled_qty: float, fill_price: float) -> None:
        super().__init__(
            f"partial fill: requested {requested_qty}, filled {filled_qty} "
            f"({filled_qty / requested_qty * 100:.1f}%)"
        )
        self.requested_qty = requested_qty
        self.filled_qty = filled_qty
        self.fill_price = fill_price


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
        # --- httpx client for read methods ---
        self._api_url = api_url
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(timeout=timeout_s)
            self._owns_client = True

        # Lazy cache: spot pair asset_id → pair name (e.g. 142 → "UBTC/USDC").
        self._spot_idx_to_name: dict[int, str] | None = None

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

        self._info = info
        self._exchange = exchange

        if account_address is not None:
            self._address: str | None = account_address
        else:
            candidate = getattr(exchange, "account_address", None) if exchange is not None else None
            self._address = candidate if isinstance(candidate, str) else None

        self._session_factory = session_factory
        self._spot_token_map: dict[str, str] = spot_token_map if spot_token_map is not None else {}
        self._spot_quote_token = spot_quote_token
        self._slippage = slippage
        self._partial_fill_tolerance = partial_fill_tolerance
        self._clock_fn = clock_fn if clock_fn is not None else lambda: datetime.now(UTC)
        self._sz_decimals_cache: dict[str, int] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "HLExchange":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Internal: httpx retry wrapper
    # ------------------------------------------------------------------

    async def _post(self, body: dict) -> Any:
        async for attempt in AsyncRetrying(
            retry=_RETRYABLE,
            wait=_WAIT,
            stop=_STOP,
            reraise=True,
        ):
            with attempt:
                resp = await self._client.post(self._api_url, json=body)
                if resp.status_code >= 500:
                    raise _RetryableHTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()

    # ------------------------------------------------------------------
    # Internal: spot index map (lazy cache)
    # ------------------------------------------------------------------

    async def _load_spot_idx_map(self) -> None:
        """Cache pair_idx → base_token_name (e.g. 142 → 'UBTC')."""
        if self._spot_idx_to_name is not None:
            return
        meta = await self._post({"type": "spotMeta"})
        token_by_idx: dict[int, str] = {}
        for t in meta.get("tokens", []):
            tidx = t.get("index")
            tname = t.get("name", "")
            if isinstance(tidx, int) and tname:
                token_by_idx[tidx] = tname
        mapping: dict[int, str] = {}
        for entry in meta.get("universe", []):
            idx = entry.get("index")
            if not isinstance(idx, int):
                continue
            name = entry.get("name", "")
            if "/" in name:
                mapping[idx] = name
                continue
            toks = entry.get("tokens") or []
            if toks and isinstance(toks[0], int):
                base = token_by_idx.get(toks[0])
                if base:
                    mapping[idx] = f"{base}/USDC"
        self._spot_idx_to_name = mapping

    # ------------------------------------------------------------------
    # Internal: coin normalization
    # ------------------------------------------------------------------

    async def _normalize_hl_coin(self, hl_coin: str) -> tuple[str, str]:
        """Normalize HL coin field to (coin, leg_str) where leg_str is 'spot' or 'perp'.

        Raises ValueError if the resolved token is in BRIDGE_TOKEN_BLACKLIST.
        """
        if hl_coin.startswith("@"):
            try:
                idx = int(hl_coin[1:])
            except ValueError:
                return hl_coin, "perp"
            await self._load_spot_idx_map()
            name = (self._spot_idx_to_name or {}).get(idx)
            if name and "/" in name:
                wrapped = name.split("/")[0]
                if wrapped in BRIDGE_TOKEN_BLACKLIST:
                    raise ValueError(
                        f"HL spot token {wrapped!r} is in BRIDGE_TOKEN_BLACKLIST "
                        f"(independent price discovery — not safe to map to perp coin)"
                    )
                return _SPOT_TOKEN_INVERSE.get(wrapped, wrapped), "spot"
            return hl_coin, "perp"
        if "/" in hl_coin:
            wrapped = hl_coin.split("/")[0]
            if wrapped in BRIDGE_TOKEN_BLACKLIST:
                raise ValueError(
                    f"HL spot token {wrapped!r} is in BRIDGE_TOKEN_BLACKLIST "
                    f"(independent price discovery — not safe to map to perp coin)"
                )
            coin = _SPOT_TOKEN_INVERSE.get(wrapped, wrapped)
            return coin, "spot"
        return hl_coin, "perp"

    def _record_to_tick(self, coin: str, record: dict) -> FundingTick:
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
        mids_data, book = await asyncio.gather(
            self._post({"type": "allMids"}),
            self._post({"type": "l2Book", "coin": coin}),
        )
        mark = float(mids_data[coin])
        levels = book.get("levels") or []
        bids = levels[0] if len(levels) >= 1 else []
        asks = levels[1] if len(levels) >= 2 else []
        bid = float(bids[0]["px"]) if bids else mark
        ask = float(asks[0]["px"]) if asks else mark
        ts_ms = int(book["time"])
        return Quote(coin=coin, mark=mark, spot=None, bid=bid, ask=ask, ts_ms=ts_ms)

    async def get_funding_rate(self, coin: str) -> FundingTick:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        data: list[dict] = await self._post({
            "type": "fundingHistory",
            "coin": coin,
            "startTime": now_ms - 2 * 3600 * 1000,
        })
        if not data:
            raise ValueError(f"no recent funding for {coin}")
        return self._record_to_tick(coin, data[-1])

    async def get_meta(self) -> list[MarketSpec]:
        data = await self._post({"type": "meta"})
        specs: list[MarketSpec] = []
        for entry in data["universe"]:
            sz = int(entry["szDecimals"])
            min_size = 10 ** -sz
            exp = 6 - sz
            tick_size = 10 ** -exp if exp >= 0 else 1.0
            specs.append(MarketSpec(
                coin=entry["name"],
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
        now = self._clock_fn()
        now_ms = _dt_to_ms(now)

        if req.instrument == Instrument.COLLATERAL:
            # Transfer USDC spot → perp, then record a COLLATERAL position
            await self.transfer(req.coin, req.qty, WalletKind.SPOT, WalletKind.PERP)
            if self._session_factory is None:
                raise RuntimeError("session_factory required for open_position")
            async with session_scope(self._session_factory) as s:
                exchange_id = await self._get_exchange_id(s)
                row = DBPosition(
                    exchange_id=exchange_id,
                    coin=req.coin,
                    instrument=Instrument.COLLATERAL.value,
                    side=Side.NONE.value,
                    qty=req.qty,
                    entry_price=1.0,
                    opened_at=now_ms,
                    closed_at=None,
                    status=PositionStatus.OPEN.value,
                    farb_position_id=req.farb_position_id,
                )
                s.add(row)
                await s.flush()
                pos = self._db_pos_to_domain(row)
            return pos

        # SPOT or PERP
        exchange_sdk = self._require_exchange()

        # Floor qty to coin's szDecimals — HL SDK's float_to_wire rejects
        # quantities with sub-szDecimal precision (e.g. 0.000163124... for BTC
        # which requires a 0.00001 step). We also reject sub-step requests.
        wire_qty = await self.round_qty(req.coin, req.qty)
        if wire_qty <= 0:
            raise RuntimeError(
                f"qty {req.qty} rounds to 0 at szDecimals for coin={req.coin}"
            )

        if req.instrument == Instrument.SPOT:
            is_buy = req.side == Side.LONG
            spot_name = self._make_spot_name(req.coin)
            resp = await asyncio.to_thread(
                exchange_sdk.market_open, spot_name, is_buy, wire_qty, None, self._slippage
            )
        else:  # PERP
            is_buy = req.side == Side.LONG
            resp = await asyncio.to_thread(
                exchange_sdk.market_open, req.coin, is_buy, wire_qty, None, self._slippage
            )

        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise RuntimeError(f"HL order rejected: {resp!r}")
        try:
            status0 = resp["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"HL order response shape unexpected: {resp!r}") from exc

        if "filled" in status0:
            filled = status0["filled"]
            qty_filled = float(filled["totalSz"])
            fill_price = float(filled["avgPx"])
            oid = int(filled.get("oid", 0)) or None
            taker = SPOT_TAKER if req.instrument == Instrument.SPOT else PERP_TAKER
            estimate = qty_filled * fill_price * taker
            if "fee" in filled:
                fee = float(filled["fee"])
            else:
                real_fee = (
                    await self._fetch_real_fee_usdc(oid, since_ms=now_ms - 5_000)
                    if oid else None
                )
                fee = real_fee if real_fee is not None else estimate
        elif "error" in status0:
            raise RuntimeError(f"HL order error: {status0['error']!r}")
        elif "resting" in status0:
            raise RuntimeError(f"HL market order unexpectedly resting: {status0!r}")
        else:
            raise RuntimeError(f"HL order unrecognized status: {status0!r}")

        if qty_filled < wire_qty * (1 - self._partial_fill_tolerance):
            raise PartialFillError(
                requested_qty=wire_qty,
                filled_qty=qty_filled,
                fill_price=fill_price,
            )

        if self._session_factory is None:
            raise RuntimeError("session_factory required for open_position")
        async with session_scope(self._session_factory) as s:
            exchange_id = await self._get_exchange_id(s)
            row = DBPosition(
                exchange_id=exchange_id,
                coin=req.coin,
                instrument=req.instrument.value,
                side=req.side.value,
                qty=qty_filled,
                entry_price=fill_price,
                opened_at=now_ms,
                closed_at=None,
                status=PositionStatus.OPEN.value,
                farb_position_id=req.farb_position_id,
            )
            s.add(row)
            await s.flush()
            fill_row = DBFill(
                position_id=row.id,
                ts_ms=now_ms,
                side=req.side.value,
                qty=qty_filled,
                price=fill_price,
                fee=fee,
                slippage_bps=self._slippage * 1e4,
                is_paper=False,
            )
            s.add(fill_row)
            pos = self._db_pos_to_domain(row)
        return pos

    # ------------------------------------------------------------------
    # Protocol: close_position
    # ------------------------------------------------------------------

    async def close_position(self, pos: Position) -> Position:
        """Close a position on HL and update DB. Returns closed Position."""
        now = self._clock_fn()
        now_ms = _dt_to_ms(now)

        if pos.instrument == Instrument.COLLATERAL:
            await self.transfer(pos.coin, pos.qty, WalletKind.PERP, WalletKind.SPOT)
            if self._session_factory is None:
                raise RuntimeError("session_factory required for close_position")
            async with session_scope(self._session_factory) as s:
                row = await s.get(DBPosition, pos.id)
                if row is None:
                    raise RuntimeError(f"Position {pos.id} not found in DB")
                row.status = PositionStatus.CLOSED.value
                row.closed_at = now_ms
                closed_pos = self._db_pos_to_domain(row)
            return closed_pos

        # SPOT or PERP: market close
        exchange_sdk = self._require_exchange()

        if pos.instrument == Instrument.SPOT:
            # Sell the spot holdings
            is_buy = False  # SELL
            spot_name = self._make_spot_name(pos.coin)
            resp = await asyncio.to_thread(
                exchange_sdk.market_open, spot_name, is_buy, pos.qty, None, self._slippage
            )
        else:  # PERP: use market_close
            resp = await asyncio.to_thread(
                exchange_sdk.market_close, pos.coin, None, None, self._slippage
            )

        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise RuntimeError(f"HL close rejected: {resp!r}")
        try:
            status0 = resp["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"HL close response shape unexpected: {resp!r}") from exc

        if "filled" in status0:
            filled = status0["filled"]
            qty_filled = float(filled["totalSz"])
            fill_price = float(filled["avgPx"])
            oid = int(filled.get("oid", 0)) or None
            taker = SPOT_TAKER if pos.instrument == Instrument.SPOT else PERP_TAKER
            estimate = qty_filled * fill_price * taker
            if "fee" in filled:
                fee = float(filled["fee"])
            else:
                real_fee = (
                    await self._fetch_real_fee_usdc(oid, since_ms=now_ms - 5_000)
                    if oid else None
                )
                fee = real_fee if real_fee is not None else estimate
        elif "error" in status0:
            raise RuntimeError(f"HL close error: {status0['error']!r}")
        else:
            raise RuntimeError(f"HL close unrecognized status: {status0!r}")

        # Closing side is opposite to the opening side
        closing_side = Side.SHORT if pos.side == Side.LONG else Side.LONG

        if self._session_factory is None:
            raise RuntimeError("session_factory required for close_position")
        async with session_scope(self._session_factory) as s:
            row = await s.get(DBPosition, pos.id)
            if row is None:
                raise RuntimeError(f"Position {pos.id} not found in DB")
            row.status = PositionStatus.CLOSED.value
            row.closed_at = now_ms
            fill_row = DBFill(
                position_id=row.id,
                ts_ms=now_ms,
                side=closing_side.value,
                qty=qty_filled,
                price=fill_price,
                fee=fee,
                slippage_bps=self._slippage * 1e4,
                is_paper=False,
            )
            s.add(fill_row)
            closed_pos = self._db_pos_to_domain(row)
        return closed_pos

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
            asyncio.to_thread(self._info.user_state, address),
            asyncio.to_thread(self._info.spot_user_state, address),
        )

        # Build set of coins with open positions on HL
        hl_perp_coins: set[str] = set()
        for entry in perp_state.get("assetPositions", []):
            pos = entry.get("position", {})
            szi = float(pos.get("szi", 0))
            if abs(szi) > 1e-12:
                hl_perp_coins.add(pos["coin"])

        hl_spot_coins: set[str] = set()
        for balance in spot_state.get("balances", []):
            total = float(balance.get("total", 0))
            if total > 1e-12:
                hl_spot_coins.add(balance["coin"])

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
            spot_hl_coin = self._spot_token_map.get(row.coin, row.coin)
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
        data: list[dict] = await self._post({
            "type": "userFunding",
            "user": address,
            "startTime": since_ms,
        })

        # Filter to matching coin
        total = 0.0
        new_accruals: list[tuple[int, float]] = []
        for record in data:
            delta = record.get("delta", {})
            if delta.get("coin") != pos.coin:
                continue
            ts_ms = int(record["time"])
            amount = float(delta["usdc"])
            new_accruals.append((ts_ms, amount))
            total += amount

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
    # Perp unrealized PnL from HL (authoritative)
    # ------------------------------------------------------------------

    async def get_perp_unrealized_by_coin(self) -> dict[str, float]:
        """Return {coin: unrealizedPnl_USDC} from HL assetPositions.

        Empty dict if HL reports no open perp positions. Caller decides whether
        to use this as a per-coin override or to fall back to local m-to-m.
        """
        address = self._require_address()
        try:
            state = await asyncio.to_thread(self._info.user_state, address)
        except Exception as exc:
            logger.warning("get_perp_unrealized_by_coin: user_state failed: %s", exc)
            return {}
        out: dict[str, float] = {}
        for entry in state.get("assetPositions") or []:
            pos = entry.get("position", {})
            coin = pos.get("coin")
            if not coin:
                continue
            try:
                out[coin] = float(pos.get("unrealizedPnl", 0.0))
            except (TypeError, ValueError):
                continue
        return out

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
        """Look up the fee for a specific oid from HL userFillsByTime.

        Returns the fee converted to USDC (multiplies wrapped-token fees by the
        fill price). Returns None if the fill has not appeared after `attempts`
        polls — caller should fall back to a taker-rate estimate.
        """
        address = self._require_address()
        end_ms = _dt_to_ms(self._clock_fn()) + 60_000
        for i in range(attempts):
            try:
                fills = await asyncio.to_thread(
                    self._info.user_fills_by_time, address, since_ms, end_ms
                )
            except Exception as exc:
                logger.warning("user_fills_by_time failed (attempt %d): %s", i + 1, exc)
                fills = []
            if not isinstance(fills, list):
                return None
            for f in fills:
                if not isinstance(f, dict) or int(f.get("oid", 0)) != oid:
                    continue
                fee_raw = float(f.get("fee", 0))
                fee_token = str(f.get("feeToken") or "USDC").upper()
                if fee_token in ("USDC", "USD"):
                    return fee_raw
                # Wrapped spot tokens: fee is in the base asset; convert to USDC
                # using the fill price (px is in USDC per base unit).
                px = float(f.get("px", 0))
                if fee_token in _SPOT_TOKEN_INVERSE and px > 0:
                    return fee_raw * px
                logger.warning(
                    "unknown feeToken %r oid=%s — skipping conversion", fee_token, oid
                )
                return fee_raw
            if i + 1 < attempts:
                await asyncio.sleep(sleep_s)
        logger.warning("fee for oid=%s not found in userFillsByTime after %d attempts",
                       oid, attempts)
        return None

    async def backfill_fill_fees(self, strategy_id: int) -> int:
        """Update DB fills whose fee == 0 with the real fee from HL userFills.

        Matches HL fills to DB fills by (coin canonical, ts_ms ± 30s, qty, side).
        Returns the number of rows updated. Used at startup to fix historical
        fills that were written before the real-fee capture was wired up.
        """
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
            hl_fills = await asyncio.to_thread(
                self._info.user_fills_by_time, address, min_ts
            )
        except Exception as exc:
            logger.warning("backfill_fill_fees: user_fills_by_time failed: %s", exc)
            return 0

        updated = 0
        for fill_row, pos_row in zero_fee_fills:
            match = self._match_hl_fill(hl_fills, fill_row, pos_row)
            if match is None:
                continue
            fee_raw = float(match.get("fee", 0))
            fee_token = str(match.get("feeToken") or "USDC").upper()
            if fee_token in ("USDC", "USD"):
                fee_usdc = fee_raw
            elif fee_token in _SPOT_TOKEN_INVERSE:
                px = float(match.get("px", 0))
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
    def _match_hl_fill(hl_fills: list[dict], db_fill: Any, db_pos: Any) -> dict | None:
        """Find an HL fill matching a DB (fill, position) row by side/qty/time."""
        # HL side: "B" = buy, "A" = ask/sell. DB side: "long" or "short".
        want_side = "B" if db_fill.side == Side.LONG.value else "A"
        for f in hl_fills or []:
            if f.get("side") != want_side:
                continue
            try:
                sz = float(f.get("sz", 0))
                ts = int(f.get("time", 0))
            except (TypeError, ValueError):
                continue
            if abs(sz - db_fill.qty) > max(db_fill.qty * 0.01, 1e-9):
                continue
            if abs(ts - db_fill.ts_ms) > 30_000:
                continue
            # Coin matching: PERP fills carry the coin name (e.g. "BTC");
            # SPOT fills carry the pair symbol (e.g. "@142" or "UBTC/USDC").
            hl_coin = str(f.get("coin", ""))
            instrument = db_pos.instrument
            if instrument == Instrument.PERP.value:
                if hl_coin != db_pos.coin:
                    continue
            else:  # SPOT — accept either the symbolic or @-prefixed form
                if not (hl_coin.startswith("@") or "/" in hl_coin):
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
            asyncio.to_thread(self._info.user_state, address),
            asyncio.to_thread(self._info.spot_user_state, address),
        )

        now_ms = _dt_to_ms(self._clock_fn())

        # Per-kind balance (returned to caller; state-machine code uses this)
        if kind == WalletKind.PERP:
            margin = perp_state.get("marginSummary", {})
            if coin in ("USDC", "USD"):
                balance = float(margin.get("accountValue", 0.0))
            else:
                # For other coins in perp — not standard HL usage
                balance = 0.0
        else:  # SPOT
            spot_coin = self._spot_token_map.get(coin, coin)
            balance = 0.0
            for bal in spot_state.get("balances", []):
                if bal.get("coin") == spot_coin or bal.get("coin") == coin:
                    balance = float(bal.get("total", 0.0))
                    break

        # Total balance across BOTH sub-wallets (equity-relevant quantity).
        # On HL all wallets belong to one account; the ledger needs the total.
        # For USDC: perp accountValue + spot USDC balance.
        # For non-USDC coins (e.g. BTC): only spot balance is relevant.
        if coin in ("USDC", "USD"):
            perp_value = float(perp_state.get("marginSummary", {}).get("accountValue", 0.0))
            spot_coin = self._spot_token_map.get(coin, coin)
            spot_value = 0.0
            for bal in spot_state.get("balances", []):
                if bal.get("coin") == spot_coin or bal.get("coin") == coin:
                    spot_value = float(bal.get("total", 0.0))
                    break
            total_balance = perp_value + spot_value
        else:
            # Non-USDC: only spot balance matters for this strategy
            spot_coin = self._spot_token_map.get(coin, coin)
            total_balance = 0.0
            for bal in spot_state.get("balances", []):
                if bal.get("coin") == spot_coin or bal.get("coin") == coin:
                    total_balance = float(bal.get("total", 0.0))
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
        exchange_sdk = self._require_exchange()
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

        resp = await asyncio.to_thread(exchange_sdk.usd_class_transfer, amount, to_perp)
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise HLTransferError(
                f"HL usdClassTransfer {from_wallet}→{to_wallet} rejected: {resp!r}"
            )
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
                    asyncio.to_thread(self._info.user_state, address),
                    asyncio.to_thread(self._info.spot_user_state, address),
                )
                perp_balance = float(
                    perp_state.get("marginSummary", {}).get("accountValue", 0.0)
                )
                spot_coin = self._spot_token_map.get(coin, coin)
                spot_balance = 0.0
                for bal in spot_state.get("balances", []):
                    if bal.get("coin") == spot_coin or bal.get("coin") == coin:
                        spot_balance = float(bal.get("total", 0.0))
                        break

                # Write ONE snapshot with the post-transfer TOTAL balance.
                # Same semantics as get_wallet: equity-relevant quantity is the
                # account total, not the per-sub-wallet split.
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

    def _require_address(self) -> str:
        if self._address is None:
            raise RuntimeError("account_address required")
        return self._address

    def _make_spot_name(self, coin: str) -> str:
        base = self._spot_token_map.get(coin, coin)
        return f"{base}/{self._spot_quote_token}"

    async def _sz_decimals(self, coin: str) -> int:
        if self._sz_decimals_cache is None:
            meta = await asyncio.to_thread(self._info.meta)
            self._sz_decimals_cache = {u["name"]: int(u["szDecimals"]) for u in meta["universe"]}
        sz_dec = self._sz_decimals_cache.get(coin)
        if sz_dec is None:
            raise ValueError(f"unknown coin {coin!r} (not in perp meta)")
        return sz_dec

    async def round_qty(self, coin: str, qty: float) -> float:
        """Floor qty to asset's szDecimals (conservative; used for initial sizing)."""
        sz_dec = await self._sz_decimals(coin)
        quant = Decimal(10) ** -sz_dec
        return float(Decimal(str(qty)).quantize(quant, rounding=ROUND_DOWN))

    async def round_qty_to_nearest(self, coin: str, qty: float) -> float:
        """Round qty to asset's szDecimals with ROUND_HALF_UP."""
        sz_dec = await self._sz_decimals(coin)
        quant = Decimal(10) ** -sz_dec
        return float(Decimal(str(qty)).quantize(quant, rounding=ROUND_HALF_UP))

    # ------------------------------------------------------------------
    # Additional read helpers (non-Protocol, for CLI/internal use)
    # ------------------------------------------------------------------

    async def fetch_funding_history(self, coin: str, since_ms: int) -> list[FundingTick]:
        ticks: list[FundingTick] = []
        start = since_ms
        while True:
            data: list[dict] = await self._post({
                "type": "fundingHistory",
                "coin": coin,
                "startTime": start,
            })
            for rec in data:
                ticks.append(self._record_to_tick(coin, rec))
            if len(data) < 500:
                break
            start = int(data[-1]["time"]) + 1
        ticks.sort(key=lambda t: t.ts_ms)
        return ticks

    async def fetch_account_state(self) -> dict[str, Any]:
        """Return raw perp + spot account state dicts."""
        address = self._require_address()
        perp_state, spot_state = await asyncio.gather(
            asyncio.to_thread(self._info.user_state, address),
            asyncio.to_thread(self._info.spot_user_state, address),
        )
        return {"perp": perp_state, "spot": spot_state}

    def _inverse_spot_token_map(self) -> dict[str, str]:
        return {v: k for k, v in self._spot_token_map.items()}

    def _normalize_spot_coin(self, hl_coin: str) -> str:
        return self._inverse_spot_token_map().get(hl_coin, hl_coin)

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

            canonical = self._normalize_spot_coin(hl_coin)

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
