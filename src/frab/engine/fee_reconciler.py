"""Periodic fee reconciliation: stamp real HL fees onto local Fill rows.

Problem: `market_open` response on HL doesn't echo fees, so Fill.fee stays 0
after execution. This module polls `userFillsByTime` and matches HL fills back
to local Fill rows, writing the real fee into Fill.fee and recomputing
Position.fees_paid as SUM(fills.fee) for each affected position.
"""
from __future__ import annotations

import structlog
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from frab.db.models import Fill, Market, Position
from frab.db.session import session_scope
from frab.events.bus import Event, EventBus
from frab.exchanges.base import UserFill
from frab.strategies.base import Strategy

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReconcileMatchReport:
    candidates_seen: int       # HL userFills rows returned
    matched: int               # local fills updated
    skipped_already_set: int   # local fill already had fee > 0
    unmatched_hl: int          # HL rows with no matching local fill


@dataclass
class _LocalFill:
    """Lightweight in-memory representation of a local Fill row for matching."""
    id: int
    coin: str
    leg: object  # Leg enum
    side: object  # Side enum
    qty: float
    ts: datetime
    position_id: int
    fee_nonzero: bool  # True if fill.fee > 0 at load time


class FeeReconciler:
    """Periodically pull HL userFills and stamp real fees onto local fills + positions.

    Matching rule:
      A local Fill matches an HL fill when they share the same coin, leg, and
      side, the fill timestamps are within TS_WINDOW_S seconds of each other,
      and the absolute relative qty difference is ≤ QTY_TOL. This covers HL's
      szDecimals rounding while avoiding cross-coin false matches.

    Fee unit normalization:
      HL returns fees in the feeToken field. For perp fills and spot SELL fills
      the fee is always in USDC → written as-is. For spot BUY fills HL returns
      the fee in the bought asset (e.g. UBTC units); multiplying by fill.price
      (USDC/asset) converts to USDC.
    """

    TS_WINDOW_S = 10.0
    QTY_TOL = 0.001  # 0.1% relative tolerance

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker,
        market_data,
        user_address: str,
        bus: EventBus,
        lookback_hours: int = 24,
        clock_fn: Callable[[], datetime] | None = None,
        strategy: Strategy | None = None,
        strategy_id: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._market_data = market_data
        self._user_address = user_address
        self._bus = bus
        self._lookback_hours = lookback_hours
        self._clock_fn = clock_fn if clock_fn is not None else (lambda: datetime.now(UTC))
        self._strategy = strategy
        self._strategy_id = strategy_id

    def _fee_usdc(self, hl_fill: UserFill) -> float:
        """Return fee in USDC.

        If feeToken is not USDC the fee is denominated in the bought asset
        (spot BUY case) — multiply by the fill price to convert to USDC.
        """
        if hl_fill.fee_token == "USDC":
            return hl_fill.fee
        # Asset-denominated fee (e.g. UBTC): convert at fill price.
        return hl_fill.fee * hl_fill.price

    def _matches(self, local: _LocalFill, hl: UserFill) -> bool:
        if local.coin != hl.coin:
            return False
        if local.leg != hl.leg:
            return False
        if local.side != hl.side:
            return False
        ts_diff = abs((local.ts - hl.ts).total_seconds())
        if ts_diff > self.TS_WINDOW_S:
            return False
        if hl.qty == 0:
            return False
        qty_rel = abs(local.qty - hl.qty) / hl.qty
        if qty_rel > self.QTY_TOL:
            return False
        return True

    async def run_once(self) -> ReconcileMatchReport:
        """One pass: query HL, match local fills, write. Idempotent on repeat."""
        now = self._clock_fn()
        now_ms = int(now.timestamp() * 1000)
        since_ms = now_ms - self._lookback_hours * 3_600_000

        hl_fills = await self._market_data.fetch_user_fills(self._user_address, since_ms)

        since_dt = datetime.fromtimestamp(since_ms / 1000, tz=UTC)

        matched = 0
        skipped_already_set = 0
        unmatched_hl = 0
        unmatched_hl_fills: list[UserFill] = []

        async with session_scope(self._session_factory) as session:
            # Load ALL local fills in the time window (both fee==0 and fee>0).
            # We need fee>0 fills too so we can classify HL fills as "already set"
            # vs "genuinely unmatched" — without touching them a second time.
            stmt = (
                select(Fill, Market.coin)
                .join(Position, Fill.position_id == Position.id)
                .join(Market, Position.market_id == Market.id)
                .where(Fill.ts >= since_dt)
            )
            result = await session.execute(stmt)
            rows = result.all()

            # Separate into fee==0 candidates (writable) and fee>0 (read-only for classification).
            zero_candidates: dict[int, _LocalFill] = {}
            nonzero_pool: list[_LocalFill] = []

            for fill, coin in rows:
                ts = fill.ts if fill.ts.tzinfo is not None else fill.ts.replace(tzinfo=UTC)
                lf = _LocalFill(
                    id=fill.id,
                    coin=coin,
                    leg=fill.leg,
                    side=fill.side,
                    qty=fill.qty,
                    ts=ts,
                    position_id=fill.position_id,
                    fee_nonzero=(fill.fee > 0),
                )
                if fill.fee == 0:
                    zero_candidates[fill.id] = lf
                else:
                    nonzero_pool.append(lf)

            affected_position_ids: set[int] = set()

            for hl in hl_fills:
                # Try fee==0 candidates first (these are the ones we want to write).
                matched_id: int | None = None
                for fid, local in zero_candidates.items():
                    if self._matches(local, hl):
                        matched_id = fid
                        break

                if matched_id is not None:
                    local = zero_candidates.pop(matched_id)
                    fee_usdc = self._fee_usdc(hl)
                    fill_obj = await session.get(Fill, matched_id)
                    if fill_obj is None:
                        continue
                    fill_obj.fee = fee_usdc
                    affected_position_ids.add(local.position_id)
                    matched += 1
                    continue

                # No fee==0 match: check if this HL fill corresponds to a fill
                # that was already reconciled in a prior run (fee > 0).
                already_idx: int | None = None
                for i, local in enumerate(nonzero_pool):
                    if self._matches(local, hl):
                        already_idx = i
                        break

                if already_idx is not None:
                    nonzero_pool.pop(already_idx)
                    skipped_already_set += 1
                else:
                    unmatched_hl += 1
                    unmatched_hl_fills.append(hl)

            # Recompute fees_paid for every position that had a fill updated.
            for pos_id in affected_position_ids:
                fee_sum_stmt = (
                    select(func.sum(Fill.fee))
                    .where(Fill.position_id == pos_id)
                )
                total_fees = (await session.execute(fee_sum_stmt)).scalar() or 0.0
                await session.execute(
                    update(Position)
                    .where(Position.id == pos_id)
                    .values(fees_paid=total_fees)
                )

            # Sync strategy's running fees counter from DB authoritative SUM
            # so the next equity snapshot picks up reconciled fees.
            if self._strategy is not None and self._strategy_id is not None:
                sum_stmt = (
                    select(func.sum(Position.fees_paid))
                    .where(Position.strategy_id == self._strategy_id)
                )
                total = (await session.execute(sum_stmt)).scalar() or 0.0
                self._strategy.set_fees_cum(float(total))

        report = ReconcileMatchReport(
            candidates_seen=len(hl_fills),
            matched=matched,
            skipped_already_set=skipped_already_set,
            unmatched_hl=unmatched_hl,
        )

        await self._bus.publish(Event(
            ts=now,
            level="INFO",
            source="fee_reconcile",
            kind="fee_reconcile_done",
            message=(
                f"Fee reconcile done: {matched} matched, "
                f"{skipped_already_set} skipped, {unmatched_hl} unmatched"
            ),
            payload_json={
                "candidates_seen": report.candidates_seen,
                "matched": report.matched,
                "skipped_already_set": report.skipped_already_set,
                "unmatched_hl": report.unmatched_hl,
            },
        ))

        if unmatched_hl > 0:
            await self._bus.publish(Event(
                ts=now,
                level="WARNING",
                source="fee_reconcile",
                kind="fee_reconcile_unmatched",
                message=f"Fee reconcile: {unmatched_hl} HL fills had no matching local fill",
                payload_json={
                    "unmatched": [
                        {"coin": f.coin, "ts": f.ts.isoformat(), "qty": f.qty}
                        for f in unmatched_hl_fills
                    ]
                },
            ))

        logger.info(
            "fee_reconcile_done",
            candidates_seen=report.candidates_seen,
            matched=report.matched,
            skipped=report.skipped_already_set,
            unmatched=report.unmatched_hl,
        )
        return report
