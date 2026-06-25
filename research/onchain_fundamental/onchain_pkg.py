"""
Harness package adapter for onchain_fundamental cross-sectional strategy.

DESIGN (mirrors crypto_pkg.py pattern):
  The whole long-short book is ONE synthetic instrument ("ONCHAIN").
  A cross-sectional book is not separable per coin (ranking is global), so
  we expose exactly one synthetic instrument whose "series" is the book's
  daily net pnl. Each menu config is a full-period portfolio pnl series.

Pipeline per config (no look-ahead, seam-safe):
  fee_panel (daily fees, t-aligned)
    → fee_growth_ensemble (signal[t] uses fees[t'] for t' <= t ONLY)
    → zscore_by_group (normalize within DeFi / Chain groups)
    → xsec.rank_to_weights (dollar-neutral tercile)
    → xsec.portfolio_returns(weights, fwd_ret, costs_bps=4.4, rebal_every=7)
    → daily net pnl pd.Series

SEAM-SAFETY: signals and pnl are precomputed on the FULL panel (lookbacks
intact). CPCV only selects rows. MAX_LOOKBACK_DAYS = 180 (2*90 for the
growth window) — the runner MUST be called with purge >= 180.

SELECTED = "ens_30_60_90": the pre-registered ensemble signal.
Menu also contains single-lookback configs (growth30, growth60, growth90)
for PBO dimension (how often does the IS-best lookback transfer OOS).

COSTS: 4.4 bps one-way (documented in project_execution_costs.md: real perp
cost on HL = taker 3.5bps confirmed + slippage ~0.9bps ≈ 4.4bps total).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).parent
_XSEC = _HERE.parent / "cross_sectional"
_CRYPTO = _XSEC / "crypto"
_HARNESS = _HERE.parent / "validation_harness"

for _p in (_XSEC, _CRYPTO, _HARNESS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import xsec
import cryptodata
from costs import Costs

from fees_data import build_fee_panel, ALL_COINS, DEFI_COINS, CHAIN_COINS
from fees_signal import (
    fee_growth,
    fee_growth_ensemble,
    zscore_by_group,
    GROWTH_LOOKBACKS,
)

# ── Constants ─────────────────────────────────────────────────────────────────
# Lookback for signal: each growth_N uses 2*N days of fee history.
# Max lookback = 2 * 90 = 180 days.
MAX_LOOKBACK_DAYS = 2 * max(GROWTH_LOOKBACKS)   # = 180

COSTS_BPS  = 4.4    # one-way cost in bps (perp taker + slippage, project_execution_costs.md)
REBAL_EVERY = 7     # weekly rebalance (standard for cross-sectional crypto)

SELECTED = "ens_30_60_90"  # pre-registered ensemble (PLAN §Signal design)


# ── Strategy adapter ──────────────────────────────────────────────────────────

class _OnchainStrategy:
    """Harness contract adapter. FIXED config — fit() returns None.

    simulate() returns the precomputed full-period pnl sliced to the CPCV
    test segment (positional slice, seam-safe: pnl was built on full panel).
    """

    def __init__(self, name: str, pnl: pd.Series):
        self.name = name
        self._pnl = pnl

    def fit(self, df: pd.DataFrame, train_idx: np.ndarray, costs: Costs):
        return None  # fixed config, no in-sample selection

    def simulate(self, df: pd.DataFrame, seg: slice, config, costs: Costs) -> np.ndarray:
        # df.index == pnl.index (same panel dates) → positional slice
        return self._pnl.values[seg]


# ── Package ───────────────────────────────────────────────────────────────────

class OnchainFundamentalPackage:
    """Package protocol for validation_harness.

    One synthetic coin "ONCHAIN", menu = {growth30, growth60, growth90, ens_30_60_90}.
    SELECTED = ens_30_60_90 (pre-registered ensemble).

    Parameters
    ----------
    coins : coin list to load (default = ALL_COINS from fees_data). Override
        for testing with a smaller subset.
    rebal_every : days between rebalances (default 7 = weekly).
    costs_bps : one-way cost in bps (default 4.4).
    refresh : whether to re-fetch DefiLlama data (default False).
    """

    name = "Onchain Fundamental Momentum (fee-growth cross-sectional)"
    selected_name = SELECTED
    coins = ["ONCHAIN"]  # one synthetic instrument

    def __init__(
        self,
        *,
        coin_list: list[str] | None = None,
        rebal_every: int = REBAL_EVERY,
        costs_bps: float = COSTS_BPS,
        refresh: bool = False,
    ):
        self._coin_list = coin_list or ALL_COINS
        self.rebal_every = rebal_every
        self.costs_bps = costs_bps
        self._refresh = refresh

        self._fee_panel: pd.DataFrame | None = None
        self._price_panel: dict | None = None
        self._menu_cache: dict[str, pd.Series] | None = None

    # ── Data loading ───────────────────────────────────────────────────────────

    def _fees(self) -> pd.DataFrame:
        if self._fee_panel is None:
            print(f"  Loading fee panel for {len(self._coin_list)} coins...")
            self._fee_panel = build_fee_panel(coins=self._coin_list,
                                              refresh=self._refresh)
        return self._fee_panel

    def _prices(self, coins: list[str]) -> dict:
        """Load price/fwd_ret for coins that exist in cryptodata."""
        if self._price_panel is None:
            # Only load coins available in cryptodata (some fundamental coins
            # may not be on HL; skip gracefully)
            available = []
            for c in coins:
                try:
                    cryptodata.load_panel([c])
                    available.append(c)
                except Exception:
                    pass
            if available:
                self._price_panel = cryptodata.load_panel(available)
            else:
                raise RuntimeError("No coins available in cryptodata")
        return self._price_panel

    # ── Signal + pnl per config ────────────────────────────────────────────────

    def _config_pnl(self, signal: pd.DataFrame, fwd_ret: pd.DataFrame) -> pd.Series:
        """signal panel → weights → net pnl series."""
        w = xsec.rank_to_weights(signal)
        return xsec.portfolio_returns(
            w, fwd_ret,
            costs_bps=self.costs_bps,
            rebal_every=self.rebal_every,
        )

    def _build_menu(self) -> dict[str, pd.Series]:
        """Build full-period pnl for each menu config (no look-ahead)."""
        if self._menu_cache is not None:
            return self._menu_cache

        fee_panel = self._fees()

        # Determine coins that have BOTH fee data and price data
        fee_coins = list(fee_panel.columns)
        price_data = self._prices(fee_coins)
        price_coins = price_data["coins"]
        common_coins = [c for c in fee_coins if c in price_coins]

        if not common_coins:
            raise RuntimeError("No coins with both fee and price data")

        print(f"  Common coins (fee+price): {common_coins}")

        fee_sub = fee_panel[common_coins]
        fwd_ret = price_data["fwd_ret"][common_coins]

        # Align fee panel to price panel's daily index (forward-return index)
        # NOTE: fee_panel may have different date range than price panel.
        # We align to the intersection to avoid NaN-filled edges.
        shared_idx = fee_sub.index.intersection(fwd_ret.index)
        fee_sub = fee_sub.reindex(shared_idx)
        fwd_ret = fwd_ret.reindex(shared_idx)

        # Defi and chain subsets within common coins
        defi_sub  = [c for c in common_coins if c in DEFI_COINS]
        chain_sub = [c for c in common_coins if c in CHAIN_COINS]

        menu: dict[str, pd.Series] = {}

        # Single-lookback configs (for PBO dimension)
        for lb in GROWTH_LOOKBACKS:
            raw = fee_growth(fee_sub, lb)
            z   = zscore_by_group(raw, defi_coins=defi_sub, chain_coins=chain_sub)
            menu[f"growth{lb}"] = self._config_pnl(z, fwd_ret)

        # Ensemble (pre-registered selected config)
        raw_ens = fee_growth_ensemble(fee_sub, lookbacks=GROWTH_LOOKBACKS)
        z_ens   = zscore_by_group(raw_ens, defi_coins=defi_sub, chain_coins=chain_sub)
        menu[SELECTED] = self._config_pnl(z_ens, fwd_ret)

        self._menu_cache = menu
        self._common_coins = common_coins
        self._fee_sub = fee_sub
        self._fwd_ret = fwd_ret
        return menu

    # ── Package protocol ───────────────────────────────────────────────────────

    def load(self, coin: str) -> pd.DataFrame:
        """Holder frame: index = panel dates (drives CPCV time-splitting)."""
        menu = self._build_menu()
        idx = menu[SELECTED].index
        return pd.DataFrame({"close": menu[SELECTED].values}, index=idx)

    def selected(self, coin: str, df: pd.DataFrame) -> _OnchainStrategy:
        return _OnchainStrategy(self.selected_name, self._build_menu()[SELECTED])

    def menu(self, coin: str, df: pd.DataFrame) -> dict[str, pd.Series]:
        return dict(self._build_menu())

    # ── Convenience accessors (for run_onchain.py) ─────────────────────────────

    def book_pnl(self) -> pd.Series:
        """Full-period PnL of the selected config (for orthogonality gate)."""
        return self._build_menu()[SELECTED]

    def fee_panel_used(self) -> pd.DataFrame:
        """Fee panel used (after alignment with price data)."""
        self._build_menu()  # ensure built
        return self._fee_sub

    def common_coins_used(self) -> list[str]:
        """Coins used (have both fee data and price data)."""
        self._build_menu()
        return self._common_coins

    def per_date_valid_count(self) -> pd.Series:
        """Number of valid coins per date in the fee panel."""
        self._build_menu()
        return self._fee_sub.notna().sum(axis=1)


# ── Self-test (fast, no network) ──────────────────────────────────────────────

if __name__ == "__main__":
    print("=== onchain_pkg self-test (instantiation only, no network) ===")
    pkg = OnchainFundamentalPackage.__new__(OnchainFundamentalPackage)
    pkg.name = OnchainFundamentalPackage.name
    pkg.selected_name = OnchainFundamentalPackage.selected_name
    pkg.coins = OnchainFundamentalPackage.coins
    pkg._refresh = False
    pkg._coin_list = ALL_COINS
    pkg.rebal_every = REBAL_EVERY
    pkg.costs_bps = COSTS_BPS
    pkg._fee_panel = None
    pkg._price_panel = None
    pkg._menu_cache = None
    print(f"  name: {pkg.name}")
    print(f"  selected: {pkg.selected_name}")
    print(f"  MAX_LOOKBACK_DAYS: {MAX_LOOKBACK_DAYS}")
    print(f"  costs_bps: {pkg.costs_bps}  rebal_every: {pkg.rebal_every}")
    print("  self-test PASSED (instantiation ok)")
