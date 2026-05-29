from __future__ import annotations

from dataclasses import dataclass, field


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
