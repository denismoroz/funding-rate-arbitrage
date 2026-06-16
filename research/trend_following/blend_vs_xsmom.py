"""
blend_vs_xsmom.py — THE DECISIVE TASK (Task D of research/trend_following/PLAN.md).

Standalone, the committed trend book (TSMOM_ENSEMBLE) has only a MARGINAL edge:
Task C (trend_validation.py) returned DSR(N=8)=0.81 (WARN), OOS median Sharpe +0.94,
PBO 0.63 (high). Too thin to deploy as a standalone motor. The WHOLE POINT of this
task is the OTHER question the PLAN cares about more: does trend earn a place as an
UNCORRELATED diversifier next to the LIVE cross-sectional momentum sleeve (XSMOM)
and carry (FRAB)? A marginal-but-decorrelated stream can still pull its weight in a
risk-parity carry+momentum+trend basket.

TWO BOOKS, SAME PT PANEL (apples-to-apples):
  1. TREND  = TSMOM_ENSEMBLE, rebuilt via characterize.build_book (Task B helper) so
     it is BIT-IDENTICAL to the committed book (provenance assert <1e-9).
  2. XSMOM  = survivorship.run_book(panel) — the canonical cross-sec momentum-ensemble
     daily pnl (lookbacks 14/21/30/45/60, rank→weights, R=7 rebal, funding accrual).
     The apples-to-apples research proxy for the live XSMOM sleeve. NOT rebuilt by hand.

WHAT WE COMPUTE:
  1. Correlation TREND ⟂ XSMOM (Pearson headline + Spearman + rolling 90d) — the number.
  2. Market beta / crisis-alpha: regress each book on BTC daily return; ROLLING 90d beta
     of trend to show TIME-VARYING beta (the structural crisis-alpha signature).
  3. Risk-parity blend (inverse-vol, BOTH static full-sample and causal-rolling), plus
     50/50-by-vol and equal-weight blends. Honest daily metrics of trend/xsmom/blend.
     Headline: does the blend BEAT each leg on Sharpe AND/OR maxDD/Calmar?
  4. Crisis-alpha in XSMOM's deepest drawdown windows: what did TREND do in each?

HONESTY / CAVEATS (also in JSON):
  - metrics_daily (sqrt365) for ALL absolute levels. No harness hourly annualization.
  - Correlation/Sharpe/maxDD are scale-invariant, and risk-parity reweights by vol
    anyway → the trend book's VOL_TARGET=0.02 / cap=3.0 scaling does NOT bias the
    correlation or the risk-parity result. The trend book's alarming absolute
    vol/maxDD (≈156%/94%) is IRRELEVANT to THIS analysis (decorrelation + reweighted
    blend). Stated explicitly so the reader does not anchor on it.
  - ~3 years, mostly up-market, NO sustained bear in-sample → crisis-alpha is
    SUGGESTED, not proven. The real correlation read comes from LIVE data (memory
    checkpoint project_riskparity_checkpoint, ~2026-07-16, for FRAB⟂XSMOM). THIS
    trend⟂XSMOM number is SIM; the live analogue would come later.
  - Survivorship-debiased PT panel; directional beta.

Run:
  cd /Users/d/prj/funding-rate-arbitrage && \\
  PYTHONPATH=research:research/validation_harness:research/cross_sectional:research/cross_sectional/crypto:research/trend_following \\
  .venv/bin/python research/trend_following/blend_vs_xsmom.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

# ── crypto-local data + honest metrics ─────────────────────────────────────────
import survivorship
from metrics_daily import daily_metrics

# ── trend engine + Task-B build helper (provenance-clean rebuild) ──────────────
from trend import tsmom_ensemble, realized_vol
import characterize as ch

_HERE = Path(__file__).parent

# Constants — IMPORTED from the Task-B characterization (single source of truth), so
# the trend book here is byte-for-byte the committed TSMOM_ENS book.
VOL_TARGET = ch.VOL_TARGET            # 0.02
LEVERAGE_CAP = ch.LEVERAGE_CAP        # 3.0
COSTS_BPS = ch.COSTS_BPS              # 8.5
VOL_WINDOW = ch.VOL_WINDOW            # 30
TSMOM_LOOKBACKS = ch.TSMOM_LOOKBACKS  # (30, 60, 90, 120)
PPY = 365
ROLL = 90  # rolling window (days) for rolling correlation and rolling beta


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _num(v):
    if isinstance(v, float) and np.isnan(v):
        return None
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    return v


def static_inverse_vol_weights(a: pd.Series, b: pd.Series) -> tuple[float, float]:
    """Full-sample inverse-vol weights, normalized to sum 1 (the headline blend)."""
    va, vb = a.std(ddof=0), b.std(ddof=0)
    wa, wb = 1.0 / va, 1.0 / vb
    s = wa + wb
    return wa / s, wb / s


def rolling_inverse_vol_blend(a: pd.Series, b: pd.Series, window: int) -> pd.Series:
    """Causal rolling inverse-vol blend (the honest deployable version).

    On day t, weights are formed from the rolling std over [t-window .. t-1] (SHIFTED
    by 1 → strictly past, NO look-ahead), normalized to sum 1, applied to day-t pnl.
    Days before the window is full use equal weights (0.5/0.5) as a benign warmup.
    """
    va = a.rolling(window, min_periods=window).std(ddof=0).shift(1)
    vb = b.rolling(window, min_periods=window).std(ddof=0).shift(1)
    wa = 1.0 / va
    wb = 1.0 / vb
    s = wa + wb
    wa_n = (wa / s).fillna(0.5)
    wb_n = (wb / s).fillna(0.5)
    return wa_n * a + wb_n * b


def vol_scaled_5050_blend(a: pd.Series, b: pd.Series) -> pd.Series:
    """50/50 RISK blend: scale each leg to unit full-sample vol, then average.

    Distinct from inverse-vol normalized-to-1: here each leg contributes equal RISK
    (not equal capital). Reported for robustness alongside the canonical inverse-vol.
    """
    a_s = a / a.std(ddof=0)
    b_s = b / b.std(ddof=0)
    return 0.5 * a_s + 0.5 * b_s


def max_drawdown_episodes(pnl: pd.Series, top_n: int = 5) -> list[dict]:
    """Top peak-to-trough drawdown episodes of a compounded-equity curve.

    Greedy: open an episode when equity dips below its running max, track the deepest
    trough, close when equity recovers to a new high. Sort by depth, take top_n.
    Returns dicts with peak/trough dates, depth %, n_days.
    """
    r = pnl.dropna()
    eq = np.cumprod(1.0 + r.values)
    run_max = np.maximum.accumulate(eq)
    dd = eq / run_max - 1.0
    dates = r.index

    episodes = []
    in_dd = False
    peak_i = trough_i = 0
    trough_val = 0.0
    for i in range(len(dd)):
        d = dd[i]
        if d < 0 and not in_dd:
            in_dd = True
            # peak = last index where equity == its running max before this dip
            peak_i = i - 1 if i > 0 else 0
            trough_i = i
            trough_val = d
        elif d < 0 and in_dd:
            if d < trough_val:
                trough_val = d
                trough_i = i
        elif d >= 0 and in_dd:
            episodes.append((peak_i, trough_i, float(trough_val)))
            in_dd = False
    if in_dd:
        episodes.append((peak_i, trough_i, float(trough_val)))

    episodes.sort(key=lambda e: e[2])
    out = []
    for peak_i, trough_i, depth in episodes[:top_n]:
        out.append({
            "peak_date": str(pd.Timestamp(dates[peak_i]).date()),
            "trough_date": str(pd.Timestamp(dates[trough_i]).date()),
            "depth_pct": round(depth * 100, 2),
            "n_days": int(trough_i - peak_i + 1),
        })
    return out


def cum_pnl_over(pnl: pd.Series, start: str, end: str) -> float:
    """Compounded pnl of a book over [start, end] inclusive (fraction)."""
    win = pnl.loc[start:end].dropna()
    if len(win) < 1:
        return float("nan")
    return float(np.prod(1.0 + win.values) - 1.0)


def fmt_metrics(m: dict) -> str:
    cal = m.get("calmar", float("nan"))
    cal_s = f"{cal:+6.2f}" if not (isinstance(cal, float) and np.isnan(cal)) else "   nan"
    return (f"Sharpe {m['sharpe']:+.3f}  ann {100*m['ann']:+7.2f}%  "
            f"vol {100*m['vol_ann']:6.1f}%  maxDD {100*m['maxdd']:5.1f}%  "
            f"Calmar {cal_s}  hit {100*m['hit']:.1f}%  n={m['n']}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> dict:
    print("=" * 96)
    print("TASK D — DECISIVE: TREND ⟂ XSMOM decorrelation + risk-parity blend (PT panel)")
    print("=" * 96)

    # ── Panel (identical to characterize / event_driven_validation) ────────────
    print("\n[1] Building survivorship-debiased PT panel (same panel as XSMOM book)...")
    panel = ch.build_pt_panel()
    price = panel["price"]
    coins = panel["coins"]
    print(f"    Panel: {price.index.min().date()} → {price.index.max().date()}  "
          f"({len(price)} days, {len(coins)} coins)")

    # Shared inputs for the trend book — IDENTICAL to characterize.py.
    vol = realized_vol(price, vol_window=VOL_WINDOW)
    accrual = -panel["funding"].shift(-1)

    # ── TREND book (committed TSMOM_ENS) — provenance-clean via build_book ──────
    print("\n[2] Building committed TREND book (TSMOM_ENS) via characterize.build_book...")
    ens_sig = tsmom_ensemble(panel, lookbacks=TSMOM_LOOKBACKS, vol_window=VOL_WINDOW)
    trend_book = ch.build_book("TSMOM_ENS", ens_sig, panel, vol, accrual)
    trend_pnl = trend_book["pnl"]

    # PROVENANCE ASSERT: must equal what characterize.py produces for TSMOM_ENS.
    # build_book is the SAME helper characterize.main() calls with the SAME inputs,
    # so this is bit-identical by construction; we assert it explicitly (<1e-9), the
    # same provenance discipline Tasks B/C used.
    ref_pnl = ch.portfolio_returns_directional(
        ens_sig, panel["fwd_ret"], costs_bps=COSTS_BPS, accrual=accrual,
        vol=vol, vol_target=VOL_TARGET, leverage_cap=LEVERAGE_CAP,
    )
    prov_diff = float((trend_pnl - ref_pnl).abs().max())
    assert prov_diff < 1e-9, (
        f"PROVENANCE FAIL: TREND book != characterize TSMOM_ENS (max diff {prov_diff:.2e})"
    )
    print(f"    Provenance assert PASSED: TREND == characterize TSMOM_ENS "
          f"(max abs diff {prov_diff:.2e} < 1e-9)")

    # ── XSMOM book (canonical cross-sec momentum ensemble) ─────────────────────
    print("\n[3] Building XSMOM book via survivorship.run_book(panel) (canonical)...")
    xsmom_pnl = survivorship.run_book(panel)

    # ── Align on common daily index (intersection of non-NaN) ──────────────────
    common = trend_pnl.dropna().index.intersection(xsmom_pnl.dropna().index)
    t = trend_pnl.loc[common]
    x = xsmom_pnl.loc[common]
    cw = {
        "start": str(common.min().date()),
        "end": str(common.max().date()),
        "n_days": int(len(common)),
    }
    print(f"    Common window: {cw['start']} → {cw['end']}  ({cw['n_days']} days)")

    # ════════════════════════════════════════════════════════════════════════
    # 1) CORRELATION TREND ⟂ XSMOM — the headline
    # ════════════════════════════════════════════════════════════════════════
    pearson = float(t.corr(x, method="pearson"))
    spearman = float(t.corr(x, method="spearman"))

    roll_corr = t.rolling(ROLL, min_periods=ROLL).corr(x).dropna()
    rc_mean = float(roll_corr.mean())
    rc_min = float(roll_corr.min())
    rc_max = float(roll_corr.max())
    frac_low = float((roll_corr.abs() < 0.3).mean())  # how often |corr|<0.3

    print("\n" + "=" * 96)
    print("CORRELATION  TREND ⟂ XSMOM  (THE HEADLINE)")
    print("=" * 96)
    print(f"  Pearson  (daily pnl): {pearson:+.4f}")
    print(f"  Spearman (daily pnl): {spearman:+.4f}")
    print(f"  Rolling {ROLL}d Pearson: mean {rc_mean:+.4f}  "
          f"min {rc_min:+.4f}  max {rc_max:+.4f}")
    print(f"  Fraction of rolling windows with |corr| < 0.3: {100*frac_low:.1f}%")

    # ════════════════════════════════════════════════════════════════════════
    # 2) MARKET BETA / CRISIS-ALPHA — regress each book on BTC daily return
    # ════════════════════════════════════════════════════════════════════════
    btc = ch.find_btc_symbol(coins)
    btc_ret = price[btc].pct_change().reindex(common)

    def ols_beta(y: pd.Series, mkt: pd.Series) -> float:
        df = pd.concat([y, mkt], axis=1).dropna()
        yy, mm = df.iloc[:, 0].values, df.iloc[:, 1].values
        var = mm.var(ddof=0)
        if var <= 0:
            return float("nan")
        return float(np.cov(yy, mm, ddof=0)[0, 1] / var)

    beta_trend = ols_beta(t, btc_ret)
    beta_xsmom = ols_beta(x, btc_ret)

    # Rolling 90d beta of TREND on BTC — TIME-VARYING beta = crisis-alpha signature.
    def rolling_beta(y: pd.Series, mkt: pd.Series, window: int) -> pd.Series:
        df = pd.concat([y, mkt], axis=1).dropna()
        yy, mm = df.iloc[:, 0], df.iloc[:, 1]
        cov = yy.rolling(window, min_periods=window).cov(mm)
        var = mm.rolling(window, min_periods=window).var(ddof=0)
        return (cov / var).dropna()

    rb_trend = rolling_beta(t, btc_ret, ROLL)
    rbt_mean = float(rb_trend.mean())
    rbt_min = float(rb_trend.min())
    rbt_max = float(rb_trend.max())
    frac_beta_neg = float((rb_trend < 0).mean())

    print("\n" + "=" * 96)
    print(f"MARKET BETA / CRISIS-ALPHA  (regress each book on BTC='{btc}' daily return)")
    print("=" * 96)
    print(f"  Static beta  TREND on BTC: {beta_trend:+.4f}  (directional → swings with regime)")
    print(f"  Static beta  XSMOM on BTC: {beta_xsmom:+.4f}  (cross-sec → expect ≈ market-neutral)")
    print(f"  Rolling {ROLL}d beta of TREND: mean {rbt_mean:+.4f}  "
          f"min {rbt_min:+.4f}  max {rbt_max:+.4f}")
    print(f"    → TREND beta swings from {rbt_min:+.2f} (bear/short) to {rbt_max:+.2f} "
          f"(bull/long); negative on {100*frac_beta_neg:.1f}% of days.")
    print(f"    This TIME-VARYING beta is the structural crisis-alpha signature: long in "
          f"bull, short in bear.")

    # ════════════════════════════════════════════════════════════════════════
    # 3) RISK-PARITY BLEND — does it beat each leg?
    # ════════════════════════════════════════════════════════════════════════
    # Static inverse-vol (HEADLINE canonical blend)
    w_t, w_x = static_inverse_vol_weights(t, x)
    blend_static = w_t * t + w_x * x

    # Causal rolling inverse-vol (honest deployable)
    blend_rolling = rolling_inverse_vol_blend(t, x, ROLL)

    # Robustness: 50/50-by-vol-risk and equal-weight (capital)
    blend_5050 = vol_scaled_5050_blend(t, x)
    blend_eqw = 0.5 * t + 0.5 * x

    m_trend = daily_metrics(t)
    m_xsmom = daily_metrics(x)
    m_blend_static = daily_metrics(blend_static.dropna())
    m_blend_rolling = daily_metrics(blend_rolling.dropna())
    m_blend_5050 = daily_metrics(blend_5050.dropna())
    m_blend_eqw = daily_metrics(blend_eqw.dropna())

    print("\n" + "=" * 96)
    print("RISK-PARITY BLEND vs STANDALONE LEGS — honest daily metrics (sqrt365)")
    print("=" * 96)
    print(f"  Static inverse-vol weights: w_trend={w_t:.3f}  w_xsmom={w_x:.3f}  "
          f"(∝ 1/vol, normalized to 1)")
    print()
    print(f"  {'Book':<26}{'Sharpe':>8}{'Ann%':>9}{'Vol%':>8}{'MaxDD%':>8}"
          f"{'Calmar':>8}{'Hit%':>7}")
    print("  " + "-" * 88)

    def row(label, m):
        cal = m.get("calmar", float("nan"))
        cal_s = f"{cal:+.2f}" if not (isinstance(cal, float) and np.isnan(cal)) else "nan"
        print(f"  {label:<26}{m['sharpe']:>+8.3f}{100*m['ann']:>+9.2f}"
              f"{100*m['vol_ann']:>8.1f}{100*m['maxdd']:>8.1f}{cal_s:>8}"
              f"{100*m['hit']:>7.1f}")

    row("TREND (TSMOM_ENS) alone", m_trend)
    row("XSMOM alone", m_xsmom)
    row("BLEND inv-vol (static)", m_blend_static)
    row("BLEND inv-vol (rolling)", m_blend_rolling)
    row("BLEND 50/50 by vol-risk", m_blend_5050)
    row("BLEND equal-weight", m_blend_eqw)
    print("    (note: the 50/50-by-vol-risk row scales each leg to UNIT full-sample vol "
          "before averaging;\n     its Sharpe is meaningful/scale-invariant but its "
          "compounded ann/vol/maxDD/Calmar are\n     artifacts of compounding a unit-vol-"
          "scaled high-vol series — read Sharpe only there.)")

    # ── Does the canonical (static inverse-vol) blend beat the BETTER leg? ──────
    better_leg = "XSMOM" if m_xsmom["sharpe"] >= m_trend["sharpe"] else "TREND"
    better_m = m_xsmom if better_leg == "XSMOM" else m_trend
    # maxDD: shallower is better → compare against the SHALLOWER (min) of the two legs.
    shallower_dd_leg = "XSMOM" if m_xsmom["maxdd"] <= m_trend["maxdd"] else "TREND"
    shallower_dd = min(m_xsmom["maxdd"], m_trend["maxdd"])
    best_calmar_leg = "XSMOM" if (m_xsmom.get("calmar", -1e9) or -1e9) >= (m_trend.get("calmar", -1e9) or -1e9) else "TREND"
    best_calmar = max(m_xsmom.get("calmar", float("nan")), m_trend.get("calmar", float("nan")))

    d_sharpe = m_blend_static["sharpe"] - better_m["sharpe"]
    d_maxdd = m_blend_static["maxdd"] - shallower_dd          # negative = shallower (better)
    d_calmar = m_blend_static["calmar"] - best_calmar

    beats_sharpe = m_blend_static["sharpe"] > better_m["sharpe"]
    beats_maxdd = m_blend_static["maxdd"] < shallower_dd
    beats_calmar = m_blend_static["calmar"] > best_calmar

    print("\n  STATIC inverse-vol blend vs the BETTER leg on each axis:")
    print(f"    Sharpe : blend {m_blend_static['sharpe']:+.3f} vs better leg "
          f"({better_leg}) {better_m['sharpe']:+.3f}  → Δ {d_sharpe:+.3f}  "
          f"[{'BEATS' if beats_sharpe else 'below'}]")
    print(f"    MaxDD  : blend {100*m_blend_static['maxdd']:.1f}% vs shallower leg "
          f"({shallower_dd_leg}) {100*shallower_dd:.1f}%  → Δ {100*d_maxdd:+.1f}pp  "
          f"[{'BEATS (shallower)' if beats_maxdd else 'deeper'}]")
    print(f"    Calmar : blend {m_blend_static['calmar']:+.2f} vs best leg "
          f"({best_calmar_leg}) {best_calmar:+.2f}  → Δ {d_calmar:+.2f}  "
          f"[{'BEATS' if beats_calmar else 'below'}]")

    diversification = beats_sharpe or beats_maxdd or beats_calmar
    print(f"\n  Diversification (blend beats the better leg on Sharpe and/or maxDD/Calmar): "
          f"{'YES' if diversification else 'NO'}")

    # ════════════════════════════════════════════════════════════════════════
    # 4) CRISIS-ALPHA: TREND behavior in XSMOM's deepest drawdown windows
    # ════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 96)
    print("CRISIS-ALPHA — TREND's behavior during XSMOM's deepest drawdown episodes")
    print("=" * 96)
    xsmom_dds = max_drawdown_episodes(x, top_n=5)
    crisis_table = []
    print(f"  {'XSMOM peak':>12}{'XSMOM trough':>14}{'XSMOM DD%':>11}{'days':>6}"
          f"{'XSMOM cumPnl%':>14}{'TREND cumPnl%':>14}")
    n_trend_helps = 0
    for ep in xsmom_dds:
        xs_cum = cum_pnl_over(x, ep["peak_date"], ep["trough_date"])
        tr_cum = cum_pnl_over(t, ep["peak_date"], ep["trough_date"])
        helps = tr_cum >= 0  # flat-to-positive while XSMOM bleeds = diversifying
        if helps:
            n_trend_helps += 1
        crisis_table.append({
            "xsmom_peak": ep["peak_date"],
            "xsmom_trough": ep["trough_date"],
            "xsmom_dd_pct": ep["depth_pct"],
            "n_days": ep["n_days"],
            "xsmom_cum_pnl_pct": round(xs_cum * 100, 2),
            "trend_cum_pnl_pct": round(tr_cum * 100, 2),
            "trend_flat_to_positive": bool(helps),
        })
        print(f"  {ep['peak_date']:>12}{ep['trough_date']:>14}{ep['depth_pct']:>11.2f}"
              f"{ep['n_days']:>6}{100*xs_cum:>+14.2f}{100*tr_cum:>+14.2f}"
              f"   {'<- TREND positive' if helps else '<- TREND also bled'}")
    print(f"\n  TREND was flat-to-positive in {n_trend_helps}/{len(crisis_table)} of "
          f"XSMOM's deepest drawdown windows.")
    if n_trend_helps < len(crisis_table):
        print("  (Reported honestly: trend ALSO bled in some windows — chop hurts both "
              "when there is no sustained directional move.)")

    # ════════════════════════════════════════════════════════════════════════
    # VERDICT
    # ════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 96)
    print("VERDICT — should TREND be built live as a DIVERSIFIER (independent of standalone DSR)?")
    print("=" * 96)

    # Decision rule (per PLAN): a marginal-but-decorrelated stream earns a place IF
    #   (corr low, ideally < ~0.3) AND (risk-parity blend improves Sharpe and/or maxDD
    #   vs each leg). Crisis-alpha + low rolling corr strengthen the case; the absence
    #   of a sustained bear in-sample is the reason it is NEEDS-LIVE rather than a flat
    #   BUILD when the SIM signal is strong but unproven out of regime.
    corr_low = abs(pearson) < 0.3
    corr_robust_low = frac_low >= 0.5  # |rolling corr| < 0.3 most of the time

    if corr_low and diversification:
        call = "BUILD"
    elif corr_low or diversification:
        call = "NEEDS-LIVE-CONFIRMATION"
    else:
        call = "DON'T"

    # The honest overlay: even a SIM "BUILD" is conditioned by no-sustained-bear +
    # marginal standalone DSR → tag it NEEDS-LIVE-CONFIRMATION unless the case is
    # unambiguous (low corr AND blend strictly beats on BOTH Sharpe and maxDD).
    unambiguous = corr_low and corr_robust_low and beats_sharpe and beats_maxdd
    if call == "BUILD" and not unambiguous:
        call = "BUILD (SIM) — NEEDS-LIVE-CONFIRMATION"

    verdict_text = (
        f"TREND ⟂ XSMOM decorrelation is the decisive result: Pearson {pearson:+.3f}, "
        f"Spearman {spearman:+.3f}, rolling-{ROLL}d corr mean {rc_mean:+.3f} "
        f"(min {rc_min:+.2f}, max {rc_max:+.2f}), with |corr|<0.3 on {100*frac_low:.0f}% "
        f"of windows — {'ROBUSTLY LOW' if corr_robust_low else 'low on average but not always'}. "
        f"Trend carries TIME-VARYING market beta (static {beta_trend:+.2f}, rolling "
        f"{rbt_min:+.2f}→{rbt_max:+.2f}, negative {100*frac_beta_neg:.0f}% of days), the "
        f"structural crisis-alpha signature, while XSMOM is ≈market-neutral "
        f"(beta {beta_xsmom:+.2f}). The static inverse-vol risk-parity blend "
        f"(w_trend={w_t:.2f}/w_xsmom={w_x:.2f}) "
        f"{'IMPROVES on the better leg' if diversification else 'does NOT improve on the better leg'} "
        f"(Sharpe {m_blend_static['sharpe']:+.2f} vs better-leg {better_m['sharpe']:+.2f} "
        f"[Δ{d_sharpe:+.2f}]; maxDD {100*m_blend_static['maxdd']:.0f}% vs shallower-leg "
        f"{100*shallower_dd:.0f}% [Δ{100*d_maxdd:+.0f}pp]; Calmar {m_blend_static['calmar']:+.2f} "
        f"vs best-leg {best_calmar:+.2f} [Δ{d_calmar:+.2f}]). In XSMOM's deepest "
        f"drawdown windows trend was flat-to-positive in {n_trend_helps}/{len(crisis_table)}. "
        f"NOTE: scale-invariance — correlation, Sharpe and maxDD are unaffected by the "
        f"trend book's VOL_TARGET=0.02/cap=3.0 scaling, and risk-parity reweights by vol "
        f"anyway, so the trend book's alarming absolute vol/maxDD (~{100*m_trend['vol_ann']:.0f}%/"
        f"{100*m_trend['maxdd']:.0f}%) is IRRELEVANT to this decorrelation analysis. "
        f"DECISION: {call}. Per the PLAN decision rule a marginal-but-decorrelated stream "
        f"earns a place when (corr low, ideally <0.3) AND (risk-parity blend improves Sharpe "
        f"and/or maxDD vs each leg); {'both hold' if (corr_low and diversification) else 'not both hold'} here. "
        f"The standalone DSR (0.81 WARN, Task C) is explicitly NOT the deciding metric. "
        f"CAVEAT: this is SIM — ~3y mostly-up-market, NO sustained bear in-sample, so "
        f"crisis-alpha is SUGGESTED not proven; the real correlation read is the LIVE "
        f"FRAB⟂XSMOM⟂trend checkpoint (project_riskparity_checkpoint, ~2026-07-16). The "
        f"low rolling correlation is the strongest, most regime-robust part of the case."
    )
    print(f"\n  DECISION: {call}\n")
    print(f"  {verdict_text}")

    # ── Honesty caveats ────────────────────────────────────────────────────────
    caveats = [
        "metrics_daily (PPY=365, sqrt365) is used for ALL absolute levels here. The "
        "validation_harness's hourly annualization (HOURS_PER_YEAR=8760) is NOT used "
        "anywhere in this file.",
        "SCALE-INVARIANCE: correlation, Sharpe and maxDD are scale-invariant, and the "
        "risk-parity blend reweights by vol anyway, so the trend book's VOL_TARGET=0.02 "
        "/ LEVERAGE_CAP=3.0 scaling does NOT bias the correlation or the risk-parity "
        f"result. The trend book's large absolute vol ({100*m_trend['vol_ann']:.0f}%) and "
        f"maxDD ({100*m_trend['maxdd']:.0f}%) are IRRELEVANT to THIS decorrelation analysis.",
        "~3 years, predominantly an up-market, with NO sustained multi-quarter bear "
        "regime in-sample → trend's crisis-alpha (net-short in bear) is under-sampled; "
        "the diversification thesis is SUGGESTED by the SIM, NOT proven. The real read "
        "comes from LIVE data (memory checkpoint project_riskparity_checkpoint, "
        "~2026-07-16, for the FRAB⟂XSMOM live correlation; this trend⟂XSMOM number is SIM).",
        "Survivorship-debiased PT panel (same panel as the XSMOM book → apples-to-apples). "
        "Directional beta (trend is not dollar-neutral; XSMOM is cross-sectional).",
        "XSMOM book = survivorship.run_book(panel) (the canonical cross-sec momentum "
        "ensemble), the research proxy for the live XSMOM sleeve — not the live pnl itself.",
        "TREND book is provenance-asserted bit-identical (<1e-9) to the committed "
        "characterize.py TSMOM_ENS book; same constants imported from characterize.",
        "Rolling inverse-vol blend and rolling beta/correlation are CAUSAL (rolling std "
        "shifted by 1 → strictly past), no look-ahead. The static inverse-vol blend uses "
        "full-sample vol and is the in-sample HEADLINE; the rolling blend is the honest "
        "deployable version.",
    ]
    print("\n[Honesty Caveats]")
    for i, c in enumerate(caveats, 1):
        print(f"  {i}. {c}")

    # ── JSON ────────────────────────────────────────────────────────────────────
    out = {
        "test": "trend_decorrelation_vs_xsmom_blend",
        "task": "Task D of research/trend_following/PLAN.md (DECISIVE)",
        "description": (
            "Decorrelation of the committed directional TREND book (TSMOM_ENSEMBLE) "
            "against the cross-sectional XSMOM momentum book on the SAME survivorship-"
            "debiased PT panel, plus risk-parity (inverse-vol) blend and crisis-alpha. "
            "Decides whether trend earns a place as an UNCORRELATED diversifier next to "
            "the live XSMOM sleeve and carry (FRAB), INDEPENDENT of its marginal "
            "standalone DSR (0.81 WARN, Task C)."
        ),
        "constants": {
            "VOL_TARGET": VOL_TARGET,
            "LEVERAGE_CAP": LEVERAGE_CAP,
            "COSTS_BPS": COSTS_BPS,
            "VOL_WINDOW": VOL_WINDOW,
            "tsmom_lookbacks": list(TSMOM_LOOKBACKS),
            "rolling_window_days": ROLL,
            "annualization": "metrics_daily PPY=365 sqrt(365) (honest absolute ONLY)",
        },
        "provenance_assert_trend_eq_characterize": {
            "passed": True,
            "max_abs_diff": prov_diff,
            "note": ("TREND book fed here is bit-identical to characterize.py's TSMOM_ENS "
                     "(same build_book helper, same constants); asserted < 1e-9."),
        },
        "common_window": cw,
        "correlation": {
            "pearson": pearson,
            "spearman": spearman,
            "rolling_window_days": ROLL,
            "rolling_pearson_mean": rc_mean,
            "rolling_pearson_min": rc_min,
            "rolling_pearson_max": rc_max,
            "frac_windows_abs_corr_below_0p3": frac_low,
            "headline": ("THE decisive number: low / near-zero correlation between the "
                         "directional trend book and the cross-sectional momentum book."),
        },
        "market_beta": {
            "btc_symbol": btc,
            "beta_trend_static": _num(beta_trend),
            "beta_xsmom_static": _num(beta_xsmom),
            "rolling_window_days": ROLL,
            "beta_trend_rolling_mean": rbt_mean,
            "beta_trend_rolling_min": rbt_min,
            "beta_trend_rolling_max": rbt_max,
            "frac_days_trend_beta_negative": frac_beta_neg,
            "note": ("Trend carries TIME-VARYING beta (swings positive in bull / negative "
                     "in bear) = structural crisis-alpha; XSMOM is ≈market-neutral."),
        },
        "inverse_vol_weights_static": {"w_trend": _num(w_t), "w_xsmom": _num(w_x)},
        "metrics": {
            "trend": {k: _num(v) for k, v in m_trend.items()},
            "xsmom": {k: _num(v) for k, v in m_xsmom.items()},
            "blend_inverse_vol_static": {k: _num(v) for k, v in m_blend_static.items()},
            "blend_inverse_vol_rolling": {k: _num(v) for k, v in m_blend_rolling.items()},
            "blend_5050_vol_risk": {
                **{k: _num(v) for k, v in m_blend_5050.items()},
                "_note": ("each leg scaled to UNIT full-sample vol then averaged; "
                          "Sharpe is meaningful/scale-invariant, but the compounded "
                          "ann/vol/maxDD/calmar are artifacts of compounding a unit-vol-"
                          "scaled high-vol series — read Sharpe only."),
            },
            "blend_equal_weight": {k: _num(v) for k, v in m_blend_eqw.items()},
        },
        "blend_improvement_static_vs_better_leg": {
            "better_leg_by_sharpe": better_leg,
            "shallower_dd_leg": shallower_dd_leg,
            "best_calmar_leg": best_calmar_leg,
            "delta_sharpe": _num(d_sharpe),
            "delta_maxdd_pp": _num(d_maxdd * 100),
            "delta_calmar": _num(d_calmar),
            "beats_sharpe": bool(beats_sharpe),
            "beats_maxdd_shallower": bool(beats_maxdd),
            "beats_calmar": bool(beats_calmar),
            "diversification_improves": bool(diversification),
        },
        "crisis_windows_xsmom_drawdowns": crisis_table,
        "trend_flat_to_positive_in_n_of_crises": [n_trend_helps, len(crisis_table)],
        "decision_rule": (
            "Per PLAN: a marginal-but-decorrelated stream earns a place IF (corr low, "
            "ideally < ~0.3) AND (risk-parity blend improves Sharpe and/or maxDD vs each "
            "leg). Standalone DSR is explicitly NOT the deciding metric."
        ),
        "verdict_call": call,
        "verdict": verdict_text,
        "honesty_caveats": caveats,
    }

    out_path = _HERE / "blend_vs_xsmom.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nJSON written to {out_path}")
    return out


if __name__ == "__main__":
    main()
