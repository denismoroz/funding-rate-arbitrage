"""XsmomParams — tunable parameters for the XSMOM cross-sectional momentum strategy."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class XsmomParams:
    """All tunable parameters for XsmomStrategy.

    ``n_positions`` is the *total* even count of long+short legs (e.g. 6 → 3 long, 3 short).
    Set to None to use the auto tercile rule: k = max(1, universe_len // 3) per side.
    """

    budget_cap: float
    universe: tuple[str, ...]
    n_positions: int | None = None          # None → auto tercile; else total even count
    auto: bool = True
    leverage: int = 1
    margin_buffer_factor: float = 3.0
    lookbacks: tuple[int, ...] = (14, 21, 30, 45, 60)
    rebalance_days: int = 7
    anchor_dow: int = 3                     # Thursday

    # ── class-method constructor ──────────────────────────────────────────────

    @classmethod
    def from_dict(cls, d: dict) -> "XsmomParams":
        """Construct from a JSON-round-tripped dict. Unknown keys are ignored.
        Lists are coerced to tuples for ``universe`` and ``lookbacks``.
        """
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in d.items() if k in known}
        # JSON round-trip: lists → tuples
        if "universe" in filtered:
            filtered["universe"] = tuple(filtered["universe"])
        if "lookbacks" in filtered:
            filtered["lookbacks"] = tuple(filtered["lookbacks"])
        return cls(**filtered)

    def to_dict(self) -> dict:
        """Serialise to a JSON-compatible dict (tuples → lists)."""
        return {
            "budget_cap": self.budget_cap,
            "n_positions": self.n_positions,
            "auto": self.auto,
            "universe": list(self.universe),
            "leverage": self.leverage,
            "margin_buffer_factor": self.margin_buffer_factor,
            "lookbacks": list(self.lookbacks),
            "rebalance_days": self.rebalance_days,
            "anchor_dow": self.anchor_dow,
        }

    # Alias so callers can use either name.
    def asdict(self) -> dict:
        return self.to_dict()

    # ── sizing helpers ────────────────────────────────────────────────────────

    def compute_k(self, universe_len: int) -> int:
        """Number of legs per side (long k, short k).

        Auto mode: tercile = max(1, universe_len // 3).
        Manual mode: n_positions // 2 (n_positions is the total even count).
        Clamped to [1, universe_len // 2] so we never exceed the universe.
        """
        if self.n_positions is None:
            k = max(1, universe_len // 3)
        else:
            k = self.n_positions // 2
        # Guard: at least 1, at most half the universe
        max_k = max(1, universe_len // 2) if universe_len >= 2 else 1
        return min(max(k, 1), max_k)

    def compute_notional_per_position(self, k: int) -> float:
        """Notional per single leg.

        Budget is split equally across both sides: ``budget_cap / 2`` per side,
        divided equally among ``k`` legs per side.
        """
        return (self.budget_cap / 2.0) / k

    def compute_required_margin(self, notional: float) -> float:
        """Required USDC margin for a single leg (mirrors TwoPhaseParams math).

        margin = (notional / leverage) * margin_buffer_factor
        """
        return (notional / self.leverage) * self.margin_buffer_factor
