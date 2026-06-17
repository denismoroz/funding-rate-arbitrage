"""XsmomMarginWatchdog — hour-tick margin monitor for the XSMOM strategy.

Analogue of frab.engine.margin_watchdog.MarginWatchdog but reads XsmomRepo
instead of FarbRepo and transitions the weakest position to XsmomState.CLOSE
(not FarbState.CLOSING_SHORT).

Reuses MarginManager / FpMarginSnapshot / AccountAssessment / MarginStatus
unchanged (pure-logic, no I/O).  FpMarginSnapshot.farb_position_id and
FpMarginSnapshot.short_size are reused with xsmom semantics:
  - farb_position_id → xsmom_position_id (field name is a FRAB legacy label)
  - short_size → abs(ap.szi)  (works for both LONG and SHORT legs)

Source field in events: ``"xsmom_margin_watchdog"`` to distinguish from
the FRAB watchdog.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from frab.coin_registry import CoinRegistry
from frab.domain import XsmomState
from frab.engine.margin_manager import (
    AccountAssessment,
    FpMarginSnapshot,
    MarginManager,
    MarginStatus,
)
from frab.events.bus import Event, EventBus
from frab.repo.xsmom_repo import XsmomRepo, XsmomStateConflict

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchdogReport:
    assessment: AccountAssessment
    actions_taken: list[str]


class XsmomMarginWatchdog:
    """Monitors live HL margin and per-XsmomPosition virtual ratios.

    Decision rule (two-tier):
      - Account ratio drives WARNING / FORCED_CLOSE / LIQ event emission.
      - When account triggers close, the weakest position (lowest virtual_ratio)
        is transitioned to XsmomState.CLOSE.
    """

    def __init__(
        self,
        *,
        strategy_id: int,
        exchange,  # typed loosely to avoid cycle
        xsmom_repo: XsmomRepo,
        margin_manager: MarginManager,
        registry: CoinRegistry,
        event_bus: EventBus,
    ) -> None:
        self._strategy_id = strategy_id
        self._exchange = exchange
        self._repo = xsmom_repo
        self._mgr = margin_manager
        self._registry = registry
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

        # Force-close only when account triggers AND weakest is selected
        if assessment.weakest_fp_id is not None:
            weakest_id = assessment.weakest_fp_id
            fp = await self._repo.get(weakest_id)
            if fp is None:
                logger.warning(
                    "xsmom margin watchdog: XsmomPosition %s not found in DB; skipping transition",
                    weakest_id,
                )
            else:
                try:
                    merged_state_data = {
                        **fp.state_data,
                        "exit_decision": "watchdog_forced",
                        "exit_requested_at_ms": now_ms,
                    }
                    await self._repo.transition(
                        weakest_id,
                        from_state=fp.state,
                        to_state=XsmomState.CLOSE,
                        state_data=merged_state_data,
                    )
                    actions.append(f"forced_close:fp={weakest_id}")
                    logger.error(
                        "xsmom margin watchdog forced-close XsmomPosition %s "
                        "(account_ratio=%.3f, weakest_virtual=%.3f)",
                        weakest_id,
                        assessment.account_ratio,
                        next(
                            a.virtual_ratio
                            for a in assessment.per_fp
                            if a.farb_position_id == weakest_id
                        ),
                    )
                except XsmomStateConflict as exc:
                    logger.warning(
                        "xsmom margin watchdog: XsmomStateConflict transitioning id=%s: %s",
                        weakest_id,
                        exc,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "xsmom margin watchdog: failed transition id=%s: %s",
                        weakest_id,
                        exc,
                    )

        return WatchdogReport(assessment=assessment, actions_taken=actions)

    async def dry_assess(self) -> AccountAssessment:
        """Read-only assess (for API / debug endpoints)."""
        return await self._compute_assessment()

    # ── private ───────────────────────────────────────────────────────────────

    async def _compute_assessment(self) -> AccountAssessment:
        perp_state, _ = await self._exchange.get_account_snapshot()
        account_value = perp_state.account_value

        open_fps = await self._repo.list_active(self._strategy_id)
        snapshots: list[FpMarginSnapshot] = []
        maint_by_coin: dict[str, float] = {}

        for fp in open_fps:
            ap = next(
                (a for a in perp_state.asset_positions if a.coin == fp.coin),
                None,
            )
            if ap is None:
                logger.warning(
                    "xsmom watchdog: XsmomPosition %s coin %s missing from HL asset_positions",
                    fp.id,
                    fp.coin,
                )
                continue

            spec = self._registry.get_coin_spec(fp.coin)
            # abs(szi) works for both LONG (szi > 0) and SHORT (szi < 0)
            size = abs(ap.szi)
            mark = ap.position_value / size if size else 0.0

            # FpMarginSnapshot field names are FRAB legacy labels; we reuse them:
            #   farb_position_id → xsmom_position_id
            #   short_size       → abs(szi) regardless of direction
            snapshots.append(
                FpMarginSnapshot(
                    farb_position_id=fp.id,
                    coin=fp.coin,
                    short_size=size,
                    current_mark=mark,
                    required_margin=float(fp.state_data.get("required_margin", 0.0)),
                    unrealized_pnl=ap.unrealized_pnl,
                    funding_accrued=float(fp.state_data.get("gross_funding_so_far", 0.0)),
                    fees_paid=float(fp.state_data.get("total_fees_paid", 0.0)),
                    signal_apr=0.0,  # XSMOM does not track a signal_apr per position
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
                source="xsmom_margin_watchdog",
                kind=f"margin.{assessment.account_status.value}",
                message=(
                    f"account_ratio={assessment.account_ratio:.3f} "
                    f"status={assessment.account_status.value} "
                    f"weakest_xsmom={assessment.weakest_fp_id}"
                ),
                payload_json={
                    "account_ratio": assessment.account_ratio,
                    "account_equity_usdc": assessment.account_equity_usdc,
                    "total_maintenance_usdc": assessment.total_maintenance_usdc,
                    "weakest_xsmom_id": assessment.weakest_fp_id,
                    "per_fp": [
                        {
                            "xsmom_position_id": a.farb_position_id,
                            "coin": a.coin,
                            "virtual_ratio": a.virtual_ratio,
                            "status": a.status.value,
                        }
                        for a in assessment.per_fp
                    ],
                },
            )
        )
