"""C6 — THE DECISIVE OOS EXPERIMENT: commit to a fixed ENSEMBLE of momentum
lookbacks vs ADAPTIVELY pick the best lookback on history — out of sample.

THE QUESTION
------------
The sweep (sweep.py) showed crypto momentum is a PLATEAU: daily Sharpe is positive
across lookback 14→120d, not a spike. Two ways to harvest it:
  • FixedEnsemble — AVERAGE the plateau: one fixed signal = equal-weight mean of
    z-scored momentum over (14,21,30,45,60). NO lookback selection, ever.
  • AdaptiveSelect — PICK the lookback whose book had the best Sharpe on the train
    rows of each split, then trade THAT lookback OOS. This is the "optimize the
    number on history" strawman that gave v1 its bad PBO (0.83).
Both are run through runner.run_cpcv on the SAME CPCV splits → an apples-to-apples
POOLED OOS comparison. We also run the full harness (DSR on the ensemble + PBO
across a single-meaning menu) and report honest DAILY metrics for the books.

DESIGN — single synthetic "XSEC" coin (mirrors crypto_pkg.py)
-------------------------------------------------------------
A cross-sectional book is ONE portfolio (its return at t depends on the ranking
across all coins), so we expose exactly one synthetic instrument whose price
series is the book's daily net pnl. Each lookback's book pnl and the ensemble book
pnl are PRECOMPUTED ONCE on the FROZEN full panel (lookbacks intact); CPCV only
SELECTS contiguous rows. simulate() SLICES the precomputed series — never recompute
per split, which is what keeps the seam safe.

SEAM-SAFETY: the signal that produced a test-day return used prices up to
MAX_LB=60d earlier, so purge MUST be >= 60d. We use purge=60 (= the ensemble's
longest leg, the binding lookback) — the tight, correct lower bound.

ADAPTIVE fit IS THE TRAP WE TEST, AND IT MUST BE HONEST: AdaptiveSelect.fit reads
ONLY train_idx rows of each precomputed book pnl (NEVER test rows), scores each
lookback by its train Sharpe, and returns the argmax. simulate then slices THAT
lookback's full-period book to the test segment. So the only thing fit sees is
train; the selection danger it courts is the whole point.

ANNUALIZATION CAVEAT (same as run_crypto.py): engine.compute_metrics annualizes
with HOURS_PER_YEAR=8760 (it models an HOURLY book); our pnl is DAILY, so the
pooled-OOS annual_pct/sharpe/calmar are INFLATED (~×35 ann, ~×5.9 sharpe/calmar).
The COMPARISON FixedEnsemble-vs-AdaptiveSelect is apples-to-apples (both go
through the SAME compute_metrics), and DSR/PBO are period-agnostic. For ABSOLUTE
levels we print honest DAILY metrics via metrics_daily.daily_metrics.

Run:
  PYTHONPATH=<research>:<research/validation_harness>:<research/cross_sectional>:<this dir> \
    python -u run_crypto_v2.py
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

from contract import Strategy
from runner import run_cpcv, _DIST_KEYS
from splitter import cpcv
from harness import run_harness, save_json
from report import print_report
from metrics_daily import daily_metrics

_HERE = Path(__file__).parent
UNIVERSE_JSON = _HERE / "universe.json"

# ── Config (matches the prompt) ─────────────────────────────────────────────────
LOOKBACKS = (14, 21, 30, 45, 60)     # the plateau region; ensemble averages all
MAX_LB = max(LOOKBACKS)              # = 60 → purge lower bound (seam-safety)
COSTS_BPS = 8.5                      # one-way per-leg (HL taker+slip), same as v1/sweep
REBAL_EVERY = 7                      # weekly cadence
TERCILE_FRAC = 0.33                  # leg size

N_GROUPS = 6
K = 2
PURGE = MAX_LB                       # = 60d (>= max lookback → seam-safe)
EMBARGO = 7                          # days


def _frozen_universe() -> list[str]:
    """FROZEN 34-coin list → deterministic. NOT a live universe()."""
    return list(json.loads(UNIVERSE_JSON.read_text())["coins"])


def _book_pnl(panel: dict, score: pd.DataFrame) -> pd.Series:
    """score panel → dollar-neutral weights → net daily book pnl (full period)."""
    w = xsec.rank_to_weights(score, tercile_frac=TERCILE_FRAC)
    return xsec.portfolio_returns(w, panel["fwd_ret"],
                                  costs_bps=COSTS_BPS, rebal_every=REBAL_EVERY)


def build_books(panel: dict) -> tuple[dict[int, pd.Series], pd.Series]:
    """Precompute, on the FULL frozen panel: per-lookback momentum book pnl and the
    ensemble book pnl. All share the panel date index. NO look-ahead (signals use
    price<=t; fwd_ret is forward-aligned by the panel)."""
    per_lb = {lb: _book_pnl(panel, signals.momentum(panel, lb)) for lb in LOOKBACKS}
    ens = _book_pnl(panel, signals.momentum_ensemble(panel, lookbacks=LOOKBACKS))
    return per_lb, ens


def _daily_sharpe(pnl_slice: np.ndarray) -> float:
    """Daily Sharpe on a raw pnl slice (no annualization — only used to RANK
    lookbacks within a fit, so the sqrt(365) factor cancels). 0 if degenerate."""
    r = pnl_slice[np.isfinite(pnl_slice)]
    if r.size < 2 or r.std(ddof=0) == 0:
        return 0.0
    return float(r.mean() / r.std(ddof=0))


# ── Two strategies under the harness contract ───────────────────────────────────

class FixedEnsemble:
    """Commit to the fixed ensemble book. fit() is a no-op (NO selection)."""
    name = "FixedEnsemble"

    def __init__(self, ens_pnl: pd.Series):
        self._pnl = ens_pnl.values

    def fit(self, df: pd.DataFrame, train_idx: np.ndarray, costs: Costs):
        return None  # nothing to choose — we always trade the ensemble

    def simulate(self, df: pd.DataFrame, seg: slice, config, costs: Costs) -> np.ndarray:
        return self._pnl[seg]


class AdaptiveSelect:
    """Pick the lookback with the best TRAIN-window daily Sharpe, trade it OOS.

    fit() reads ONLY train_idx rows of each precomputed book pnl (never test rows)
    and returns the argmax-Sharpe lookback. simulate() slices THAT lookback's full
    book to the test segment. This is the in-sample-selection strawman the
    experiment is designed to beat (or not)."""
    name = "AdaptiveSelect"

    def __init__(self, per_lb_pnl: dict[int, pd.Series]):
        self._books = {lb: s.values for lb, s in per_lb_pnl.items()}
        self._lbs = list(per_lb_pnl.keys())

    def fit(self, df: pd.DataFrame, train_idx: np.ndarray, costs: Costs) -> int:
        # ONLY train rows are touched here — the selection sees no test data.
        best_lb, best_sh = self._lbs[0], -np.inf
        for lb in self._lbs:
            sh = _daily_sharpe(self._books[lb][train_idx])
            if sh > best_sh:
                best_lb, best_sh = lb, sh
        return best_lb

    def simulate(self, df: pd.DataFrame, seg: slice, config: int, costs: Costs) -> np.ndarray:
        return self._books[config][seg]


# ── Harness package: selected = ensemble, menu = single-meaning configs ─────────

class _MenuStrategy:
    """Fixed-config adapter for run_harness's per-coin CPCV (selected = ensemble)."""
    def __init__(self, name: str, pnl: pd.Series):
        self.name = name
        self._pnl = pnl.values

    def fit(self, df, train_idx, costs):
        return None

    def simulate(self, df, seg, config, costs) -> np.ndarray:
        return self._pnl[seg]


class EnsemblePackage:
    """Package protocol. ONE synthetic coin "XSEC"; selected = the ensemble;
    menu = {ensemble, mom14, mom30, mom60} — every config has ONE meaning (no
    selection inside a config). PBO measures selection-danger ACROSS this menu;
    our POINT is we commit to `ensemble` and never select — so PBO is context for
    the strawman, not a verdict on the committed strategy."""

    name = "Crypto XSEC — momentum ENSEMBLE (commit) vs lookback menu"
    selected_name = "ensemble"
    coins = ["XSEC"]

    def __init__(self, per_lb_pnl: dict[int, pd.Series], ens_pnl: pd.Series):
        self._menu = {"ensemble": ens_pnl,
                      "mom14": per_lb_pnl[14],
                      "mom30": per_lb_pnl[30],
                      "mom60": per_lb_pnl[60]}

    def load(self, coin: str) -> pd.DataFrame:
        idx = self._menu["ensemble"].index
        return pd.DataFrame({"close": self._menu["ensemble"].values}, index=idx)

    def selected(self, coin: str, df: pd.DataFrame) -> _MenuStrategy:
        return _MenuStrategy(self.selected_name, self._menu["ensemble"])

    def menu(self, coin: str, df: pd.DataFrame) -> dict[str, pd.Series]:
        return dict(self._menu)


# ── head-to-head OOS table ──────────────────────────────────────────────────────

def _oos_row(label: str, rep) -> str:
    sh = rep.dist.get("sharpe", {}).get("median", float("nan"))
    cal = rep.dist.get("calmar", {}).get("median", float("nan"))
    ann = rep.dist.get("annual_pct", {}).get("median", float("nan"))
    return (f"  {label:<16}{sh:>12.3f}{cal:>12.3f}{ann:>12.2f}"
            f"{rep.frac_sharpe_pos*100:>11.1f}%{rep.frac_calmar_pos*100:>11.1f}%"
            f"{rep.n_segments:>8d}")


def main() -> None:
    print("#" * 78)
    print("##### C6 — FixedEnsemble vs AdaptiveSelect — DECISIVE OOS comparison #####")
    print("#" * 78)

    coins = _frozen_universe()
    panel = cryptodata.load_panel(coins=coins)
    px = panel["price"]
    print(f"PANEL  {px.shape[0]} days x {px.shape[1]} coins  "
          f"({px.index.min().date()} -> {px.index.max().date()})")
    print(f"lookbacks={LOOKBACKS}  MAX_LB={MAX_LB}  costs={COSTS_BPS}bps/leg  "
          f"rebal_every={REBAL_EVERY}d  tercile_frac={TERCILE_FRAC}")
    print(f"CPCV: n_groups={N_GROUPS} k={K} purge={PURGE}d(=max lb) embargo={EMBARGO}d")

    per_lb, ens = build_books(panel)
    df = pd.DataFrame({"close": ens.values}, index=ens.index)   # index drives CPCV

    # SAME splits for both strategies (apples-to-apples).
    splits = cpcv(len(df), n_groups=N_GROUPS, k=K, purge=PURGE, embargo=EMBARGO)

    fixed = FixedEnsemble(ens)
    adapt = AdaptiveSelect(per_lb)
    rep_fixed = run_cpcv(fixed, df, splits=splits, costs=TAKER,
                         n_groups=N_GROUPS, k=K, purge=PURGE, embargo=EMBARGO)
    rep_adapt = run_cpcv(adapt, df, splits=splits, costs=TAKER,
                         n_groups=N_GROUPS, k=K, purge=PURGE, embargo=EMBARGO)

    # Which lookbacks did AdaptiveSelect pick across splits? (diagnostic)
    picks = [adapt.fit(df, sp.train_idx, TAKER) for sp in splits]
    pick_counts = {lb: picks.count(lb) for lb in LOOKBACKS if picks.count(lb)}

    print("\n" + "=" * 78)
    print("HEAD-TO-HEAD — POOLED OOS (same CPCV splits; harness HOURLY scale, see caveat)")
    print("=" * 78)
    print(f"  {'strategy':<16}{'med Sharpe':>12}{'med Calmar':>12}{'med ann%':>12}"
          f"{'%Sh>0':>12}{'%Cal>0':>12}{'segs':>8}")
    print(_oos_row("FixedEnsemble", rep_fixed))
    print(_oos_row("AdaptiveSelect", rep_adapt))
    print(f"\n  AdaptiveSelect picks across {len(splits)} splits: "
          + ", ".join(f"lb{lb}×{c}" for lb, c in pick_counts.items()))

    # Verdict
    d_sh = rep_fixed.dist["sharpe"]["median"] - rep_adapt.dist["sharpe"]["median"]
    d_cal = rep_fixed.dist["calmar"]["median"] - rep_adapt.dist["calmar"]["median"]
    d_pos = rep_fixed.frac_sharpe_pos - rep_adapt.frac_sharpe_pos
    winner = "FixedEnsemble" if d_sh >= 0 else "AdaptiveSelect"
    print(f"\n  VERDICT: {winner} wins OOS.")
    print(f"    median Sharpe: Fixed {rep_fixed.dist['sharpe']['median']:+.3f} vs "
          f"Adaptive {rep_adapt.dist['sharpe']['median']:+.3f}  (Δ = {d_sh:+.3f})")
    print(f"    median Calmar: Fixed {rep_fixed.dist['calmar']['median']:+.3f} vs "
          f"Adaptive {rep_adapt.dist['calmar']['median']:+.3f}  (Δ = {d_cal:+.3f})")
    print(f"    frac Sharpe>0: Fixed {rep_fixed.frac_sharpe_pos*100:.1f}% vs "
          f"Adaptive {rep_adapt.frac_sharpe_pos*100:.1f}%  (Δ = {d_pos*100:+.1f}pp)")
    print(f"    → committing to the ensemble {'BEATS' if d_sh >= 0 else 'LOSES TO'} "
          f"picking the best lookback in-sample, OOS.")

    # ── Honest DAILY full-period metrics (absolute levels) ──────────────────────
    print("\n" + "=" * 78)
    print("HONEST FULL-PERIOD DAILY METRICS (metrics_daily, sqrt(365) — TRUE levels)")
    print("=" * 78)
    print(f"  {'book':<16}{'sharpe':>9}{'calmar':>9}{'ann%':>9}{'maxDD%':>9}"
          f"{'vol%':>9}{'hit%':>8}{'n':>7}")

    def _drow(label, pnl):
        m = daily_metrics(pnl)
        cal = m["calmar"]
        cal_s = f"{cal:>9.2f}" if not np.isnan(cal) else f"{'nan':>9}"
        print(f"  {label:<16}{m['sharpe']:>9.2f}{cal_s}{100*m['ann']:>9.2f}"
              f"{100*m['maxdd']:>9.2f}{100*m['vol_ann']:>9.2f}{100*m['hit']:>8.1f}"
              f"{m['n']:>7d}")

    for lb in LOOKBACKS:
        _drow(f"mom{lb}", per_lb[lb])
    _drow("ENSEMBLE", ens)

    # ── Harness verdict: DSR on ensemble + PBO across the menu ───────────────────
    print("\n" + "=" * 78)
    print("HARNESS VERDICT — DSR(ensemble) + PBO across {ensemble,mom14,mom30,mom60}")
    print("=" * 78)
    pkg = EnsemblePackage(per_lb, ens)
    rep = run_harness(pkg, costs=TAKER,
                      n_groups=N_GROUPS, k=K, purge=PURGE, embargo=EMBARGO)
    print()
    print_report(rep)
    print("\n  NOTE: PBO measures selection-danger ACROSS the menu. Our strategy "
          "COMMITS\n  to `ensemble` and NEVER selects, so a high menu-PBO is exactly the "
          "trap we\n  sidestep — read DSR(ensemble) for the committed signal, PBO for the "
          "strawman.")

    out = _HERE / "run_crypto_v2.json"
    save_json(rep, out)
    print(f"\nJSON → {out.name}")


if __name__ == "__main__":
    main()
