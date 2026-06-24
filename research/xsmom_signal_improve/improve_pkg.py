"""
XSMOM signal-improvement package for the validation harness (RESEARCH ONLY).

ONE menu: baseline + 5 arms (R, G, K, T, B) — all cells pre-registered in PLAN.md.
SELECTED = baseline. PBO over the full menu correctly penalises multiple testing.

Seam-safety:
- All PnL series are precomputed ONCE on the full panel; CPCV only slices rows.
- Purge in run_improve.py must be >= MAX_LOOKBACK + MAX_GAP = 60 + 7 = 67 days.
- Arm T trend windows are causal: price[t]/price[t-trend_lb]-1 uses data <= t only.
- Arm B / baseline: rank_to_weights slices scores[day] per row — no future info.

Costs: 4.4 bps/leg (validated real HL per-leg cost). rebal_every=7 (weekly, like live).
Universe: FROZEN 32-coin live XSMOM list ∩ data (same as xsmom_risk_overlay).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).parent
_harness_dir = str(_HERE.parent / "validation_harness")
_crypto_dir  = str(_HERE.parent / "cross_sectional" / "crypto")
_xsec_dir    = str(_HERE.parent / "cross_sectional")
_research_dir = str(_HERE.parent)
for _d in [_harness_dir, _research_dir, _crypto_dir, _xsec_dir, str(_HERE)]:
    if _d not in sys.path:
        sys.path.insert(0, _d)

import cryptodata         # noqa: E402
import signals            # noqa: E402
import xsec              # noqa: E402
from costs import Costs, TAKER   # noqa: E402
from signals_plus import (       # noqa: E402
    arm_R_sharpe, arm_R_tstat,
    arm_G,
    arm_K,
    arm_T_weights,
    arm_B_weights,
    LOOKBACKS, MAX_LOOKBACK,
)

# ── Frozen live XSMOM universe (32 coins, per PLAN.md) ─────────────────────────
LIVE_XSMOM = [
    "AAVE", "ADA", "APT", "ARB", "ATOM", "AVAX", "BCH", "BNB", "BTC",
    "CRV", "DOGE", "DOT", "EIGEN", "ENA", "ETH", "INJ", "JTO", "JUP",
    "LINK", "LTC", "NEAR", "PENDLE", "PYTH", "SOL", "SUI", "TAO", "TRX",
    "UNI", "WLD", "XLM", "XRP", "ZRO",
]

UNIVERSE_JSON = _HERE.parent / "xsmom_risk_overlay" / "universe.json"

# ── Pre-registered grid constants ───────────────────────────────────────────────
ENSEMBLE_LOOKBACKS = LOOKBACKS           # (14, 21, 30, 45, 60)
MAX_LOOKBACK_DAYS  = MAX_LOOKBACK        # 60
MAX_GAP            = 7                   # max gap in Arm G
PURGE_DAYS         = MAX_LOOKBACK_DAYS + MAX_GAP   # 67  (use in run_improve.py)

REBAL_EVERY = 7                          # weekly, matches live XSMOM
COSTS_BPS   = 4.4                        # HL validated per-leg cost

# Arm G gaps
G_GAPS = (3, 5, 7)

# Arm T trend lookbacks
T_TREND_LBS = (30, 60)

# Arm B fractions
B_FRACS = (1 / 5, 1 / 3, 1 / 2)

SELECTED = "baseline"


# ── Minimal Strategy adapter ────────────────────────────────────────────────────
class _FixedPnL:
    """Harness contract: fit() is no-op; simulate() slices precomputed PnL."""

    def __init__(self, name: str, pnl: pd.Series):
        self.name  = name
        self._pnl  = pnl

    def fit(self, df, train_idx, costs):
        return None

    def simulate(self, df, seg, config, costs) -> np.ndarray:
        return self._pnl.values[seg]


# ── Package ────────────────────────────────────────────────────────────────────
class ImprovePackage:
    """Package: one synthetic coin XSMOM_SIG, menu = baseline + Arms R/G/K/T/B."""

    name          = "XSMOM signal-improvement (R/G/K/T/B arms)"
    selected_name = SELECTED
    coins         = ["XSMOM_SIG"]

    def __init__(self, *, costs_bps: float = COSTS_BPS, rebal_every: int = REBAL_EVERY):
        self.costs_bps   = costs_bps
        self.rebal_every = rebal_every
        self._frozen     = _frozen_universe()
        self._panel: dict | None = None
        self._menu_cache: dict[str, pd.Series] | None = None

    # ── Panel (loaded once) ─────────────────────────────────────────────────────
    def _panels(self) -> dict:
        if self._panel is None:
            self._panel = cryptodata.load_panel(coins=self._frozen)
        return self._panel

    # ── PnL builder helpers ─────────────────────────────────────────────────────
    def _score_to_pnl(self, scores: pd.DataFrame) -> pd.Series:
        """XS → weights → portfolio_returns (weekly, standard costs)."""
        P = self._panels()
        w = xsec.rank_to_weights(scores, tercile_frac=1 / 3)
        return xsec.portfolio_returns(
            w, P["fwd_ret"],
            costs_bps=self.costs_bps,
            rebal_every=self.rebal_every,
        )

    def _weights_to_pnl(self, w: pd.DataFrame) -> pd.Series:
        """Precomputed weights → portfolio_returns."""
        P = self._panels()
        return xsec.portfolio_returns(
            w, P["fwd_ret"],
            costs_bps=self.costs_bps,
            rebal_every=self.rebal_every,
        )

    # ── Full menu construction ─────────────────────────────────────────────────
    def _build_menu(self) -> dict[str, pd.Series]:
        if self._menu_cache is not None:
            return self._menu_cache
        P = self._panels()
        menu: dict[str, pd.Series] = {}

        # ── Baseline ─────────────────────────────────────────────────────────
        base_scores = signals.momentum_ensemble(P, lookbacks=ENSEMBLE_LOOKBACKS)
        menu["baseline"] = self._score_to_pnl(base_scores)

        # ── Arm R — risk-adjusted momentum ───────────────────────────────────
        menu["R_sharpe"] = self._score_to_pnl(arm_R_sharpe(P, ENSEMBLE_LOOKBACKS))
        menu["R_tstat"]  = self._score_to_pnl(arm_R_tstat(P, ENSEMBLE_LOOKBACKS))

        # ── Arm G — skip-recent gap ───────────────────────────────────────────
        for gap in G_GAPS:
            menu[f"G_gap{gap}"] = self._score_to_pnl(arm_G(P, gap, ENSEMBLE_LOOKBACKS))

        # ── Arm K — rank-based (percentile) ──────────────────────────────────
        menu["K_rank"] = self._score_to_pnl(arm_K(P, ENSEMBLE_LOOKBACKS))

        # ── Arm T — TS×XS gate ───────────────────────────────────────────────
        for tlb in T_TREND_LBS:
            w_T = arm_T_weights(P, trend_lb=tlb,
                                lookbacks=ENSEMBLE_LOOKBACKS,
                                tercile_frac=1 / 3)
            menu[f"T_trend{tlb}"] = self._weights_to_pnl(w_T)

        # ── Arm B — breadth sweep ─────────────────────────────────────────────
        for frac in B_FRACS:
            frac_name = f"B_frac{int(round(frac * 10))}in10"
            w_B = arm_B_weights(P, frac=frac, lookbacks=ENSEMBLE_LOOKBACKS)
            menu[frac_name] = self._weights_to_pnl(w_B)

        # Align all configs on baseline index (common valid span)
        idx = menu[SELECTED].index
        menu = {k: v.reindex(idx) for k, v in menu.items()}
        self._menu_cache = menu
        return menu

    # ── Package protocol ────────────────────────────────────────────────────────
    def load(self, coin: str) -> pd.DataFrame:
        menu = self._build_menu()
        idx  = menu[SELECTED].index
        return pd.DataFrame({"close": menu[SELECTED].values}, index=idx)

    def selected(self, coin: str, df: pd.DataFrame) -> _FixedPnL:
        return _FixedPnL(self.selected_name, self._build_menu()[SELECTED])

    def menu(self, coin: str, df: pd.DataFrame) -> dict[str, pd.Series]:
        return dict(self._build_menu())


# ── Universe helper ─────────────────────────────────────────────────────────────
def _frozen_universe() -> list[str]:
    """Live XSMOM 32-coin list ∩ data available (FROZEN for reproducibility)."""
    # Prefer universe.json from xsmom_risk_overlay if it exists and has the coins
    if UNIVERSE_JSON.exists():
        stored = json.loads(UNIVERSE_JSON.read_text()).get("coins", [])
        # intersect with live XSMOM list (drop HMSTR/TON removed in chore 4ea6710)
        available = [c for c in stored if c in LIVE_XSMOM]
        if len(available) >= 20:   # sanity: enough coins
            return sorted(available)
    # Fallback: full LIVE_XSMOM list (cryptodata will raise on missing CSVs)
    return sorted(LIVE_XSMOM)


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    pkg = ImprovePackage()
    print(f"frozen universe: {len(pkg._frozen)} coins → {pkg._frozen}")
    df   = pkg.load("XSMOM_SIG")
    menu = pkg.menu("XSMOM_SIG", df)
    print(f"panel days: {len(df)}  {df.index.min().date()} -> {df.index.max().date()}")
    print(f"menu configs: {len(menu)}")

    expected_names = {
        "baseline",
        "R_sharpe", "R_tstat",
        "G_gap3", "G_gap5", "G_gap7",
        "K_rank",
        "T_trend30", "T_trend60",
        "B_frac2in10", "B_frac3in10", "B_frac5in10",
    }
    assert set(menu.keys()) == expected_names, \
        f"menu keys mismatch:\n  got: {sorted(menu.keys())}\n  exp: {sorted(expected_names)}"
    assert SELECTED in menu, "selected must be in menu"

    def _q(s):
        r = s.dropna().values
        if len(r) < 10:
            return float("nan"), float("nan"), float("nan")
        ann   = r.mean() * 252
        sr    = ann / (r.std() * np.sqrt(252)) if r.std() > 0 else 0.0
        cum   = np.cumprod(1 + r)
        maxdd = (cum / np.maximum.accumulate(cum) - 1).min()
        return ann, sr, maxdd

    print(f"\n{'config':<22}{'ann':>9}{'sharpe':>9}{'maxDD':>9}")
    for nm in sorted(menu):
        a, s, d = _q(menu[nm])
        print(f"  {nm:<20}{a:>+8.2%}{s:>9.2f}{d:>9.2%}")

    print("\nself-test passed.")
