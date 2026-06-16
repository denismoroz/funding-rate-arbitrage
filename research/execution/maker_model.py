"""
maker_model.py — Execution-cost model: TAKER (market) vs POST-ONLY LIMIT (maker).

QUESTION
========
The live engine currently sends MARKET orders → pays TAKER fee + slippage on
every leg. Should we switch to POST-ONLY LIMIT (maker) execution? Maker is NOT
free: a passive resting order risks (a) ADVERSE SELECTION (it fills
disproportionately when the market moves against you) and (b) NON-FILL (you
either cross late as a taker, or under-fill the target book). This script
quantifies the economics + the fill-rate BREAKEVEN, then re-runs the two real
research books at the resulting effective costs to size the Sharpe stakes.

This is RESEARCH ONLY. We have NO order-book / spread data and NO measured
fill-rate in this panel. spread_capture, adverse_selection, drift_penalty and
p_fill are ASSUMPTIONS, sensitivity-swept here — NOT measured. See CAVEATS.

COST DECOMPOSITION (facts from our own HL fee audit, base volume tier)
=====================================================================
  - HL perp TAKER fee = 0.035% = 3.5 bps.
  - HL perp MAKER fee = 0.010% = 1.0 bps (base tier; NO rebate without volume
    we don't have → maker_fee is a param, default 1.0 bps, NEVER a rebate).
  - Research books assume COSTS_BPS = 8.5 bps per leg one-way. Decompose:
        8.5 = taker_fee(3.5) + implied_slippage(5.0).
    So the research assumption bakes in ~5.0 bps of slippage/impact per leg on
    top of the taker fee. We back this out explicitly and treat implied_slippage
    as a calibrated parameter.

MAKER E[cost_per_leg] MODEL (policy A = cross-after-timeout)
===========================================================
  E[cost_per_leg] = p_fill * c_maker_filled + (1 - p_fill) * c_unfilled
    c_maker_filled = maker_fee - spread_capture + adverse_selection
        maker_fee        : 1.0 bps   (param; HL base maker fee, paid)
        spread_capture   : 2.5 bps   (param; EARNED by resting at/inside the
                                       touch instead of crossing. A market order
                                       PAYS ~half-spread; a passive order can
                                       EARN part of it. Default = half of the
                                       implied 5bps slippage. REDUCES cost.)
        adverse_selection: 2.5 bps   (param; PENALTY — passive fills land on the
                                       wrong side of the move. INCREASES cost.)
    c_unfilled = c_taker + drift_penalty
        c_taker          : 8.5 bps   (full current taker cost = 3.5 fee + 5 slip)
        drift_penalty    : 1.5 bps   (param; price drifted while you waited
                                       before crossing. INCREASES cost.)

  At default params:
    c_maker_filled = 1.0 - 2.5 + 2.5 = 1.0 bps
    c_unfilled     = 8.5 + 1.5       = 10.0 bps

BREAKEVEN (headline)
====================
  Solve p_fill* such that E[cost_per_leg] == c_taker (8.5):
    p* = (c_unfilled - c_taker) / (c_unfilled - c_maker_filled)
  Above p* maker SAVES money; below p* maker is WORSE. We report p* at default
  params and across an adverse_selection / spread_capture / drift sweep.

POLICY B (skip / tracking-error)
================================
  The unfilled portion does NOT cross — it stays off the target book, so the
  book holds (1 - p_fill) LESS of the intended rebalance delta that period. This
  is TRACKING ERROR, not a per-leg bp cost, and cannot be priced cleanly in this
  panel (no per-order fill simulation). We handle it honestly: quantify it as a
  fraction-of-delta shortfall and bound its return impact qualitatively. The
  quantitative results below are policy A; policy B is a documented caveat.

BOOK IMPACT
===========
  We take the policy-A effective per-leg cost at p_fill ∈ {0.3,0.5,0.7,0.9,1.0}
  (default params) and re-run the two REAL books at each cost:
    - XSMOM incumbent  (cross-sectional momentum, turnover ~54/yr)
    - TREND committed  (TSMOM ensemble directional, turnover ~235/yr)
  Honest DAILY metrics via metrics_daily.daily_metrics. Annual cost drag =
  turnover_per_yr * cost_per_leg / 1e4. Trend (high turnover) is FAR more
  cost-sensitive → a maker switch helps trend ~4x more per bp saved.

ENV
===
  PYTHONPATH=research:research/validation_harness:research/cross_sectional:\
             research/cross_sectional/crypto:research/trend_following
  .venv/bin/python research/execution/maker_model.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

# ── Research book APIs (read-only imports; we do not modify these) ─────────────
import signals                       # momentum_ensemble
import xsec                          # rank_to_weights, portfolio_returns
import survivorship                  # build_pt_panel, LOOKBACKS, REBAL, run_book
import trend                         # tsmom_ensemble, portfolio_returns_directional, realized_vol
from metrics_daily import daily_metrics

_HERE = Path(__file__).parent
_OUT = _HERE / "maker_model.json"

# ══════════════════════════════════════════════════════════════════════════════
# 1. COST DECOMPOSITION + MAKER MODEL PARAMETERS
# ══════════════════════════════════════════════════════════════════════════════

TAKER_FEE = 3.5          # bps — HL perp taker, base tier (audited)
MAKER_FEE = 1.0          # bps — HL perp maker, base tier (param, NO rebate)
COSTS_BPS = 8.5          # bps — research per-leg one-way assumption
IMPLIED_SLIPPAGE = COSTS_BPS - TAKER_FEE   # = 5.0 bps backed out
C_TAKER = COSTS_BPS      # full current taker cost (3.5 fee + 5.0 slippage)

# Maker model defaults (ALL assumptions, sensitivity-swept below)
DEF_SPREAD_CAPTURE = 2.5    # bps EARNED resting (half of implied 5bps slippage)
DEF_ADVERSE_SEL    = 2.5    # bps PENALTY (passive fills on the wrong side)
DEF_DRIFT_PENALTY  = 1.5    # bps PENALTY on the taker-fallback leg


def c_maker_filled(maker_fee=MAKER_FEE, spread_capture=DEF_SPREAD_CAPTURE,
                   adverse_selection=DEF_ADVERSE_SEL) -> float:
    """Cost of a leg that DOES fill passively (bps)."""
    return maker_fee - spread_capture + adverse_selection


def c_unfilled(drift_penalty=DEF_DRIFT_PENALTY) -> float:
    """Cost of the taker-fallback leg after timeout (bps), policy A."""
    return C_TAKER + drift_penalty


def e_cost_per_leg(p_fill, maker_fee=MAKER_FEE, spread_capture=DEF_SPREAD_CAPTURE,
                   adverse_selection=DEF_ADVERSE_SEL, drift_penalty=DEF_DRIFT_PENALTY):
    """Policy-A expected per-leg cost (bps) at fill-rate p_fill."""
    cf = c_maker_filled(maker_fee, spread_capture, adverse_selection)
    cu = c_unfilled(drift_penalty)
    return p_fill * cf + (1.0 - p_fill) * cu


def breakeven_pfill(maker_fee=MAKER_FEE, spread_capture=DEF_SPREAD_CAPTURE,
                    adverse_selection=DEF_ADVERSE_SEL, drift_penalty=DEF_DRIFT_PENALTY):
    """p_fill* where E[cost] == C_TAKER (8.5). Above p* maker saves money.

    If c_maker_filled >= C_TAKER, maker never wins (p* > 1 → return inf-flag).
    If c_unfilled <= C_TAKER, maker always wins (p* <= 0 → return 0.0-flag).
    """
    cf = c_maker_filled(maker_fee, spread_capture, adverse_selection)
    cu = c_unfilled(drift_penalty)
    denom = cu - cf
    if abs(denom) < 1e-12:
        return float("nan")
    p = (cu - C_TAKER) / denom
    return p


# ══════════════════════════════════════════════════════════════════════════════
# 2. BUILD THE SHARED POINT-IN-TIME PANEL (same as XSMOM + TREND books)
# ══════════════════════════════════════════════════════════════════════════════

def build_panel() -> dict:
    """frozen survivors ∪ extra dead/delisted → survivorship.build_pt_panel.

    Identical construction to survivorship.py / characterize.py so the book
    re-runs reproduce the committed research numbers."""
    surv = json.loads((_HERE.parent / "cross_sectional" / "crypto" /
                       "survivorship.json").read_text())
    coins = sorted(set(surv["frozen_survivor_coins"])
                   | set(surv["extra_dead_coins_included"]))
    return survivorship.build_pt_panel(coins)


# ══════════════════════════════════════════════════════════════════════════════
# 3. THE TWO REAL BOOKS, PARAMETERIZED BY EFFECTIVE COST
# ══════════════════════════════════════════════════════════════════════════════

# Trend committed config (spec + characterize.py constants)
TREND_LOOKBACKS = (30, 60, 90, 120)
VOL_WINDOW = 30
VOL_TARGET = 0.02
LEVERAGE_CAP = 3.0


def xsmom_pnl(panel: dict, eff_cost_bps: float) -> pd.Series:
    """XSMOM incumbent book at a parameterized per-leg cost.

    Replicates survivorship.run_book exactly but with EFF_COST instead of the
    hardcoded 8.5: momentum_ensemble → rank_to_weights → portfolio_returns."""
    score = signals.momentum_ensemble(panel, lookbacks=survivorship.LOOKBACKS)
    weights = xsec.rank_to_weights(score)
    accrual = -panel["funding"].shift(-1)
    return xsec.portfolio_returns(
        weights, panel["fwd_ret"], costs_bps=eff_cost_bps,
        rebal_every=survivorship.REBAL, accrual=accrual,
    )


def trend_pnl(panel: dict, eff_cost_bps: float) -> pd.Series:
    """TREND committed (TSMOM ensemble) book at a parameterized per-leg cost."""
    positions = trend.tsmom_ensemble(panel, lookbacks=TREND_LOOKBACKS,
                                     vol_window=VOL_WINDOW)
    vol = trend.realized_vol(panel["price"], VOL_WINDOW)
    accrual = -panel["funding"].shift(-1)
    return trend.portfolio_returns_directional(
        positions, panel["fwd_ret"], costs_bps=eff_cost_bps, accrual=accrual,
        vol=vol, vol_target=VOL_TARGET, leverage_cap=LEVERAGE_CAP,
    )


def measure_xsmom_turnover(panel: dict) -> float:
    """Annualized one-way turnover of the XSMOM book (Σ|Δheld| per yr).

    Mirrors xsec.portfolio_returns' rebalance loop: turnover charged only on
    rebal_every boundaries; held carried forward between."""
    score = signals.momentum_ensemble(panel, lookbacks=survivorship.LOOKBACKS)
    w = xsec.rank_to_weights(score).reindex_like(panel["fwd_ret"]).fillna(0.0)
    rebal = survivorship.REBAL
    prev = pd.Series(0.0, index=w.columns)
    tot = 0.0
    for i in range(len(w)):
        if i % rebal == 0:
            held = w.iloc[i]
            tot += float((held - prev).abs().sum())
            prev = held
    days = len(w)
    return tot / days * 365.0


def measure_trend_turnover(panel: dict) -> float:
    """Annualized one-way turnover of the TREND book (Σ|Δheld| per yr).

    Replicates portfolio_returns_directional position-scaling (vol-target +
    leverage cap) then sums daily |Δheld|."""
    positions = trend.tsmom_ensemble(panel, lookbacks=TREND_LOOKBACKS,
                                     vol_window=VOL_WINDOW)
    vol = trend.realized_vol(panel["price"], VOL_WINDOW)
    fwd = panel["fwd_ret"]
    pos = positions.reindex_like(fwd).fillna(0.0)
    # vol-targeting
    v = vol.reindex_like(fwd)
    scale = (VOL_TARGET / v).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    pos = pos * scale
    # leverage cap
    gross_abs = pos.abs().sum(axis=1)
    factor = pd.Series(1.0, index=pos.index)
    over = gross_abs > LEVERAGE_CAP
    factor[over] = LEVERAGE_CAP / gross_abs[over]
    pos = pos.mul(factor, axis=0)
    prev = pd.Series(0.0, index=pos.columns)
    tot = 0.0
    for i in range(len(pos)):
        held = pos.iloc[i]
        tot += float((held - prev).abs().sum())
        prev = held
    days = len(pos)
    return tot / days * 365.0


# ══════════════════════════════════════════════════════════════════════════════
# 4. MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 78)
    print("EXECUTION-COST MODEL — TAKER (market) vs POST-ONLY LIMIT (maker)")
    print("=" * 78)

    # ── Cost decomposition ────────────────────────────────────────────────────
    print("\n[1] COST DECOMPOSITION")
    print(f"    Research per-leg assumption  COSTS_BPS = {COSTS_BPS} bps")
    print(f"      = taker_fee {TAKER_FEE} + implied_slippage {IMPLIED_SLIPPAGE}")
    print(f"    HL taker fee = {TAKER_FEE} bps | HL maker fee = {MAKER_FEE} bps "
          f"(base tier, NO rebate)")

    # ── Maker model at default params ─────────────────────────────────────────
    cf = c_maker_filled()
    cu = c_unfilled()
    p_star = breakeven_pfill()
    print("\n[2] MAKER E[cost_per_leg] MODEL (policy A: cross-after-timeout)")
    print(f"    params: maker_fee={MAKER_FEE}  spread_capture={DEF_SPREAD_CAPTURE}  "
          f"adverse_selection={DEF_ADVERSE_SEL}  drift_penalty={DEF_DRIFT_PENALTY}")
    print(f"    c_maker_filled = {MAKER_FEE} - {DEF_SPREAD_CAPTURE} + {DEF_ADVERSE_SEL} "
          f"= {cf:.2f} bps")
    print(f"    c_unfilled     = {C_TAKER} + {DEF_DRIFT_PENALTY} = {cu:.2f} bps")
    print(f"    E[cost] = p*{cf:.2f} + (1-p)*{cu:.2f}")
    print(f"\n    >>> BREAKEVEN p_fill* = {p_star:.4f}  "
          f"({100*p_star:.1f}% passive fills needed for maker to beat 8.5bps taker)")

    # E[cost] at the sweep fill-rates (default params)
    pfills = [0.3, 0.5, 0.7, 0.9, 1.0]
    eff_costs = {p: e_cost_per_leg(p) for p in pfills}
    print("\n    E[cost_per_leg] vs p_fill (default params):")
    for p in pfills:
        verdict = "saves" if eff_costs[p] < C_TAKER else "WORSE"
        print(f"      p_fill={p:.1f} -> {eff_costs[p]:6.2f} bps   ({verdict} vs {C_TAKER})")

    # ── Breakeven sweep over adverse_selection / spread_capture / drift ───────
    print("\n[3] BREAKEVEN p* SWEEP (one param varied, others at default)")
    sweep = {"adverse_selection": [0.0, 1.0, 2.5, 4.0, 6.0],
             "spread_capture":    [0.0, 1.0, 2.5, 4.0, 5.0],
             "drift_penalty":     [0.0, 1.0, 1.5, 3.0, 5.0]}
    breakeven_grid = {}
    for param, vals in sweep.items():
        row = {}
        for v in vals:
            kw = {param: v}
            p = breakeven_pfill(**kw)
            row[v] = p
        breakeven_grid[param] = row
        cells = "  ".join(
            f"{v}->{'>1(never)' if row[v] > 1 else ('<=0(always)' if row[v] <= 0 else f'{row[v]:.3f}')}"
            for v in vals)
        print(f"    {param:18s}: {cells}")

    # ── Build panel + measure turnovers ───────────────────────────────────────
    print("\n[4] BUILDING SHARED PT PANEL + MEASURING REAL TURNOVERS...")
    panel = build_panel()
    price = panel["price"]
    print(f"    Panel: {price.index.min().date()} -> {price.index.max().date()}  "
          f"({len(price)} days, {len(panel['coins'])} coins)")
    xsmom_turn = measure_xsmom_turnover(panel)
    trend_turn = measure_trend_turnover(panel)
    print(f"    XSMOM turnover  = {xsmom_turn:6.1f} /yr  (one-way Σ|Δheld|, ~54 expected)")
    print(f"    TREND turnover  = {trend_turn:6.1f} /yr  (one-way Σ|Δheld|, ~235 expected)")

    # ── Re-run both books at baseline 8.5 + each maker-effective cost ──────────
    print("\n[5] BOOK IMPACT — honest DAILY metrics at each effective cost")
    cost_points = {"baseline_8.5": C_TAKER}
    for p in pfills:
        cost_points[f"maker_p{p:.1f}"] = eff_costs[p]

    results = {"XSMOM": {}, "TREND": {}}
    drag = {"XSMOM": {}, "TREND": {}}
    base_sharpe = {}
    print(f"\n    {'book':6s} {'cost-pt':14s} {'effbps':>7s} {'Sharpe':>8s} "
          f"{'ann%':>8s} {'maxDD%':>8s} {'Calmar':>8s} {'dragApr%':>9s} {'dSharpe':>8s}")
    for book, pnl_fn, turn in (("XSMOM", xsmom_pnl, xsmom_turn),
                               ("TREND", trend_pnl, trend_turn)):
        for label, cost in cost_points.items():
            m = daily_metrics(pnl_fn(panel, cost))
            d = turn * cost / 1e4 * 100.0   # annual cost drag in %
            results[book][label] = {
                "eff_cost_bps": round(cost, 3),
                "sharpe": round(m["sharpe"], 4), "ann_pct": round(100*m["ann"], 3),
                "maxdd_pct": round(100*m["maxdd"], 3),
                "calmar": (round(m["calmar"], 3) if m["calmar"] == m["calmar"] else None),
                "n": m["n"],
            }
            drag[book][label] = round(d, 3)
            if label == "baseline_8.5":
                base_sharpe[book] = m["sharpe"]
            dsh = m["sharpe"] - base_sharpe.get(book, m["sharpe"])
            print(f"    {book:6s} {label:14s} {cost:7.2f} {m['sharpe']:8.3f} "
                  f"{100*m['ann']:8.2f} {100*m['maxdd']:8.2f} "
                  f"{m['calmar'] if m['calmar']==m['calmar'] else float('nan'):8.2f} "
                  f"{d:9.3f} {dsh:+8.3f}")

    # ── Sharpe gain if maker delivers ~3 bps (vs 8.5) ─────────────────────────
    # Find the p_fill whose eff cost is closest to 3.0 for an apples-to-apples
    # comparison, AND also report a direct 3.0bps re-run.
    print("\n[6] HEADLINE: Sharpe gain if maker delivers effective ~3 bps vs 8.5")
    gain3 = {}
    for book, pnl_fn in (("XSMOM", xsmom_pnl), ("TREND", trend_pnl)):
        m85 = daily_metrics(pnl_fn(panel, 8.5))
        m30 = daily_metrics(pnl_fn(panel, 3.0))
        gain3[book] = {
            "sharpe_8.5": round(m85["sharpe"], 4), "sharpe_3.0": round(m30["sharpe"], 4),
            "dsharpe": round(m30["sharpe"] - m85["sharpe"], 4),
            "ann_8.5_pct": round(100*m85["ann"], 3), "ann_3.0_pct": round(100*m30["ann"], 3),
            "dann_pct": round(100*(m30["ann"] - m85["ann"]), 3),
        }
        print(f"    {book}: Sharpe {m85['sharpe']:.3f} -> {m30['sharpe']:.3f} "
              f"(+{m30['sharpe']-m85['sharpe']:.3f})   "
              f"ann {100*m85['ann']:.2f}% -> {100*m30['ann']:.2f}% "
              f"(+{100*(m30['ann']-m85['ann']):.2f}pp)")

    # ── Policy B bound (tracking error) ───────────────────────────────────────
    print("\n[7] POLICY B (skip/tracking-error) — qualitative bound")
    print("    Unfilled fraction (1-p_fill) stays OFF the target book that period.")
    print("    NOT priceable as bps/leg here (no per-order fill sim). Bound:")
    for p in pfills:
        print(f"      p_fill={p:.1f} -> book holds {100*(1-p):.0f}% LESS of the "
              f"intended rebal delta that period (tracking error, sign-ambiguous).")

    # ── CAVEATS + VERDICT ─────────────────────────────────────────────────────
    caveats = [
        "NO order-book / spread data and NO measured fill-rate in this research "
        "panel. spread_capture, adverse_selection, drift_penalty and p_fill are "
        "ASSUMPTIONS, sensitivity-swept here, NOT measured.",
        "Real fill-rate + spread must come from (a) auditing the live prod fills "
        "DB and (b) a live post-only A/B test. This model frames 'what fill-rate "
        "is needed and what is at stake' — it does NOT prove maker is cheaper.",
        "HL maker fee 1.0 bps is BASE TIER. No rebate is assumed (rebates need "
        "volume our ~$345 occupied capital does not generate).",
        "Policy B (skip) is tracking error, not a per-leg bp cost; quantitative "
        "results are policy A (cross-after-timeout) only. B is a documented caveat.",
        "Maker changes are LIVE EXECUTION changes to src/frab — OUT OF SCOPE here "
        "(research only). Adverse selection is hardest on thin alts (wide spread, "
        "low fill-rate) and on a directional book that chases momentum.",
        "Turnover is measured on the PT survivorship panel; live universe (~15-20 "
        "coins) and live rebalance cadence may differ from the backtest grid.",
    ]

    verdict = (
        f"MAKER SWITCH — CONDITIONALLY worth pursuing, and it matters MOST for "
        f"the TREND book by a wide margin. Decomposition: research 8.5bps/leg = "
        f"3.5 taker fee + 5.0 implied slippage; HL maker fee is 1.0bps. At default "
        f"params (spread_capture {DEF_SPREAD_CAPTURE}, adverse_selection "
        f"{DEF_ADVERSE_SEL}, drift {DEF_DRIFT_PENALTY}) the breakeven is "
        f"p_fill*={p_star:.2f}: we must fill >={100*p_star:.0f}% of legs passively "
        f"for maker (policy A) to beat all-taker. The stakes scale with turnover: "
        f"TREND (~{trend_turn:.0f}/yr) is ~{trend_turn/xsmom_turn:.1f}x more "
        f"cost-sensitive than XSMOM (~{xsmom_turn:.0f}/yr), so the same per-bp "
        f"saving buys ~{trend_turn/xsmom_turn:.1f}x more Sharpe/return on TREND. "
        f"At effective 3bps vs 8.5, TREND gains Sharpe "
        f"+{gain3['TREND']['dsharpe']:.3f} (ann +{gain3['TREND']['dann_pct']:.2f}pp) "
        f"vs XSMOM +{gain3['XSMOM']['dsharpe']:.3f} "
        f"(ann +{gain3['XSMOM']['dann_pct']:.2f}pp). PLAUSIBILITY: a weekly "
        f"target-book reconcile on liquid majors can plausibly fill >="
        f"{100*p_star:.0f}% passively; thin alts (wide spread → biggest fee win) "
        f"are exactly where fill-risk + adverse selection are worst, so a blended "
        f"fill-rate near breakeven is realistic but UNCERTAIN. DECISION: do NOT "
        f"flip live blindly. First audit the live prod fills DB for realized "
        f"spread + passive fill-rate, then run a live post-only A/B test — "
        f"prioritising TREND, where the per-bp payoff is largest."
    )
    print("\n[8] VERDICT")
    for line in verdict.split(". "):
        print(f"    {line.strip()}")
    print("\n    CRITICAL CAVEATS:")
    for c in caveats:
        print(f"      - {c}")

    # ── Write JSON ────────────────────────────────────────────────────────────
    out = {
        "test": "maker_model",
        "description": "Execution-cost model: TAKER (market) vs POST-ONLY LIMIT "
                       "(maker). Breakeven fill-rate + per-book Sharpe impact. "
                       "RESEARCH ONLY — all maker params are swept assumptions.",
        "cost_decomposition": {
            "research_costs_bps_per_leg": COSTS_BPS,
            "taker_fee_bps": TAKER_FEE,
            "implied_slippage_bps": IMPLIED_SLIPPAGE,
            "maker_fee_bps": MAKER_FEE,
            "note": "8.5 = 3.5 taker fee + 5.0 implied slippage; maker fee 1.0bps base tier, NO rebate",
        },
        "maker_model": {
            "policy_A": "cross-after-timeout: unfilled portion crosses as taker",
            "policy_B": "skip/tracking-error: unfilled portion stays off target book (qualitative)",
            "formula": "E[cost] = p_fill*(maker_fee - spread_capture + adverse_selection) "
                       "+ (1-p_fill)*(c_taker + drift_penalty)",
            "default_params_bps": {
                "maker_fee": MAKER_FEE, "spread_capture": DEF_SPREAD_CAPTURE,
                "adverse_selection": DEF_ADVERSE_SEL, "drift_penalty": DEF_DRIFT_PENALTY,
                "c_taker": C_TAKER,
            },
            "c_maker_filled_bps": round(cf, 3),
            "c_unfilled_bps": round(cu, 3),
        },
        "breakeven": {
            "p_fill_star_default": round(p_star, 4),
            "interpretation": "fill-rate at which policy-A E[cost] == 8.5bps taker; "
                              "above p* maker saves, below p* maker is worse",
            "sweep": {param: {str(k): (round(v, 4) if abs(v) < 1e6 else None)
                              for k, v in row.items()}
                      for param, row in breakeven_grid.items()},
        },
        "eff_cost_by_pfill_bps": {f"p{p:.1f}": round(eff_costs[p], 3) for p in pfills},
        "turnover_per_yr": {"XSMOM": round(xsmom_turn, 2), "TREND": round(trend_turn, 2),
                            "ratio_trend_over_xsmom": round(trend_turn / xsmom_turn, 3)},
        "book_metrics": results,
        "annual_cost_drag_pct": drag,
        "headline_3bps_vs_8.5bps": gain3,
        "policy_B_tracking_error_bound": {
            f"p{p:.1f}": f"book holds {round(100*(1-p))}% less of intended rebal delta/period"
            for p in pfills},
        "caveats": caveats,
        "verdict": verdict,
    }
    _OUT.write_text(json.dumps(out, indent=2))
    print(f"\n[written] {_OUT}")


if __name__ == "__main__":
    main()
