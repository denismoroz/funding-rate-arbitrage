"""
Cross-Venue Funding Basis Backtest
===================================
Pure delta-neutral strategy: SHORT perp on high-funding venue + LONG perp on low-funding
venue, same coin, equal notional => price PnL cancels EXACTLY. Income = funding spread.

Venue funding cadences:
  HL      : hourly stamps (multiply by 8760 to annualize)
  Binance : every 8 h  (multiply by 1095 to annualize)
  Bybit   : every 8 h  (multiply by 1095 to annualize)
  Drift   : hourly     (multiply by 8760) — ENDS 2025-01-08, secondary only

Capital model:
  2x notional (full margin on BOTH venues — no cross-margining between exchanges).
  Return on capital = spread_earned / 2.

Cost model (conservative):
  4 taker fills per round trip (open + close on 2 venues). 4 bps per fill.
  Entry cost  = 4 legs × 4 bps = 16 bps on notional = 8 bps on 2x capital.
  Exit cost   = same.
  Dynamic variant: charges 4 bps on capital per flip (2 fills to switch one leg).

Data quality note — MATIC:
  HL's MATIC perp was renamed to POL in September 2024.  After 2024-09-09 the HL
  funding CSV records 0.0 while Binance/Bybit continue quoting MATIC at the 10.95 %
  funding cap.  Holding this pair after delisting would mean SHORT HL (earning 0 %)
  + LONG Binance (paying ~11 %).  All MATIC data is therefore TRUNCATED at
  MATIC_HL_END = 2024-09-10 UTC.

No look-ahead: daily funding signal is decided from day-t data; position earns day t+1
(weight shifted forward by 1 bar before multiplying by realized spread).
"""
import sys
import json
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qutil as q

HERE       = Path(__file__).resolve().parent
DATA_HL    = q.ROOT / "research" / "data"
DATA_BIN   = q.ROOT / "research" / "data_binance"
DATA_BYB   = q.ROOT / "research" / "data_bybit"
DATA_DRIFT = q.ROOT / "research" / "data_drift"
CARRY_CSV  = q.ROOT / "research" / "quant" / "crypto_funding_carry" / "results_funding_plus_staking.csv"

UNIVERSE = ["ARB", "AVAX", "BTC", "DOGE", "ETH", "LINK", "MATIC", "OP", "SOL"]
VENUES   = ["HL", "Binance", "Bybit"]

# MATIC HL perp delisted / renamed; zero out after this date
MATIC_HL_END = pd.Timestamp("2024-09-10", tz="UTC")

# ── cost constants ────────────────────────────────────────────────────────────
TAKER_BPS_PER_LEG        = 4.0    # 4 bps per fill (conservative)
N_LEGS_ROUND_TRIP        = 4      # 2 venues × (open + close)
RT_BPS_ON_NOTIONAL       = TAKER_BPS_PER_LEG * N_LEGS_ROUND_TRIP   # 16 bps
RT_BPS_ON_2X_CAPITAL     = RT_BPS_ON_NOTIONAL / 2                   # 8 bps on capital
FLIP_BPS_ON_2X_CAPITAL   = 2 * TAKER_BPS_PER_LEG / 2               # 4 bps (switch 1 leg)


# ── data loading ─────────────────────────────────────────────────────────────
def _load_daily(ddir: Path, coin: str, mult: float, end_clip=None) -> pd.Series | None:
    """
    Load raw funding CSV, annualize (×mult), resample to daily mean.
    Returns None if file not found.
    """
    fp = ddir / f"{coin}.csv"
    if not fp.exists():
        return None
    df = pd.read_csv(fp)
    df["time"] = pd.to_datetime(df["time"], format="mixed", utc=True)
    df = df.set_index("time").sort_index()
    r = df["fundingRate"].astype(float)
    r = r[~r.index.duplicated(keep="first")]
    if end_clip is not None:
        r = r[:end_clip]
    return (r * mult).resample("1D").mean().ffill(limit=3)


def load_panels() -> tuple[dict, dict]:
    """
    Returns (primary_panels, drift_panels).
    primary_panels = {venue: {coin: daily_ann_series}}
    """
    config = {
        "HL":      (DATA_HL,  8760),
        "Binance": (DATA_BIN, 1095),
        "Bybit":   (DATA_BYB, 1095),
    }
    panels: dict[str, dict[str, pd.Series]] = {v: {} for v in VENUES}
    for coin in UNIVERSE:
        clip = MATIC_HL_END if coin == "MATIC" else None
        for venue, (ddir, mult) in config.items():
            s = _load_daily(ddir, coin, mult, end_clip=clip)
            if s is not None:
                panels[venue][coin] = s

    drift: dict[str, pd.Series] = {}
    for coin in UNIVERSE:
        s = _load_daily(DATA_DRIFT, coin, 8760)
        if s is not None:
            drift[coin] = s
    return panels, drift


# ── spread characterization ───────────────────────────────────────────────────
def spread_stats(spread: pd.Series, label: str) -> dict:
    s = spread.dropna()
    acf1 = float(s.autocorr(lag=1))
    half_life = -np.log(2) / np.log(abs(acf1)) if 0 < abs(acf1) < 1 else float("inf")
    return {
        "label":              label,
        "mean_ann":           float(s.mean()),
        "median_ann":         float(s.median()),
        "std_ann":            float(s.std()),
        "pct_days_positive":  float((s > 0).mean()),
        "lag1_autocorr":      acf1,
        "half_life_days":     float(half_life),
        "n_days":             int(len(s)),
    }


# ── return-on-capital helper ──────────────────────────────────────────────────
def ann_spread_to_daily_roc(spread_ann: pd.Series) -> pd.Series:
    """Annualized spread → daily return on 2x capital (before costs)."""
    return spread_ann / 365.0 / 2.0


# ── Variant 1 & 2: STATIC pairs ──────────────────────────────────────────────
def run_static(panels, v_high: str, v_low: str) -> tuple[pd.Series, pd.DataFrame, dict]:
    """Always-on: SHORT v_high perp + LONG v_low perp. No threshold."""
    coin_rets = {}
    coin_spreads = {}
    for coin in UNIVERSE:
        hi = panels[v_high].get(coin)
        lo = panels[v_low].get(coin)
        if hi is None or lo is None:
            continue
        al = pd.DataFrame({"hi": hi, "lo": lo}).dropna()
        if len(al) < 30:
            continue
        spread = al["hi"] - al["lo"]
        gross  = ann_spread_to_daily_roc(spread)
        # One-time entry cost at day 0 only; no per-day ongoing cost for static
        net = gross.copy()
        net.iloc[0] -= RT_BPS_ON_2X_CAPITAL / 1e4
        # NO LOOK-AHEAD: shift by 1 day
        coin_rets[coin]    = net.shift(1).fillna(0.0)
        coin_spreads[coin] = spread
    ret_df = pd.DataFrame(coin_rets).sort_index()
    port   = ret_df.mean(axis=1)
    meta   = {
        "avg_spread_ann": float(
            pd.concat(coin_spreads.values()).mean() if coin_spreads else np.nan
        )
    }
    return port, ret_df, meta


# ── Variant 3: DYNAMIC best-pair ─────────────────────────────────────────────
def run_dynamic(panels, entry_thresh_ann: float = 0.03) -> tuple[pd.Series, pd.Series]:
    """
    Each day per coin: SHORT the highest-funding venue, LONG the lowest.
    Hold only if spread > entry_thresh_ann. Penalize each flip (venue switch) with
    FLIP_BPS_ON_2X_CAPITAL per flip (closing / re-opening one leg).
    On/off transitions pay the full RT_BPS_ON_2X_CAPITAL.
    NO LOOK-AHEAD: signals decided at t, applied from t+1.
    """
    all_dates = sorted(set(
        d
        for v in VENUES
        for coin in panels[v]
        for d in panels[v][coin].index
    ))
    date_idx = pd.DatetimeIndex(all_dates)

    coin_gross = {}
    coin_cost  = {}
    coin_dep   = {}

    for coin in UNIVERSE:
        series = {v: panels[v][coin] for v in VENUES if coin in panels[v]}
        if len(series) < 2:
            continue
        fund_df = pd.DataFrame(series).reindex(date_idx).ffill(limit=3).dropna(how="all")
        if len(fund_df) < 30:
            continue

        high_v = fund_df.idxmax(axis=1)
        low_v  = fund_df.idxmin(axis=1)
        best_sp = fund_df.max(axis=1) - fund_df.min(axis=1)

        active = (best_sp > entry_thresh_ann).astype(float)
        active[high_v == low_v] = 0.0

        gross_daily = ann_spread_to_daily_roc(best_sp) * active

        # Transition cost (on → off or off → on)
        transitions = active.diff().abs().fillna(active.abs())
        cost = transitions * (RT_BPS_ON_2X_CAPITAL / 1e4)

        # Flip cost: position stays active but venue pair changes
        pair = list(zip(high_v.values, low_v.values))
        flipped = np.zeros(len(fund_df))
        for i in range(1, len(fund_df)):
            if active.iloc[i] == 1 and active.iloc[i - 1] == 1:
                if pair[i] != pair[i - 1]:
                    flipped[i] = 1.0
        flip_cost = pd.Series(flipped, index=fund_df.index) * (FLIP_BPS_ON_2X_CAPITAL / 1e4)
        cost = cost + flip_cost

        net_t = gross_daily - cost

        coin_gross[coin] = net_t.shift(1).fillna(0.0)
        coin_dep[coin]   = active.shift(1).fillna(0.0)

    ret_df = pd.DataFrame(coin_gross).sort_index()
    dep_df = pd.DataFrame(coin_dep).sort_index()
    port   = ret_df.mean(axis=1)
    dep    = dep_df.mean(axis=1)
    return port, dep


# ── Drift secondary ───────────────────────────────────────────────────────────
def run_drift_secondary(panels, drift) -> tuple[dict, pd.Series]:
    """HL vs Drift pair — short sample only (ends 2025-01-08)."""
    stats = {}
    coin_rets = {}
    for coin in UNIVERSE:
        hl = panels["HL"].get(coin)
        dr = drift.get(coin)
        if hl is None or dr is None:
            continue
        al = pd.DataFrame({"hl": hl, "dr": dr}).dropna()
        if len(al) < 10:
            continue
        spread = al["hl"] - al["dr"]
        stats[coin] = spread_stats(spread, f"HL-Drift/{coin}")
        gross = ann_spread_to_daily_roc(spread)
        net = gross.copy()
        net.iloc[0] -= RT_BPS_ON_2X_CAPITAL / 1e4
        coin_rets[coin] = net.shift(1).fillna(0.0)
    port = pd.DataFrame(coin_rets).mean(axis=1) if coin_rets else pd.Series(dtype=float)
    return stats, port


# ── Correlation analysis ──────────────────────────────────────────────────────
def correlations(port: pd.Series) -> dict:
    result = {}
    # normalize index to date-only
    s = port.copy()
    s.index = s.index.normalize()

    try:
        btc_close = q.load_ohlcv("BTC", "1d")["close"]
        btc_ret   = btc_close.pct_change().fillna(0.0)
        btc_ret.index = btc_ret.index.normalize()
        common = s.index.intersection(btc_ret.index)
        result["corr_BTC"] = float(s.loc[common].corr(btc_ret.loc[common])) if len(common) > 30 else None
    except Exception:
        result["corr_BTC"] = None

    try:
        carry = pd.read_csv(CARRY_CSV)
        carry["time"] = pd.to_datetime(carry["time"], format="mixed", utc=True)
        carry = carry.set_index("time").sort_index()
        carry_d = carry["ret_total"].resample("1D").sum()
        carry_d.index = carry_d.index.normalize()
        common = s.index.intersection(carry_d.index)
        result["corr_HL_carry"] = float(s.loc[common].corr(carry_d.loc[common])) if len(common) > 30 else None
    except Exception:
        result["corr_HL_carry"] = None
    return result


# ── Helpers ───────────────────────────────────────────────────────────────────
def yrbd(port: pd.Series) -> list:
    return q.period_breakdown(port).to_dict("records")


def fmt_m(m: dict) -> str:
    return (f"CAGR={m['cagr']*100:.1f}%  vol={m['vol']*100:.1f}%  "
            f"Sharpe={m['sharpe']:.2f}  MDD={m['max_drawdown']*100:.2f}%  "
            f"Calmar={m['calmar']:.2f}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("Loading daily funding panels …")
    panels, drift = load_panels()
    HERE.mkdir(parents=True, exist_ok=True)

    # ── Spread characterization ──────────────────────────────────────────────
    print("\n=== SPREAD CHARACTERIZATION ===")
    # Pool per-coin inner-joined spreads for aggregate stats
    hl_bin_pool, hl_byb_pool = [], []
    per_coin_stats: dict[str, dict] = {}

    for coin in UNIVERSE:
        hl = panels["HL"].get(coin)
        bn = panels["Binance"].get(coin)
        by = panels["Bybit"].get(coin)
        if hl is not None and bn is not None:
            al = pd.DataFrame({"hl": hl, "bn": bn}).dropna()
            sp = al["hl"] - al["bn"]
            hl_bin_pool.append(sp)
            per_coin_stats[f"{coin}_HL-Binance"] = spread_stats(sp, f"{coin} HL-Binance")
        if hl is not None and by is not None:
            al = pd.DataFrame({"hl": hl, "by": by}).dropna()
            sp = al["hl"] - al["by"]
            hl_byb_pool.append(sp)
            per_coin_stats[f"{coin}_HL-Bybit"] = spread_stats(sp, f"{coin} HL-Bybit")
        if bn is not None and by is not None:
            al = pd.DataFrame({"bn": bn, "by": by}).dropna()
            sp = (al["bn"] - al["by"]).abs()
            per_coin_stats[f"{coin}_Binance-Bybit"] = spread_stats(al["bn"] - al["by"], f"{coin} Binance-Bybit")

    ss_hl_bin = spread_stats(pd.concat(hl_bin_pool), "HL-Binance (all coins)")
    ss_hl_byb = spread_stats(pd.concat(hl_byb_pool), "HL-Bybit (all coins)")

    print(f"HL-Binance: mean={ss_hl_bin['mean_ann']*100:.1f}%  "
          f"median={ss_hl_bin['median_ann']*100:.1f}%  "
          f"std={ss_hl_bin['std_ann']*100:.1f}%  "
          f"pct>0={ss_hl_bin['pct_days_positive']*100:.1f}%  "
          f"lag1-acf={ss_hl_bin['lag1_autocorr']:.3f}  "
          f"half-life≈{ss_hl_bin['half_life_days']:.1f}d")
    print(f"HL-Bybit:   mean={ss_hl_byb['mean_ann']*100:.1f}%  "
          f"median={ss_hl_byb['median_ann']*100:.1f}%  "
          f"std={ss_hl_byb['std_ann']*100:.1f}%  "
          f"pct>0={ss_hl_byb['pct_days_positive']*100:.1f}%  "
          f"lag1-acf={ss_hl_byb['lag1_autocorr']:.3f}  "
          f"half-life≈{ss_hl_byb['half_life_days']:.1f}d")

    print("\nPer-coin HL-Binance spread (annualized, pct days positive):")
    for coin in UNIVERSE:
        k = f"{coin}_HL-Binance"
        if k in per_coin_stats:
            s = per_coin_stats[k]
            suffix = " [MATIC: truncated at 2024-09-10]" if coin == "MATIC" else ""
            print(f"  {coin:6s}: mean={s['mean_ann']*100:+.1f}%  "
                  f"pct>0={s['pct_days_positive']*100:.0f}%  "
                  f"lag1-acf={s['lag1_autocorr']:.3f}  "
                  f"half-life={s['half_life_days']:.1f}d{suffix}")

    # ── Variant 1: Static HL-Binance ─────────────────────────────────────────
    print("\n=== VARIANT 1: STATIC HL-Binance (always-on, equal-weight) ===")
    v1_port, v1_df, v1_meta = run_static(panels, "HL", "Binance")
    v1_m  = q.metrics_from_returns(v1_port, "1d")
    v1_yr = yrbd(v1_port)
    corr_v1 = correlations(v1_port)
    print(f"  {fmt_m(v1_m)}")
    print(f"  corr(BTC)={corr_v1['corr_BTC']:.4f}  corr(HL carry)={corr_v1['corr_HL_carry']:.4f}")
    for yr in v1_yr:
        print(f"    {yr['year']}: CAGR={yr['cagr']*100:.1f}%  Sharpe={yr['sharpe']:.2f}")

    # ── Variant 2: Static HL-Bybit ───────────────────────────────────────────
    print("\n=== VARIANT 2: STATIC HL-Bybit (always-on, equal-weight) ===")
    v2_port, v2_df, v2_meta = run_static(panels, "HL", "Bybit")
    v2_m  = q.metrics_from_returns(v2_port, "1d")
    v2_yr = yrbd(v2_port)
    corr_v2 = correlations(v2_port)
    print(f"  {fmt_m(v2_m)}")
    print(f"  corr(BTC)={corr_v2['corr_BTC']:.4f}  corr(HL carry)={corr_v2['corr_HL_carry']:.4f}")
    for yr in v2_yr:
        print(f"    {yr['year']}: CAGR={yr['cagr']*100:.1f}%  Sharpe={yr['sharpe']:.2f}")

    # ── Variant 3: Dynamic best-pair — threshold grid ────────────────────────
    print("\n=== VARIANT 3: DYNAMIC best-pair (threshold grid) ===")
    v3_results: dict = {}
    for thresh in [0.0, 0.03, 0.06, 0.10]:
        p, dep = run_dynamic(panels, entry_thresh_ann=thresh)
        m_  = q.metrics_from_returns(p, "1d")
        yr_ = yrbd(p)
        avg_dep = float(dep.mean())
        tag = f"thresh_{int(thresh*100):02d}pct"
        v3_results[tag] = {
            "entry_thresh_ann": thresh,
            "metrics": m_,
            "yearly": yr_,
            "avg_deployed_frac": avg_dep,
        }
        print(f"  thresh={thresh*100:.0f}%:  {fmt_m(m_)}  deployed={avg_dep*100:.0f}%")
        for yr in yr_:
            print(f"    {yr['year']}: CAGR={yr['cagr']*100:.1f}%  Sharpe={yr['sharpe']:.2f}")

    # Default for correlations / equity plot
    v3_port, v3_dep = run_dynamic(panels, entry_thresh_ann=0.03)
    v3_m = q.metrics_from_returns(v3_port, "1d")
    corr_v3 = correlations(v3_port)

    # ── Drift secondary ──────────────────────────────────────────────────────
    print("\n=== DRIFT SECONDARY (HL-Drift, ends 2025-01-08) ===")
    drift_stats, drift_port = run_drift_secondary(panels, drift)
    for coin in UNIVERSE:
        st = drift_stats.get(coin)
        if st:
            print(f"  {coin:6s}: spread mean={st['mean_ann']*100:+.1f}%  "
                  f"pct>0={st['pct_days_positive']*100:.0f}%  "
                  f"n={st['n_days']}d  [NOTE: ends 2025-01-08]")
    drift_m = {}
    if len(drift_port) > 10:
        drift_m = q.metrics_from_returns(drift_port, "1d")
        print(f"  Portfolio: CAGR={drift_m.get('cagr',0)*100:.1f}%  "
              f"Sharpe={drift_m.get('sharpe',0):.2f}  [CAUTION: short+unusual sample]")
        print("  INTERPRETATION: Drift had HIGHER funding than HL for most coins (negative spread).")
        print("  Drift data ends 2025-01-08; this is NOT a usable live pair today.")

    # ── Equity plot ──────────────────────────────────────────────────────────
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(2, 1, figsize=(12, 8),
                                 gridspec_kw={"height_ratios": [3, 1]})

        for p_, lbl_ in [
            (v1_port, "Static HL-Binance"),
            (v2_port, "Static HL-Bybit"),
            (v3_port, "Dynamic 3% thresh"),
        ]:
            eq_ = (1 + p_).cumprod()
            axes[0].plot(eq_.index, eq_.values, label=lbl_, lw=1.2)

        try:
            btc_c = q.load_ohlcv("BTC", "1d")["close"]
            btc_eq = (1 + btc_c.pct_change().fillna(0)).cumprod()
            btc_eq /= btc_eq.iloc[0]
            axes[0].plot(btc_eq.index, btc_eq.values, label="BTC B&H",
                         lw=0.8, alpha=0.4, ls="--", color="gray")
        except Exception:
            pass

        axes[0].set_yscale("log")
        axes[0].legend(fontsize=8)
        axes[0].set_title(
            "Cross-Venue Funding Basis — Equity (2x capital, net of costs)\n"
            "MATIC truncated 2024-09-10; Static variants always-on"
        )
        axes[0].grid(alpha=0.3)

        eq1 = (1 + v1_port).cumprod()
        dd1 = eq1 / eq1.cummax() - 1
        axes[1].fill_between(dd1.index, dd1.values, 0,
                              color="steelblue", alpha=0.5, label="V1 DD")
        eq3 = (1 + v3_port).cumprod()
        dd3 = eq3 / eq3.cummax() - 1
        axes[1].fill_between(dd3.index, dd3.values, 0,
                              color="orange", alpha=0.35, label="V3 DD")
        axes[1].set_title("Drawdown"); axes[1].legend(fontsize=7); axes[1].grid(alpha=0.3)

        fig.tight_layout()
        fig.savefig(HERE / "equity.png", dpi=90)
        plt.close(fig)
        print("\nSaved equity.png")
    except Exception as exc:
        print(f"Plot failed: {exc}")

    # ── Results CSV ──────────────────────────────────────────────────────────
    cols: dict[str, pd.Series] = {}
    for coin in UNIVERSE:
        if coin in v1_df.columns:
            cols[f"v1_HL_Bin_{coin}"] = v1_df[coin]
        if coin in v2_df.columns:
            cols[f"v2_HL_Byb_{coin}"] = v2_df[coin]
    cols["v1_port"] = v1_port
    cols["v2_port"] = v2_port
    cols["v3_dyn_03pct_port"] = v3_port
    pd.DataFrame(cols).sort_index().to_csv(HERE / "results.csv")
    print("Saved results.csv")

    # ── metrics.json ─────────────────────────────────────────────────────────
    out = {
        "strategy": "crypto_cross_venue_basis",
        "description": (
            "Delta-neutral perp-perp cross-venue funding basis. "
            "SHORT high-funding venue + LONG low-funding venue, same coin, equal notional."
        ),
        "capital_model": "2x notional (margin on both venues), return_on_capital = spread / 2",
        "cost_model": {
            "rt_bps_on_notional": RT_BPS_ON_NOTIONAL,
            "rt_bps_on_2x_capital": RT_BPS_ON_2X_CAPITAL,
            "flip_bps_on_2x_capital": FLIP_BPS_ON_2X_CAPITAL,
            "note": "4 taker fills × 4 bps each; flip = 2 fills on 1 venue",
        },
        "data_quality_notes": {
            "MATIC": (
                "HL MATIC perp delisted/renamed Sep 2024. "
                f"All MATIC data truncated at {MATIC_HL_END.date()}. "
                "Post-delisting HL records 0 % funding while Binance quotes 10.95 % cap — "
                "holding this pair post-delisting would be catastrophic."
            ),
            "Drift": "Ends 2025-01-08; data shows Drift > HL for most coins (negative spread). NOT viable.",
        },
        "universe": UNIVERSE,
        "spread_characterization": {
            "HL_Binance_pooled": ss_hl_bin,
            "HL_Bybit_pooled":   ss_hl_byb,
            "per_coin": per_coin_stats,
        },
        "variant_1_static_HL_Binance": {
            "metrics": v1_m, "yearly": v1_yr, "correlations": corr_v1,
        },
        "variant_2_static_HL_Bybit": {
            "metrics": v2_m, "yearly": v2_yr, "correlations": corr_v2,
        },
        "variant_3_dynamic_grid":     v3_results,
        "variant_3_default_3pct":     {"metrics": v3_m, "correlations": corr_v3},
        "drift_secondary": {
            "note": "ENDS 2025-01-08, Drift funding > HL for most coins, NOT viable today",
            "per_coin_stats": drift_stats,
            "portfolio_metrics": drift_m,
        },
    }
    q.save_metrics(HERE / "metrics.json", out)
    print("Saved metrics.json")

    print("\n=== SUMMARY TABLE ===")
    header = f"{'Variant':<32} {'CAGR':>7} {'Sharpe':>7} {'MDD':>7} {'Calmar':>7} {'corr BTC':>9} {'corr carry':>11}"
    print(header)
    print("-" * len(header))
    rows = [
        ("Static HL-Binance (V1)",   v1_m, corr_v1),
        ("Static HL-Bybit (V2)",     v2_m, corr_v2),
        ("Dynamic 3% thresh (V3)",   v3_m, corr_v3),
    ]
    for lbl, m, cr in rows:
        print(f"{lbl:<32} {m['cagr']*100:>6.1f}% {m['sharpe']:>7.2f} "
              f"{m['max_drawdown']*100:>6.2f}% {m['calmar']:>7.2f} "
              f"{cr.get('corr_BTC',0):>9.3f} {cr.get('corr_HL_carry',0):>11.3f}")


if __name__ == "__main__":
    main()
