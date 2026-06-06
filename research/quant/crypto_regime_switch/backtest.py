"""
Regime-Switch: Trend vs Carry — dynamic allocation by Kaufman ER and related indicators.

HYPOTHESIS: instead of a static 50/50 trend+carry blend, switch capital between sleeves
using a regime filter — trend when market is trending, carry when choppy.

Regime indicators (all trailing only, no look-ahead):
  (i)   Kaufman Efficiency Ratio (ER) over W days on BTC daily close
  (ii)  Realized-vol percentile over trailing W days (secondary)
  (iii) Trend sleeve's own trailing-30d cumulative return (strategy momentum)

Switch variants tested:
  - HARD_ER:  ER >= trailing_median -> 100% trend, else 100% carry
  - SOFT_ER:  ER >= trailing_median -> 75% trend/25% carry, else 25% trend/75% carry
  - HARD_MOM: trend trailing-30d cum_ret > 0 -> 100% trend, else 100% carry
  - SOFT_MOM: trend trailing-30d cum_ret > 0 -> 75% trend/25% carry, else 25% trend/75% carry

Switching cost: 5bps on |Δw_trend| each rebalance day (over-frequent switching penalized).

Baselines:
  - static_50_50:  50% trend + 50% carry (no timing)
  - trend_only:    100% trend
  - carry_only:    100% carry

All signals decided at t, applied at t+1 (NO look-ahead).
"""
import sys
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import qutil as q

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
COINS = ["BTC", "ETH", "SOL"]
COST_BPS = 5.0
MA_PARAMS = [(10, 50), (20, 100), (50, 200)]
MOM_PARAMS = [30, 60, 90]
CARRY_CSV = HERE.parent / "crypto_funding_carry" / "results_funding_plus_staking.csv"
TF = "1d"
ER_WINDOW = 30          # Kaufman ER window (days)
MOM_WINDOW = 30         # strategy momentum window (days)
SWITCH_COST_BPS = 5.0   # per-side cost on |Δw_trend| when switching


# ---------------------------------------------------------------------------
# 1. Trend ensemble — exact replica from crypto_trend_carry_blend/backtest.py
# ---------------------------------------------------------------------------
def build_trend() -> pd.Series:
    """6-param ensemble: MA(10,50),(20,100),(50,200) + TSMOM(30,60,90).
    Per-coin weight = signal/3. Simple equal-weight across 6 params.
    """
    px = q.load_closes(COINS, TF).dropna()

    def basket_weight_ma(px, f, s):
        sig = (px.rolling(f).mean() > px.rolling(s).mean()).astype(float)
        return sig / px.shape[1]

    def basket_weight_mom(px, k):
        sig = (px / px.shift(k) - 1.0 > 0).astype(float)
        return sig / px.shape[1]

    rets = {}
    for f, s in MA_PARAMS:
        name = f"ma{f}_{s}"
        rets[name] = q.backtest_weights(px, basket_weight_ma(px, f, s), cost_bps=COST_BPS)["ret_net"]
    for k in MOM_PARAMS:
        name = f"mom{k}"
        rets[name] = q.backtest_weights(px, basket_weight_mom(px, k), cost_bps=COST_BPS)["ret_net"]

    R = pd.DataFrame(rets).dropna()
    trend_ret = R.mean(axis=1)
    trend_ret.name = "trend"
    return trend_ret


# ---------------------------------------------------------------------------
# 2. Carry (hourly -> daily via compounding)
# ---------------------------------------------------------------------------
def build_carry() -> pd.Series:
    df = pd.read_csv(CARRY_CSV)
    df["time"] = pd.to_datetime(df["time"], format="mixed", utc=True)
    df = df.set_index("time").sort_index()
    carry_daily = (1 + df["ret_total"]).resample("1D").prod() - 1
    carry_daily.name = "carry"
    return carry_daily


# ---------------------------------------------------------------------------
# 3. Regime indicators on BTC daily close — all TRAILING, no look-ahead
# ---------------------------------------------------------------------------
def build_btc_close() -> pd.Series:
    btc = q.load_ohlcv("BTC", "1d")["close"]
    btc.name = "btc_close"
    return btc


def kaufman_er(close: pd.Series, W: int) -> pd.Series:
    """Efficiency Ratio = |close[t] - close[t-W]| / sum(|Δclose|) over W bars.
    High ER -> trending. Low ER -> choppy.
    All data is prior-to-t (trailing), so ER at index t uses close[t-W..t].
    """
    direction = close.diff(W).abs()
    volatility = close.diff().abs().rolling(W).sum()
    er = direction / volatility
    return er.rename("er")


def er_regime(er: pd.Series) -> pd.Series:
    """1 = trending (ER >= trailing median), 0 = choppy."""
    trailing_median = er.expanding(min_periods=2).median()
    regime = (er >= trailing_median).astype(float)
    return regime.rename("er_regime")


def strat_mom_regime(trend_ret: pd.Series, W: int = MOM_WINDOW) -> pd.Series:
    """1 = positive trend momentum (trailing W-day cum_ret > 0), 0 = negative."""
    cum_ret = (1 + trend_ret).rolling(W).apply(lambda x: np.prod(x) - 1, raw=True)
    regime = (cum_ret > 0).astype(float)
    return regime.rename("mom_regime")


# ---------------------------------------------------------------------------
# 4. Portfolio construction with regime-based allocation + switching cost
# ---------------------------------------------------------------------------
def build_switch_portfolio(
    trend: pd.Series,
    carry: pd.Series,
    w_trend_signal: pd.Series,
    label: str,
) -> pd.Series:
    """
    w_trend_signal: weight in trend decided at t (0.0, 0.25, 0.75, or 1.0).
    Applied at t+1 (shift by 1). Switching cost charged on |Δw|.
    Returns net daily return series.
    """
    # Shift signal forward 1 day — no look-ahead
    w_trend = w_trend_signal.shift(1).fillna(0.5)   # start at 50/50 before signal
    w_carry = 1.0 - w_trend

    # Gross portfolio return
    ret_gross = w_trend * trend + w_carry * carry

    # Switching cost on |Δw_trend|
    dw = w_trend.diff().abs().fillna(0.0)
    switch_cost = dw * (SWITCH_COST_BPS / 1e4)

    ret_net = ret_gross - switch_cost
    ret_net.name = label
    return ret_net


# ---------------------------------------------------------------------------
# 5. Diagnostic stats for a regime signal
# ---------------------------------------------------------------------------
def regime_stats(w_trend_signal: pd.Series) -> dict:
    """Fraction of time in trend regime and number of switches (regime changes)."""
    # w_trend_signal at t (before shift); 1.0 or 0.75 = "trend regime"
    in_trend = (w_trend_signal >= 0.75).astype(int)
    n_switches = int((in_trend.diff().abs() > 0).sum())
    frac_trend = float(in_trend.mean())
    return {"frac_in_trend": frac_trend, "n_switches": n_switches}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run():
    print("Building trend ensemble...")
    trend = build_trend()
    print("Building carry series...")
    carry = build_carry()

    # Align on common window
    combined = pd.DataFrame({"trend": trend, "carry": carry}).dropna()
    n_days = len(combined)
    start = combined.index[0].date()
    end = combined.index[-1].date()
    corr = float(combined.corr().loc["trend", "carry"])

    print(f"\nCommon window: {start} to {end}  ({n_days} days)")
    print(f"Daily correlation trend vs carry: {corr:.4f}")

    # Load BTC close for regime indicators — aligned to combined window
    btc_close = build_btc_close().reindex(combined.index)

    # Compute regime indicators at t (all trailing)
    er = kaufman_er(btc_close, ER_WINDOW)
    er_reg = er_regime(er)
    mom_reg = strat_mom_regime(combined["trend"], MOM_WINDOW)

    # --- Build all variants ---
    variants = {}

    # BASELINES (no regime timing, no switching cost)
    for label, w in [("trend_only", 1.0), ("carry_only", 0.0), ("static_50_50", 0.5)]:
        ret = w * combined["trend"] + (1 - w) * combined["carry"]
        ret.name = label
        variants[label] = ret

    # REGIME SWITCH variants
    # Hard ER switch: 100% trend if trending, 100% carry if choppy
    w_hard_er = er_reg.reindex(combined.index).fillna(0.5)
    variants["hard_er"] = build_switch_portfolio(
        combined["trend"], combined["carry"], w_hard_er, "hard_er"
    )

    # Soft ER switch: 75/25 tilt
    w_soft_er = er_reg.reindex(combined.index).fillna(0.5).map({1.0: 0.75, 0.0: 0.25})
    variants["soft_er"] = build_switch_portfolio(
        combined["trend"], combined["carry"], w_soft_er, "soft_er"
    )

    # Hard momentum switch: 100/0 based on strategy momentum
    w_hard_mom = mom_reg.reindex(combined.index).fillna(0.5)
    variants["hard_mom"] = build_switch_portfolio(
        combined["trend"], combined["carry"], w_hard_mom, "hard_mom"
    )

    # Soft momentum switch: 75/25 tilt
    w_soft_mom = mom_reg.reindex(combined.index).fillna(0.5).map({1.0: 0.75, 0.0: 0.25})
    variants["soft_mom"] = build_switch_portfolio(
        combined["trend"], combined["carry"], w_soft_mom, "soft_mom"
    )

    # --- Compute diagnostics ---
    regime_diagnostics = {
        "hard_er": regime_stats(w_hard_er),
        "soft_er": regime_stats(w_soft_er),
        "hard_mom": regime_stats(w_hard_mom),
        "soft_mom": regime_stats(w_soft_mom),
    }

    # --- Compute metrics ---
    all_metrics = {}
    for label, ret in variants.items():
        ret_aligned = ret.reindex(combined.index).dropna()
        m = q.metrics_from_returns(ret_aligned, TF)
        all_metrics[label] = m

    # --- Summary table ---
    BASELINE_ORDER = ["trend_only", "carry_only", "static_50_50"]
    SWITCH_ORDER = ["hard_er", "soft_er", "hard_mom", "soft_mom"]

    print("\n=== Baselines ===")
    print(f"{'Variant':<18} {'CAGR':>8} {'Vol':>7} {'Sharpe':>8} {'MDD':>8} {'Calmar':>8}")
    for label in BASELINE_ORDER:
        m = all_metrics[label]
        print(f"{label:<18} {m['cagr']*100:>7.1f}% {m['vol']*100:>6.1f}% {m['sharpe']:>8.2f} "
              f"{m['max_drawdown']*100:>7.1f}% {m['calmar']:>8.2f}")

    print("\n=== Regime-Switch Variants (net of 5bps switching cost) ===")
    print(f"{'Variant':<18} {'CAGR':>8} {'Vol':>7} {'Sharpe':>8} {'MDD':>8} {'Calmar':>8} "
          f"{'%Trend':>8} {'#Switches':>10}")
    for label in SWITCH_ORDER:
        m = all_metrics[label]
        d = regime_diagnostics[label]
        print(f"{label:<18} {m['cagr']*100:>7.1f}% {m['vol']*100:>6.1f}% {m['sharpe']:>8.2f} "
              f"{m['max_drawdown']*100:>7.1f}% {m['calmar']:>8.2f} "
              f"{d['frac_in_trend']*100:>7.1f}% {d['n_switches']:>10d}")

    # --- Yearly breakdown ---
    yearly = {}
    for label in BASELINE_ORDER + SWITCH_ORDER:
        ret = variants[label].reindex(combined.index).dropna()
        yearly[label] = q.period_breakdown(ret).to_dict("records")

    print("\n=== Yearly Breakdown (static_50_50 baseline) ===")
    print(pd.DataFrame(yearly["static_50_50"]).to_string(index=False))

    print("\n=== Yearly Breakdown (best switch variant) ===")
    # Pick best switch by Calmar
    switch_calmar = {l: all_metrics[l]["calmar"] for l in SWITCH_ORDER}
    best_switch = max(switch_calmar, key=switch_calmar.get)
    print(f"Best switch variant: {best_switch}")
    print(pd.DataFrame(yearly[best_switch]).to_string(index=False))

    # --- Verdict ---
    static_m = all_metrics["static_50_50"]
    beats_calmar = {l: all_metrics[l]["calmar"] > static_m["calmar"] for l in SWITCH_ORDER}
    beats_sharpe = {l: all_metrics[l]["sharpe"] > static_m["sharpe"] for l in SWITCH_ORDER}
    beats_both = {l: beats_calmar[l] and beats_sharpe[l] for l in SWITCH_ORDER}
    any_beats = any(beats_both.values())

    print(f"\n=== Verdict ===")
    print(f"Static 50/50 baseline: CAGR={static_m['cagr']*100:.1f}% Sharpe={static_m['sharpe']:.2f} "
          f"MDD={static_m['max_drawdown']*100:.1f}% Calmar={static_m['calmar']:.2f}")
    print(f"Any switch beats static 50/50 on BOTH Calmar AND Sharpe: {any_beats}")
    for l in SWITCH_ORDER:
        m = all_metrics[l]
        print(f"  {l}: Calmar={m['calmar']:.2f} {'>' if beats_calmar[l] else '<='} {static_m['calmar']:.2f} | "
              f"Sharpe={m['sharpe']:.2f} {'>' if beats_sharpe[l] else '<='} {static_m['sharpe']:.2f} | "
              f"beats both: {beats_both[l]}")

    # --- Save results.csv ---
    results_df = pd.DataFrame({l: variants[l] for l in BASELINE_ORDER + SWITCH_ORDER})
    results_df.to_csv(HERE / "results.csv")

    # --- Save metrics.json ---
    metrics_out = {
        "window": {"start": str(start), "end": str(end), "n_days": n_days},
        "daily_correlation_trend_vs_carry": corr,
        "er_window": ER_WINDOW,
        "mom_window": MOM_WINDOW,
        "switch_cost_bps": SWITCH_COST_BPS,
        "variants": {},
        "regime_diagnostics": regime_diagnostics,
        "yearly_breakdown": yearly,
        "verdict": {
            "static_50_50": {k: static_m[k] for k in ["cagr","sharpe","max_drawdown","calmar"]},
            "any_switch_beats_both_metrics": any_beats,
            "beats_calmar": beats_calmar,
            "beats_sharpe": beats_sharpe,
            "beats_both": beats_both,
            "best_switch_by_calmar": best_switch,
        },
    }
    for label in BASELINE_ORDER + SWITCH_ORDER:
        m = all_metrics[label]
        entry = {k: m[k] for k in ["cagr","vol","sharpe","sortino","max_drawdown","calmar","exposure","n_bars","years"]}
        if label in regime_diagnostics:
            entry.update(regime_diagnostics[label])
        metrics_out["variants"][label] = entry

    q.save_metrics(HERE / "metrics.json", metrics_out)
    print(f"\nSaved metrics.json")

    # --- Save equity.png ---
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(2, 1, figsize=(13, 8), gridspec_kw={"height_ratios": [3, 1]})
        ax, ax_dd = axes

        plot_variants = {
            "trend_only":  ("Trend-only",    "steelblue",  1.2, "--"),
            "carry_only":  ("Carry-only",    "darkorange", 1.2, "--"),
            "static_50_50":("Static 50/50",  "black",      2.0, "-"),
            "hard_er":     ("Hard ER switch","crimson",    1.5, "-"),
            "soft_er":     ("Soft ER switch","tomato",     1.2, ":"),
            "hard_mom":    ("Hard Mom switch","green",     1.5, "-"),
            "soft_mom":    ("Soft Mom switch","limegreen", 1.2, ":"),
        }

        for label, (name, color, lw, ls) in plot_variants.items():
            ret = variants[label].reindex(combined.index).dropna()
            eq = (1 + ret).cumprod()
            ax.plot(eq.index, eq.values, label=name, color=color, lw=lw, ls=ls)
            dd = eq / eq.cummax() - 1
            ax_dd.plot(dd.index, dd.values, color=color, lw=0.8, alpha=0.6, ls=ls)
            if label in ("static_50_50", "hard_er", "hard_mom"):
                ax_dd.fill_between(dd.index, dd.values, 0, alpha=0.12, color=color)

        ax.set_yscale("log")
        ax.legend(loc="upper left", fontsize=8)
        ax.set_title("Regime-Switch: Trend vs Carry — Equity Curves (log scale)")
        ax.set_ylabel("Portfolio value (rebased to 1.0)")
        ax.grid(alpha=0.3)

        ax_dd.set_title("Drawdown")
        ax_dd.set_ylabel("Drawdown")
        ax_dd.grid(alpha=0.3)

        fig.tight_layout()
        fig.savefig(HERE / "equity.png", dpi=100)
        plt.close(fig)
        print("Saved equity.png")
    except Exception as e:
        print(f"equity.png skipped: {e}")

    print(f"\nOutputs in: {HERE}")
    return metrics_out, combined, variants, all_metrics, regime_diagnostics, yearly


if __name__ == "__main__":
    run()
