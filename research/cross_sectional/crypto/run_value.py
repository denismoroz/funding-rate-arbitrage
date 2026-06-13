"""
Crypto VALUE factor study — rigorous harness test.

KEY QUESTION: does crypto value diversify momentum (improve blend maxDD/Calmar/crash
behavior), the way value diversified FX momentum despite being weak standalone?

Design mirrors run_crypto_v2.py exactly:
  - FROZEN 34-coin universe (same as validated momentum book)
  - costs_bps=8.5, rebal_every=7, accrual=-funding.shift(-1)
  - CPCV/DSR/PBO via validation_harness
  - Honest daily metrics via metrics_daily.daily_metrics (sqrt(365))
  - PURGE = 200 days (>= max signal window = dma200; seam-safe for all signals)
  - EMBARGO = 7 days (matches momentum book)

Sections:
  A. STANDALONE value factors — DSR/PBO per signal
  B. ROBUSTNESS MAP — sweep windows within each value family
  C. THE KEY TEST — diversification: momentum + value blends
  D. CRASH BEHAVIOR — worst momentum drawdown windows + bear-2022

Run:
  cd research/cross_sectional/crypto
  PYTHONPATH=.../research:.../validation_harness:.../cross_sectional:.../crypto \\
    python -u run_value.py

Output: run_value.json (machine-readable) + printed report.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import cryptodata
import signals
import xsec
import bear_fetch
import bear_regime as br
import value_signals as vs
from metrics_daily import daily_metrics
from contract import Strategy
from runner import run_cpcv, _DIST_KEYS
from splitter import cpcv
from harness import run_harness, save_json, to_dict
from report import print_report
from costs import Costs, TAKER

_HERE = Path(__file__).parent
UNIVERSE_JSON = _HERE / "universe.json"

# ── Config (frozen, mirrors run_crypto_v2.py) ──────────────────────────────────
COSTS_BPS    = 8.5
REBAL_EVERY  = 7
TERCILE_FRAC = 1 / 3
MOM_LOOKBACKS = (14, 21, 30, 45, 60)   # validated momentum ensemble
PURGE        = 200                      # >= dma200 (longest window); seam-safe
EMBARGO      = 7
N_GROUPS     = 6
K            = 2


def _frozen_universe() -> list[str]:
    return list(json.loads(UNIVERSE_JSON.read_text())["coins"])


def _book_pnl(panel: dict, score: pd.DataFrame, accrual: pd.DataFrame | None = None) -> pd.Series:
    """score -> dollar-neutral weights -> net daily book pnl."""
    w = xsec.rank_to_weights(score, tercile_frac=TERCILE_FRAC)
    return xsec.portfolio_returns(
        w, panel["fwd_ret"],
        costs_bps=COSTS_BPS,
        rebal_every=REBAL_EVERY,
        accrual=accrual,
    )


def _fmt(m: dict, name: str = "") -> str:
    if not m:
        return f"  {name:<22} (too few days)"
    cal = m['calmar']
    cal_s = f"{cal:>8.2f}" if not np.isnan(cal) else "     nan"
    return (f"  {name:<22} Sharpe {m['sharpe']:>+6.2f}  ann {100*m['ann']:>+7.2f}%  "
            f"maxDD {100*m['maxdd']:>6.2f}%  Calmar{cal_s}  hit {100*m['hit']:>5.1f}%"
            f"  n={m['n']}")


def _safe_dict(d: dict) -> dict:
    """Convert numpy scalars to Python for JSON serialization."""
    out = {}
    for k, v in d.items():
        if isinstance(v, (np.floating, np.float64, np.float32)):
            out[k] = float(v)
        elif isinstance(v, (np.integer,)):
            out[k] = int(v)
        else:
            out[k] = v
    return out


# ── Harness adapter: one synthetic pnl series ──────────────────────────────────

class _FixedBook:
    """Strategy adapter: fixed precomputed pnl series, no fitting."""
    def __init__(self, name: str, pnl: pd.Series):
        self.name = name
        self._pnl = pnl.values

    def fit(self, df: pd.DataFrame, train_idx: np.ndarray, costs: Costs):
        return None

    def simulate(self, df: pd.DataFrame, seg: slice, config, costs: Costs) -> np.ndarray:
        return self._pnl[seg]


class _SingleBookPackage:
    """Package with ONE selected book + a menu of N configs for DSR/PBO.

    selected_pnl is what gets DSR-evaluated (the one we commit to).
    menu_pnls includes selected + alternatives (for PBO and N-trial DSR deflation).
    """
    coins = ["XSEC"]

    def __init__(self, name: str, selected_name: str,
                 selected_pnl: pd.Series, menu_pnls: dict[str, pd.Series]):
        self.name = name
        self.selected_name = selected_name
        self._sel = selected_pnl
        self._menu = dict(menu_pnls)
        if selected_name not in self._menu:
            self._menu[selected_name] = selected_pnl

    def load(self, coin: str) -> pd.DataFrame:
        idx = self._sel.index
        return pd.DataFrame({"close": self._sel.values}, index=idx)

    def selected(self, coin: str, df: pd.DataFrame) -> _FixedBook:
        return _FixedBook(self.selected_name, self._sel)

    def menu(self, coin: str, df: pd.DataFrame) -> dict[str, pd.Series]:
        return dict(self._menu)


# ── Drawdown-window helpers ─────────────────────────────────────────────────────

def _drawdown_windows(pnl: pd.Series, top_n: int = 3) -> list[tuple[pd.Timestamp, pd.Timestamp, float]]:
    """Find top_n worst drawdown episodes for a pnl series.

    Returns list of (start, trough_date, drawdown_fraction) sorted by severity.
    """
    r = pnl.dropna()
    eq = (1 + r).cumprod()
    dd = 1.0 - eq / np.maximum.accumulate(eq.values)
    dd_s = pd.Series(dd, index=eq.index)

    windows = []
    # Simple approach: for each trough find the preceding peak
    for i in range(len(dd_s)):
        if dd_s.iloc[i] > 0:
            trough_date = dd_s.index[i]
            dd_val = float(dd_s.iloc[i])
            # find the preceding peak (where cummax was last equal to eq at that point)
            eq_trough = eq.iloc[i]
            peak_level = eq.iloc[:i+1].max()
            peak_idx = (eq.iloc[:i+1] - peak_level).abs().idxmin()
            windows.append((peak_idx, trough_date, dd_val))

    if not windows:
        return []

    windows_df = pd.DataFrame(windows, columns=["peak", "trough", "dd"])
    windows_df = windows_df.sort_values("dd", ascending=False).drop_duplicates("trough")
    # Return top N
    return [(row["peak"], row["trough"], row["dd"])
            for _, row in windows_df.head(top_n).iterrows()]


def _window_metrics(pnl: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    sub = pnl.loc[(pnl.index >= start) & (pnl.index <= end)]
    return daily_metrics(sub)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    pd.set_option("display.width", 200)

    print("=" * 90)
    print("CRYPTO VALUE FACTOR STUDY — HARNESS-BACKED (CPCV + DSR + PBO)")
    print("KEY QUESTION: does value DIVERSIFY momentum (better blend maxDD/Calmar)?")
    print("=" * 90)

    coins = _frozen_universe()
    panel = cryptodata.load_panel(coins=coins)
    px = panel["price"]
    funding = panel["funding"]
    fwd_ret = panel["fwd_ret"]
    accrual = -(funding.shift(-1))   # canonical: -funding.shift(-1)

    print(f"\nPanel: {px.shape[0]} days x {px.shape[1]} coins  "
          f"({px.index.min().date()} -> {px.index.max().date()})")
    print(f"Config: costs={COSTS_BPS}bps/leg  rebal_every={REBAL_EVERY}d  "
          f"tercile={TERCILE_FRAC:.2f}  accrual=-funding.shift(-1)")
    print(f"CPCV: n_groups={N_GROUPS} k={K} purge={PURGE}d(>=dma200 window) embargo={EMBARGO}d")
    print(f"Annualization: sqrt(365) daily (honest, not harness hourly)")

    # ── Precompute momentum ensemble (the validated baseline) ──────────────────
    ens_score = signals.momentum_ensemble(panel, lookbacks=MOM_LOOKBACKS)
    pnl_mom = _book_pnl(panel, ens_score, accrual=accrual)
    m_mom = daily_metrics(pnl_mom)
    print(f"\nMomentum ensemble baseline (WITH funding accrual):")
    print(_fmt(m_mom, "momentum_ensemble"))

    # ── ALL VALUE SIGNAL PNL SERIES ────────────────────────────────────────────
    print("\n" + "=" * 90)
    print("COMPUTING VALUE SIGNAL BOOKS ...")
    print("=" * 90)

    # Build all signal variants
    signal_defs = {
        "dd90":    vs.drawdown_from_high(panel, window=90),
        "dd180":   vs.drawdown_from_high(panel, window=180),
        "dd_exp":  vs.drawdown_from_high(panel, window=None),
        "dma100":  vs.dist_from_ma(panel, ma_window=100),
        "dma200":  vs.dist_from_ma(panel, ma_window=200),
        "ltr120":  vs.long_term_reversal(panel, lookback=120),
        "ltr180":  vs.long_term_reversal(panel, lookback=180),
    }

    pnl_value = {}
    for name, score in signal_defs.items():
        pnl_value[name] = _book_pnl(panel, score, accrual=accrual)

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION A: STANDALONE VALUE METRICS + HARNESS
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 90)
    print("SECTION A — STANDALONE VALUE: METRICS + DSR + PBO")
    print("(Expect: weak like FX value standalone. Report honestly either way.)")
    print("=" * 90)

    HDR = f"  {'signal':<22}{'sharpe':>8}{'ann%':>9}{'maxDD%':>9}{'calmar':>9}{'hit%':>8}{'n':>7}"
    print(HDR)
    standalone_metrics = {}
    for name, pnl in pnl_value.items():
        m = daily_metrics(pnl)
        standalone_metrics[name] = m
        cal_s = f"{m['calmar']:>9.2f}" if m and not np.isnan(m['calmar']) else f"{'nan':>9}"
        if m:
            print(f"  {name:<22}{m['sharpe']:>8.2f}{100*m['ann']:>9.2f}{100*m['maxdd']:>9.2f}"
                  f"{cal_s}{100*m['hit']:>8.1f}{m['n']:>7d}")
        else:
            print(f"  {name:<22}  (too few days)")

    # Run harness on EACH standalone value signal
    # For DSR: menu = the 7 value signals (N=7 trials)
    # For PBO: same menu
    print("\n--- Running harness for standalone value DSR/PBO ---")
    print("(menu = 7 value signals; selected = each signal's own pnl)")

    # Build a joint menu pnl series dict for DSR deflation (common N_trials)
    all_value_pnls = dict(pnl_value)  # all 7

    standalone_harness = {}
    for name, pnl in pnl_value.items():
        pkg = _SingleBookPackage(
            name=f"value:{name}",
            selected_name=name,
            selected_pnl=pnl,
            menu_pnls=all_value_pnls,
        )
        rep = run_harness(pkg, costs=TAKER,
                          n_groups=N_GROUPS, k=K, purge=PURGE, embargo=EMBARGO)
        standalone_harness[name] = rep
        dsr_val = rep.dsr.get("dsr", float("nan"))
        pbo_val = rep.pbo.pbo if rep.pbo else float("nan")
        oos_sh = rep.pooled_oos.dist.get("sharpe", {}).get("median", float("nan"))
        print(f"  {name:<22} DSR={dsr_val:.3f}  PBO={pbo_val:.3f}  OOS_Sharpe_med={oos_sh:.3f}")

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION B: ROBUSTNESS MAP
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 90)
    print("SECTION B — ROBUSTNESS MAP (plateau vs spike?)")
    print("Same sign + consistent Sharpe across windows = real-but-modest edge")
    print("=" * 90)

    families = {
        "Drawdown-from-high": [("dd90", 90), ("dd180", 180), ("dd_exp", "expanding")],
        "Dist-from-MA":       [("dma100", 100), ("dma200", 200)],
        "Long-term reversal": [("ltr120", 120), ("ltr180", 180)],
    }

    robustness_map = {}
    for family_name, variants in families.items():
        print(f"\n  Family: {family_name}")
        print(f"  {'param':<16}{'sharpe':>9}{'calmar':>9}{'ann%':>9}{'maxDD%':>9}{'hit%':>8}")
        sharpes = []
        fam_rows = {}
        for sig_name, param in variants:
            m = standalone_metrics[sig_name]
            fam_rows[str(param)] = m
            if m:
                cal_s = f"{m['calmar']:>9.2f}" if not np.isnan(m['calmar']) else f"{'nan':>9}"
                print(f"  {str(param):<16}{m['sharpe']:>9.2f}{cal_s}"
                      f"{100*m['ann']:>9.2f}{100*m['maxdd']:>9.2f}{100*m['hit']:>8.1f}")
                sharpes.append(m['sharpe'])
            else:
                print(f"  {str(param):<16}  (too few days)")
        # Verdict
        if sharpes:
            s = np.array(sharpes)
            same_sign = (s > 0).all() or (s < 0).all()
            spread = s.max() - s.min()
            best = s.max()
            rest_mean = s[s < best].mean() if (s < best).any() else best
            gap = best - rest_mean
            if abs(best) < 0.3:
                verdict = f"FLAT/WEAK — |Sharpe|<0.3 (max {abs(best):.2f})"
            elif not same_sign:
                verdict = f"SPIKE — sign flips (min {s.min():+.2f} max {s.max():+.2f})"
            elif gap > 0.5 * abs(best) and gap > 0.30:
                verdict = f"SPIKE — best {best:+.2f} vs rest mean {rest_mean:+.2f} (gap {gap:.2f})"
            elif same_sign and best > 0:
                verdict = f"PLATEAU+ — {s.min():+.2f}..{s.max():+.2f} (spread {spread:.2f})"
            else:
                verdict = f"PLATEAU- — {s.min():+.2f}..{s.max():+.2f} (consistently negative)"
        else:
            verdict = "n/a"
        print(f"  VERDICT: {verdict}")
        robustness_map[family_name] = {"variants": fam_rows, "verdict": verdict}

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION C: DIVERSIFICATION / BLEND
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 90)
    print("SECTION C — DIVERSIFICATION: MOMENTUM + VALUE BLENDS")
    print("THE KEY QUESTION: does any blend beat momentum-alone on maxDD/Calmar/DSR?")
    print("=" * 90)

    # Correlations: momentum vs each value pnl series
    print("\n  Momentum vs value daily pnl CORRELATIONS:")
    print(f"  {'signal':<22}{'corr(mom,val)':>16}{'corr_abs':>10}")
    correlations = {}
    for name, pnl in pnl_value.items():
        common_idx = pnl_mom.dropna().index.intersection(pnl.dropna().index)
        if len(common_idx) > 30:
            c = float(np.corrcoef(pnl_mom.loc[common_idx].values, pnl.loc[common_idx].values)[0, 1])
        else:
            c = float("nan")
        correlations[name] = c
        print(f"  {name:<22}{c:>+16.3f}{abs(c):>10.3f}")

    # Pick the best standalone value signal for blending (by Sharpe, then Calmar as tiebreak)
    def _score(nm):
        m = standalone_metrics.get(nm, {})
        if not m:
            return (-99.0, -99.0)
        return (m.get("sharpe", -99.0), m.get("calmar", -99.0) if not np.isnan(m.get("calmar", float("nan"))) else -99.0)

    best_val_name = max(pnl_value.keys(), key=_score)
    best_val_pnl = pnl_value[best_val_name]
    print(f"\n  Best standalone value signal: {best_val_name}  (used for primary blend)")

    # Build blend scores and pnls
    # Equal-weight z-blend: z-score each signal first, then average
    z_mom = signals.zscore_cross_section(ens_score)
    z_val = signal_defs[best_val_name]   # already cross-sectionally z-scored by value_signals

    def _blend_pnl(mom_w: float, val_w: float, val_name: str = best_val_name) -> pd.Series:
        """Build a blend score from momentum + value with given weights."""
        z_v = signal_defs[val_name]
        blended = signals.blend([z_mom, z_v], weights=[mom_w, val_w])
        return _book_pnl(panel, blended, accrual=accrual)

    # Build blend variants
    blend_variants = {
        "momentum_alone":  pnl_mom,
        "blend_50/50":     _blend_pnl(0.5, 0.5),
        "blend_70/30":     _blend_pnl(0.7, 0.3),
        "blend_80/20":     _blend_pnl(0.8, 0.2),
        "blend_90/10":     _blend_pnl(0.9, 0.1),
    }

    # Also test ALL value signals at 70/30 to find best
    for val_name in pnl_value.keys():
        if val_name != best_val_name:
            blend_variants[f"70/30_{val_name}"] = _blend_pnl(0.7, 0.3, val_name)

    print(f"\n  Blend comparison (momentum + {best_val_name} unless noted):")
    print(f"  {'book':<24}{'sharpe':>8}{'ann%':>9}{'maxDD%':>9}{'calmar':>9}{'hit%':>8}")

    blend_metrics = {}
    for name, pnl in blend_variants.items():
        m = daily_metrics(pnl)
        blend_metrics[name] = m
        if m:
            cal_s = f"{m['calmar']:>9.2f}" if not np.isnan(m['calmar']) else f"{'nan':>9}"
            print(f"  {name:<24}{m['sharpe']:>8.2f}{100*m['ann']:>9.2f}{100*m['maxdd']:>9.2f}"
                  f"{cal_s}{100*m['hit']:>8.1f}")
        else:
            print(f"  {name:<24}  (too few days)")

    # Run harness on momentum-alone and best blend for DSR/PBO comparison
    print("\n  Running harness on momentum-alone and best blends for DSR/PBO ...")

    # Find the blend with best Calmar improvement vs momentum-alone — search ALL blends
    mom_calmar = m_mom.get("calmar", float("nan")) if m_mom else float("nan")
    best_blend_name = "blend_70/30"
    best_blend_calmar = blend_metrics.get("blend_70/30", {}).get("calmar", float("nan"))
    for bname, bm in blend_metrics.items():
        if bname == "momentum_alone":
            continue
        if bm and not np.isnan(bm.get("calmar", float("nan"))):
            if np.isnan(best_blend_calmar) or bm["calmar"] > best_blend_calmar:
                best_blend_calmar = bm["calmar"]
                best_blend_name = bname

    # Build harness menu: momentum + primary blends + best blend
    blend_menu_pnls = {
        "momentum_alone": pnl_mom,
        "blend_50/50":    blend_variants["blend_50/50"],
        "blend_70/30":    blend_variants["blend_70/30"],
        "blend_80/20":    blend_variants["blend_80/20"],
        "blend_90/10":    blend_variants["blend_90/10"],
    }
    # Ensure the best blend (possibly 70/30_dma200 etc) is in the harness menu
    if best_blend_name not in blend_menu_pnls:
        blend_menu_pnls[best_blend_name] = blend_variants[best_blend_name]

    harness_results = {}
    for name in ["momentum_alone", best_blend_name]:
        pnl = blend_variants[name]
        pkg = _SingleBookPackage(
            name=f"blend:{name}",
            selected_name=name,
            selected_pnl=pnl,
            menu_pnls=blend_menu_pnls,
        )
        rep = run_harness(pkg, costs=TAKER,
                          n_groups=N_GROUPS, k=K, purge=PURGE, embargo=EMBARGO)
        harness_results[name] = rep
        dsr_val = rep.dsr.get("dsr", float("nan"))
        pbo_val = rep.pbo.pbo if rep.pbo else float("nan")
        oos_sh = rep.pooled_oos.dist.get("sharpe", {}).get("median", float("nan"))
        oos_cal = rep.pooled_oos.dist.get("calmar", {}).get("median", float("nan"))
        print(f"  {name:<24} DSR={dsr_val:.3f}  PBO={pbo_val:.3f}  "
              f"OOS_Sharpe={oos_sh:.3f}  OOS_Calmar={oos_cal:.3f}")

    # Diversification verdict
    print("\n  DIVERSIFICATION VERDICT:")
    mom_m = blend_metrics.get("momentum_alone", m_mom)
    best_m = blend_metrics.get(best_blend_name, {})
    if mom_m and best_m:
        dd_improvement = mom_m.get("maxdd", 0) - best_m.get("maxdd", 0)
        calmar_improvement = (best_m.get("calmar", 0) - mom_m.get("calmar", 0)
                              if not (np.isnan(best_m.get("calmar", float("nan"))) or
                                      np.isnan(mom_m.get("calmar", float("nan")))) else float("nan"))
        sharpe_diff = best_m.get("sharpe", 0) - mom_m.get("sharpe", 0)
        print(f"  momentum_alone vs {best_blend_name}:")
        print(f"    maxDD: {100*mom_m.get('maxdd',0):.2f}% -> {100*best_m.get('maxdd',0):.2f}% "
              f"(improvement: {100*dd_improvement:+.2f}pp)")
        print(f"    Calmar: {mom_m.get('calmar',float('nan')):.2f} -> "
              f"{best_m.get('calmar',float('nan')):.2f} "
              f"(change: {calmar_improvement:+.2f})")
        print(f"    Sharpe: {mom_m.get('sharpe',0):.2f} -> {best_m.get('sharpe',0):.2f} "
              f"(change: {sharpe_diff:+.2f})")
        # Thresholds: >2pp maxDD reduction OR >0.30 Calmar improvement (at similar Sharpe)
        # These match the final verdict logic below
        if (dd_improvement > 0.02 and not np.isnan(blend_sh) and blend_sh >= mom_sh - 0.1) or \
           (not np.isnan(calmar_improvement) and calmar_improvement > 0.30 and
            not np.isnan(blend_sh) and blend_sh >= mom_sh - 0.1):
            print("    -> MEANINGFUL DIVERSIFICATION: blend reduces maxDD or improves Calmar materially")
        else:
            print("    -> MINIMAL/NO DIVERSIFICATION: blend improvements are noise-level")

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION D: CRASH BEHAVIOR
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 90)
    print("SECTION D — CRASH BEHAVIOR")
    print("=" * 90)

    # D1: HL-era momentum worst drawdown windows
    print("\n  D1: Momentum worst drawdown windows (HL era) — how did value/blend perform?")
    worst_windows = _drawdown_windows(pnl_mom, top_n=5)
    crash_table = []
    if worst_windows:
        print(f"\n  {'window':<32}{'mom_ann%':>10}{'val_ann%':>12}{'blend_ann%':>12}"
              f"{'mom_maxDD%':>12}{'val_maxDD%':>12}{'blend_maxDD%':>12}")
        for peak, trough, dd_val in worst_windows:
            label = f"{peak.date()} -> {trough.date()}"
            m_w = _window_metrics(pnl_mom, peak, trough)
            v_w = _window_metrics(best_val_pnl, peak, trough)
            b_w = _window_metrics(blend_variants[best_blend_name], peak, trough)
            row = {
                "window": label,
                "mom_dd": dd_val,
                "mom_metrics": _safe_dict(m_w) if m_w else {},
                "val_metrics": _safe_dict(v_w) if v_w else {},
                "blend_metrics": _safe_dict(b_w) if b_w else {},
            }
            crash_table.append(row)
            mom_ann = 100 * m_w.get("ann", float("nan")) if m_w else float("nan")
            val_ann = 100 * v_w.get("ann", float("nan")) if v_w else float("nan")
            blend_ann = 100 * b_w.get("ann", float("nan")) if b_w else float("nan")
            mom_dd = 100 * m_w.get("maxdd", float("nan")) if m_w else float("nan")
            val_dd = 100 * v_w.get("maxdd", float("nan")) if v_w else float("nan")
            blend_dd = 100 * b_w.get("maxdd", float("nan")) if b_w else float("nan")
            print(f"  {label:<32}{mom_ann:>10.1f}{val_ann:>12.1f}{blend_ann:>12.1f}"
                  f"{mom_dd:>12.2f}{val_dd:>12.2f}{blend_dd:>12.2f}")

    # D2: Bear-2022 panel
    print("\n  D2: Bear-2022 panel (Binance perps, 2021-01 to 2022-12-31)")
    print("  Checking data availability ...")
    bear_available = []
    bear_not_available = {}
    for coin in bear_fetch.BEAR_BASKET:
        ok_o, ok_f = bear_fetch.ensure_bear_coin(coin)
        if ok_o:
            pr = br._bear_daily_price(coin)
            if pr.dropna().__len__() >= max(MOM_LOOKBACKS) + 10:
                bear_available.append(coin)
            else:
                bear_not_available[coin] = "too short"
        else:
            bear_not_available[coin] = "OHLCV not available"

    bear_2022_result = {}
    if len(bear_available) >= 5:
        bear_panel = br.build_bear_panel(bear_available)
        bear_price = bear_panel["price"]
        bear_fund = bear_panel["funding"]
        bear_fwd = bear_panel["fwd_ret"]
        bear_accr = -(bear_fund.shift(-1))

        print(f"  Bear panel: {bear_price.index.min().date()} -> {bear_price.index.max().date()} "
              f" {len(bear_available)} coins")

        # Build books on the BEAR panel
        b_ens_score = signals.momentum_ensemble(bear_panel, lookbacks=MOM_LOOKBACKS)

        # Value signals on bear panel
        b_val_scores = {
            "dd90":   vs.drawdown_from_high(bear_panel, window=90),
            "dd180":  vs.drawdown_from_high(bear_panel, window=180),
            "dd_exp": vs.drawdown_from_high(bear_panel, window=None),
            "dma100": vs.dist_from_ma(bear_panel, ma_window=100),
            "dma200": vs.dist_from_ma(bear_panel, ma_window=200),
            "ltr120": vs.long_term_reversal(bear_panel, lookback=120),
            "ltr180": vs.long_term_reversal(bear_panel, lookback=180),
        }
        b_pnl_mom = _book_pnl(bear_panel, b_ens_score, accrual=bear_accr)
        b_pnl_val_best = _book_pnl(bear_panel, b_val_scores[best_val_name], accrual=bear_accr)

        # Best blend on bear panel
        b_z_mom = signals.zscore_cross_section(b_ens_score)
        b_z_val = b_val_scores[best_val_name]
        b_blend_score = signals.blend([b_z_mom, b_z_val],
                                      weights=[0.7, 0.3] if best_blend_name == "blend_70/30" else [0.5, 0.5])
        b_pnl_blend = _book_pnl(bear_panel, b_blend_score, accrual=bear_accr)

        # Trim to after warmup
        n_valid = b_ens_score.notna().sum(axis=1)
        warmup_end = (n_valid >= 2).idxmax()
        b_pnl_mom = b_pnl_mom[b_pnl_mom.index >= warmup_end]
        b_pnl_val_best = b_pnl_val_best[b_pnl_val_best.index >= warmup_end]
        b_pnl_blend = b_pnl_blend[b_pnl_blend.index >= warmup_end]

        # All value signals on bear (for comparison)
        b_pnl_vals = {}
        for nm, sc in b_val_scores.items():
            b_pnl_vals[nm] = _book_pnl(bear_panel, sc, accrual=bear_accr)

        # Full bear window
        print(f"\n  Full bear window ({b_pnl_mom.index.min().date()} - "
              f"{b_pnl_mom.index.max().date()}):")
        print(f"  {'book':<24}{'sharpe':>8}{'ann%':>9}{'maxDD%':>9}{'calmar':>9}")
        for label, pnl in [
            ("momentum_alone", b_pnl_mom),
            (f"value({best_val_name})", b_pnl_val_best),
            (f"{best_blend_name}", b_pnl_blend),
        ]:
            bm = daily_metrics(pnl)
            if bm:
                cal_s = f"{bm['calmar']:>9.2f}" if not np.isnan(bm['calmar']) else f"{'nan':>9}"
                print(f"  {label:<24}{bm['sharpe']:>8.2f}{100*bm['ann']:>9.2f}"
                      f"{100*bm['maxdd']:>9.2f}{cal_s}")
            else:
                print(f"  {label:<24}  (too few days)")
            bear_2022_result[label] = _safe_dict(bm) if bm else {}

        # Year-by-year bear
        print(f"\n  Bear panel year-by-year:")
        print(f"  {'year':<8}{'book':<24}{'sharpe':>8}{'ann%':>9}{'maxDD%':>9}")
        bear_yearly = {}
        for yr in [2021, 2022]:
            for label, pnl in [
                ("momentum_alone", b_pnl_mom),
                (f"value({best_val_name})", b_pnl_val_best),
                (f"{best_blend_name}", b_pnl_blend),
            ]:
                msk = pnl.index.year == yr
                bm = daily_metrics(pnl[msk])
                if bm:
                    print(f"  {yr:<8}{label:<24}{bm['sharpe']:>8.2f}"
                          f"{100*bm['ann']:>9.2f}{100*bm['maxdd']:>9.2f}")
                else:
                    print(f"  {yr:<8}{label:<24}  (too few days)")
                bear_yearly[f"{yr}_{label}"] = _safe_dict(bm) if bm else {}

        # Value signals on bear
        print(f"\n  ALL value signals on bear panel:")
        print(f"  {'signal':<22}{'sharpe':>8}{'ann%':>9}{'maxDD%':>9}{'calmar':>9}")
        bear_val_metrics = {}
        for nm, pnl in b_pnl_vals.items():
            pnl_trim = pnl[pnl.index >= warmup_end]
            bm = daily_metrics(pnl_trim)
            if bm:
                cal_s = f"{bm['calmar']:>9.2f}" if not np.isnan(bm['calmar']) else f"{'nan':>9}"
                print(f"  {nm:<22}{bm['sharpe']:>8.2f}{100*bm['ann']:>9.2f}"
                      f"{100*bm['maxdd']:>9.2f}{cal_s}")
            else:
                print(f"  {nm:<22}  (too few days)")
            bear_val_metrics[nm] = _safe_dict(bm) if bm else {}

    else:
        print(f"  SKIP: only {len(bear_available)} coins available (need >= 5)")
        bear_2022_result = {}
        bear_yearly = {}
        bear_val_metrics = {}

    # ══════════════════════════════════════════════════════════════════════════
    # FINAL VERDICT
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 90)
    print("FINAL VERDICT")
    print("=" * 90)

    # Standalone verdict
    val_sharpes = [standalone_metrics[n].get("sharpe", float("nan"))
                   for n in pnl_value if standalone_metrics.get(n)]
    val_sharpes_clean = [s for s in val_sharpes if not np.isnan(s)]
    best_val_sharpe = max(val_sharpes_clean) if val_sharpes_clean else float("nan")
    median_val_sharpe = float(np.median(val_sharpes_clean)) if val_sharpes_clean else float("nan")
    val_dsrs = {n: standalone_harness[n].dsr.get("dsr", float("nan"))
                for n in pnl_value}
    best_val_dsr = max((v for v in val_dsrs.values() if not np.isnan(v)), default=float("nan"))

    print(f"\n1. STANDALONE VALUE:")
    print(f"   Best Sharpe:  {best_val_sharpe:+.3f} ({best_val_name})")
    print(f"   Median Sharpe across 7 signals: {median_val_sharpe:+.3f}")
    print(f"   Best DSR: {best_val_dsr:.3f}")
    all_positive = all(s > 0 for s in val_sharpes_clean) if val_sharpes_clean else False
    if best_val_sharpe < 0.3:
        val_verdict = "DEAD/FLAT — value has no standalone edge (Sharpe < 0.3)"
    elif best_val_sharpe < 0.6 and not all_positive:
        val_verdict = "WEAK STANDALONE — some positive but inconsistent across windows"
    elif all_positive and median_val_sharpe > 0.4:
        val_verdict = "MODEST STANDALONE EDGE — consistent positive Sharpe across windows"
    else:
        val_verdict = f"MIXED — best {best_val_sharpe:+.2f} but not uniform across windows"
    print(f"   Standalone verdict: {val_verdict}")

    print(f"\n2. DIVERSIFICATION (momentum + value blend):")
    mom_sh = m_mom.get("sharpe", float("nan")) if m_mom else float("nan")
    mom_dd = m_mom.get("maxdd", float("nan")) if m_mom else float("nan")
    mom_cal = m_mom.get("calmar", float("nan")) if m_mom else float("nan")
    best_blend_m = blend_metrics.get(best_blend_name, {})
    blend_sh = best_blend_m.get("sharpe", float("nan")) if best_blend_m else float("nan")
    blend_dd = best_blend_m.get("maxdd", float("nan")) if best_blend_m else float("nan")
    blend_cal = best_blend_m.get("calmar", float("nan")) if best_blend_m else float("nan")
    blend_dsr = harness_results.get(best_blend_name, {}).dsr.get("dsr", float("nan")) if best_blend_name in harness_results else float("nan")
    mom_dsr = harness_results.get("momentum_alone", {}).dsr.get("dsr", float("nan")) if "momentum_alone" in harness_results else float("nan")
    best_val_corr = correlations.get(best_val_name, float("nan"))
    print(f"   momentum_alone: Sharpe {mom_sh:+.2f}, maxDD {100*mom_dd:.1f}%, Calmar {mom_cal:.2f}, DSR {mom_dsr:.3f}")
    print(f"   {best_blend_name}: Sharpe {blend_sh:+.2f}, maxDD {100*blend_dd:.1f}%, Calmar {blend_cal:.2f}, DSR {blend_dsr:.3f}")
    print(f"   Correlation (mom vs {best_val_name}): {best_val_corr:+.3f}")
    if not (np.isnan(mom_dd) or np.isnan(blend_dd)):
        dd_delta = mom_dd - blend_dd
        cal_delta = (blend_cal - mom_cal) if not (np.isnan(blend_cal) or np.isnan(mom_cal)) else float("nan")
        if dd_delta > 0.02 and not np.isnan(blend_sh) and blend_sh >= mom_sh - 0.1:
            div_verdict = f"REAL DIVERSIFIER — blend ({best_blend_name}) reduces maxDD by {100*dd_delta:.1f}pp at similar Sharpe"
        elif not np.isnan(cal_delta) and cal_delta > 0.3 and not np.isnan(blend_sh) and blend_sh >= mom_sh - 0.1:
            div_verdict = f"MODEST DIVERSIFIER — blend ({best_blend_name}) improves Calmar by {cal_delta:.2f}"
        elif best_val_corr < -0.70:
            # Strongly anti-correlated: blending just dilutes momentum
            div_verdict = f"ANTI-CORRELATED (r={best_val_corr:+.2f}) — value cancels momentum signal, no blend benefit"
        else:
            div_verdict = (f"NO MEANINGFUL DIVERSIFICATION — best blend ({best_blend_name}) does not "
                           f"materially improve maxDD ({100*dd_delta:+.1f}pp) or Calmar "
                           f"({cal_delta:+.2f}) over momentum-alone")
    else:
        div_verdict = "UNABLE TO EVALUATE (insufficient data)"
    print(f"   Blend verdict: {div_verdict}")

    print(f"\n3. BEAR-2022:")
    if bear_2022_result:
        bm_bear = bear_2022_result.get("momentum_alone", {})
        bv_bear = bear_2022_result.get(f"value({best_val_name})", {})
        bb_bear = bear_2022_result.get(f"{best_blend_name}", {})
        bm_sh = bm_bear.get("sharpe", float("nan"))
        bv_sh = bv_bear.get("sharpe", float("nan"))
        bb_sh = bb_bear.get("sharpe", float("nan"))
        print(f"   momentum_alone:  Sharpe {bm_sh:+.3f}")
        print(f"   value({best_val_name}): Sharpe {bv_sh:+.3f}")
        print(f"   {best_blend_name}: Sharpe {bb_sh:+.3f}")
        if not np.isnan(bm_sh) and not np.isnan(bv_sh) and not np.isnan(bb_sh):
            if bv_sh > bm_sh + 0.1:
                bear_verdict = f"VALUE DIVERSIFIES: value {bv_sh:+.2f} beats momentum {bm_sh:+.2f} in the bear"
            elif bb_sh > bm_sh + 0.1:
                bear_verdict = f"BLEND HELPS IN BEAR: blend {bb_sh:+.2f} vs momentum {bm_sh:+.2f}"
            elif bv_sh < bm_sh - 0.1:
                bear_verdict = f"VALUE HURTS IN BEAR: value {bv_sh:+.2f} worse than momentum {bm_sh:+.2f}"
            else:
                bear_verdict = f"SIMILAR BEAR PERFORMANCE (mom={bm_sh:+.2f}, val={bv_sh:+.2f}, blend={bb_sh:+.2f})"
        else:
            bear_verdict = "BEAR DATA INSUFFICIENT"
        print(f"   Bear verdict: {bear_verdict}")
    else:
        print("   (bear panel unavailable)")
        bear_verdict = "UNAVAILABLE"

    print(f"\n4. PLAIN-LANGUAGE FINAL VERDICT:")
    # Consolidate
    if val_sharpes_clean and abs(max(val_sharpes_clean)) < 0.3:
        final = ("DEAD — crypto value signals have no standalone edge (all Sharpe < 0.3) "
                 "and do not diversify momentum. Like short reversal: avoid.")
    elif div_verdict.startswith("REAL DIVERSIFIER") or div_verdict.startswith("MODEST DIVERSIFIER"):
        final = (f"DIVERSIFIER (FX-style) — value is weak standalone (best Sharpe {best_val_sharpe:.2f}) "
                 f"but blending with momentum {best_blend_name} meaningfully reduces drawdown/improves Calmar. "
                 f"Add to the book at ~20-30% weight as a crash cushion.")
    elif div_verdict.startswith("NO MEANINGFUL"):
        final = (f"WEAK STANDALONE, NO BLEND BENEFIT — value signals show modest standalone edge "
                 f"(best Sharpe {best_val_sharpe:.2f}, DSR {best_val_dsr:.3f}) but do NOT improve "
                 f"the momentum blend. Benefit below the FX bar. Do not add to live book.")
    else:
        final = (f"MIXED — value signals show some signal ({val_verdict}), blend impact: {div_verdict}. "
                 f"Bear evidence: {bear_verdict}. Best-value DSR {best_val_dsr:.3f}.")
    print(f"   {final}")
    print()

    # ══════════════════════════════════════════════════════════════════════════
    # OUTPUT JSON
    # ══════════════════════════════════════════════════════════════════════════
    print("=" * 90)
    print("Saving run_value.json ...")

    def _harness_to_dict(rep):
        if rep is None:
            return {}
        try:
            return to_dict(rep)
        except Exception as e:
            return {"error": str(e)}

    def _safe_metrics(m: dict) -> dict:
        if not m:
            return {}
        return _safe_dict(m)

    output = {
        "description": "Crypto value factor study — CPCV + DSR + PBO via validation_harness",
        "config": {
            "costs_bps": COSTS_BPS,
            "rebal_every": REBAL_EVERY,
            "tercile_frac": TERCILE_FRAC,
            "purge_days": PURGE,
            "embargo_days": EMBARGO,
            "n_groups": N_GROUPS,
            "k": K,
            "accrual": "-funding.shift(-1)",
            "annualization": "sqrt(365) daily (honest)",
        },
        "momentum_baseline": _safe_metrics(m_mom),
        "section_A_standalone": {
            nm: {
                "daily_metrics": _safe_metrics(standalone_metrics.get(nm, {})),
                "dsr": standalone_harness[nm].dsr if nm in standalone_harness else {},
                "pbo": standalone_harness[nm].pbo.pbo if nm in standalone_harness and standalone_harness[nm].pbo else float("nan"),
                "oos_sharpe_median": (standalone_harness[nm].pooled_oos.dist.get("sharpe", {}).get("median", float("nan"))
                                     if nm in standalone_harness else float("nan")),
            }
            for nm in pnl_value
        },
        "section_B_robustness": {
            fam: {
                "verdict": robustness_map[fam]["verdict"],
            }
            for fam in robustness_map
        },
        "section_C_blend": {
            "best_value_signal": best_val_name,
            "correlations_mom_vs_value": {nm: float(c) for nm, c in correlations.items()},
            "blend_metrics": {nm: _safe_metrics(m) for nm, m in blend_metrics.items()},
            "harness_momentum_alone": _harness_to_dict(harness_results.get("momentum_alone")),
            "harness_best_blend": _harness_to_dict(harness_results.get(best_blend_name)),
            "best_blend_name": best_blend_name,
            "diversification_verdict": div_verdict,
        },
        "section_D_crash": {
            "hl_era_crash_windows": crash_table,
            "bear_2022_full_window": {k: _safe_metrics(v) if isinstance(v, dict) else v
                                      for k, v in bear_2022_result.items()},
            "bear_2022_yearly": {k: _safe_metrics(v) if isinstance(v, dict) else v
                                 for k, v in (bear_yearly.items() if bear_yearly else {}.items())},
            "bear_2022_value_signals": {k: _safe_metrics(v) if isinstance(v, dict) else v
                                        for k, v in (bear_val_metrics.items() if bear_val_metrics else {}.items())},
            "bear_verdict": bear_verdict,
        },
        "final_verdict": {
            "standalone_verdict": val_verdict,
            "diversification_verdict": div_verdict,
            "bear_verdict": bear_verdict,
            "final": final,
        },
    }

    out_path = _HERE / "run_value.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str))
    print(f"JSON -> {out_path}")
    print("DONE.")


if __name__ == "__main__":
    main()
