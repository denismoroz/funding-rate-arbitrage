"""TwoPhaseStrategy — thin orchestrator for two-phase dynamic funding-rate arb.

State machine is driven one step per FarbPosition per tick.
NO in-memory accumulators: all state lives in FarbRepo / Exchange / DB.

Params sourced from research/two_phase_dynamic_stability.py "Candidate C":
    coins:           ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
    K=3, entry_threshold=0.10 (annualized), signal_window=12h, base_min_hold=24h
    safety_mult=5.0, cap_min_hold=720h
    phase1_negative_patience=72, phase1_breakeven_cap_hours=720
    phase2_exit_threshold=-0.10
Signal math: two_phase_signals.decide_two_phase + compute_position_min_hold.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.db.models import Strategy as StrategyRow
from frab.db.session import session_scope
from frab.domain import FarbPosition, FarbState
from frab.events.bus import EventBus
from frab.exchanges.protocol import Exchange
from frab.repo.farb_repo import FarbRepo, StateConflict
import frab.strategy.two_phase as _pkg  # logger looked up at call time so patch.object works
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.states._helpers import publish_event
from frab.strategy.two_phase.state_machine import StateMachine
from frab.strategy.two_phase.states.check_margin import CheckMarginState
from frab.strategy.two_phase.states.closing_long import ClosingLongState
from frab.strategy.two_phase.states.closing_short import ClosingShortState
from frab.strategy.two_phase.states.opening_long import OpeningLongState
from frab.strategy.two_phase.states.opening_margin import OpeningMarginState
from frab.strategy.two_phase.states.opening_short import OpeningShortState
from frab.strategy.two_phase.states.releasing_margin import ReleasingMarginState
from frab.strategy.two_phase.evaluators.signal import SignalComputer
from frab.strategy.two_phase.evaluators.entry import EntryEvaluator
from frab.strategy.two_phase.evaluators.exit import ExitEvaluator
from frab.strategy.two_phase.actions.funding_accrual import FundingAccrual
from frab.strategy.two_phase.actions.rollback import RollbackAction


# ─── Strategy ────────────────────────────────────────────────────────────────

class TwoPhaseStrategy:
    """Stateless orchestrator that drives FarbPositions through their lifecycle.

    The only instance state is the constructor arguments (ids, wired deps, params).
    All position / wallet state is fetched from Exchange / FarbRepo on every call.
    """

    def __init__(
        self,
        *,
        strategy_id: int,
        exchange: Exchange,
        farb_repo: FarbRepo,
        session_factory: async_sessionmaker[AsyncSession],
        params: TwoPhaseParams,
        event_bus: EventBus | None = None,
    ) -> None:
        self.strategy_id = strategy_id
        self.exchange = exchange
        self.farb_repo = farb_repo
        self._sf = session_factory
        self.params = params
        self._bus = event_bus
        # Set by the force-tick API to bypass the same-hour entry cooldown on a
        # single hour_tick invocation. The API resets it after _hour_tick returns.
        self.force_entry_cooldown_bypass = False

        signal_computer = SignalComputer(
            exchange_name=exchange.name,
            session_factory=session_factory,
            signal_window_hours=params.signal_window_hours,
        )
        self._entry_evaluator = EntryEvaluator(
            strategy_id=strategy_id,
            farb_repo=farb_repo,
            params=params,
            signal_computer=signal_computer,
        )
        self._exit_evaluator = ExitEvaluator(
            strategy_id=strategy_id,
            farb_repo=farb_repo,
            params=params,
            signal_computer=signal_computer,
        )
        self._funding_accrual = FundingAccrual(
            strategy_id=strategy_id,
            exchange=exchange,
            farb_repo=farb_repo,
            session_factory=session_factory,
            signal_computer=signal_computer,
        )
        self._rollback_action = RollbackAction(
            exchange=exchange,
            session_factory=session_factory,
            params=params,
        )
        self._state_machine = StateMachine({
            FarbState.CHECK_MARGIN: CheckMarginState(
                exchange=exchange,
                farb_repo=farb_repo,
                params=params,
                event_bus=event_bus,
            ),
            FarbState.OPENING_MARGIN: OpeningMarginState(
                exchange=exchange,
                farb_repo=farb_repo,
                params=params,
            ),
            FarbState.OPENING_LONG: OpeningLongState(
                exchange=exchange,
                farb_repo=farb_repo,
                params=params,
            ),
            FarbState.OPENING_SHORT: OpeningShortState(
                exchange=exchange,
                farb_repo=farb_repo,
                params=params,
                event_bus=event_bus,
            ),
            FarbState.CLOSING_SHORT: ClosingShortState(
                exchange=exchange,
                farb_repo=farb_repo,
                session_factory=session_factory,
            ),
            FarbState.CLOSING_LONG: ClosingLongState(
                exchange=exchange,
                farb_repo=farb_repo,
                session_factory=session_factory,
                event_bus=event_bus,
            ),
            FarbState.RELEASING_MARGIN: ReleasingMarginState(
                exchange=exchange,
                farb_repo=farb_repo,
                session_factory=session_factory,
            ),
        })

    # ── Public entry points ───────────────────────────────────────────────────

    async def advance_all_pending(self) -> None:
        """For every FarbPosition not in steady state, take ONE state-machine step."""
        pending = await self.farb_repo.list_active(self.strategy_id)
        for fp in pending:
            await self._advance_one(fp)

    async def on_hour_tick(self, *, now_ms: int) -> None:
        """Hourly: accrue funding on open positions, evaluate exits, then entries."""
        # Read fresh status from DB (params can be edited without restart).
        async with session_scope(self._sf) as session:
            strat_row = await session.get(StrategyRow, self.strategy_id)
            status = strat_row.status if strat_row is not None else "active"

        if status == "paused":
            _pkg.logger.info("paused: skipping exits/entries strategy_id=%s", self.strategy_id)
            await self._accrue_funding(now_ms=now_ms)
            return

        await self._accrue_funding(now_ms=now_ms)
        await self._evaluate_exits(now_ms=now_ms)
        await self._evaluate_entries(now_ms=now_ms)

    async def on_minute_tick(self, *, now_ms: int) -> None:
        """Minute tick: advance pending state machines only."""
        await self.advance_all_pending()

    # ── State machine ─────────────────────────────────────────────────────────

    _STEADY_STATES = frozenset({FarbState.OPEN, FarbState.CLOSED, FarbState.FAILED})
    _ADVANCE_MAX_ITERS = 20

    async def _advance_one(self, fp: FarbPosition) -> None:
        """Drive the state machine in a tight loop until a steady/terminal state.

        Each iteration dispatches the current state, then refetches the FarbPosition
        from DB (because each handler does its own atomic transition).  Stops when:
          - current.state is OPEN / CLOSED / FAILED (steady/terminal)
          - StateConflict — another process is touching this FP; log + break
          - generic Exception — rollback + mark_failed + break
          - farb_repo.get returns None (defensive; log error + break)
          - 20 iterations reached without a terminal state (safety cap; log error)
        """
        current = fp
        for iteration in range(self._ADVANCE_MAX_ITERS):
            if current.state in self._STEADY_STATES:
                break

            try:
                await self._dispatch(current)
            except StateConflict as exc:
                _pkg.logger.warning(
                    "state_conflict farb_position_id=%s: %s — skipping tick",
                    current.id,
                    exc,
                )
                break
            except Exception as exc:  # noqa: BLE001
                _pkg.logger.error(
                    "advance_one error farb_position_id=%s state=%s: %s — rolling back",
                    current.id,
                    current.state.value,
                    exc,
                    exc_info=True,
                )
                await self._rollback(current, partial_state=current.state, error=exc)
                await self.farb_repo.mark_failed(current.id, reason=str(exc))
                await publish_event(
                    self._bus,
                    level="ERROR",
                    kind="farb.failed",
                    message=f"{current.coin} FAILED at {current.state.value}: {exc}",
                    payload={
                        "farb_position_id": current.id,
                        "coin": current.coin,
                        "state": current.state.value,
                        "error": str(exc),
                    },
                )
                break

            # Refetch to see the new state written by the handler
            refreshed = await self.farb_repo.get(current.id)
            if refreshed is None:
                _pkg.logger.error(
                    "advance_one: farb_repo.get returned None for farb_position_id=%s after dispatch — aborting",
                    current.id,
                )
                break
            current = refreshed
        else:
            # Safety cap: loop exhausted without reaching a terminal state
            _pkg.logger.error(
                "advance_one safety cap hit farb_position_id=%s state=%s — aborting burst",
                current.id,
                current.state.value,
            )

    async def _dispatch(self, fp: FarbPosition) -> None:
        """Route to the registered state handler. Steady/terminal states have no
        handler registered → StateMachine.step returns None (no-op)."""
        await self._state_machine.step(fp)

    # ── Thin delegates (required for test compat) ─────────────────────────────

    async def _evaluate_entries(self, *, now_ms: int) -> None:
        await self._entry_evaluator.evaluate(
            now_ms=now_ms,
            force_cooldown_bypass=self.force_entry_cooldown_bypass,
        )

    async def _evaluate_exits(self, *, now_ms: int) -> None:
        await self._exit_evaluator.evaluate(now_ms=now_ms)

    async def _accrue_funding(self, *, now_ms: int) -> None:
        await self._funding_accrual.accrue(now_ms=now_ms)

    async def _rollback(self, fp: FarbPosition, *, partial_state: FarbState, error: Exception) -> None:
        await self._rollback_action.execute(fp, partial_state=partial_state, error=error)
