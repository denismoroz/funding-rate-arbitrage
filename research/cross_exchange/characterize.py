"""
Task B — Characterize cross-exchange funding-spread carry (sanity, no harness).

Reads the FROZEN Task A engine (`spread.py`) and characterizes spread carry across
the 3 venue pairs HL-Binance / HL-Bybit / Binance-Bybit (plus optional short-history
secondary pairs HL-Backpack / HL-Drift), then picks ONE committed (pair, threshold,
hysteresis) config for Task C.

THE central question (PLAN.md): the Task A smoke showed thr=0/hys=0 sign-chasing nets
~ -27%/yr — double costs x churn obliterate the gross ~ +8-12%/yr. So we sweep configs
that REDUCE turnover (high threshold = sticky, plus a full-sample-direction static
baseline as an in-sample DIAGNOSTIC CEILING) and ask whether ANY config is net-positive
after double costs.

Honest absolute levels ONLY via metrics_daily.daily_metrics (PPY=365, sqrt365). We never
annualize 8h/hourly. Signal/portfolio come ONLY from the Task A engine — we do not
reimplement carry/cost. The static baseline is the ONE exception allowed by PLAN: a
constant sign(full-sample mean_spread) position fed through portfolio_returns_spread; it
peeks at the full-sample mean to pick direction, so it is labelled in-sample-direction
(a ceiling, NOT deployable).

Run:
  PYTHONPATH=research:research/validation_harness:research/cross_sectional:\
research/cross_sectional/crypto:research/cross_exchange \
  .venv/bin/python research/cross_exchange/characterize.py
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

from spread import (
    build_spread_panel,
    spread_signal,
    portfolio_returns_spread,
)
from metrics_daily import daily_metrics

# ── Config ───────────────────────────────────────────────────────────────────

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "research"

# Core universe = HL ∩ Binance ∩ Bybit with ~3yr history. build_spread_panel
# silently drops coins missing in either venue, so we pass the wider list and
# record what actually survives per pair.
CORE_COINS = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE", "ARB", "OP", "MATIC"]

# Per-venue perp taker bps (PLAN / multi_exchange.py), slip 0.2 each leg.
TAKER = {
    "HL": 3.5,
    "Binance": 5.0,
    "Bybit": 5.5,
    "Backpack": 5.0,   # backpack taker ~ binance-tier (secondary, flagged)
    "Drift": 5.0,      # drift taker approx (secondary, flagged)
}
SLIP = 0.2

# Venue dir + native funding interval (h). HL/Drift native 1h (sum 8 -> 8h bucket);
# Binance/Bybit/Backpack native 8h.
VENUE = {
    "HL": (DATA / "data", 1),
    "Binance": (DATA / "data_binance", 8),
    "Bybit": (DATA / "data_bybit", 8),
    "Backpack": (DATA / "data_backpack", 8),
    "Drift": (DATA / "data_drift", 1),
}

# Core pairs (committed candidate space) + secondary short-history pairs.
CORE_PAIRS = [("HL", "Binance"), ("HL", "Bybit"), ("Binance", "Bybit")]
SECONDARY_PAIRS = [("HL", "Backpack"), ("HL", "Drift")]

PERIODS_PER_YEAR = 3 * 365  # 8h periods/year, for annualizing per-period spread stats

# Per-period band thresholds (annualized %/yr equivalents -> per-period level).
# 5%/yr ~ 4.57e-5, 10%/yr ~ 9.13e-5, 20%/yr ~ 1.83e-4 per 8h period.
BAND_5PCT = 0.05 / PERIODS_PER_YEAR
BAND_10PCT = 0.10 / PERIODS_PER_YEAR
BAND_20PCT = 0.20 / PERIODS_PER_YEAR

# Signal config sweep: (label, threshold, hysteresis).
# churny (sign-chase) -> sticky (high threshold, rarely flips; low hyst, rarely exits).
CONFIG_SWEEP = [
    ("churn_0_0", 0.0, 0.0),
    ("band5_thr_hys0", BAND_5PCT, 0.0),
    ("sticky_thr10_hys5", BAND_10PCT, BAND_5PCT),
    ("sticky_thr20_hys5", BAND_20PCT, BAND_5PCT),
]

# ── Trailing-direction sweep (causal, the DEPLOYABLE bridge to the static ceiling) ─
# Task B found the instantaneous-spread hysteresis signal CHURNS (~1170 flips/yr) and
# loses to double costs (net Sharpe ~ -1.6). The whole loss is causal-signal churn, not
# absence of edge: the static full-sample-mean-sign book nets ~ +9.4 Sharpe / +6.3%/yr at
# turnover ~6/yr — but that uses the FULL-SAMPLE mean (look-ahead), so it is a CEILING.
# The obvious deployable causal bridge is a TRAILING-mean direction that HOLDS between
# rebalances: direction = sign(rolling-mean of spread up to t-1), updated only on
# rebalance bars. This caps turnover while staying strictly causal.
#   lookback_periods (8h-periods; 3/day) -> ~30/60/90 days.
#   rebalance_periods -> ~weekly (21 ~ 7d) / ~monthly (63 ~ 21d).
TRAIL_LOOKBACKS = [90, 180, 270]
TRAIL_REBALANCES = [21, 63]


# ── Per-coin spread statistics ───────────────────────────────────────────────

def per_coin_stats(spread: pd.DataFrame) -> dict:
    """Per-coin: mean/vol spread (annualized %), sign-persistence, %-time over bands."""
    stats = {}
    for c in spread.columns:
        s = spread[c].dropna()
        if len(s) < 30:
            stats[c] = {"n": int(len(s)), "note": "too_short"}
            continue
        mean_ann = float(s.mean() * PERIODS_PER_YEAR * 100)
        vol_ann = float(s.std(ddof=0) * np.sqrt(PERIODS_PER_YEAR) * 100)
        # sign-persistence: fraction of consecutive non-zero pairs with same sign.
        sg = np.sign(s.to_numpy())
        prev, cur = sg[:-1], sg[1:]
        valid = (prev != 0) & (cur != 0)
        sign_persist = float((prev[valid] == cur[valid]).mean()) if valid.any() else float("nan")
        # dominant-sign share (which venue richer, structurally).
        dom = max((sg > 0).mean(), (sg < 0).mean())
        a = s.abs().to_numpy()
        stats[c] = {
            "n": int(len(s)),
            "mean_ann_pct": mean_ann,
            "vol_ann_pct": vol_ann,
            "sign_persistence": sign_persist,
            "dominant_sign_share": float(dom),
            "pct_over_0": float((a > 0).mean()),
            "pct_over_band5pct": float((a > BAND_5PCT).mean()),
            "pct_over_band10pct": float((a > BAND_10PCT).mean()),
        }
    return stats


# ── Book economics ───────────────────────────────────────────────────────────

def turnover_per_year(positions: pd.DataFrame, spread: pd.DataFrame) -> float:
    """Sum of |Δposition| across coins per period, scaled to per-year.

    Mirrors portfolio_returns_spread's cost basis: present-masked positions, first
    bar counted as entry from flat. Returns sum(|Δpos|) / years (8h grid)."""
    present = spread.notna()
    pos = positions.reindex_like(spread).where(present, 0.0)
    dpos = pos.diff()
    dpos.iloc[0] = pos.iloc[0]
    total_turn = float(dpos.abs().to_numpy().sum())
    n_periods = len(spread)
    years = n_periods / PERIODS_PER_YEAR if n_periods else float("nan")
    return total_turn / years if years and years > 0 else float("nan")


def static_positions(spread: pd.DataFrame) -> pd.DataFrame:
    """In-sample-direction STATIC book: constant position = sign(full-sample mean
    spread) per coin, held while the coin is present (flat on NaN, so the gap-reset
    cost convention of the engine still applies). DIAGNOSTIC CEILING ONLY (peeks at
    full-sample mean to pick direction) — not a deployable causal signal."""
    mean_sign = np.sign(spread.mean(axis=0))  # full-sample mean per coin
    pos = pd.DataFrame(
        np.broadcast_to(mean_sign.to_numpy(), spread.shape).copy(),
        index=spread.index, columns=spread.columns,
    ).astype(float)
    pos = pos.where(spread.notna(), 0.0)  # flat where coin absent
    return pos


def trailing_direction_signal(spread: pd.DataFrame, lookback_periods: int,
                              rebalance_periods: int) -> pd.DataFrame:
    """CAUSAL trailing-mean direction signal that HOLDS between rebalances.

    Deployable bridge between the churny instantaneous-spread hysteresis signal and the
    (look-ahead) static full-sample-mean ceiling. Strictly causal, NO look-ahead:

      raw_dir[t,c] = sign( spread[c].rolling(lookback_periods, min_periods=lb//2)
                                    .mean().shift(1) )

    The `.shift(1)` is the key: the rolling mean for bar t is computed over spread up to
    t-1 ONLY (the window ending at t-1), so the direction used to hold INTO bar t never
    peeks at spread[t]. This composes correctly with portfolio_returns_spread's own lag
    (carry[t] = position[t-1]·spread[t]): the position established at t was itself chosen
    from spread <= t-1.

    REBALANCE GATING: the position only UPDATES to the current raw_dir on rebalance bars
    (every `rebalance_periods` 8h-periods, counted from the panel start). Between
    rebalances it HOLDS the last set direction. Implemented by sampling raw_dir at the
    rebalance bars and forward-filling. This caps turnover well below the instantaneous
    signal.

    NaN spread (pre-listing / data gap) -> flat 0 for that coin at that bar (the position
    is zeroed wherever the coin is absent, so a held direction never bleeds across a gap
    and the engine's gap-reset cost convention still applies).

    Returns a {-1.0, 0.0, +1.0} DataFrame shaped like spread; feed straight to the frozen
    portfolio_returns_spread.
    """
    min_p = max(1, lookback_periods // 2)
    # Rolling mean over the lookback window, then shift(1) -> window ends at t-1 (causal).
    roll_mean = spread.rolling(lookback_periods, min_periods=min_p).mean().shift(1)
    raw_dir = np.sign(roll_mean)  # {-1, 0, +1}, NaN where insufficient history

    # Rebalance gating: sample raw_dir only at rebalance bars, forward-fill in between.
    # Rebalance bars = integer positions 0, rebalance_periods, 2*rebalance_periods, ...
    n = len(spread)
    rebal_mask = np.zeros(n, dtype=bool)
    rebal_mask[::rebalance_periods] = True
    # Keep raw_dir only on rebalance bars; ffill holds the last set direction between them.
    gated = raw_dir.where(pd.Series(rebal_mask, index=spread.index), other=np.nan)
    gated = gated.ffill()

    # NaN -> flat 0: pre-warmup bars (no rolling history yet) and absent coins.
    pos = gated.fillna(0.0)
    pos = pos.where(spread.notna(), 0.0)  # coin absent this bar -> flat
    return pos.astype(float)


def book_metrics(positions: pd.DataFrame, spread: pd.DataFrame,
                 taker_a: float, taker_b: float) -> dict:
    """Gross (costs=0) and net daily metrics + turnover for a position book."""
    net_pnl = portfolio_returns_spread(positions, spread, taker_a, taker_b, slip_bps=SLIP)
    gross_pnl = portfolio_returns_spread(positions, spread, 0.0, 0.0, slip_bps=0.0)
    m_net = daily_metrics(net_pnl)
    m_gross = daily_metrics(gross_pnl)
    turn = turnover_per_year(positions, spread)
    return {
        "gross": m_gross,
        "net": m_net,
        "turnover_per_year": turn,
        "net_pnl": net_pnl,  # kept in-process for committed provenance, stripped from JSON
    }


def pnl_provenance(pnl: pd.Series) -> dict:
    """Compact provenance of a daily pnl series so Task C can verify bit-exact."""
    v = pnl.dropna()
    return {
        "sum": float(v.sum()),
        "len": int(len(v)),
        "mean": float(v.mean()),
        "std": float(v.std(ddof=0)),
        "first3_dates": [str(d.date()) for d in v.index[:3]],
        "first3_vals": [float(x) for x in v.iloc[:3]],
        "last3_dates": [str(d.date()) for d in v.index[-3:]],
        "last3_vals": [float(x) for x in v.iloc[-3:]],
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def run_pair(a: str, b: str, secondary: bool) -> dict:
    panel = build_spread_panel(VENUE[a], VENUE[b], CORE_COINS)
    spread = panel["spread"]
    coins = panel["coins"]
    if not coins:
        return {"venue_a": a, "venue_b": b, "secondary": secondary,
                "coins": [], "note": "no_overlap"}

    ta, tb = TAKER[a], TAKER[b]
    result = {
        "venue_a": a, "venue_b": b,
        "interval_a": VENUE[a][1], "interval_b": VENUE[b][1],
        "taker_a_bps": ta, "taker_b_bps": tb, "slip_bps": SLIP,
        "secondary": secondary,
        "coins": coins,
        "n_periods": int(len(spread)),
        "span": [str(spread.index[0]), str(spread.index[-1])],
        "per_coin": per_coin_stats(spread),
        "configs": {},
    }

    # Causal signal configs.
    for label, thr, hys in CONFIG_SWEEP:
        pos = spread_signal(spread, threshold=thr, hysteresis=hys)
        bm = book_metrics(pos, spread, ta, tb)
        result["configs"][label] = {
            "threshold": thr, "hysteresis": hys, "kind": "causal",
            "gross": bm["gross"], "net": bm["net"],
            "turnover_per_year": bm["turnover_per_year"],
            "net_pnl_provenance": pnl_provenance(bm["net_pnl"]),
            "_net_pnl": bm["net_pnl"],
        }

    # Trailing-direction causal sweep (the DEPLOYABLE bridge): sign of trailing mean,
    # held between rebalances. Strictly causal via rolling(...).shift(1) + rebalance ffill.
    for lb in TRAIL_LOOKBACKS:
        for rb in TRAIL_REBALANCES:
            tpos = trailing_direction_signal(spread, lookback_periods=lb,
                                             rebalance_periods=rb)
            tbm = book_metrics(tpos, spread, ta, tb)
            label = f"trail_lb{lb}_rb{rb}"
            result["configs"][label] = {
                "threshold": None, "hysteresis": None,
                "lookback_periods": lb, "rebalance_periods": rb,
                "kind": "causal_trailing_direction",
                "gross": tbm["gross"], "net": tbm["net"],
                "turnover_per_year": tbm["turnover_per_year"],
                "net_pnl_provenance": pnl_provenance(tbm["net_pnl"]),
                "_net_pnl": tbm["net_pnl"],
            }

    # Static in-sample-direction baseline (diagnostic ceiling).
    spos = static_positions(spread)
    sbm = book_metrics(spos, spread, ta, tb)
    result["configs"]["static_insample_dir"] = {
        "threshold": None, "hysteresis": None,
        "kind": "static_insample_direction_DIAGNOSTIC_CEILING_not_deployable",
        "gross": sbm["gross"], "net": sbm["net"],
        "turnover_per_year": sbm["turnover_per_year"],
        "net_pnl_provenance": pnl_provenance(sbm["net_pnl"]),
        "_net_pnl": sbm["net_pnl"],
    }
    return result


def main():
    pairs = [(a, b, False) for a, b in CORE_PAIRS] + \
            [(a, b, True) for a, b in SECONDARY_PAIRS]

    results = {}
    for a, b, sec in pairs:
        key = f"{a}-{b}"
        try:
            results[key] = run_pair(a, b, sec)
        except Exception as e:
            results[key] = {"venue_a": a, "venue_b": b, "secondary": sec,
                            "error": f"{type(e).__name__}: {e}"}

    # ── Pick committed: best NET daily Sharpe among DEPLOYABLE CAUSAL configs on CORE
    # pairs ─ this now spans BOTH the old hysteresis configs ("causal") AND the new
    # trailing-direction configs ("causal_trailing_direction"). The static baseline is a
    # ceiling (look-ahead direction), so it is EXCLUDED; secondaries are short-history.
    # Prefer positive net Sharpe + low turnover; if none net-positive, pick least-bad and
    # flag REJECT.
    DEPLOYABLE_KINDS = {"causal", "causal_trailing_direction"}
    candidates = []
    for key, r in results.items():
        if r.get("secondary") or "configs" not in r:
            continue
        for label, cfg in r["configs"].items():
            if cfg["kind"] not in DEPLOYABLE_KINDS:
                continue
            net = cfg["net"]
            if not net:
                continue
            candidates.append((key, label, cfg))

    # Sort by net Sharpe desc (tie-break: lower turnover).
    candidates.sort(key=lambda x: (x[2]["net"].get("sharpe", -1e9),
                                   -x[2]["turnover_per_year"]), reverse=True)
    any_net_positive = any(c[2]["net"].get("sharpe", -1e9) > 0 for c in candidates)

    best_key, best_label, best_cfg = candidates[0]
    r = results[best_key]

    # Static ceiling = best NET ann% of any static_insample_dir book across CORE pairs
    # (the +6.3%/yr HL-Binance figure). Used to report how much of the ceiling the best
    # deployable causal config recovers.
    static_ceiling_ann = None
    static_ceiling_pair = None
    for key, rr in results.items():
        if rr.get("secondary") or "configs" not in rr:
            continue
        scfg = rr["configs"].get("static_insample_dir")
        if not scfg or not scfg.get("net"):
            continue
        ann = scfg["net"].get("ann")
        if ann is not None and (static_ceiling_ann is None or ann > static_ceiling_ann):
            static_ceiling_ann = ann
            static_ceiling_pair = key

    best_net = best_cfg["net"]
    best_net_ann = best_net.get("ann") if best_net else None
    best_net_sharpe = best_net.get("sharpe") if best_net else None
    is_net_positive = bool(best_net_sharpe is not None and best_net_sharpe > 0)
    ceiling_recovered_frac = (
        float(best_net_ann / static_ceiling_ann)
        if (best_net_ann is not None and static_ceiling_ann
            and static_ceiling_ann > 0)
        else None
    )

    if is_net_positive:
        verdict = (
            f"ALIVE: a DEPLOYABLE causal config is net-positive after double costs "
            f"({best_key}/{best_label}: net Sharpe {best_net_sharpe:.2f}, "
            f"net ann {best_net_ann*100:.2f}%/yr, turnover {best_cfg['turnover_per_year']:.1f}/yr). "
            f"Trailing-direction HOLD kills the churn (vs ~1170/yr hysteresis) and "
            f"recovers ~{ceiling_recovered_frac*100:.0f}% of the +{static_ceiling_ann*100:.1f}%/yr "
            f"static ceiling. Validate OOS in Task C."
            if ceiling_recovered_frac is not None else
            f"ALIVE: deployable causal config net-positive ({best_key}/{best_label}, "
            f"net Sharpe {best_net_sharpe:.2f})."
        )
    else:
        verdict = (
            f"REJECT: even the best DEPLOYABLE causal config loses to double costs "
            f"({best_key}/{best_label}: net Sharpe {best_net_sharpe:.2f}, "
            f"net ann {best_net_ann*100:.2f}%/yr, turnover {best_cfg['turnover_per_year']:.1f}/yr). "
            f"Low-turnover trailing-direction holds still cannot beat the double-cost drag; "
            f"committed is least-bad for Task C reproducibility only."
        )

    committed = {
        "pair": best_key,
        "venue_a": r["venue_a"], "venue_b": r["venue_b"],
        "interval_a": r["interval_a"], "interval_b": r["interval_b"],
        "taker_a_bps": r["taker_a_bps"], "taker_b_bps": r["taker_b_bps"],
        "slip_bps": SLIP,
        "core_coins": r["coins"],
        "config_label": best_label,
        "config_kind": best_cfg["kind"],
        "threshold": best_cfg.get("threshold"),
        "hysteresis": best_cfg.get("hysteresis"),
        "lookback_periods": best_cfg.get("lookback_periods"),
        "rebalance_periods": best_cfg.get("rebalance_periods"),
        "net": best_cfg["net"],
        "gross": best_cfg["gross"],
        "turnover_per_year": best_cfg["turnover_per_year"],
        "net_pnl_provenance": best_cfg["net_pnl_provenance"],
        "any_config_net_positive_after_double_costs": bool(any_net_positive),
        "committed_is_net_positive": is_net_positive,
        "static_ceiling_ann": static_ceiling_ann,
        "static_ceiling_pair": static_ceiling_pair,
        "ceiling_recovered_frac": ceiling_recovered_frac,
        "verdict": verdict,
    }

    # ── Write full committed net pnl series to sibling CSV for bit-exact provenance ─
    committed_pnl = best_cfg["_net_pnl"]
    pnl_csv = REPO / "research" / "cross_exchange" / "characterize_committed_pnl.csv"
    committed_pnl.to_frame("spread_net").to_csv(pnl_csv)
    committed["net_pnl_csv"] = str(pnl_csv.relative_to(REPO))

    caveats = [
        "FUNDING-ONLY pnl: no perp-mark/basis model between venues -> real basis risk "
        "(price divergence on entry/exit, non-atomic execution, single-leg liquidation) "
        "is NOT modelled. Honesty ceiling per spread.py.",
        "Cadence alignment: HL native 1h (summed into 8h buckets, 00/08/16 UTC), "
        "Binance/Bybit/Backpack native 8h. Spread on inner-joined 8h grid.",
        "Static baseline is in-sample-direction (peeks at full-sample mean sign) -> "
        "DIAGNOSTIC CEILING, not deployable.",
        "Secondary pairs HL-Backpack / HL-Drift have short/odd history -> NOT in committed "
        "core; reported only as extra context.",
        "3yr sample (2023-06 ->) is a broadly trending/rising crypto regime.",
        "Double costs (4 taker legs/round-trip, venue fees > HL) are the key killer of a "
        "thin spread; net is reported separately from gross to expose the gap.",
    ]

    out = {
        "core_coins_requested": CORE_COINS,
        "taker_bps": TAKER, "slip_bps": SLIP,
        "config_sweep": [{"label": l, "threshold": t, "hysteresis": h}
                         for l, t, h in CONFIG_SWEEP],
        "bands_per_period": {"5pct_yr": BAND_5PCT, "10pct_yr": BAND_10PCT,
                             "20pct_yr": BAND_20PCT},
        "pairs": _strip_pnl(results),
        "committed": committed,
        "caveats": caveats,
    }

    out_path = REPO / "research" / "cross_exchange" / "characterize.json"
    out_path.write_text(json.dumps(out, indent=2, default=float))

    _print_summary(results, committed)
    print(f"\nWrote {out_path}")
    print(f"Wrote {pnl_csv}")


def _strip_pnl(results: dict) -> dict:
    """Drop in-process _net_pnl Series before JSON dump (provenance kept separately)."""
    clean = {}
    for k, r in results.items():
        r2 = dict(r)
        if "configs" in r2:
            r2["configs"] = {lbl: {kk: vv for kk, vv in cfg.items() if kk != "_net_pnl"}
                             for lbl, cfg in r2["configs"].items()}
        clean[k] = r2
    return clean


# ── Stdout summary ───────────────────────────────────────────────────────────

def _fmt(x, p=2):
    if x is None or (isinstance(x, float) and (np.isnan(x))):
        return "  n/a"
    return f"{x:.{p}f}"


def _print_summary(results: dict, committed: dict):
    print("\n" + "=" * 100)
    print("SUMMARY TABLE — book economics (gross vs net, honest daily metrics PPY=365)")
    print("=" * 100)
    hdr = (f"{'pair':<16}{'config':<26}{'grSh':>7}{'netSh':>7}"
           f"{'netAnn%':>9}{'maxDD%':>8}{'turn/yr':>9}{'hit':>6}")
    print(hdr)
    print("-" * len(hdr))
    for key, r in results.items():
        if "configs" not in r:
            print(f"{key:<16}{'(no data)':<26}{r.get('error', r.get('note','')):>0}")
            continue
        tag = " [SEC]" if r.get("secondary") else ""
        for label, cfg in r["configs"].items():
            g, n = cfg.get("gross", {}), cfg.get("net", {})
            print(f"{key+tag:<16}{label:<26}"
                  f"{_fmt(g.get('sharpe')):>7}{_fmt(n.get('sharpe')):>7}"
                  f"{_fmt((n.get('ann') or 0)*100):>9}"
                  f"{_fmt((n.get('maxdd') or 0)*100):>8}"
                  f"{_fmt(cfg.get('turnover_per_year'), 1):>9}"
                  f"{_fmt(n.get('hit')):>6}")
        print("-" * len(hdr))

    print("\n" + "=" * 100)
    print("PER-COIN SIGN-PERSISTENCE  (fraction of consecutive periods with same spread sign)")
    print("           — the key structural diagnostic: high => 'which venue is richer' is stable")
    print("=" * 100)
    for key, r in results.items():
        if "per_coin" not in r:
            continue
        tag = " [SEC]" if r.get("secondary") else ""
        print(f"\n{key}{tag}:")
        print(f"  {'coin':<7}{'signPersist':>12}{'domSign%':>10}{'meanAnn%':>10}"
              f"{'volAnn%':>9}{'%>0':>7}{'%>5%/yr':>9}")
        for c, st in r["per_coin"].items():
            if "sign_persistence" not in st:
                print(f"  {c:<7}{'(short)':>12}")
                continue
            print(f"  {c:<7}{_fmt(st['sign_persistence'],3):>12}"
                  f"{_fmt(st['dominant_sign_share']*100):>10}"
                  f"{_fmt(st['mean_ann_pct']):>10}{_fmt(st['vol_ann_pct']):>9}"
                  f"{_fmt(st['pct_over_0']*100):>7}{_fmt(st['pct_over_band5pct']*100):>9}")

    print("\n" + "=" * 100)
    print("COMMITTED CHOICE")
    print("=" * 100)
    print(f"  pair       : {committed['pair']}  ({committed['venue_a']} vs {committed['venue_b']})")
    print(f"  config     : {committed['config_label']}  (kind={committed['config_kind']})")
    if committed.get("lookback_periods") is not None:
        print(f"               lookback={committed['lookback_periods']} periods "
              f"(~{committed['lookback_periods']//3}d)  rebalance="
              f"{committed['rebalance_periods']} periods (~{committed['rebalance_periods']//3}d)")
    else:
        print(f"               thr={committed['threshold']} hys={committed['hysteresis']}")
    print(f"  coins      : {committed['core_coins']}")
    n = committed["net"]
    print(f"  net daily  : Sharpe={_fmt(n.get('sharpe'),3)}  ann={_fmt((n.get('ann') or 0)*100)}%  "
          f"maxDD={_fmt((n.get('maxdd') or 0)*100)}%  Calmar={_fmt(n.get('calmar'))}  "
          f"hit={_fmt(n.get('hit'),3)}  turn/yr={_fmt(committed['turnover_per_year'],1)}")
    print(f"  net-positive after double costs? {committed['committed_is_net_positive']}")
    print(f"  any deployable config net-positive? "
          f"{committed['any_config_net_positive_after_double_costs']}")
    if committed.get("static_ceiling_ann") is not None:
        cr = committed.get("ceiling_recovered_frac")
        print(f"  static ceiling: +{committed['static_ceiling_ann']*100:.2f}%/yr "
              f"({committed['static_ceiling_pair']})  -> committed recovers "
              f"{_fmt((cr or 0)*100,0)}% of it")
    print(f"\n  VERDICT: {committed['verdict']}")


if __name__ == "__main__":
    main()
