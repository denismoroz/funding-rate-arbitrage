"""
XSMOM risk-overlay package for the validation harness (RESEARCH ONLY).

DESIGN — mirrors crypto_pkg / unlock_pkg: the whole long-short book is ONE
synthetic "coin" (XSMOM_OVL). Each menu config is a full-period daily PnL series.
The harness wraps the menu through CPCV (OOS distribution), PBO (does the
IS-best config transfer?) and DSR (Sharpe deflated for the menu size).

The menu is the FULL pre-registered grid (PLAN.md):
  - baseline                  (1 config)  ← SELECTED (the incumbent to beat)
  - Arm A vol-target          (3 target_vol × 2 vol_window = 6)
  - Arm B paired stop         (3 S × 2 R × 2 E = 12)
  - Arm C paired take-profit  (3 P × 2 R × 2 E = 12)
  - Arm D replacement stop    (3 S = 3)
  - Arm E replacement take-profit (3 P = 3)
  - Arm F vol-linked replacement stop (3 k = 3)
  - Arm G vol-linked replacement take-profit (3 k = 3)
Putting EVERY arm cell in the menu makes the PBO honestly penalise multiple
testing: if an arm only wins via its luckiest cell, PBO catches it.

SELECTED = baseline. fit() does nothing (no in-sample selection) — the CPCV OOS
path runs the FIXED baseline book; the PBO dimension is what tests whether picking
the best overlay cell transfers forward.

SEAM-SAFETY (critical, identical rationale to crypto_pkg):
  - Signals (momentum_ensemble) and every config's PnL are precomputed ONCE on the
    FULL panel; CPCV only SELECTS rows. Lookbacks stay intact across folds.
  - The signal driving a test-day return used prices up to MAX_LOOKBACK_DAYS (=60)
    earlier, so run_overlay.py MUST pass purge >= MAX_LOOKBACK_DAYS.
  - Arm A vol scaler shifts the trailing vol by 1 (no same-day look-ahead).
  - Arms B/C realise the triggering day's move (no retroactive avoidance).
  - Arms D/E/F/G: same timing as B/C for trigger; σ_coin shifted by 1 (causal);
    replacement coin picked from scores[d] (info ≤ d); new leg earns from d+1.

Bit-for-bit live parity: the score is signals.momentum_ensemble (the same z-score
momentum ensemble the live XSMOM ranks on); the engine is xsec.rank_to_weights +
the path-aware overlay, weekly rebalance, dollar-neutral terciles, 1x baseline.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).parent
_harness_dir = str(_HERE.parent / "validation_harness")
_crypto_dir = str(_HERE.parent / "cross_sectional" / "crypto")
_xsec_dir = str(_HERE.parent / "cross_sectional")
_research_dir = str(_HERE.parent)
for _d in [_harness_dir, _research_dir, _crypto_dir, _xsec_dir, str(_HERE)]:
    if _d not in sys.path:
        sys.path.insert(0, _d)

import cryptodata        # noqa: E402
import signals          # noqa: E402  (research/cross_sectional/crypto/signals.py)
import xsec             # noqa: E402  (research/cross_sectional/xsec.py)
from costs import Costs, TAKER   # noqa: E402

import overlay          # noqa: E402  (this folder)

UNIVERSE_JSON = _HERE / "universe.json"

# ── Pre-registered config (PLAN.md — DO NOT EXPAND) ────────────────────────────
ENSEMBLE_LOOKBACKS = (14, 21, 30, 45, 60)
MAX_LOOKBACK_DAYS = max(ENSEMBLE_LOOKBACKS)        # = 60 → purge >= 60
REBAL_EVERY = 7                                    # weekly
COSTS_BPS = 4.4                                    # validated real HL per-leg cost

# Arm A grid
A_TARGET_VOLS = (0.10, 0.15, 0.20)
A_VOL_WINDOWS = (20, 40)

# Arm B / C grids
B_STOPS = (-0.08, -0.12, -0.20)
C_TAKES = (+0.08, +0.12, +0.20)
PAIR_RULES = ("worst_opposite", "symmetric_rank")
REENTRIES = ("next_rebalance", "none")

# Arm D / E grids (fixed-% replacement; same thresholds as B/C for apples-to-apples)
D_STOPS = (-0.08, -0.12, -0.20)
E_TAKES = (+0.08, +0.12, +0.20)

# Arm F / G grids (vol-linked replacement; k multiplier)
FG_KS = (1.5, 2.5, 4.0)
VOL_WINDOW = 20   # trailing daily vol window for F/G σ_coin

SELECTED = "baseline"


class _OverlayStrategy:
    """Harness contract adapter. The SELECTED book (baseline) is FIXED — fit()
    returns None (no in-sample selection). simulate() slices the precomputed full-
    period PnL (seam-safe: built once on the full panel; CPCV only picks rows)."""

    def __init__(self, name: str, pnl: pd.Series):
        self.name = name
        self._pnl = pnl

    def fit(self, df: pd.DataFrame, train_idx: np.ndarray, costs: Costs):
        return None

    def simulate(self, df: pd.DataFrame, seg: slice, config, costs: Costs) -> np.ndarray:
        return self._pnl.values[seg]


class OverlayPackage:
    """Package: one synthetic coin XSMOM_OVL, menu = baseline + Arms A–G grid."""

    name = "XSMOM intra-week risk overlay (vol-target / paired stop / take-profit / replacement)"
    selected_name = SELECTED
    coins = ["XSMOM_OVL"]

    def __init__(self, *, costs: Costs = TAKER, costs_bps: float = COSTS_BPS,
                 rebal_every: int = REBAL_EVERY):
        self._costs = costs
        self.costs_bps = costs_bps
        self.rebal_every = rebal_every
        self._frozen = _frozen_universe()
        self._panel: dict | None = None
        self._weights: pd.DataFrame | None = None
        self._scores: pd.DataFrame | None = None   # full daily scores for replacement
        self._menu_cache: dict[str, pd.Series] | None = None

    # ── panel / signals / target weights (computed ONCE on the full panel) ──────
    def _panels(self) -> dict:
        if self._panel is None:
            self._panel = cryptodata.load_panel(coins=self._frozen)
        return self._panel

    def _momentum_scores(self) -> pd.DataFrame:
        """Full daily momentum_ensemble scores (all coins, all dates).

        Computed ONCE on the full panel (seam-safe).  Used by Arms D/E/F/G to pick
        the best-ranked replacement coin.  Same scores that drive rank_to_weights,
        so the replacement picks from the same ranking the live book uses.
        """
        if self._scores is None:
            P = self._panels()
            self._scores = signals.momentum_ensemble(P, lookbacks=ENSEMBLE_LOOKBACKS)
        return self._scores

    def _target_weights(self) -> pd.DataFrame:
        """Weekly XSMOM target book (dollar-neutral terciles on momentum_ensemble).

        NOTE: rank_to_weights produces a weight EVERY day; the weekly cadence is
        enforced by the engine (rebal_every) which only re-reads the target on
        rebalance days. We therefore pass the FULL daily weight panel; baseline and
        every overlay sample it on the same rebalance grid → identical entry books.
        """
        if self._weights is None:
            self._weights = xsec.rank_to_weights(self._momentum_scores())
        return self._weights

    # ── per-config PnL builders ─────────────────────────────────────────────────
    def _baseline_pnl(self) -> pd.Series:
        P = self._panels()
        return xsec.portfolio_returns(
            self._target_weights(), P["fwd_ret"],
            costs_bps=self.costs_bps, rebal_every=self.rebal_every,
        )

    def _arm_a_pnl(self, base: pd.Series, target_vol: float, vol_window: int) -> pd.Series:
        return overlay.vol_target_scale(
            base, target_vol_annual=target_vol, vol_window=vol_window, ewma=True,
        )

    def _path_pnl(self, mode: str, threshold: float, pair_rule: str,
                  reentry: str) -> pd.Series:
        P = self._panels()
        return overlay.path_aware_overlay(
            self._target_weights(), P["fwd_ret"],
            threshold=threshold, mode=mode, pair_rule=pair_rule, reentry=reentry,
            costs_bps=self.costs_bps, rebal_every=self.rebal_every,
        )

    def _replacement_pnl(self, mode: str, threshold: float,
                         vol_linked: bool) -> pd.Series:
        P = self._panels()
        return overlay.replacement_overlay(
            self._target_weights(), self._momentum_scores(), P["fwd_ret"],
            threshold=threshold, mode=mode, vol_linked=vol_linked,
            vol_window=VOL_WINDOW,
            costs_bps=self.costs_bps, rebal_every=self.rebal_every,
        )

    def _build_menu(self) -> dict[str, pd.Series]:
        if self._menu_cache is not None:
            return self._menu_cache
        menu: dict[str, pd.Series] = {}

        base = self._baseline_pnl()
        menu["baseline"] = base

        # Arm A — book-level vol target (rescales gross of the baseline book)
        for tv in A_TARGET_VOLS:
            for vw in A_VOL_WINDOWS:
                menu[f"A_vt{tv:.2f}_w{vw}"] = self._arm_a_pnl(base, tv, vw)

        # Arm B — paired stop
        for s in B_STOPS:
            for pr in PAIR_RULES:
                for e in REENTRIES:
                    nm = f"B_stop{int(s*100)}_{_abbr(pr)}_{_abbr(e)}"
                    menu[nm] = self._path_pnl("stop", s, pr, e)

        # Arm C — paired take-profit (clean mirror of B)
        for p in C_TAKES:
            for pr in PAIR_RULES:
                for e in REENTRIES:
                    nm = f"C_tp{int(p*100)}_{_abbr(pr)}_{_abbr(e)}"
                    menu[nm] = self._path_pnl("take_profit", p, pr, e)

        # Arm D — replacement stop, fixed %
        for s in D_STOPS:
            nm = f"D_stop{int(s*100)}"
            menu[nm] = self._replacement_pnl("stop", s, vol_linked=False)

        # Arm E — replacement take-profit, fixed %
        for p in E_TAKES:
            nm = f"E_tp{int(p*100)}"
            menu[nm] = self._replacement_pnl("take_profit", p, vol_linked=False)

        # Arm F — replacement stop, vol-linked
        for k in FG_KS:
            nm = f"F_vstop_k{k}"
            menu[nm] = self._replacement_pnl("stop", k, vol_linked=True)

        # Arm G — replacement take-profit, vol-linked
        for k in FG_KS:
            nm = f"G_vtp_k{k}"
            menu[nm] = self._replacement_pnl("take_profit", k, vol_linked=True)

        # Align every config on the union of dates so the harness portfolio matrix
        # (which dropna's rows where ANY config is NaN) keeps the common valid span.
        idx = base.index
        menu = {k: v.reindex(idx) for k, v in menu.items()}
        self._menu_cache = menu
        return menu

    # ── Package protocol ────────────────────────────────────────────────────────
    def load(self, coin: str) -> pd.DataFrame:
        menu = self._build_menu()
        idx = menu[SELECTED].index
        return pd.DataFrame({"close": menu[SELECTED].values}, index=idx)

    def selected(self, coin: str, df: pd.DataFrame) -> _OverlayStrategy:
        return _OverlayStrategy(self.selected_name, self._build_menu()[SELECTED])

    def menu(self, coin: str, df: pd.DataFrame) -> dict[str, pd.Series]:
        return dict(self._build_menu())


def _abbr(s: str) -> str:
    return {
        "worst_opposite": "wo", "symmetric_rank": "sr",
        "next_rebalance": "reb", "none": "non",
    }.get(s, s)


def _frozen_universe() -> list[str]:
    return list(json.loads(UNIVERSE_JSON.read_text())["coins"])


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    pkg = OverlayPackage()
    print(f"frozen universe: {len(pkg._frozen)} coins")
    df = pkg.load("XSMOM_OVL")
    m = pkg.menu("XSMOM_OVL", df)
    print(f"panel days: {len(df)}  {df.index.min().date()} -> {df.index.max().date()}")
    print(f"menu configs ({len(m)})")
    assert SELECTED in m, "selected must be in menu"
    expected = (
        1                                                        # baseline
        + len(A_TARGET_VOLS) * len(A_VOL_WINDOWS)               # Arm A
        + 2 * len(B_STOPS) * len(PAIR_RULES) * len(REENTRIES)   # Arm B + C
        + len(D_STOPS)                                           # Arm D
        + len(E_TAKES)                                           # Arm E
        + len(FG_KS)                                             # Arm F
        + len(FG_KS)                                             # Arm G
    )
    assert len(m) == expected, f"menu size {len(m)} != expected {expected}"

    def _q(s):
        r = s.dropna().values
        if len(r) < 10:
            return (float("nan"),) * 3
        ann = r.mean() * 252
        sr = ann / (r.std() * np.sqrt(252)) if r.std() > 0 else 0.0
        cum = np.cumprod(1 + r)
        dd = (cum / np.maximum.accumulate(cum) - 1).min()
        return ann, sr, dd

    print(f"\n{'config':<26}{'ann':>9}{'sharpe':>9}{'maxDD':>9}")
    for nm in sorted(m):
        a, s, d = _q(m[nm])
        print(f"  {nm:<24}{a:>+8.2%}{s:>9.2f}{d:>9.2%}")
    print("\nself-test passed.")
