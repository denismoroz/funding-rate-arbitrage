"""MarginWatchdog — hour-tick orchestrator for live margin monitoring.

Pulls HL account snapshot + DB FP data, builds FpMarginSnapshots,
calls MarginManager.assess_account, emits events and triggers forced-close
when account ratio breaches thresholds.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from frab.domain import FarbState
from frab.engine.margin_manager import (
    AccountAssessment,
    FpMarginSnapshot,
    MarginManager,
    MarginStatus,
)
from frab.events.bus import Event, EventBus
from frab.repo.farb_repo import FarbRepo, StateConflict
from frab.settings import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchdogReport:
    assessment: AccountAssessment
    actions_taken: list[str]


class MarginWatchdog:
    """Monitors live HL margin (account-wide) and per-FP virtual ratios.

    Decision rule (two-tier):
      - Account ratio drives WARNING/FORCED_CLOSE/LIQ event emission.
      - When account triggers close, weakest FP (lowest virtual_ratio) is closed.
    """

    def __init__(
        self,
        *,
        strategy_id: int,
        exchange,  # HLExchange (typed loosely to avoid cycle)
        farb_repo: FarbRepo,
        margin_manager: MarginManager,
        settings: Settings,
        event_bus: EventBus,
    ) -> None:
        self._strategy_id = strategy_id
        self._exchange = exchange
        self._farb_repo = farb_repo
        self._mgr = margin_manager
        self._settings = settings
        self._bus = event_bus

    async def run_check(self, *, now_ms: int) -> WatchdogReport:
        """Full check: assess + emit events + force-close if triggered."""
        assessment = await self._compute_assessment()
        actions: list[str] = []

        if assessment.account_status == MarginStatus.HEALTHY:
            return WatchdogReport(assessment=assessment, actions_taken=actions)

        # Emit event for any non-healthy status
        await self._emit_event(assessment, now_ms)
        actions.append(f"event:margin.{assessment.account_status.value}")

        # Force-close only when account triggers AND weakest selected
        if assessment.weakest_fp_id is not None:
            weakest_fp_id = assessment.weakest_fp_id
            fp = await self._farb_repo.get(weakest_fp_id)
            if fp is None:
                logger.warning(
                    "margin watchdog: FP %s not found in DB; skipping transition",
                    weakest_fp_id,
                )
            else:
                try:
                    merged_state_data = {
                        **fp.state_data,
                        "exit_decision": "watchdog_forced",
                        "exit_requested_at_ms": now_ms,
                    }
                    await self._farb_repo.transition(
                        weakest_fp_id,
                        from_state=fp.state,
                        to_state=FarbState.CLOSING_SHORT,
                        state_data=merged_state_data,
                    )
                    actions.append(f"forced_close:fp={weakest_fp_id}")
                    logger.error(
                        "margin watchdog forced-close FP %s (account_ratio=%.3f, weakest_virtual=%.3f)",
                        weakest_fp_id,
                        assessment.account_ratio,
                        next(
                            a.virtual_ratio
                            for a in assessment.per_fp
                            if a.farb_position_id == weakest_fp_id
                        ),
                    )
                except StateConflict as exc:
                    logger.warning(
                        "margin watchdog: StateConflict transitioning FP %s: %s",
                        weakest_fp_id,
                        exc,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "margin watchdog: failed transition FP %s: %s",
                        weakest_fp_id,
                        exc,
                    )

        return WatchdogReport(assessment=assessment, actions_taken=actions)

    async def dry_assess(self) -> AccountAssessment:
        """Read-only assess for /api/equity/margin endpoint."""
        return await self._compute_assessment()

    # ── private ───────────────────────────────────────────────────────────────

    async def _compute_assessment(self) -> AccountAssessment:
        perp_state, _ = await self._exchange.get_account_snapshot()
        account_value = perp_state.account_value

        open_fps = await self._farb_repo.list_active(self._strategy_id)
        snapshots: list[FpMarginSnapshot] = []
        maint_by_coin: dict[str, float] = {}

        for fp in open_fps:
            ap = next(
                (a for a in perp_state.asset_positions if a.coin == fp.coin),
                None,
            )
            if ap is None:
                logger.warning(
                    "watchdog: FP %s coin %s missing from HL asset_positions",
                    fp.id,
                    fp.coin,
                )
                continue

            spec = self._settings.get_coin_spec(fp.coin)
            short_size = abs(ap.szi)
            mark = ap.position_value / short_size if short_size else 0.0

            snapshots.append(
                FpMarginSnapshot(
                    farb_position_id=fp.id,
                    coin=fp.coin,
                    short_size=short_size,
                    current_mark=mark,
                    required_margin=float(fp.state_data.get("required_margin", 0.0)),
                    unrealized_pnl=ap.unrealized_pnl,
                    funding_accrued=float(fp.state_data.get("gross_funding_so_far", 0.0)),
                    fees_paid=float(fp.state_data.get("total_fees_paid", 0.0)),
                    signal_apr=float(fp.state_data.get("current_signal_apr", 0.0)),
                )
            )
            maint_by_coin[fp.coin] = spec.maint_ratio

        return self._mgr.assess_account(
            account_equity_usdc=account_value,
            per_fp_snapshots=snapshots,
            maint_ratio_by_coin=maint_by_coin,
        )

    async def _emit_event(self, assessment: AccountAssessment, now_ms: int) -> None:
        level = {
            MarginStatus.WARNING: "WARNING",
            MarginStatus.FORCED_CLOSE: "ERROR",
            MarginStatus.LIQUIDATION_IMMINENT: "CRITICAL",
        }[assessment.account_status]

        await self._bus.publish(
            Event(
                ts=datetime.now(timezone.utc),
                level=level,
                source="margin_watchdog",
                kind=f"margin.{assessment.account_status.value}",
                message=(
                    f"account_ratio={assessment.account_ratio:.3f} "
                    f"status={assessment.account_status.value} "
                    f"weakest_fp={assessment.weakest_fp_id}"
                ),
                payload_json={
                    "account_ratio": assessment.account_ratio,
                    "account_equity_usdc": assessment.account_equity_usdc,
                    "total_maintenance_usdc": assessment.total_maintenance_usdc,
                    "weakest_fp_id": assessment.weakest_fp_id,
                    "per_fp": [
                        {
                            "farb_position_id": a.farb_position_id,
                            "coin": a.coin,
                            "virtual_ratio": a.virtual_ratio,
                            "status": a.status.value,
                        }
                        for a in assessment.per_fp
                    ],
                },
            )
        )
