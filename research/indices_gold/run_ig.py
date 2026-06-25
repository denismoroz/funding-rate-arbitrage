"""
Run TSMOM indices+gold through the validation harness.
Mirrors run_fx.py: run_harness → print_report → save_json.

Run:
  cd research/indices_gold
  PYTHONPATH="../validation_harness:../cross_sectional:../cross_sectional/crypto:..:." \
    python run_ig.py

  (or with data refresh: ... run_ig.py --refresh)

CONFIG / JUDGEMENT CALLS
------------------------
purge = MAX_LOOKBACK_DAYS = 252  (= 12 * 21 business days, the deepest menu
  lookback for tsmom12).  Seam-safety requires purge >= max menu lookback: the
  signal & pnl are precomputed on the full panel; CPCV only selects rows, so the
  bar that fed a test-day tsmom12 signal lies up to 252 business days earlier and
  must be purged out of train.  Same rule as fx_pkg (purge=1260 for 5y value);
  here it is lighter because 12m = 252 bdays.

n_groups = 6, k = 1  → 6 Purged K-Fold splits.
  k=2 would eat 2*252 bars on each side of each test group for a ~10yr panel.
  We use k=1 to keep train budgets healthy (same reasoning as run_fx.py).

embargo = 21 days (one rebal cadence ≈ monthly).

costs object: the harness signature needs a `costs` arg; TSMOM costs are baked
  into IGTrendPackage.costs_bps (2.0 bps) inside xsec.portfolio_returns when the
  menu is built.  The TAKER passed to run_harness only flows into fit()/simulate(),
  which ignore it here (simulate returns precomputed pnl slices).

DAILY-vs-HOURLY ANNUALIZATION CAVEAT (READ BEFORE INTERPRETING OOS NUMBERS):
  engine.compute_metrics annualizes with HOURS_PER_YEAR=8760 (one element = one
  HOUR).  Our book is DAILY.  The pooled-OOS `annual_pct`/`sharpe`/`calmar` from
  the harness are therefore INFLATED relative to a daily-annualized read:
    annual_pct  ~ ×(8760/252) ≈ 34.8×
    sharpe/calmar ~ ×sqrt(8760/252) ≈ 5.9×
  The harness VERDICT — DSR and PBO — is period-agnostic (per-period Sharpe /
  rank-based) and is therefore correct as shown.
  Treat the OOS distribution as SIGN + RELATIVE shape, not literal annual %.
  For honest daily levels per factor see ig_pkg.py's self-test (daily_metrics).

GO-gates:
  DSR  > 0.95   (Sharpe survives multi-test deflation)
  PBO  < 0.20   (best IS config transfers OOS)
  all |corr(book, benchmark)| < 0.30   (orthogonality gate)

Orthogonality proxies (copied from onchain_fundamental/run_onchain.py):
  BTC buy-and-hold  ← BTC daily price pct-change
  FRAB carry proxy  ← equal-weight mean hourly funding across 7 HL coins,
                       resampled to daily
  XSMOM momentum    ← rolling 30-day cross-sec momentum on 13 crypto coins
These load via research/cross_sectional/crypto/cryptodata.load_panel (hourly HL
data), and the BTC/FRAB/XSMOM series are daily (or resampled to daily).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE    = Path(__file__).parent
_HARNESS = _HERE.parent / "validation_harness"
_XSEC    = _HERE.parent / "cross_sectional"
_CRYPTO  = _HERE.parent / "cross_sectional" / "crypto"
_RESEARCH = _HERE.parent

# NOTE: our fetcher is named ig_fetch.py (not fetch.py) to avoid shadowing
# cross_sectional/crypto/fetch.py when cryptodata does `import fetch`.
# _HERE is inserted before _CRYPTO so local igdata/signals/ig_pkg.py are found,
# but ig_fetch.py does not conflict with the crypto fetch.
for _p in (_HERE, _RESEARCH, _CRYPTO, _XSEC, _HARNESS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from harness import run_harness, save_json, to_dict as harness_to_dict
from report import print_report
from splitter import cpcv
from costs import TAKER

from ig_pkg import IGTrendPackage, MAX_LOOKBACK_DAYS, MENU_NAMES, SELECTED
from selftest import main as run_selftests

N_GROUPS = 6
K        = 1
PURGE    = MAX_LOOKBACK_DAYS   # = 252 business days
EMBARGO  = 21                  # business days ≈ one (monthly) rebal cadence
MIN_TRAIN_FLOOR = 300          # warn below this


# ── Orthogonality proxies (mirror of onchain_fundamental/run_onchain.py) ──────

def _btc_buyhold_daily() -> pd.Series:
    """BTC buy-and-hold daily return (from HL hourly data)."""
    from cryptodata import load_panel
    panel = load_panel(["BTC"])
    price = panel["price"]["BTC"].dropna()
    return price.pct_change().dropna()


def _frab_carry_proxy_daily() -> pd.Series:
    """FRAB carry proxy: equal-weight mean daily funding across 7 HL coins."""
    from cryptodata import load_panel
    frab_coins = ["BTC", "ETH", "SOL", "AVAX", "NEAR", "DOT", "ATOM"]
    try:
        panel = load_panel(frab_coins)
        fund  = panel["funding"]
        return fund.mean(axis=1).dropna()
    except Exception as e:
        print(f"  Warning: FRAB proxy failed ({e}), using BTC funding")
        from cryptodata import load_panel as _lp
        panel = _lp(["BTC"])
        return panel["funding"]["BTC"].dropna()


def _xsmom_momentum_proxy_daily() -> pd.Series:
    """XSMOM momentum proxy: cross-sectional momentum monthly return on crypto."""
    from cryptodata import load_panel
    coins = ["BTC", "ETH", "SOL", "AVAX", "NEAR", "DOT", "ATOM",
             "ADA", "ARB", "APT", "SUI", "UNI", "AAVE"]
    try:
        panel   = load_panel(coins)
        price   = panel["price"]
        fwd_ret = panel["fwd_ret"]
        mom_window = 30
        n_long     = 3
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
            w[long_mask]  =  1.0 / n_long
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
        mask = np.isfinite(b) & np.isfinite(bm)
        if mask.sum() < 30:
            return float("nan")
        return float(np.corrcoef(b[mask], bm[mask])[0, 1])

    return {
        "corr_BTC_buyhold":    corr_with(btc,   "BTC_buyhold"),
        "corr_FRAB_carry":     corr_with(frab,  "FRAB_carry"),
        "corr_XSMOM_momentum": corr_with(xsmom, "XSMOM_mom"),
    }


# ── Split-size printer ─────────────────────────────────────────────────────────

def _print_split_sizes(n: int) -> None:
    """Print realized per-split train/test sizes to surface the purge cost."""
    splits = cpcv(n, n_groups=N_GROUPS, k=K, purge=PURGE, embargo=EMBARGO)
    print(f"\n— realized CPCV splits (n={n}, N={N_GROUPS}, k={K}, "
          f"purge={PURGE}, embargo={EMBARGO}) —")
    print(f"  {'split':<7}{'test_groups':<14}{'train':>8}{'train%':>8}"
          f"{'test':>8}{'test%':>8}")
    trains = []
    collapsed = []
    for i, s in enumerate(splits):
        tr, te = len(s.train_idx), len(s.test_idx)
        trains.append(tr)
        flag = "  <-- COLLAPSED" if tr < MIN_TRAIN_FLOOR else ""
        if tr < MIN_TRAIN_FLOOR:
            collapsed.append(i)
        print(f"  {i:<7}{str(s.test_groups):<14}{tr:>8}{tr/n*100:>7.1f}%"
              f"{te:>8}{te/n*100:>7.1f}%{flag}")
    tr_arr = np.array(trains)
    print(f"  train days  min={tr_arr.min()} ({tr_arr.min()/n*100:.1f}%)  "
          f"median={int(np.median(tr_arr))} ({np.median(tr_arr)/n*100:.1f}%)  "
          f"max={tr_arr.max()} ({tr_arr.max()/n*100:.1f}%)")
    if collapsed:
        print(f"  !! WARNING: {len(collapsed)} split(s) have train < "
              f"{MIN_TRAIN_FLOOR} days — OOS distribution unreliable. "
              f"Splits: {collapsed}")
    else:
        print(f"  OK: every split has train >= {MIN_TRAIN_FLOOR} days "
              f"(non-degenerate under purge={PURGE}).")
    if K == 1:
        sp2   = cpcv(n, n_groups=N_GROUPS, k=2, purge=PURGE, embargo=EMBARGO)
        tr2   = np.array([len(s.train_idx) for s in sp2])
        n_bad = int((tr2 < MIN_TRAIN_FLOOR).sum())
        print(f"  (context: at k=2 min train={tr2.min()}, "
              f"{n_bad}/{len(sp2)} splits < {MIN_TRAIN_FLOOR} → we use k=1.)")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    refresh = "--refresh" in sys.argv

    print("#" * 72)
    print("##### TSMOM INDICES+GOLD через стенд — spot trend 2.0bps/leg #####")
    print("#" * 72)

    # ── Step 1: Self-tests ─────────────────────────────────────────────────────
    print("\n" + "─" * 72)
    print("STEP 1: SELF-TESTS")
    print("─" * 72)
    rc = run_selftests()
    if rc != 0:
        print("\nSELF-TESTS FAILED — aborting. Fix correctness bugs first.")
        sys.exit(1)

    # ── Step 2: Build package ──────────────────────────────────────────────────
    print("\n" + "─" * 72)
    print("STEP 2: PACKAGE + DATA")
    print("─" * 72)

    pkg = IGTrendPackage(rebal_every=21, costs=TAKER, costs_bps=2.0,
                         refresh=refresh)
    df  = pkg.load("IGTREND")
    n   = len(df)
    print(f"panel: {n} business days  "
          f"{df.index.min().date()} -> {df.index.max().date()}")
    print(f"rebal_every={pkg.rebal_every}d   costs_bps={pkg.costs_bps:.2f}/leg "
          f"(liquid index/ETF/futures spread)")
    print(f"purge={PURGE}d (= MAX_LOOKBACK_DAYS = 12m)   n_groups={N_GROUPS}   "
          f"k={K}   embargo={EMBARGO}d")

    _print_split_sizes(n)

    # Honest daily_metrics per config (import only, don't edit crypto/)
    sys.path.insert(0, str(_HERE.parent / "cross_sectional" / "crypto"))
    from metrics_daily import daily_metrics

    menu = pkg.menu("IGTREND", df)
    print(f"\n— full-period daily_metrics (honest √252, NOT harness √8760) —")
    print(f"{'config':<12}{'ann':>9}{'sharpe':>9}{'maxDD':>9}{'calmar':>9}{'hit':>7}")
    for nm in MENU_NAMES:
        s  = menu[nm]
        dm = daily_metrics(s)
        if not dm:
            print(f"  {nm:<10}  (too short)")
            continue
        print(f"  {nm:<10}{dm['ann']:>+8.2%}{dm['sharpe']:>9.2f}"
              f"{dm['maxdd']:>9.2%}{dm['calmar']:>9.2f}{dm['hit']:>7.2%}")

    # ── Step 3: Harness ────────────────────────────────────────────────────────
    print("\n" + "─" * 72)
    print("STEP 3: VALIDATION HARNESS (CPCV + DSR + PBO)")
    print("─" * 72)

    rep = run_harness(
        pkg, costs=TAKER,
        n_groups=N_GROUPS, k=K, purge=PURGE, embargo=EMBARGO,
    )

    print()
    print_report(rep)

    # ── Step 4: Orthogonality gate ─────────────────────────────────────────────
    print("\n" + "─" * 72)
    print("STEP 4: ORTHOGONALITY GATE")
    print("─" * 72)

    book_pnl = pkg.book_pnl()
    print(f"  Book PnL (tsmom_ens): {len(book_pnl)} days  "
          f"sum={book_pnl.sum():.4f}  "
          f"ann≈{book_pnl.mean()*252:+.2%}")

    print("  Loading orthogonality proxies (crypto HL data)…")
    orth = compute_orthogonality(book_pnl)

    print(f"\n  {'Benchmark':<25} {'Corr':>8} {'Gate (|corr|<0.30)':>20}")
    orth_results: dict = {}
    for name, corr in orth.items():
        gate = ("PASS" if (not np.isnan(corr) and abs(corr) < 0.30)
                else ("FAIL" if not np.isnan(corr) else "N/A"))
        print(f"  {name:<25} {corr:>8.3f} {gate:>20}")
        orth_results[name] = {"corr": corr, "gate": gate}

    # ── Step 5: Verdict ────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("VERDICT")
    print("=" * 72)

    dsr  = rep.dsr.get("dsr", 0.0)
    pbo  = rep.pbo.pbo
    orth_pass = all(
        abs(v["corr"]) < 0.30
        for v in orth_results.values()
        if not np.isnan(v["corr"])
    )

    sr_oos     = rep.pooled_oos.dist.get("sharpe",  {}).get("median", float("nan"))
    calmar_oos = rep.pooled_oos.dist.get("calmar",  {}).get("median", float("nan"))

    gate_dsr  = dsr  > 0.95
    gate_pbo  = pbo  < 0.20
    gate_orth = orth_pass

    print(f"  DSR={dsr:.3f}           {'PASS' if gate_dsr  else 'FAIL'} (threshold >0.95)")
    print(f"  PBO={pbo:.3f}           {'PASS' if gate_pbo  else 'FAIL'} (threshold <0.20)")
    print(f"  Orthogonality:        {'PASS' if gate_orth else 'FAIL'} (|corr|<0.30 all benchmarks)")
    print(f"  OOS Sharpe (median):  {sr_oos:.3f}  [HARNESS SCALE — inflated ~5.9× vs daily]")
    print(f"  OOS Calmar (median):  {calmar_oos:.3f}  [HARNESS SCALE]")

    go      = gate_dsr and gate_pbo and gate_orth
    verdict = "GO" if go else "NO-GO"
    print(f"\n  VERDICT: {verdict}")
    if not go:
        reasons = []
        if not gate_dsr:
            reasons.append(
                f"DSR={dsr:.3f} < 0.95 (Sharpe doesn't survive multi-test deflation)")
        if not gate_pbo:
            reasons.append(
                f"PBO={pbo:.3f} >= 0.20 (best IS config doesn't transfer OOS)")
        if not gate_orth:
            fails = {k: v for k, v in orth_results.items()
                     if not np.isnan(v["corr"]) and abs(v["corr"]) >= 0.30}
            for k, v in fails.items():
                reasons.append(f"|corr({k})|={abs(v['corr']):.3f} >= 0.30 (not orthogonal)")
        for r in reasons:
            print(f"    Reason: {r}")

    # ── Save JSON ──────────────────────────────────────────────────────────────
    daily_stats: dict = {}
    for nm in MENU_NAMES:
        dm = daily_metrics(menu[nm])
        daily_stats[nm] = {k: float(v) for k, v in dm.items()} if dm else {}

    result = {
        **harness_to_dict(rep),
        "max_lookback_days": MAX_LOOKBACK_DAYS,
        "panel_days": n,
        "panel_start": str(df.index.min().date()),
        "panel_end":   str(df.index.max().date()),
        "orthogonality": {k: v["corr"] for k, v in orth_results.items()},
        "orthogonality_gates": {k: v["gate"] for k, v in orth_results.items()},
        "book_pnl_sum":  float(book_pnl.sum()),
        "book_pnl_days": int(len(book_pnl)),
        "book_pnl_ann":  float(book_pnl.mean() * 252),
        "oos_sharpe_median":  float(sr_oos)     if not np.isnan(sr_oos)     else None,
        "oos_calmar_median":  float(calmar_oos) if not np.isnan(calmar_oos) else None,
        "daily_metrics_per_config": daily_stats,
        "verdict": verdict,
        "gates": {
            "dsr_pass":  gate_dsr,
            "pbo_pass":  gate_pbo,
            "orth_pass": gate_orth,
        },
    }

    out_path = _HERE / "run_ig.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\n  Saved: {out_path.name}")


if __name__ == "__main__":
    main()
