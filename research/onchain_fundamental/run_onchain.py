"""
Main runner: onchain_fundamental hypothesis validation.

Runs:
  1. Self-tests (must all pass before harness).
  2. Fee panel coverage report (coins × days, per-date valid count).
  3. Validation harness (CPCV + DSR + PBO).
  4. Orthogonality gate (vs XSMOM-proxy, FRAB-carry-proxy, BTC buy&hold).
  5. Verdict: GO / NO-GO.

Saves run_onchain.json with all results.

Run from research/onchain_fundamental/:
  PYTHONPATH=../.. /Users/d/prj/funding-rate-arbitrage/.venv/bin/python run_onchain.py

Or with data refresh:
  ... run_onchain.py --refresh
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).parent
_HARNESS = _HERE.parent / "validation_harness"
_CRYPTO  = _HERE.parent / "cross_sectional" / "crypto"
_XSEC    = _HERE.parent / "cross_sectional"
_RESEARCH = _HERE.parent

for _p in (_HARNESS, _CRYPTO, _XSEC, _RESEARCH, _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from harness import run_harness, to_dict as harness_to_dict
from report import print_report
from cryptodata import load_panel

from selftest import main as run_selftests
from fees_data import coverage_report, ALL_COINS
from onchain_pkg import OnchainFundamentalPackage, MAX_LOOKBACK_DAYS


# ── Orthogonality proxies (reused from pairs_cointegration pattern) ───────────

def _btc_buyhold_daily() -> pd.Series:
    """BTC buy&hold daily return."""
    panel = load_panel(["BTC"])
    price = panel["price"]["BTC"].dropna()
    return price.pct_change().dropna()


def _frab_carry_proxy_daily() -> pd.Series:
    """FRAB carry proxy: equal-weight mean daily funding across 7 HL coins."""
    frab_coins = ["BTC", "ETH", "SOL", "AVAX", "NEAR", "DOT", "ATOM"]
    try:
        panel = load_panel(frab_coins)
        fund = panel["funding"]
        return fund.mean(axis=1).dropna()
    except Exception as e:
        print(f"  Warning: FRAB proxy failed ({e}), using BTC funding")
        panel = load_panel(["BTC"])
        return panel["funding"]["BTC"].dropna()


def _xsmom_momentum_proxy_daily() -> pd.Series:
    """XSMOM momentum proxy: cross-sectional momentum monthly return."""
    coins = ["BTC", "ETH", "SOL", "AVAX", "NEAR", "DOT", "ATOM",
             "ADA", "ARB", "APT", "SUI", "UNI", "AAVE"]
    try:
        panel = load_panel(coins)
        price = panel["price"]
        fwd_ret = panel["fwd_ret"]
        mom_window = 30
        n_long = 3
        pnl_rows: list[pd.Series] = []
        for t in range(mom_window, len(price) - 1):
            past_ret = (price.iloc[t] / price.iloc[t - mom_window] - 1.0).dropna()
            if len(past_ret) < 6:
                pnl_rows.append(pd.Series([0.0], index=[price.index[t]]))
                continue
            ranked = past_ret.rank()
            n = len(ranked)
            long_mask  = ranked >= (n - n_long + 1)
            short_mask = ranked <= n_long
            w = pd.Series(0.0, index=past_ret.index)
            w[long_mask]  = 1.0 / n_long
            w[short_mask] = -1.0 / n_long
            fr = fwd_ret.iloc[t].dropna()
            pnl_rows.append(pd.Series([(w * fr).sum()], index=[price.index[t]]))
        if not pnl_rows:
            return pd.Series(dtype=float)
        return pd.concat(pnl_rows).rename("xsmom")
    except Exception as e:
        print(f"  Warning: XSMOM proxy failed ({e})")
        return pd.Series(dtype=float)


def compute_orthogonality(book_pnl: pd.Series) -> dict[str, float]:
    """Correlate book_pnl to three benchmarks."""
    btc   = _btc_buyhold_daily()
    frab  = _frab_carry_proxy_daily()
    xsmom = _xsmom_momentum_proxy_daily()

    def corr_with(bench: pd.Series, name: str) -> float:
        if bench.empty:
            print(f"  Warning: {name} proxy is empty")
            return float("nan")
        common = book_pnl.index.intersection(bench.index)
        if len(common) < 30:
            print(f"  Warning: {name} overlap < 30 days ({len(common)})")
            return float("nan")
        b  = book_pnl.loc[common].values
        bm = bench.loc[common].values
        # Drop NaNs
        mask = np.isfinite(b) & np.isfinite(bm)
        if mask.sum() < 30:
            return float("nan")
        return float(np.corrcoef(b[mask], bm[mask])[0, 1])

    return {
        "corr_BTC_buyhold":    corr_with(btc,   "BTC_buyhold"),
        "corr_FRAB_carry":     corr_with(frab,  "FRAB_carry"),
        "corr_XSMOM_momentum": corr_with(xsmom, "XSMOM_mom"),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    refresh = "--refresh" in sys.argv
    print("=" * 72)
    print("ONCHAIN FUNDAMENTAL MOMENTUM — HYPOTHESIS VALIDATION")
    print("=" * 72)
    print(f"  Universe: {len(ALL_COINS)} coins ({ALL_COINS})")
    print(f"  MAX_LOOKBACK_DAYS: {MAX_LOOKBACK_DAYS}  (purge for CPCV)")
    print(f"  refresh={refresh}")

    # ── Step 1: Self-tests ────────────────────────────────────────────────────
    print("\n" + "─" * 72)
    print("STEP 1: SELF-TESTS")
    print("─" * 72)
    rc = run_selftests()
    if rc != 0:
        print("\nSELF-TESTS FAILED — aborting. Fix correctness bugs first.")
        sys.exit(1)

    # ── Step 2: Build package + coverage ─────────────────────────────────────
    print("\n" + "─" * 72)
    print("STEP 2: DATA COVERAGE")
    print("─" * 72)
    pkg = OnchainFundamentalPackage(refresh=refresh)
    pkg._build_menu()  # trigger data load

    fee_panel = pkg.fee_panel_used()
    common_coins = pkg.common_coins_used()
    coverage_report(fee_panel)

    valid_count = pkg.per_date_valid_count()
    print(f"\nValid coins in signal panel (post-price-alignment):")
    for yr in sorted(valid_count.index.year.unique()):
        mask = valid_count.index.year == yr
        vc = valid_count[mask]
        print(f"  {yr}: median={vc.median():.0f}  min={vc.min():.0f}  max={vc.max():.0f}")

    # ── Step 3: Harness ───────────────────────────────────────────────────────
    print("\n" + "─" * 72)
    print("STEP 3: VALIDATION HARNESS (CPCV + DSR + PBO)")
    print("─" * 72)
    print(f"  n_groups=6  k=2  purge={MAX_LOOKBACK_DAYS}d  embargo=1d  S=16")

    rep = run_harness(
        pkg,
        n_groups=6,
        k=2,
        purge=MAX_LOOKBACK_DAYS,  # 2*90 = 180 days (2*max lookback for growth window)
        embargo=1,                 # daily data → 1-day embargo
        S=16,                      # PBO splits
    )
    print_report(rep)

    # ── Step 4: Orthogonality gate ────────────────────────────────────────────
    print("\n" + "─" * 72)
    print("STEP 4: ORTHOGONALITY GATE")
    print("─" * 72)
    book_pnl = pkg.book_pnl()
    print(f"  Book PnL: {len(book_pnl)} days  "
          f"sum={book_pnl.sum():.4f}  "
          f"ann≈{book_pnl.mean()*252:+.2%}")

    orth = compute_orthogonality(book_pnl)
    print(f"\n  {'Benchmark':<25} {'Corr':>8} {'Gate (|corr|<0.3)':>18}")
    orth_results = {}
    for name, corr in orth.items():
        gate = "PASS" if (not np.isnan(corr) and abs(corr) < 0.30) else \
               ("FAIL" if not np.isnan(corr) else "N/A")
        print(f"  {name:<25} {corr:>8.3f} {gate:>18}")
        orth_results[name] = {"corr": corr, "gate": gate}

    # ── Step 5: Verdict ───────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("VERDICT")
    print("=" * 72)

    dsr = rep.dsr.get("dsr", 0.0)
    pbo = rep.pbo.pbo
    orth_pass = all(
        abs(v) < 0.30
        for v in orth.values()
        if not np.isnan(v)
    )

    sr_oos = rep.pooled_oos.dist.get("sharpe", {}).get("median", float("nan"))
    calmar_oos = rep.pooled_oos.dist.get("calmar", {}).get("median", float("nan"))

    gate_dsr  = dsr > 0.95
    gate_pbo  = pbo < 0.20
    gate_orth = orth_pass

    print(f"  DSR={dsr:.3f}          {'PASS' if gate_dsr else 'FAIL'} (threshold >0.95)")
    print(f"  PBO={pbo:.3f}          {'PASS' if gate_pbo else 'FAIL'} (threshold <0.20)")
    print(f"  Orthogonality:       {'PASS' if gate_orth else 'FAIL'} (|corr|<0.30 all benchmarks)")
    print(f"  OOS Sharpe (median): {sr_oos:.3f}")
    print(f"  OOS Calmar (median): {calmar_oos:.3f}")

    go = gate_dsr and gate_pbo and gate_orth
    verdict = "GO" if go else "NO-GO"
    print(f"\n  VERDICT: {verdict}")
    if not go:
        reasons = []
        if not gate_dsr:
            reasons.append(f"DSR={dsr:.3f} < 0.95 (Sharpe doesn't survive multi-test deflation)")
        if not gate_pbo:
            reasons.append(f"PBO={pbo:.3f} >= 0.20 (best IS config doesn't transfer OOS)")
        if not gate_orth:
            fails = {k: v for k, v in orth.items() if not np.isnan(v) and abs(v) >= 0.30}
            for k, v in fails.items():
                reasons.append(f"|corr({k})|={abs(v):.3f} >= 0.30 (not orthogonal)")
        for r in reasons:
            print(f"    Reason: {r}")

    # ── Save JSON ─────────────────────────────────────────────────────────────
    result = {
        **harness_to_dict(rep),
        "max_lookback_days": MAX_LOOKBACK_DAYS,
        "common_coins": common_coins,
        "coverage": {
            "n_coins": len(common_coins),
            "n_days": len(fee_panel),
            "date_start": str(fee_panel.index.min().date()),
            "date_end": str(fee_panel.index.max().date()),
            "per_year_median_valid_coins": {
                str(yr): float(valid_count[valid_count.index.year == yr].median())
                for yr in sorted(valid_count.index.year.unique())
            },
        },
        "orthogonality": orth,
        "book_pnl_sum": float(book_pnl.sum()),
        "book_pnl_days": int(len(book_pnl)),
        "book_pnl_ann": float(book_pnl.mean() * 252),
        "oos_sharpe_median": float(sr_oos) if not np.isnan(sr_oos) else None,
        "oos_calmar_median": float(calmar_oos) if not np.isnan(calmar_oos) else None,
        "verdict": verdict,
        "gates": {
            "dsr_pass": gate_dsr,
            "pbo_pass": gate_pbo,
            "orth_pass": gate_orth,
        },
    }

    out_path = _HERE / "run_onchain.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\n  Saved: {out_path}")


if __name__ == "__main__":
    main()
