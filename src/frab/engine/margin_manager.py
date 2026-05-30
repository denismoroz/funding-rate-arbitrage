"""Pure-logic margin assessment for cross-margin perp portfolios."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum


class MarginStatus(Enum):
    HEALTHY = "healthy"
    WARNING = "warning"
    FORCED_CLOSE = "forced_close"
    LIQUIDATION_IMMINENT = "liquidation_imminent"


@dataclass(frozen=True)
class FpMarginSnapshot:
    """Per-FP virtual margin state (research-style isolated view)."""
    farb_position_id: int
    coin: str
    short_size: float
    current_mark: float
    required_margin: float
    unrealized_pnl: float
    funding_accrued: float
    fees_paid: float
    signal_apr: float


@dataclass(frozen=True)
class FpAssessment:
    farb_position_id: int
    coin: str
    virtual_equity: float
    virtual_maintenance: float
    virtual_ratio: float
    status: MarginStatus


@dataclass(frozen=True)
class AccountAssessment:
    """Two-tier assessment: account triggers, per-FP picks."""
    account_ratio: float
    account_equity_usdc: float
    total_maintenance_usdc: float
    account_status: MarginStatus
    per_fp: list[FpAssessment]
    weakest_fp_id: int | None


class MarginManager:
    """Pure-logic margin policy for cross-margin perp portfolios.

    Two-tier:
      - Account-wide ratio = decision trigger
      - Per-FP virtual ratio = which FP to close + UI visibility
    """

    def __init__(
        self,
        *,
        top_up_trigger: float,
        forced_close_trigger: float,
        healthy_ratio: float,
    ) -> None:
        if not (1.0 < forced_close_trigger < top_up_trigger <= healthy_ratio):
            raise ValueError(
                f"thresholds must satisfy 1.0 < forced_close_trigger ({forced_close_trigger}) "
                f"< top_up_trigger ({top_up_trigger}) <= healthy_ratio ({healthy_ratio})"
            )
        self.top_up_trigger = top_up_trigger
        self.forced_close_trigger = forced_close_trigger
        self.healthy_ratio = healthy_ratio

    def assess_fp(self, snap: FpMarginSnapshot, maint_ratio: float) -> FpAssessment:
        virtual_equity = (
            snap.required_margin
            + snap.unrealized_pnl
            + snap.funding_accrued
            - snap.fees_paid
        )
        virtual_maint = snap.short_size * snap.current_mark * maint_ratio
        ratio = virtual_equity / virtual_maint if virtual_maint > 0 else float("inf")
        return FpAssessment(
            farb_position_id=snap.farb_position_id,
            coin=snap.coin,
            virtual_equity=virtual_equity,
            virtual_maintenance=virtual_maint,
            virtual_ratio=ratio,
            status=self._classify(ratio),
        )

    def _classify(self, ratio: float) -> MarginStatus:
        if ratio >= self.top_up_trigger:
            return MarginStatus.HEALTHY
        if ratio > self.forced_close_trigger:
            return MarginStatus.WARNING
        if ratio > 1.0:
            return MarginStatus.FORCED_CLOSE
        return MarginStatus.LIQUIDATION_IMMINENT

    def assess_account(
        self,
        *,
        account_equity_usdc: float,
        per_fp_snapshots: list[FpMarginSnapshot],
        maint_ratio_by_coin: dict[str, float],
    ) -> AccountAssessment:
        per_fp_assessments = [
            self.assess_fp(snap, maint_ratio_by_coin[snap.coin])
            for snap in per_fp_snapshots
        ]
        total_maint = sum(a.virtual_maintenance for a in per_fp_assessments)
        account_ratio = (
            account_equity_usdc / total_maint if total_maint > 0 else float("inf")
        )
        account_status = self._classify(account_ratio)

        weakest_id: int | None = None
        if account_status in (MarginStatus.FORCED_CLOSE, MarginStatus.LIQUIDATION_IMMINENT):
            if per_fp_assessments:
                weakest = min(per_fp_assessments, key=lambda a: a.virtual_ratio)
                weakest_id = weakest.farb_position_id

        return AccountAssessment(
            account_ratio=account_ratio,
            account_equity_usdc=account_equity_usdc,
            total_maintenance_usdc=total_maint,
            account_status=account_status,
            per_fp=per_fp_assessments,
            weakest_fp_id=weakest_id,
        )
