"""TwoPhaseStrategy — thin orchestrator for two-phase dynamic funding-rate arb.

State machine is driven one step per FarbPosition per tick.
NO in-memory accumulators: all state lives in FarbRepo / Exchange / DB.

Params sourced from research/two_phase_dynamic_stability.py "Candidate C":
    coins:           ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
    K=3, entry_threshold=0.10 (annualized), signal_window=12h, base_min_hold=24h
    safety_mult=5.0, cap_min_hold=720h
    phase1_negative_patience=72, phase1_breakeven_cap_hours=720
    phase2_exit_threshold=-0.10
Signal math: two_phase_signals.decide_entry / decide_pre_breakeven / decide_post_breakeven + compute_position_min_hold.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.db.models import Strategy as StrategyRow
from frab.db.session import session_scope
from frab.domain import FarbPosition, FarbState, ACTIVE_STATES
from frab.events.bus import EventBus
from frab.exchanges.protocol import Exchange
from frab.repo.farb_repo import FarbRepo, StateConflict
from frab.settings import Settings
import frab.strategy.two_phase as _pkg  # logger looked up at call time so patch.object works
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.states._helpers import publish_event
from frab.strategy.two_phase.state_machine import StateMachine
from frab.strategy.two_phase.states import STATE_CLASSES, StrategyContext
from frab.strategy.two_phase.evaluators.signal import SignalComputer
from frab.strategy.two_phase.evaluators.entry import EntryEvaluator
from frab.strategy.two_phase.evaluators.exit import ExitEvaluator
from frab.strategy.two_phase.actions.funding_accrual import FundingAccrual
from frab.strategy.two_phase.actions.rollback import RollbackAction
from frab.engine.margin_watchdog import MarginWatchdog


# ─── Manual-open exceptions ───────────────────────────────────────────────────

class ManualOpenError(Exception):
    """Base class for manual-open rejections."""


class ManualOpenCoinNotInUniverse(ManualOpenError): pass
class ManualOpenAlreadyExists(ManualOpenError): pass
class ManualOpenConcurrencyCapReached(ManualOpenError): pass
class ManualOpenBudgetCapReached(ManualOpenError): pass
class ManualOpenSignalUnavailable(ManualOpenError): pass


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
        settings: Settings,
        event_bus: EventBus | None = None,
        margin_watchdog: MarginWatchdog | None = None,
    ) -> None:
        self.strategy_id = strategy_id
        self.exchange = exchange
        self.farb_repo = farb_repo
        self._sf = session_factory
        self._settings = settings
        self._bus = event_bus
        self._margin_watchdog = margin_watchdog
        # Set by the force-tick API to bypass the same-hour entry cooldown on a
        # single hour_tick invocation. The API resets it after _hour_tick returns.
        self.force_entry_cooldown_bypass = False

        self.params = params
        self._build_internals(params)

    def _build_internals(self, params: TwoPhaseParams) -> None:
        """Construct all params-dependent components and wire them onto self.

        Called from __init__ and reload_params. Does NOT touch strategy_id,
        exchange, farb_repo, _sf, _settings, _bus, _margin_watchdog, or
        force_entry_cooldown_bypass — those are params-independent.
        """
        signal_computer = SignalComputer(
            exchange_name=self.exchange.name,
            session_factory=self._sf,
            signal_window_hours=params.signal_window_hours,
        )
        self._signal_computer = signal_computer
        self._entry_evaluator = EntryEvaluator(
            strategy_id=self.strategy_id,
            farb_repo=self.farb_repo,
            params=params,
            signal_computer=signal_computer,
        )
        self._exit_evaluator = ExitEvaluator(
            strategy_id=self.strategy_id,
            farb_repo=self.farb_repo,
            params=params,
            signal_computer=signal_computer,
            settings=self._settings,
        )
        self._funding_accrual = FundingAccrual(
            strategy_id=self.strategy_id,
            exchange=self.exchange,
            farb_repo=self.farb_repo,
            session_factory=self._sf,
            signal_computer=signal_computer,
        )
        self._rollback_action = RollbackAction(
            exchange=self.exchange,
            session_factory=self._sf,
            params=params,
        )
        ctx = StrategyContext(
            exchange=self.exchange,
            farb_repo=self.farb_repo,
            params=params,
            session_factory=self._sf,
            settings=self._settings,
            event_bus=self._bus,
        )
        self._state_machine = StateMachine({cls.state: cls(ctx) for cls in STATE_CLASSES})

    def reload_params(self, new_params: TwoPhaseParams) -> None:
        """Rebuild all params-dependent internals with new_params. Idempotent."""
        if new_params == self.params:
            return  # no-op fast path; TwoPhaseParams is frozen dataclass so == is structural
        self.params = new_params
        self._build_internals(new_params)

    # ── Public entry points ───────────────────────────────────────────────────

    async def advance_all_pending(self) -> None:
        """For every FarbPosition not in a terminal state, take ONE state-machine step."""
        pending = await self.farb_repo.list_non_terminal(self.strategy_id)
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
        if self._margin_watchdog is not None:
            try:
                report = await self._margin_watchdog.run_check(now_ms=now_ms)
                if report.actions_taken:
                    _pkg.logger.info("margin_watchdog actions: %s", report.actions_taken)
            except Exception:
                _pkg.logger.exception("margin_watchdog crashed; skipping this tick")
        await self._evaluate_exits(now_ms=now_ms)
        await self._evaluate_entries(now_ms=now_ms)

    async def on_minute_tick(self, *, now_ms: int) -> None:
        """Minute tick: advance pending state machines only."""
        await self.advance_all_pending()

    # ── State machine ─────────────────────────────────────────────────────────

    # States in which _advance_one should stop stepping: either terminal (CLOSED/FAILED)
    # or resting (PRE_BREAKEVEN/POST_BREAKEVEN) — these are evaluated hourly, not per-tick.
    _NON_TRANSIENT_STATES = frozenset(ACTIVE_STATES | {FarbState.CLOSED, FarbState.FAILED})
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
            if current.state in self._NON_TRANSIENT_STATES:
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

    # ── Test seams ─────────────────────────────────────────────────────────────

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

    # ── Manual open ───────────────────────────────────────────────────────────

    async def manual_open(self, *, coin: str, now_ms: int) -> FarbPosition:
        """Create a FarbPosition for `coin` bypassing entry_threshold.

        Validates: coin in universe, no existing non-terminal FP for coin,
        concurrency cap, budget cap, signal computable. Returns the created
        FarbPosition; the engine's minute-tick will drive it to OPEN.
        """
        p = self.params
        if coin not in p.coins:
            raise ManualOpenCoinNotInUniverse(coin)

        # list_by_coin(include_terminal=False) excludes CLOSED/FAILED, so it covers
        # all transient + resting (PRE/POST) states — sufficient for coin uniqueness check.
        existing = await self.farb_repo.list_by_coin(
            self.strategy_id, coin, include_terminal=False
        )
        if existing:
            raise ManualOpenAlreadyExists(coin)

        non_terminal = await self.farb_repo.list_non_terminal(self.strategy_id)
        non_terminal_count = len(non_terminal)
        if non_terminal_count >= p.concurrency_cap:
            raise ManualOpenConcurrencyCapReached(
                f"{non_terminal_count}/{p.concurrency_cap}"
            )

        footprint = p.compute_footprint()
        committed_usdc = non_terminal_count * footprint
        if committed_usdc + footprint > p.budget_cap_usdc:
            raise ManualOpenBudgetCapReached(
                f"committed={committed_usdc:.2f} + footprint={footprint:.2f} > cap={p.budget_cap_usdc:.2f}"
            )

        signal = await self._signal_computer.compute(coin)
        if signal is None:
            raise ManualOpenSignalUnavailable(coin)

        fp = await self.farb_repo.create(
            strategy_id=self.strategy_id,
            coin=coin,
            initial_state=FarbState.CHECK_MARGIN,
            state_data={
                "target_signal_apr": signal,
                "entry_ts_ms": now_ms,
                "manual_open": True,
            },
        )
        await publish_event(
            self._bus,
            level="INFO",
            kind="farb.manual_open",
            message=f"{coin} manual open requested (signal={signal:.4f} APR)",
            payload={"farb_position_id": fp.id, "coin": coin, "signal_apr": signal},
        )
        _pkg.logger.info(
            "manual_open: coin=%s signal_apr=%.4f fp_id=%s", coin, signal, fp.id
        )
        return fp
