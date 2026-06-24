"""
Token-unlock strategy package for the validation harness.

DESIGN — mirrors fx_pkg / crypto_pkg: the whole short book is ONE synthetic
"coin" (UNLOCK). Each menu config is a full-period daily PnL series for a
(W × thr × sizing) combination. The harness wraps it through CPCV + PBO + DSR.

SEAM-SAFETY: PnL is precomputed on the FULL panel with no in-sample lookback
(weights are purely event-schedule driven — no past-price lookback). CPCV only
SELECTS rows. Purge = max(W) days so train segments exclude bars that were in a
test-segment window entry zone.

DAILY-vs-HOURLY annualization: same caveat as crypto_pkg. engine.compute_metrics
uses HOURS_PER_YEAR=8760 but our PnL is daily. The harness OOS numbers (annual_pct
sharpe calmar) are on the hourly scale (~×5.9 for Sharpe). Treat as SIGN + shape.
The DSR and PBO are period-agnostic and are the primary verdict.

SELECTED: W=10, thr=1%, sizing="prop" (proportional to unlock fraction).
  Rationale: W=10 matches the diagnostic CAR study; thr=1% filters noise (many tiny
  linear/monthly vests split into micro-cliffs); prop sizing exploits the
  monotonic signal (larger unlock → stronger reaction).

MENU: 3 × 3 = 9 configs (W ∈ {7,10,14} × thr ∈ {0.005,0.01,0.02}).
  All use sizing="prop" as the primary axis (diagnostic showed prop > equal).
  equal-weight variant added for the selected config to keep menu honest.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).parent

# ── Harness integration ───────────────────────────────────────────────────────
import sys as _sys
_harness_dir = str(_HERE.parent / "validation_harness")
_crypto_dir  = str(_HERE.parent / "cross_sectional" / "crypto")
_research_dir = str(_HERE.parent)   # for engine.py (PERP_TAKER/SPOT_TAKER)
for _d in [_harness_dir, _research_dir, _crypto_dir, str(_HERE)]:
    if _d not in _sys.path:
        _sys.path.insert(0, _d)

from costs import Costs, TAKER
from contract import Strategy

from unlock_data import load_events
from unlock_strategy import build_book

# ── Menu configuration space ─────────────────────────────────────────────────
# (W, thr, sizing) triplets
_MENU_CONFIGS: list[tuple[int, float, str]] = [
    # prop sizing — primary axis
    (7,  0.005, "prop"),
    (7,  0.010, "prop"),
    (7,  0.020, "prop"),
    (10, 0.005, "prop"),
    (10, 0.010, "prop"),   # ← SELECTED
    (10, 0.020, "prop"),
    (14, 0.005, "prop"),
    (14, 0.010, "prop"),
    (14, 0.020, "prop"),
    # equal-weight variants for selected W (PBO comparison)
    (7,  0.010, "equal"),
    (10, 0.010, "equal"),
    (14, 0.010, "equal"),
]

SELECTED = "W10_thr0.010_prop"
MAX_LOOKBACK_DAYS = 14   # max W in menu


def _config_name(W: int, thr: float, sizing: str) -> str:
    return f"W{W}_thr{thr:.3f}_{sizing}"


class _UnlockStrategy:
    """Adapter for the harness contract.

    The SELECTED book is a FIXED precomputed config. fit() returns None (no
    in-sample selection). simulate() slices the precomputed full-period PnL.
    Slicing is seam-safe: PnL was built once on the full panel; CPCV selects rows.
    """

    def __init__(self, name: str, pnl: pd.Series):
        self.name = name
        self._pnl = pnl

    def fit(self, df: pd.DataFrame, train_idx: np.ndarray, costs: Costs):
        return None  # selected is fixed — nothing to fit

    def simulate(self, df: pd.DataFrame, seg: slice, config, costs: Costs) -> np.ndarray:
        # df.index aligns with self._pnl.index (same panel dates)
        return self._pnl.values[seg]


class UnlockPackage:
    """Package protocol: one synthetic coin "UNLOCK", menu = (W × thr × sizing).

    Parameters
    ----------
    costs : Costs object (passed by harness; actual costs already baked into pnl
            via COST_PER_LEG in unlock_strategy.build_book, so harness costs are
            informational here — same pattern as crypto_pkg).
    force_refresh : re-fetch emission data from network.
    """

    name = "Token-unlock cliff short book (market-hedged)"
    selected_name = SELECTED
    coins = ["UNLOCK"]

    def __init__(self, *, costs: Costs = TAKER, force_refresh: bool = False):
        self._costs = costs
        self._force_refresh = force_refresh
        self._events: pd.DataFrame | None = None
        self._menu_cache: dict[str, pd.Series] | None = None

    def _get_events(self) -> pd.DataFrame:
        if self._events is None:
            self._events = load_events(verbose=False, force_refresh=self._force_refresh)
        return self._events

    def _build_menu(self) -> dict[str, pd.Series]:
        if self._menu_cache is not None:
            return self._menu_cache
        events = self._get_events()
        menu: dict[str, pd.Series] = {}
        for W, thr, sizing in _MENU_CONFIGS:
            name = _config_name(W, thr, sizing)
            try:
                pnl = build_book(events, W=W, thr=thr, sizing=sizing)
                menu[name] = pnl
            except Exception as e:
                print(f"  [unlock_pkg] config {name} failed: {e}")
        self._menu_cache = menu
        return menu

    # ── Package protocol ──────────────────────────────────────────────────────
    def load(self, coin: str) -> pd.DataFrame:
        """Holder frame: index = panel dates, drives CPCV time-splitting."""
        menu = self._build_menu()
        idx = menu[SELECTED].index
        return pd.DataFrame({"close": menu[SELECTED].values}, index=idx)

    def selected(self, coin: str, df: pd.DataFrame) -> _UnlockStrategy:
        return _UnlockStrategy(self.selected_name, self._build_menu()[SELECTED])

    def menu(self, coin: str, df: pd.DataFrame) -> dict[str, pd.Series]:
        """Full-period PnL per config — one series per (W, thr, sizing) combo."""
        return dict(self._build_menu())


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json

    print("=== UnlockPackage self-test ===")
    pkg = UnlockPackage()
    df = pkg.load("UNLOCK")
    m = pkg.menu("UNLOCK", df)
    print(f"Panel: {len(df)} days  {df.index.min().date()} → {df.index.max().date()}")
    print(f"Menu configs ({len(m)}): {sorted(m)}")
    assert SELECTED in m, f"selected config '{SELECTED}' not in menu"

    print(f"\n{'config':<22}{'ann':>9}{'sharpe':>9}{'maxDD':>9}{'calmar':>9}")
    for nm in sorted(m):
        s = m[nm].dropna()
        if s.empty:
            continue
        ann = s.mean() * 252
        std = s.std() * (252 ** 0.5)
        sr  = ann / std if std > 0 else 0.0
        # maxDD
        cum  = (1 + s).cumprod()
        roll = cum.cummax()
        dd   = (cum / roll - 1)
        maxdd = dd.min()
        cal  = ann / abs(maxdd) if maxdd < 0 else float("inf")
        print(f"  {nm:<20}{ann:>+8.2%}{sr:>9.2f}{maxdd:>9.2%}{cal:>9.2f}")

    print("\nself-test passed.")
