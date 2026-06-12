"""C5 — PARAMETER ROBUSTNESS MAP for the crypto cross-sectional book.

WHY this exists: v1 momentum was real-but-FRAGILE (PBO 0.83 — the best lookback
did not transfer forward). The cure is ROBUSTNESS, not optimization. A real edge
is a PLATEAU (works across a NEIGHBOURHOOD of parameter values); a fake one is a
SPIKE (works only at one value, dies next to it). This script sweeps each signal
family's key parameter over a sensible grid and prints daily Sharpe AND Calmar
per value so a human can SEE plateau-vs-spike at a glance. We NEVER pick a
parameter because it maximizes the backtest — we look at the SHAPE of the map.

Honest DAILY metrics (annualize sqrt(365)) via metrics_daily — the shared harness
annualizes hourly and would inflate levels ~5x. Frozen 34-coin universe →
deterministic. Cost = 8.5 bps/leg (same as v1). The engine (xsec) is used
straight: portfolio_returns earns fwd_ret[t] on weights[t] (fwd_ret is already
forward-aligned in the panel, so NO extra shift).

Run:
  PYTHONPATH=.../research:.../validation_harness:.../cross_sectional:.../crypto \\
    python -u sweep.py
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

import cryptodata
import signals
import xsec
from metrics_daily import daily_metrics

COSTS_BPS = 8.5          # one-way per-leg cost (same as v1)
REBAL_EVERY = 7          # weekly rebalance baseline (crypto xsec standard cadence)
TERCILE_FRAC = 1 / 3     # default leg size
_HERE = Path(__file__).parent
UNIVERSE_JSON = _HERE / "universe.json"


def _frozen_universe() -> list[str]:
    return list(json.loads(UNIVERSE_JSON.read_text())["coins"])


def _pnl(panel: dict, score: pd.DataFrame,
         tercile_frac: float = TERCILE_FRAC, rebal_every: int = REBAL_EVERY,
         costs_bps: float = COSTS_BPS) -> pd.Series:
    """score panel → dollar-neutral weights → net daily book pnl (full period)."""
    w = xsec.rank_to_weights(score, tercile_frac=tercile_frac)
    return xsec.portfolio_returns(w, panel["fwd_ret"],
                                  costs_bps=costs_bps, rebal_every=rebal_every)


# ── plateau / spike verdict ─────────────────────────────────────────────────────

def _verdict(sharpes: list[float]) -> str:
    """One-line PLATEAU vs SPIKE verdict from a row of daily Sharpes.

    PLATEAU  — same sign across the WHOLE grid and the spread is modest relative to
               the level (the edge survives in a neighbourhood, not one point).
    SPIKE    — one value carries it: the best is far above the rest, OR the sign
               flips across the grid (no stable neighbourhood).
    FLAT/WEAK— all small (|Sharpe| < 0.3 everywhere) → no edge to speak of.
    """
    s = np.asarray(sharpes, dtype=float)
    s = s[~np.isnan(s)]
    if len(s) < 2:
        return "n/a (too few points)"
    same_sign = (s > 0).all() or (s < 0).all()
    amax = np.abs(s).max()
    if amax < 0.3:
        return f"FLAT/WEAK — |Sharpe|<0.3 across the grid (max {amax:.2f}); no edge"
    best = s.max()
    rest = np.sort(s)[:-1]                      # all but the single best
    rest_mean = rest.mean() if len(rest) else best
    gap = best - rest_mean
    spread = s.max() - s.min()
    if not same_sign:
        return (f"SPIKE — sign FLIPS across grid (min {s.min():+.2f}, max {s.max():+.2f}); "
                f"no stable neighbourhood")
    # same sign: plateau unless the best dwarfs the rest
    if best > 0:
        if gap > 0.5 * abs(best) and gap > 0.30:
            return (f"SPIKE — best {best:+.2f} dwarfs rest (mean {rest_mean:+.2f}, "
                    f"gap {gap:.2f}); rides one value")
        return (f"PLATEAU — Sharpe stays {('+' if best>0 else '-')}ve across grid "
                f"(range {s.min():+.2f}..{s.max():+.2f}, spread {spread:.2f})")
    # all negative & same sign → consistently bad (a stable anti-edge)
    return (f"PLATEAU(neg) — Sharpe {s.min():+.2f}..{s.max():+.2f} negative across grid; "
            f"consistently UNprofitable, not noise")


# ── table printers ──────────────────────────────────────────────────────────────

_HDR = f"  {'param':<16}{'sharpe':>9}{'calmar':>9}{'ann%':>9}{'maxDD%':>9}{'hit%':>8}{'n':>7}"


def _row(label: str, m: dict) -> tuple[str, float]:
    if not m:
        return f"  {label:<16}{'(too short)':>42}", np.nan
    cal = m["calmar"]
    cal_s = f"{cal:>9.2f}" if not np.isnan(cal) else f"{'nan':>9}"
    line = (f"  {label:<16}{m['sharpe']:>9.2f}{cal_s}"
            f"{100*m['ann']:>9.2f}{100*m['maxdd']:>9.2f}{100*m['hit']:>8.1f}{m['n']:>7d}")
    return line, m["sharpe"]


def sweep_one_param(panel, title, build, values, fmt=str):
    """Sweep a single-parameter family; print table + verdict. build(v)->score."""
    print(f"\n=== {title} ===")
    print(_HDR)
    sharpes = []
    for v in values:
        m = daily_metrics(_pnl(panel, build(v)))
        line, sh = _row(fmt(v), m)
        print(line)
        sharpes.append(sh)
    print(f"  VERDICT: {_verdict(sharpes)}")
    return sharpes


# ── first-half / second-half split (time stability) ─────────────────────────────

def split_check(panel, title, build, values, fmt=str):
    """For each param value, daily Sharpe on the FIRST half vs SECOND half of the
    sample. Robust-across-TIME, not just across params: a real edge should not be
    carried entirely by one regime."""
    print(f"\n=== {title} — first-half / second-half SPLIT (daily Sharpe) ===")
    print(f"  {'param':<16}{'full':>9}{'1st-half':>11}{'2nd-half':>11}{'stable?':>10}")
    for v in values:
        pnl = _pnl(panel, build(v)).dropna()
        n = len(pnl)
        h1, h2 = pnl.iloc[:n // 2], pnl.iloc[n // 2:]
        sf = daily_metrics(pnl).get("sharpe", np.nan)
        s1 = daily_metrics(h1).get("sharpe", np.nan)
        s2 = daily_metrics(h2).get("sharpe", np.nan)
        stable = "yes" if (np.sign(s1) == np.sign(s2) and not np.isnan(s1*s2)) else "NO"
        print(f"  {fmt(v):<16}{sf:>9.2f}{s1:>11.2f}{s2:>11.2f}{stable:>10}")


# ── main ─────────────────────────────────────────────────────────────────────────

def main():
    coins = _frozen_universe()
    panel = cryptodata.load_panel(coins=coins)
    px = panel["price"]
    print(f"PANEL  {px.shape[0]} days x {px.shape[1]} coins  "
          f"({px.index.min().date()} -> {px.index.max().date()})")
    print(f"engine: cost={COSTS_BPS} bps/leg  rebal_every={REBAL_EVERY}d  "
          f"tercile_frac={TERCILE_FRAC:.2f}  annualize=sqrt(365) (DAILY)")
    print("\nREAD AS: PLATEAU = edge survives in a neighbourhood of the param "
          "(trustworthy);\n         SPIKE = rides one value (fragile, do NOT build on it).")

    # 1) momentum lookback
    mom_lbs = [14, 21, 30, 45, 60, 90, 120]
    mom_sh = sweep_one_param(
        panel, "MOMENTUM — lookback_days",
        lambda lb: signals.momentum(panel, lb), mom_lbs, fmt=lambda v: f"lb={v}d")

    # 2) reversal lookback
    rev_lbs = [3, 5, 7, 10, 14]
    rev_sh = sweep_one_param(
        panel, "REVERSAL — lookback_days",
        lambda lb: signals.reversal(panel, lb), rev_lbs, fmt=lambda v: f"lb={v}d")

    # 3) vol-adjusted momentum: lookback x vol_window
    vam_lbs = [30, 60, 90]
    vam_vws = [20, 30]
    print(f"\n=== VOL_ADJ_MOMENTUM — lookback_days x vol_window ===")
    print(_HDR)
    vam_sh = []
    for lb in vam_lbs:
        for vw in vam_vws:
            m = daily_metrics(_pnl(panel, signals.vol_adjusted_momentum(panel, lb, vw)))
            line, sh = _row(f"lb={lb} vw={vw}", m)
            print(line)
            vam_sh.append(sh)
    print(f"  VERDICT: {_verdict(vam_sh)}")

    # 4) portfolio knobs on momentum(30): tercile_frac and rebal_every
    print(f"\n=== PORTFOLIO KNOBS on momentum(30) ===")
    base_score = signals.momentum(panel, 30)
    print(f"\n  -- tercile_frac (leg size), rebal_every={REBAL_EVERY}d --")
    print(_HDR)
    tf_sh = []
    for tf in [0.20, 0.25, 0.33]:
        m = daily_metrics(_pnl(panel, base_score, tercile_frac=tf))
        line, sh = _row(f"frac={tf:.2f}", m)
        print(line)
        tf_sh.append(sh)
    print(f"  VERDICT: {_verdict(tf_sh)}")

    print(f"\n  -- rebal_every (holding days), tercile_frac={TERCILE_FRAC:.2f} --")
    print(_HDR)
    re_sh = []
    for re in [1, 7, 14, 30]:
        m = daily_metrics(_pnl(panel, base_score, rebal_every=re))
        line, sh = _row(f"rebal={re}d", m)
        print(line)
        re_sh.append(sh)
    print(f"  VERDICT: {_verdict(re_sh)}")

    # 5) time-stability split for the two return-based families
    split_check(panel, "MOMENTUM",
                lambda lb: signals.momentum(panel, lb), mom_lbs, fmt=lambda v: f"lb={v}d")
    split_check(panel, "REVERSAL",
                lambda lb: signals.reversal(panel, lb), rev_lbs, fmt=lambda v: f"lb={v}d")

    print("\nDONE. Judge each family by the SHAPE of its map (plateau vs spike) and "
          "by\nwhether the edge holds in BOTH halves — never by the single best cell.")


if __name__ == "__main__":
    main()
