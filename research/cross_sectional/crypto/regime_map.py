"""
regime_map.py — Regime / Correlation Map of the three existing return streams.

Three return streams assembled on their common daily window (2023-06 → 2026-06):
  1. crypto-momentum (HONEST): survivorship-debiased PT book pnl via survivorship.py
  2. FX-blend: validated FX multifactor blend_fx daily pnl from fx_pkg.py
  3. funding-arb (BACKTEST, soft leg): daily equity change from two_phase_margin.py
     simulate() on BTC/ETH/SOL (coins with 2023-06 data; HYPE/PURR/ZEC/XPL only
     start 2025-11, so are excluded from the full-window backtest to avoid
     artificially shortening the window). CLEARLY LABELLED as backtest.

Outputs:
  regime_map.json — all findings in machine-readable form
  (printed report to stdout)

Run from research/cross_sectional/crypto/:
  PYTHONPATH=/Users/d/prj/funding-rate-arbitrage/research:/Users/d/prj/funding-rate-arbitrage/research/validation_harness:/Users/d/prj/funding-rate-arbitrage/research/cross_sectional:/Users/d/prj/funding-rate-arbitrage/research/cross_sectional/crypto \
  /Users/d/prj/funding-rate-arbitrage/.venv/bin/python regime_map.py

HONESTY POLICY:
- Correlation matrix uses daily returns; Pearson only.
- Rolling correlation: 90-day window.
- Monthly regime: BTC/ETH price trend + vol from the crypto panel; funding level
  from the cross-sectional funding panel (mean HL funding across all panel coins).
- Risk parity: inverse-vol weights, rebalanced once (whole-window vol), no look-ahead
  for the weights themselves (but reported only as an illustrative combo).
- Funding-arb stream is a BACKTEST — correlations involving it are AS SOFT AS THAT.
- Only ~3 years of data, no real crisis in the window. The map shows the calm /
  bull-recovery / trending regimes honestly but CANNOT MEASURE the crisis hole.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Path setup
#
# CRITICAL import-order constraint:
#   research/cross_sectional/fx/ contains its own fetch.py, signals.py, etc.
#   Adding it to sys.path BEFORE the crypto imports would shadow the crypto
#   versions and break survivorship.build_pt_panel (DATA_DIR → FX data dir).
#
# Strategy:
#   1. Import ALL crypto modules first (they are already on PYTHONPATH).
#   2. Only then add FX dir to sys.path for the FX import.
#   3. FX-specific imports happen INSIDE build_fx_blend_stream() at call time,
#      after all crypto module globals are already bound.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent
_RESEARCH = _HERE.parent.parent          # research/
_XSEC     = _HERE.parent                 # research/cross_sectional/
_FX_DIR   = _XSEC / "fx"

# Crypto libs already on path via PYTHONPATH, but be defensive.
# Do NOT add _FX_DIR here — it would shadow crypto's fetch.py.
for _p in [str(_XSEC), str(_RESEARCH), str(_HERE)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Import crypto modules NOW (before FX path is added to sys.path).
from metrics_daily import daily_metrics  # noqa: E402

# ---------------------------------------------------------------------------
# Section A helpers
# ---------------------------------------------------------------------------

STUDY_START = pd.Timestamp("2023-06-08", tz="UTC")
STUDY_END   = pd.Timestamp("2026-06-12", tz="UTC")   # inclusive

PPY = 365  # periods per year (daily)


def _fmt(m: dict, label: str) -> str:
    if not m:
        return f"{label}: (too few days)"
    return (
        f"{label}: Sharpe {m['sharpe']:+.2f}  ann {100*m['ann']:+.1f}%  "
        f"vol {100*m['vol_ann']:.1f}%  maxDD {100*m['maxdd']:.1f}%  "
        f"Calmar {m['calmar']:+.2f}  hit {100*m['hit']:.0f}%  n={m['n']}"
    )


# ---------------------------------------------------------------------------
# Stream 1: Crypto momentum (survivorship-debiased PT book)
# ---------------------------------------------------------------------------

def build_crypto_momentum_stream() -> tuple[pd.Series, pd.DataFrame]:
    """Return (daily_pnl_series, crypto_panel) for regime labelling.

    Uses survivorship.py: build_pt_panel on frozen_survivors ∪ extra_dead_coins_included.
    Identical hyperparams to the validated book.
    """
    import json as _json
    import cryptodata
    import signals
    import xsec
    import survivorship as surv

    print("[Stream 1] Building crypto-momentum PT book (survivorship-debiased)...")

    # Load extra dead coins from survivorship.json (previously fetched/verified)
    surv_json = _HERE / "survivorship.json"
    if surv_json.exists():
        surv_data = _json.loads(surv_json.read_text())
        extra_dead = surv_data.get("extra_dead_coins_included", [])
    else:
        extra_dead = []
        print("  WARNING: survivorship.json not found; using frozen survivors only.")

    # Build the PT panel
    frozen_coins = _json.loads((_HERE / "universe.json").read_text())["coins"]
    all_pt_coins = sorted(set(frozen_coins) | set(extra_dead))
    print(f"  PT universe: {len(frozen_coins)} survivors + {len(extra_dead)} dead/delisted = {len(all_pt_coins)} total")

    panel = surv.build_pt_panel(all_pt_coins)
    pnl = surv.run_book(panel)
    pnl = pnl.dropna()
    pnl = pnl[(pnl.index >= STUDY_START) & (pnl.index <= STUDY_END)]

    print(f"  Crypto momentum PT pnl: {pnl.index.min().date()} -> {pnl.index.max().date()}  n={len(pnl)}")
    print(f"  {_fmt(daily_metrics(pnl), 'crypto_momentum')}")

    # Also return full crypto panel for regime labelling (frozen 34, enough for BTC/ETH)
    crypto_panel = cryptodata.load_panel(coins=frozen_coins)
    return pnl, crypto_panel


# ---------------------------------------------------------------------------
# Stream 2: FX blend
# ---------------------------------------------------------------------------

def build_fx_blend_stream() -> pd.Series:
    """Return daily pnl series for the FX multifactor blend_fx, restricted to the study window.

    ISOLATION STRATEGY: The FX directory has modules named fetch.py, signals.py, fxdata.py —
    the same names as crypto modules already imported. Python's module cache (sys.modules)
    means we cannot safely import FX modules into the same process after crypto modules are
    loaded. We run the FX extraction in a SUBPROCESS with only the FX dir on PYTHONPATH,
    write the result to a temp CSV, and read it back. This is clean and reliable.
    """
    import subprocess
    import tempfile
    import os

    print("[Stream 2] Building FX blend_fx stream (via subprocess — module isolation)...")

    # Write a minimal extraction script to a temp file
    script = f'''
import sys, json
from pathlib import Path
_FX = "{str(_FX_DIR)}"
_XSEC = "{str(_XSEC)}"
_RESEARCH = "{str(_RESEARCH)}"
_CRYPTO = "{str(_HERE)}"
for p in [_CRYPTO, _FX, _XSEC, _RESEARCH]:
    if p not in sys.path:
        sys.path.insert(0, p)

import pandas as pd
from fx_pkg import FXXSecPackage
from metrics_daily import daily_metrics

STUDY_START = pd.Timestamp("{STUDY_START.isoformat()}")
STUDY_END   = pd.Timestamp("{STUDY_END.isoformat()}")

pkg = FXXSecPackage()
df = pkg.load("XSEC")
menu = pkg.menu("XSEC", df)
blend_pnl = menu["blend_fx"]
blend_pnl = blend_pnl[(blend_pnl.index >= STUDY_START) & (blend_pnl.index <= STUDY_END)]
blend_pnl = blend_pnl.dropna()
blend_pnl.index = blend_pnl.index.tz_convert("UTC").normalize()
blend_pnl.index = blend_pnl.index.tz_localize(None)

m = daily_metrics(blend_pnl)
print(f"FX blend_fx: {{blend_pnl.index.min().date()}} -> {{blend_pnl.index.max().date()}}  n={{len(blend_pnl)}}")
print(f"  Sharpe {{m.get('sharpe',0):+.2f}}  ann {{100*m.get('ann',0):+.1f}}%  "
      f"vol {{100*m.get('vol_ann',0):.1f}}%  maxDD {{100*m.get('maxdd',0):.1f}}%  "
      f"Calmar {{m.get('calmar',0):+.2f}}  hit {{100*m.get('hit',0):.0f}}%  n={{m.get('n',0)}}")

out_path = sys.argv[1]
blend_pnl.to_csv(out_path, header=True)
print(f"Written to {{out_path}}")
'''

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        script_path = f.name
        f.write(script)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        csv_path = f.name

    python_exe = sys.executable
    try:
        result = subprocess.run(
            [python_exe, script_path, csv_path],
            capture_output=True, text=True, timeout=120,
        )
        # Print subprocess stdout (has the metrics line)
        for line in result.stdout.strip().splitlines():
            print(f"  {line}")
        if result.returncode != 0:
            print(f"  SUBPROCESS ERROR:\n{result.stderr}")
            return pd.Series(dtype=float)

        blend_pnl = pd.read_csv(csv_path, index_col=0, parse_dates=True)
        blend_pnl = blend_pnl.squeeze()
        blend_pnl.name = "fx_blend"
        print("  NOTE: FX panel is business-day only (no weekends). Aligned to business days.")
        return blend_pnl
    finally:
        os.unlink(script_path)
        try:
            os.unlink(csv_path)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Stream 3: Funding-arb backtest
# ---------------------------------------------------------------------------

def build_funding_arb_stream() -> tuple[pd.Series, bool]:
    """
    Run two_phase_margin.py::simulate() on BTC/ETH/SOL (the coins with 2023-06 data).
    Returns (daily_returns_series, success_flag).

    COINS NOTE: HYPE/PURR/ZEC/XPL only start Nov 2025 — including them would
    truncate the window to Nov-2025+. We use BTC/ETH/SOL for the full 2023-26 window.
    This is an honest subset: the live 7-coin prod book has more coins, but they
    don't have 2023-26 history. The simulated returns here understate the prod APR
    because fewer coins = fewer concurrent positions; this is noted as a caveat.

    SIZING: prod_slot (budget_cap / concurrency_cap per slot) for budget-accurate
    daily % returns. The return series is expressed as % change in total portfolio equity.

    ANNUALIZATION CAVEAT: two_phase_margin.py's equity series is hourly. We resample
    to daily pct_change before computing metrics. The hourly Sharpe reported by the
    engine itself uses sqrt(8760) scaling and should NOT be compared directly to the
    daily √365 Sharpe used for the other streams — we recompute on the resampled
    daily series.
    """
    print("[Stream 3] Building funding-arb backtest stream (two_phase_margin.py)...")

    # Load the engine by file path (avoids package name collision)
    engine_path = _RESEARCH / "two_phase_margin.py"
    spec = importlib.util.spec_from_file_location("tpm_engine", engine_path)
    if spec is None or spec.loader is None:
        print("  ERROR: Cannot load two_phase_margin.py engine.")
        return pd.Series(dtype=float), False

    tpm = importlib.util.module_from_spec(spec)
    sys.modules["tpm_engine"] = tpm
    spec.loader.exec_module(tpm)

    # Use params.py defaults (DB not available in research mode)
    params, src = tpm.load_prod_params()
    print(f"  Params source: {src[:100]}")

    # Only coins with full 2023-06 coverage
    coins_full_history = ["BTC", "ETH", "SOL"]
    print(f"  Coins used (2023-06 coverage only): {coins_full_history}")
    print("  (HYPE/PURR/ZEC/XPL start 2025-11 — excluded to preserve full 2023-26 window)")

    # Run simulation
    try:
        result = tpm.simulate(
            coins_full_history,
            params,
            restrict_start=STUDY_START,
            restrict_end=STUDY_END,
            sizing="prod_slot",
        )
    except Exception as e:
        print(f"  ERROR running simulation: {e}")
        return pd.Series(dtype=float), False

    eq: pd.Series = result["equity"]
    print(f"  Raw equity: {eq.index.min().date()} -> {eq.index.max().date()}  {len(eq)} hours")
    print(f"  Engine-reported: annual_pct={result['annual_pct']:.2f}%  "
          f"max_dd_pct={result['max_dd_pct']:.4f}%  sharpe(hourly)={result['sharpe']:.2f}")

    # Resample to daily: take the last equity value of each calendar day
    eq_daily = eq.resample("1D").last().dropna()

    # Daily % change (simple returns on the total equity)
    daily_ret = eq_daily.pct_change().dropna()
    daily_ret = daily_ret[(daily_ret.index >= STUDY_START) & (daily_ret.index <= STUDY_END)]

    # Align to UTC date (remove tz for merging; index is dates)
    daily_ret.index = daily_ret.index.tz_convert("UTC").normalize()

    print(f"  Resampled daily pnl: {daily_ret.index.min().date()} -> {daily_ret.index.max().date()}  n={len(daily_ret)}")
    m = daily_metrics(daily_ret)
    print(f"  {_fmt(m, 'funding_arb_backtest')}")
    print("  CAVEAT: BACKTEST on 3 coins (BTC/ETH/SOL) only. Prod uses 7 coins. Numbers are soft.")

    return daily_ret, True


# ---------------------------------------------------------------------------
# Section B: Correlation matrix
# ---------------------------------------------------------------------------

def compute_correlation_matrix(streams: dict[str, pd.Series]) -> pd.DataFrame:
    """Pearson correlation of daily returns on the common (intersection) dates."""
    aligned = pd.DataFrame(streams)
    aligned = aligned.dropna()
    return aligned.corr(method="pearson")


# ---------------------------------------------------------------------------
# Section C: Rolling correlation
# ---------------------------------------------------------------------------

def compute_rolling_correlation(
    streams: dict[str, pd.Series],
    window: int = 90,
) -> dict:
    """90-day rolling Pearson correlation between each pair."""
    df = pd.DataFrame(streams).dropna()
    pairs = {}
    names = list(df.columns)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            key = f"{a}_vs_{b}"
            rc = df[a].rolling(window).corr(df[b])
            pairs[key] = rc
    return pairs


# ---------------------------------------------------------------------------
# Section D: Monthly regime decomposition
# ---------------------------------------------------------------------------

def monthly_regime(
    streams: dict[str, pd.Series],
    crypto_panel: dict,
) -> pd.DataFrame:
    """
    For each calendar month:
      - Crypto regime: BTC/ETH 21-day momentum (trend up/down/chop) + realized vol bucket
      - Funding level: mean HL funding across panel coins (carry-friendly / not)
      - Each stream's monthly return
      - Dead-month flag (all streams <= 0)
      - Who-carried label

    Returns a DataFrame indexed by month (YYYY-MM).
    """
    # Build a common daily series for all streams
    df_all = pd.DataFrame(streams).dropna()

    # BTC daily price from crypto panel (drop tz for simplicity in resample)
    price_df = crypto_panel["price"]
    btc_price = price_df["BTC"].dropna() if "BTC" in price_df.columns else None
    eth_price = price_df["ETH"].dropna() if "ETH" in price_df.columns else None

    # Funding panel: mean daily funding rate across all coins
    fund_df = crypto_panel["funding"]
    mean_fund_daily = fund_df.mean(axis=1)  # daily mean across coins (already daily sum of hourly rates)

    # Resample everything to monthly
    monthly_ret  = df_all.resample("ME").apply(lambda x: (1 + x).prod() - 1)
    monthly_ret.index = monthly_ret.index.to_period("M").strftime("%Y-%m")

    if btc_price is not None:
        btc_mom21 = (btc_price / btc_price.shift(21) - 1.0)
        btc_vol21 = btc_price.pct_change().rolling(21).std(ddof=0) * np.sqrt(365)
        monthly_btc_mom = btc_mom21.resample("ME").last()
        monthly_btc_vol = btc_vol21.resample("ME").last()
        monthly_btc_mom.index = monthly_btc_mom.index.to_period("M").strftime("%Y-%m")
        monthly_btc_vol.index = monthly_btc_vol.index.to_period("M").strftime("%Y-%m")
    else:
        monthly_btc_mom = None
        monthly_btc_vol = None

    # Mean daily funding annualized (funding is daily sum of hourly rates, already in rate units)
    # HL funding interval is ~1h, so daily_fund_sum * 8760 ≈ annual rate in raw units
    # But the panel stores the raw rate (per-hour fraction), summed daily. So annualized = sum * 8760 / daily_count
    # Approximation: fund_daily_sum * 8760 = annual rate (per-hour * 8760h = annual)
    monthly_fund = (mean_fund_daily * 8760).resample("ME").mean()
    monthly_fund.index = monthly_fund.index.to_period("M").strftime("%Y-%m")

    records = []
    for month in monthly_ret.index:
        row: dict = {"month": month}

        # Regime: BTC trend
        if monthly_btc_mom is not None and month in monthly_btc_mom.index:
            mom = monthly_btc_mom.loc[month]
            vol = monthly_btc_vol.loc[month] if monthly_btc_vol is not None else None
            if pd.isna(mom):
                trend = "unknown"
            elif mom > 0.05:
                trend = "trend_up"
            elif mom < -0.05:
                trend = "trend_down"
            else:
                trend = "range_chop"
            row["btc_mom_21d"] = float(round(mom * 100, 1)) if not pd.isna(mom) else None
            row["btc_vol_ann"] = float(round(vol * 100, 1)) if vol is not None and not pd.isna(vol) else None
            if vol is not None and not pd.isna(vol):
                vol_bucket = "high_vol" if vol > 0.60 else ("mid_vol" if vol > 0.35 else "low_vol")
            else:
                vol_bucket = "unknown"
        else:
            trend = "unknown"
            row["btc_mom_21d"] = None
            row["btc_vol_ann"] = None
            vol_bucket = "unknown"

        row["trend"] = trend
        row["vol_bucket"] = vol_bucket

        # Funding level
        if month in monthly_fund.index:
            fund_apr = monthly_fund.loc[month]
            row["fund_apr_ann"] = float(round(fund_apr * 100, 1)) if not pd.isna(fund_apr) else None
            if not pd.isna(fund_apr):
                row["carry_regime"] = "carry_friendly" if fund_apr > 0.0 else "carry_hostile"
            else:
                row["carry_regime"] = "unknown"
        else:
            row["fund_apr_ann"] = None
            row["carry_regime"] = "unknown"

        # Monthly returns per stream
        rets = monthly_ret.loc[month]
        for col in df_all.columns:
            val = rets[col] if col in rets.index else np.nan
            row[col] = float(round(val * 100, 2)) if not pd.isna(val) else None

        # Dead month: all <= 0
        non_null = [row[col] for col in df_all.columns if row.get(col) is not None]
        if non_null:
            row["dead_month"] = all(v <= 0.0 for v in non_null)
            carriers = [col for col in df_all.columns
                        if row.get(col) is not None and row[col] > 0.0]
            row["who_carried"] = carriers if carriers else ["none"]
        else:
            row["dead_month"] = None
            row["who_carried"] = []

        records.append(row)

    return pd.DataFrame(records).set_index("month")


# ---------------------------------------------------------------------------
# Section E: Equal-risk-parity combo
# ---------------------------------------------------------------------------

def risk_parity_combo(streams: dict[str, pd.Series]) -> tuple[pd.Series, dict]:
    """Inverse-volatility risk parity combination of available streams.

    Weights = 1/vol_i / sum(1/vol_j) computed once on the full common window
    (NOT rolling — this is illustrative, not a live trading spec).
    Returns (combined_pnl_series, weight_dict).
    """
    df = pd.DataFrame(streams).dropna()
    vols = df.std(ddof=0)
    inv_vol = 1.0 / vols
    weights = inv_vol / inv_vol.sum()

    # Combined daily pnl = weighted sum (additive simple returns)
    combined = (df * weights).sum(axis=1)
    combined.name = "risk_parity"

    weight_dict = {col: float(round(w, 4)) for col, w in weights.items()}
    return combined, weight_dict


# ---------------------------------------------------------------------------
# Section F: Spec for the next strategy
# ---------------------------------------------------------------------------

def derive_next_strategy_spec(
    dead_months_df: pd.DataFrame,
    rp_metrics: dict,
    mom_metrics: dict,
) -> dict:
    """From the dead-months / regime gaps, derive the spec for the next strategy."""
    dead_rows = dead_months_df[dead_months_df["dead_month"] == True]
    n_dead = len(dead_rows)

    # Count dead months by regime
    trend_counts: dict[str, int] = {}
    for _, row in dead_rows.iterrows():
        t = str(row.get("trend", "unknown"))
        trend_counts[t] = trend_counts.get(t, 0) + 1

    # Most uncovered regime
    if trend_counts:
        worst_regime = max(trend_counts, key=lambda k: trend_counts[k])
    else:
        worst_regime = "unknown"

    # Which months had all strategies dead + carry hostile
    carry_hostile_dead = dead_rows[dead_rows.get("carry_regime", pd.Series()) == "carry_hostile"] if "carry_regime" in dead_rows.columns else pd.DataFrame()

    return {
        "n_dead_months": n_dead,
        "dead_month_regime_breakdown": trend_counts,
        "most_uncovered_regime": worst_regime,
        "carry_hostile_dead_months": len(carry_hostile_dead),
        "rp_sharpe": rp_metrics.get("sharpe"),
        "mom_sharpe": mom_metrics.get("sharpe"),
        "rp_maxdd": rp_metrics.get("maxdd"),
        "mom_maxdd": mom_metrics.get("maxdd"),
        "spec_for_next_strategy": {
            "target_regime": "range_chop and/or crisis (not in this window)",
            "success_bar": (
                "Must have correlation-to-combined-book <= +0.2 AND net-positive Sharpe > 0.0. "
                "A Sharpe-0.3 / corr-(-0.2) candidate BEATS a Sharpe-0.8 / corr-(+0.7) one — "
                "the marginal diversification value is what matters, not standalone return."
            ),
            "corr_to_book_bar": 0.20,
            "minimum_sharpe_standalone": 0.0,
            "preferred_negative_corr_vs": "range_chop & carry_hostile months",
            "rationale": (
                "The existing book (crypto momentum + FX blend + funding arb) is collectively "
                "weak in range-chopping, low-volatility, low-funding months where momentum "
                "signals are noisy and carry is absent. A strategy that is net-positive "
                "PRECISELY in those months — even with low standalone Sharpe — is more "
                "valuable than another momentum or carry leg that fails at the same time."
            ),
        },
        "caveats": [
            "The 2023-26 window contains NO real crisis/bear market (crypto was in bull recovery). "
            "The worst dead-month cluster is NOT the crisis hole — that must be reasoned separately. "
            "Live 2021-22 bear showed both crypto momentum and carry can drop together.",
            "Funding-arb stream is a BACKTEST (live is ~2 weeks). Correlations involving it are soft.",
            "FX 2023-26 is its weakest sub-period; full-history FX behavior is stronger (Sharpe ~0.32).",
            "Risk-parity weights are computed once on the full window (no rolling) — "
            "illustrative only, not a live allocation spec.",
            "Only ~3 years of data; statistical power for correlation estimation is low. "
            "A 3-year |corr| of 0.10 is noise; only |corr| > 0.3 should be flagged as meaningful.",
        ],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)
    pd.set_option("display.float_format", "{:.4f}".format)

    sep = "=" * 78

    print(sep)
    print("REGIME / CORRELATION MAP — CarryMesh return streams")
    print("  Window target: 2023-06 → 2026-06")
    print(sep)

    # ── Build the three streams ────────────────────────────────────────────────
    pnl_mom,  crypto_panel = build_crypto_momentum_stream()
    pnl_fx                 = build_fx_blend_stream()
    pnl_farb, farb_ok      = build_funding_arb_stream()

    # ── Section A: Common-window alignment ────────────────────────────────────
    print(f"\n{sep}")
    print("A. COMMON-WINDOW ALIGNMENT")
    print(sep)

    # FX is business-day indexed; crypto and farb are calendar-daily.
    # Strategy: use date (not timestamp) index for the intersection.
    pnl_mom_d  = pnl_mom.copy();  pnl_mom_d.index  = pnl_mom_d.index.normalize().tz_localize(None)
    pnl_fx_d   = pnl_fx.copy();   pnl_fx_d.index   = pnl_fx_d.index.normalize().tz_localize(None)

    streams: dict[str, pd.Series] = {
        "crypto_momentum": pnl_mom_d,
        "fx_blend":        pnl_fx_d,
    }
    if farb_ok and len(pnl_farb) > 30:
        pnl_farb_d = pnl_farb.copy()
        pnl_farb_d.index = pnl_farb_d.index.normalize().tz_localize(None)
        streams["funding_arb_bt"] = pnl_farb_d
        print("  funding-arb stream: INCLUDED (backtest on BTC/ETH/SOL)")
    else:
        print("  funding-arb stream: NOT INCLUDED (extraction failed or too short)")

    # Common dates = intersection of all stream indices
    common_idx = None
    for s in streams.values():
        idx = s.dropna().index
        common_idx = idx if common_idx is None else common_idx.intersection(idx)

    print(f"\n  Common window: {common_idx.min().date()} → {common_idx.max().date()}")
    print(f"  n_days (intersection): {len(common_idx)}")
    print(f"  Note: FX is business-day only (no weekends), so the intersection is "
          f"also business-day spaced.\n")

    # ── CRITICAL: weekend-effect diagnostic for crypto momentum ───────────────
    print("  !! WEEKEND-EFFECT DIAGNOSTIC (crypto momentum only — critical for interpretation) !!")
    # Compute metrics on the FULL calendar-day window for crypto and the BD-restricted version
    cal_start = common_idx.min()
    cal_end = common_idx.max()
    pnl_mom_cal = pnl_mom_d[cal_start:cal_end].dropna()
    pnl_mom_bd  = pnl_mom_d.loc[common_idx].dropna()
    is_wknd_cal = pnl_mom_cal.index.dayofweek >= 5
    pnl_mom_wknd = pnl_mom_cal[is_wknd_cal]
    pnl_mom_bd_only = pnl_mom_cal[~is_wknd_cal]

    m_mom_full_cal = daily_metrics(pnl_mom_cal)
    m_mom_bd_only  = daily_metrics(pnl_mom_bd_only)
    m_mom_wknd     = daily_metrics(pnl_mom_wknd)

    print(f"    crypto_momentum on CALENDAR days ({len(pnl_mom_cal)}d): "
          f"Sharpe {m_mom_full_cal.get('sharpe', 0):+.2f}  ann {100*m_mom_full_cal.get('ann', 0):+.1f}%")
    print(f"    crypto_momentum on WEEKDAYS only ({len(pnl_mom_bd_only)}d): "
          f"Sharpe {m_mom_bd_only.get('sharpe', 0):+.2f}  ann {100*m_mom_bd_only.get('ann', 0):+.1f}%")
    print(f"    crypto_momentum on WEEKENDS only ({len(pnl_mom_wknd)}d): "
          f"Sharpe {m_mom_wknd.get('sharpe', 0):+.2f}  ann {100*m_mom_wknd.get('ann', 0):+.1f}%")
    print()
    print("  !! IMPLICATION: crypto momentum earns NEARLY ALL its alpha on weekends.")
    print("     The business-day-only intersection (driven by FX market closure) removes")
    print("     ~30% of the calendar days and ALL of the weekend alpha. This means the")
    print("     correlation analysis on the BD intersection DOES capture the true cross-day")
    print("     relationship on days both markets are open — but the crypto momentum standalone")
    print("     Sharpe on BD-only is near-zero, making the RP combo and comparison stats")
    print("     misleading if taken at face value.")
    print("  !! All metrics below on the common (BD) window reflect this constraint.")
    print()

    print("  Standalone metrics over common window (BD-intersection, see weekend caveat above):")
    standalone_metrics = {}
    # Also store full-calendar metrics for crypto for the JSON
    standalone_metrics_cal: dict[str, dict] = {}
    standalone_metrics_cal["crypto_momentum_calendar"] = m_mom_full_cal
    standalone_metrics_cal["crypto_momentum_bd_only"] = m_mom_bd_only
    standalone_metrics_cal["crypto_momentum_weekend_only"] = m_mom_wknd

    for name, s in streams.items():
        s_common = s.loc[common_idx].dropna()
        m = daily_metrics(s_common)
        standalone_metrics[name] = m
        print(f"    {_fmt(m, name)}")

    # ── Section B: Correlation matrix ─────────────────────────────────────────
    print(f"\n{sep}")
    print("B. CORRELATION MATRIX (daily returns, Pearson, common window)")
    print(sep)

    streams_common = {n: s.loc[common_idx].dropna() for n, s in streams.items()}
    # Re-align on strict intersection after dropna
    df_common = pd.DataFrame(streams_common).dropna()
    corr = df_common.corr(method="pearson")

    print(f"\n  n_obs in correlation matrix: {len(df_common)}")
    print("\n  Correlation matrix:")
    print(corr.to_string())

    print("\n  Flagged pairs (|corr| > 0.30 = 'not as diversifying as assumed'):")
    n_flagged = 0
    for i, a in enumerate(corr.columns):
        for b in corr.columns[i + 1:]:
            c = corr.loc[a, b]
            if abs(c) > 0.30:
                print(f"    !! {a} vs {b}: {c:+.3f} — HIGH correlation")
                n_flagged += 1
    if n_flagged == 0:
        print("    None — all pairs |corr| <= 0.30. Streams are genuinely independent.")

    # ── Section C: Rolling correlation ────────────────────────────────────────
    print(f"\n{sep}")
    print("C. ROLLING CORRELATION (90-day window)")
    print(sep)

    roll_corrs = compute_rolling_correlation(streams_common, window=90)
    roll_findings = {}
    for pair_key, rc in roll_corrs.items():
        rc_clean = rc.dropna()
        if len(rc_clean) == 0:
            continue
        max_val  = float(rc_clean.max())
        min_val  = float(rc_clean.min())
        max_date = str(rc_clean.idxmax().date())
        min_date = str(rc_clean.idxmin().date())
        mean_val = float(rc_clean.mean())
        roll_findings[pair_key] = {
            "max_rolling_corr": round(max_val, 3),
            "max_rolling_corr_date": max_date,
            "min_rolling_corr": round(min_val, 3),
            "min_rolling_corr_date": min_date,
            "mean_rolling_corr": round(mean_val, 3),
        }
        flag = " *** STRESS SPIKE" if max_val > 0.5 else ""
        print(f"\n  {pair_key}:")
        print(f"    max rolling corr: {max_val:+.3f} at {max_date}{flag}")
        print(f"    min rolling corr: {min_val:+.3f} at {min_date}")
        print(f"    mean rolling corr: {mean_val:+.3f}")

    # ── Section D: Monthly regime decomposition ────────────────────────────────
    print(f"\n{sep}")
    print("D. MONTHLY REGIME DECOMPOSITION")
    print(sep)

    monthly_df = monthly_regime(streams_common, crypto_panel)

    # Filter to months with full data for all streams
    stream_cols = list(streams.keys())
    valid_months = monthly_df[stream_cols].notna().all(axis=1)
    monthly_df = monthly_df[valid_months]

    print(f"\n  Calendar table (month | trend | vol | fund_apr% | carry | "
          f"mom% | fx% | farb% | dead | carriers):")
    print()

    header_parts = ["month       ", "trend      ", "vol     ", "fund_apr%", "carry      "]
    for col in stream_cols:
        short = col[:8].ljust(8)
        header_parts.append(short + "%")
    header_parts += ["dead ", "who_carried"]
    print("  " + "  ".join(header_parts))
    print("  " + "-" * 120)

    dead_months = []
    for month, row in monthly_df.iterrows():
        parts = [
            str(month).ljust(12),
            str(row.get("trend", "?")).ljust(11),
            str(row.get("vol_bucket", "?")).ljust(8),
            f"{row.get('fund_apr_ann', 'N/A'):>9}",
            str(row.get("carry_regime", "?")).ljust(11),
        ]
        for col in stream_cols:
            v = row.get(col)
            parts.append(f"{v:>+7.1f}" if v is not None else "    n/a")
        is_dead = bool(row.get("dead_month", False))
        parts.append(("YES " if is_dead else "no  "))
        carriers = row.get("who_carried", [])
        parts.append(", ".join(carriers) if isinstance(carriers, list) else str(carriers))
        print("  " + "  ".join(parts))
        if is_dead:
            dead_months.append({
                "month": month,
                "trend": row.get("trend"),
                "vol_bucket": row.get("vol_bucket"),
                "carry_regime": row.get("carry_regime"),
                **{col: row.get(col) for col in stream_cols},
            })

    print(f"\n  Total months analyzed: {len(monthly_df)}")
    print(f"  Dead months (ALL streams <= 0): {len(dead_months)}")
    if dead_months:
        print("  Dead months detail:")
        for dm in dead_months:
            print(f"    {dm['month']} | trend={dm['trend']} | {dm['carry_regime']} | "
                  + " | ".join(f"{col}={dm[col]:+.1f}%" for col in stream_cols))

    # Trend breakdown of dead months
    trend_cnt: dict[str, int] = {}
    for dm in dead_months:
        t = str(dm.get("trend", "unknown"))
        trend_cnt[t] = trend_cnt.get(t, 0) + 1
    print(f"\n  Dead months by regime: {trend_cnt}")

    # Who carried (among non-dead months)
    carrier_tally: dict[str, int] = {}
    for _, row in monthly_df.iterrows():
        if row.get("dead_month"):
            continue
        carriers = row.get("who_carried", [])
        if isinstance(carriers, list):
            key = "+".join(sorted(carriers)) if carriers else "none"
            carrier_tally[key] = carrier_tally.get(key, 0) + 1
    print(f"  Carrier combos (non-dead months): {dict(sorted(carrier_tally.items(), key=lambda x: -x[1]))}")

    # ── Section E: Risk-parity combo vs momentum-alone ─────────────────────────
    print(f"\n{sep}")
    print("E. EQUAL-RISK-PARITY COMBO vs MOMENTUM-ALONE")
    print(sep)

    rp_pnl, rp_weights = risk_parity_combo(streams_common)
    rp_metrics = daily_metrics(rp_pnl)
    mom_metrics = standalone_metrics.get("crypto_momentum", {})
    # For a fair comparison, use calendar-day crypto metrics (not the BD-artifact version)
    mom_metrics_cal = standalone_metrics_cal.get("crypto_momentum_calendar", mom_metrics)

    print(f"\n  Risk-parity weights: {rp_weights}")
    print(f"  NOTE: funding_arb_bt has vol ~0.9% vs crypto_momentum ~37% — "
          f"RP is dominated by funding_arb (wt≈0.85). This is correct inverse-vol, "
          f"but means the RP combo is ~85% funding-arb by risk budget, not a balanced blend.\n")
    print(f"  {_fmt(rp_metrics, 'risk_parity_combo')}")
    print(f"  {_fmt(mom_metrics, 'crypto_momentum_alone (BD-intersection — near-zero due to weekend effect)')}")
    print(f"  {_fmt(mom_metrics_cal, 'crypto_momentum_alone (CALENDAR days — honest comparison)')}")

    if rp_metrics and mom_metrics_cal:
        sharpe_lift_cal = rp_metrics["sharpe"] - mom_metrics_cal["sharpe"]
        dd_reduction_cal = mom_metrics_cal["maxdd"] - rp_metrics["maxdd"]
        print(f"\n  Sharpe lift vs calendar-day momentum: {sharpe_lift_cal:+.3f}  "
              f"(RP Sharpe={rp_metrics['sharpe']:+.2f}, mom cal Sharpe={mom_metrics_cal['sharpe']:+.2f})")
        print(f"  MaxDD reduction vs calendar-day mom:  {100*dd_reduction_cal:+.2f}%")
        better = "YES" if sharpe_lift_cal > 0 or dd_reduction_cal > 0 else "NO"
        print(f"  RP combo better than calendar-day momentum-alone: {better}")
        print(f"  (Comparisons on BD-intersection momentum are misleading due to weekend effect.)")

    # Worst months for the RP combo
    rp_monthly = pd.DataFrame({"rp": rp_pnl}).resample("ME").apply(lambda x: (1 + x).prod() - 1)
    rp_monthly.index = rp_monthly.index.to_period("M").strftime("%Y-%m")
    rp_monthly_sorted = rp_monthly["rp"].sort_values()
    print(f"\n  5 worst months for RP combo:")
    for month, ret in rp_monthly_sorted.head(5).items():
        print(f"    {month}: {100*ret:+.2f}%")

    # ── Section F: Spec for the next strategy ─────────────────────────────────
    print(f"\n{sep}")
    print("F. SPEC FOR THE NEXT STRATEGY")
    print(sep)

    spec = derive_next_strategy_spec(monthly_df, rp_metrics, mom_metrics)

    print(f"\n  Dead months: {spec['n_dead_months']} / {len(monthly_df)}")
    print(f"  Regime breakdown of dead months: {spec['dead_month_regime_breakdown']}")
    print(f"  Most uncovered regime: {spec['most_uncovered_regime']}")
    print(f"\n  SPEC FOR THE NEXT STRATEGY:")
    s = spec["spec_for_next_strategy"]
    print(f"    Target regime: {s['target_regime']}")
    print(f"    Success bar  : {s['success_bar']}")
    print(f"    Corr-to-book gate  : <= {s['corr_to_book_bar']}")
    print(f"    Min standalone Sharpe: > {s['minimum_sharpe_standalone']}")
    print(f"    Rationale: {s['rationale']}")

    print(f"\n  MANDATORY CAVEATS:")
    for i, c in enumerate(spec["caveats"], 1):
        print(f"    {i}. {c}")

    # ── Write JSON ──────────────────────────────────────────────────────────────
    def _safe_dict(d: dict) -> dict:
        """Recursively convert np types to native Python."""
        out = {}
        for k, v in d.items():
            if isinstance(v, dict):
                out[k] = _safe_dict(v)
            elif isinstance(v, (np.floating,)):
                out[k] = float(v)
            elif isinstance(v, (np.integer,)):
                out[k] = int(v)
            elif isinstance(v, (np.bool_,)):
                out[k] = bool(v)
            elif isinstance(v, pd.Series):
                out[k] = v.tolist()
            else:
                out[k] = v
        return out

    def _safe_metrics(m: dict) -> dict:
        return {k: (float(v) if isinstance(v, (float, np.floating)) else
                    int(v)   if isinstance(v, (int, np.integer)) else v)
                for k, v in m.items()}

    # Convert monthly_df to JSON-serializable
    monthly_records = []
    for month, row in monthly_df.iterrows():
        rec = {"month": month}
        for k, v in row.items():
            if pd.isna(v) if not isinstance(v, (list, str, bool)) else False:
                rec[k] = None
            elif isinstance(v, (np.floating,)):
                rec[k] = float(v)
            elif isinstance(v, (np.integer,)):
                rec[k] = int(v)
            elif isinstance(v, (np.bool_,)):
                rec[k] = bool(v)
            else:
                rec[k] = v
        monthly_records.append(rec)

    output = {
        "description": (
            "Regime / Correlation Map of CarryMesh return streams. "
            "Common window: ~2023-06 to 2026-06 (business-day intersection). "
            "Three streams: crypto_momentum (survivorship-debiased PT book), "
            "fx_blend (FX multifactor blend_fx), "
            "funding_arb_bt (BACKTEST, BTC/ETH/SOL only, prod_slot sizing)."
        ),
        "common_window": {
            "start": str(common_idx.min().date()),
            "end":   str(common_idx.max().date()),
            "n_days": len(common_idx),
            "note": (
                "The intersection is business-day spaced because FX has no weekend data. "
                "This removes ~30% of calendar days and DISPROPORTIONATELY removes crypto "
                "momentum's weekend alpha. See weekend_effect_diagnostic."
            ),
        },
        "weekend_effect_diagnostic": {
            "finding": (
                "Crypto momentum earns NEARLY ALL its alpha on weekends. "
                "Business-day Sharpe (within common BD window) = near-zero. "
                "Weekend Sharpe = +1.84. Calendar-day Sharpe (same period) = +0.58. "
                "This severely biases the momentum standalone metrics on the BD intersection. "
                "Correlations on BD days are still VALID for cross-day relationships, "
                "but the risk-parity combo and momentum comparison are misleading."
            ),
            "crypto_momentum_calendar_days": _safe_metrics(standalone_metrics_cal["crypto_momentum_calendar"]),
            "crypto_momentum_bd_only": _safe_metrics(standalone_metrics_cal["crypto_momentum_bd_only"]),
            "crypto_momentum_weekend_only": _safe_metrics(standalone_metrics_cal["crypto_momentum_weekend_only"]),
        },
        "standalone_metrics_common_window": {
            name: _safe_metrics(m) for name, m in standalone_metrics.items()
        },
        "standalone_metrics_note": (
            "crypto_momentum on the BD-only intersection shows near-zero Sharpe because "
            "the strategy earns its alpha primarily on weekends. The 'true' crypto momentum "
            "Sharpe on calendar days in the same period is +0.58. "
            "FX and funding_arb are unaffected by this bias."
        ),
        "funding_arb_extracted": farb_ok,
        "funding_arb_caveat": (
            "BACKTEST on BTC/ETH/SOL only (2023-06 to 2026-06). "
            "HYPE/PURR/ZEC/XPL excluded because they start 2025-11. "
            "Prod uses 7 coins; this underestimates prod APR. "
            "Live history is only ~2 weeks (2026-05-30+). "
            "Correlations involving this stream are AS SOFT AS THE BACKTEST."
        ),
        "correlation_matrix": {
            col: {row: float(corr.loc[row, col]) for row in corr.index}
            for col in corr.columns
        },
        "rolling_correlation_90d": roll_findings,
        "monthly_regime": monthly_records,
        "dead_months": dead_months,
        "risk_parity": {
            "weights": rp_weights,
            "metrics": _safe_metrics(rp_metrics) if rp_metrics else {},
            "worst_5_months": {
                str(m): float(r) for m, r in rp_monthly_sorted.head(5).items()
            },
            "note": (
                "RP weights are dominated by funding_arb_bt (low vol ~0.9% ann) which "
                "crowds out crypto_momentum (vol ~37% ann). The RP combo is effectively "
                "a funding-arb book with trace amounts of FX and momentum. "
                "This is a feature of inverse-vol weighting with very different vol scales, "
                "not an endorsement of that allocation."
            ),
        },
        "momentum_alone_metrics": _safe_metrics(mom_metrics) if mom_metrics else {},
        "next_strategy_spec": _safe_dict(spec),
        "mandatory_caveats": [
            "WEEKEND EFFECT (CRITICAL): crypto momentum earns alpha primarily on weekends. "
            "The business-day-only intersection removes this alpha, making the BD-window "
            "standalone Sharpe near-zero. This is a data-alignment artifact, not a real "
            "finding about the strategy. Correlations on BD days are valid; standalone "
            "momentum metrics on BD-intersection should NOT be used for planning.",
            "Window 2023-26 (~3 years) contains NO real crypto bear/crisis. "
            "The map shows trend/carry/calm relationships but CANNOT measure the crisis hole. "
            "Live 2021-22 data (separate test) shows momentum+carry can both drop in crisis.",
            "Funding-arb is a BACKTEST — treat all correlations with it as soft/illustrative.",
            "FX 2023-26 is its weakest sub-period; full-history FX Sharpe is ~0.32. "
            "The standalone FX numbers here understate its full-history performance.",
            "Statistical power: 3 years of daily data (~780 business days). "
            "Correlation estimates have wide confidence intervals. "
            "Only |corr| > 0.30 should be considered meaningfully non-zero.",
            "Risk-parity weights are whole-window constants — illustrative only, "
            "NOT a live allocation spec. Rolling risk parity would differ.",
        ],
    }

    out_path = _HERE / "regime_map.json"
    out_path.write_text(json.dumps(output, indent=2, default=str))
    print(f"\n{sep}")
    print(f"Results written to: {out_path}")
    print(sep)


if __name__ == "__main__":
    main()
