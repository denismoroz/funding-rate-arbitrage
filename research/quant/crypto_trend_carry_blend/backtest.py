"""
Trend + Carry portfolio blend — regime-orthogonality test.

Combines two EXISTING daily net-return series:
  1. Trend ensemble: MA crossovers (10/50, 20/100, 50/200) + TSMOM (30, 60, 90),
     BTC/ETH/SOL basket, 5bps cost, long/flat, equal-weight across 6 params.
  2. Carry: delta-neutral funding-rate + staking carry from
     crypto_funding_carry/results_funding_plus_staking.csv (hourly, compounded daily).

Tests whether regime-orthogonality (trend earns in trending regimes, carry earns in
choppy regimes) produces a better Calmar ratio in blend form vs either standalone.

No look-ahead: trend signals use qutil's internal shift; carry is a realized net-return
series. Inverse-vol weights use 30d trailing vol shifted 1 day (past data only).

Outputs: metrics.json, results.csv, equity.png, final_assessment.md
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
FIXED_WEIGHTS = [1.0, 0.75, 0.5, 0.25, 0.0]
INV_VOL_WINDOW = 30   # trailing days for vol estimation
TF = "1d"


# ---------------------------------------------------------------------------
# 1. Trend ensemble
# ---------------------------------------------------------------------------
def build_trend() -> pd.Series:
    """6-param ensemble: simple average of each param's daily net return."""
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

    # Simple equal-weight ensemble of all 6 params
    R = pd.DataFrame(rets).dropna()
    trend_ret = R.mean(axis=1)
    trend_ret.name = "trend"
    return trend_ret


# ---------------------------------------------------------------------------
# 2. Carry (hourly -> daily via compounding)
# ---------------------------------------------------------------------------
def build_carry() -> pd.Series:
    """Compound hourly ret_total to daily."""
    df = pd.read_csv(CARRY_CSV)
    df["time"] = pd.to_datetime(df["time"], format="mixed", utc=True)
    df = df.set_index("time").sort_index()
    carry_daily = (1 + df["ret_total"]).resample("1D").prod() - 1
    carry_daily.name = "carry"
    return carry_daily


# ---------------------------------------------------------------------------
# 3. Align on common window
# ---------------------------------------------------------------------------
def align(trend: pd.Series, carry: pd.Series) -> pd.DataFrame:
    return pd.DataFrame({"trend": trend, "carry": carry}).dropna()


# ---------------------------------------------------------------------------
# 4. Inverse-vol (risk parity) blend
#    Weights ∝ 1/vol(30d trailing), rebalanced monthly, shifted 1d (no LA)
# ---------------------------------------------------------------------------
def inv_vol_blend(combined: pd.DataFrame) -> pd.Series:
    vol_t = combined["trend"].rolling(INV_VOL_WINDOW).std()
    vol_c = combined["carry"].rolling(INV_VOL_WINDOW).std()
    w_t = (1.0 / vol_t) / (1.0 / vol_t + 1.0 / vol_c)
    # Monthly snap: use the weight computed at the end of the prior month
    w_t_monthly = w_t.resample("MS").last().shift(1).reindex(combined.index, method="ffill")
    # Shift 1 day so we use yesterday's computed weight
    w_t_shifted = w_t_monthly.shift(1).fillna(w_t_monthly.dropna().iloc[0])
    port = (w_t_shifted * combined["trend"] + (1 - w_t_shifted) * combined["carry"])
    port.name = "inv_vol"
    avg_w_trend = float(w_t_shifted.dropna().mean())
    return port.dropna(), avg_w_trend


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run():
    print("Building trend ensemble...")
    trend = build_trend()
    print("Building carry series...")
    carry = build_carry()

    combined = align(trend, carry)
    n_days = len(combined)
    start = combined.index[0].date()
    end = combined.index[-1].date()
    corr = float(combined.corr().loc["trend", "carry"])

    print(f"\nCommon window: {start} to {end}  ({n_days} days)")
    print(f"Daily correlation trend vs carry: {corr:.4f}")

    # --- build all variants ---
    results = pd.DataFrame({"trend": combined["trend"], "carry": combined["carry"]})

    variant_metrics = {}
    frontier = []

    # Fixed splits
    for w in FIXED_WEIGHTS:
        label = f"w{int(w*100):03d}"
        port = w * combined["trend"] + (1 - w) * combined["carry"]
        port.name = label
        results[label] = port
        m = q.metrics_from_returns(port, TF)
        variant_metrics[label] = {
            "w_trend": w, "w_carry": round(1 - w, 2),
            **{k: m[k] for k in ["cagr", "vol", "sharpe", "sortino", "max_drawdown", "calmar", "exposure"]},
        }
        frontier.append({"w_trend": w, "cagr": m["cagr"], "max_dd": m["max_drawdown"], "calmar": m["calmar"]})

    # Inverse-vol
    port_iv, avg_w_trend = inv_vol_blend(combined)
    results["inv_vol"] = port_iv
    m_iv = q.metrics_from_returns(port_iv, TF)
    variant_metrics["inv_vol"] = {
        "w_trend": f"dynamic(avg={avg_w_trend:.3f})", "w_carry": f"dynamic(avg={1-avg_w_trend:.3f})",
        **{k: m_iv[k] for k in ["cagr", "vol", "sharpe", "sortino", "max_drawdown", "calmar", "exposure"]},
        "caveat": (
            "Risk parity is degenerate here: carry daily vol (~0.04%) is ~100x smaller than "
            "trend daily vol (~2.3%), so naive inverse-vol collapses to ~98-99% carry. "
            "The fixed-split variants are more informative for strategy selection."
        ),
    }

    # Equity curves (rebased to 1.0)
    eq_cols = {}
    for col in results.columns:
        eq_cols[col] = (1 + results[col]).cumprod()
    equity = pd.DataFrame(eq_cols)

    # Identify best Calmar among fixed splits
    fixed_calmar = {lbl: variant_metrics[lbl]["calmar"] for lbl in [f"w{int(w*100):03d}" for w in FIXED_WEIGHTS]}
    best_label = max(fixed_calmar, key=fixed_calmar.get)
    best_w = variant_metrics[best_label]["w_trend"]
    best_m = variant_metrics[best_label]

    # Yearly breakdown for all variants
    yearly = {}
    for col in [f"w{int(w*100):03d}" for w in FIXED_WEIGHTS] + ["inv_vol"]:
        yearly[col] = q.period_breakdown(results[col]).to_dict("records")

    # --- print summary ---
    print("\n=== Portfolio Variants ===")
    print(f"{'w_trend':>8} {'CAGR':>8} {'Vol':>7} {'Sharpe':>8} {'MDD':>8} {'Calmar':>8}")
    for w in FIXED_WEIGHTS:
        lbl = f"w{int(w*100):03d}"
        m = variant_metrics[lbl]
        flag = " <-- best Calmar" if lbl == best_label else ""
        print(f"{w:>8.2f} {m['cagr']*100:>7.1f}% {m['vol']*100:>6.1f}% {m['sharpe']:>8.2f} {m['max_drawdown']*100:>7.1f}% {m['calmar']:>8.2f}{flag}")
    print(f"\nInv-vol (avg w_trend={avg_w_trend:.3f}): CAGR={m_iv['cagr']*100:.1f}% Sharpe={m_iv['sharpe']:.2f} Calmar={m_iv['calmar']:.2f}")
    print(f"  CAVEAT: risk-parity collapses to ~carry-only; fixed splits are more informative.")

    print(f"\nBest Calmar: {best_label} (w_trend={best_w})")
    print(f"\nYearly breakdown of best blend ({best_label}, w_trend={best_w}):")
    print(pd.DataFrame(yearly[best_label]).to_string(index=False))

    # --- save outputs ---
    HERE.mkdir(parents=True, exist_ok=True)

    # results.csv: daily returns + equity curves
    out_df = results.copy()
    for col in results.columns:
        out_df[f"eq_{col}"] = equity[col]
    out_df.to_csv(HERE / "results.csv")

    # metrics.json
    metrics_out = {
        "window": {"start": str(start), "end": str(end), "n_days": n_days},
        "daily_correlation_trend_vs_carry": corr,
        "variants": variant_metrics,
        "frontier_fixed_splits": frontier,
        "best_calmar_variant": best_label,
        "yearly_breakdown": yearly,
    }
    q.save_metrics(HERE / "metrics.json", metrics_out)

    # equity.png: trend-only, carry-only, best blend
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(2, 1, figsize=(13, 8), gridspec_kw={"height_ratios": [3, 1]})
        ax, ax_dd = axes

        styles = {
            "w100": ("Trend-only", "steelblue", 1.5, "-"),
            "w000": ("Carry-only", "darkorange", 1.5, "-"),
            best_label: (f"Best blend (w_trend={best_w})", "green", 2.0, "-"),
        }
        if best_label not in styles:
            styles[best_label] = (f"Best blend ({best_label})", "green", 2.0, "--")

        dd_series = {}
        for lbl, (name, color, lw, ls) in styles.items():
            eq_s = equity[lbl]
            ax.plot(eq_s.index, eq_s.values, label=name, color=color, lw=lw, ls=ls)
            dd = eq_s / eq_s.cummax() - 1
            dd_series[lbl] = (dd, color, name)

        ax.set_yscale("log")
        ax.legend(loc="upper left")
        ax.set_title("Trend + Carry Blend — Equity Curves (log scale)")
        ax.set_ylabel("Portfolio value (rebased to 1.0)")
        ax.grid(alpha=0.3)

        for lbl, (dd, color, name) in dd_series.items():
            ax_dd.fill_between(dd.index, dd.values, 0, alpha=0.25, color=color, label=name)
            ax_dd.plot(dd.index, dd.values, color=color, lw=0.8, alpha=0.7)
        ax_dd.set_title("Drawdown")
        ax_dd.set_ylabel("Drawdown")
        ax_dd.legend(loc="lower left", fontsize=8)
        ax_dd.grid(alpha=0.3)

        fig.tight_layout()
        fig.savefig(HERE / "equity.png", dpi=100)
        plt.close(fig)
        print("\nSaved equity.png")
    except Exception as e:
        print(f"equity.png skipped: {e}")

    print(f"\nOutputs in: {HERE}")
    return metrics_out, combined, results, equity, variant_metrics, frontier, best_label, avg_w_trend


if __name__ == "__main__":
    run()
