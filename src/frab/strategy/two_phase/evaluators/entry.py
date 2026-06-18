"""EntryEvaluator — decides which coins to open new FarbPositions for."""
from __future__ import annotations

from typing import TYPE_CHECKING

from frab.domain import FarbState
from frab.engine.two_phase_signals import TwoPhaseDecision, decide_entry
from frab.repo.farb_repo import FarbRepo
import frab.strategy.two_phase as _pkg  # logger looked up at call time so patch.object works
from frab.strategy.two_phase.params import TwoPhaseParams
from frab.strategy.two_phase.evaluators.signal import SignalComputer

if TYPE_CHECKING:
    from frab.coin_registry import CoinRegistry


class EntryEvaluator:
    """Evaluates each coin for entry and creates FarbPositions when signals qualify.

    The candidate coin universe is read from ``registry.universe()`` on every call
    (hot re-read, no restart needed).  This allows newly-activated coins to be
    considered immediately.

    A ``CoinRegistry`` must be provided in production.  When ``registry`` is
    ``None`` (some unit-test scenarios), the entry universe is empty — no new
    positions will be created.
    """

    def __init__(
        self,
        *,
        strategy_id: int,
        farb_repo: FarbRepo,
        params: TwoPhaseParams,
        signal_computer: SignalComputer,
        registry: "CoinRegistry | None" = None,
    ) -> None:
        self._strategy_id = strategy_id
        self._farb_repo = farb_repo
        self._params = params
        self._signal_computer = signal_computer
        self._registry = registry

    async def evaluate(self, *, now_ms: int, force_cooldown_bypass: bool) -> None:
        """For each coin: compute signal, check concurrency cap, create new arbs."""
        p = self._params

        # Count non-terminal positions (includes all transient + PRE/POST resting states)
        all_non_terminal = await self._farb_repo.list_non_terminal(self._strategy_id)
        non_terminal_count = len(all_non_terminal)

        slots = p.concurrency_cap - non_terminal_count
        if slots <= 0:
            return

        # Budget cap: further constrain slots by available committed capital
        footprint = p.compute_footprint()
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

        # Determine the active entry universe from the registry (hot re-read each call).
        # Only active+validated coins are offered for new entry.
        # If no registry is configured (unit-test / misconfigured path), produce no
        # entries rather than falling back to a hardcoded list.
        entry_universe: tuple[str, ...] = (
            self._registry.universe() if self._registry is not None else ()
        )

        # Evaluate each coin for entry
        candidates: list[tuple[str, float]] = []
        current_hour = now_ms // 3_600_000
        for coin in entry_universe:
            # Skip if already has a non-terminal position (includes PRE/POST and all transient states)
            # list_by_coin(include_terminal=False) excludes only CLOSED/FAILED
            existing = await self._farb_repo.list_by_coin(
                self._strategy_id, coin, include_terminal=False
            )
            if existing:
                continue

            # Cooldown: if a FP for this coin failed in the current hour, wait
            # for the next hour-tick before retrying (avoids tight failure loop).
            all_for_coin = await self._farb_repo.list_by_coin(
                self._strategy_id, coin, include_terminal=True
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
                and not force_cooldown_bypass
            ):
                _pkg.logger.info(
                    "entry cooldown: coin=%s last_failed_at_ms=%d this_hour=%d, skip",
                    coin, last_failed_ms, current_hour,
                )
                continue

            signal = await self._signal_computer.compute(coin)
            if signal is None:
                continue

            decision = decide_entry(
                smoothed_signal_annual=signal,
                entry_threshold=p.entry_threshold_apr,
            )
            if decision == TwoPhaseDecision.OPEN:
                candidates.append((coin, signal))

        # Sort by signal strength descending, pick top `slots`
        candidates.sort(key=lambda x: -x[1])
        for coin, signal in candidates[:slots]:
            await self._farb_repo.create(
                strategy_id=self._strategy_id,
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
