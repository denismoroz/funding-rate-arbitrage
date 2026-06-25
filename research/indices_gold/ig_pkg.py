"""
TSMOM Indices+Gold package for the validation harness.

DESIGN — mirrors fx_pkg.FXXSecPackage exactly.  The whole TSMOM book is ONE
synthetic "coin" ("IGTREND").  TSMOM is a time-series factor: each asset's
position depends ONLY on its own trailing trend + vol (not cross-sectional
ranking), so the 13-asset portfolio is still ONE book (aggregated pnl series).

Pipeline per config (no look-ahead, seam-safe — see signals.py):
  tsmom weights (signals.tsmom / tsmom_ensemble / xs_mom)
    → xsec.portfolio_returns(weights, fwd_ret, costs_bps, rebal_every)
  → one daily pnl pd.Series indexed by the panel dates.

SEAM-SAFETY (CRITICAL, same rationale as fx_pkg):
  Signals & portfolio pnl are precomputed on the FULL panel (lookbacks intact);
  CPCV only SELECTS rows (positional slice).  The deepest menu lookback is
  12 months = 12*21 = 252 business days, so MAX_LOOKBACK_DAYS = 252 and
  purge >= 252 is required in run_ig.py.

TSMOM IS NOT DOLLAR-NEUTRAL.  The book takes net-long positions when most assets
  trend up and net-short when they trend down.  We do NOT force net-zero.  We just
  normalize gross leverage to ~1.0 (div by n_valid in signals.tsmom) for cost
  comparability.  This is the DEFINING difference vs the FX cross-sectional book.

  accrual = None.  PURE SPOT TREND — no carry/financing.
  Thesis: the equity-index trend edge does NOT depend on broker swap / funding
  (unlike FX carry).  Index futures roll is ~riskfree−dividend; ETF spot is zero-
  carry.  For a research-level daily price study of the DIRECTION of the trend,
  accrual terms are small relative to price-trend returns and are intentionally
  omitted to keep the thesis clean and to avoid look-ahead in accrual estimation.

FX-SPECIFIC CONFIG CHOICES (documented):
  rebal_every = 21 (≈ monthly, same as FX).  TSMOM is a slow factor; weekly or
    daily rebal adds cost without signal improvement (Moskowitz et al.  rebalance
    monthly).
  costs_bps = 2.0 one-way per leg.  Index futures / ETFs are extremely liquid.
    2 bps is generous (SPY bid-ask is sub-1bp; index futures sub-0.5bp).
  MAX_LOOKBACK_DAYS = 12*21 = 252 (the longest menu lookback: 12-month TSMOM).

DAILY-vs-HOURLY ANNUALIZATION CAVEAT (same as run_fx.py):
  The harness annualizes √8760 (hourly book assumption).  Our book is DAILY.
  Sharpe / calmar / annual_pct from the harness are inflated ~5.9×.  The verdicts
  (DSR, PBO) are period-agnostic and correct as-is.  For honest daily levels see
  the self-test below (using metrics_daily.daily_metrics, √252).

SELECTED = "tsmom_ens" (FIXED equal-lookback-blend, no in-sample selection).
MENU = tsmom3, tsmom6, tsmom12, tsmom_ens, xs_mom.

Only numpy/pandas (+ igdata/signals/xsec).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

import igdata
import signals as sig
import xsec

try:
    from costs import Costs, TAKER
except ImportError:
    # Running standalone (self-test): mock Costs so the import chain completes
    class Costs:  # type: ignore
        pass
    TAKER = Costs()  # type: ignore

_HERE = Path(__file__).parent

# ── Config ────────────────────────────────────────────────────────────────────
MAX_LOOKBACK_DAYS = 12 * sig.MONTH   # = 252 business days → purge >= 252

REBAL_EVERY = 21     # ≈ monthly (business days)
COSTS_BPS   = 2.0   # one-way per leg (conservative for liquid index ETF/futures)

SELECTED    = "tsmom_ens"
MENU_NAMES  = ("tsmom3", "tsmom6", "tsmom12", "tsmom_ens", "xs_mom")


class _IGTrendStrategy:
    """Adapter for the harness contract (mirror of fx_pkg._XSecStrategy).

    SELECTED book (tsmom_ens) is a FIXED config — fit() returns None (no
    in-sample selection). simulate() returns the precomputed full-period pnl
    sliced to the contiguous CPCV segment. Slicing (not recomputing) makes this
    seam-safe.
    """

    def __init__(self, name: str, sel_pnl: pd.Series):
        self.name = name
        self._sel_pnl = sel_pnl

    def fit(self, df: pd.DataFrame, train_idx: np.ndarray, costs: "Costs"):
        return None  # selected is fixed → nothing to choose

    def simulate(self, df: pd.DataFrame, seg: slice, config, costs: "Costs") -> np.ndarray:
        # df index == sel_pnl index (same panel dates) → positional slice aligns.
        return self._sel_pnl.values[seg]


class IGTrendPackage:
    """Package protocol: one synthetic coin "IGTREND", menu = TSMOM configs.

    Parameters
    ----------
    rebal_every : hold weights this many business days between rebalances.
        DEFAULT 21 (≈ MONTHLY).
    costs_bps : one-way cost in basis points per unit of turnover.
        DEFAULT 2.0 — conservative liquid-index / ETF spread per leg.
    costs : kept for signature parity with harness; costs are baked into the
        precomputed pnl via costs_bps, not via this Costs object.
    refresh : if True, re-fetch raw CSV data on first load.
    """

    name          = "TSMOM indices + gold (spot trend, vol-scaled, 2bps/leg)"
    selected_name = SELECTED
    coins         = ["IGTREND"]

    def __init__(self, *, rebal_every: int = REBAL_EVERY, costs: "Costs" = None,
                 costs_bps: float = COSTS_BPS, refresh: bool = False):
        self.rebal_every = rebal_every
        self._costs      = costs if costs is not None else TAKER
        self.costs_bps   = costs_bps
        self._refresh    = refresh
        self._panel: dict | None       = None
        self._menu_cache: dict | None  = None

    # ── panel ─────────────────────────────────────────────────────────────────
    def _panels(self) -> dict:
        if self._panel is None:
            self._panel = igdata.load_panel(refresh=self._refresh)
        return self._panel

    def _config_pnl_from_weights(self, weights: pd.DataFrame) -> pd.Series:
        """Weights → net portfolio pnl series (no accrual — pure spot trend)."""
        P = self._panels()
        return xsec.portfolio_returns(
            weights, P["fwd_ret"],
            costs_bps=self.costs_bps,
            rebal_every=self.rebal_every,
            accrual=None,   # PURE SPOT TREND: no carry/financing accrual
        )

    def _build_menu(self) -> dict[str, pd.Series]:
        if self._menu_cache is not None:
            return self._menu_cache
        P = self._panels()
        price = P["price"]

        menu: dict[str, pd.Series] = {}

        # Individual TSMOM lookbacks
        for lb, nm in [(3, "tsmom3"), (6, "tsmom6"), (12, "tsmom12")]:
            w = sig.tsmom(price, lookback_months=lb)
            menu[nm] = self._config_pnl_from_weights(w)

        # Ensemble (3+6+12)
        w_ens = sig.tsmom_ensemble(price, lookbacks=(3, 6, 12))
        menu["tsmom_ens"] = self._config_pnl_from_weights(w_ens)

        # Cross-sectional momentum (dollar-neutral, rank-based)
        xs_score = sig.xs_momentum(price, lookback_months=12, skip_months=1)
        xs_w     = xsec.rank_to_weights(xs_score)
        menu["xs_mom"] = xsec.portfolio_returns(
            xs_w, P["fwd_ret"],
            costs_bps=self.costs_bps,
            rebal_every=self.rebal_every,
            accrual=None,
        )

        self._menu_cache = menu
        return menu

    # ── Package protocol ──────────────────────────────────────────────────────
    def load(self, coin: str) -> pd.DataFrame:
        """Holder frame whose INDEX (panel dates) drives CPCV time-splitting."""
        menu = self._build_menu()
        idx = menu[SELECTED].index
        return pd.DataFrame({"close": menu[SELECTED].values}, index=idx)

    def selected(self, coin: str, df: pd.DataFrame) -> _IGTrendStrategy:
        return _IGTrendStrategy(self.selected_name, self._build_menu()[SELECTED])

    def menu(self, coin: str, df: pd.DataFrame) -> dict[str, pd.Series]:
        return dict(self._build_menu())

    def book_pnl(self) -> pd.Series:
        """Full-period tsmom_ens pnl (for orthogonality check in run_ig.py)."""
        return self._build_menu()[SELECTED]


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    # Import daily_metrics from crypto/ (shared helper, not edited)
    sys.path.insert(0, str(_HERE.parent / "cross_sectional" / "crypto"))
    from metrics_daily import daily_metrics

    pkg = IGTrendPackage()
    print(f"rebal_every={pkg.rebal_every}d  costs_bps={pkg.costs_bps:.2f}/leg  "
          f"MAX_LOOKBACK_DAYS={MAX_LOOKBACK_DAYS}")
    df = pkg.load("IGTREND")
    m  = pkg.menu("IGTREND", df)
    print(f"panel days: {len(df)}  {df.index.min().date()} -> {df.index.max().date()}")
    print(f"menu configs ({len(m)}): {sorted(m)}")
    assert pkg.selected_name in m, "selected must be a menu config"
    assert set(m) == set(MENU_NAMES), f"menu mismatch: {sorted(m)} != {sorted(MENU_NAMES)}"

    print(f"\n{'config':<12}{'mean/day':>11}{'ann':>10}{'sharpe':>9}"
          f"{'maxDD':>9}{'calmar':>9}{'hit':>7}{'nan':>7}")
    for nm in MENU_NAMES:
        s = m[nm]
        assert s.index.equals(df.index), f"{nm} index != panel index"
        dm = daily_metrics(s)
        if not dm:
            print(f"  {nm:<10}  (too short for daily_metrics)")
            continue
        print(f"  {nm:<10}{s.mean():>+11.6f}{dm['ann']:>+9.2%}{dm['sharpe']:>9.2f}"
              f"{dm['maxdd']:>9.2%}{dm['calmar']:>9.2f}{dm['hit']:>7.2%}"
              f"{int(s.isna().sum()):>7d}")
    print("\n(daily_metrics = honest √252 bday annualization; NOT the harness √8760)")
    print("self-test passed.")
