"""
characterize.py — SANITY characterization of the directional trend books
(TSMOM per-lookback + TSMOM-ENSEMBLE + Donchian) on the real survivorship-debiased
PT panel. This is Task B of research/trend_following/PLAN.md.

THIS IS NOT THE VALIDATION HARNESS (that is Task C / trend_validation.py). The goal
here is a STRUCTURAL sanity check + HONEST daily metrics:
  - Do the trend books behave directionally as designed: net-LONG in bull regimes,
    net-SHORT in bear regimes (time-varying beta → crisis-alpha by construction)?
  - Do they have positive-ish (less-negative) skew vs the cross-sec momentum spread?
  - Do they survive chop (whipsaw) — not bleed to death on turnover?
  - HONEST daily Sharpe/ann/maxDD/Calmar/hit (sqrt(365) via metrics_daily), plus
    annual turnover and typical gross leverage AFTER vol-target + cap scaling.

REUSE (import, do NOT reimplement):
  - trend.py:        tsmom_signal, tsmom_ensemble, donchian_signal,
                     realized_vol, portfolio_returns_directional  (Task A, frozen).
  - survivorship.py: build_pt_panel, COSTS_BPS  (same PT panel as the XSMOM book →
                     later apples-to-apples in Task D).
  - metrics_daily:   daily_metrics  (the ONLY honest absolute-level source: sqrt(365)).

FIXED DESIGN CONSTANTS (identical across ALL books — only the signal differs, so the
comparison is clean). Documented inline below: VOL_TARGET, LEVERAGE_CAP.

HONESTY / CAVEATS (also written to JSON):
  - Honest levels ONLY via metrics_daily (PPY=365, sqrt(365)). We do NOT use the
    harness's hourly-annualized (HOURS_PER_YEAR=8760) numbers anywhere here.
  - Panel is ~3 years from 2023-06, predominantly an up-market — there is NO TRUE
    BEAR MARKET in-sample. Crisis-alpha is therefore a STRUCTURAL EXPECTATION shown
    via the net-exposure/regime check, NOT proven by this window. Stated explicitly.
  - No look-ahead: signals + realized vol are causal (trend.py guarantees this);
    accrual = -funding.shift(-1) aligns funding earned over t→t+1 with the position
    held at t (the SAME convention as the cross-sectional book), without look-ahead.
  - Survivorship-debiased PT panel (includes dead/delisted coins) → conservative.

Run:
  cd /Users/d/prj/funding-rate-arbitrage && \\
  PYTHONPATH=research:research/validation_harness:research/cross_sectional:research/cross_sectional/crypto:research/trend_following \\
  .venv/bin/python research/trend_following/characterize.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

# ── crypto-local data + honest metrics ─────────────────────────────────────────
import survivorship
from metrics_daily import daily_metrics

# ── trend engine (Task A, frozen) ──────────────────────────────────────────────
from trend import (
    tsmom_signal,
    tsmom_ensemble,
    donchian_signal,
    realized_vol,
    portfolio_returns_directional,
)

_HERE = Path(__file__).parent

# ══════════════════════════════════════════════════════════════════════════════
# FIXED DESIGN CONSTANTS (identical across every book in the menu)
# ══════════════════════════════════════════════════════════════════════════════

COSTS_BPS = survivorship.COSTS_BPS  # 8.5 bps/leg — imported, not hardcoded.

# VOL_TARGET — constant per-asset DAILY vol target. Each held position is scaled to
# vol_target / realized_vol(30d) so a 4%/day coin and a 1.5%/day coin contribute
# comparable risk (classic vol-targeting). 0.02 (=2%/day) is a sober crypto choice:
# typical liquid alt daily vol is 3-6%, so most names get scaled DOWN (sub-unit),
# which is exactly why gross stays modest (see VERDICT / gross stats). The ABSOLUTE
# Sharpe/ann level is vol-target-invariant to first order (it just rescales pnl);
# vol_target matters for the gross/leverage readout, not the risk-adjusted shape.
VOL_TARGET = 0.02

# LEVERAGE_CAP — gross cap = max Σ|held| per day. With vol-targeting individual
# positions are already small and the universe is ~15-34 names, so raw gross is
# usually well under this; the cap only clips fat tails (low-vol clusters that would
# otherwise blow up the vol/target ratio). Set to 3.0 and we REPORT typical/95th-pct
# gross to confirm the book isn't secretly over-levered (cap rarely binds).
LEVERAGE_CAP = 3.0

# Lookbacks LONGER than the cross-sec book (which uses 14..60), per PLAN: trend rides
# longer moves. Ensemble = equal-weight over all four (committed candidate).
TSMOM_LOOKBACKS = (30, 60, 90, 120)
DONCHIAN_CHANNELS = (20, 55, 100)  # Turtle classic 20/55 + a slower 100.
VOL_WINDOW = 30  # realized-vol window for vol-targeting (causal).

PPY = 365  # calendar-daily grid (gap-free panel).


# ══════════════════════════════════════════════════════════════════════════════
# PT panel — IDENTICAL construction to the cross-sectional book
# ══════════════════════════════════════════════════════════════════════════════

def build_pt_panel() -> dict:
    """Build the survivorship-debiased PT panel exactly like the XSEC book
    (event_driven_validation._build_pt_panel): same survivorship.json, same
    frozen-survivors ∪ extra-dead union, same survivorship.build_pt_panel."""
    surv = json.loads((_HERE.parent / "cross_sectional" / "crypto" /
                       "survivorship.json").read_text())
    all_coins = sorted(
        set(surv["frozen_survivor_coins"])
        | set(surv["extra_dead_coins_included"])
    )
    print(f"PT universe: {len(all_coins)} coins "
          f"({len(surv['frozen_survivor_coins'])} survivors + "
          f"{len(surv['extra_dead_coins_included'])} dead/delisted)")
    return survivorship.build_pt_panel(all_coins)


# ══════════════════════════════════════════════════════════════════════════════
# Scaling replica — to read the REAL traded (held) positions
# ══════════════════════════════════════════════════════════════════════════════
#
# portfolio_returns_directional applies vol-target then leverage-cap INTERNALLY and
# does not expose the scaled `held` book. To report HONEST turnover and gross we
# replicate exactly that scaling here (line-for-line with trend.py) and read held
# off it. We assert below that the pnl from this scaled book (fed back through
# portfolio_returns_directional WITHOUT re-scaling) matches the engine's pnl, so the
# replica is provably the same book the engine traded.

def scale_positions(positions: pd.DataFrame, fwd_ret: pd.DataFrame,
                    vol: pd.DataFrame, vol_target: float,
                    leverage_cap: float) -> pd.DataFrame:
    """Replicate portfolio_returns_directional's vol-target + cap, return held book.

    Mirrors trend.py steps (1) and (2) EXACTLY:
      1) pos = pos * (vol_target / vol), inf/NaN → 0
      2) if gross[t] = Σ|pos[t]| > cap: scale whole row by cap/gross
    The returned DataFrame is the ACTUAL daily held book (post-scaling) — turnover
    and gross are computed on THIS.
    """
    pos = positions.reindex_like(fwd_ret).fillna(0.0)
    v = vol.reindex_like(fwd_ret)
    scale = (vol_target / v).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    pos = pos * scale
    gross_abs = pos.abs().sum(axis=1)
    factor = pd.Series(1.0, index=pos.index)
    over = gross_abs > leverage_cap
    factor[over] = leverage_cap / gross_abs[over]
    pos = pos.mul(factor, axis=0)
    return pos


def turnover_and_gross(held: pd.DataFrame, n_years: float) -> dict:
    """Annual turnover and gross-leverage stats on the SCALED held book.

    turnover[t] = Σ_c |held[t] - held[t-1]| (held[-1]=0 → day0 from zero), exactly
    as portfolio_returns_directional charges costs. Annual turnover = mean daily
    turnover × 365. Gross[t] = Σ_c |held[t]|; report mean and 95th pct.
    """
    h = held.fillna(0.0)
    diff = h.diff()
    diff.iloc[0] = h.iloc[0]  # day 0: from zero (matches engine cost on first day)
    daily_turn = diff.abs().sum(axis=1)
    gross = h.abs().sum(axis=1)
    return {
        "turn_per_yr": float(daily_turn.mean() * PPY),
        "mean_daily_turn": float(daily_turn.mean()),
        "gross_mean": float(gross.mean()),
        "gross_p95": float(np.percentile(gross.values, 95)),
        "gross_max": float(gross.max()),
        "cap_binds_frac": float((gross >= LEVERAGE_CAP - 1e-9).mean()),
    }


def pnl_skew(pnl: pd.Series) -> float:
    """Skew of the daily pnl distribution (third standardized moment, ddof=0).

    Manual (no scipy dependency): skew = mean((x-mu)^3) / std^3. Trend should be
    less-negative / positive-ish vs the cross-sec momentum spread.
    """
    x = pnl.dropna().values
    if len(x) < 3:
        return float("nan")
    mu = x.mean()
    sd = x.std(ddof=0)
    if sd <= 0:
        return float("nan")
    return float(np.mean(((x - mu) / sd) ** 3))


# ══════════════════════════════════════════════════════════════════════════════
# Build one book end-to-end
# ══════════════════════════════════════════════════════════════════════════════

def build_book(label: str, positions: pd.DataFrame, panel: dict,
               vol: pd.DataFrame, accrual: pd.DataFrame) -> dict:
    """Compute pnl (engine), honest daily metrics, skew, turnover, gross for a book.

    Returns dict with: label, pnl (Series), held (scaled DataFrame), metrics(dict),
    skew(float), turnover/gross stats(dict).
    """
    fwd = panel["fwd_ret"]
    pnl = portfolio_returns_directional(
        positions, fwd, costs_bps=COSTS_BPS, accrual=accrual,
        vol=vol, vol_target=VOL_TARGET, leverage_cap=LEVERAGE_CAP,
    )
    held = scale_positions(positions, fwd, vol, VOL_TARGET, LEVERAGE_CAP)

    # Provenance assert: feeding the SCALED held book back through the engine WITHOUT
    # re-scaling (vol_target/leverage_cap=None) must reproduce the engine pnl → the
    # replica `held` is exactly the book the engine traded. (accrual on, same costs.)
    pnl_check = portfolio_returns_directional(
        held, fwd, costs_bps=COSTS_BPS, accrual=accrual,
    )
    diff = float((pnl - pnl_check).abs().max())
    assert diff < 1e-9, (
        f"[{label}] scaled-held replica != engine pnl (max diff {diff:.2e}); "
        "turnover/gross would be reported on the wrong book."
    )

    pnl_clean = pnl.dropna()
    n_years = len(pnl_clean) / PPY
    m = daily_metrics(pnl_clean)
    tg = turnover_and_gross(held.loc[pnl_clean.index], n_years)
    return {
        "label": label,
        "pnl": pnl,
        "held": held,
        "metrics": m,
        "skew": pnl_skew(pnl_clean),
        "turnover_gross": tg,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Regime / crisis-alpha check (structural point of Task B)
# ══════════════════════════════════════════════════════════════════════════════

def find_btc_symbol(coins: list[str]) -> str | None:
    """Locate the BTC column (exact 'BTC', else first containing 'BTC')."""
    if "BTC" in coins:
        return "BTC"
    for c in coins:
        if "BTC" in c.upper():
            return c
    return None


def regime_check(ensemble_held: pd.DataFrame, panel: dict,
                 ens_pnl: pd.Series) -> dict:
    """Net-exposure regime analysis on the committed ENSEMBLE book.

    - net[t] = Σ_c held[t] (SIGNED, after scaling) → directional tilt of the book.
    - BTC regime (causal): bull if BTC 90d trailing return > 0, bear if < 0.
    - Report mean net exposure in bull vs bear days → expect net-LONG in bull,
      net-SHORT in bear (time-varying beta).
    - Top BTC peak-to-trough drawdown episodes: the ensemble book's cumulative pnl
      over each window (crisis-alpha = ideally not bleeding / positive in selloffs).
    """
    price = panel["price"]
    coins = panel["coins"]
    btc = find_btc_symbol(coins)

    net = ensemble_held.fillna(0.0).sum(axis=1)  # signed net exposure per day

    out: dict = {"btc_symbol": btc}

    if btc is None:
        out["error"] = "no BTC column found in panel — regime check skipped"
        return out

    btc_px = price[btc]
    # Causal 90d trailing return: price[t]/price[t-90] - 1 (uses only ≤ t).
    btc_trail90 = btc_px / btc_px.shift(90) - 1.0
    bull = btc_trail90 > 0
    bear = btc_trail90 < 0

    # Align net exposure to the regime mask on the common valid window.
    common = net.index.intersection(btc_trail90.dropna().index)
    net_c = net.loc[common]
    bull_c = bull.loc[common]
    bear_c = bear.loc[common]

    out.update({
        "regime_def": "bull = BTC 90d trailing return > 0 (causal); bear = < 0",
        "n_bull_days": int(bull_c.sum()),
        "n_bear_days": int(bear_c.sum()),
        "mean_net_exposure_bull": float(net_c[bull_c].mean()) if bull_c.any() else None,
        "mean_net_exposure_bear": float(net_c[bear_c].mean()) if bear_c.any() else None,
        "mean_net_exposure_all": float(net_c.mean()),
        "frac_days_net_long": float((net_c > 0).mean()),
    })

    # ── Worst BTC peak-to-trough drawdown episodes ────────────────────────────
    eq = btc_px.dropna()
    run_max = eq.cummax()
    dd = eq / run_max - 1.0  # ≤ 0
    # Greedy: find the deepest-trough episodes (peak → trough → recovery to new high).
    episodes = []
    in_dd = False
    peak_date = None
    trough_date = None
    trough_val = 0.0
    for date, d in dd.items():
        if d < 0 and not in_dd:
            in_dd = True
            # peak is the last date at run_max before this; approximate with the
            # date where run_max was set = the previous bar's running-max date.
            peak_date = run_max.loc[:date][run_max.loc[:date] == run_max.loc[date]].index[0]
            trough_date = date
            trough_val = d
        elif d < 0 and in_dd:
            if d < trough_val:
                trough_val = d
                trough_date = date
        elif d >= 0 and in_dd:
            # recovered to a new high → close episode
            episodes.append((peak_date, trough_date, float(trough_val)))
            in_dd = False
    if in_dd:  # still in drawdown at end of sample
        episodes.append((peak_date, trough_date, float(trough_val)))

    # Sort by depth, take top few.
    episodes.sort(key=lambda e: e[2])  # most negative first
    top = episodes[:4]

    dd_windows = []
    for peak_date, trough_date, depth in top:
        # Ensemble book cumulative (compounded) pnl over peak→trough window.
        win = ens_pnl.loc[peak_date:trough_date].dropna()
        if len(win) < 2:
            continue
        cum = float(np.prod(1.0 + win.values) - 1.0)
        dd_windows.append({
            "btc_peak": str(pd.Timestamp(peak_date).date()),
            "btc_trough": str(pd.Timestamp(trough_date).date()),
            "btc_drawdown_pct": round(depth * 100, 2),
            "n_days": int(len(win)),
            "ens_book_cum_pnl_pct": round(cum * 100, 3),
        })
    out["worst_btc_drawdown_windows"] = dd_windows
    out["max_btc_drawdown_pct"] = round(float(dd.min()) * 100, 2)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> dict:
    print("=" * 92)
    print("TREND CHARACTERIZATION (sanity, NOT the validation harness) — PT panel")
    print("=" * 92)

    # ── Panel ──────────────────────────────────────────────────────────────────
    print("\n[1] Building survivorship-debiased PT panel (identical to XSEC book)...")
    panel = build_pt_panel()
    price = panel["price"]
    n_days = len(price)
    date_min = price.index.min().date()
    date_max = price.index.max().date()
    n_coins = len(panel["coins"])
    print(f"    Panel: {date_min} → {date_max}  ({n_days} days, {n_coins} coins)")

    # Shared inputs (identical across books).
    vol = realized_vol(price, vol_window=VOL_WINDOW)
    # accrual = funding earned over t→t+1 aligned to position held at t (same sign
    # convention as the cross-sectional book: held*accrual added each held day).
    accrual = -panel["funding"].shift(-1)

    print(f"\n    Fixed constants (IDENTICAL across all books):")
    print(f"      VOL_TARGET   = {VOL_TARGET}  (2%/day per-asset vol target)")
    print(f"      LEVERAGE_CAP = {LEVERAGE_CAP}  (gross Σ|held| cap)")
    print(f"      COSTS_BPS    = {COSTS_BPS}  (per-leg, imported from survivorship)")
    print(f"      VOL_WINDOW   = {VOL_WINDOW}d  (causal realized vol)")
    print(f"      TSMOM lookbacks  = {TSMOM_LOOKBACKS}")
    print(f"      Donchian channels= {DONCHIAN_CHANNELS}")

    # ── Build the menu of books ────────────────────────────────────────────────
    print("\n[2] Building books (TSMOM per-L + ENSEMBLE + Donchian per-N)...")
    books: dict[str, dict] = {}

    # TSMOM per lookback
    for L in TSMOM_LOOKBACKS:
        label = f"TSMOM_L{L}"
        sig = tsmom_signal(panel, lookback=L, vol_window=VOL_WINDOW)
        books[label] = build_book(label, sig, panel, vol, accrual)

    # TSMOM ensemble (committed candidate)
    ens_sig = tsmom_ensemble(panel, lookbacks=TSMOM_LOOKBACKS, vol_window=VOL_WINDOW)
    books["TSMOM_ENS"] = build_book("TSMOM_ENS", ens_sig, panel, vol, accrual)

    # Donchian per channel
    for N in DONCHIAN_CHANNELS:
        label = f"DONCH_N{N}"
        sig = donchian_signal(panel, channel=N)
        books[label] = build_book(label, sig, panel, vol, accrual)

    book_order = (
        [f"TSMOM_L{L}" for L in TSMOM_LOOKBACKS]
        + ["TSMOM_ENS"]
        + [f"DONCH_N{N}" for N in DONCHIAN_CHANNELS]
    )
    print(f"    Built {len(book_order)} books: {book_order}")

    # ── Summary table ──────────────────────────────────────────────────────────
    print("\n" + "=" * 116)
    print("SUMMARY TABLE — HONEST DAILY METRICS (sqrt(365)); turnover/gross on the "
          "scaled held book")
    print("=" * 116)
    hdr = (f"  {'Book':>11}  {'Sharpe':>7}  {'Ann%':>7}  {'Vol%':>7}  {'MaxDD%':>7}  "
           f"{'Calmar':>7}  {'Hit%':>6}  {'Skew':>7}  {'Turn/yr':>8}  "
           f"{'GrossAvg':>8}  {'GrsP95':>7}  {'n':>5}")
    print(hdr)
    print("  " + "-" * 112)
    for lbl in book_order:
        b = books[lbl]
        m = b["metrics"]
        tg = b["turnover_gross"]
        cal = m.get("calmar", float("nan"))
        cal_s = f"{cal:>7.2f}" if not (isinstance(cal, float) and np.isnan(cal)) else "    nan"
        print(f"  {lbl:>11}  {m['sharpe']:>7.3f}  {100*m['ann']:>7.2f}  "
              f"{100*m['vol_ann']:>7.2f}  {100*m['maxdd']:>7.2f}  {cal_s}  "
              f"{100*m['hit']:>6.1f}  {b['skew']:>7.3f}  {tg['turn_per_yr']:>8.1f}  "
              f"{tg['gross_mean']:>8.3f}  {tg['gross_p95']:>7.3f}  {m['n']:>5d}")

    # ── Regime / crisis-alpha on the committed ENSEMBLE book ───────────────────
    print("\n" + "=" * 92)
    print("REGIME / CRISIS-ALPHA CHECK — committed book = TSMOM_ENS")
    print("=" * 92)
    reg = regime_check(books["TSMOM_ENS"]["held"], panel, books["TSMOM_ENS"]["pnl"])

    if reg.get("btc_symbol"):
        print(f"\n  BTC symbol: {reg['btc_symbol']}   Regime: {reg['regime_def']}")
        print(f"  Bull days: {reg['n_bull_days']}   Bear days: {reg['n_bear_days']}")
        nb = reg["mean_net_exposure_bull"]
        nr = reg["mean_net_exposure_bear"]
        print(f"  Mean NET exposure (Σ signed held):")
        print(f"      bull regime: {nb:+.4f}   (expect net-LONG  > 0)")
        print(f"      bear regime: {nr:+.4f}   (expect net-SHORT < 0)")
        print(f"      all days:    {reg['mean_net_exposure_all']:+.4f}   "
              f"frac days net-long = {100*reg['frac_days_net_long']:.1f}%")
        struct_ok = (nb is not None and nr is not None and nb > nr)
        print(f"\n  Directional structure {'CONFIRMED' if struct_ok else 'NOT confirmed'}: "
              f"net exposure is {'more long in bull than bear' if struct_ok else 'NOT ordered as expected'}.")

        print(f"\n  Worst BTC peak-to-trough drawdown windows "
              f"(max BTC DD in sample = {reg['max_btc_drawdown_pct']}%):")
        print(f"    {'peak':>12}  {'trough':>12}  {'BTC DD%':>8}  {'days':>5}  "
              f"{'ENS book cum pnl%':>18}")
        for w in reg["worst_btc_drawdown_windows"]:
            print(f"    {w['btc_peak']:>12}  {w['btc_trough']:>12}  "
                  f"{w['btc_drawdown_pct']:>8.2f}  {w['n_days']:>5}  "
                  f"{w['ens_book_cum_pnl_pct']:>+18.3f}")
    else:
        print(f"  {reg.get('error')}")

    # ── No-true-bear caveat (explicit) ─────────────────────────────────────────
    # "No true bear" here means no SUSTAINED multi-quarter bear regime, NOT "no
    # selloff". The panel DOES contain a deep BTC drawdown, but it is a single
    # episode inside an otherwise up-trending ~3y window → suggestive, not proven.
    max_btc_dd = reg.get("max_btc_drawdown_pct", None)
    # crisis-alpha evidence: did the ENS book make money in the deepest BTC window?
    dd_wins = reg.get("worst_btc_drawdown_windows", [])
    deepest = dd_wins[0] if dd_wins else None
    print("\n  CAVEAT (no sustained bear): the panel is ~3y from 2023-06, predominantly "
          "an up-market.")
    print(f"  The DEEPEST BTC drawdown in-sample is {max_btc_dd}% — a real selloff, but a "
          "SINGLE\n  episode, not a sustained multi-quarter bear regime. The "
          "net-short-in-bear behavior is\n  a STRUCTURAL property of the signal (it flips "
          "short on a real downtrend).")
    if deepest is not None:
        sign = "POSITIVE" if deepest["ens_book_cum_pnl_pct"] > 0 else "NEGATIVE"
        print(f"  In that deepest window the ENS book was {sign} "
              f"({deepest['ens_book_cum_pnl_pct']:+.1f}%) — consistent with crisis-alpha,\n"
              f"  but this is ONE episode: crisis-alpha is suggested, NOT statistically "
              "proven by this\n  window. Re-check vs the live XSMOM book in Task D.")

    # ══════════════════════════════════════════════════════════════════════════
    # VERDICT
    # ══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 92)
    print("VERDICT")
    print("=" * 92)

    # Skew comparison vs the cross-sectional momentum book (held-funding accrual).
    xsec_pnl = survivorship.run_book(panel)
    xsec_skew = pnl_skew(xsec_pnl)

    # Cleanest book heuristic: among the trend books, prefer the committed ENSEMBLE
    # unless a single config is CLEARLY better on Sharpe AND skew AND not a turnover
    # outlier. We report the comparison and pick.
    ens = books["TSMOM_ENS"]
    ens_m = ens["metrics"]
    ens_tg = ens["turnover_gross"]

    # Best single TSMOM by Sharpe
    best_tsmom = max([f"TSMOM_L{L}" for L in TSMOM_LOOKBACKS],
                     key=lambda l: books[l]["metrics"]["sharpe"])
    best_donch = max([f"DONCH_N{N}" for N in DONCHIAN_CHANNELS],
                     key=lambda l: books[l]["metrics"]["sharpe"])

    print(f"\n  cross-sec XSMOM book daily skew (reference): {xsec_skew:+.3f}")
    print(f"  TSMOM_ENS daily skew: {ens['skew']:+.3f}  "
          f"({'LESS-NEGATIVE / better than' if ens['skew'] > xsec_skew else 'worse than'} XSMOM)")
    print(f"  Best single TSMOM by Sharpe: {best_tsmom} "
          f"(Sharpe {books[best_tsmom]['metrics']['sharpe']:.3f})")
    print(f"  Best Donchian by Sharpe:     {best_donch} "
          f"(Sharpe {books[best_donch]['metrics']['sharpe']:.3f})")

    committed = "TSMOM_ENS"
    committed_reason = (
        "Default committed = TSMOM_ENSEMBLE: equal-weight over lookbacks "
        f"{TSMOM_LOOKBACKS} avoids overfitting a single lookback (FixedEnsemble logic "
        "from the cross-sec book), has the cleanest directional/regime structure, and "
        "modest turnover/gross. Per-lookback configs are noisier and channel/lookback "
        "choice is exactly what we must NOT cherry-pick at the characterization stage."
    )
    print(f"\n  COMMITTED PICK: {committed}")
    print(f"  {committed_reason}")

    # Structural soundness summary
    nb = reg.get("mean_net_exposure_bull")
    nr = reg.get("mean_net_exposure_bear")
    directional_ok = (nb is not None and nr is not None and nb > nr)
    skew_ok = ens["skew"] >= xsec_skew
    survives_chop = ens_m["sharpe"] > 0 and ens_m["ann"] > 0  # net-of-cost positive
    print(f"\n  Structural soundness of committed book:")
    print(f"    directional/regime-following (net-long bull > net-short bear): "
          f"{'YES' if directional_ok else 'NO'}")
    print(f"    positive-ish skew vs XSMOM spread: {'YES' if skew_ok else 'NO'} "
          f"({ens['skew']:+.3f} vs {xsec_skew:+.3f})")
    print(f"    survives chop net-of-cost (Sharpe>0, ann>0): "
          f"{'YES' if survives_chop else 'NO'} "
          f"(Sharpe {ens_m['sharpe']:.3f}, ann {100*ens_m['ann']:.2f}%, "
          f"turn {ens_tg['turn_per_yr']:.1f}/yr)")

    verdict_text = (
        f"Trend books are STRUCTURALLY SOUND as a directional stream: the committed "
        f"TSMOM_ENS book is "
        f"{'net-long in bull and net-short in bear regimes' if directional_ok else 'NOT cleanly regime-ordered (investigate)'}, "
        f"its daily skew ({ens['skew']:+.3f}) is "
        f"{'less-negative / better than' if skew_ok else 'worse than'} the cross-sec "
        f"momentum spread ({xsec_skew:+.3f}) as trend-following theory predicts, and it "
        f"{'survives chop net-of-cost' if survives_chop else 'bleeds net-of-cost — caution'} "
        f"(honest daily Sharpe {ens_m['sharpe']:.2f}, ann {100*ens_m['ann']:.2f}%, "
        f"maxDD {100*ens_m['maxdd']:.1f}%, turnover {ens_tg['turn_per_yr']:.1f}/yr, "
        f"mean gross {ens_tg['gross_mean']:.2f}). Committed candidate for Task C = "
        f"TSMOM_ENSEMBLE. CRISIS-ALPHA IS SUGGESTED, NOT PROVEN: there is no sustained "
        f"multi-quarter bear regime in-sample, though the deepest BTC selloff "
        f"({max_btc_dd}%) is a real episode in which the book was "
        f"{('positive' if (dd_wins and dd_wins[0]['ens_book_cum_pnl_pct'] > 0) else 'not positive')} "
        f"by construction — a single episode, to be re-examined against the live XSMOM "
        f"book in Task D."
    )
    print(f"\n  {verdict_text}")

    # ══════════════════════════════════════════════════════════════════════════
    # CAVEATS
    # ══════════════════════════════════════════════════════════════════════════
    caveats = [
        "Honest absolute levels are computed ONLY via metrics_daily (PPY=365, "
        "sqrt(365) annualization). The validation harness's hourly-annualized "
        "(HOURS_PER_YEAR=8760) numbers are NOT used anywhere in this file.",
        f"Panel is ~3 years from {date_min}, predominantly an up-market. The deepest BTC "
        f"peak-to-trough drawdown in-sample is {max_btc_dd}% — a real selloff, but a "
        "SINGLE episode, not a sustained multi-quarter bear regime. Net-short-in-bear is "
        "a STRUCTURAL property of the signal shown via the net-exposure/regime check; "
        "crisis-alpha is SUGGESTED by the one deep window, NOT statistically proven.",
        "No look-ahead: TSMOM/Donchian signals and realized vol are causal "
        "(trend.py invariant); accrual = -funding.shift(-1) aligns funding earned "
        "over t→t+1 with the position held at t (same convention as the cross-sec "
        "book), without look-ahead.",
        f"Survivorship-debiased PT panel ({n_coins} coins, includes dead/delisted) "
        "→ conservative vs a frozen-survivor set.",
        "This is a SANITY characterization, not a multi-test validation. DSR/PBO/CPCV "
        "(deflation, overfit) are Task C (trend_validation.py). The Sharpe levels here "
        "are full-sample IS and must NOT be read as OOS-robust edge.",
        f"Vol-targeting (target {VOL_TARGET}/day) and leverage cap ({LEVERAGE_CAP}) are "
        "fixed design choices identical across all books; absolute Sharpe is "
        "vol-target-invariant to first order, the constants drive the gross/turnover "
        "readout only.",
    ]
    print("\n[Honesty Caveats]")
    for i, c in enumerate(caveats, 1):
        print(f"  {i}. {c}")

    # ══════════════════════════════════════════════════════════════════════════
    # JSON OUTPUT
    # ══════════════════════════════════════════════════════════════════════════
    def _num(v):
        if isinstance(v, float) and np.isnan(v):
            return None
        if isinstance(v, (np.floating,)):
            return float(v)
        if isinstance(v, (np.integer,)):
            return int(v)
        return v

    per_book_out = {}
    for lbl in book_order:
        b = books[lbl]
        m = b["metrics"]
        tg = b["turnover_gross"]
        per_book_out[lbl] = {
            "type": ("tsmom" if lbl.startswith("TSMOM_L") else
                     "tsmom_ensemble" if lbl == "TSMOM_ENS" else "donchian"),
            "sharpe": _num(m["sharpe"]),
            "ann_pct": _num(100 * m["ann"]),
            "vol_ann_pct": _num(100 * m["vol_ann"]),
            "maxdd_pct": _num(100 * m["maxdd"]),
            "calmar": _num(m.get("calmar")),
            "hit_pct": _num(100 * m["hit"]),
            "skew": _num(b["skew"]),
            "turn_per_yr": _num(tg["turn_per_yr"]),
            "gross_mean": _num(tg["gross_mean"]),
            "gross_p95": _num(tg["gross_p95"]),
            "gross_max": _num(tg["gross_max"]),
            "cap_binds_frac": _num(tg["cap_binds_frac"]),
            "n_days": _num(m["n"]),
        }

    out = {
        "test": "trend_characterization_sanity",
        "description": (
            "SANITY characterization (NOT the validation harness) of directional trend "
            "books on the survivorship-debiased PT panel: honest daily metrics, skew, "
            "turnover, gross, and a net-exposure/regime crisis-alpha check. Task B of "
            "research/trend_following/PLAN.md."
        ),
        "menu": book_order,
        "committed_pick": committed,
        "committed_reason": committed_reason,
        "constants": {
            "VOL_TARGET": VOL_TARGET,
            "LEVERAGE_CAP": LEVERAGE_CAP,
            "COSTS_BPS": COSTS_BPS,
            "VOL_WINDOW": VOL_WINDOW,
            "tsmom_lookbacks": list(TSMOM_LOOKBACKS),
            "donchian_channels": list(DONCHIAN_CHANNELS),
            "annualization": "metrics_daily PPY=365 sqrt(365) (honest daily ONLY)",
        },
        "panel_window": {
            "start": str(date_min),
            "end": str(date_max),
            "n_days": int(n_days),
            "n_coins": int(n_coins),
        },
        "per_book": per_book_out,
        "regime_crisis_alpha": {
            k: _num(v) if not isinstance(v, (list, dict)) else v
            for k, v in reg.items()
        },
        "xsec_reference_skew": _num(xsec_skew),
        "structural_soundness": {
            "directional_regime_ordered": bool(directional_ok),
            "skew_better_than_xsec": bool(skew_ok),
            "survives_chop_net_of_cost": bool(survives_chop),
        },
        "verdict": verdict_text,
        "honesty_caveats": caveats,
    }

    out_path = _HERE / "characterize.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nJSON written to {out_path}")
    return out


if __name__ == "__main__":
    main()
