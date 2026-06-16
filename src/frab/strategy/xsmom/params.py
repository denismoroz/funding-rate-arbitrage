"""XsmomParams — tunable parameters for the XSMOM cross-sectional momentum strategy."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# Default candidate universe = the frozen, backtest-validated HL set
# (research/cross_sectional/crypto/universe.json, snapshot 2026-06-12, 34 coins:
# vol≥$1M, ≥547d history, fresh ≤5d). Starting on the exact validated universe keeps
# live == backtest risk. Operators narrow/override it via the UI (PATCH /api/xsmom/params).
DEFAULT_XSMOM_UNIVERSE: tuple[str, ...] = (
    "AAVE", "ADA", "APT", "ARB", "ATOM", "AVAX", "BCH", "BNB", "BTC", "CRV",
    "DOGE", "DOT", "EIGEN", "ENA", "ETH", "HMSTR", "INJ", "JTO", "JUP", "LINK",
    "LTC", "NEAR", "PENDLE", "PYTH", "SOL", "SUI", "TAO", "TON", "TRX", "UNI",
    "WLD", "XLM", "XRP", "ZRO",
)


@dataclass(frozen=True)
class XsmomParams:
    """All tunable parameters for XsmomStrategy.

    ``n_positions`` is the *total* even count of long+short legs (e.g. 6 → 3 long, 3 short).
    Set to None to use the auto tercile rule: k = max(1, universe_len // 3) per side.
    """

    budget_cap: float = 1000.0
    universe: tuple[str, ...] = DEFAULT_XSMOM_UNIVERSE   # backtest-validated default; edit via UI
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

    def sizing_breakdown(
        self,
        universe_len: int,
        wallet: float | None,
    ) -> dict:
        """Compute the full envelope sizing breakdown.

        This is the SINGLE SOURCE OF TRUTH for position sizing.  Both the live
        rebalancer and the preview API must call this method so the two can
        never drift apart.

        Parameters
        ----------
        universe_len:
            Number of coins currently in the candidate universe.
        wallet:
            Live USDC spot balance of the XSMOM wallet.  When None (pure param
            preview without a live balance) ``effective`` is set to
            ``budget_cap`` and ``free`` is returned as None.

        Returns
        -------
        dict with keys:
            reserve       — USDC kept as safety buffer (not deployed).
            effective     — min(budget_cap, wallet); the capital actually at play.
            book          — gross notional deployed (effective - reserve).
            per_side      — book / 2  (== long notional == short notional).
            long          — alias for per_side.
            short         — alias for per_side.
            k_requested   — compute_k(universe_len) before min-leg clamp.
            k             — actual legs per side (possibly reduced by min-leg floor).
            per_leg       — notional per individual position.
            min_leg       — hard minimum per-leg notional (~HL $10 min + slippage).
            min_leg_ok    — False when book is too small for even k=1 valid leg.
            free          — wallet - book when wallet is known, else None.
        """
        _MIN_LEG = 12.0  # HL ~$10 min order + slippage buffer

        # ── capital envelope ──────────────────────────────────────────────────
        reserve = max(20.0, 0.08 * self.budget_cap)
        effective = min(self.budget_cap, wallet) if wallet is not None else self.budget_cap
        book = max(0.0, effective - reserve)
        per_side = book / 2.0

        # ── k with min-leg clamp ──────────────────────────────────────────────
        k_req = self.compute_k(universe_len)
        max_k = math.floor(per_side / _MIN_LEG) if per_side > 0 else 0
        if max_k >= 1:
            k = max(1, min(k_req, max_k))
        else:
            k = 1  # clamp to 1 even if book too small; min_leg_ok will flag it

        per_leg = per_side / k if k > 0 else 0.0
        min_leg_ok = per_leg >= _MIN_LEG

        # ── free capital ──────────────────────────────────────────────────────
        free: float | None = None
        if wallet is not None:
            free = max(0.0, wallet - book)

        return {
            "reserve": reserve,
            "effective": effective,
            "book": book,
            "per_side": per_side,
            "long": per_side,
            "short": per_side,
            "k_requested": k_req,
            "k": k,
            "per_leg": per_leg,
            "min_leg": _MIN_LEG,
            "min_leg_ok": min_leg_ok,
            "free": free,
        }
