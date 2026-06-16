"""Ledger — stateless equity aggregator.

Reads DB, computes an EquitySnapshot. NO writes during compute_equity.
Only save_snapshot / compute_and_save write to the DB.

Cash semantics:
    cash = SUM of the *latest* wallet_snapshot balance per (exchange_id, coin)
           where coin IN ('USDC', 'USDT').
    Rationale: USDC and USDT are the settlement stablecoins used by this
    strategy. Non-stablecoin spot balances (e.g. BTC held in spot wallet)
    are already captured by spot_value; other stablecoins (BUSD etc.) are
    not relevant to this implementation. Step 7 may broaden this list.

Total-equity formula:
    total_equity = cash + spot_value   (see frab.domain.equity.total_equity_usd)

    perp_unrealized, perp_realized_cum, funding_cum and fees_cum are surfaced as
    *visibility counters only* — they are NOT added to total_equity. Under HL
    unified margin the perp unrealized PnL mirrors the spot-token marks in this
    delta-neutral book (and realized P&L / fees / funding already flow through
    wallet balances reflected in cash), so adding any of them double-counts and
    diverges from HL's reported account value.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.db.models import (
    EquitySnapshot as EquitySnapshotRow,
    Exchange as ExchangeRow,
    FarbPosition as FarbPositionRow,
    Fill as FillRow,
    FundingAccrual as FundingAccrualRow,
    Position as PositionRow,
    WalletSnapshot as WalletSnapshotRow,
)
from frab.db.session import session_scope
from frab.domain.enums import ACTIVE_STATES, FarbState, Instrument, PositionStatus, Side
from frab.domain.equity import total_equity_usd
from frab.exchanges.protocol import Quote

logger = logging.getLogger(__name__)

# Stablecoins treated as cash.  See module-level docstring for rationale.
_CASH_COINS = frozenset({"USDC", "USDT"})


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


@dataclass(frozen=True)
class EquitySnapshot:
    strategy_id: int
    ts_ms: int
    total_equity: float
    cash: float
    spot_value: float
    perp_unrealized: float
    perp_realized_cum: float
    funding_cum: float
    fees_cum: float


class Ledger:
    """Stateless equity aggregator.

    Constructor takes a session_factory (async_sessionmaker).  No other state.
    compute_equity is read-only.  save_snapshot is the only write method.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        account: str | None = None,
    ) -> None:
        self._sf = session_factory
        # HL account address this strategy's cash belongs to. When set, cash is
        # summed only from wallet_snapshots tagged with this account — so two
        # strategies sharing an exchange_id (different wallets) don't cross-count
        # each other's USDC. None → legacy global behaviour (all accounts).
        self._account = account.lower() if account else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def compute_equity(
        self,
        strategy_id: int,
        quotes: dict[str, Quote],
        *,
        perp_unrealized_by_coin: dict[str, float] | None = None,
        spot_mids_by_coin: dict[str, float] | None = None,
    ) -> EquitySnapshot:
        """Compute equity snapshot from DB + quotes.  Read-only — no DB writes.

        Parameters
        ----------
        strategy_id:
            The strategy whose equity is being computed.
        quotes:
            Mapping coin → fresh Quote.  For coins with open positions that are
            absent from this dict, the position's contribution is treated as 0
            and a WARN event is logged.
        perp_unrealized_by_coin:
            Optional {coin: unrealizedPnl_USDC} sourced from the exchange
            (e.g. HL assetPositions[].unrealizedPnl). When provided for a coin
            that has an open PERP position in the DB, this value is used
            instead of the local (entry-mark)*qty estimate. Missing coins fall
            back to the local computation.

        Returns
        -------
        EquitySnapshot
            All counters populated.  ts_ms = now.
        """
        ts_ms = _now_ms()

        async with self._sf() as session:
            cash = await self._compute_cash(session)
            latest_cash_ts = await self._latest_cash_snapshot_ts(session)
            spot_value, perp_unrealized = await self._compute_position_values(
                session, strategy_id, quotes,
                perp_unrealized_by_coin=perp_unrealized_by_coin,
                spot_mids_by_coin=spot_mids_by_coin,
                latest_cash_ts=latest_cash_ts,
            )
            perp_realized_cum = await self._compute_perp_realized(session, strategy_id)
            funding_cum = await self._compute_funding_cum(session, strategy_id)
            fees_cum = await self._compute_fees_cum(session, strategy_id)

        # Canonical equity: cash (spot USDC) + spot tokens. perp_unrealized /
        # funding_cum / fees are visibility-only counters (see module docstring
        # and frab.domain.equity.total_equity_usd).
        total_equity = total_equity_usd(cash, spot_value)

        return EquitySnapshot(
            strategy_id=strategy_id,
            ts_ms=ts_ms,
            total_equity=total_equity,
            cash=cash,
            spot_value=spot_value,
            perp_unrealized=perp_unrealized,
            perp_realized_cum=perp_realized_cum,
            funding_cum=funding_cum,
            fees_cum=fees_cum,
        )

    async def save_snapshot(self, snapshot: EquitySnapshot) -> None:
        """INSERT a row into equity_snapshots.  Separate from compute so the
        caller controls cadence (e.g. hourly vs. on-demand)."""
        async with session_scope(self._sf) as session:
            row = EquitySnapshotRow(
                strategy_id=snapshot.strategy_id,
                ts_ms=snapshot.ts_ms,
                total_equity=snapshot.total_equity,
                cash=snapshot.cash,
                spot_value=snapshot.spot_value,
                perp_unrealized=snapshot.perp_unrealized,
                perp_realized_cum=snapshot.perp_realized_cum,
                funding_cum=snapshot.funding_cum,
                fees_cum=snapshot.fees_cum,
            )
            session.add(row)

    async def compute_and_save(
        self,
        strategy_id: int,
        quotes: dict[str, Quote],
        *,
        perp_unrealized_by_coin: dict[str, float] | None = None,
        spot_mids_by_coin: dict[str, float] | None = None,
    ) -> EquitySnapshot:
        """Convenience: compute then save.  Returns the snapshot."""
        snapshot = await self.compute_equity(
            strategy_id, quotes,
            perp_unrealized_by_coin=perp_unrealized_by_coin,
            spot_mids_by_coin=spot_mids_by_coin,
        )
        await self.save_snapshot(snapshot)
        return snapshot

    # ------------------------------------------------------------------
    # Private helpers — each uses the provided session (no new session)
    # ------------------------------------------------------------------

    async def _compute_cash(
        self,
        session: AsyncSession,
    ) -> float:
        """SUM of latest wallet_snapshot balance per (exchange_id, coin)
        for all exchanges linked to this strategy, restricted to
        cash-equivalent coins (USDC, USDT).

        We use a NOT-EXISTS correlated subquery to find the 'latest' row per
        (exchange_id, coin): a row is 'latest' if there is no newer row for
        the same (exchange_id, coin).  This is SQLite-compatible.

        The strategy does not own exchanges directly; wallet_snapshots are
        scoped by exchange_id.  We sum across ALL exchanges registered in the
        DB for now (Step 7 will scope by strategy config if needed).
        """
        # Alias to avoid clash with domain-level Exchange name
        ws = WalletSnapshotRow

        # Latest ts_ms per (exchange_id, coin) for cash coins via GROUP BY MAX.
        # This is O(N) with the ix_wallet_snapshots_latest index, vs the previous
        # correlated NOT-EXISTS which was O(N^2) over the (large, ever-growing)
        # wallet_snapshots table — that scan alone took ~48s on prod and was the
        # dominant cost of the minute tick (see ix_wallet_snapshots_latest migration).
        latest = (
            select(
                ws.exchange_id.label("eid"),
                ws.coin.label("coin"),
                func.max(ws.ts_ms).label("mt"),
            )
            .where(ws.coin.in_(list(_CASH_COINS)))
        )
        if self._account is not None:
            latest = latest.where(ws.account == self._account)
        latest = latest.group_by(ws.exchange_id, ws.coin).subquery()
        stmt = (
            select(func.coalesce(func.sum(ws.balance), 0.0))
            .join(
                latest,
                and_(
                    ws.exchange_id == latest.c.eid,
                    ws.coin == latest.c.coin,
                    ws.ts_ms == latest.c.mt,
                ),
            )
            .where(ws.coin.in_(list(_CASH_COINS)))
        )
        if self._account is not None:
            stmt = stmt.where(ws.account == self._account)
        result = await session.execute(stmt)
        return float(result.scalar())

    async def _latest_cash_snapshot_ts(
        self,
        session: AsyncSession,
    ) -> int | None:
        """Return the MAX ts_ms among wallet_snapshot rows for cash coins (USDC/USDT).

        This is the timestamp of the freshest cash snapshot and is used to gate
        SPOT position values: a spot position whose opened_at is *after* this
        timestamp was bought with USDC that is still sitting in cash (the wallet
        snapshot hasn't caught up yet), so counting it would double-count that
        USDC.

        Returns None when no cash snapshots exist (fresh DB), in which case
        callers should fall back to counting all spot positions as normal.
        """
        ws = WalletSnapshotRow
        stmt = select(func.max(ws.ts_ms)).where(ws.coin.in_(list(_CASH_COINS)))
        if self._account is not None:
            stmt = stmt.where(ws.account == self._account)
        result = await session.execute(stmt)
        val = result.scalar()
        return int(val) if val is not None else None

    async def _compute_position_values(
        self,
        session: AsyncSession,
        strategy_id: int,
        quotes: dict[str, Quote],
        *,
        perp_unrealized_by_coin: dict[str, float] | None = None,
        spot_mids_by_coin: dict[str, float] | None = None,
        latest_cash_ts: int | None = None,
    ) -> tuple[float, float]:
        """Return (spot_value, perp_unrealized) for all OPEN positions
        linked to this strategy via farb_positions.

        COLLATERAL positions are intentionally excluded: their value is
        already captured in cash (wallet_snapshots reflects perp-wallet USDC
        which includes the collateral reservation).

        For coins missing from quotes, contribution = 0 and a WARN is logged.

        ``latest_cash_ts`` gates SPOT positions: if a spot position's
        opened_at is strictly after the freshest cash wallet_snapshot timestamp,
        the spot leg is skipped.  Rationale: while cash is stale-high (still
        holds the USDC spent on the buy), counting the freshly-bought spot would
        double-count that USDC; gating the spot leg until cash catches up keeps a
        delta-neutral open from bumping equity.  When latest_cash_ts is None
        (fresh DB, no cash snapshots yet) all spot positions are counted as
        normal — no regression to zero equity.
        """
        # Fetch all OPEN, non-COLLATERAL positions linked to this strategy.
        stmt = (
            select(PositionRow)
            .join(
                FarbPositionRow,
                PositionRow.farb_position_id == FarbPositionRow.id,
            )
            .where(
                FarbPositionRow.strategy_id == strategy_id,
                PositionRow.status == PositionStatus.OPEN.value,
                PositionRow.instrument != Instrument.COLLATERAL.value,
            )
        )
        result = await session.execute(stmt)
        open_positions = result.scalars().all()

        spot_value = 0.0
        perp_unrealized = 0.0
        warned_coins: set[str] = set()

        for pos in open_positions:
            coin = pos.coin
            instrument = Instrument(pos.instrument)
            side = Side(pos.side)

            if coin not in quotes:
                if coin not in warned_coins:
                    logger.warning(
                        "compute_equity: open %s %s position for coin %r has no quote; "
                        "contribution treated as 0",
                        instrument.value,
                        side.value,
                        coin,
                    )
                    warned_coins.add(coin)
                continue

            quote = quotes[coin]

            if instrument == Instrument.SPOT:
                # Gate: skip this spot leg if cash hasn't been re-snapshotted
                # since the position opened.  pos.opened_at and latest_cash_ts
                # are both Unix ms ints (see Position model: opened_at Mapped[int]).
                if (
                    latest_cash_ts is not None
                    and pos.opened_at is not None
                    and pos.opened_at > latest_cash_ts
                ):
                    logger.debug(
                        "compute_equity: skipping SPOT %s opened_at=%d (cash snapshot "
                        "is stale: latest_cash_ts=%d); USDC still in cash leg",
                        coin, pos.opened_at, latest_cash_ts,
                    )
                    continue
                # Prefer authoritative HL spot mid, then quote.spot, then mark.
                if spot_mids_by_coin is not None and coin in spot_mids_by_coin:
                    price = spot_mids_by_coin[coin]
                elif quote.spot is not None:
                    price = quote.spot
                else:
                    price = quote.mark
                spot_value += pos.qty * price

            elif instrument == Instrument.PERP:
                if perp_unrealized_by_coin is not None and coin in perp_unrealized_by_coin:
                    perp_unrealized += perp_unrealized_by_coin[coin]
                    continue
                mark = quote.mark
                if side == Side.LONG:
                    perp_unrealized += (mark - pos.entry_price) * pos.qty
                elif side == Side.SHORT:
                    perp_unrealized += (pos.entry_price - mark) * pos.qty
                # Side.NONE should not appear for PERP, but if it does: skip

        return spot_value, perp_unrealized

    async def _compute_perp_realized(
        self,
        session: AsyncSession,
        strategy_id: int,
    ) -> float:
        """SUM realized P&L for CLOSED PERP positions: (exit-entry)*qty for LONG, (entry-exit)*qty for SHORT, minus fees."""
        # Fetch CLOSED PERP positions for this strategy
        stmt = (
            select(PositionRow)
            .join(
                FarbPositionRow,
                PositionRow.farb_position_id == FarbPositionRow.id,
            )
            .where(
                FarbPositionRow.strategy_id == strategy_id,
                PositionRow.status == PositionStatus.CLOSED.value,
                PositionRow.instrument == Instrument.PERP.value,
            )
        )
        result = await session.execute(stmt)
        closed_perp_positions = result.scalars().all()

        total_realized = 0.0

        for pos in closed_perp_positions:
            # Find the closing fill: the fill whose side is opposite to the
            # position's opening side.
            # Opening LONG → closing fill side = "short"
            # Opening SHORT → closing fill side = "long"
            side = Side(pos.side)
            if side == Side.LONG:
                closing_fill_side = Side.SHORT.value
            elif side == Side.SHORT:
                closing_fill_side = Side.LONG.value
            else:
                continue  # NONE side on PERP — skip

            # Aggregate closing fills for this position
            close_fill_stmt = select(
                func.coalesce(func.sum(FillRow.price * FillRow.qty), 0.0),
                func.coalesce(func.sum(FillRow.qty), 0.0),
                func.coalesce(func.sum(FillRow.fee), 0.0),
            ).where(
                FillRow.position_id == pos.id,
                FillRow.side == closing_fill_side,
            )
            fill_result = await session.execute(close_fill_stmt)
            price_x_qty, close_qty, close_fees = fill_result.one()

            if close_qty == 0.0:
                # No closing fill recorded — skip (shouldn't happen for CLOSED)
                continue

            avg_exit_price = price_x_qty / close_qty

            if side == Side.LONG:
                pnl = (avg_exit_price - pos.entry_price) * close_qty
            else:  # SHORT
                pnl = (pos.entry_price - avg_exit_price) * close_qty

            total_realized += pnl - close_fees

        return total_realized

    async def _compute_funding_cum(
        self,
        session: AsyncSession,
        strategy_id: int,
    ) -> float:
        """SUM of funding_accruals.amount for actively-held positions (PRE/POST_BREAKEVEN).
        Closed positions' funding stays out of the live counter (it has already
        settled into wallet cash)."""
        active_values = [s.value for s in ACTIVE_STATES]
        stmt = (
            select(func.coalesce(func.sum(FundingAccrualRow.amount), 0.0))
            .join(PositionRow, FundingAccrualRow.position_id == PositionRow.id)
            .join(FarbPositionRow, PositionRow.farb_position_id == FarbPositionRow.id)
            .where(
                FarbPositionRow.strategy_id == strategy_id,
                FarbPositionRow.state.in_(active_values),
            )
        )
        result = await session.execute(stmt)
        return float(result.scalar())

    async def _compute_fees_cum(
        self,
        session: AsyncSession,
        strategy_id: int,
    ) -> float:
        """SUM of fills.fee for actively-held positions (PRE/POST_BREAKEVEN) only.
        Closed positions' fees stay out of the live counter (they've already
        settled into wallet cash via realized PnL)."""
        active_values = [s.value for s in ACTIVE_STATES]
        stmt = (
            select(func.coalesce(func.sum(FillRow.fee), 0.0))
            .join(PositionRow, FillRow.position_id == PositionRow.id)
            .join(FarbPositionRow, PositionRow.farb_position_id == FarbPositionRow.id)
            .where(
                FarbPositionRow.strategy_id == strategy_id,
                FarbPositionRow.state.in_(active_values),
            )
        )
        result = await session.execute(stmt)
        return float(result.scalar())
