"""
TIME-SERIES MEAN-REVERSION — Rigorous standalone crypto strategy test.

KEY QUESTION: does per-asset "buy low / sell high" (time-series z-score MR)
have a real after-cost edge at daily scale, or is it cost-eaten noise?

DESIGN:
  - FROZEN 34-coin universe (same as validated momentum book)
  - accrual = -funding.shift(-1)  (canonical)
  - TWO book variants:
      RAW TS-MR:       per-asset MR positions, gross Σ|w| = 2 (same as momentum tercile)
      BETA-NEUTRAL MR: cross-demean each day (Σw=0), gross = 2
  - COST GRID: costs_bps ∈ {2, 8.5, 15} × rebal_every ∈ {1, 2, 3, 5}
  - ROBUSTNESS MAP: z-score window ∈ {3, 5, 10, 20, 40} + alt (raw-distance)
  - HARNESS VERDICT: best net-of-cost variant through CPCV → DSR + PBO + OOS
  - REGIME TIE-IN: correlation with momentum_ensemble + performance in
    momentum's flat/negative months + brief bear-2022 view

HONEST DAILY ANNUALIZATION: metrics_daily.daily_metrics (sqrt(365)).
If the harness prints sqrt(8760) numbers, they are labeled.

Run:
  cd research/cross_sectional/crypto
  PYTHONPATH=.../research:.../validation_harness:.../cross_sectional:.../crypto \\
    python -u run_mr.py

Output: run_mr.json + printed report.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import cryptodata
import signals as sig_mod
import xsec
import bear_regime as br
import bear_fetch
import mr_signals as mrs
from metrics_daily import daily_metrics
from contract import Strategy
from runner import run_cpcv, _DIST_KEYS
from splitter import cpcv
from harness import run_harness, save_json, to_dict
from report import print_report
from costs import Costs, TAKER

_HERE = Path(__file__).parent
UNIVERSE_JSON = _HERE / "universe.json"

# ── Config ───────────────────────────────────────────────────────────────────
MOM_LOOKBACKS   = (14, 21, 30, 45, 60)   # validated momentum ensemble
TARGET_GROSS    = 2.0                    # same as momentum tercile book
PURGE           = 40                     # >= max MR window tested (40); seam-safe
EMBARGO         = 7                      # days, mirrors momentum book
N_GROUPS        = 6
K               = 2
MR_WINDOWS      = (3, 5, 10, 20, 40)    # robustness sweep
COST_GRID       = (2.0, 8.5, 15.0)      # bps — maker-ish / taker / stress
REBAL_GRID      = (1, 2, 3, 5)          # daily cadences for MR

# The "canonical" cadence for the primary test (daily; MR needs fast rebal)
PRIMARY_REBAL   = 1
PRIMARY_WINDOW  = 10   # picked AFTER seeing robustness map below; default here is 10
PRIMARY_COSTS   = 8.5  # taker


def _frozen_universe() -> list[str]:
    return list(json.loads(UNIVERSE_JSON.read_text())["coins"])


def _book_pnl_from_weights(w: pd.DataFrame, fwd_ret: pd.DataFrame,
                            accrual: pd.DataFrame,
                            costs_bps: float, rebal_every: int) -> pd.Series:
    """Compute net daily pnl for arbitrary weights (MR weights, not rank_to_weights)."""
    return xsec.portfolio_returns(
        w, fwd_ret,
        costs_bps=costs_bps,
        rebal_every=rebal_every,
        accrual=accrual,
    )


def _fmt(m: dict, name: str = "") -> str:
    if not m:
        return f"  {name:<28} (too few days)"
    cal = m['calmar']
    cal_s = f"{cal:>8.2f}" if not np.isnan(cal) else "     nan"
    return (f"  {name:<28} Sharpe {m['sharpe']:>+6.2f}  ann {100*m['ann']:>+7.2f}%  "
            f"maxDD {100*m['maxdd']:>6.2f}%  Calmar{cal_s}  hit {100*m['hit']:>5.1f}%"
            f"  n={m['n']}")


def _safe(v):
    if isinstance(v, (np.floating, np.float64, np.float32)):
        return float(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    return v


def _safe_dict(d: dict) -> dict:
    return {k: _safe(v) for k, v in d.items()}


def _turnover(w: pd.DataFrame, rebal_every: int) -> float:
    """Average daily turnover = avg Σ|Δw| per day, accounting for rebal_every."""
    w_filled = w.fillna(0.0)
    total_tv = 0.0
    n_rebal = 0
    for i in range(len(w_filled)):
        if i % rebal_every == 0:
            prev = w_filled.iloc[i - 1] if i > 0 else pd.Series(0.0, index=w_filled.columns)
            tv = (w_filled.iloc[i] - prev).abs().sum()
            total_tv += float(tv)
            n_rebal += 1
    # avg turnover per rebalance event, then per day
    return (total_tv / max(n_rebal, 1)) / rebal_every if n_rebal > 0 else 0.0


def _cost_drag(w: pd.DataFrame, rebal_every: int, costs_bps: float) -> float:
    """Estimated annualized cost drag (%/yr) = avg_daily_turnover * costs_bps/1e4 * 365."""
    tv = _turnover(w, rebal_every)
    return tv * (costs_bps / 1e4) * 365.0


# ── Harness adapter (mirrors _SingleBookPackage from run_value.py) ─────────────

class _FixedBook:
    def __init__(self, name: str, pnl: pd.Series):
        self.name = name
        self._pnl = pnl.values

    def fit(self, df, train_idx, costs):
        return None

    def simulate(self, df, seg, config, costs):
        return self._pnl[seg]


class _SingleBookPackage:
    """Package wrapping ONE selected pnl series + a menu of alternatives for
    DSR deflation and PBO. Follows the same pattern as run_value.py."""
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


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    pd.set_option("display.width", 200)

    print("=" * 100)
    print("TIME-SERIES MEAN-REVERSION — STANDALONE CRYPTO STRATEGY TEST (CPCV + DSR + PBO)")
    print("KEY QUESTION: does per-asset 'buy low/sell high' survive real taker costs at daily scale?")
    print("=" * 100)

    coins = _frozen_universe()
    panel = cryptodata.load_panel(coins=coins)
    px = panel["price"]
    funding = panel["funding"]
    fwd_ret = panel["fwd_ret"]
    accrual = -(funding.shift(-1))   # canonical: -funding.shift(-1)

    print(f"\nPanel: {px.shape[0]} days x {px.shape[1]} coins  "
          f"({px.index.min().date()} -> {px.index.max().date()})")
    print(f"Frozen 34-coin universe, accrual=-funding.shift(-1)")
    print(f"Cost grid: {COST_GRID} bps  |  Rebal grid: {REBAL_GRID}d  |  "
          f"Window sweep: {MR_WINDOWS}")
    print(f"CPCV: n_groups={N_GROUPS} k={K} purge={PURGE}d (>=max window {max(MR_WINDOWS)}) "
          f"embargo={EMBARGO}d")
    print(f"Annualization: sqrt(365) daily (honest)")

    # ── Momentum ensemble baseline ──────────────────────────────────────────────
    ens_score = sig_mod.momentum_ensemble(panel, lookbacks=MOM_LOOKBACKS)
    w_mom = xsec.rank_to_weights(ens_score, tercile_frac=1/3)
    pnl_mom = xsec.portfolio_returns(w_mom, fwd_ret, costs_bps=8.5,
                                     rebal_every=7, accrual=accrual)
    m_mom = daily_metrics(pnl_mom)
    print(f"\nMomentum ensemble baseline (WITH funding accrual):")
    print(_fmt(m_mom, "momentum_ensemble"))

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION A — RAW vs BETA-NEUTRAL AT PRIMARY PARAMS
    # Compare gross-exposure and dollar-neutral MR books head-to-head.
    # Shows whether any edge is reversion alpha or net-long crypto beta.
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("SECTION A — RAW TS-MR vs BETA-NEUTRAL TS-MR (primary params)")
    print(f"  window={PRIMARY_WINDOW}, costs={PRIMARY_COSTS}bps, rebal_every={PRIMARY_REBAL}d")
    print(f"  RAW:          per-asset positions, gross=2  (keeps net exposure / 'buy the dip')")
    print(f"  BETA-NEUTRAL: cross-demean each day (Σw=0), gross=2  (pure reversion alpha)")
    print("=" * 100)

    mr_z   = mrs.ts_mr_signal(panel, PRIMARY_WINDOW)
    w_raw  = mrs.normalize_weights(mr_z, target_gross=TARGET_GROSS)
    w_bn   = mrs.beta_neutral_weights(mr_z, target_gross=TARGET_GROSS)

    pnl_raw = _book_pnl_from_weights(w_raw, fwd_ret, accrual, PRIMARY_COSTS, PRIMARY_REBAL)
    pnl_bn  = _book_pnl_from_weights(w_bn,  fwd_ret, accrual, PRIMARY_COSTS, PRIMARY_REBAL)

    m_raw = daily_metrics(pnl_raw)
    m_bn  = daily_metrics(pnl_bn)

    tv_raw = _turnover(w_raw, PRIMARY_REBAL)
    tv_bn  = _turnover(w_bn, PRIMARY_REBAL)
    drag_raw = tv_raw * (PRIMARY_COSTS / 1e4) * 365.0 * 100
    drag_bn  = tv_bn  * (PRIMARY_COSTS / 1e4) * 365.0 * 100

    print(f"\n{'book':<32}{'sharpe':>8}{'ann%':>9}{'maxDD%':>9}{'calmar':>9}{'hit%':>8}  "
          f"turnover/day  cost_drag%/yr")
    for name, m, tv, drag in [
        ("RAW_TS-MR", m_raw, tv_raw, drag_raw),
        ("BETA-NEUTRAL_TS-MR", m_bn, tv_bn, drag_bn),
        ("momentum_ensemble(baseline)", m_mom, None, None),
    ]:
        if m:
            cal_s = f"{m['calmar']:>9.2f}" if not np.isnan(m['calmar']) else f"{'nan':>9}"
            tv_s  = f"{tv:.4f}" if tv is not None else "  --"
            dr_s  = f"{drag:.2f}%/yr" if drag is not None else "  --"
            print(f"  {name:<30}{m['sharpe']:>8.2f}{100*m['ann']:>9.2f}{100*m['maxdd']:>9.2f}"
                  f"{cal_s}{100*m['hit']:>8.1f}  {tv_s:>12}  {dr_s:>14}")
        else:
            print(f"  {name:<30}  (too few days)")

    # ── PNLS FOR GROSS (pre-cost) COMPARISON ────────────────────────────────
    pnl_raw_gross = _book_pnl_from_weights(w_raw, fwd_ret, accrual, 0.0, PRIMARY_REBAL)
    pnl_bn_gross  = _book_pnl_from_weights(w_bn,  fwd_ret, accrual, 0.0, PRIMARY_REBAL)
    m_raw_g = daily_metrics(pnl_raw_gross)
    m_bn_g  = daily_metrics(pnl_bn_gross)
    print(f"\nGROSS (pre-cost) Sharpe:")
    print(f"  RAW_TS-MR         gross Sharpe {m_raw_g.get('sharpe', float('nan')):+.3f}  "
          f"-> net {m_raw.get('sharpe', float('nan')):+.3f}")
    print(f"  BETA-NEUTRAL_TS-MR gross Sharpe {m_bn_g.get('sharpe', float('nan')):+.3f}  "
          f"-> net {m_bn.get('sharpe', float('nan')):+.3f}")

    section_a = {
        "raw": _safe_dict(m_raw) if m_raw else {},
        "beta_neutral": _safe_dict(m_bn) if m_bn else {},
        "raw_gross": _safe_dict(m_raw_g) if m_raw_g else {},
        "beta_neutral_gross": _safe_dict(m_bn_g) if m_bn_g else {},
        "turnover_raw": float(tv_raw),
        "turnover_bn": float(tv_bn),
        "cost_drag_raw_pct_yr": float(drag_raw),
        "cost_drag_bn_pct_yr": float(drag_bn),
    }

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION B — COST-DECAY TABLE
    # For each (book_type x costs_bps x rebal_every): net Sharpe, cost drag.
    # The question: at what cost level does the edge disappear?
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("SECTION B — COST DECAY (window=10, book=BETA-NEUTRAL unless RAW noted)")
    print(f"  costs ∈ {COST_GRID} bps  |  rebal_every ∈ {REBAL_GRID}d")
    print("=" * 100)

    HDR = (f"  {'book_variant':<22}{'costs':>8}{'rebal':>8}{'net_sh':>9}"
           f"{'net_ann%':>10}{'gross_sh':>10}{'drag%/yr':>10}{'tv/day':>10}")
    print(HDR)

    cost_decay = {}
    for btype, w_b in [("RAW", w_raw), ("BETA-NEUTRAL", w_bn)]:
        for costs_bps in COST_GRID:
            for re in REBAL_GRID:
                # Recompute weights at this rebal cadence
                # (weights themselves don't change; rebal_every affects cost charging)
                p_net  = _book_pnl_from_weights(w_b, fwd_ret, accrual, costs_bps, re)
                p_gr   = _book_pnl_from_weights(w_b, fwd_ret, accrual, 0.0, re)
                m_net  = daily_metrics(p_net)
                m_gr   = daily_metrics(p_gr)
                tv     = _turnover(w_b, re)
                drag   = tv * (costs_bps / 1e4) * 365.0 * 100
                key = f"{btype}_c{int(costs_bps*10)}_r{re}"
                cost_decay[key] = {
                    "book_type": btype,
                    "costs_bps": float(costs_bps),
                    "rebal_every": int(re),
                    "net_sharpe": float(m_net.get("sharpe", float("nan"))) if m_net else float("nan"),
                    "gross_sharpe": float(m_gr.get("sharpe", float("nan"))) if m_gr else float("nan"),
                    "cost_drag_pct_yr": float(drag),
                    "turnover_per_day": float(tv),
                }
                sh_s  = f"{m_net.get('sharpe', float('nan')):>9.3f}" if m_net else f"{'n/a':>9}"
                ann_s = f"{100*m_net.get('ann', float('nan')):>10.2f}" if m_net else f"{'n/a':>10}"
                gsh_s = f"{m_gr.get('sharpe', float('nan')):>10.3f}" if m_gr else f"{'n/a':>10}"
                print(f"  {btype:<22}{costs_bps:>8.1f}{re:>8d}{sh_s}{ann_s}{gsh_s}"
                      f"{drag:>10.2f}{tv:>10.4f}")

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION C — ROBUSTNESS MAP
    # Sweep z-score window ∈ {3,5,10,20,40} + alt (raw-distance) signal.
    # Plateau of positive net Sharpe = real edge. Single-window spike = mirage.
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("SECTION C — ROBUSTNESS MAP (z-score window sweep + alternative signal)")
    print(f"  book=BETA-NEUTRAL, costs=8.5bps, rebal_every=1d")
    print("=" * 100)

    HDR2 = (f"  {'signal':<28}{'window':>8}{'sharpe':>9}{'ann%':>9}"
            f"{'maxDD%':>9}{'calmar':>9}{'hit%':>8}{'drag%/yr':>12}")
    print(HDR2)

    robustness = {}
    best_net_sharpe = -np.inf
    best_variant_name = None
    best_variant_pnl  = None
    best_variant_w    = None

    all_bn_sharpes = []
    all_raw_sharpes = []

    for window in MR_WINDOWS:
        for sig_name, get_signal in [
            ("ts_zscore_MR", lambda w: mrs.ts_mr_signal(panel, w)),
            ("raw_dist_MR",  lambda w: mrs.ts_mr_raw_distance(panel, w)),
        ]:
            s_raw  = get_signal(window)
            w_b_re = mrs.beta_neutral_weights(s_raw, target_gross=TARGET_GROSS)
            w_r_re = mrs.normalize_weights(s_raw, target_gross=TARGET_GROSS)

            p_bn   = _book_pnl_from_weights(w_b_re, fwd_ret, accrual, 8.5, PRIMARY_REBAL)
            p_ra   = _book_pnl_from_weights(w_r_re, fwd_ret, accrual, 8.5, PRIMARY_REBAL)
            m_bn_  = daily_metrics(p_bn)
            m_ra_  = daily_metrics(p_ra)
            tv_    = _turnover(w_b_re, PRIMARY_REBAL)
            drag_  = tv_ * (8.5 / 1e4) * 365.0 * 100

            sh_bn = m_bn_.get("sharpe", float("nan")) if m_bn_ else float("nan")
            sh_ra = m_ra_.get("sharpe", float("nan")) if m_ra_ else float("nan")

            robustness[f"{sig_name}_w{window}_BN"] = {
                "signal": sig_name, "window": window, "book": "beta_neutral",
                "sharpe": float(sh_bn), "ann": float(m_bn_.get("ann", float("nan"))) if m_bn_ else float("nan"),
                "maxdd": float(m_bn_.get("maxdd", float("nan"))) if m_bn_ else float("nan"),
                "cost_drag_pct_yr": float(drag_),
                "turnover_per_day": float(tv_),
            }
            robustness[f"{sig_name}_w{window}_RAW"] = {
                "signal": sig_name, "window": window, "book": "raw",
                "sharpe": float(sh_ra),
            }

            if sig_name == "ts_zscore_MR":
                all_bn_sharpes.append(sh_bn)
                all_raw_sharpes.append(sh_ra)

            # Track best net-of-cost beta-neutral variant
            if not np.isnan(sh_bn) and sh_bn > best_net_sharpe:
                best_net_sharpe = sh_bn
                best_variant_name = f"{sig_name}_w{window}_BN"
                best_variant_pnl  = p_bn
                best_variant_w    = w_b_re

            cal_s = f"{m_bn_.get('calmar', float('nan')):>9.2f}" if m_bn_ and not np.isnan(m_bn_.get('calmar', float('nan'))) else f"{'nan':>9}"
            ann_s = f"{100*m_bn_.get('ann', float('nan')):>9.2f}" if m_bn_ else f"{'n/a':>9}"
            dd_s  = f"{100*m_bn_.get('maxdd', float('nan')):>9.2f}" if m_bn_ else f"{'n/a':>9}"
            hit_s = f"{100*m_bn_.get('hit', float('nan')):>8.1f}" if m_bn_ else f"{'n/a':>8}"
            print(f"  {sig_name}_w{window:<5}(BN){sh_bn:>9.3f}{ann_s}{dd_s}{cal_s}{hit_s}{drag_:>12.2f}")

    # Robustness verdict
    valid_bn = [s for s in all_bn_sharpes if not np.isnan(s)]
    if valid_bn:
        s_arr = np.array(valid_bn)
        same_sign = (s_arr > 0).all() or (s_arr < 0).all()
        spread = s_arr.max() - s_arr.min()
        best_s = s_arr.max()
        worst_s = s_arr.min()
        if best_s <= 0:
            plateau_verdict = f"PLATEAU- — all non-positive (max {best_s:+.3f})"
        elif not same_sign:
            plateau_verdict = f"MIXED-SIGN — sign flips across windows (min {worst_s:+.3f} max {best_s:+.3f})"
        elif spread < 0.20:
            plateau_verdict = f"PLATEAU (spread {spread:.3f}) — consistent across windows {worst_s:+.3f}..{best_s:+.3f}"
        else:
            plateau_verdict = f"SPIKE/UNSTABLE — spread {spread:.3f} (min {worst_s:+.3f} max {best_s:+.3f})"
    else:
        plateau_verdict = "INSUFFICIENT DATA"

    print(f"\n  ROBUSTNESS VERDICT (beta-neutral z-score MR, windows {MR_WINDOWS}):")
    print(f"  Net Sharpes: {[f'{s:.3f}' for s in all_bn_sharpes]}")
    print(f"  -> {plateau_verdict}")

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION D — HARNESS VERDICT (CPCV + DSR + PBO)
    # Best net-of-cost variant through the rigorous harness.
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("SECTION D — HARNESS VERDICT (CPCV + DSR + PBO)")
    print(f"  Best net-of-cost variant: {best_variant_name}")
    print(f"  Net Sharpe (full period, daily): {best_net_sharpe:+.3f}")
    print("=" * 100)

    # Build a menu of BETA-NEUTRAL MR variants for DSR deflation + PBO
    menu_pnls = {}
    for window in MR_WINDOWS:
        for sig_name, get_signal in [
            ("ts_zscore", lambda w: mrs.ts_mr_signal(panel, w)),
            ("raw_dist",  lambda w: mrs.ts_mr_raw_distance(panel, w)),
        ]:
            s_raw = get_signal(window)
            w_b_  = mrs.beta_neutral_weights(s_raw, target_gross=TARGET_GROSS)
            p_    = _book_pnl_from_weights(w_b_, fwd_ret, accrual, 8.5, PRIMARY_REBAL)
            menu_pnls[f"{sig_name}_w{window}"] = p_

    # Also include the RAW best window for PBO context
    for window in MR_WINDOWS:
        s_raw = mrs.ts_mr_signal(panel, window)
        w_r_  = mrs.normalize_weights(s_raw, target_gross=TARGET_GROSS)
        p_    = _book_pnl_from_weights(w_r_, fwd_ret, accrual, 8.5, PRIMARY_REBAL)
        menu_pnls[f"raw_w{window}"] = p_

    n_menu = len(menu_pnls)
    print(f"\n  Menu size for DSR deflation: {n_menu} variants")
    print(f"  Running CPCV harness ...")

    pkg = _SingleBookPackage(
        name="TS-MR best beta-neutral",
        selected_name=best_variant_name,
        selected_pnl=best_variant_pnl,
        menu_pnls=menu_pnls,
    )
    rep = run_harness(pkg, costs=TAKER,
                      n_groups=N_GROUPS, k=K, purge=PURGE, embargo=EMBARGO)

    dsr_val = rep.dsr.get("dsr", float("nan"))
    pbo_val = rep.pbo.pbo if rep.pbo else float("nan")
    oos_sh  = rep.pooled_oos.dist.get("sharpe", {}).get("median", float("nan"))
    oos_cal = rep.pooled_oos.dist.get("calmar", {}).get("median", float("nan"))
    frac_pos = rep.pooled_oos.frac_sharpe_pos

    print(f"\n  DSR = {dsr_val:.4f}  (deflated Sharpe; > 0.5 ~ potential edge; > 0.8 ~ strong)")
    print(f"  PBO = {pbo_val:.4f}  (probability of overfitting; < 0.5 ~ not pathological)")
    print(f"  OOS Sharpe (median CPCV segments, harness scale) = {oos_sh:.4f}")
    print(f"  OOS Calmar (median CPCV segments, harness scale) = {oos_cal:.4f}")
    print(f"  Fraction of OOS segments with Sharpe > 0: {frac_pos:.1%}")
    print()
    print_report(rep)
    print("\n  CAVEAT: harness metrics use sqrt(8760) scale (hourly engine); for DAILY honest")
    print(f"  levels use Section A/C Sharpe values above (sqrt(365) annualization).")

    section_d = {
        "best_variant": best_variant_name,
        "full_period_daily_sharpe": float(best_net_sharpe),
        "dsr": rep.dsr,
        "pbo": float(pbo_val),
        "oos_sharpe_median_harness": float(oos_sh),
        "oos_calmar_median_harness": float(oos_cal),
        "frac_oos_sharpe_pos": float(frac_pos),
    }

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION E — REGIME / DIVERSIFICATION TIE-IN
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("SECTION E — REGIME / DIVERSIFICATION TIE-IN")
    print("=" * 100)

    # E1: Correlation with momentum_ensemble
    common_idx = pnl_mom.dropna().index.intersection(best_variant_pnl.dropna().index)
    if len(common_idx) > 30:
        corr_val = float(np.corrcoef(
            pnl_mom.loc[common_idx].values,
            best_variant_pnl.loc[common_idx].values
        )[0, 1])
    else:
        corr_val = float("nan")

    print(f"\n  E1: Correlation (MR best BN vs momentum_ensemble daily pnl)")
    print(f"  corr = {corr_val:+.4f}  "
          f"({'anti-correlated = potential diversifier' if corr_val < -0.2 else 'positively correlated = not a diversifier' if corr_val > 0.2 else 'near-zero = independent'} )")

    # E2: Performance in momentum's flat/negative months
    print(f"\n  E2: MR performance in MOMENTUM'S flat/negative months")
    mom_monthly = pnl_mom.resample("ME").sum()
    mr_monthly  = best_variant_pnl.resample("ME").sum()
    common_mo   = mom_monthly.dropna().index.intersection(mr_monthly.dropna().index)
    if len(common_mo) > 0:
        mom_mo = mom_monthly.loc[common_mo]
        mr_mo  = mr_monthly.loc[common_mo]
        # Months where momentum was flat or negative
        flat_neg_mask = mom_mo <= 0
        flat_neg_count = flat_neg_mask.sum()
        if flat_neg_count > 0:
            mr_in_flat = mr_mo[flat_neg_mask]
            mr_flat_mean = float(mr_in_flat.mean())
            mr_flat_pos  = float((mr_in_flat > 0).mean())
            mr_flat_ann  = mr_flat_mean * 12.0   # monthly → annual approx
            print(f"  Momentum flat/negative months: {flat_neg_count} of {len(common_mo)}")
            print(f"  MR mean monthly pnl in those months: {100*mr_flat_mean:+.3f}%")
            print(f"  MR % positive in those months: {100*mr_flat_pos:.1f}%")
            print(f"  -> Annualized MR return in momentum flat months (approx): {100*mr_flat_ann:+.2f}%")
            if mr_flat_pos > 0.55 and mr_flat_mean > 0:
                regime_comment = "MR EARNS in momentum's dead patches — genuine regime diversifier"
            elif mr_flat_mean > 0:
                regime_comment = "MR slightly positive in momentum flat months but inconsistent"
            else:
                regime_comment = "MR does NOT earn in momentum's flat months — correlated to same factor"
        else:
            flat_neg_count = 0
            mr_flat_mean = float("nan")
            mr_flat_pos = float("nan")
            mr_flat_ann = float("nan")
            regime_comment = "No flat/negative momentum months in common period"
            print(f"  (No flat/negative momentum months found)")
        print(f"  -> REGIME VERDICT: {regime_comment}")
    else:
        flat_neg_count = 0
        mr_flat_mean = float("nan")
        mr_flat_pos = float("nan")
        mr_flat_ann = float("nan")
        regime_comment = "Insufficient monthly data"
        print(f"  (Insufficient monthly data for regime analysis)")

    # Full year-by-year breakout
    print(f"\n  Year-by-year: MR (best BN) vs momentum_ensemble")
    print(f"  {'year':<8}{'mom_sharpe':>12}{'mom_ann%':>10}{'mr_sharpe':>12}{'mr_ann%':>10}")
    yearly_comparison = {}
    all_years = sorted(set(pnl_mom.dropna().index.year).intersection(
                       set(best_variant_pnl.dropna().index.year)))
    for yr in all_years:
        msk_m = pnl_mom.dropna().index.year == yr
        msk_r = best_variant_pnl.dropna().index.year == yr
        ym = daily_metrics(pnl_mom.dropna()[msk_m])
        yr_ = daily_metrics(best_variant_pnl.dropna()[msk_r])
        if ym and yr_:
            print(f"  {yr:<8}{ym['sharpe']:>12.2f}{100*ym['ann']:>10.2f}"
                  f"{yr_['sharpe']:>12.2f}{100*yr_['ann']:>10.2f}")
            yearly_comparison[str(yr)] = {
                "mom_sharpe": float(ym['sharpe']),
                "mom_ann_pct": float(100*ym['ann']),
                "mr_sharpe": float(yr_['sharpe']),
                "mr_ann_pct": float(100*yr_['ann']),
            }
        else:
            print(f"  {yr:<8}  (too few days for one or both)")

    # E3: Bear-2022 panel — does MR catch falling knives or violent reversions?
    print(f"\n  E3: Bear-regime 2021-22 (brief, Binance perps) — falling knives or reversions?")
    bear_mr_result = {}
    try:
        bear_available = []
        for coin in bear_fetch.BEAR_BASKET:
            ok_o, ok_f = bear_fetch.ensure_bear_coin(coin)
            if ok_o:
                pr = br._bear_daily_price(coin)
                if len(pr.dropna()) >= max(MR_WINDOWS) + 10:
                    bear_available.append(coin)

        if len(bear_available) >= 5:
            bear_panel = br.build_bear_panel(bear_available)
            bear_fund_  = bear_panel["funding"]
            bear_fwd_   = bear_panel["fwd_ret"]
            bear_accr   = -(bear_fund_.shift(-1))

            # Use best window from the HL-era sweep on bear panel
            s_bear  = mrs.ts_mr_signal(bear_panel, PRIMARY_WINDOW)
            w_bear  = mrs.beta_neutral_weights(s_bear, target_gross=TARGET_GROSS)
            p_bear  = _book_pnl_from_weights(w_bear, bear_fwd_, bear_accr, 8.5, PRIMARY_REBAL)

            # Also momentum on bear
            ens_bear = sig_mod.momentum_ensemble(bear_panel, lookbacks=MOM_LOOKBACKS)
            w_mbear  = xsec.rank_to_weights(ens_bear, tercile_frac=1/3)
            p_mbear  = xsec.portfolio_returns(w_mbear, bear_fwd_, costs_bps=8.5,
                                              rebal_every=7, accrual=bear_accr)

            # Trim warmup
            warmup_end = (bear_panel["price"].notna().sum(axis=1) >= 5).idxmax()
            p_bear  = p_bear[p_bear.index >= warmup_end]
            p_mbear = p_mbear[p_mbear.index >= warmup_end]

            print(f"  Bear panel: {bear_panel['price'].index.min().date()} -> "
                  f"{bear_panel['price'].index.max().date()}  ({len(bear_available)} coins)")
            print(f"  {'book':<30}{'sharpe':>9}{'ann%':>9}{'maxDD%':>9}{'calmar':>9}")
            for label, pnl_ in [("MR_BN(bear)", p_bear), ("momentum(bear)", p_mbear)]:
                bm = daily_metrics(pnl_)
                if bm:
                    cal_s = f"{bm['calmar']:>9.2f}" if not np.isnan(bm['calmar']) else f"{'nan':>9}"
                    print(f"  {label:<30}{bm['sharpe']:>9.2f}{100*bm['ann']:>9.2f}"
                          f"{100*bm['maxdd']:>9.2f}{cal_s}")
                    bear_mr_result[label] = _safe_dict(bm)
                else:
                    print(f"  {label:<30}  (too few days)")

            # Sub-windows
            for sname, (s0, s1) in br.SUBWINDOWS.items():
                t0 = pd.Timestamp(s0, tz="UTC")
                t1 = pd.Timestamp(s1, tz="UTC")
                sub = p_bear.loc[(p_bear.index >= t0) & (p_bear.index <= t1)]
                sm  = daily_metrics(sub)
                s_mr_sh = sm.get("sharpe", float("nan")) if sm else float("nan")
                sub_m = p_mbear.loc[(p_mbear.index >= t0) & (p_mbear.index <= t1)]
                sm_m  = daily_metrics(sub_m)
                s_mom_sh = sm_m.get("sharpe", float("nan")) if sm_m else float("nan")
                print(f"    {sname:<34} MR {s_mr_sh:+.2f}  mom {s_mom_sh:+.2f}")
                bear_mr_result[sname] = {"mr_sharpe": float(s_mr_sh), "mom_sharpe": float(s_mom_sh)}
        else:
            print(f"  SKIP: only {len(bear_available)} bear coins available (need >= 5)")
    except Exception as e:
        print(f"  Bear analysis error: {type(e).__name__}: {e}")

    section_e = {
        "corr_mr_vs_momentum": float(corr_val),
        "momentum_flat_neg_months": int(flat_neg_count),
        "mr_mean_monthly_pnl_in_flat_months": float(mr_flat_mean),
        "mr_pct_positive_in_flat_months": float(mr_flat_pos),
        "mr_ann_approx_in_flat_months": float(mr_flat_ann) if not np.isnan(mr_flat_ann) else None,
        "regime_comment": regime_comment,
        "yearly_comparison": yearly_comparison,
        "bear_2022": bear_mr_result,
    }

    # ══════════════════════════════════════════════════════════════════════════
    # FINAL VERDICT
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("FINAL VERDICT")
    print("=" * 100)

    # Collect key numbers
    best_bn_net_sh   = best_net_sharpe
    best_bn_ann_pct  = best_variant_pnl.mean() * 365.0 * 100 if best_variant_pnl is not None else float("nan")
    dsr              = dsr_val
    pbo              = pbo_val

    # Use raw-MR at best window for RAW verdict
    raw_sh_at_best = float("nan")
    for window in MR_WINDOWS:
        s_raw_ = mrs.ts_mr_signal(panel, window)
        w_r_   = mrs.normalize_weights(s_raw_, target_gross=TARGET_GROSS)
        p_r_   = _book_pnl_from_weights(w_r_, fwd_ret, accrual, 8.5, PRIMARY_REBAL)
        m_r_   = daily_metrics(p_r_)
        if m_r_ and (np.isnan(raw_sh_at_best) or m_r_["sharpe"] > raw_sh_at_best):
            raw_sh_at_best = float(m_r_["sharpe"])

    # Does edge survive taker?
    # Find net Sharpe at 8.5bps, BN, window that matches best_variant_name
    print(f"\n1. RAW vs BETA-NEUTRAL at {PRIMARY_COSTS}bps, rebal=1d, window={PRIMARY_WINDOW}:")
    print(f"   RAW TS-MR:          gross Sharpe {m_raw_g.get('sharpe', float('nan')):+.3f}  "
          f"net {m_raw.get('sharpe', float('nan')):+.3f}  "
          f"ann {100*m_raw.get('ann', float('nan')):+.2f}%  "
          f"maxDD {100*m_raw.get('maxdd', float('nan')):.2f}%  "
          f"turnover/day {tv_raw:.4f}  cost_drag {drag_raw:.2f}%/yr")
    print(f"   BETA-NEUTRAL TS-MR: gross Sharpe {m_bn_g.get('sharpe', float('nan')):+.3f}  "
          f"net {m_bn.get('sharpe', float('nan')):+.3f}  "
          f"ann {100*m_bn.get('ann', float('nan')):+.2f}%  "
          f"maxDD {100*m_bn.get('maxdd', float('nan')):.2f}%  "
          f"turnover/day {tv_bn:.4f}  cost_drag {drag_bn:.2f}%/yr")

    print(f"\n2. Cost-decay summary (beta-neutral, window=10):")
    print(f"   {'costs_bps':<14}{'rebal=1':>10}{'rebal=2':>10}{'rebal=3':>10}{'rebal=5':>10}")
    for costs_bps in COST_GRID:
        row = f"   {costs_bps:<14.1f}"
        for re in REBAL_GRID:
            key = f"BETA-NEUTRAL_c{int(costs_bps*10)}_r{re}"
            sh = cost_decay.get(key, {}).get("net_sharpe", float("nan"))
            row += f"{sh:>10.3f}"
        print(row)

    print(f"\n3. Robustness map (beta-neutral z-score MR, {COST_GRID[1]}bps):")
    print(f"   windows={MR_WINDOWS}")
    robust_sharpes = [f"{robustness.get('ts_zscore_MR_w' + str(w) + '_BN', {}).get('sharpe', float('nan')):+.3f}" for w in MR_WINDOWS]
    print(f"   net Sharpes: {robust_sharpes}")
    print(f"   -> {plateau_verdict}")

    print(f"\n4. Harness: best variant={best_variant_name}")
    print(f"   DSR={dsr_val:.4f}  PBO={pbo_val:.4f}  "
          f"OOS_Sharpe_median(harness)={oos_sh:.4f}  frac_pos={frac_pos:.1%}")

    print(f"\n5. Momentum correlation + regime:")
    print(f"   corr(MR, momentum) = {corr_val:+.4f}")
    print(f"   MR in momentum flat months: {100*mr_flat_mean:+.3f}% mean  "
          f"{100*mr_flat_pos:.1f}% positive")
    print(f"   Regime verdict: {regime_comment}")

    # ── Plain-language verdict ────────────────────────────────────────────────
    print(f"\n6. PLAIN-LANGUAGE VERDICT")
    print("-" * 80)

    # Decision logic:
    # DEAD: all-negative or near-zero plateau + DSR < 0.5
    # SPARK-AT-DAILY: positive plateau + DSR moderately positive
    # NEEDS-MAKER: edge only at 2bps, gone at 8.5bps
    cost_grid_entries = [(c, r) for c in COST_GRID for r in [PRIMARY_REBAL]]
    sh_at_taker  = cost_decay.get(f"BETA-NEUTRAL_c{int(8.5*10)}_r{PRIMARY_REBAL}", {}).get("net_sharpe", float("nan"))
    sh_at_maker  = cost_decay.get(f"BETA-NEUTRAL_c{int(2.0*10)}_r{PRIMARY_REBAL}", {}).get("net_sharpe", float("nan"))
    sh_at_stress = cost_decay.get(f"BETA-NEUTRAL_c{int(15.0*10)}_r{PRIMARY_REBAL}", {}).get("net_sharpe", float("nan"))

    has_positive_plateau = (all_bn_sharpes and
                            all(s > 0.05 for s in all_bn_sharpes if not np.isnan(s)) and
                            not np.isnan(best_net_sharpe) and best_net_sharpe > 0.10)
    has_positive_at_taker = not np.isnan(sh_at_taker) and sh_at_taker > 0.10
    has_positive_at_maker = not np.isnan(sh_at_maker) and sh_at_maker > 0.10
    dsr_strong = not np.isnan(dsr_val) and dsr_val > 0.5
    dsr_weak   = not np.isnan(dsr_val) and dsr_val > 0.3

    if has_positive_plateau and has_positive_at_taker and dsr_strong:
        verdict_tag = "SPARK-AT-DAILY"
        verdict_text = (
            f"SPARK-AT-DAILY: beta-neutral TS-MR shows a positive net-Sharpe plateau at "
            f"taker costs ({sh_at_taker:+.3f} at 8.5bps, rebal=1d). Robustness map: "
            f"{plateau_verdict}. DSR={dsr_val:.3f} (> 0.5 threshold). The edge DOES survive "
            f"taker fills but is modest. Execution: needs daily rebalancing — any relaxation "
            f"(rebal=3d+) may hurt. This is worth deeper sub-daily work (intraday bars)."
        )
    elif has_positive_at_maker and not has_positive_at_taker:
        verdict_tag = "NEEDS-MAKER"
        verdict_text = (
            f"NEEDS-MAKER: beta-neutral TS-MR shows edge at maker costs ({sh_at_maker:+.3f} at "
            f"2bps) but NOT at taker costs ({sh_at_taker:+.3f} at 8.5bps). DSR={dsr_val:.3f}. "
            f"The strategy is execution-bound — it requires maker fills or sub-daily bars "
            f"where signals refresh before the cost is fully charged. Not tradeable for us "
            f"on HL with standard taker fills at daily granularity."
        )
    elif has_positive_plateau and not dsr_strong and dsr_weak:
        verdict_tag = "SPARK-AT-DAILY (WEAK)"
        verdict_text = (
            f"SPARK-AT-DAILY (WEAK): positive net-Sharpe plateau ({plateau_verdict}) but "
            f"DSR={dsr_val:.3f} (below 0.5 strong threshold). The edge is too thin at daily "
            f"scale to be confident after DSR deflation across {n_menu} tested variants. "
            f"Not a clear actionable edge on daily bars."
        )
    else:
        verdict_tag = "DEAD"
        verdict_text = (
            f"DEAD: time-series MR at daily scale shows {'no consistent positive edge' if not has_positive_plateau else 'edge only in gross'}. "
            f"Net-of-cost Sharpe: best={best_net_sharpe:+.3f}, "
            f"at taker={sh_at_taker:+.3f}, at maker={sh_at_maker:+.3f}. "
            f"DSR={dsr_val:.3f}. {plateau_verdict}. "
            f"Daily-bar MR is cost-eaten at taker rates, consistent with the cross-sectional "
            f"value finding. Strategy requires intraday bars or maker-only execution."
        )

    print(f"\n   VERDICT: [{verdict_tag}]")
    print(f"   {verdict_text}")

    caveat = (
        "CAVEAT: daily bars ALMOST CERTAINLY UNDERSTATE the intraday MR edge. "
        "A negative daily result does NOT kill the intraday version — it merely confirms "
        "that the edge does not survive daily-bar costs. A positive daily result is a strong "
        "forward signal worth deeper sub-daily work. We cannot test the intraday version "
        "with this data (we only have daily OHLCV, not tick/minute bars)."
    )
    print(f"\n   {caveat}")

    # ══════════════════════════════════════════════════════════════════════════
    # OUTPUT JSON
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 100)
    print("Saving run_mr.json ...")

    def _harness_to_dict(r):
        if r is None:
            return {}
        try:
            return to_dict(r)
        except Exception as e:
            return {"error": str(e)}

    output = {
        "description": "Time-series mean-reversion standalone crypto test — CPCV + DSR + PBO",
        "config": {
            "frozen_universe": len(coins),
            "accrual": "-funding.shift(-1)",
            "primary_window": PRIMARY_WINDOW,
            "primary_costs_bps": PRIMARY_COSTS,
            "primary_rebal_every": PRIMARY_REBAL,
            "mr_windows": list(MR_WINDOWS),
            "cost_grid": list(COST_GRID),
            "rebal_grid": list(REBAL_GRID),
            "purge_days": PURGE,
            "embargo_days": EMBARGO,
            "n_groups": N_GROUPS,
            "k": K,
            "annualization": "sqrt(365) daily (honest) for sections A-C-E; harness uses sqrt(8760)",
        },
        "momentum_baseline": _safe_dict(m_mom) if m_mom else {},
        "section_A_raw_vs_beta_neutral": section_a,
        "section_B_cost_decay": cost_decay,
        "section_C_robustness": {
            "variants": {k: v for k, v in robustness.items()},
            "plateau_verdict": plateau_verdict,
            "all_bn_sharpes_by_window": {str(MR_WINDOWS[i]): float(s) for i, s in enumerate(all_bn_sharpes)},
        },
        "section_D_harness": section_d,
        "section_E_regime": section_e,
        "final_verdict": {
            "verdict_tag": verdict_tag,
            "verdict_text": verdict_text,
            "caveat": caveat,
            "best_bn_net_sharpe_at_85bps_rebal1": float(sh_at_taker),
            "best_bn_net_sharpe_at_20bps_maker":  float(sh_at_maker),
            "best_bn_net_sharpe_at_150bps_stress": float(sh_at_stress),
            "dsr": float(dsr_val),
            "pbo": float(pbo_val),
            "plateau": plateau_verdict,
        },
    }

    out_path = _HERE / "run_mr.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str))
    print(f"JSON -> {out_path}")
    print("DONE.")


if __name__ == "__main__":
    main()
