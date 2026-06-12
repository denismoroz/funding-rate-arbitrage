"""
Cross-sectional G10 FX long-short package for the validation harness.

DESIGN — mirror of crypto/crypto_pkg.CryptoXSecPackage. The whole long-short book
is ONE synthetic "coin" ("XSEC"). A cross-sectional book is a SINGLE portfolio:
its return at t depends on the RANKING across all currencies at t, so it is NOT
separable per currency. The harness has a per-coin model, so we adapt by exposing
exactly one synthetic instrument whose "price series" is the book's daily net pnl.
Each menu config is a full-period portfolio pnl series (one factor → one column);
_portfolio_menu over one coin then yields the (T × n_config) matrix directly
(PBO across factors, DSR on the selected column).

Pipeline per config (no look-ahead, seam-safe — see signals.py / xsec.py):
  score panel (signals)            # uses data <= t only (carry/mom/value/blend_fx)
    → xsec.rank_to_weights         # dollar-neutral long top / short bottom tercile
    → xsec.portfolio_returns(weights, fwd_ret, costs_bps, rebal_every)
                                   # fwd_ret[t] = t→t+1 realised return (forward)
  → one daily pnl pd.Series indexed by the panel dates.

SEAM-SAFETY (CRITICAL, same rationale as crypto_pkg): signals & portfolio pnl are
precomputed on the FULL panel (lookbacks intact); CPCV only SELECTS rows. The
signal that produced a test-day return used prices up to MAX_LOOKBACK_DAYS earlier
(value's 5y REER change = 1260 business days), so the runner MUST be called with
purge >= MAX_LOOKBACK_DAYS to keep those source bars out of train. We expose
MAX_LOOKBACK_DAYS for run_fx.py.

SELECTED is the FIXED equal-weight multi-factor blend ("blend_fx" = z-carry +
z-mom(12-1) + z-value(5y)). fit() does nothing: NO in-sample factor selection in
the OOS path — that keeps the CPCV OOS honest (the PBO dimension is what tests
whether picking the best menu config transfers).

FX-SPECIFIC CONFIG CHOICES (documented, all sensitivity knobs):

  rebal_every = 21 (≈ MONTHLY, business days).  FX factors are SLOW: carry is a
    3M-rate differential, value is a 5y REER change, momentum is 12-1. They move
    on a monthly-or-slower cadence; daily rebal would only churn the dollar-neutral
    book and pay turnover cost on noise. Monthly (≈21 bdays) is the standard
    cross-asset cadence (AQR "Value and Momentum Everywhere" rebalances monthly).
    This is the FX analog of crypto's WEEKLY default — exposed as a knob.

  costs_bps = 2.0 one-way per leg.  G10 SPOT FX is far more liquid than crypto
    perps; a 2 bps one-way spread is conservative for the majors (EUR/JPY/GBP),
    with NOK/SEK a touch wider — 2 bps is a single blended assumption, not a
    per-pair model. This is a SPOT-FX SPREAD assumption, explicitly NOT the crypto
    HL perp TAKER (8.5 bps). xsec.portfolio_returns charges turnover * costs_bps/1e4
    at each rebalance, and turnover counts BOTH legs (Σ|Δw| over the dollar-neutral
    book), so a one-way per-leg cost is the right unit.

    CAVEAT — UNMODELLED HELD-POSITION CARRY FLOW (the FX analog of crypto's
    unmodelled perp funding; F3 MUST flag this): like crypto, the book does NOT
    model the ongoing carry/funding *flow* accruing on held positions inside
    portfolio_returns — only TURNOVER cost. In FX the interest-rate-differential
    carry IS the held-position cash flow (you earn/pay the rate diff every day you
    hold the position), and it is NOT captured here as a daily accrual. The carry
    FACTOR captures the rate differential only as a cross-sectional *signal* (rank
    high-yielders long), NOT as a realised held cash flow added to pnl. So the
    book's pnl is spot-FX price return minus turnover cost — the carry/funding
    accrual is omitted on BOTH the long and short legs. F3 must surface that the
    realised FX-carry edge is therefore understated here, just as crypto's perp
    funding accrual was omitted.

  MAX_LOOKBACK_DAYS = 1260.  The deepest menu lookback in business days:
    momentum 12-1 = 12*21 = 252; value 5y = 5*252 = 1260; carry = 0 (point-in-time
    rate diff). max = 1260 → purge >= 1260. NB this is HEAVY: the panel is ~5240
    business days, so purge=1260 is ~24% of the series PER test-group side, far
    heavier than crypto's 90/1101≈8%. run_fx.py surfaces the resulting purge
    tension and prints realized per-split train/test sizes.

Only numpy/pandas (+ xsec/signals/fxdata).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

import fxdata
import signals
import xsec
from costs import Costs, TAKER

_HERE = Path(__file__).parent

# ── Menu lookbacks (business days). MAX drives purge (seam-safety lower bound). ──
MOM_LOOKBACK_MONTHS = 12          # momentum 12-1 (canonical cross-asset)
MOM_SKIP_MONTHS = 1
VALUE_LOOKBACK_YEARS = 5          # value = 5y REER change (AMP/AQR currency value)
MOM_LB_DAYS = MOM_LOOKBACK_MONTHS * signals.MONTH    # 252
VALUE_LB_DAYS = VALUE_LOOKBACK_YEARS * signals.YEAR  # 1260
MAX_LOOKBACK_DAYS = max(MOM_LB_DAYS, VALUE_LB_DAYS)  # = 1260 → purge >= 1260

# FX-specific defaults (see module docstring for the reasoning).
REBAL_EVERY = 21                  # ≈ monthly (business days)
COSTS_BPS = 2.0                   # spot-FX one-way spread per leg (NOT crypto taker)

SELECTED = "blend_fx"
MENU_NAMES = ("carry", "momentum", "value", "blend_fx")


class _XSecStrategy:
    """Adapter for the harness contract (mirror of crypto _XSecStrategy).

    The selected book (blend_fx) is a FIXED config — fit() returns None (no
    in-sample selection). simulate() returns the precomputed full-period blend_fx
    pnl sliced to the contiguous CPCV segment. Slicing (not recomputing) is what
    makes this seam-safe: the pnl was built once on the full panel with lookbacks
    intact; CPCV only chooses which contiguous rows form this OOS segment.
    """

    def __init__(self, name: str, sel_pnl: pd.Series):
        self.name = name
        self._sel_pnl = sel_pnl

    def fit(self, df: pd.DataFrame, train_idx: np.ndarray, costs: Costs):
        return None  # selected is fixed → nothing to choose

    def simulate(self, df: pd.DataFrame, seg: slice, config, costs: Costs) -> np.ndarray:
        # df index == sel_pnl index (same panel dates) → positional slice aligns.
        return self._sel_pnl.values[seg]


class FXXSecPackage:
    """Package protocol: one synthetic coin "XSEC", menu = FX factor configs.

    Parameters
    ----------
    rebal_every : hold weights this many business days between rebalances.
        DEFAULT 21 (≈ MONTHLY). FX factors are slow (rate diff / 5y REER / 12-1
        momentum); daily rebal over-trades. Exposed so sensitivity can be probed.
    costs_bps : one-way cost in basis points per unit of turnover applied by
        xsec.portfolio_returns. DEFAULT 2.0 — a conservative G10 SPOT-FX spread
        per leg, NOT the crypto perp taker. portfolio_returns charges
        turnover * costs_bps/1e4 at each rebalance; turnover counts both legs.
    costs : kept for signature parity with CryptoXSecPackage / harness; FX costs
        are driven by costs_bps, not by this Costs object (the xsec adapter's
        simulate returns precomputed pnl, so the harness `costs` arg does not bake
        FX costs — costs_bps does).
    """

    name = "FX cross-sectional long-short (carry + momentum + value)"
    selected_name = SELECTED
    coins = ["XSEC"]

    def __init__(self, *, rebal_every: int = REBAL_EVERY, costs: Costs = TAKER,
                 costs_bps: float | None = None):
        self.rebal_every = rebal_every
        self._costs = costs
        self.costs_bps = COSTS_BPS if costs_bps is None else costs_bps
        self._panel: dict | None = None
        self._menu_cache: dict[str, pd.Series] | None = None

    # ── panel & signals ─────────────────────────────────────────────────────────
    def _panels(self) -> dict:
        if self._panel is None:
            self._panel = fxdata.load_panel()
        return self._panel

    def _score_panels(self) -> dict[str, pd.DataFrame]:
        """Raw factor score panels per single-factor menu config name.

        carry / momentum(12-1) / value(5y) are scale-free for RANKING, so the
        single-factor menu configs rank the RAW score (z-scoring before ranking is
        a no-op — rank is scale-invariant). blend_fx z-scores its three legs
        internally (signals.blend_fx) so the factors are comparable before
        averaging.
        """
        P = self._panels()
        return {
            "carry": signals.carry(P),
            "momentum": signals.momentum(
                P, lookback_months=MOM_LOOKBACK_MONTHS, skip_months=MOM_SKIP_MONTHS),
            "value": signals.value(P, lookback_years=VALUE_LOOKBACK_YEARS),
        }

    def _config_pnl(self, score: pd.DataFrame) -> pd.Series:
        """score panel → dollar-neutral weights → net portfolio pnl series."""
        P = self._panels()
        weights = xsec.rank_to_weights(score)
        return xsec.portfolio_returns(
            weights, P["fwd_ret"],
            costs_bps=self.costs_bps, rebal_every=self.rebal_every,
        )

    def _build_menu(self) -> dict[str, pd.Series]:
        if self._menu_cache is not None:
            return self._menu_cache
        P = self._panels()
        scores = self._score_panels()
        menu: dict[str, pd.Series] = {nm: self._config_pnl(s) for nm, s in scores.items()}

        # blend_fx = equal-weight z(carry)+z(mom)+z(value); the signal module
        # z-scores the legs itself, so we hand it the raw panel.
        blend_score = signals.blend_fx(P)
        menu[SELECTED] = self._config_pnl(blend_score)

        self._menu_cache = menu
        return menu

    # ── Package protocol ─────────────────────────────────────────────────────────
    def load(self, coin: str) -> pd.DataFrame:
        """Holder frame whose INDEX (panel dates) drives CPCV time-splitting.
        Carries the selected (blend_fx) pnl as a column so it is trivially
        inspectable; the Strategy uses the precomputed series, not this frame."""
        menu = self._build_menu()
        idx = menu[SELECTED].index
        return pd.DataFrame({"close": menu[SELECTED].values}, index=idx)

    def selected(self, coin: str, df: pd.DataFrame) -> _XSecStrategy:
        return _XSecStrategy(self.selected_name, self._build_menu()[SELECTED])

    def menu(self, coin: str, df: pd.DataFrame) -> dict[str, pd.Series]:
        # full-period portfolio pnl per factor config (single-meaning per config)
        return dict(self._build_menu())


# ── Self-test ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    # Reuse crypto/metrics_daily.daily_metrics (honest √365) by import — crypto/ is
    # frozen, we only add it to sys.path to import the shared helper, never edit it.
    sys.path.insert(0, str(_HERE.parent / "crypto"))
    from metrics_daily import daily_metrics

    pkg = FXXSecPackage()
    print(f"rebal_every={pkg.rebal_every}d  costs_bps={pkg.costs_bps:.2f}/leg  "
          f"MAX_LOOKBACK_DAYS={MAX_LOOKBACK_DAYS} "
          f"(mom={MOM_LB_DAYS}, value={VALUE_LB_DAYS})")
    df = pkg.load("XSEC")
    m = pkg.menu("XSEC", df)
    print(f"panel days: {len(df)}  {df.index.min().date()} -> {df.index.max().date()}")
    print(f"menu configs ({len(m)}): {sorted(m)}")
    assert pkg.selected_name in m, "selected must be a menu config"
    assert set(m) == set(MENU_NAMES), f"menu mismatch: {sorted(m)} != {sorted(MENU_NAMES)}"

    print(f"\n{'config':<10}{'mean/day':>11}{'ann':>10}{'sharpe':>9}"
          f"{'maxDD':>9}{'calmar':>9}{'hit':>7}{'nan':>7}")
    for nm in MENU_NAMES:
        s = m[nm]
        assert s.index.equals(df.index), f"{nm} index != panel index"
        dm = daily_metrics(s)
        print(f"  {nm:<8}{s.mean():>+11.6f}{dm['ann']:>+9.2%}{dm['sharpe']:>9.2f}"
              f"{dm['maxdd']:>9.2%}{dm['calmar']:>9.2f}{dm['hit']:>7.2%}"
              f"{int(s.isna().sum()):>7d}")
    print("\n(daily_metrics = honest √365 annualization; NOT the harness √8760)")
    print("self-test passed.")
