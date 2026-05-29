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

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from frab.db.models import Exchange as ExchangeRow
from frab.db.models import FundingAccrual as FundingAccrualRow
from frab.db.models import FundingRate as FundingRateRow
from frab.db.session import session_scope
from frab.domain import FarbPosition, FarbState, Instrument, Position, Side
from frab.constants import PERP_TAKER, SPOT_TAKER
from frab.engine.two_phase_signals import (
    TwoPhaseDecision,
    compute_position_min_hold,
    decide_two_phase,
    update_consec_negative,
)
from frab.events.bus import Event, EventBus
from frab.exchanges.protocol import Exchange, OpenRequest, WalletKind
from frab.repo.farb_repo import FarbRepo, StateConflict

logger = logging.getLogger(__name__)

# HL hourly funding intervals per year
_HOURS_PER_YEAR = 8760


# ─── Params ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TwoPhaseParams:
    """All tunable parameters for TwoPhaseStrategy.

    Defaults are Candidate C from research/two_phase_dynamic_stability.py.
    """
    coins: list[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    entry_threshold_apr: float = 0.10        # entry when smoothed signal > this
    phase2_exit_threshold: float = -0.10     # exit (phase2) when signal < this
    base_min_hold_hours: int = 24            # floor on dynamic min_hold
    cap_min_hold_hours: int = 720            # ceiling on dynamic min_hold
    safety_mult: float = 5.0                # multiplier for breakeven-based min_hold
    signal_window_hours: int = 12           # rolling MA window (funding ticks)
    concurrency_cap: int = 3               # K: max simultaneous open positions
    position_size_usdc: float = 1000.0      # notional per spot leg
    budget_cap_usdc: float = 10000.0        # max total committed capital (spot notional + margin) across open + pending FarbPositions
    margin_buffer_factor: float = 3.0       # perp margin = size/leverage * buffer
    perp_leverage: float = 5.0             # perp leverage for margin calculation
    # Two-phase exit params
    phase1_negative_patience: int = 72      # hours of consecutive negative before phase1 exit
    phase1_breakeven_cap_hours: int = 720   # if hours-to-breakeven > this → exit phase1

    @classmethod
    def from_dict(cls, d: dict) -> "TwoPhaseParams":
        """Construct from a params_json dict. Unknown keys are ignored."""
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)

    def required_margin(self) -> float:
        """USDC to transfer to perp wallet when opening a new position."""
        return (self.position_size_usdc / self.perp_leverage) * self.margin_buffer_factor

    def per_position_footprint(self) -> float:
        """Total USDC committed by one FarbPosition: spot notional + reserved margin."""
        return self.position_size_usdc + self.required_margin()


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
                logger.warning(
                    "state_conflict farb_position_id=%s: %s — skipping tick",
                    current.id,
                    exc,
                )
                break
            except Exception as exc:  # noqa: BLE001
                logger.error(
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
                logger.error(
                    "advance_one: farb_repo.get returned None for farb_position_id=%s after dispatch — aborting",
                    current.id,
                )
                break
            current = refreshed
        else:
            # Safety cap: loop exhausted without reaching a terminal state
            logger.error(
                "advance_one safety cap hit farb_position_id=%s state=%s — aborting burst",
                current.id,
                current.state.value,
            )

    async def _dispatch(self, fp: FarbPosition) -> None:
        """Route to the correct handler based on fp.state."""
        match fp.state:
            case FarbState.CHECK_MARGIN:
                await self._step_check_margin(fp)
            case FarbState.OPENING_MARGIN:
                await self._step_opening_margin(fp)
            case FarbState.OPENING_LONG:
                await self._step_opening_long(fp)
            case FarbState.OPENING_SHORT:
                await self._step_opening_short(fp)
            case FarbState.OPEN:
                pass  # steady state — no-op
            case FarbState.CLOSING_SHORT:
                await self._step_closing_short(fp)
            case FarbState.CLOSING_LONG:
                await self._step_closing_long(fp)
            case FarbState.RELEASING_MARGIN:
                await self._step_releasing_margin(fp)
            case FarbState.CLOSED | FarbState.FAILED:
                pass  # terminal — no-op

    # ── Open-side steps ───────────────────────────────────────────────────────

    async def _step_check_margin(self, fp: FarbPosition) -> None:
        required = self.params.required_margin()
        balance = await self.exchange.get_wallet("USDC", WalletKind.SPOT)
        if balance < required:
            reason = f"insufficient_margin: need {required:.4f}, have {balance:.4f}"
            logger.warning(
                "check_margin failed farb_position_id=%s coin=%s "
                "required=%.4f available=%.4f → FAILED",
                fp.id, fp.coin, required, balance,
            )
            await self.farb_repo.mark_failed(fp.id, reason=reason)
            await self._publish(
                level="WARNING",
                kind="farb.failed",
                message=f"{fp.coin} FAILED at CHECK_MARGIN: {reason}",
                payload={
                    "farb_position_id": fp.id,
                    "coin": fp.coin,
                    "state": FarbState.CHECK_MARGIN.value,
                    "required": required,
                    "available": balance,
                    "reason": reason,
                },
            )
            return
        await self.farb_repo.transition(
            fp.id,
            from_state=FarbState.CHECK_MARGIN,
            to_state=FarbState.OPENING_LONG,
            state_data={**fp.state_data, "required_margin": required},
        )

    async def _step_opening_margin(self, fp: FarbPosition) -> None:
        # HL is cross-margin on one account; no spot→perp transfer needed.
        # Kept as a pass-through transition for any FP already mid-flight in this state.
        await self.farb_repo.transition(
            fp.id,
            from_state=FarbState.OPENING_MARGIN,
            to_state=FarbState.OPENING_LONG,
        )

    async def _step_opening_long(self, fp: FarbPosition) -> None:
        quote = await self.exchange.get_quote(fp.coin)
        price = quote.spot if quote.spot is not None else quote.mark
        spot_qty = self.params.position_size_usdc / price
        req = OpenRequest(
            coin=fp.coin,
            instrument=Instrument.SPOT,
            side=Side.LONG,
            qty=spot_qty,
            farb_position_id=fp.id,
        )
        pos = await self.exchange.open_position(req)
        await self.farb_repo.set_leg(fp.id, instrument=Instrument.SPOT, position_id=pos.id)
        # Use the actual filled qty (pos.qty) for the perp short so spot and
        # perp legs match in size after HL's szDecimals flooring + partial fills.
        await self.farb_repo.transition(
            fp.id,
            from_state=FarbState.OPENING_LONG,
            to_state=FarbState.OPENING_SHORT,
            state_data={**fp.state_data, "spot_qty": pos.qty, "spot_entry_price": pos.entry_price},
        )

    async def _step_opening_short(self, fp: FarbPosition) -> None:
        spot_qty = fp.state_data.get("spot_qty")
        if spot_qty is None:
            # Fallback: recompute from current price
            quote = await self.exchange.get_quote(fp.coin)
            price = quote.spot if quote.spot is not None else quote.mark
            spot_qty = self.params.position_size_usdc / price

        req = OpenRequest(
            coin=fp.coin,
            instrument=Instrument.PERP,
            side=Side.SHORT,
            qty=spot_qty,
            farb_position_id=fp.id,
            leverage=int(self.params.perp_leverage),
        )
        pos = await self.exchange.open_position(req)
        await self.farb_repo.set_leg(fp.id, instrument=Instrument.PERP, position_id=pos.id)
        # Record two-phase dynamic state at entry
        entry_signal = fp.state_data.get("target_signal_apr", 0.0)
        pos_min_hold = compute_position_min_hold(
            entry_signal_annual=entry_signal,
            safety_mult=self.params.safety_mult,
            base_min_hold_hours=self.params.base_min_hold_hours,
            cap_min_hold_hours=self.params.cap_min_hold_hours,
        )
        # total_fees_paid: round-trip fees at entry (perp taker + spot taker, both sides)
        total_fees_paid = self.params.position_size_usdc * (PERP_TAKER + SPOT_TAKER) * 2
        await self.farb_repo.transition(
            fp.id,
            from_state=FarbState.OPENING_SHORT,
            to_state=FarbState.OPEN,
            state_data={
                **fp.state_data,
                "position_min_hold_hours": pos_min_hold,
                "gross_funding_so_far": 0.0,
                "total_fees_paid": total_fees_paid,
                "consec_negative_hours": 0,
                "opened_at_ms": _now_ms(),
            },
        )
        await self._publish(
            level="INFO",
            kind="farb.opened",
            message=(
                f"{fp.coin} OPEN: spot={spot_qty:.6f} @ "
                f"{fp.state_data.get('spot_entry_price', 0):.2f}, "
                f"perp_short={pos.qty:.6f} @ {pos.entry_price:.2f}"
            ),
            payload={
                "farb_position_id": fp.id,
                "coin": fp.coin,
                "spot_qty": spot_qty,
                "perp_qty": pos.qty,
                "spot_entry_price": fp.state_data.get("spot_entry_price"),
                "perp_entry_price": pos.entry_price,
                "target_signal_apr": entry_signal,
                "position_min_hold_hours": pos_min_hold,
            },
        )

    # ── Close-side steps ──────────────────────────────────────────────────────

    async def _step_closing_short(self, fp: FarbPosition) -> None:
        if fp.perp_position_id is None:
            raise RuntimeError(f"FarbPosition {fp.id} has no perp_position_id in CLOSING_SHORT")
        perp_pos = await self._get_position(fp.perp_position_id)
        await self.exchange.close_position(perp_pos)
        await self.farb_repo.transition(
            fp.id,
            from_state=FarbState.CLOSING_SHORT,
            to_state=FarbState.CLOSING_LONG,
        )

    async def _step_closing_long(self, fp: FarbPosition) -> None:
        if fp.spot_position_id is None:
            raise RuntimeError(f"FarbPosition {fp.id} has no spot_position_id in CLOSING_LONG")
        spot_pos = await self._get_position(fp.spot_position_id)
        await self.exchange.close_position(spot_pos)
        await self.farb_repo.mark_closed(fp.id)
        await self._publish(
            level="INFO",
            kind="farb.closed",
            message=f"{fp.coin} CLOSED (held {fp.state_data.get('hours_in_position', '?')}h)",
            payload={
                "farb_position_id": fp.id,
                "coin": fp.coin,
                "exit_signal_apr": fp.state_data.get("exit_signal_apr"),
                "exit_decision": fp.state_data.get("exit_decision"),
            },
        )

    async def _step_releasing_margin(self, fp: FarbPosition) -> None:
        # HL is cross-margin on one account; no perp→spot transfer needed.
        # Kept as a pass-through close for any FP already mid-flight in this state.
        await self.farb_repo.mark_closed(fp.id)

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
            perp_pos = await self._get_position(fp.perp_position_id)
            try:
                gross = await self.exchange.get_accrued_funding(perp_pos)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
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

            logger.info(
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
            logger.info(
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
                logger.info(
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
            logger.info(
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
                logger.info(
                    "exit triggered farb_position_id=%s decision=%s signal=%.4f",
                    fp.id,
                    decision.value,
                    signal if signal is not None else float("nan"),
                )
            except StateConflict:
                logger.debug(
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
        logger.info(
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
                        spot_pos = await self._get_position(fp.spot_position_id)
                        await self.exchange.close_position(spot_pos)
                        logger.info(
                            "rollback: closed spot leg farb_position_id=%s spot_position_id=%s",
                            fp.id,
                            fp.spot_position_id,
                        )
                    except Exception as inner:  # noqa: BLE001
                        logger.error(
                            "rollback: failed to close spot leg farb_position_id=%s: %s",
                            fp.id,
                            inner,
                        )

            elif partial_state == FarbState.OPENING_LONG:
                # Margin is reserved; transfer it back to spot
                required = fp.state_data.get("required_margin", self.params.required_margin())
                try:
                    await self.exchange.transfer("USDC", required, WalletKind.PERP, WalletKind.SPOT)
                    logger.info(
                        "rollback: returned margin farb_position_id=%s amount=%.4f",
                        fp.id,
                        required,
                    )
                except Exception as inner:  # noqa: BLE001
                    logger.error(
                        "rollback: failed to return margin farb_position_id=%s: %s",
                        fp.id,
                        inner,
                    )

            elif partial_state in (FarbState.CLOSING_LONG, FarbState.CLOSING_SHORT):
                # Close-side failure: log for human/oncall, do NOT auto-reopen
                logger.error(
                    "rollback: close-side failure farb_position_id=%s state=%s — "
                    "manual intervention required, NOT auto-reopening",
                    fp.id,
                    partial_state.value,
                )

        except Exception as outer:  # noqa: BLE001
            logger.error(
                "rollback: unexpected error farb_position_id=%s: %s",
                fp.id,
                outer,
            )

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _get_position(self, position_id: int) -> Position:
        """Load a Position domain object from DB by id."""
        from frab.db.models import Position as PositionRow
        from frab.domain import Position as DomainPosition
        from frab.domain.enums import Instrument as Inst, PositionStatus, Side as S
        from datetime import timezone

        async with session_scope(self._sf) as session:
            row = await session.get(PositionRow, position_id)
            if row is None:
                raise KeyError(f"Position {position_id} not found")
            return DomainPosition(
                id=row.id,
                exchange_name=str(row.exchange_id),  # exchange name resolved elsewhere
                coin=row.coin,
                instrument=Inst(row.instrument),
                side=S(row.side),
                qty=row.qty,
                entry_price=row.entry_price,
                opened_at=datetime.fromtimestamp(row.opened_at / 1000, tz=timezone.utc),
                closed_at=(
                    datetime.fromtimestamp(row.closed_at / 1000, tz=timezone.utc)
                    if row.closed_at is not None
                    else None
                ),
                status=PositionStatus(row.status),
                farb_position_id=row.farb_position_id,
            )


# ─── Tiny utilities ──────────────────────────────────────────────────────────

def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)
