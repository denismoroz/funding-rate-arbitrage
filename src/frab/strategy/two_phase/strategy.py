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

from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.db.models import Exchange as ExchangeRow
from frab.db.models import FundingRate as FundingRateRow
from frab.db.models import Strategy as StrategyRow
from frab.db.session import session_scope
from frab.domain import FarbPosition, FarbState
from frab.engine.two_phase_signals import (
    TwoPhaseDecision,
    decide_two_phase,
    update_consec_negative,
)
from frab.events.bus import Event, EventBus
from frab.exchanges.protocol import Exchange, WalletKind
from frab.repo.farb_repo import FarbRepo, StateConflict
import frab.strategy.two_phase as _pkg  # logger looked up at call time so patch.object works
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.states._helpers import load_position
from frab.strategy.two_phase.state_machine import StateMachine
from frab.strategy.two_phase.states.check_margin import CheckMarginState
from frab.strategy.two_phase.states.closing_long import ClosingLongState
from frab.strategy.two_phase.states.closing_short import ClosingShortState
from frab.strategy.two_phase.states.opening_long import OpeningLongState
from frab.strategy.two_phase.states.opening_margin import OpeningMarginState
from frab.strategy.two_phase.states.opening_short import OpeningShortState
from frab.strategy.two_phase.states.releasing_margin import ReleasingMarginState

# HL hourly funding intervals per year
_HOURS_PER_YEAR = 8760


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

    async def _publish(self, *, level: str, kind: str, message: str, payload: dict | None = None) -> None:
        if self._bus is None:
            return
        await self._bus.publish(Event(
            ts=datetime.now(timezone.utc),
            level=level,
            source="strategy",
            kind=kind,
            message=message,
            payload_json=payload,
        ))

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
                await self._publish(
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

    # ── Signal computation ────────────────────────────────────────────────────

    async def _compute_signal(self, coin: str) -> float | None:
        """Fetch last signal_window_hours funding rates from DB and compute smoothed signal."""
        window = self.params.signal_window_hours
        async with session_scope(self._sf) as session:
            # Look up exchange id
            result = await session.execute(
                select(ExchangeRow).where(ExchangeRow.name == self.exchange.name)
            )
            exc_row = result.scalar_one_or_none()
            if exc_row is None:
                return None
            intervals_per_year = _HOURS_PER_YEAR // exc_row.funding_interval_h

            # Fetch recent rates
            rates_result = await session.execute(
                select(FundingRateRow.rate)
                .where(
                    FundingRateRow.exchange_id == exc_row.id,
                    FundingRateRow.coin == coin,
                )
                .order_by(desc(FundingRateRow.ts_ms))
                .limit(window)
            )
            rates = [r for (r,) in rates_result.all()]

        if len(rates) < window:
            return None
        # Rates come newest-first from ORDER BY DESC; mean is order-independent
        mean_rate = sum(rates) / len(rates)
        return mean_rate * intervals_per_year

    async def _latest_funding_rate(self, coin: str) -> float | None:
        """Most recent funding_rates.rate for the coin on this exchange."""
        async with session_scope(self._sf) as session:
            exc_row = (await session.execute(
                select(ExchangeRow).where(ExchangeRow.name == self.exchange.name)
            )).scalar_one_or_none()
            if exc_row is None:
                return None
            row = (await session.execute(
                select(FundingRateRow.rate)
                .where(
                    FundingRateRow.exchange_id == exc_row.id,
                    FundingRateRow.coin == coin,
                )
                .order_by(desc(FundingRateRow.ts_ms))
                .limit(1)
            )).first()
        return float(row[0]) if row is not None else None

    # ── Funding accrual ───────────────────────────────────────────────────────

    async def _accrue_funding(self, *, now_ms: int) -> None:
        """For each OPEN FP, refresh funding accruals from HL's authoritative
        userFunding endpoint via Exchange.get_accrued_funding. That helper is
        already idempotent (dedupes by (position_id, ts_ms) before insert) and
        returns the cumulative DB sum. We just mirror that sum into state_data
        and refresh the cached current smoothed signal.
        """
        open_fps = await self.farb_repo.list_open(self.strategy_id)
        for fp in open_fps:
            if fp.perp_position_id is None:
                continue
            perp_pos = await load_position(self._sf,fp.perp_position_id)
            try:
                gross = await self.exchange.get_accrued_funding(perp_pos)
            except Exception as exc:  # noqa: BLE001
                _pkg.logger.warning(
                    "accrue_funding: get_accrued_funding failed fp=%s coin=%s: %s",
                    fp.id, fp.coin, exc,
                )
                continue

            sd = dict(fp.state_data)
            sd["gross_funding_so_far"] = float(gross)
            smoothed = await self._compute_signal(fp.coin)
            if smoothed is not None:
                sd["current_signal_apr"] = smoothed
            await self.farb_repo.update_state_data(fp.id, sd)

            _pkg.logger.info(
                "funding accrued fp=%s coin=%s gross_from_HL=%.6f",
                fp.id, fp.coin, gross,
            )

    # ── Entry decision ────────────────────────────────────────────────────────

    async def _evaluate_entries(self, *, now_ms: int) -> None:
        """For each coin: compute signal, check concurrency cap, create new arbs."""
        p = self.params

        # Count non-terminal positions (includes OPEN + in-flight)
        all_active = await self.farb_repo.list_active(self.strategy_id)
        open_fps = await self.farb_repo.list_open(self.strategy_id)
        non_terminal_count = len(all_active) + len(open_fps)

        slots = p.concurrency_cap - non_terminal_count
        if slots <= 0:
            return

        # Budget cap: further constrain slots by available committed capital
        footprint = p.per_position_footprint()
        committed_usdc = non_terminal_count * footprint
        remaining_budget = p.budget_cap_usdc - committed_usdc
        slots_by_budget = int(remaining_budget // footprint) if footprint > 0 else 0
        if slots_by_budget <= 0:
            _pkg.logger.info(
                "budget_cap blocks new entries: committed=%.2f cap=%.2f remaining=%.2f",
                committed_usdc,
                p.budget_cap_usdc,
                remaining_budget,
            )
            return
        slots = min(slots, slots_by_budget)

        # Evaluate each coin for entry
        candidates: list[tuple[str, float]] = []
        current_hour = now_ms // 3_600_000
        for coin in p.coins:
            # Skip if already has a non-terminal position
            existing = await self.farb_repo.list_by_coin(
                self.strategy_id, coin, include_terminal=False
            )
            # list_by_coin with include_terminal=False excludes OPEN/CLOSED/FAILED
            # but we also want to skip if there's an OPEN position
            open_for_coin = [fp for fp in open_fps if fp.coin == coin]
            if existing or open_for_coin:
                continue

            # Cooldown: if a FP for this coin failed in the current hour, wait
            # for the next hour-tick before retrying (avoids tight failure loop).
            all_for_coin = await self.farb_repo.list_by_coin(
                self.strategy_id, coin, include_terminal=True
            )
            last_failed_ms = max(
                (int(fp.closed_at.timestamp() * 1000)
                 for fp in all_for_coin
                 if fp.state == FarbState.FAILED and fp.closed_at is not None),
                default=None,
            )
            if (
                last_failed_ms is not None
                and last_failed_ms // 3_600_000 == current_hour
                and not self.force_entry_cooldown_bypass
            ):
                _pkg.logger.info(
                    "entry cooldown: coin=%s last_failed_at_ms=%d this_hour=%d, skip",
                    coin, last_failed_ms, current_hour,
                )
                continue

            signal = await self._compute_signal(coin)
            if signal is None:
                continue

            decision = decide_two_phase(
                in_position=False,
                smoothed_signal_annual=signal,
                entry_threshold=p.entry_threshold_apr,
                # Below fields irrelevant when not in_position:
                hours_in_position=0,
                position_min_hold_hours=0,
                gross_funding_so_far=0.0,
                total_fees_paid=0.0,
                consec_negative_hours=0,
                current_hourly_income_quote=0.0,
                phase1_negative_patience=p.phase1_negative_patience,
                phase1_breakeven_cap_hours=p.phase1_breakeven_cap_hours,
                phase2_exit_threshold=p.phase2_exit_threshold,
            )
            if decision == TwoPhaseDecision.OPEN:
                candidates.append((coin, signal))

        # Sort by signal strength descending, pick top `slots`
        candidates.sort(key=lambda x: -x[1])
        for coin, signal in candidates[:slots]:
            await self.farb_repo.create(
                strategy_id=self.strategy_id,
                coin=coin,
                initial_state=FarbState.CHECK_MARGIN,
                state_data={
                    "target_signal_apr": signal,
                    "entry_ts_ms": now_ms,
                },
            )
            _pkg.logger.info(
                "entry candidate farb created coin=%s signal_apr=%.4f", coin, signal
            )

    # ── Exit decision ─────────────────────────────────────────────────────────

    async def _evaluate_exits(self, *, now_ms: int) -> None:
        """For each OPEN FarbPosition: check if we should begin closing."""
        open_fps = await self.farb_repo.list_open(self.strategy_id)
        for fp in open_fps:
            await self._evaluate_exit_one(fp, now_ms=now_ms)

    async def _evaluate_exit_one(self, fp: FarbPosition, *, now_ms: int) -> None:
        signal = await self._compute_signal(fp.coin)

        sd = fp.state_data
        opened_at_ms: int = sd.get("opened_at_ms", _dt_to_ms(fp.opened_at))
        hours_held = (now_ms - opened_at_ms) / 3_600_000

        pos_min_hold = sd.get("position_min_hold_hours", self.params.base_min_hold_hours)
        gross_funding = sd.get("gross_funding_so_far", 0.0)
        total_fees = sd.get("total_fees_paid", 0.0)
        consec_neg = sd.get("consec_negative_hours", 0)

        # Compute current hourly income quote (position_size × signal / hours_per_year)
        if signal is not None and signal > 0:
            current_hourly_income = self.params.position_size_usdc * signal / _HOURS_PER_YEAR
        else:
            current_hourly_income = 0.0

        # Update consec_negative counter in state_data
        new_consec_neg = update_consec_negative(
            prev_consec_negative=consec_neg,
            smoothed_signal_annual=signal,
        )

        decision = decide_two_phase(
            in_position=True,
            smoothed_signal_annual=signal,
            entry_threshold=self.params.entry_threshold_apr,
            hours_in_position=int(hours_held),
            position_min_hold_hours=pos_min_hold,
            gross_funding_so_far=gross_funding,
            total_fees_paid=total_fees,
            consec_negative_hours=new_consec_neg,
            current_hourly_income_quote=current_hourly_income,
            phase1_negative_patience=self.params.phase1_negative_patience,
            phase1_breakeven_cap_hours=self.params.phase1_breakeven_cap_hours,
            phase2_exit_threshold=self.params.phase2_exit_threshold,
        )

        # Always persist updated counters
        updated_sd = {
            **sd,
            "consec_negative_hours": new_consec_neg,
        }

        if decision != TwoPhaseDecision.NONE:
            try:
                await self.farb_repo.transition(
                    fp.id,
                    from_state=FarbState.OPEN,
                    to_state=FarbState.CLOSING_SHORT,
                    state_data={**updated_sd, "exit_signal_apr": signal, "exit_decision": decision.value},
                )
                _pkg.logger.info(
                    "exit triggered farb_position_id=%s decision=%s signal=%.4f",
                    fp.id,
                    decision.value,
                    signal if signal is not None else float("nan"),
                )
            except StateConflict:
                _pkg.logger.debug(
                    "state_conflict on exit transition farb_position_id=%s — skipping",
                    fp.id,
                )
        else:
            # Checkpoint updated counters without changing state
            await self.farb_repo.update_state_data(fp.id, updated_sd)

    # ── Rollback ──────────────────────────────────────────────────────────────

    async def _rollback(self, fp: FarbPosition, *, partial_state: FarbState, error: Exception) -> None:
        """Best-effort cleanup of partially-opened positions.

        Does NOT re-raise. Logs all inner failures at ERROR level.
        """
        _pkg.logger.info(
            "rollback starting farb_position_id=%s partial_state=%s error=%s",
            fp.id,
            partial_state.value,
            error,
        )
        try:
            if partial_state == FarbState.OPENING_SHORT:
                # Spot leg is open; close it
                if fp.spot_position_id is not None:
                    try:
                        spot_pos = await load_position(self._sf,fp.spot_position_id)
                        await self.exchange.close_position(spot_pos)
                        _pkg.logger.info(
                            "rollback: closed spot leg farb_position_id=%s spot_position_id=%s",
                            fp.id,
                            fp.spot_position_id,
                        )
                    except Exception as inner:  # noqa: BLE001
                        _pkg.logger.error(
                            "rollback: failed to close spot leg farb_position_id=%s: %s",
                            fp.id,
                            inner,
                        )

            elif partial_state == FarbState.OPENING_LONG:
                # Margin is reserved; transfer it back to spot
                required = fp.state_data.get("required_margin", self.params.required_margin())
                try:
                    await self.exchange.transfer("USDC", required, WalletKind.PERP, WalletKind.SPOT)
                    _pkg.logger.info(
                        "rollback: returned margin farb_position_id=%s amount=%.4f",
                        fp.id,
                        required,
                    )
                except Exception as inner:  # noqa: BLE001
                    _pkg.logger.error(
                        "rollback: failed to return margin farb_position_id=%s: %s",
                        fp.id,
                        inner,
                    )

            elif partial_state in (FarbState.CLOSING_LONG, FarbState.CLOSING_SHORT):
                # Close-side failure: log for human/oncall, do NOT auto-reopen
                _pkg.logger.error(
                    "rollback: close-side failure farb_position_id=%s state=%s — "
                    "manual intervention required, NOT auto-reopening",
                    fp.id,
                    partial_state.value,
                )

        except Exception as outer:  # noqa: BLE001
            _pkg.logger.error(
                "rollback: unexpected error farb_position_id=%s: %s",
                fp.id,
                outer,
            )


# ─── Tiny utilities ──────────────────────────────────────────────────────────

def _dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)
