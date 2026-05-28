"""MarginManager — pure-logic margin policy decisions.

No I/O, no async, no external calls. All methods are deterministic
given their inputs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Exchange fee constants
PERP_TAKER = 0.00035
SPOT_TAKER = 0.00070


@dataclass(frozen=True)
class PerCoinSpec:
    """Per-coin trading parameters."""

    position_size_usd: float
    leverage: int
    maint_ratio: float


@dataclass(frozen=True)
class OpenPosition:
    """Snapshot of one open spot+perp pair."""

    coin: str
    spot_qty: float          # absolute, in coin units
    short_size: float        # absolute, in coin units
    perp_entry: float        # USDC per coin at perp entry
    required_margin: float   # USDC reserved at open


class MarginManager:
    """Computes margin policy decisions for spot+short-perp pairs.

    All methods are pure functions — no state mutation, no I/O.
    """

    def __init__(
        self,
        per_coin_params: dict[str, PerCoinSpec],
        margin_buffer_x: float,
        top_up_trigger: float,
        healthy_ratio: float,
        budget_cap_usd: float,
    ) -> None:
        if not per_coin_params:
            raise ValueError("per_coin_params must not be empty")
        for coin, spec in per_coin_params.items():
            if not isinstance(spec, PerCoinSpec):
                raise ValueError(
                    f"per_coin_params[{coin!r}] must be a PerCoinSpec, got {type(spec)}"
                )
            if spec.leverage < 1:
                raise ValueError(
                    f"per_coin_params[{coin!r}].leverage must be >= 1, got {spec.leverage}"
                )
            if spec.maint_ratio <= 0:
                raise ValueError(
                    f"per_coin_params[{coin!r}].maint_ratio must be > 0, got {spec.maint_ratio}"
                )
            if spec.position_size_usd <= 0:
                raise ValueError(
                    f"per_coin_params[{coin!r}].position_size_usd must be > 0, "
                    f"got {spec.position_size_usd}"
                )

        if margin_buffer_x < 1.0:
            raise ValueError(
                f"margin_buffer_x must be >= 1.0, got {margin_buffer_x}"
            )
        if not (top_up_trigger > 1.0 and top_up_trigger < healthy_ratio):
            raise ValueError(
                f"top_up_trigger must satisfy 1.0 < top_up_trigger < healthy_ratio "
                f"({healthy_ratio}), got {top_up_trigger}"
            )
        if budget_cap_usd <= 0:
            raise ValueError(
                f"budget_cap_usd must be > 0, got {budget_cap_usd}"
            )

        self._params = per_coin_params
        self.margin_buffer_x = margin_buffer_x
        self.top_up_trigger = top_up_trigger
        self.healthy_ratio = healthy_ratio
        self.budget_cap_usd = budget_cap_usd

    # ------------------------------------------------------------------
    # Footprint helpers
    # ------------------------------------------------------------------

    def compute_pair_footprint(self, coin: str) -> tuple[float, float]:
        """Return (spot_cost_usd, perp_margin_usd) for opening one pair.

        perp_margin includes the margin_buffer_x safety multiplier.
        """
        spec = self._params[coin]
        spot_cost = spec.position_size_usd
        perp_margin = spec.position_size_usd / spec.leverage * self.margin_buffer_x
        return spot_cost, perp_margin

    def compute_required_margin_for_open(self, coin: str) -> float:
        """Return the perp margin (with buffer) required to open this coin."""
        _, perp_margin = self.compute_pair_footprint(coin)
        return perp_margin

    def position_size_for(self, coin: str) -> float:
        """Spot leg notional size for opening this coin's pair."""
        return self._params[coin].position_size_usd

    # ------------------------------------------------------------------
    # Mark-to-market helpers
    # ------------------------------------------------------------------

    def compute_total_maintenance(
        self,
        opens: list[OpenPosition],
        current_prices: dict[str, float],
    ) -> float:
        """Sum of maintenance margin across all open shorts.

        Raises KeyError if a coin is missing from current_prices or
        per_coin_params.
        """
        total = 0.0
        for pos in opens:
            price = current_prices[pos.coin]  # raises KeyError if missing
            spec = self._params[pos.coin]     # raises KeyError if missing
            total += pos.short_size * price * spec.maint_ratio
        return total

    def compute_perp_unrealized(
        self,
        opens: list[OpenPosition],
        current_prices: dict[str, float],
    ) -> float:
        """Sum of unrealized PnL on perp shorts.

        Short PnL = size * (entry_price - current_price).
        Price rise => negative unrealized (loss for short).

        Raises KeyError if a coin is missing from current_prices.
        """
        total = 0.0
        for pos in opens:
            price = current_prices[pos.coin]  # raises KeyError if missing
            total += pos.short_size * (pos.perp_entry - price)
        return total

    def compute_margin_ratio(
        self,
        perp_cash: float,
        opens: list[OpenPosition],
        current_prices: dict[str, float],
    ) -> float:
        """Account margin ratio: (perp_cash + unrealized) / maintenance.

        Returns float("inf") when maintenance is zero (no open positions
        or degenerate params).
        """
        maintenance = self.compute_total_maintenance(opens, current_prices)
        if maintenance == 0.0:
            return float("inf")
        unrealized = self.compute_perp_unrealized(opens, current_prices)
        return (perp_cash + unrealized) / maintenance

    # ------------------------------------------------------------------
    # Budget helpers
    # ------------------------------------------------------------------

    def compute_budget_committed(
        self,
        opens: list[OpenPosition],
        perp_cash: float,
    ) -> float:
        """Total capital committed: entry-value spot + perp_cash.

        Uses entry position_size_usd for spot (not MTM) so the budget
        check is stable and doesn't fluctuate with price.
        """
        spot_total = sum(
            self._params[pos.coin].position_size_usd for pos in opens
        )
        return spot_total + perp_cash

    def can_open(
        self,
        coin: str,
        spot_cash: float,
        opens: list[OpenPosition],
        perp_cash: float,
    ) -> tuple[bool, Optional[str]]:
        """Check whether opening this coin pair is feasible.

        Returns (True, None) on success or (False, reason) on failure.
        Checks:
          1. coin is a known coin in per_coin_params
          2. coin not already open
          3. spot_cash covers spot cost + spot taker fee + required margin
          4. new committed budget <= budget_cap_usd
        """
        if coin not in self._params:
            return False, f"unknown coin: {coin!r}"

        open_coins = {pos.coin for pos in opens}
        if coin in open_coins:
            return False, f"{coin} is already open"

        spec = self._params[coin]
        spot_cost = spec.position_size_usd
        required_margin = self.compute_required_margin_for_open(coin)
        spot_fee = SPOT_TAKER * spot_cost

        needed_spot_cash = spot_cost + spot_fee + required_margin
        if spot_cash < needed_spot_cash:
            return (
                False,
                f"insufficient spot_cash: need {needed_spot_cash:.4f}, have {spot_cash:.4f}",
            )

        committed = self.compute_budget_committed(opens, perp_cash)
        new_committed = committed + spot_cost + required_margin
        if new_committed > self.budget_cap_usd:
            return (
                False,
                f"budget exceeded: committed {new_committed:.4f} > cap {self.budget_cap_usd:.4f}",
            )

        return True, None

    # ------------------------------------------------------------------
    # Selection helpers
    # ------------------------------------------------------------------

    def select_weakest_for_close(
        self,
        opens: list[OpenPosition],
        signals: dict[str, float],
    ) -> Optional[str]:
        """Return the coin with the lowest signal value, or None if no opens.

        Raises KeyError if any open coin is missing from signals.
        """
        if not opens:
            return None
        # Validate all coins present before sorting (fail-fast, no partial result)
        for pos in opens:
            if pos.coin not in signals:
                raise KeyError(
                    f"signal missing for open coin {pos.coin!r}"
                )
        return min(opens, key=lambda p: signals[p.coin]).coin

    # ------------------------------------------------------------------
    # Top-up helper
    # ------------------------------------------------------------------

    def compute_top_up_amount(
        self,
        perp_cash: float,
        total_maintenance: float,
    ) -> float:
        """Amount of additional cash needed to reach the healthy margin ratio.

        Returns 0 if perp_cash already satisfies the healthy ratio.
        """
        target = self.healthy_ratio * total_maintenance
        return max(0.0, target - perp_cash)
