"""
Cross-sectional crypto long-short package for the validation harness.

DESIGN — the whole long-short book is ONE synthetic "coin" ("XSEC").
A cross-sectional book is a SINGLE portfolio: its return at t depends on the
RANKING across all coins at t, so it is NOT separable per coin. The harness has a
per-coin model, so we adapt by exposing exactly one synthetic instrument whose
"price series" is the book's daily net pnl. Each menu config is a full-period
portfolio pnl series (one factor → one column); _portfolio_menu over one coin
then yields the (T × n_config) matrix directly (PBO across factors, DSR on the
selected column).

Pipeline per config (no look-ahead, seam-safe — see signals.py / xsec.py):
  score panel (signals)            # uses data <= t only
    → xsec.rank_to_weights         # dollar-neutral long top / short bottom tercile
    → xsec.portfolio_returns(weights, fwd_ret, costs_bps, rebal_every)
                                   # fwd_ret[t] = t→t+1 realised return (forward)
  → one daily pnl pd.Series indexed by the panel dates.

SEAM-SAFETY (CRITICAL, same rationale as b_pkg): signals & portfolio pnl are
precomputed on the FULL panel (lookbacks intact); CPCV only SELECTS rows. The
signal that produced a test-day return used prices up to MAX_LOOKBACK_DAYS
earlier, so the runner MUST be called with purge >= MAX_LOOKBACK_DAYS to keep
those source bars out of train. We expose MAX_LOOKBACK_DAYS for run_crypto.py.

SELECTED is a FIXED equal-weight blend (z-mom60 + z-carry). fit() does nothing:
NO in-sample factor selection in the OOS path — that keeps the CPCV OOS honest
(the PBO dimension is what tests whether picking the best menu config transfers).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import cryptodata
import signals
import xsec
from costs import Costs, TAKER

_HERE = Path(__file__).parent
UNIVERSE_JSON = _HERE / "universe.json"

# ── Menu lookbacks (days). MAX drives purge (seam-safety lower bound). ──────────
MOM_LOOKBACKS = (30, 60, 90)     # momentum configs mom30/mom60/mom90
CARRY_SMOOTH  = 14               # carry funding-smoothing window (<= 90, not binding)
BLEND_MOM     = 60               # the momentum leg used inside the blend
MAX_LOOKBACK_DAYS = max(max(MOM_LOOKBACKS), CARRY_SMOOTH)   # = 90 → purge >= 90

SELECTED = "blend"


def _frozen_universe() -> list[str]:
    """Load the FROZEN coin list (deterministic backtest). NOT a live universe()."""
    return list(json.loads(UNIVERSE_JSON.read_text())["coins"])


class _XSecStrategy:
    """Adapter for the harness contract.

    The selected book (equal-weight blend) is a FIXED config — fit() returns None
    (no in-sample selection). simulate() returns the precomputed full-period blend
    pnl sliced to the contiguous CPCV segment. Slicing (not recomputing) is what
    makes this seam-safe: the pnl was built once on the full panel with lookbacks
    intact; CPCV only chooses which contiguous rows form this OOS segment.
    """

    def __init__(self, name: str, blend_pnl: pd.Series):
        self.name = name
        self._blend_pnl = blend_pnl

    def fit(self, df: pd.DataFrame, train_idx: np.ndarray, costs: Costs):
        return None  # selected is fixed → nothing to choose

    def simulate(self, df: pd.DataFrame, seg: slice, config, costs: Costs) -> np.ndarray:
        # df index == blend_pnl index (same panel dates) → positional slice aligns.
        return self._blend_pnl.values[seg]


class CryptoXSecPackage:
    """Package protocol: one synthetic coin "XSEC", menu = factor configs.

    Parameters
    ----------
    rebal_every : hold weights this many days between rebalances. DEFAULT 7
        (WEEKLY). Crypto cross-sectional momentum decays faster than FX, so daily
        rebal over-trades (turnover cost dominates) and monthly is too stale;
        weekly is the standard cadence and keeps turnover cost sane. Exposed so
        sensitivity can be probed without editing the package.
    costs_bps : one-way cost in basis points applied per unit of turnover by
        xsec.portfolio_returns. DEFAULT derived from the harness Costs: HL perp
        taker (perp_fee) + slippage, in bps. With TAKER that is
        (0.00035 + 0.0005)*1e4 = 8.5 bps one-way. portfolio_returns charges
        turnover * costs_bps/1e4 at each rebalance, and turnover already counts
        BOTH legs (Σ|Δw| over the dollar-neutral book), so a one-way per-leg cost
        is the right unit (round-trip emerges across consecutive rebalances).
    """

    name = "Crypto cross-sectional long-short (momentum + carry)"
    selected_name = SELECTED
    coins = ["XSEC"]

    def __init__(self, *, rebal_every: int = 7, costs: Costs = TAKER,
                 costs_bps: float | None = None):
        self.rebal_every = rebal_every
        self._costs = costs
        self.costs_bps = (costs.perp_cost * 1e4) if costs_bps is None else costs_bps
        self._frozen = _frozen_universe()
        self._panel: dict | None = None
        self._menu_cache: dict[str, pd.Series] | None = None

    # ── panel & signals ────────────────────────────────────────────────────────
    def _panels(self) -> dict:
        if self._panel is None:
            self._panel = cryptodata.load_panel(coins=self._frozen)
        return self._panel

    def _score_panels(self) -> dict[str, pd.DataFrame]:
        """Raw (un-z-scored) factor score panels per menu config name.

        Momentum at each lookback and carry are scale-free for ranking, so the
        single-factor menu configs rank the RAW score (z-scoring before ranking is
        a no-op — rank is scale-invariant). The blend legs ARE z-scored (below) so
        the two factors are on a comparable scale before averaging.
        """
        P = self._panels()
        out: dict[str, pd.DataFrame] = {}
        for lb in MOM_LOOKBACKS:
            out[f"mom{lb}"] = signals.momentum(P, lb)
        out["carry"] = signals.carry(P, smooth_days=CARRY_SMOOTH)
        return out

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
        scores = self._score_panels()
        menu: dict[str, pd.Series] = {nm: self._config_pnl(s) for nm, s in scores.items()}

        # blend = equal-weight z(mom60) + z(carry); z-score the legs first so the
        # two factors are comparable, then rank the blended score.
        z_mom = signals.zscore_cross_section(scores[f"mom{BLEND_MOM}"])
        z_carry = signals.zscore_cross_section(scores["carry"])
        blend_score = signals.blend([z_mom, z_carry])           # equal weights
        menu[SELECTED] = self._config_pnl(blend_score)

        self._menu_cache = menu
        return menu

    # ── Package protocol ───────────────────────────────────────────────────────
    def load(self, coin: str) -> pd.DataFrame:
        """Holder frame whose INDEX (panel dates) drives CPCV time-splitting.
        Carries the blend pnl as a column so it is trivially inspectable; the
        Strategy uses the precomputed series, not this frame's values."""
        menu = self._build_menu()
        idx = menu[SELECTED].index
        return pd.DataFrame({"close": menu[SELECTED].values}, index=idx)

    def selected(self, coin: str, df: pd.DataFrame) -> _XSecStrategy:
        return _XSecStrategy(self.selected_name, self._build_menu()[SELECTED])

    def menu(self, coin: str, df: pd.DataFrame) -> dict[str, pd.Series]:
        # full-period portfolio pnl per factor config (single-meaning per config)
        return dict(self._build_menu())


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    pkg = CryptoXSecPackage()
    print(f"frozen universe: {len(pkg._frozen)} coins")
    print(f"rebal_every={pkg.rebal_every}d  costs_bps={pkg.costs_bps:.2f}  "
          f"MAX_LOOKBACK_DAYS={MAX_LOOKBACK_DAYS}")
    df = pkg.load("XSEC")
    m = pkg.menu("XSEC", df)
    print(f"panel days: {len(df)}  {df.index.min().date()} -> {df.index.max().date()}")
    print(f"menu configs ({len(m)}): {sorted(m)}")
    assert pkg.selected_name in m, "selected must be a menu config"
    for nm, s in m.items():
        assert s.index.equals(df.index), f"{nm} index != panel index"
        ann = s.mean() * 252
        print(f"  {nm:<8} mean/day={s.mean():+.5f}  ann≈{ann:+.2%}  "
              f"std={s.std():.5f}  nan={s.isna().sum()}")
    print("\nself-test passed.")
