"""FarbRepo — thin DAO for farb_positions rows.

No business logic. Only persistence and atomic state transitions.
Strategy code (Step 7) calls FarbRepo; Ledger (Step 6) does NOT.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.domain import FarbPosition, FarbState, Instrument
from frab.domain import ACTIVE_STATES
from frab.db.models import FarbPosition as FarbPositionRow
from frab.db.session import session_scope


class StateConflict(Exception):
    """Atomic transition failed: row state was not the expected `from_state`."""

    def __init__(
        self,
        farb_position_id: int,
        expected: FarbState | str,
        actual: FarbState | None,
    ) -> None:
        expected_str = expected.value if isinstance(expected, FarbState) else expected
        actual_str = actual.value if actual is not None else "MISSING"
        super().__init__(
            f"FarbPosition {farb_position_id}: expected state={expected_str}, actual={actual_str}"
        )
        self.farb_position_id = farb_position_id
        self.expected = expected
        self.actual = actual


# ── Datetime helpers ──────────────────────────────────────────────────────────

def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


# ── ORM → domain mapper ───────────────────────────────────────────────────────

def _to_domain(row: FarbPositionRow) -> FarbPosition:
    return FarbPosition(
        id=row.id,
        strategy_id=row.strategy_id,
        coin=row.coin,
        state=FarbState(row.state),
        state_data=row.state_data if row.state_data is not None else {},
        spot_position_id=row.spot_position_id,
        perp_position_id=row.perp_position_id,
        margin_position_id=row.margin_position_id,
        opened_at=_ms_to_dt(row.opened_at),
        closed_at=_ms_to_dt(row.closed_at) if row.closed_at is not None else None,
    )


# ── Terminal states ───────────────────────────────────────────────────────────

_TERMINAL_STATES = {FarbState.CLOSED, FarbState.FAILED}


# ── FarbRepo ──────────────────────────────────────────────────────────────────

class FarbRepo:
    """Data-access object for farb_positions rows.

    Each public method opens its own session, commits, and closes it.
    No long-lived sessions are held.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # ── CRUD ──────────────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        strategy_id: int,
        coin: str,
        initial_state: FarbState = FarbState.CHECK_MARGIN,
        state_data: dict | None = None,
    ) -> FarbPosition:
        """Insert new farb_positions row. Returns domain FarbPosition with id populated."""
        now_ms = _now_ms()
        row = FarbPositionRow(
            strategy_id=strategy_id,
            coin=coin,
            state=initial_state,
            state_data=state_data if state_data is not None else {},
            spot_position_id=None,
            perp_position_id=None,
            margin_position_id=None,
            opened_at=now_ms,
            closed_at=None,
        )
        async with session_scope(self._sf) as session:
            session.add(row)
            await session.flush()
            # flush assigns the PK; expire_on_commit=False keeps it accessible after commit
            domain = _to_domain(row)
        return domain

    async def get(self, farb_position_id: int) -> FarbPosition | None:
        """Return domain FarbPosition by id, or None if not found."""
        async with session_scope(self._sf) as session:
            row = await session.get(FarbPositionRow, farb_position_id)
            if row is None:
                return None
            return _to_domain(row)

    async def list_non_terminal(self, strategy_id: int) -> list[FarbPosition]:
        """All farb_positions for strategy where state NOT IN (CLOSED, FAILED).

        Returns all transient (opening/closing) and resting (PRE/POST_BREAKEVEN) positions.
        Used at startup for recovery of in-progress positions and by the minute-tick advance loop.
        """
        terminal_values = [s.value for s in _TERMINAL_STATES]
        async with session_scope(self._sf) as session:
            result = await session.execute(
                select(FarbPositionRow).where(
                    FarbPositionRow.strategy_id == strategy_id,
                    FarbPositionRow.state.not_in(terminal_values),
                )
            )
            rows = result.scalars().all()
            return [_to_domain(r) for r in rows]

    async def list_active(self, strategy_id: int) -> list[FarbPosition]:
        """Return all farb_positions in an actively-holding state (PRE_BREAKEVEN or POST_BREAKEVEN).

        Used by the hourly ExitEvaluator to find positions to evaluate for exit,
        and by funding accrual and margin watchdog.
        """
        active_values = [s.value for s in ACTIVE_STATES]
        async with session_scope(self._sf) as session:
            result = await session.execute(
                select(FarbPositionRow).where(
                    FarbPositionRow.strategy_id == strategy_id,
                    FarbPositionRow.state.in_(active_values),
                )
            )
            rows = result.scalars().all()
            return [_to_domain(r) for r in rows]

    async def list_in_state(
        self, strategy_id: int, state: FarbState
    ) -> list[FarbPosition]:
        """Return all farb_positions in the given state for the strategy."""
        async with session_scope(self._sf) as session:
            result = await session.execute(
                select(FarbPositionRow).where(
                    FarbPositionRow.strategy_id == strategy_id,
                    FarbPositionRow.state == state.value,
                )
            )
            rows = result.scalars().all()
            return [_to_domain(r) for r in rows]

    async def list_by_coin(
        self,
        strategy_id: int,
        coin: str,
        include_terminal: bool = False,
    ) -> list[FarbPosition]:
        """Return farb_positions for a specific coin.

        By default excludes terminal states (CLOSED, FAILED).
        Pass include_terminal=True to include all rows.
        """
        async with session_scope(self._sf) as session:
            stmt = select(FarbPositionRow).where(
                FarbPositionRow.strategy_id == strategy_id,
                FarbPositionRow.coin == coin,
            )
            if not include_terminal:
                terminal_values = [s.value for s in _TERMINAL_STATES]
                stmt = stmt.where(FarbPositionRow.state.not_in(terminal_values))
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [_to_domain(r) for r in rows]

    # ── Atomic state transitions ──────────────────────────────────────────────

    async def transition(
        self,
        farb_position_id: int,
        *,
        from_state: FarbState,
        to_state: FarbState,
        state_data: dict | None = None,
    ) -> FarbPosition:
        """Atomic UPDATE: SET state=to_state WHERE id=? AND state=from_state.

        Raises StateConflict if rowcount == 0 (wrong state or missing row).
        Returns the updated domain FarbPosition.
        """
        async with session_scope(self._sf) as session:
            values: dict = {"state": to_state.value}
            if state_data is not None:
                values["state_data"] = state_data

            result = await session.execute(
                update(FarbPositionRow)
                .where(
                    FarbPositionRow.id == farb_position_id,
                    FarbPositionRow.state == from_state.value,
                )
                .values(**values)
                .returning(FarbPositionRow)
            )
            updated_row = result.scalar_one_or_none()

            if updated_row is None:
                # Determine actual state for the error message
                current = await session.get(FarbPositionRow, farb_position_id)
                actual: FarbState | None = (
                    FarbState(current.state) if current is not None else None
                )
                raise StateConflict(farb_position_id, from_state, actual)

            return _to_domain(updated_row)

    async def set_leg(
        self,
        farb_position_id: int,
        *,
        instrument: Instrument,
        position_id: int,
    ) -> FarbPosition:
        """Set spot_position_id / perp_position_id / margin_position_id based on instrument.

        NOT atomic with respect to state — does not check or change state.
        """
        column_map = {
            Instrument.SPOT: "spot_position_id",
            Instrument.PERP: "perp_position_id",
            Instrument.COLLATERAL: "margin_position_id",
        }
        column = column_map[instrument]

        async with session_scope(self._sf) as session:
            result = await session.execute(
                update(FarbPositionRow)
                .where(FarbPositionRow.id == farb_position_id)
                .values(**{column: position_id})
                .returning(FarbPositionRow)
            )
            updated_row = result.scalar_one_or_none()
            if updated_row is None:
                raise KeyError(f"FarbPosition {farb_position_id} not found")
            return _to_domain(updated_row)

    async def update_state_data(
        self,
        farb_position_id: int,
        state_data: dict,
    ) -> FarbPosition:
        """Replace state_data wholesale. State is unchanged.

        Use when strategy needs to checkpoint counters etc. without a state change.
        """
        async with session_scope(self._sf) as session:
            result = await session.execute(
                update(FarbPositionRow)
                .where(FarbPositionRow.id == farb_position_id)
                .values(state_data=state_data)
                .returning(FarbPositionRow)
            )
            updated_row = result.scalar_one_or_none()
            if updated_row is None:
                raise KeyError(f"FarbPosition {farb_position_id} not found")
            return _to_domain(updated_row)

    async def mark_closed(self, farb_position_id: int) -> FarbPosition:
        """Atomic transition to CLOSED + set closed_at=now.

        Caller must have already moved through RELEASING_MARGIN before calling this.
        Raises StateConflict if the current state is already CLOSED or FAILED.
        """
        now_ms = _now_ms()
        async with session_scope(self._sf) as session:
            # Allow any non-terminal non-FAILED state to close
            # (caller is responsible for proper sequencing)
            result = await session.execute(
                update(FarbPositionRow)
                .where(
                    FarbPositionRow.id == farb_position_id,
                    FarbPositionRow.state != FarbState.CLOSED.value,
                    FarbPositionRow.state != FarbState.FAILED.value,
                )
                .values(state=FarbState.CLOSED.value, closed_at=now_ms)
                .returning(FarbPositionRow)
            )
            updated_row = result.scalar_one_or_none()
            if updated_row is None:
                current = await session.get(FarbPositionRow, farb_position_id)
                actual = FarbState(current.state) if current is not None else None
                raise StateConflict(farb_position_id, "non-terminal", actual)
            return _to_domain(updated_row)

    async def mark_failed(
        self,
        farb_position_id: int,
        reason: str,
    ) -> FarbPosition:
        """Move to FAILED from any non-terminal state.

        Sets state_data['failure_reason']=reason and closed_at=now.
        """
        now_ms = _now_ms()
        async with session_scope(self._sf) as session:
            # Read current state_data first so we can merge failure_reason in
            current = await session.get(FarbPositionRow, farb_position_id)
            if current is None:
                raise KeyError(f"FarbPosition {farb_position_id} not found")

            existing_state_data: dict = (
                current.state_data if current.state_data is not None else {}
            )
            new_state_data = {**existing_state_data, "failure_reason": reason}

            result = await session.execute(
                update(FarbPositionRow)
                .where(
                    FarbPositionRow.id == farb_position_id,
                    FarbPositionRow.state != FarbState.CLOSED.value,
                    FarbPositionRow.state != FarbState.FAILED.value,
                )
                .values(
                    state=FarbState.FAILED.value,
                    state_data=new_state_data,
                    closed_at=now_ms,
                )
                .returning(FarbPositionRow)
            )
            updated_row = result.scalar_one_or_none()
            if updated_row is None:
                raise StateConflict(farb_position_id, "non-terminal", FarbState(current.state))
            return _to_domain(updated_row)
