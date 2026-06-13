"""
bear_regime.py — Bear-regime stress test for the crypto cross-sectional momentum book.

Runs the IDENTICAL ensemble logic (momentum_ensemble lookbacks (14,21,30,45,60),
rank_to_weights tercile 1/3, portfolio_returns costs_bps=8.5 rebal_every=7,
accrual=-funding.shift(-1)) on a 2021-01-01 → 2023-01-01 Binance-perp panel,
covering:
  - Full bear window (2021-01 → 2023-01)
  - ATH-to-FTX crash window (2021-11-10 → 2022-11-10)
  - LUNA collapse (2022-04-05 → 2022-05-20)
  - FTX collapse (2022-11-01 → 2022-11-30)

NO look-ahead:
  - Signals use only price data at index <= t (momentum = price[t]/price[t-lb]-1,
    computed on the full panel with shift).
  - Funding accrual = -funding.shift(-1): realized over the forward hold t→t+1
    using funding printed DURING day t+1, not used in any signal. It mirrors
    exactly the seam-safe alignment in funding_impact.py and the HL-era book.
  - fwd_ret[t] = price[t+1]/price[t]-1, indexed at t, never read by signals.

Data: Binance hourly OHLCV + Binance USDⓈ-M 8h funding, fetched via bear_fetch.py.
      Daily funding = sum of the day's 8h prints (exactly analogous to cryptodata's
      daily HL funding = sum of the day's hourly prints).

Hyperparameters are IDENTICAL to the validated HL-era book — no new knobs.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import bear_fetch
import signals
import xsec
from metrics_daily import daily_metrics

_HERE = Path(__file__).parent

# ── Hyperparameters — IDENTICAL to validated book ──────────────────────────────
LOOKBACKS  = (14, 21, 30, 45, 60)   # momentum ensemble lookbacks
COSTS_BPS  = 8.5                    # HL perp taker + slippage (bps / leg)
REBAL      = 7                      # weekly rebalance
TERCILE    = 1 / 3                  # tercile fraction

BEAR_START = pd.Timestamp("2021-01-01", tz="UTC")
BEAR_END   = pd.Timestamp("2023-01-01", tz="UTC")

# Named sub-windows for crash analysis
SUBWINDOWS = {
    "full_bear_2021-2022":      ("2021-01-01", "2022-12-31"),
    "ATH_to_FTX_crash":         ("2021-11-10", "2022-11-10"),
    "LUNA_collapse":            ("2022-04-05", "2022-05-20"),
    "FTX_collapse":             ("2022-11-01", "2022-11-30"),
    "H1_2021_bull":             ("2021-01-01", "2021-06-30"),
    "H2_2021_toATH":            ("2021-07-01", "2021-11-09"),
}

# ── Panel builder (mirrors cryptodata logic, NOT editing cryptodata.py) ────────

HISTORY_START_MS = int(bear_fetch.BEAR_START_MS)


def _bear_daily_price(coin: str) -> pd.Series:
    """Daily close price from Binance hourly OHLCV cached by bear_fetch.

    Daily close = last hourly close of the day (same as cryptodata._daily_price).
    """
    p = bear_fetch.DATA_DIR / f"bear_{coin}_1h.csv"
    if not p.exists():
        return pd.Series(dtype=float)
    df = pd.read_csv(p)
    df["time"] = pd.to_datetime(df["time"], format="ISO8601", utc=True).dt.floor("h")
    df = df.set_index("time")["close"].astype(float).sort_index()
    # trim to bear window
    df = df[(df.index >= BEAR_START) & (df.index < BEAR_END + pd.Timedelta(days=2))]
    return df.resample("1D").last()


def _bear_daily_funding(coin: str) -> pd.Series:
    """Daily funding = SUM of day's Binance 8h prints (analogous to cryptodata).

    Binance charges funding at 00:00, 08:00, 16:00 UTC — three prints per day.
    Summing them is the daily carry equivalent, exactly analogous to cryptodata's
    'daily carry = SUM of the day's hourly funding rates'.
    """
    p = bear_fetch.DATA_DIR / f"bear_{coin}_funding.csv"
    if not p.exists():
        return pd.Series(dtype=float)
    df = pd.read_csv(p)
    df["fundingTime"] = pd.to_datetime(df["fundingTime"], format="ISO8601", utc=True)
    df = df.set_index("fundingTime")["fundingRate"].astype(float).sort_index()
    # trim to bear window
    df = df[(df.index >= BEAR_START) & (df.index < BEAR_END + pd.Timedelta(days=2))]
    # sum intraday 8h prints to get daily total
    return df.resample("1D").sum()


def build_bear_panel(coins: list[str]) -> dict:
    """Build aligned daily bear-era panel for `coins`.

    Mirrors cryptodata.load_panel structure exactly:
      price   — DataFrame[date x coin]  daily close
      fwd_ret — DataFrame[date x coin]  next-day close-to-close return (forward)
      funding — DataFrame[date x coin]  daily summed funding rate

    NaN where a coin is not yet listed or outside its Binance data window.
    """
    price_cols, fund_cols = {}, {}
    for c in coins:
        pr = _bear_daily_price(c)
        fu = _bear_daily_funding(c)
        if not pr.empty:
            price_cols[c] = pr
        if not fu.empty:
            fund_cols[c] = fu

    if not price_cols:
        raise RuntimeError("No price data available for bear panel")

    price = pd.DataFrame(price_cols).sort_index()
    full_idx = pd.date_range(price.index.min(), BEAR_END, freq="1D", tz="UTC")
    price = price.reindex(full_idx)

    funding = pd.DataFrame(fund_cols).reindex(full_idx)
    # funding only meaningful where coin is price-listed
    funding = funding.where(price.notna())

    # forward return: r_{t+1} = P_{t+1}/P_t - 1, indexed at t (seam-safe)
    fwd_ret = price.shift(-1) / price - 1.0

    return {"coins": list(price.columns), "price": price, "fwd_ret": fwd_ret,
            "funding": funding}


# ── Metrics helpers ────────────────────────────────────────────────────────────

def _fmt(m: dict) -> str:
    if not m:
        return "  (too few days)"
    return (f"Sharpe {m['sharpe']:+.3f}  ann {100*m['ann']:+6.2f}%  "
            f"vol {100*m['vol_ann']:.1f}%  maxDD {100*m['maxdd']:5.2f}%  "
            f"Calmar {m['calmar']:+5.2f}  hit {100*m['hit']:.1f}%  n={m['n']}")


def _worst_week(pnl: pd.Series) -> tuple[str, float]:
    """Find the worst 7-day rolling window return."""
    eq = (1 + pnl.dropna()).cumprod()
    worst_ret = float("inf")
    worst_date = None
    vals = eq.values
    idx = eq.index
    for i in range(7, len(vals)):
        ret = vals[i] / vals[i - 7] - 1.0
        if ret < worst_ret:
            worst_ret = ret
            worst_date = idx[i]
    return str(worst_date.date()) if worst_date else "?", worst_ret


def _worst_rebalance(pnl: pd.Series, rebal: int = REBAL) -> tuple[str, float]:
    """Worst single rebalance cycle (sum over rebal days)."""
    r = pnl.dropna()
    worst = float("inf")
    worst_date = None
    for i in range(0, len(r) - rebal, rebal):
        window_ret = r.iloc[i:i + rebal].sum()
        if window_ret < worst:
            worst = window_ret
            worst_date = r.index[i]
    return str(worst_date.date()) if worst_date else "?", worst


def _subwindow_metrics(pnl: pd.Series, label: str,
                       start: str, end: str) -> dict:
    s, e = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    sub = pnl.loc[(pnl.index >= s) & (pnl.index <= e)]
    m = daily_metrics(sub)
    return m


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    pd.set_option("display.width", 200)

    print("=" * 80)
    print("BEAR-REGIME STRESS TEST — 2021-01 → 2023-01 (BINANCE PERPS)")
    print("Identical ensemble logic: momentum_ensemble (14,21,30,45,60), "
          "8.5bps, rebal=7, accrual=-funding.shift(-1)")
    print("=" * 80)

    # ── Step 1: ensure all bear data is cached ─────────────────────────────────
    print("\n[1] Downloading / checking bear-era data cache...")
    available, not_available = [], {}
    for coin in bear_fetch.BEAR_BASKET:
        ok_o, ok_f = bear_fetch.ensure_bear_coin(coin)
        if ok_o:
            # check how much history we actually have
            pr = _bear_daily_price(coin)
            # require at least max(lookbacks)=60 days to be useful
            if pr.dropna().__len__() >= max(LOOKBACKS) + 10:
                available.append(coin)
            else:
                not_available[coin] = "too short (<70 days of bear-era price)"
        else:
            not_available[coin] = "OHLCV not available on Binance"
        if not ok_f:
            not_available.setdefault(coin, "")
            not_available[coin] += " | funding not available on Binance perp"

    print(f"\nAvailable for bear panel ({len(available)} coins): {available}")
    if not_available:
        print(f"NOT available ({len(not_available)} coins):")
        for c, reason in not_available.items():
            print(f"  {c}: {reason}")

    if len(available) < 5:
        print("ERROR: fewer than 5 coins available — cannot build panel")
        sys.exit(1)

    # ── Step 2: build panel ────────────────────────────────────────────────────
    print("\n[2] Building bear-era daily panel...")
    panel = build_bear_panel(available)
    price, fwd_ret, funding = panel["price"], panel["fwd_ret"], panel["funding"]
    coins_in_panel = panel["coins"]

    print(f"Panel: {price.index.min().date()} → {price.index.max().date()}  "
          f"({len(price)} days)  {len(coins_in_panel)} coins")
    coverage = price.notna().sum().sum() / price.size * 100
    print(f"Coverage: {coverage:.1f}% non-NaN cells")

    # Show per-coin first/last dates
    print(f"\n{'coin':<8}{'first':>12}{'last':>12}{'price_days':>12}{'fund_days':>12}")
    for c in sorted(coins_in_panel):
        pr_c = price[c].dropna()
        fu_c = funding[c].dropna()
        first = pr_c.index.min().date() if len(pr_c) else "?"
        last  = pr_c.index.max().date() if len(pr_c) else "?"
        print(f"{c:<8}{str(first):>12}{str(last):>12}{len(pr_c):>12}{len(fu_c):>12}")

    # ── Step 3: run the same ensemble book ────────────────────────────────────
    print("\n[3] Computing momentum_ensemble signals and portfolio pnl...")
    score = signals.momentum_ensemble(panel, lookbacks=LOOKBACKS)
    weights = xsec.rank_to_weights(score)

    # how many coins are eligible per date?
    n_valid = score.notna().sum(axis=1)
    warmup_end = (n_valid >= 2).idxmax()
    print(f"First date with >=2 valid coins (warmup done): {warmup_end.date()}")

    # build pnl with and without funding accrual
    accrual_panel = -funding.shift(-1)  # per cryptodata / funding_impact convention
    pnl_nofund = xsec.portfolio_returns(
        weights, fwd_ret, costs_bps=COSTS_BPS, rebal_every=REBAL,
    )
    pnl_fund = xsec.portfolio_returns(
        weights, fwd_ret, costs_bps=COSTS_BPS, rebal_every=REBAL,
        accrual=accrual_panel,
    )
    # trim to warmup-done period (exclude NaN / zero-weight warmup)
    pnl_nofund = pnl_nofund[pnl_nofund.index >= warmup_end]
    pnl_fund   = pnl_fund[pnl_fund.index >= warmup_end]

    # ── Step 4: full-window metrics ────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("FULL BEAR WINDOW METRICS (daily √365 annualization)")
    print("=" * 80)
    m_nf = daily_metrics(pnl_nofund)
    m_f  = daily_metrics(pnl_fund)
    print(f"\nWithout funding accrual : {_fmt(m_nf)}")
    print(f"With    funding accrual : {_fmt(m_f)}")
    funding_drag = m_nf.get("ann", 0) - m_f.get("ann", 0)
    print(f"Funding drag            : {100*funding_drag:+.2f}%/yr")

    # worst week and worst rebalance
    ww_date, ww_ret = _worst_week(pnl_fund)
    wr_date, wr_ret = _worst_rebalance(pnl_fund)
    print(f"\nWorst week (7d window ending {ww_date}): {100*ww_ret:+.2f}%")
    print(f"Worst rebal cycle (starting {wr_date}): {100*wr_ret:+.2f}%")

    # ── Step 5: named crash sub-windows ────────────────────────────────────────
    print("\n" + "=" * 80)
    print("NAMED CRASH SUB-WINDOW METRICS (WITH funding accrual)")
    print("=" * 80)
    results_by_window = {}
    for label, (start, end) in SUBWINDOWS.items():
        m = _subwindow_metrics(pnl_fund, label, start, end)
        results_by_window[label] = m
        print(f"\n{label}  ({start} → {end})")
        print(f"  {_fmt(m)}")
        if m:
            # worst week in sub-window
            s, e = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
            sub = pnl_fund.loc[(pnl_fund.index >= s) & (pnl_fund.index <= e)]
            if len(sub) >= 7:
                ww_d, ww_r = _worst_week(sub)
                print(f"  worst 7d window ending {ww_d}: {100*ww_r:+.2f}%")

    # ── Step 6: year-by-year breakdown ────────────────────────────────────────
    print("\n" + "=" * 80)
    print("YEAR-BY-YEAR BREAKDOWN (WITH funding accrual)")
    print("=" * 80)
    print(f"  {'year':<6}{'n':>5}{'ann%':>9}{'sharpe':>9}{'maxDD%':>9}{'hit%':>8}")
    yearly = {}
    for yr in [2021, 2022]:
        msk = pnl_fund.index.year == yr
        sub = pnl_fund[msk]
        m = daily_metrics(sub)
        yearly[yr] = m
        if m:
            print(f"  {yr:<6}{m['n']:>5}{100*m['ann']:>9.2f}"
                  f"{m['sharpe']:>9.3f}{100*m['maxdd']:>9.2f}{100*m['hit']:>8.1f}")
        else:
            print(f"  {yr:<6}  (too few days)")

    # ── Step 7: hypothesis check ───────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("VERDICT — DID CROSS-SECTIONAL MOMENTUM SURVIVE THE 2021-22 CRYPTO BEAR?")
    print("=" * 80)
    if m_f:
        sharpe_bear = m_f["sharpe"]
        maxdd_bear  = m_f["maxdd"]
        ann_bear    = m_f["ann"]

        # Prior from CRITICAL_REVIEW.md: "maxDD in a real bear could be 1.5-2× the
        # friendly-regime 26%", i.e. expect 39-52% maxDD in a bad regime.
        # HL-era baseline Sharpe ~1.34, ann ~50%, maxDD ~26%.
        hl_era_sharpe = 1.34
        hl_era_maxdd  = 0.26

        ratio_maxdd = maxdd_bear / hl_era_maxdd
        print(f"\nBear-era (with funding): Sharpe {sharpe_bear:+.3f}, ann {100*ann_bear:+.1f}%, "
              f"maxDD {100*maxdd_bear:.1f}%")
        print(f"HL-era baseline:         Sharpe {hl_era_sharpe:.2f}, ann ~50%, "
              f"maxDD {100*hl_era_maxdd:.0f}%")
        print(f"MaxDD ratio bear/HL:     {ratio_maxdd:.2f}x")
        print(f"(Review prior: expect 1.5-2x → {100*hl_era_maxdd*1.5:.0f}-"
              f"{100*hl_era_maxdd*2.0:.0f}% — confirmed / refuted by number above)")

        if sharpe_bear > 0.5:
            verdict = "SURVIVED — positive Sharpe, strategy retains edge in bear"
        elif sharpe_bear > 0:
            verdict = "LIMPING — weakly positive but Sharpe < 0.5, deep doubt"
        else:
            verdict = "FAILED — negative Sharpe in bear-era, edge evaporates"
        print(f"\nVerdict: {verdict}")

    # ── Step 8: dump JSON ──────────────────────────────────────────────────────
    def _safe(d):
        if not d:
            return {}
        return {k: (float(v) if isinstance(v, (float, np.floating)) else
                    int(v)   if isinstance(v, (int, np.integer)) else v)
                for k, v in d.items()}

    out = {
        "test": "bear_regime",
        "description": (
            "Bear-regime stress: identical ensemble book on Binance-perp panel "
            "2021-01-01 → 2023-01-01. Same hyperparams as HL-era validated book."
        ),
        "data_source": "Binance USDM futures klines (hourly) + fundingRate (8h)",
        "panel_coins_available": available,
        "panel_coins_not_available": not_available,
        "window": {
            "start": str(pnl_fund.index.min().date()),
            "end":   str(pnl_fund.index.max().date()),
            "n_days": int(m_f.get("n", 0)) if m_f else 0,
        },
        "full_window_without_funding": _safe(m_nf),
        "full_window_with_funding": _safe(m_f),
        "funding_drag_ann_pct": float(funding_drag * 100),
        "worst_week": {"date_end": ww_date, "return_pct": float(ww_ret * 100)},
        "worst_rebalance": {"date_start": wr_date, "return_pct": float(wr_ret * 100)},
        "subwindows": {
            lbl: _safe(results_by_window.get(lbl, {}))
            for lbl in SUBWINDOWS
        },
        "yearly": {str(yr): _safe(m) for yr, m in yearly.items()},
        "hl_era_reference": {
            "sharpe": 1.34, "ann_pct": 50.0, "maxdd_pct": 26.0,
            "note": "validated HL-era (2023-06 → 2026-06) from CRITICAL_REVIEW.md"
        },
    }

    out_path = _HERE / "bear_regime.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nResults written to {out_path}")
