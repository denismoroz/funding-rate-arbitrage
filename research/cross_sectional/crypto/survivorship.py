"""
survivorship.py — Survivorship bias stress test for the crypto cross-sectional
momentum book.

The frozen 34-coin universe (universe.json) is built by cryptodata._candidates()
which keeps only NON-delisted coins with fresh OHLCV and >=547d history. That is
a FORWARD-SELECTED SURVIVOR set. The long leg (buy what rose and survived to 2026)
is plausibly inflated because:
  - coins that listed and then died are excluded ex ante, even for the periods they
    were alive and trading;
  - dead coins are disproportionately in the "went to zero" / "crashed hard" bucket,
    which the long leg would have picked up during their rise and then been hurt by.

This script builds a POINT-IN-TIME eligible universe and re-runs the identical
ensemble book on it, then compares HEAD-TO-HEAD to the frozen survivor book.

POINT-IN-TIME ELIGIBILITY RULE:
  At each rebalance date t, coin c is eligible if:
    (a) it has >= max(lookbacks)=60 calendar days of trailing price history ending at t
        (binding warmup, same as the ensemble's NaN mask), AND
    (b) its price feed is present (not dead/flat) at t — i.e. price[t] is not NaN.
  A coin that later delists DROPS OUT at the date its feed stops (price becomes NaN),
  not earlier. This is honest: we include it for the dates it was alive.

SOURCES of extra coins beyond the frozen survivor set:
  (a) HL meta coins with isDelisted=True (fetch.hl_meta_ctxs()) — filtered OUT in
      cryptodata._candidates(). We include them here.
  (b) Additional coins whose HL feed went stale/incomplete but Binance price exists
      (e.g. MATIC→POL rebrand, where MATIC data ends mid-history).
  Note: bridge tokens (name ending in digit) are excluded per project policy.
  Note: coins with <60 days of overlapping data within the study window
        (2023-06-08 → present) are excluded as insufficiently contributing.

DATA SOURCES:
  - Price (OHLCV): Binance hourly klines (same as the existing book, via fetch.py).
  - Funding: HL hourly funding history (via fetch.py/fetch_funding).
    For coins where HL funding ran dry (delisted), the funding panel is NaN after
    the last print; accrual is forced to 0 on those NaN periods (per xsec.py
    fillna(0.0) on accrual_aligned). That is slightly favorable to the PT universe
    (dead coins stop bleeding funding) — noted as a caveat.

NO LOOK-AHEAD:
  - All signals use only data <= t (momentum = price[t]/price[t-lb]-1, no fwd_ret).
  - Accrual = -funding.shift(-1): forward cost, realized, never used in signal.
  - Point-in-time eligibility at date t uses only price[t] presence, not future data.

HYPERPARAMETERS: IDENTICAL to the validated book (no new knobs).
  lookbacks=(14,21,30,45,60), costs_bps=8.5, rebal_every=7, tercile=1/3.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import cryptodata
import fetch
import signals
import xsec
from metrics_daily import daily_metrics

_HERE = Path(__file__).parent
DATA_DIR = fetch.DATA_DIR

# ── Hyperparameters — IDENTICAL to validated book ──────────────────────────────
LOOKBACKS = (14, 21, 30, 45, 60)
MAX_LB    = max(LOOKBACKS)       # 60d warmup
COSTS_BPS = 8.5
REBAL     = 7
HISTORY_START = pd.Timestamp("2023-06-08", tz="UTC")

# ── Candidates beyond the frozen survivor set ─────────────────────────────────

# From HL meta isDelisted=True (non-bridge): we filter to those with >=60 days
# of HL funding history (the listing died early enough to matter).
# Source: bgzjat5ax background task output (manually verified).
# Bridge token policy: skip names ending in digit.
_HL_DELISTED_NON_BRIDGE = [
    "MATIC", "RNDR", "FTM", "MKR", "FXS", "HPOS", "RLB", "UNIBOT", "OX",
    "FRIEND", "SHIA", "CYBER", "BLZ", "FTT", "LOOM", "OGN", "RDNT", "BNT",
    "CANTO", "REQ", "ORBS", "STG", "STRAX", "BADGER", "ILV", "USTC", "NFTI",
    "NTRN", "MAV", "MAVIA", "PANDORA", "PIXEL", "AI", "MYRO", "OMNI", "BLAST",
    "LISTA", "MEW", "CATI", "SCR", "NEIROETH", "CHILLGUY", "AI16Z", "ZEREBRO",
    "JELLY", "TST", "OM", "PROMPT", "LAUNCHCOIN", "YZY",
]

# Coins that fail cryptodata's freshness/history filter but we want to try:
_EXTRA_CANDIDATES = [
    # MATIC -> POL rebrand: HL MATIC data ends ~2024-01, Binance MATIC delisted
    # Already in _HL_DELISTED_NON_BRIDGE. No need to add again.
]


def _is_bridge_token(name: str) -> bool:
    return name[-1].isdigit()


def _fetch_and_cache_coin(coin: str) -> bool:
    """Fetch OHLCV and HL funding for a coin (via fetch.py). Return True if both OK."""
    fpath = DATA_DIR / f"{coin}.csv"
    opath = DATA_DIR / f"{coin}_1h.csv"

    # Use existing files if fresh enough (don't re-fetch survivors, only missing)
    need_fund = not fpath.exists()
    need_ohlcv = not opath.exists()

    if need_ohlcv:
        try:
            o = fetch.fetch_ohlcv(coin)
            if not o.empty:
                o.to_csv(opath, index=False)
            else:
                return False
        except Exception as e:
            print(f"    {coin}: ohlcv fetch error: {e}")
            return False
        time.sleep(0.1)

    if need_fund:
        try:
            f = fetch.fetch_funding(coin)
            if not f.empty:
                f.to_csv(fpath, index=False)
            else:
                # Some HL-native coins might not have HL funding (no perp).
                # For delisted perps, funding will stop at delist date — that's fine.
                # An empty result here means the coin never had HL perp funding at all.
                pass  # we'll handle missing funding gracefully as NaN
        except Exception as e:
            print(f"    {coin}: funding fetch error: {e}")
        time.sleep(0.15)

    return opath.exists()


def _daily_price(coin: str) -> pd.Series:
    """Daily close from cached Binance 1h OHLCV. Returns empty Series if missing."""
    p = DATA_DIR / f"{coin}_1h.csv"
    if not p.exists():
        return pd.Series(dtype=float)
    df = pd.read_csv(p)
    df["time"] = pd.to_datetime(df["time"], format="ISO8601", utc=True).dt.floor("h")
    s = df.set_index("time")["close"].astype(float).sort_index()
    s = s[s.index >= HISTORY_START]
    return s.resample("1D").last()


def _daily_funding(coin: str) -> pd.Series:
    """Daily summed HL funding from cache. Returns empty Series if missing."""
    p = DATA_DIR / f"{coin}.csv"
    if not p.exists():
        return pd.Series(dtype=float)
    df = pd.read_csv(p)
    df["time"] = pd.to_datetime(df["time"], format="ISO8601", utc=True).dt.floor("h")
    s = df.set_index("time")["fundingRate"].astype(float).sort_index()
    s = s[s.index >= HISTORY_START]
    return s.resample("1D").sum()


def build_pt_panel(coins: list[str]) -> dict:
    """Build the point-in-time panel for a list of coins.

    Mirrors cryptodata.load_panel exactly. NaN cells where a coin is not yet
    listed (pre-history or post-delist). The ensemble signal's NaN mask then
    naturally enforces point-in-time eligibility: a cell is NaN unless ALL
    lookback legs have price data (i.e., >=MAX_LB days of uninterrupted price).
    """
    price_cols, fund_cols = {}, {}
    for c in coins:
        pr = _daily_price(c)
        fu = _daily_funding(c)
        if not pr.empty:
            price_cols[c] = pr
        if not fu.empty:
            fund_cols[c] = fu

    price = pd.DataFrame(price_cols).sort_index()
    full_idx = pd.date_range(price.index.min(), price.index.max(), freq="1D", tz="UTC")
    price = price.reindex(full_idx)
    funding = pd.DataFrame(fund_cols).reindex(full_idx)
    funding = funding.where(price.notna())

    fwd_ret = price.shift(-1) / price - 1.0
    return {"coins": list(price.columns), "price": price, "fwd_ret": fwd_ret,
            "funding": funding}


def run_book(panel: dict) -> pd.Series:
    """Run the full ensemble book on a panel. Returns daily pnl with accrual."""
    score = signals.momentum_ensemble(panel, lookbacks=LOOKBACKS)
    weights = xsec.rank_to_weights(score)
    accrual = -panel["funding"].shift(-1)
    return xsec.portfolio_returns(
        weights, panel["fwd_ret"], costs_bps=COSTS_BPS, rebal_every=REBAL,
        accrual=accrual,
    )


def _fmt(m: dict) -> str:
    if not m:
        return "(too few days)"
    return (f"Sharpe {m['sharpe']:+.3f}  ann {100*m['ann']:+6.2f}%  "
            f"vol {100*m['vol_ann']:.1f}%  maxDD {100*m['maxdd']:5.2f}%  "
            f"Calmar {m['calmar']:+5.2f}  hit {100*m['hit']:.1f}%  n={m['n']}")


def _half_metrics(pnl: pd.Series) -> tuple[dict, dict]:
    r = pnl.dropna()
    h = len(r) // 2
    return daily_metrics(r.iloc[:h]), daily_metrics(r.iloc[h:])


def _long_leg_attribution(survivor_weights: pd.DataFrame,
                          pt_weights: pd.DataFrame,
                          fwd_ret: pd.DataFrame) -> dict:
    """Decompose the survivor long leg's return into:
      - return from coins present in both books (overlap)
      - return from coins ONLY in the survivor book (survivorship premium coins)

    'Only in survivor' means: survivor weight > 0 but PT weight == 0 at that date/coin,
    because the coin wasn't in the PT universe (it's a survivor we didn't include).
    """
    # Align on common dates
    common_dates = survivor_weights.index.intersection(pt_weights.index)
    sw = survivor_weights.reindex(common_dates).fillna(0.0)
    pw = pt_weights.reindex(common_dates).fillna(0.0)
    fr = fwd_ret.reindex(common_dates).fillna(0.0)

    # Coins in survivor universe only (long leg, not in PT)
    surv_only_long = (sw > 0) & (pw == 0)
    surv_and_pt_long = (sw > 0) & (pw != 0)

    # Daily return from each bucket
    r_surv_only = (sw.where(surv_only_long, 0.0) * fr).sum(axis=1)
    r_overlap   = (sw.where(surv_and_pt_long, 0.0) * fr).sum(axis=1)

    # Total survivor long-leg gross
    r_long_total = (sw.where(sw > 0, 0.0) * fr).sum(axis=1)

    # Normalize: what fraction of the survivor long's return came from surv-only coins?
    total_long_ann = r_long_total.mean() * 365
    surv_only_ann  = r_surv_only.mean() * 365
    overlap_ann    = r_overlap.mean() * 365

    # Per-date: how many long positions are from survivor-only coins?
    n_surv_only_longs = surv_only_long.sum(axis=1)
    n_overlap_longs   = surv_and_pt_long.sum(axis=1)

    return {
        "total_long_leg_ann_pct":    float(total_long_ann * 100),
        "surv_only_long_ann_pct":    float(surv_only_ann * 100),
        "overlap_long_ann_pct":      float(overlap_ann * 100),
        "surv_only_fraction_of_long": (float(surv_only_ann / total_long_ann)
                                       if abs(total_long_ann) > 1e-9 else float("nan")),
        "avg_surv_only_longs_per_date": float(n_surv_only_longs.mean()),
        "avg_overlap_longs_per_date":   float(n_overlap_longs.mean()),
    }


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    pd.set_option("display.width", 200)

    print("=" * 80)
    print("SURVIVORSHIP BIAS STRESS TEST — CRYPTO CROSS-SECTIONAL MOMENTUM")
    print("=" * 80)
    print("Frozen survivor book vs point-in-time book with dead/delisted coins")

    # ── Step 1: load frozen survivor book ─────────────────────────────────────
    print("\n[1] Loading frozen survivor book (universe.json, 34 coins)...")
    frozen_coins = json.loads((_HERE / "universe.json").read_text())["coins"]
    survivor_panel = cryptodata.load_panel(coins=frozen_coins)
    pnl_survivor = run_book(survivor_panel)
    m_survivor = daily_metrics(pnl_survivor.dropna())
    print(f"Survivor book: {_fmt(m_survivor)}")

    # ── Step 2: fetch extra (dead/delisted) coins ──────────────────────────────
    print("\n[2] Fetching data for dead/delisted HL coins not in survivor set...")
    extra_to_try = [c for c in _HL_DELISTED_NON_BRIDGE if c not in frozen_coins]
    print(f"Extra candidates to try: {len(extra_to_try)} coins")

    fetched_ok = []
    fetch_failed = []
    for coin in extra_to_try:
        ok = _fetch_and_cache_coin(coin)
        if ok:
            # quick sanity: does it have any data in the study window?
            pr = _daily_price(coin)
            listed_days = pr.dropna().__len__()
            if listed_days >= MAX_LB + 5:   # at least warmup + a few trading days
                fetched_ok.append(coin)
                print(f"  {coin}: {listed_days}d of price in study window -> INCLUDED")
            else:
                fetch_failed.append((coin, f"only {listed_days}d in study window (<{MAX_LB+5})"))
                print(f"  {coin}: only {listed_days}d -> EXCLUDED (too short)")
        else:
            fetch_failed.append((coin, "OHLCV not on Binance"))
            print(f"  {coin}: not on Binance -> EXCLUDED")

    print(f"\nExtra coins fetched and included: {len(fetched_ok)}")
    print(f"Could NOT get (data gap): {len(fetch_failed)}")
    for c, reason in fetch_failed:
        print(f"  {c}: {reason}")

    if not fetched_ok:
        print("WARNING: no extra coins could be fetched! Survivorship test will show 0 premium.")

    # ── Step 3: build point-in-time panel (survivors + dead coins) ────────────
    all_pt_coins = sorted(set(frozen_coins) | set(fetched_ok))
    print(f"\n[3] Building point-in-time panel ({len(all_pt_coins)} coins)...")
    print(f"  = {len(frozen_coins)} survivors + {len(fetched_ok)} extra dead/delisted")

    pt_panel = build_pt_panel(all_pt_coins)
    pt_price = pt_panel["price"]
    print(f"Panel: {pt_price.index.min().date()} → {pt_price.index.max().date()}  "
          f"({len(pt_price)} days)")

    # Count how many coins have data at each date (to see the time-varying universe size)
    n_listed = pt_price.notna().sum(axis=1)
    n_eligible = signals.momentum_ensemble(pt_panel, lookbacks=LOOKBACKS).notna().sum(axis=1)
    print(f"Avg coins listed per date:   {n_listed.mean():.1f}")
    print(f"Avg coins eligible per date: {n_eligible.mean():.1f}  "
          f"(eligible = has full {MAX_LB}d lookback window)")

    # Show when extra dead coins are present vs gone
    print("\nPoint-in-time membership for extra (dead) coins:")
    print(f"  {'coin':<12}{'first':>12}{'last':>12}{'n_days':>8}")
    for c in sorted(fetched_ok):
        if c in pt_price.columns:
            ser = pt_price[c].dropna()
            if len(ser):
                print(f"  {c:<12}{str(ser.index.min().date()):>12}"
                      f"{str(ser.index.max().date()):>12}{len(ser):>8}")

    # ── Step 4: run point-in-time book ─────────────────────────────────────────
    print("\n[4] Running ensemble book on point-in-time universe...")
    pnl_pt = run_book(pt_panel)

    # ── Step 5: compare on common window ──────────────────────────────────────
    common_idx = pnl_survivor.dropna().index.intersection(pnl_pt.dropna().index)
    pnl_s_common = pnl_survivor.loc[common_idx]
    pnl_pt_common = pnl_pt.loc[common_idx]
    m_s  = daily_metrics(pnl_s_common)
    m_pt = daily_metrics(pnl_pt_common)

    print("\n" + "=" * 80)
    print("HEAD-TO-HEAD COMPARISON — COMMON WINDOW")
    print("=" * 80)
    print(f"Common window: {common_idx.min().date()} → {common_idx.max().date()}  "
          f"({len(common_idx)} days)")
    print(f"\nSurvivor book (frozen 34):   {_fmt(m_s)}")
    print(f"Point-in-time book ({len(all_pt_coins)} coins): {_fmt(m_pt)}")

    sharpe_premium = m_s.get("sharpe", 0) - m_pt.get("sharpe", 0)
    ann_premium    = m_s.get("ann", 0)    - m_pt.get("ann", 0)
    print(f"\nSurvivorship PREMIUM:")
    print(f"  Sharpe premium (survivor − PT): {sharpe_premium:+.3f}")
    print(f"  Ann return premium:             {100*ann_premium:+.2f}%/yr")

    # ── Step 6: half-split ─────────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("HALF-SPLIT (first vs second half of common window)")
    print("=" * 80)
    h1_s, h2_s   = _half_metrics(pnl_s_common)
    h1_pt, h2_pt = _half_metrics(pnl_pt_common)
    print(f"\nSurvivor  1st half: {_fmt(h1_s)}")
    print(f"PT        1st half: {_fmt(h1_pt)}")
    print(f"Survivorship premium 1st half Sharpe: "
          f"{h1_s.get('sharpe',0)-h1_pt.get('sharpe',0):+.3f}")
    print(f"\nSurvivor  2nd half: {_fmt(h2_s)}")
    print(f"PT        2nd half: {_fmt(h2_pt)}")
    print(f"Survivorship premium 2nd half Sharpe: "
          f"{h2_s.get('sharpe',0)-h2_pt.get('sharpe',0):+.3f}")

    # ── Step 7: long-leg attribution ───────────────────────────────────────────
    print("\n" + "=" * 80)
    print("LONG-LEG ATTRIBUTION — HOW MUCH SURVIVOR BOOK LONG-LEG RETURN CAME")
    print("FROM COINS ABSENT IN PT UNIVERSE?")
    print("=" * 80)

    # survivor weights from frozen panel
    score_s = signals.momentum_ensemble(survivor_panel, lookbacks=LOOKBACKS)
    w_s = xsec.rank_to_weights(score_s)

    # pt weights from pt panel
    score_pt = signals.momentum_ensemble(pt_panel, lookbacks=LOOKBACKS)
    w_pt = xsec.rank_to_weights(score_pt)

    # fwd_ret: use survivor panel's (covers the full frozen universe)
    attr = _long_leg_attribution(
        w_s.reindex(common_idx),
        w_pt.reindex(common_idx, columns=w_s.columns, fill_value=0.0),
        survivor_panel["fwd_ret"].reindex(common_idx),
    )

    print(f"\nSurvivor long-leg ann return:      {attr['total_long_leg_ann_pct']:+.2f}%/yr")
    print(f"  of which from overlap coins:     {attr['overlap_long_ann_pct']:+.2f}%/yr")
    print(f"  of which from survivor-ONLY:     {attr['surv_only_long_ann_pct']:+.2f}%/yr "
          f"({100*attr['surv_only_fraction_of_long']:.1f}% of long-leg total)")
    print(f"Avg survivor-only long positions per date: "
          f"{attr['avg_surv_only_longs_per_date']:.1f}")
    print(f"Avg overlap long positions per date: "
          f"{attr['avg_overlap_longs_per_date']:.1f}")

    # ── Step 8: extra-coins presence/impact breakdown ──────────────────────────
    if fetched_ok:
        print("\n" + "=" * 80)
        print("EXTRA (DEAD) COIN IMPACT PER COIN")
        print("=" * 80)
        print(f"  {'coin':<12}{'first':>12}{'last':>12}{'days':>8}{'avg_wt':>9}{'long%':>8}{'short%':>8}")

        score_pt_full = signals.momentum_ensemble(pt_panel, lookbacks=LOOKBACKS)
        w_pt_full = xsec.rank_to_weights(score_pt_full)

        for c in sorted(fetched_ok):
            if c not in w_pt_full.columns:
                continue
            wt = w_pt_full[c]
            active = wt.loc[wt != 0]
            if active.empty:
                continue
            pr_c = pt_price[c].dropna()
            first = pr_c.index.min().date() if len(pr_c) else "?"
            last  = pr_c.index.max().date() if len(pr_c) else "?"
            n_days = len(pr_c)
            avg_abs = active.abs().mean()
            pct_long  = float((active > 0).mean() * 100)
            pct_short = float((active < 0).mean() * 100)
            print(f"  {c:<12}{str(first):>12}{str(last):>12}{n_days:>8}"
                  f"{avg_abs:>9.4f}{pct_long:>8.1f}{pct_short:>8.1f}")

    # ── Step 9: verdict ────────────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("VERDICT")
    print("=" * 80)
    if abs(sharpe_premium) < 0.05:
        verdict_tag = "NEGLIGIBLE BIAS"
        verdict_body = (
            f"The survivorship premium is only {sharpe_premium:+.3f} Sharpe / "
            f"{100*ann_premium:+.1f}%/yr. Including {len(fetched_ok)} dead/delisted coins "
            f"barely changes the book. The frozen survivor book's edge is essentially "
            f"unaffected by the survivorship bias from the tested dead coins."
        )
    elif abs(sharpe_premium) < 0.15:
        verdict_tag = "SMALL BUT REAL BIAS"
        verdict_body = (
            f"Survivorship premium is {sharpe_premium:+.3f} Sharpe / "
            f"{100*ann_premium:+.1f}%/yr — small but non-trivial. Including dead coins "
            f"modestly deflates the book, confirming some survivorship inflation in the "
            f"frozen universe. The edge persists but numbers need to be discounted."
        )
    elif abs(sharpe_premium) < 0.40:
        verdict_tag = "MEANINGFUL SURVIVORSHIP BIAS"
        verdict_body = (
            f"Survivorship premium is {sharpe_premium:+.3f} Sharpe / "
            f"{100*ann_premium:+.1f}%/yr — meaningful. The frozen survivor book is "
            f"noticeably inflated. Point-in-time Sharpe {m_pt.get('sharpe',0):.2f} is "
            f"the more honest number. The edge survives de-biasing but is weaker."
        )
    else:
        verdict_tag = "LARGE SURVIVORSHIP BIAS — FORWARD NUMBERS UNRELIABLE"
        verdict_body = (
            f"Survivorship premium is {sharpe_premium:+.3f} Sharpe / "
            f"{100*ann_premium:+.1f}%/yr — very large. The frozen book's headline "
            f"numbers are seriously inflated by survivor selection. The point-in-time "
            f"Sharpe {m_pt.get('sharpe',0):.2f} / ann {100*m_pt.get('ann',0):.1f}% "
            f"should be used for forward planning."
        )

    print(f"\n[{verdict_tag}]")
    print(f"\n{verdict_body}")
    n_pt = len(all_pt_coins)
    n_extra_included = len(fetched_ok)
    n_could_not_get = len(fetch_failed)
    print(f"\nData caveat: {n_could_not_get} extra candidates could NOT be obtained "
          f"(Binance data gap or too short). The true survivorship premium may be "
          f"larger if those missing coins were the worst-performing dead coins.")

    # ── Step 10: write JSON ────────────────────────────────────────────────────
    def _safe(d):
        if not d:
            return {}
        return {k: (float(v) if isinstance(v, (float, np.floating)) else
                    int(v)   if isinstance(v, (int, np.integer)) else v)
                for k, v in d.items()}

    out = {
        "test": "survivorship",
        "description": (
            "Survivorship stress test: frozen survivor book (34 coins) vs "
            "point-in-time book (survivors + dead/delisted HL coins). "
            "Identical hyperparams."
        ),
        "frozen_survivor_coins": frozen_coins,
        "extra_dead_coins_included": sorted(fetched_ok),
        "extra_candidates_not_available": {c: r for c, r in fetch_failed},
        "pt_universe_total_coins": len(all_pt_coins),
        "common_window": {
            "start": str(common_idx.min().date()),
            "end":   str(common_idx.max().date()),
            "n_days": len(common_idx),
        },
        "survivor_book_metrics": _safe(m_s),
        "pt_book_metrics": _safe(m_pt),
        "survivorship_premium": {
            "sharpe": float(sharpe_premium),
            "ann_pct": float(ann_premium * 100),
        },
        "half_split": {
            "h1_survivor": _safe(h1_s),
            "h1_pt":       _safe(h1_pt),
            "h1_premium_sharpe": float(h1_s.get("sharpe", 0) - h1_pt.get("sharpe", 0)),
            "h2_survivor": _safe(h2_s),
            "h2_pt":       _safe(h2_pt),
            "h2_premium_sharpe": float(h2_s.get("sharpe", 0) - h2_pt.get("sharpe", 0)),
        },
        "long_leg_attribution": attr,
        "verdict": verdict_tag,
        "verdict_body": verdict_body,
        "data_caveats": [
            f"{n_could_not_get} extra candidates could not be obtained "
            f"(Binance data gap or too short in study window).",
            "Dead coins' funding becomes 0 (NaN→0 via fillna) after delist — "
            "slightly favorable to PT universe post-delist.",
            "HL funding for dead coins may be stale (all zeros near delist) — "
            "captured correctly as zero-accrual in the PT book.",
            "Some very small/illiquid delisted coins (HPOS, RLB, UNIBOT, etc.) "
            "may have had very thin Binance spot markets — price data may not "
            "reflect realistic execution. These are included as-is.",
        ],
    }

    out_path = _HERE / "survivorship.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nResults written to {out_path}")
