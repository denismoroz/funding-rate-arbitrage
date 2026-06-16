"""
blend_vs_book.py — THE DECISIVE TASK (Task D of research/cross_exchange/PLAN.md).

Standalone, the committed cross-exchange SPREAD book (HL-Binance perp-vs-perp funding
spread, trailing-direction lb90/rb21) shows an eye-popping net daily Sharpe (~13.6) — but
that number is SMOOTHNESS-INFLATED. The book is funding-only (no perp-mark / basis model
between venues, see spread.py caveat), so its realized vol is artificially tiny and its
daily pnl is highly auto-correlated (lag-1 autocorr ~0.8). The standalone risk metrics are
therefore NOT trustworthy in absolute terms.

The WHOLE POINT of this task is the OTHER question the PLAN cares about more: is
perp-vs-perp funding-spread carry a STRUCTURALLY DIFFERENT funding source than HL
spot-vs-perp basis (FRAB) — i.e. is it DECORRELATED from the existing/candidate live
sleeves (FRAB carry and XSMOM momentum)? A genuinely uncorrelated funding stream earns a
place in a carry+momentum basket even if its standalone (inflated) Sharpe is unmodeled.

THREE BOOKS, ALIGNED ON A COMMON DAILY UTC WINDOW:
  1. SPREAD (committed): the HL-Binance trail_lb90_rb21 book, rebuilt EXACTLY as Task B/C
     produced it (spread.py engine + characterize.trailing_direction_signal), then
     PROVENANCE-asserted bit-exact against characterize_committed_pnl.csv (sum/len).
  2. XSMOM: survivorship.run_book(build_pt_panel(core_coins)) — the canonical cross-sec
     momentum-ensemble daily pnl. The research proxy for the live XSMOM sleeve.
  3. FRAB-PROXY (HL basis carry STAND-IN): there is NO clean live FRAB series in research,
     so we build a SIMPLE, deliberately-crude HL-only funding-harvest book and LABEL it a
     PROXY. Per coin per day: hold the funding-positive side of HL funding (causal: sign of
     the TRAILING HL funding), collect funding[t], minus a modest single-venue HL taker cost
     on rebalance. funding-only, delta-neutral, no price leg — the SAME spirit as FRAB carry
     (harvest HL funding) but a rough stand-in, NOT real FRAB. Its ROLE here is a correlation
     reference for "HL carry", not a polished strategy. The REAL FRAB⟂ measurement is the
     LIVE checkpoint (~2026-07-16, memory project_riskparity_checkpoint).

WHAT WE COMPUTE (mirrors trend_following/blend_vs_xsmom.py):
  1. Correlation SPREAD⟂XSMOM and SPREAD⟂FRAB-proxy (Pearson + Spearman + rolling-90d:
     mean/range, %|corr|<0.3). Plus XSMOM⟂FRAB-proxy for context. THE HEADLINE NUMBERS.
  2. Risk-parity (inverse-vol) blends SPREAD+FRAB-proxy and SPREAD+XSMOM vs each leg, PLUS
     equal-weight blends as a sanity alternative. Honest daily metrics (metrics_daily,
     sqrt365).
  3. Crisis-alpha: how SPREAD behaves in the deepest drawdown windows of XSMOM and of the
     FRAB-proxy.

HONESTY / CAVEATS (also in JSON):
  - metrics_daily (PPY=365, sqrt365) for ALL absolute levels. No harness hourly annualization.
  - SMOOTHNESS ARTIFACT: the SPREAD book is funding-only with no basis risk → its vol is
    artificially tiny and lag-1 autocorr is high. An inverse-vol risk-parity weight will
    therefore MASSIVELY OVER-WEIGHT spread (1/vol blows up). We REPORT the inverse-vol weight
    it would get and FLAG it as inflated; the equal-weight blend is the sanity alternative.
    The DECORRELATION (correlation numbers) is the robust takeaway; the blend Sharpe is
    SUGGESTIVE only.
  - FRAB-proxy is a STAND-IN, not real FRAB. The real read is LIVE.
  - ~3 years, mostly up-market, no sustained bear in-sample → crisis-alpha is SUGGESTED.

Run:
  cd /Users/d/prj/funding-rate-arbitrage && \\
  PYTHONPATH=research:research/validation_harness:research/cross_sectional:research/cross_sectional/crypto:research/cross_exchange \\
  .venv/bin/python research/cross_exchange/blend_vs_book.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

# ── frozen spread engine (Task A) + Task-B trailing-direction signal ───────────
from spread import build_spread_panel, portfolio_returns_spread
import characterize as ch  # Task B: trailing_direction_signal + VENUE/TAKER/CORE config

# ── XSMOM book machinery (same as trend Task D) ────────────────────────────────
import survivorship

# ── honest daily metrics (sqrt365) ────────────────────────────────────────────
from metrics_daily import daily_metrics

_HERE = Path(__file__).parent
REPO = _HERE.parents[1]

ROLL = 90  # rolling window (days) for rolling correlation
PPY = 365

# Committed SPREAD config (from characterize.json → committed). Imported/mirrored.
COMMITTED_PAIR = ("HL", "Binance")
COMMITTED_LB = 90
COMMITTED_RB = 21
CORE_COINS = ch.CORE_COINS  # [BTC,ETH,SOL,AVAX,LINK,AAVE,DOGE,ARB,OP,MATIC]

# FRAB-proxy params: causal trailing-mean direction on HL funding, held between weekly
# rebalances. Single-venue HL taker on rebalance. Deliberately simple.
FRAB_LOOKBACK_DAYS = 30   # trailing-mean window on daily-summed HL funding
FRAB_REBAL_DAYS = 7       # weekly rebalance (matches XSMOM REBAL spirit)
HL_TAKER_BPS = 3.5        # single-venue HL taker (one leg, basis carry is one-sided)
HL_SLIP_BPS = 0.2


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
    """Full-sample inverse-vol weights, normalized to sum 1."""
    va, vb = a.std(ddof=0), b.std(ddof=0)
    wa, wb = 1.0 / va, 1.0 / vb
    s = wa + wb
    return wa / s, wb / s


def lag1_autocorr(s: pd.Series) -> float:
    s = s.dropna()
    if len(s) < 3:
        return float("nan")
    return float(s.autocorr(lag=1))


def max_drawdown_episodes(pnl: pd.Series, top_n: int = 5) -> list[dict]:
    """Top peak-to-trough drawdown episodes of a compounded-equity curve."""
    r = pnl.dropna()
    if len(r) < 2:
        return []
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
            "depth_pct": round(depth * 100, 4),
            "n_days": int(trough_i - peak_i + 1),
        })
    return out


def cum_pnl_over(pnl: pd.Series, start: str, end: str) -> float:
    """Compounded pnl of a book over [start, end] inclusive (fraction)."""
    win = pnl.loc[start:end].dropna()
    if len(win) < 1:
        return float("nan")
    return float(np.prod(1.0 + win.values) - 1.0)


# ══════════════════════════════════════════════════════════════════════════════
# Book builders
# ══════════════════════════════════════════════════════════════════════════════

def build_spread_book() -> pd.Series:
    """Rebuild the committed SPREAD book EXACTLY as Task B/C produced it.

    HL-Binance pair, core coins, trailing-direction lb90/rb21 signal through the FROZEN
    spread.portfolio_returns_spread engine. Returns a daily net pnl Series.
    """
    a, b = COMMITTED_PAIR
    panel = build_spread_panel(ch.VENUE[a], ch.VENUE[b], CORE_COINS)
    spread = panel["spread"]
    pos = ch.trailing_direction_signal(
        spread, lookback_periods=COMMITTED_LB, rebalance_periods=COMMITTED_RB)
    pnl = portfolio_returns_spread(
        pos, spread, taker_a_bps=ch.TAKER[a], taker_b_bps=ch.TAKER[b], slip_bps=ch.SLIP)
    return pnl


def build_frab_proxy(panel: dict) -> pd.Series:
    """HL-only funding-harvest book — a CRUDE STAND-IN for FRAB carry (NOT real FRAB).

    Construction (deliberately simple, causal, labelled a PROXY):
      - Source = panel["funding"]: daily-summed HL funding per coin (same HL funding the
        XSMOM panel carries; survivorship._daily_funding resamples 1h→daily-sum). This is
        the natural daily-grid HL-carry reference and aligns 1-for-1 with the XSMOM book.
      - Direction (CAUSAL, no look-ahead): for each coin, the held position is
        sign( rolling-mean(funding, FRAB_LOOKBACK_DAYS).shift(1) ) — the trailing funding
        regime up to t-1 ONLY. Updated only on weekly rebalance bars (every FRAB_REBAL_DAYS),
        forward-filled in between (caps turnover, mirrors XSMOM weekly rebal). This harvests
        the funding-positive side: when HL funding has been positive, you short-perp /
        long-spot to COLLECT funding (basis carry); sign captures the funding-positive side
        generically.
      - Per-coin per-day pnl = position[t] · funding[t] (held the carry side INTO day t;
        the position was chosen from funding ≤ t-1 → no look-ahead). funding-only,
        delta-neutral, NO price leg — same honesty ceiling as the spread engine.
      - Cost: single-venue HL taker + slip on each |Δposition| (one-sided basis trade =
        ONE venue, unlike the cross-venue spread's two legs). Charged on rebalance changes.
      - Equal-weight: book = per-day MEAN of per-coin net pnl over coins present that day.

    LABEL: this is a PROXY for HL carry, a correlation REFERENCE only. Real FRAB⟂ is the
    LIVE checkpoint (~2026-07-16, project_riskparity_checkpoint).
    """
    funding = panel["funding"]          # daily HL funding, NaN pre-listing
    present = funding.notna()

    # Causal trailing-mean direction, held between weekly rebalances.
    min_p = max(1, FRAB_LOOKBACK_DAYS // 2)
    roll = funding.rolling(FRAB_LOOKBACK_DAYS, min_periods=min_p).mean().shift(1)
    raw_dir = np.sign(roll)             # {-1,0,+1}, NaN until warmup

    n = len(funding)
    rebal_mask = np.zeros(n, dtype=bool)
    rebal_mask[::FRAB_REBAL_DAYS] = True
    gated = raw_dir.where(pd.Series(rebal_mask, index=funding.index), other=np.nan).ffill()
    pos = gated.fillna(0.0).where(present, 0.0)   # flat where coin absent / pre-warmup

    # Carry: position held INTO day t collects funding[t] (no look-ahead — pos from ≤t-1).
    carry = (pos * funding).where(present, 0.0)

    # Cost: single-venue HL taker + slip on |Δposition| (basis trade = ONE venue).
    unit_cost = (HL_TAKER_BPS + HL_SLIP_BPS) / 1e4
    dpos = pos.diff()
    dpos.iloc[0] = pos.iloc[0]          # entry from flat on first bar
    cost = dpos.abs() * unit_cost

    net = carry - cost
    n_present = present.sum(axis=1)
    book = net.where(present, np.nan).sum(axis=1) / n_present.replace(0, np.nan)
    book = book.fillna(0.0)
    book.name = "frab_proxy"
    return book


# ══════════════════════════════════════════════════════════════════════════════
# Pairwise correlation
# ══════════════════════════════════════════════════════════════════════════════

def pair_correlation(a: pd.Series, b: pd.Series, label_a: str, label_b: str) -> dict:
    """Full-sample Pearson+Spearman and rolling-90d correlation summary."""
    df = pd.concat([a, b], axis=1).dropna()
    x, y = df.iloc[:, 0], df.iloc[:, 1]
    pearson = float(x.corr(y, method="pearson"))
    spearman = float(x.corr(y, method="spearman"))
    roll = x.rolling(ROLL, min_periods=ROLL).corr(y).dropna()
    if len(roll):
        rc_mean, rc_min, rc_max = float(roll.mean()), float(roll.min()), float(roll.max())
        frac_low = float((roll.abs() < 0.3).mean())
    else:
        rc_mean = rc_min = rc_max = frac_low = float("nan")
    return {
        "pair": f"{label_a}_vs_{label_b}",
        "n": int(len(df)),
        "pearson": pearson,
        "spearman": spearman,
        "rolling_window_days": ROLL,
        "rolling_pearson_mean": rc_mean,
        "rolling_pearson_min": rc_min,
        "rolling_pearson_max": rc_max,
        "frac_windows_abs_corr_below_0p3": frac_low,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Blend (risk-parity inverse-vol + equal-weight) vs legs
# ══════════════════════════════════════════════════════════════════════════════

def blend_analysis(spread: pd.Series, other: pd.Series,
                   spread_label: str, other_label: str) -> dict:
    """Risk-parity (inverse-vol) AND equal-weight blends of SPREAD+other vs each leg."""
    df = pd.concat([spread, other], axis=1).dropna()
    s, o = df.iloc[:, 0], df.iloc[:, 1]

    w_s, w_o = static_inverse_vol_weights(s, o)
    blend_iv = w_s * s + w_o * o
    blend_eqw = 0.5 * s + 0.5 * o

    m_spread = daily_metrics(s)
    m_other = daily_metrics(o)
    m_iv = daily_metrics(blend_iv)
    m_eqw = daily_metrics(blend_eqw)

    return {
        "other_leg": other_label,
        "n": int(len(df)),
        "inverse_vol_weights_FLAGGED_inflated": {
            "w_spread": _num(w_s),
            "w_other": _num(w_o),
            "flag": (
                f"w_spread={w_s:.3f} is INFLATED: the SPREAD book is funding-only "
                f"(no basis risk) so its vol is artificially tiny → 1/vol over-weights it. "
                f"Read the inverse-vol blend as SUGGESTIVE only; the equal-weight blend is "
                f"the sanity alternative and the DECORRELATION number is the robust takeaway."
            ),
        },
        "metrics": {
            "spread": {k: _num(v) for k, v in m_spread.items()},
            other_label: {k: _num(v) for k, v in m_other.items()},
            "blend_inverse_vol": {k: _num(v) for k, v in m_iv.items()},
            "blend_equal_weight": {k: _num(v) for k, v in m_eqw.items()},
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# Crisis-alpha
# ══════════════════════════════════════════════════════════════════════════════

def crisis_alpha(spread: pd.Series, other: pd.Series, other_label: str) -> dict:
    """SPREAD behavior during the other book's deepest drawdown episodes."""
    dds = max_drawdown_episodes(other, top_n=5)
    rows = []
    n_holds = 0
    for ep in dds:
        o_cum = cum_pnl_over(other, ep["peak_date"], ep["trough_date"])
        s_cum = cum_pnl_over(spread, ep["peak_date"], ep["trough_date"])
        holds = bool(not np.isnan(s_cum) and s_cum >= 0)
        if holds:
            n_holds += 1
        rows.append({
            "peak_date": ep["peak_date"],
            "trough_date": ep["trough_date"],
            f"{other_label}_dd_pct": ep["depth_pct"],
            "n_days": ep["n_days"],
            f"{other_label}_cum_pnl_pct": round(o_cum * 100, 4),
            "spread_cum_pnl_pct": round(s_cum * 100, 4) if not np.isnan(s_cum) else None,
            "spread_flat_to_positive": holds,
        })
    return {"other_leg": other_label, "windows": rows,
            "spread_flat_to_positive_in_n": [n_holds, len(rows)]}


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> dict:
    print("=" * 96)
    print("TASK D — DECISIVE: cross-exchange SPREAD ⟂ XSMOM & FRAB-proxy decorrelation")
    print("=" * 96)

    # ── [1] SPREAD book (committed) — rebuild + provenance assert vs csv ─────────
    print("\n[1] Rebuilding committed SPREAD book (HL-Binance trail_lb90_rb21)...")
    spread_pnl = build_spread_book()

    csv_path = _HERE / "characterize_committed_pnl.csv"
    ref = pd.read_csv(csv_path, index_col=0)
    ref.index = pd.to_datetime(ref.index, utc=True)
    ref_pnl = ref["spread_net"]

    # Provenance: sum/len bit-exact + max abs diff on aligned index.
    prov_sum = float(spread_pnl.sum())
    prov_len = int(len(spread_pnl))
    csv_sum, csv_len = float(ref_pnl.sum()), int(len(ref_pnl))
    aligned = pd.concat([spread_pnl.rename("rebuilt"), ref_pnl.rename("csv")], axis=1)
    max_abs_diff = float((aligned["rebuilt"] - aligned["csv"]).abs().max())
    assert prov_len == csv_len, f"PROVENANCE len: {prov_len} != csv {csv_len}"
    assert np.isclose(prov_sum, csv_sum, atol=1e-9), \
        f"PROVENANCE sum: {prov_sum} != csv {csv_sum}"
    assert max_abs_diff < 1e-9, f"PROVENANCE max abs diff {max_abs_diff:.2e} >= 1e-9"
    print(f"    Provenance PASSED: sum={prov_sum:.17g} (csv {csv_sum:.17g}), "
          f"len={prov_len} (csv {csv_len}), max abs diff {max_abs_diff:.2e} < 1e-9")
    print(f"    SPREAD span: {spread_pnl.index.min().date()} → "
          f"{spread_pnl.index.max().date()} ({prov_len} days)")
    sp_autocorr = lag1_autocorr(spread_pnl)
    print(f"    SPREAD lag-1 autocorr = {sp_autocorr:+.3f}  "
          f"(SMOOTHNESS ARTIFACT — funding-only, no basis risk → vol inflated-tiny)")

    # ── [2] XSMOM book + panel ──────────────────────────────────────────────────
    print("\n[2] Building XSMOM book via survivorship.run_book(build_pt_panel(core))...")
    panel = survivorship.build_pt_panel(CORE_COINS)
    xsmom_pnl = survivorship.run_book(panel)
    xs_coins = panel["coins"]
    print(f"    XSMOM coins: {xs_coins}")
    print(f"    XSMOM span: {xsmom_pnl.dropna().index.min().date()} → "
          f"{xsmom_pnl.dropna().index.max().date()} ({len(xsmom_pnl.dropna())} days)")
    if set(xs_coins) != set(CORE_COINS):
        missing = sorted(set(CORE_COINS) - set(xs_coins))
        print(f"    NOTE coin mismatch: {missing} absent in XSMOM data dir "
              f"(survivorship has no price cache) → {len(xs_coins)} coins used.")

    # ── [3] FRAB-proxy book (HL carry STAND-IN) ─────────────────────────────────
    print("\n[3] Building FRAB-PROXY (HL-only funding-harvest STAND-IN, NOT real FRAB)...")
    frab_pnl = build_frab_proxy(panel)
    print(f"    FRAB-proxy: lookback {FRAB_LOOKBACK_DAYS}d, weekly rebal, "
          f"single-venue HL taker {HL_TAKER_BPS}bps+slip {HL_SLIP_BPS}bps")
    m_frab_full = daily_metrics(frab_pnl.dropna())
    print(f"    FRAB-proxy (full): Sharpe {m_frab_full['sharpe']:+.2f}  "
          f"ann {100*m_frab_full['ann']:+.2f}%  maxDD {100*m_frab_full['maxdd']:.2f}%  "
          f"lag-1 autocorr {lag1_autocorr(frab_pnl):+.3f}")
    print("    LABEL: PROXY for HL carry, a correlation reference only — NOT real FRAB. "
          "Real FRAB⟂ = LIVE checkpoint ~2026-07-16.")

    # ── Align all three on a common daily window ────────────────────────────────
    common = (spread_pnl.dropna().index
              .intersection(xsmom_pnl.dropna().index)
              .intersection(frab_pnl.dropna().index))
    s = spread_pnl.loc[common]
    x = xsmom_pnl.loc[common]
    f = frab_pnl.loc[common]
    cw = {"start": str(common.min().date()), "end": str(common.max().date()),
          "n_days": int(len(common))}
    print(f"\n    Common window (all 3 books): {cw['start']} → {cw['end']} "
          f"({cw['n_days']} days)")

    # ════════════════════════════════════════════════════════════════════════
    # CORRELATION — the headline
    # ════════════════════════════════════════════════════════════════════════
    corr_sx = pair_correlation(s, x, "spread", "xsmom")
    corr_sf = pair_correlation(s, f, "spread", "frab_proxy")
    corr_xf = pair_correlation(x, f, "xsmom", "frab_proxy")

    print("\n" + "=" * 96)
    print("CORRELATION  (THE HEADLINE — decorrelation of cross-venue spread carry)")
    print("=" * 96)
    hdr = (f"  {'pair':<26}{'Pearson':>9}{'Spearman':>10}"
           f"{'roll90 mean':>13}{'roll90 min':>12}{'roll90 max':>12}{'%|c|<0.3':>10}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for c in (corr_sx, corr_sf, corr_xf):
        print(f"  {c['pair']:<26}{c['pearson']:>+9.4f}{c['spearman']:>+10.4f}"
              f"{c['rolling_pearson_mean']:>+13.4f}{c['rolling_pearson_min']:>+12.4f}"
              f"{c['rolling_pearson_max']:>+12.4f}"
              f"{100*c['frac_windows_abs_corr_below_0p3']:>9.1f}%")

    # ════════════════════════════════════════════════════════════════════════
    # RISK-PARITY + EQUAL-WEIGHT BLENDS
    # ════════════════════════════════════════════════════════════════════════
    blend_sf = blend_analysis(s, f, "spread", "frab_proxy")
    blend_sx = blend_analysis(s, x, "spread", "xsmom")

    print("\n" + "=" * 96)
    print("BLEND vs LEGS — honest daily metrics (sqrt365). Inverse-vol weight FLAGGED inflated.")
    print("=" * 96)

    def print_blend(ba: dict, title: str):
        print(f"\n  {title}")
        w = ba["inverse_vol_weights_FLAGGED_inflated"]
        print(f"    inverse-vol weights: w_spread={w['w_spread']:.4f}  "
              f"w_other={w['w_other']:.4f}   [FLAGGED INFLATED — see note]")
        print(f"    {'Book':<26}{'Sharpe':>9}{'Ann%':>9}{'Vol%':>9}"
              f"{'MaxDD%':>9}{'Calmar':>9}{'Hit%':>7}")
        print("    " + "-" * 88)
        order = ["spread", ba["other_leg"], "blend_inverse_vol", "blend_equal_weight"]
        labels = {"spread": "SPREAD (committed)", ba["other_leg"]: ba["other_leg"].upper(),
                  "blend_inverse_vol": "BLEND inv-vol [inflated]",
                  "blend_equal_weight": "BLEND equal-weight"}
        for key in order:
            m = ba["metrics"][key]
            cal = m.get("calmar")
            cal_s = f"{cal:+.2f}" if cal is not None else "nan"
            print(f"    {labels[key]:<26}{m['sharpe']:>+9.3f}{100*m['ann']:>+9.2f}"
                  f"{100*m['vol_ann']:>9.2f}{100*m['maxdd']:>9.2f}{cal_s:>9}"
                  f"{100*m['hit']:>7.1f}")

    print_blend(blend_sf, "SPREAD + FRAB-proxy  (the carry-sleeve question)")
    print_blend(blend_sx, "SPREAD + XSMOM       (the momentum-sleeve question)")
    print("\n    NOTE: the inverse-vol blend OVER-weights SPREAD because its funding-only vol "
          "is\n    artificially tiny (smoothness artifact). Read it as SUGGESTIVE; the "
          "equal-weight blend\n    is the sanity check. The DECORRELATION numbers above are "
          "the robust takeaway.")

    # ════════════════════════════════════════════════════════════════════════
    # CRISIS-ALPHA
    # ════════════════════════════════════════════════════════════════════════
    crisis_x = crisis_alpha(s, x, "xsmom")
    crisis_f = crisis_alpha(s, f, "frab_proxy")

    print("\n" + "=" * 96)
    print("CRISIS-ALPHA — SPREAD's behavior during each other book's deepest drawdowns")
    print("=" * 96)

    def print_crisis(ca: dict):
        ol = ca["other_leg"]
        print(f"\n  During {ol.upper()}'s deepest drawdowns:")
        print(f"    {'peak':>12}{'trough':>14}{ol+' DD%':>14}{'days':>6}"
              f"{ol+' cum%':>13}{'SPREAD cum%':>13}")
        for w in ca["windows"]:
            sc = w["spread_cum_pnl_pct"]
            sc_s = f"{sc:+.3f}" if sc is not None else "  n/a"
            print(f"    {w['peak_date']:>12}{w['trough_date']:>14}"
                  f"{w[ol+'_dd_pct']:>+14.3f}{w['n_days']:>6}"
                  f"{w[ol+'_cum_pnl_pct']:>+13.3f}{sc_s:>13}"
                  f"   {'<- SPREAD held' if w['spread_flat_to_positive'] else '<- SPREAD bled'}")
        nh, nt = ca["spread_flat_to_positive_in_n"]
        print(f"    → SPREAD flat-to-positive in {nh}/{nt} of {ol.upper()}'s deepest windows.")

    print_crisis(crisis_x)
    print_crisis(crisis_f)

    # ════════════════════════════════════════════════════════════════════════
    # VERDICT
    # ════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 96)
    print("VERDICT — is cross-exchange SPREAD carry a GENUINE DIVERSIFIER?")
    print("=" * 96)

    # Decision logic: the question is DECORRELATION (independent of inflated standalone
    # Sharpe). Headline = SPREAD⟂FRAB-proxy: if LOW → structurally distinct funding source
    # worth a LIVE decorrelation test. If HIGH (HL funding drives both legs) → not a
    # diversifier. SPREAD⟂XSMOM matters too (momentum sleeve).
    sf_p = corr_sf["pearson"]
    sf_roll = corr_sf["rolling_pearson_mean"]
    sf_frac = corr_sf["frac_windows_abs_corr_below_0p3"]
    sx_p = corr_sx["pearson"]
    sx_roll = corr_sx["rolling_pearson_mean"]
    sx_frac = corr_sx["frac_windows_abs_corr_below_0p3"]

    sf_low = abs(sf_p) < 0.3
    sf_robust = sf_frac >= 0.5
    sx_low = abs(sx_p) < 0.3
    sx_robust = sx_frac >= 0.5

    if sf_low and sx_low:
        call = "DIVERSIFIER (SIM) — NEEDS-LIVE-CONFIRMATION"
    elif sf_low or sx_low:
        call = "PARTIAL DIVERSIFIER — NEEDS-LIVE-CONFIRMATION"
    else:
        call = "NOT A DIVERSIFIER"

    verdict_text = (
        f"DECORRELATION is the decisive read (independent of the SPREAD book's smoothness-"
        f"inflated standalone Sharpe). SPREAD ⟂ FRAB-proxy (HL carry): Pearson {sf_p:+.3f}, "
        f"rolling-{ROLL}d mean {sf_roll:+.3f}, |corr|<0.3 on {100*sf_frac:.0f}% of windows — "
        f"{'LOW (structurally distinct funding source)' if sf_low else 'HIGH (HL funding drives both → NOT distinct)'}. "
        f"SPREAD ⟂ XSMOM: Pearson {sx_p:+.3f}, rolling mean {sx_roll:+.3f}, |corr|<0.3 on "
        f"{100*sx_frac:.0f}% of windows — {'LOW' if sx_low else 'HIGH'}. "
        f"XSMOM ⟂ FRAB-proxy (context): Pearson {corr_xf['pearson']:+.3f}. "
        f"BLEND CAVEAT: the inverse-vol risk-parity weight MASSIVELY over-weights SPREAD "
        f"(w_spread≈{blend_sf['inverse_vol_weights_FLAGGED_inflated']['w_spread']:.2f} vs "
        f"FRAB-proxy) because the funding-only SPREAD book has artificially tiny vol "
        f"(lag-1 autocorr {sp_autocorr:+.2f}); the blend Sharpe is therefore SUGGESTIVE only, "
        f"the equal-weight blend is the sanity alternative, and the correlation numbers are "
        f"the robust takeaway. DECISION: {call}. "
        f"{'SPREAD is a structurally distinct (perp-vs-perp) funding source, low-correlated to HL spot-vs-perp basis carry → worth a LIVE decorrelation test as a carry diversifier.' if sf_low else 'SPREAD is highly correlated with HL carry (HL funding is the common driver of both legs) → it is NOT a genuine diversifier.'} "
        f"CAVEAT: this is SIM with a FRAB PROXY (HL-only funding-harvest stand-in, NOT real "
        f"FRAB), funding-only with NO basis-risk model, ~3y mostly-up-market. The real read "
        f"is the LIVE FRAB⟂spread checkpoint (project_riskparity_checkpoint, ~2026-07-16)."
    )
    print(f"\n  DECISION: {call}\n")
    print(f"  {verdict_text}")

    # ── Honesty caveats ──────────────────────────────────────────────────────────
    caveats = [
        "metrics_daily (PPY=365, sqrt365) for ALL absolute levels. No harness hourly "
        "annualization anywhere in this file.",
        "SMOOTHNESS ARTIFACT: the committed SPREAD book is funding-only (spread.py models NO "
        f"perp-mark/basis between venues), so its realized vol is artificially tiny and lag-1 "
        f"autocorr is high ({sp_autocorr:+.2f}). An inverse-vol risk-parity weight therefore "
        "MASSIVELY over-weights SPREAD; that blend Sharpe is SUGGESTIVE only. The equal-weight "
        "blend is the sanity alternative, and the DECORRELATION (correlation numbers) is the "
        "robust, scale-invariant takeaway.",
        "FRAB-PROXY is a CRUDE STAND-IN, NOT real FRAB: an HL-only funding-harvest book "
        "(causal trailing-sign of daily-summed HL funding, weekly rebal, single-venue HL "
        "taker). Its role is a correlation REFERENCE for 'HL carry', not a polished strategy. "
        "The real FRAB⟂spread measurement is the LIVE checkpoint (project_riskparity_"
        "checkpoint, ~2026-07-16).",
        "CAUSAL / no look-ahead: SPREAD uses characterize.trailing_direction_signal "
        "(rolling.shift(1) + rebalance ffill) through the frozen engine; FRAB-proxy uses "
        "rolling(funding).shift(1) trailing sign, held between weekly rebalances.",
        "FUNDING-ONLY honesty ceiling: neither SPREAD nor FRAB-proxy models price/basis/"
        "liquidation risk. Real basis risk (cross-venue price divergence, non-atomic "
        "execution) is NOT captured.",
        "Coin universe: SPREAD = HL∩Binance core; XSMOM/FRAB-proxy = survivorship PT panel "
        "(OP dropped — no price cache in the crypto data dir). Books aligned on the common "
        "daily UTC window only.",
        "~3 years (2023-06 →), predominantly an up-market with no sustained bear in-sample → "
        "crisis-alpha is SUGGESTED, not proven.",
    ]
    print("\n[Honesty Caveats]")
    for i, c in enumerate(caveats, 1):
        print(f"  {i}. {c}")

    # ── JSON ──────────────────────────────────────────────────────────────────────
    out = {
        "test": "cross_exchange_spread_decorrelation_vs_frab_proxy_and_xsmom",
        "task": "Task D of research/cross_exchange/PLAN.md (DECISIVE)",
        "description": (
            "Decorrelation of the committed cross-exchange SPREAD book (HL-Binance perp-vs-"
            "perp funding spread, trail lb90/rb21) against the XSMOM cross-sec momentum book "
            "and a FRAB-PROXY (HL-only funding-harvest stand-in for HL basis carry), plus "
            "risk-parity (inverse-vol) and equal-weight blends and crisis-alpha. Decides "
            "whether cross-venue spread carry is a STRUCTURALLY DISTINCT, decorrelated funding "
            "source worth a LIVE diversification test, INDEPENDENT of its smoothness-inflated "
            "standalone Sharpe."
        ),
        "spread_provenance": {
            "rebuilt_sum": prov_sum,
            "rebuilt_len": prov_len,
            "csv_sum": csv_sum,
            "csv_len": csv_len,
            "max_abs_diff": max_abs_diff,
            "passed": True,
            "csv": str(csv_path.relative_to(REPO)),
            "note": ("SPREAD book rebuilt via spread.py engine + characterize."
                     "trailing_direction_signal, asserted bit-exact (sum/len + max abs diff "
                     "< 1e-9) vs characterize_committed_pnl.csv."),
        },
        "config": {
            "committed_spread_pair": "HL-Binance",
            "committed_spread_config": "trail_lb90_rb21",
            "spread_lag1_autocorr": _num(sp_autocorr),
            "core_coins": CORE_COINS,
            "xsmom_coins_used": list(xs_coins),
            "frab_proxy": {
                "lookback_days": FRAB_LOOKBACK_DAYS,
                "rebalance_days": FRAB_REBAL_DAYS,
                "hl_taker_bps": HL_TAKER_BPS,
                "hl_slip_bps": HL_SLIP_BPS,
                "construction": (
                    "HL-only funding-harvest: per coin, hold sign(rolling-mean(daily HL "
                    "funding, 30d).shift(1)) updated weekly (ffill between), pnl = "
                    "position·funding minus single-venue HL taker+slip on rebalance, "
                    "equal-weight across present coins, daily. A CRUDE PROXY for HL carry "
                    "(NOT real FRAB)."
                ),
                "LABEL": "PROXY for HL carry — correlation reference only, NOT real FRAB",
            },
            "rolling_window_days": ROLL,
            "annualization": "metrics_daily PPY=365 sqrt(365) (honest absolute ONLY)",
        },
        "common_window": cw,
        "frab_proxy_standalone_full_sample": {k: _num(v) for k, v in m_frab_full.items()},
        "correlation": {
            "spread_vs_xsmom": corr_sx,
            "spread_vs_frab_proxy": corr_sf,
            "xsmom_vs_frab_proxy": corr_xf,
            "headline": ("THE decisive numbers: SPREAD⟂FRAB-proxy (is cross-venue spread "
                         "carry a distinct funding source vs HL basis carry?) and SPREAD⟂XSMOM."),
        },
        "blend": {
            "spread_plus_frab_proxy": blend_sf,
            "spread_plus_xsmom": blend_sx,
            "inverse_vol_flag": ("Inverse-vol weights OVER-weight SPREAD because its "
                                 "funding-only vol is artificially tiny — SUGGESTIVE only; "
                                 "equal-weight is the sanity alternative; correlation is robust."),
        },
        "crisis_alpha": {
            "spread_in_xsmom_drawdowns": crisis_x,
            "spread_in_frab_proxy_drawdowns": crisis_f,
        },
        "decision_rule": (
            "The DECISIVE question is DECORRELATION (independent of the SPREAD book's "
            "smoothness-inflated standalone Sharpe): if SPREAD⟂FRAB-proxy is LOW (|corr|<0.3) "
            "→ cross-venue spread carry is a structurally distinct funding source worth a LIVE "
            "decorrelation test. If HIGH (HL funding drives both legs) → NOT a diversifier."
        ),
        "verdict_call": call,
        "verdict": verdict_text,
        "honesty_caveats": caveats,
    }

    out_path = _HERE / "blend_vs_book.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=_num))
    print(f"\nJSON written to {out_path}")
    return out


if __name__ == "__main__":
    main()
