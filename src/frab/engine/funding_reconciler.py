"""Periodic funding reconciliation: stamp real HL funding payments onto Position rows.

Problem: strategy previously computed per-hour funding as qty × mark × rate,
which drifts from HL's actual payment due to mark snapshot timing and can
double-count on engine restart. This module polls `userFunding` and overwrites
Position.funding_collected with the authoritative SUM from HL.
"""
from __future__ import annotations

import structlog
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from frab.application.portfolio_service import PortfolioService
from frab.db.models import Market, Position, PositionStatus
from frab.db.session import session_scope
from frab.events.bus import Event, EventBus
from frab.exchanges.base import FundingPayment

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class FundingReconcileReport:
    payments_seen: int       # HL events returned
    positions_updated: int   # local positions touched
    unmatched: int           # HL events with no matching open/closed position


class FundingReconciler:
    """Pull HL userFunding and overwrite Position.funding_collected with authoritative SUM.

    For each open or recently-closed position (within lookback window),
    sum HL userFunding payments where:
      - payment.coin == position.coin (joined via Market)
      - payment.ts ∈ [position.opened_at, position.closed_at or now]
    Then UPDATE position.funding_collected = matched_sum.

    Idempotent — re-running yields the same DB state.
    """

    LOOKBACK_HOURS_DEFAULT = 24

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker,
        market_data,
        user_address: str,
        bus: EventBus,
        lookback_hours: int = LOOKBACK_HOURS_DEFAULT,
        clock_fn: Callable[[], datetime] | None = None,
        portfolio_service: PortfolioService | None = None,
        strategy_id: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._market_data = market_data
        self._user_address = user_address
        self._bus = bus
        self._lookback_hours = lookback_hours
        self._clock_fn = clock_fn if clock_fn is not None else (lambda: datetime.now(UTC))
        self._portfolio_service = portfolio_service
        self._strategy_id = strategy_id

    async def run_once(self) -> FundingReconcileReport:
        """One pass: query HL, match positions, write. Idempotent on repeat."""
        now = self._clock_fn()
        now_ms = int(now.timestamp() * 1000)
        since_ms = now_ms - self._lookback_hours * 3_600_000

        payments: list[FundingPayment] = await self._market_data.fetch_user_funding(
            self._user_address, since_ms
        )

        since_dt = datetime.fromtimestamp(since_ms / 1000, tz=UTC)

        positions_updated = 0
        # Track which (coin, ts) pairs were matched to any position.
        matched_keys: set[tuple[str, datetime]] = set()
        # Earliest opened_at per coin across ALL positions in DB — used to
        # filter out pre-history HL funding payments that predate our first
        # tracked position (legitimate income but unattributable).
        earliest_opened: dict[str, datetime] = {}

        async with session_scope(self._session_factory) as session:
            min_stmt = (
                select(Market.coin, func.min(Position.opened_at))
                .join(Position, Position.market_id == Market.id)
                .group_by(Market.coin)
            )
            for coin_name, min_ts in (await session.execute(min_stmt)).all():
                if min_ts is None:
                    continue
                if min_ts.tzinfo is None:
                    min_ts = min_ts.replace(tzinfo=UTC)
                earliest_opened[coin_name] = min_ts

            # Load open positions and recently-closed positions within lookback.
            stmt = (
                select(Position, Market.coin)
                .join(Market, Position.market_id == Market.id)
                .where(
                    (Position.status == PositionStatus.OPEN)
                    | (
                        (Position.status == PositionStatus.CLOSED)
                        & (Position.closed_at >= since_dt)
                    )
                )
            )
            result = await session.execute(stmt)
            rows = result.all()

            for pos, coin in rows:
                opened_at = pos.opened_at
                if opened_at.tzinfo is None:
                    opened_at = opened_at.replace(tzinfo=UTC)

                if pos.status == PositionStatus.CLOSED and pos.closed_at is not None:
                    closed_at = pos.closed_at
                    if closed_at.tzinfo is None:
                        closed_at = closed_at.replace(tzinfo=UTC)
                    upper = closed_at
                else:
                    upper = now

                matched_sum = 0.0
                for p in payments:
                    if p.coin != coin:
                        continue
                    if p.ts < opened_at or p.ts > upper:
                        continue
                    matched_sum += p.usdc
                    matched_keys.add((p.coin, p.ts))

                pos.funding_collected = matched_sum
                positions_updated += 1

            # Sync portfolio_service's running funding counter from DB authoritative SUM.
            if self._portfolio_service is not None and self._strategy_id is not None:
                sum_stmt = (
                    select(func.sum(Position.funding_collected))
                    .where(Position.strategy_id == self._strategy_id)
                )
                total = (await session.execute(sum_stmt)).scalar() or 0.0
                await self._portfolio_service.set_funding_cum(float(total))

        # Count unmatched: payments with no position match at all.
        # Exclude pre-history: payments whose ts predates our earliest tracked
        # position for that coin (legitimate income but unattributable).
        # Payments for coins we've never had a position in DO count as unmatched
        # (suspicious — wrong wallet address or schema drift).
        unmatched_payments = []
        for p in payments:
            if (p.coin, p.ts) in matched_keys:
                continue
            earliest = earliest_opened.get(p.coin)
            if earliest is not None and p.ts < earliest:
                continue
            unmatched_payments.append(p)
        unmatched = len(unmatched_payments)

        report = FundingReconcileReport(
            payments_seen=len(payments),
            positions_updated=positions_updated,
            unmatched=unmatched,
        )

        await self._bus.publish(Event(
            ts=now,
            level="INFO",
            source="funding_reconcile",
            kind="funding_reconcile_done",
            message=(
                f"Funding reconcile done: {positions_updated} positions updated, "
                f"{unmatched} unmatched"
            ),
            payload_json={
                "payments_seen": report.payments_seen,
                "positions_updated": report.positions_updated,
                "unmatched": report.unmatched,
            },
        ))

        if unmatched > 0:
            await self._bus.publish(Event(
                ts=now,
                level="WARNING",
                source="funding_reconcile",
                kind="funding_reconcile_unmatched",
                message=f"Funding reconcile: {unmatched} HL payments had no matching position",
                payload_json={
                    "unmatched": [
                        {"coin": p.coin, "ts": p.ts.isoformat(), "usdc": p.usdc}
                        for p in unmatched_payments[:20]
                    ]
                },
            ))

        logger.info(
            "funding_reconcile_done",
            payments_seen=report.payments_seen,
            positions_updated=report.positions_updated,
            unmatched=report.unmatched,
        )
        return report
