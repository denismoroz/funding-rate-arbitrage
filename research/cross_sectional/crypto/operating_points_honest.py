"""
operating_points_honest.py — Operating points on the HONEST (survivorship-debiased)
point-in-time book, with bear-regime drawdown pinned by vol-equivalence cross-walk.

Replaces operating_points.py which used the inflated frozen-survivor book (ann ~50%).
This script uses the de-biased PT book (ann ~29.5%, Sharpe ~0.76) from survivorship.py.

METHODOLOGY
===========

Step 1 — rebuild the two full-tilt net-pnl series
  - HONEST book: survivorship.build_pt_panel(frozen34 ∪ fetched_dead_coins) via the
    SAME coin list stored in survivorship.json, then survivorship.run_book(). Verified
    against survivorship.json (Sharpe≈0.757, ann≈0.2946, vol≈0.3894, maxdd≈0.2778).
  - BEAR book: bear_regime.build_bear_panel(available_coins) with identical ensemble
    wiring (lookbacks=(14,21,30,45,60), costs_bps=8.5, rebal_every=7,
    accrual=-funding.shift(-1)). Verified against bear_regime.json
    (Sharpe≈1.514, vol≈0.519, maxdd≈0.401).

Step 2 — operating points at target returns
  For each target T in {0.10, 0.14, 0.15, 0.20, 0.25}:
    k_honest = T / honest_full_tilt_ann
    Scaled honest pnl = k_honest * honest_pnl → NORMAL-regime metrics.
    target_vol = k_honest * honest_full_tilt_vol  (the risk budget).

Step 3 — bear drawdown at the SAME risk budget
  Cross-walk by volatility (equal risk budget), NOT by k-fraction, because the two
  universes (HL 2023-26 vs Binance 2021-22) differ; vol-equivalence is the natural
  operational bridge:
    k_bear = target_vol / bear_full_tilt_vol
    Scaled bear pnl = k_bear * bear_pnl → BEAR-regime metrics.
  maxDD is computed on the ACTUALLY-SCALED compounded equity curve
  (1 + k*r).cumprod(), NOT as a linear k×maxDD approximation (compounding is
  non-linear at higher k; the approximation overstates at large drawdowns).
  ALSO reports 2022-only (calendar year) bear maxDD at same k_bear.

Step 4 — print table + write operating_points_honest.json

CAVEATS (also in JSON)
  - The bear cross-walk equates RISK BUDGET (annualized vol) across two different
    universes (20 Binance coins 2021-22 vs the HL-era point-in-time book). This is
    the right operational equivalence but NOT a same-universe replay.
  - maxDD is one path from limited history; the true forward drawdown distribution is
    wider. These are planning anchors, not guarantees.
  - Sharpe ~0.76 is itself a debiased point estimate; do not over-trust the digits.
  - 22 extra dead-coin candidates had no Binance data and could NOT be included in
    the PT book; the true survivorship discount may be larger.

Run:
  cd research/cross_sectional/crypto
  PYTHONPATH=<repo>/research:<repo>/validation_harness:<repo>/research/cross_sectional:<this dir> \\
    python operating_points_honest.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import bear_fetch
import bear_regime
import signals
import survivorship
import xsec
from metrics_daily import daily_metrics

_HERE = Path(__file__).parent

# ── Tolerances for anchor verification ────────────────────────────────────────
_TOL = 0.01          # absolute tolerance for Sharpe/ann/vol/maxdd checks (1 ppt)

# ── Target annual returns ──────────────────────────────────────────────────────
TARGETS = [0.10, 0.14, 0.15, 0.20, 0.25]

PPY = 365   # match metrics_daily


# ── Helper: worst-week from a pnl series ──────────────────────────────────────

def _worst_week_pct(pnl: pd.Series) -> float:
    """Worst 7-day rolling compounded return (as fraction, negative = loss)."""
    eq = (1.0 + pnl.dropna()).cumprod()
    if len(eq) < 8:
        return float("nan")
    worst = 0.0
    vals = eq.values
    for i in range(7, len(vals)):
        ret = vals[i] / vals[i - 7] - 1.0
        if ret < worst:
            worst = ret
    return worst


def _maxdd_scaled_compounded(pnl: pd.Series, k: float) -> float:
    """MaxDD on the COMPOUNDED equity curve (1 + k*r).cumprod().

    Uses exact compounding, NOT the linear approximation k * maxdd.
    This is important at higher k where compounding is non-linear.
    """
    r = pnl.dropna().values
    if len(r) < 2:
        return float("nan")
    eq = np.cumprod(1.0 + k * r)
    dd = 1.0 - eq / np.maximum.accumulate(eq)
    return float(dd.max())


def _metrics_scaled_compounded(pnl: pd.Series, k: float) -> dict:
    """daily_metrics on k * pnl. Uses standard daily_metrics (which already uses
    cumprod for maxDD internally). This is correct — daily_metrics(pnl * k) gives
    maxDD on (1 + k*r).cumprod() exactly because it calls np.cumprod(1 + r_scaled).
    """
    return daily_metrics(pnl * k)


# ── Step 1: Rebuild honest PT series ──────────────────────────────────────────

def rebuild_honest_pnl() -> pd.Series:
    """Rebuild the survivorship-debiased PT book pnl.

    Uses the EXACT same coin list that survivorship.py committed to survivorship.json
    (frozen 34 survivors + the dead/delisted coins that were successfully fetched).
    Then calls survivorship.run_book() with the SAME hyperparams.
    """
    surv_json = json.loads((_HERE / "survivorship.json").read_text())
    all_pt_coins = sorted(
        set(surv_json["frozen_survivor_coins"])
        | set(surv_json["extra_dead_coins_included"])
    )
    print(f"  PT universe: {len(all_pt_coins)} coins "
          f"({len(surv_json['frozen_survivor_coins'])} survivors + "
          f"{len(surv_json['extra_dead_coins_included'])} dead/delisted)")
    panel = survivorship.build_pt_panel(all_pt_coins)
    pnl = survivorship.run_book(panel)
    return pnl.dropna()


def verify_honest_anchor(pnl: pd.Series, ref: dict) -> None:
    """Assert the reproduced honest metrics match the committed JSON within _TOL."""
    m = daily_metrics(pnl)
    checks = [
        ("sharpe",  m["sharpe"],  ref["sharpe"],  _TOL),
        ("ann",     m["ann"],     ref["ann"],      _TOL),
        ("vol_ann", m["vol_ann"], ref["vol_ann"],  _TOL),
        ("maxdd",   m["maxdd"],   ref["maxdd"],    _TOL),
    ]
    for name, got, want, tol in checks:
        diff = abs(got - want)
        if diff > tol:
            raise AssertionError(
                f"HONEST ANCHOR MISMATCH: {name} got={got:.4f} want={want:.4f} "
                f"diff={diff:.4f} > tol={tol:.4f}. "
                f"Reproduced series does not match survivorship.json — STOPPING."
            )
    print(f"  Honest anchor OK: Sharpe={m['sharpe']:.4f} ann={100*m['ann']:.2f}% "
          f"vol={100*m['vol_ann']:.2f}% maxDD={100*m['maxdd']:.2f}%")


# ── Step 1b: Rebuild bear series ───────────────────────────────────────────────

def rebuild_bear_pnl() -> tuple[pd.Series, pd.Series]:
    """Rebuild the bear-regime pnl series, full (2021-22) and 2022-only.

    Mirrors bear_regime.py's __main__ wiring exactly:
      - build_bear_panel(available coins from BEAR_BASKET)
      - momentum_ensemble → rank_to_weights → portfolio_returns with accrual
      - trim to warmup_end (first date with >=2 valid coins)
    Returns (pnl_full, pnl_2022).
    """
    available = []
    for coin in bear_fetch.BEAR_BASKET:
        pr = bear_regime._bear_daily_price(coin)
        if pr.dropna().__len__() >= max(bear_regime.LOOKBACKS) + 10:
            available.append(coin)

    panel = bear_regime.build_bear_panel(available)
    fwd_ret = panel["fwd_ret"]
    funding = panel["funding"]

    score = signals.momentum_ensemble(panel, lookbacks=bear_regime.LOOKBACKS)
    weights = xsec.rank_to_weights(score)

    n_valid = score.notna().sum(axis=1)
    warmup_end = (n_valid >= 2).idxmax()

    accrual_panel = -funding.shift(-1)
    pnl_full = xsec.portfolio_returns(
        weights, fwd_ret,
        costs_bps=bear_regime.COSTS_BPS,
        rebal_every=bear_regime.REBAL,
        accrual=accrual_panel,
    )
    pnl_full = pnl_full[pnl_full.index >= warmup_end].dropna()

    # 2022-only slice
    pnl_2022 = pnl_full[pnl_full.index.year == 2022].dropna()

    print(f"  Bear panel: {available}")
    print(f"  Bear pnl window: {pnl_full.index.min().date()} → {pnl_full.index.max().date()} "
          f"({len(pnl_full)} days)")
    return pnl_full, pnl_2022


def verify_bear_anchor(pnl_full: pd.Series, ref: dict) -> None:
    """Assert reproduced bear metrics match the committed JSON within _TOL."""
    m = daily_metrics(pnl_full)
    # reference is "full_window_with_funding" from bear_regime.json
    checks = [
        ("sharpe",  m["sharpe"],  ref["sharpe"],  _TOL),
        ("vol_ann", m["vol_ann"], ref["vol_ann"],  _TOL),
        ("maxdd",   m["maxdd"],   ref["maxdd"],    _TOL),
    ]
    for name, got, want, tol in checks:
        diff = abs(got - want)
        if diff > tol:
            raise AssertionError(
                f"BEAR ANCHOR MISMATCH: {name} got={got:.4f} want={want:.4f} "
                f"diff={diff:.4f} > tol={tol:.4f}. "
                f"Reproduced bear series does not match bear_regime.json — STOPPING."
            )
    print(f"  Bear anchor OK: Sharpe={m['sharpe']:.4f} ann={100*m['ann']:.2f}% "
          f"vol={100*m['vol_ann']:.2f}% maxDD={100*m['maxdd']:.2f}%")


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    pd.set_option("display.width", 220)

    print("=" * 90)
    print("HONEST OPERATING POINTS — SURVIVORSHIP-DEBIASED POINT-IN-TIME BOOK")
    print("Bear-regime drawdown pinned by vol-equivalence cross-walk (NOT linear approx)")
    print("=" * 90)

    # ── Step 1: rebuild honest PT pnl ─────────────────────────────────────────
    print("\n[Step 1a] Rebuilding honest PT pnl from survivorship.json coin list...")
    pnl_honest = rebuild_honest_pnl()

    surv_json = json.loads((_HERE / "survivorship.json").read_text())
    honest_ref = surv_json["pt_book_metrics"]
    verify_honest_anchor(pnl_honest, honest_ref)

    honest_ft_m = daily_metrics(pnl_honest)
    honest_ann  = honest_ft_m["ann"]
    honest_vol  = honest_ft_m["vol_ann"]

    # ── Step 1b: rebuild bear pnl ──────────────────────────────────────────────
    print("\n[Step 1b] Rebuilding bear pnl from bear_fetch.BEAR_BASKET...")
    pnl_bear, pnl_bear_2022 = rebuild_bear_pnl()

    bear_json = json.loads((_HERE / "bear_regime.json").read_text())
    bear_ref  = bear_json["full_window_with_funding"]
    verify_bear_anchor(pnl_bear, bear_ref)

    bear_ft_m = daily_metrics(pnl_bear)
    bear_vol  = bear_ft_m["vol_ann"]

    print(f"\nFull-tilt anchors:")
    print(f"  HONEST book : ann={100*honest_ann:+.2f}%  vol={100*honest_vol:.2f}%  "
          f"Sharpe={honest_ft_m['sharpe']:.4f}  maxDD={100*honest_ft_m['maxdd']:.2f}%")
    print(f"  BEAR book   : ann={100*bear_ft_m['ann']:+.2f}%  vol={100*bear_vol:.2f}%  "
          f"Sharpe={bear_ft_m['sharpe']:.4f}  maxDD={100*bear_ft_m['maxdd']:.2f}%")

    # ── Step 2+3: per-target calculations ─────────────────────────────────────
    rows = []
    for T in TARGETS:
        # Honest sizing
        k_honest = T / honest_ann
        m_honest = _metrics_scaled_compounded(pnl_honest, k_honest)
        target_vol = m_honest["vol_ann"]      # = k_honest * honest_vol

        # Bear sizing via vol-equivalence
        k_bear = target_vol / bear_vol
        m_bear = _metrics_scaled_compounded(pnl_bear, k_bear)

        # 2022-only bear maxDD (pure bear year stress)
        # Use same k_bear but applied to 2022-only slice; maxDD on compounded equity
        m_bear_2022 = _metrics_scaled_compounded(pnl_bear_2022, k_bear)
        bear_2022_maxdd = m_bear_2022.get("maxdd", float("nan"))

        # Worst week in bear at k_bear sizing
        bear_scaled = pnl_bear * k_bear
        worst_wk = _worst_week_pct(bear_scaled)

        per_side_gross = k_honest   # dollar-neutral: Σlong=1 scaled by k → per-side=k

        rows.append({
            "target_ret":      T,
            "k_honest":        k_honest,
            "k_bear":          k_bear,
            "vol":             target_vol,
            "normal_maxdd":    m_honest["maxdd"],
            "bear_maxdd":      m_bear["maxdd"],
            "bear_2022_maxdd": bear_2022_maxdd,
            "sharpe":          m_honest["sharpe"],
            "calmar":          m_honest["calmar"],
            "per_side_gross":  per_side_gross,
            "bear_worst_week": worst_wk,
            # Store full metrics for JSON
            "_honest_metrics": m_honest,
            "_bear_metrics":   m_bear,
            "_bear_2022_metrics": m_bear_2022,
        })

    # ── Step 4: Print table ────────────────────────────────────────────────────
    print("\n" + "=" * 90)
    print("OPERATING POINTS TABLE")
    print("=" * 90)

    hdr = (f"{'target_ret':>10} | {'k_honest':>8} | {'vol':>7} | "
           f"{'NORMAL maxDD':>12} | {'BEAR maxDD':>10} | {'BEAR-2022 maxDD':>14} | "
           f"{'Sharpe':>7} | {'Calmar':>7} | {'per-side gross':>14}")
    print(hdr)
    print("-" * len(hdr))
    for row in rows:
        print(
            f"{100*row['target_ret']:>9.0f}% | "
            f"{row['k_honest']:>8.4f} | "
            f"{100*row['vol']:>6.1f}% | "
            f"{100*row['normal_maxdd']:>11.1f}% | "
            f"{100*row['bear_maxdd']:>9.1f}% | "
            f"{100*row['bear_2022_maxdd']:>13.1f}% | "
            f"{row['sharpe']:>7.3f} | "
            f"{row['calmar']:>7.3f} | "
            f"{row['per_side_gross']:>14.4f}"
        )

    print("\nNotes:")
    print("  - NORMAL maxDD  : max drawdown on compounded equity, honest book 2023-26")
    print("  - BEAR maxDD    : max drawdown at SAME risk budget (vol-equivalence) on")
    print("    Binance 2021-22 bear panel (full window including 2021 bull)")
    print("  - BEAR-2022 maxDD: same k_bear applied to 2022-only slice (pure bear year)")
    print("  - All maxDD on (1+k·r).cumprod() — exact compounding, not k·maxDD approx")
    print("  - per-side gross = k_honest (dollar-neutral book: Σ|w|=2, per-leg=1·k)")

    # ── Headline numbers (14% and 15%) ─────────────────────────────────────────
    print("\n" + "=" * 90)
    print("HEADLINE: 14% AND 15% TARGETS")
    print("=" * 90)
    for row in rows:
        if row["target_ret"] in (0.14, 0.15):
            print(f"\n  Target {100*row['target_ret']:.0f}%/yr :")
            print(f"    vol (annualized)       = {100*row['vol']:.2f}%")
            print(f"    NORMAL maxDD (2023-26) = {100*row['normal_maxdd']:.1f}%")
            print(f"    BEAR maxDD   (2021-22) = {100*row['bear_maxdd']:.1f}%")
            print(f"    BEAR-2022 maxDD        = {100*row['bear_2022_maxdd']:.1f}%")
            print(f"    Sharpe                 = {row['sharpe']:.3f}")
            print(f"    Calmar (normal regime) = {row['calmar']:.3f}")
            print(f"    k_honest               = {row['k_honest']:.4f}")
            print(f"    k_bear (vol-equiv)     = {row['k_bear']:.4f}")
            print(f"    per-side gross         = {row['per_side_gross']:.4f}x")

    # ── Caveats ────────────────────────────────────────────────────────────────
    CAVEATS = [
        ("cross_walk",
         "The bear cross-walk equates RISK BUDGET (annualized vol) across two "
         "different universes (20 Binance coins 2021-22 vs HL-era PT book). This is "
         "the right operational equivalence but NOT a same-universe replay."),
        ("maxdd_one_path",
         "maxDD is one path from limited history. The true forward drawdown "
         "distribution is wider. These are planning anchors, not guarantees."),
        ("sharpe_point_estimate",
         "Sharpe ~0.76 is itself a debiased point estimate. Do not over-trust the "
         "digits; half-splits show Sharpe 0.51 (H1) to 1.01 (H2)."),
        ("missing_dead_coins",
         "22 extra dead-coin candidates had no Binance data and could NOT be "
         "included in the PT book. The true survivorship discount may be larger; "
         "the honest ann ~29.5% may still be slightly generous."),
        ("bear_2021_bull",
         "The bear panel runs 2021-2022 full window; 2021 H1 was a strong bull "
         "season for crypto momentum (ann ~238%). Bear-2022 maxDD is the more "
         "conservative single-year stress number."),
    ]

    print("\n" + "=" * 90)
    print("CAVEATS")
    print("=" * 90)
    for tag, txt in CAVEATS:
        print(f"\n  [{tag}]\n  {txt}")

    # ── Write JSON ─────────────────────────────────────────────────────────────
    def _safe(d):
        if not d:
            return {}
        return {k: (float(v) if isinstance(v, (float, np.floating)) else
                    int(v)   if isinstance(v, (int, np.integer)) else v)
                for k, v in d.items()}

    json_rows = []
    for row in rows:
        jr = {k: v for k, v in row.items() if not k.startswith("_")}
        jr["honest_metrics"] = _safe(row["_honest_metrics"])
        jr["bear_metrics"]   = _safe(row["_bear_metrics"])
        jr["bear_2022_metrics"] = _safe(row["_bear_2022_metrics"])
        # convert all float values
        for k2, v2 in jr.items():
            if isinstance(v2, (float, np.floating)):
                jr[k2] = float(v2)
        json_rows.append(jr)

    out = {
        "meta": {
            "description": (
                "Honest operating points on survivorship-debiased PT book "
                "(Sharpe≈0.76, ann≈29.5%, vol≈38.9%). Bear drawdown pinned by "
                "vol-equivalence cross-walk to 2021-22 Binance bear panel."
            ),
            "method_note": (
                "For each target return T: k_honest = T / honest_ann scales the "
                "honest book; target_vol = k_honest * honest_vol is the risk budget. "
                "k_bear = target_vol / bear_vol scales the bear book to the SAME "
                "risk budget. maxDD is computed on (1+k*r).cumprod() (exact "
                "compounding), not the linear k*maxDD approximation."
            ),
            "honest_book": {
                "description": "Survivorship-debiased point-in-time book (2023-06 → 2026-06)",
                "n_coins_pt": len(surv_json["frozen_survivor_coins"]) + len(surv_json["extra_dead_coins_included"]),
                "period": surv_json["common_window"],
                "full_tilt_metrics": _safe(honest_ft_m),
            },
            "bear_book": {
                "description": "Binance perp 2021-22 bear stress (20 coins, warmup-trimmed)",
                "period": bear_json["window"],
                "full_tilt_metrics": _safe(bear_ft_m),
            },
        },
        "caveats": {tag: txt for tag, txt in CAVEATS},
        "operating_points": json_rows,
    }

    out_path = _HERE / "operating_points_honest.json"
    out_path.write_text(json.dumps(out, indent=2, default=float))
    print(f"\nJSON written → {out_path}")
    print("=" * 90)
