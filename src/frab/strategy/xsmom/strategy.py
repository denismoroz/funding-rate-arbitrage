"""XsmomStrategy — thin orchestrator for the XSMOM cross-sectional momentum strategy.

State machine is driven one step per XsmomPosition per tick.
NO in-memory accumulators: all state lives in XsmomRepo / Exchange / DB.

Phase C implements:
  - State machine (NEW → OPENED; CLOSE → CLOSED)
  - Funding accrual (hour tick)
  - Margin watchdog (hour tick)
  - manual_close / close_all helpers
  - reload_params fast path

Phase D adds:
  - History refresh + hourly scan (display-only, runs even when paused)
  - Weekly rebalance reconcile (KEEP/ADD/DROP/FLIP diff, active only)
  - manual_rebalance entrypoint (always reconciles, even when paused)
  - last_rebalance_ms stored in Strategy.params_json

``last_rebalance_ms`` storage
-------------------------------
Written to and read from Strategy.params_json["last_rebalance_ms"] via
session_scope on the StrategyRow.  This avoids a schema migration while
being durable (survives restarts).  The key is only set after a successful
reconcile.
"""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.db.models import Strategy as StrategyRow
from frab.db.session import session_scope
from frab.domain import XsmomPosition, XsmomState, XSMOM_ACTIVE_STATES
from frab.events.bus import EventBus
from frab.exchanges.protocol import Exchange
from frab.repo.xsmom_repo import XsmomRepo, XsmomStateConflict
from frab.settings import Settings
from frab.strategy.two_phase.states._helpers import publish_event
from frab.strategy.xsmom.actions.funding_accrual import XsmomFundingAccrual
from frab.strategy.xsmom.actions.history_refresh import XsmomHistoryRefresh
from frab.strategy.xsmom.actions.scan import XsmomScanAction
from frab.strategy.xsmom.evaluators.rebalance import XsmomRebalance, is_rebalance_due
from frab.strategy.xsmom.params import XsmomParams
from frab.strategy.xsmom.state_machine import StateMachine
from frab.strategy.xsmom.states import STATE_CLASSES, XsmomContext

logger = logging.getLogger(__name__)

# States in which _advance_one should stop stepping:
#   - OPENED  → resting (non-transient); rebalance is evaluated hourly in Phase D
#   - CLOSED  → terminal
#   - FAILED  → terminal
_NON_TRANSIENT_STATES = frozenset(
    XSMOM_ACTIVE_STATES | {XsmomState.CLOSED, XsmomState.FAILED}
)

_ADVANCE_MAX_ITERS = 20


class XsmomStrategy:
    """Stateless orchestrator that drives XsmomPositions through their lifecycle.

    The only instance state is the constructor arguments (ids, wired deps, params).
    All position / wallet state is fetched from Exchange / XsmomRepo on every call.

    NOTE on no-rollback design: XSMOM has only one perp leg (no composite spot+perp).
    On a generic Exception in _advance_one we mark_failed immediately — there is no
    partial composite to unwind (unlike FRAB which must unwind a spot leg if the
    perp fails). This is the deliberate simplification vs. TwoPhaseStrategy._rollback.
    """

    def __init__(
        self,
        *,
        strategy_id: int,
        exchange: Exchange,
        xsmom_repo: XsmomRepo,
        session_factory: async_sessionmaker[AsyncSession],
        params: XsmomParams,
        settings: Settings,
        event_bus: EventBus | None = None,
        margin_watchdog=None,  # XsmomMarginWatchdog | None (typed loosely to avoid cycle)
    ) -> None:
        self.strategy_id = strategy_id
        self.exchange = exchange
        self.xsmom_repo = xsmom_repo
        self._sf = session_factory
        self._settings = settings
        self._bus = event_bus
        self._margin_watchdog = margin_watchdog

        self.params = params
        self._build_internals(params)

    def _build_internals(self, params: XsmomParams) -> None:
        """Construct all params-dependent components and wire them onto self.

        Called from __init__ and reload_params. Does NOT touch strategy_id,
        exchange, xsmom_repo, _sf, _settings, _bus, or _margin_watchdog.
        """
        self._funding_accrual = XsmomFundingAccrual(
            strategy_id=self.strategy_id,
            exchange=self.exchange,
            xsmom_repo=self.xsmom_repo,
            session_factory=self._sf,
        )
        self._history_refresh = XsmomHistoryRefresh(
            exchange=self.exchange,
            xsmom_repo=self.xsmom_repo,
            params=params,
        )
        self._scan_action = XsmomScanAction(
            strategy_id=self.strategy_id,
            xsmom_repo=self.xsmom_repo,
            params=params,
        )
        self._rebalance = XsmomRebalance(
            strategy_id=self.strategy_id,
            exchange=self.exchange,
            xsmom_repo=self.xsmom_repo,
            params=params,
            session_factory=self._sf,
            event_bus=self._bus,
        )
        ctx = XsmomContext(
            exchange=self.exchange,
            xsmom_repo=self.xsmom_repo,
            params=params,
            session_factory=self._sf,
            settings=self._settings,
            event_bus=self._bus,
        )
        self._state_machine = StateMachine(
            {cls.state: cls(ctx) for cls in STATE_CLASSES}
        )

    def reload_params(self, new_params: XsmomParams) -> None:
        """Rebuild all params-dependent internals. Idempotent (structural equality fast path)."""
        if new_params == self.params:
            return  # XsmomParams is frozen dataclass so == is structural
        self.params = new_params
        self._build_internals(new_params)

    # ── Public entry points ───────────────────────────────────────────────────

    async def advance_all_pending(self) -> None:
        """For every XsmomPosition not in a terminal/resting state, take ONE step."""
        pending = await self.xsmom_repo.list_non_terminal(self.strategy_id)
        for fp in pending:
            await self._advance_one(fp)

    async def on_minute_tick(self, *, now_ms: int) -> None:
        """Minute tick: advance pending state machines only."""
        await self.advance_all_pending()

    async def on_hour_tick(self, *, now_ms: int) -> None:
        """Hourly: refresh history + scan (always); accrue funding; watchdog + rebalance (active only).

        Structure:
          1. Read strategy status (paused/active).
          2. Refresh daily candle history (both branches — display needs fresh data).
          3. Run scan and record an XsmomScan row (both branches — display-only, no trades).
          4. Accrue funding (both branches — keep existing Phase C behaviour).
          5. If paused → return.
          6. Run margin watchdog (active only).
          7. If rebalance is due → reconcile + persist last_rebalance_ms (active only).
        """
        # ── 1. Read status ────────────────────────────────────────────────────
        async with session_scope(self._sf) as session:
            strat_row = await session.get(StrategyRow, self.strategy_id)
            status = strat_row.status if strat_row is not None else "active"

        # ── 2 & 3. History refresh + scan (both paused and active) ───────────
        try:
            await self._history_refresh.refresh()
        except Exception:  # noqa: BLE001
            logger.exception("xsmom history_refresh crashed; continuing")

        scan_summary: dict = {}
        try:
            scan_summary = await self._scan_action.scan(now_ms=now_ms)
        except Exception:  # noqa: BLE001
            logger.exception("xsmom scan crashed; continuing")

        # ── 4. Accrue funding (both branches) ─────────────────────────────────
        await self._accrue_funding(now_ms=now_ms)

        # ── 5. Paused gate ────────────────────────────────────────────────────
        if status == "paused":
            logger.info("xsmom paused: scan+funding ran, skipping watchdog/rebalance strategy_id=%s", self.strategy_id)
            return

        # ── 6. Margin watchdog (active only) ─────────────────────────────────
        if self._margin_watchdog is not None:
            try:
                report = await self._margin_watchdog.run_check(now_ms=now_ms)
                if report.actions_taken:
                    logger.info("xsmom margin_watchdog actions: %s", report.actions_taken)
            except Exception:  # noqa: BLE001
                logger.exception("xsmom margin_watchdog crashed; skipping this tick")

        # ── 7. Rebalance (active only, when due) ─────────────────────────────
        last_rebalance_ms = await self._read_last_rebalance_ms()
        if is_rebalance_due(now_ms, last_rebalance_ms, self.params):
            scores = scan_summary.get("scores")
            try:
                await self._rebalance.reconcile(now_ms=now_ms, scores=scores)
                await self._persist_last_rebalance_ms(now_ms)
            except Exception:  # noqa: BLE001
                logger.exception("xsmom rebalance crashed; skipping this tick")

    async def manual_rebalance(self, *, now_ms: int) -> dict:
        """Force an immediate reconcile regardless of schedule or pause status.

        Manual button = explicit user intent → always reconciles (even when paused).
        History is refreshed first so scores are based on up-to-date data.
        Updates last_rebalance_ms on success.

        Returns the reconcile summary dict.
        """
        await self._history_refresh.refresh()
        scan_summary = await self._scan_action.scan(now_ms=now_ms)
        scores = scan_summary.get("scores")
        result = await self._rebalance.reconcile(now_ms=now_ms, scores=scores)
        await self._persist_last_rebalance_ms(now_ms)
        return result

    # ── last_rebalance_ms persistence (params_json) ───────────────────────────

    async def _read_last_rebalance_ms(self) -> int | None:
        """Read last_rebalance_ms from Strategy.params_json. Returns None if never set
        or if the stored value is missing/malformed (treated as 'never rebalanced')."""
        async with session_scope(self._sf) as session:
            row = await session.get(StrategyRow, self.strategy_id)
            if row is None or not isinstance(row.params_json, dict):
                return None
            val = row.params_json.get("last_rebalance_ms")
            return val if isinstance(val, int) else None

    async def _persist_last_rebalance_ms(self, now_ms: int) -> None:
        """Write last_rebalance_ms into Strategy.params_json and commit."""
        async with session_scope(self._sf) as session:
            row = await session.get(StrategyRow, self.strategy_id)
            if row is None:
                logger.warning("xsmom: cannot persist last_rebalance_ms — strategy row missing")
                return
            # params_json is a mutable JSON column; reassign to trigger SQLAlchemy dirty detection
            row.params_json = {**row.params_json, "last_rebalance_ms": now_ms}
        logger.info("xsmom: last_rebalance_ms=%d persisted strategy_id=%s", now_ms, self.strategy_id)

    # ── State machine ─────────────────────────────────────────────────────────

    async def _advance_one(self, fp: XsmomPosition) -> None:
        """Drive the state machine in a tight loop until a steady/terminal state.

        Mirrors TwoPhaseStrategy._advance_one EXACTLY (battle-tested pattern):
          - Each iteration dispatches the current state, then refetches from DB.
          - XsmomStateConflict → log warning + break (another process is touching it).
          - Generic Exception → mark_failed + break + publish xsmom.failed.
            NOTE: no rollback action here because XSMOM has a single perp leg;
            there is no partial composite to unwind (see class docstring).
          - repo.get returns None → log error + break (defensive).
          - 20 iterations without reaching terminal → safety cap + log error.
        """
        current = fp
        for _iteration in range(_ADVANCE_MAX_ITERS):
            if current.state in _NON_TRANSIENT_STATES:
                break

            try:
                await self._dispatch(current)
            except XsmomStateConflict as exc:
                logger.warning(
                    "xsmom state_conflict id=%s: %s — skipping tick",
                    current.id,
                    exc,
                )
                break
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "xsmom advance_one error id=%s state=%s: %s — marking failed",
                    current.id,
                    current.state.value,
                    exc,
                    exc_info=True,
                )
                await self.xsmom_repo.mark_failed(current.id, reason=str(exc))
                await publish_event(
                    self._bus,
                    level="ERROR",
                    kind="xsmom.failed",
                    message=f"{current.coin} FAILED at {current.state.value}: {exc}",
                    payload={
                        "xsmom_position_id": current.id,
                        "coin": current.coin,
                        "state": current.state.value,
                        "error": str(exc),
                    },
                )
                break

            # Refetch to see the new state written by the handler
            refreshed = await self.xsmom_repo.get(current.id)
            if refreshed is None:
                logger.error(
                    "xsmom advance_one: xsmom_repo.get returned None for id=%s after dispatch — aborting",
                    current.id,
                )
                break
            current = refreshed
        else:
            # Safety cap: loop exhausted without reaching a terminal/resting state
            logger.error(
                "xsmom advance_one safety cap hit id=%s state=%s — aborting burst",
                current.id,
                current.state.value,
            )

    async def _dispatch(self, fp: XsmomPosition) -> None:
        """Route to the registered state handler."""
        await self._state_machine.step(fp)

    async def _accrue_funding(self, *, now_ms: int) -> None:
        await self._funding_accrual.accrue(now_ms=now_ms)

    # ── Manual controls ───────────────────────────────────────────────────────

    async def manual_close(self, xsmom_position_id: int) -> XsmomPosition:
        """Transition a single OPENED position to CLOSE.

        Raises XsmomStateConflict if not in OPENED state.
        The minute-tick will drive it from CLOSE → CLOSED.
        """
        fp = await self.xsmom_repo.get(xsmom_position_id)
        if fp is None:
            raise KeyError(f"XsmomPosition {xsmom_position_id} not found")
        return await self.xsmom_repo.transition(
            xsmom_position_id,
            from_state=XsmomState.OPENED,
            to_state=XsmomState.CLOSE,
            state_data={**fp.state_data, "exit_decision": "manual_close"},
        )

    async def close_all(self) -> list[XsmomPosition]:
        """Transition all OPENED positions for this strategy to CLOSE.

        Skips any that raise XsmomStateConflict (already transitioning).
        Returns the list of successfully transitioned positions.
        """
        opened = await self.xsmom_repo.list_in_state(self.strategy_id, XsmomState.OPENED)
        closed: list[XsmomPosition] = []
        for fp in opened:
            try:
                result = await self.xsmom_repo.transition(
                    fp.id,
                    from_state=XsmomState.OPENED,
                    to_state=XsmomState.CLOSE,
                    state_data={**fp.state_data, "exit_decision": "close_all"},
                )
                closed.append(result)
            except XsmomStateConflict as exc:
                logger.warning("xsmom close_all: skipping id=%s: %s", fp.id, exc)
        return closed
