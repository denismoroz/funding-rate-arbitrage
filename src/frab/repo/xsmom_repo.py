"""XsmomRepo — thin DAO for xsmom_positions, xsmom_scans, and xsmom_daily_prices rows.

No business logic. Only persistence and atomic state transitions.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.domain import Side, XsmomPosition, XsmomState, XSMOM_ACTIVE_STATES
from frab.db.models import XsmomPosition as XsmomPositionRow
from frab.db.models import XsmomScan as XsmomScanRow
from frab.db.models import XsmomDailyPrice as XsmomDailyPriceRow
from frab.db.session import session_scope

# Re-export StateConflict from farb_repo to avoid duplication.
# The message says "FarbPosition …" which is slightly inaccurate for xsmom
# but the exception class itself is generic enough to reuse.  We define a
# separate XsmomStateConflict with an xsmom-specific message so callers can
# distinguish and error messages make sense.
from frab.repo.farb_repo import _now_ms, _dt_to_ms, _ms_to_dt  # noqa: F401 — re-exported helpers


class XsmomStateConflict(Exception):
    """Atomic transition failed: row state was not the expected `from_state`."""

    def __init__(
        self,
        xsmom_position_id: int,
        expected: XsmomState | str,
        actual: XsmomState | None,
    ) -> None:
        expected_str = expected.value if isinstance(expected, XsmomState) else expected
        actual_str = actual.value if actual is not None else "MISSING"
        super().__init__(
            f"XsmomPosition {xsmom_position_id}: expected state={expected_str}, actual={actual_str}"
        )
        self.xsmom_position_id = xsmom_position_id
        self.expected = expected
        self.actual = actual


# ── Terminal states ───────────────────────────────────────────────────────────

_TERMINAL_STATES = {XsmomState.CLOSED, XsmomState.FAILED}


# ── ORM → domain mapper ───────────────────────────────────────────────────────

def _to_domain(row: XsmomPositionRow) -> XsmomPosition:
    return XsmomPosition(
        id=row.id,
        strategy_id=row.strategy_id,
        coin=row.coin,
        side=Side(row.side),
        state=XsmomState(row.state),
        state_data=row.state_data if row.state_data is not None else {},
        perp_position_id=row.perp_position_id,
        collateral_position_id=row.collateral_position_id,
        target_qty=row.target_qty,
        opened_at=_ms_to_dt(row.opened_at),
        closed_at=_ms_to_dt(row.closed_at) if row.closed_at is not None else None,
    )


# ── XsmomRepo ─────────────────────────────────────────────────────────────────

class XsmomRepo:
    """Data-access object for xsmom_positions, xsmom_scans, and xsmom_daily_prices rows.

    Each public method opens its own session, commits, and closes it.
    No long-lived sessions are held.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # ── CRUD: XsmomPosition ───────────────────────────────────────────────────

    async def create(
        self,
        *,
        strategy_id: int,
        coin: str,
        side: Side,
        target_qty: float | None = None,
        initial_state: XsmomState = XsmomState.NEW,
        state_data: dict | None = None,
    ) -> XsmomPosition:
        """Insert new xsmom_positions row. Returns domain XsmomPosition with id populated."""
        now_ms = _now_ms()
        row = XsmomPositionRow(
            strategy_id=strategy_id,
            coin=coin,
            side=side,
            state=initial_state,
            state_data=state_data if state_data is not None else {},
            perp_position_id=None,
            collateral_position_id=None,
            target_qty=target_qty,
            opened_at=now_ms,
            closed_at=None,
        )
        async with session_scope(self._sf) as session:
            session.add(row)
            await session.flush()
            domain = _to_domain(row)
        return domain

    async def get(self, id: int) -> XsmomPosition | None:
        """Return domain XsmomPosition by id, or None if not found."""
        async with session_scope(self._sf) as session:
            row = await session.get(XsmomPositionRow, id)
            if row is None:
                return None
            return _to_domain(row)

    async def list_non_terminal(self, strategy_id: int) -> list[XsmomPosition]:
        """All xsmom_positions for strategy where state NOT IN (CLOSED, FAILED)."""
        terminal_values = [s.value for s in _TERMINAL_STATES]
        async with session_scope(self._sf) as session:
            result = await session.execute(
                select(XsmomPositionRow).where(
                    XsmomPositionRow.strategy_id == strategy_id,
                    XsmomPositionRow.state.not_in(terminal_values),
                )
            )
            rows = result.scalars().all()
            return [_to_domain(r) for r in rows]

    async def list_active(self, strategy_id: int) -> list[XsmomPosition]:
        """Return all xsmom_positions in an actively-holding state (OPENED)."""
        active_values = [s.value for s in XSMOM_ACTIVE_STATES]
        async with session_scope(self._sf) as session:
            result = await session.execute(
                select(XsmomPositionRow).where(
                    XsmomPositionRow.strategy_id == strategy_id,
                    XsmomPositionRow.state.in_(active_values),
                )
            )
            rows = result.scalars().all()
            return [_to_domain(r) for r in rows]

    async def list_in_state(
        self, strategy_id: int, state: XsmomState
    ) -> list[XsmomPosition]:
        """Return all xsmom_positions in the given state for the strategy."""
        async with session_scope(self._sf) as session:
            result = await session.execute(
                select(XsmomPositionRow).where(
                    XsmomPositionRow.strategy_id == strategy_id,
                    XsmomPositionRow.state == state.value,
                )
            )
            rows = result.scalars().all()
            return [_to_domain(r) for r in rows]

    async def list_by_coin(
        self,
        strategy_id: int,
        coin: str,
        include_terminal: bool = False,
    ) -> list[XsmomPosition]:
        """Return xsmom_positions for a specific coin.

        By default excludes terminal states (CLOSED, FAILED).
        Pass include_terminal=True to include all rows.
        """
        async with session_scope(self._sf) as session:
            stmt = select(XsmomPositionRow).where(
                XsmomPositionRow.strategy_id == strategy_id,
                XsmomPositionRow.coin == coin,
            )
            if not include_terminal:
                terminal_values = [s.value for s in _TERMINAL_STATES]
                stmt = stmt.where(XsmomPositionRow.state.not_in(terminal_values))
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [_to_domain(r) for r in rows]

    # ── Atomic state transitions ──────────────────────────────────────────────

    async def transition(
        self,
        id: int,
        *,
        from_state: XsmomState,
        to_state: XsmomState,
        state_data: dict | None = None,
    ) -> XsmomPosition:
        """Atomic UPDATE: SET state=to_state WHERE id=? AND state=from_state.

        Raises XsmomStateConflict if rowcount == 0 (wrong state or missing row).
        Returns the updated domain XsmomPosition.
        """
        async with session_scope(self._sf) as session:
            values: dict = {"state": to_state.value}
            if state_data is not None:
                values["state_data"] = state_data

            result = await session.execute(
                update(XsmomPositionRow)
                .where(
                    XsmomPositionRow.id == id,
                    XsmomPositionRow.state == from_state.value,
                )
                .values(**values)
                .returning(XsmomPositionRow)
            )
            updated_row = result.scalar_one_or_none()

            if updated_row is None:
                current = await session.get(XsmomPositionRow, id)
                actual: XsmomState | None = (
                    XsmomState(current.state) if current is not None else None
                )
                raise XsmomStateConflict(id, from_state, actual)

            return _to_domain(updated_row)

    async def set_leg(
        self,
        id: int,
        *,
        perp_position_id: int | None = None,
        collateral_position_id: int | None = None,
    ) -> XsmomPosition:
        """Set perp_position_id and/or collateral_position_id for whichever are provided.

        NOT atomic with respect to state — does not check or change state.
        """
        values: dict = {}
        if perp_position_id is not None:
            values["perp_position_id"] = perp_position_id
        if collateral_position_id is not None:
            values["collateral_position_id"] = collateral_position_id

        if not values:
            raise ValueError("set_leg: must provide at least one of perp_position_id, collateral_position_id")

        async with session_scope(self._sf) as session:
            result = await session.execute(
                update(XsmomPositionRow)
                .where(XsmomPositionRow.id == id)
                .values(**values)
                .returning(XsmomPositionRow)
            )
            updated_row = result.scalar_one_or_none()
            if updated_row is None:
                raise KeyError(f"XsmomPosition {id} not found")
            return _to_domain(updated_row)

    async def update_state_data(
        self,
        id: int,
        state_data: dict,
    ) -> XsmomPosition:
        """Replace state_data wholesale. State is unchanged."""
        async with session_scope(self._sf) as session:
            result = await session.execute(
                update(XsmomPositionRow)
                .where(XsmomPositionRow.id == id)
                .values(state_data=state_data)
                .returning(XsmomPositionRow)
            )
            updated_row = result.scalar_one_or_none()
            if updated_row is None:
                raise KeyError(f"XsmomPosition {id} not found")
            return _to_domain(updated_row)

    async def mark_closed(self, id: int) -> XsmomPosition:
        """Atomic transition to CLOSED + set closed_at=now, from any non-terminal state.

        Raises XsmomStateConflict if already CLOSED or FAILED.
        """
        now_ms = _now_ms()
        async with session_scope(self._sf) as session:
            result = await session.execute(
                update(XsmomPositionRow)
                .where(
                    XsmomPositionRow.id == id,
                    XsmomPositionRow.state != XsmomState.CLOSED.value,
                    XsmomPositionRow.state != XsmomState.FAILED.value,
                )
                .values(state=XsmomState.CLOSED.value, closed_at=now_ms)
                .returning(XsmomPositionRow)
            )
            updated_row = result.scalar_one_or_none()
            if updated_row is None:
                current = await session.get(XsmomPositionRow, id)
                actual = XsmomState(current.state) if current is not None else None
                raise XsmomStateConflict(id, "non-terminal", actual)
            return _to_domain(updated_row)

    async def mark_failed(
        self,
        id: int,
        reason: str,
    ) -> XsmomPosition:
        """Move to FAILED from any non-terminal state.

        Sets state_data['failure_reason']=reason and closed_at=now.
        """
        now_ms = _now_ms()
        async with session_scope(self._sf) as session:
            current = await session.get(XsmomPositionRow, id)
            if current is None:
                raise KeyError(f"XsmomPosition {id} not found")

            existing_state_data: dict = (
                current.state_data if current.state_data is not None else {}
            )
            new_state_data = {**existing_state_data, "failure_reason": reason}

            result = await session.execute(
                update(XsmomPositionRow)
                .where(
                    XsmomPositionRow.id == id,
                    XsmomPositionRow.state != XsmomState.CLOSED.value,
                    XsmomPositionRow.state != XsmomState.FAILED.value,
                )
                .values(
                    state=XsmomState.FAILED.value,
                    state_data=new_state_data,
                    closed_at=now_ms,
                )
                .returning(XsmomPositionRow)
            )
            updated_row = result.scalar_one_or_none()
            if updated_row is None:
                raise XsmomStateConflict(id, "non-terminal", XsmomState(current.state))
            return _to_domain(updated_row)

    # ── XsmomScan ─────────────────────────────────────────────────────────────

    async def record_scan(
        self,
        *,
        strategy_id: int,
        ts_ms: int,
        ranking: list | dict,
        n_long: int,
        n_short: int,
        note: str | None = None,
    ) -> int:
        """Insert an XsmomScan row. Returns the new scan id."""
        row = XsmomScanRow(
            strategy_id=strategy_id,
            ts_ms=ts_ms,
            ranking_json=ranking,
            n_long=n_long,
            n_short=n_short,
            note=note,
        )
        async with session_scope(self._sf) as session:
            session.add(row)
            await session.flush()
            scan_id = row.id
        return scan_id

    async def latest_scans(
        self, strategy_id: int, limit: int = 50
    ) -> list[dict]:
        """Return the most recent scans for the strategy (most recent first)."""
        async with session_scope(self._sf) as session:
            result = await session.execute(
                select(XsmomScanRow)
                .where(XsmomScanRow.strategy_id == strategy_id)
                .order_by(XsmomScanRow.ts_ms.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
            return [
                {
                    "id": r.id,
                    "strategy_id": r.strategy_id,
                    "ts_ms": r.ts_ms,
                    "ranking": r.ranking_json,
                    "n_long": r.n_long,
                    "n_short": r.n_short,
                    "note": r.note,
                }
                for r in rows
            ]

    # ── XsmomDailyPrice ───────────────────────────────────────────────────────

    async def upsert_daily_prices(self, rows: list[tuple[str, int, float]]) -> None:
        """Upsert (coin, day_ms, close) rows.

        Uses SQLite ON CONFLICT(coin, day_ms) DO UPDATE to make it idempotent.
        """
        if not rows:
            return
        async with session_scope(self._sf) as session:
            await session.execute(
                text("""
                    INSERT INTO xsmom_daily_prices (coin, day_ms, close)
                    VALUES (:coin, :day_ms, :close)
                    ON CONFLICT(coin, day_ms) DO UPDATE SET close = excluded.close
                """),
                [{"coin": coin, "day_ms": day_ms, "close": close} for coin, day_ms, close in rows],
            )

    async def get_daily_closes(
        self,
        coins: list[str],
        since_day_ms: int | None = None,
    ) -> dict[str, list[tuple[int, float]]]:
        """Return per-coin close prices, ascending by day_ms.

        Returns dict mapping coin → [(day_ms, close), ...].
        """
        if not coins:
            return {}
        async with session_scope(self._sf) as session:
            stmt = select(XsmomDailyPriceRow).where(
                XsmomDailyPriceRow.coin.in_(coins),
            )
            if since_day_ms is not None:
                stmt = stmt.where(XsmomDailyPriceRow.day_ms >= since_day_ms)
            stmt = stmt.order_by(XsmomDailyPriceRow.coin, XsmomDailyPriceRow.day_ms)
            result = await session.execute(stmt)
            price_rows = result.scalars().all()

        out: dict[str, list[tuple[int, float]]] = {coin: [] for coin in coins}
        for r in price_rows:
            out[r.coin].append((r.day_ms, r.close))
        return out
