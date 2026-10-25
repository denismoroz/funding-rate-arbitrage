"""SpotReconcile — compares the spot qty recorded for a coin with the wallet balance."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.db.models import Position as PositionRow
from frab.db.session import session_scope
from frab.domain import FarbPosition, Instrument, PositionStatus
from frab.events.bus import EventBus
from frab.exchanges.protocol import Exchange
import frab.strategy.two_phase as _pkg  # logger looked up at call time so patch.object works
from frab.strategy.two_phase.states._helpers import publish_event


async def reconcile_spot_leg(
    *,
    exchange: Exchange,
    session_factory: async_sessionmaker[AsyncSession],
    event_bus: EventBus | None,
    fp: FarbPosition,
    wallet_qty: float,
) -> None:
    """Right after a spot leg is opened, compare what the DB says is held in the coin
    (all OPEN spot rows of the coin) with what the wallet holds, and raise a WARNING
    event when they part by more than one lot.

    A difference below one lot is normal: remainders of earlier closes that cannot be
    sold (below the lot) stay in the wallet and are covered by the hedge. More than that
    means the record and the exchange disagree — the spot leg recorded at the ordered
    quantity instead of the filled one (HYPE #31) is what this check exists to catch.

    Never raises: a failed check must not stop a live open.
    """
    try:
        lot_of = getattr(exchange, "get_lot_size", None)
        if lot_of is None:
            return
        lot = float(await lot_of(fp.coin))
        async with session_scope(session_factory) as session:
            recorded = float((await session.execute(
                select(func.coalesce(func.sum(PositionRow.qty), 0.0)).where(
                    PositionRow.coin == fp.coin,
                    PositionRow.instrument == Instrument.SPOT.value,
                    PositionRow.status == PositionStatus.OPEN.value,
                )
            )).scalar_one())
        diff = wallet_qty - recorded
        # The relative epsilon keeps float noise on a difference of exactly one lot quiet.
        if abs(diff) <= lot * (1 + 1e-9):
            _pkg.logger.info(
                "spot reconciled fp=%s coin=%s db=%.8f wallet=%.8f diff=%.8f lot=%s",
                fp.id, fp.coin, recorded, wallet_qty, diff, lot,
            )
            return
        _pkg.logger.warning(
            "spot mismatch fp=%s coin=%s db=%.8f wallet=%.8f diff=%.8f lot=%s",
            fp.id, fp.coin, recorded, wallet_qty, diff, lot,
        )
        await publish_event(
            event_bus,
            level="WARNING",
            kind="spot.mismatch",
            message=(f"{fp.coin}: spot in DB {recorded:.8f} vs wallet {wallet_qty:.8f} "
                     f"(diff {diff:+.8f}, lot {lot})"),
            payload={"farb_position_id": fp.id, "coin": fp.coin, "db": recorded,
                     "wallet": wallet_qty, "diff": diff, "lot": lot},
        )
    except Exception as exc:  # noqa: BLE001
        _pkg.logger.warning("spot reconcile failed fp=%s coin=%s: %s", fp.id, fp.coin, exc)
