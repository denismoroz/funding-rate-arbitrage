"""Release COLLATERAL bookkeeping rows left open under FAILED xsmom positions.

A FAILED position never reaches CLOSE, which is the only place that releases its
collateral row — so the lock stays OPEN forever and inflates reserved margin in
the API/UI. Real money is not moved by this: COLLATERAL is an internal accounting
row, not an exchange-side lock.

Audit before the first run (2026-09-12): CLOSED positions had 0 stale locks (the
normal path releases correctly), FAILED had 4 locks worth $179.40.

    uv run python scripts/release_failed_collateral.py            # show the plan
    uv run python scripts/release_failed_collateral.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import time

from sqlalchemy import select

from frab.db.models import Position as DBPosition, XsmomPosition as XsmomPositionRow
from frab.db.session import create_engine, make_session_factory
from frab.domain import Instrument, PositionStatus, XsmomState
from frab.settings import get_settings


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually close the rows")
    args = ap.parse_args()

    settings = get_settings()
    sf = make_session_factory(create_engine(settings.db_url))

    async with sf() as s:
        rows = (await s.execute(
            select(XsmomPositionRow, DBPosition)
            .join(DBPosition, DBPosition.id == XsmomPositionRow.collateral_position_id)
            .where(
                XsmomPositionRow.state == XsmomState.FAILED.value,
                DBPosition.status == PositionStatus.OPEN.value,
                DBPosition.instrument == Instrument.COLLATERAL.value,
            )
        )).all()

        if not rows:
            print("no stale collateral locks under FAILED positions.")
            return 0

        total = 0.0
        print(f"{'xsmom':>6} {'coin':<6} {'coll_id':>8} {'usdc':>9}  failure_reason")
        for xp, pos in rows:
            total += pos.qty
            reason = (xp.state_data or {}).get("failure_reason", "")
            print(f"{xp.id:>6} {xp.coin:<6} {pos.id:>8} {pos.qty:>9.2f}  {reason[:60]}")
        print(f"\n{len(rows)} locks, ${total:,.2f} of reserved margin to release")

        if not args.apply:
            print("\nDRY RUN — rerun with --apply to close these rows.")
            return 0

        now_ms = int(time.time() * 1000)
        for _xp, pos in rows:
            pos.status = PositionStatus.CLOSED.value
            pos.closed_at = now_ms
        await s.commit()
        print(f"\nreleased {len(rows)} collateral rows.")

    # Verify in a fresh session that nothing is left.
    async with sf() as s:
        left = (await s.execute(
            select(DBPosition.id)
            .join(XsmomPositionRow,
                  XsmomPositionRow.collateral_position_id == DBPosition.id)
            .where(
                XsmomPositionRow.state == XsmomState.FAILED.value,
                DBPosition.status == PositionStatus.OPEN.value,
            )
        )).all()
    print(f"remaining stale locks: {len(left)}")
    return 0 if not left else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
