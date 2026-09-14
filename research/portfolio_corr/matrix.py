"""How correlated are the three sleeves: FRAB (live carry), B v2 (spot + trend hedge) and Trend?

Measurement, not a hypothesis test: no thresholds, no verdict — just the numbers and what they are made of.

Series
  FRAB   LIVE. Daily return = change in total_equity over the equity at the start of the day. Total
         equity is the right series: FRAB is delta neutral, so the spot leg and the perp leg offset
         inside it and what is left is the carry. Days whose equity moves more than 3% are deposits,
         withdrawals or accounting artifacts (dust, a thin book's mid) rather than P&L, and are dropped:
         over the live window that is 3 days out of 105.
  B v2   MODEL. The production CoinBook, cold-wallet config, 4 coins, $1000 each, fresh start at the
         window start (research/strategy_b_v2/limits.py). The paper test itself is one day old.
  TREND  MODEL. The committed trend book on the HL-listed coins of the point-in-time Binance panel,
         scaled to 30% annual volatility (research/trend_following/long_history.py). The paper test
         started today.
  CARRY  MODEL. The FRAB-like funding harvest on the same panel (long_history.carry_proxy) — a stand-in
         for FRAB over the years when FRAB was not live yet.

Two windows: the live one (since FRAB's first snapshot) with FRAB's real returns, and the long one
(2021-01 .. 2026-09) with the three models.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
for p in ("research/trend_following", "research/cross_sectional", "research/xsmom_v2",
          "research/validation_harness", "research/strategy_b_v2", "src"):
    sys.path.insert(0, str(ROOT / p))

import engine as X                                    # noqa: E402  xsmom_v2 panel
import limits as B                                    # noqa: E402  strategy B book
import long_history as T                              # noqa: E402  trend study
from trend import realized_vol, tsmom_ensemble, portfolio_returns_directional  # noqa: E402

SCRATCH = Path("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad")
OUT = Path(__file__).with_name("matrix.json")
LONG_START = "2021-01-01"


def frab_live(transfer_threshold: float = 0.03) -> pd.Series:
    """Daily FRAB returns from the prod snapshots, with transfer days dropped."""
    df = pd.read_csv(SCRATCH / "frab_live.csv")
    df["t"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    eq = df.set_index("t").sort_index()["total_equity"].resample("1D").last().dropna()
    r = eq.pct_change().dropna()
    dropped = int((r.abs() > transfer_threshold).sum())
    print(f"FRAB live: {len(r)} days, {dropped} dropped as transfers/artifacts (|move| > {transfer_threshold:.0%})")
    return r[r.abs() <= transfer_threshold]


def b_model(lo: str, hi: str) -> pd.Series:
    data = {c: B.load(c) for c in B.COINS}
    pf = B.portfolio(data, B.COINS, lo, hi)
    idx = data["BTC"].index[B.idx_of(data["BTC"], lo):][:len(pf["eq"])]
    eq = pd.Series(pf["eq"], index=idx).resample("1D").last().dropna()
    return eq.pct_change().dropna()


def trend_model(p) -> pd.Series:
    """The committed book on HL-listed coins, scaled to 30% annual vol."""
    panel, accr, elig = T.to_trend_panel(p, hl_only=True)
    vol = realized_vol(panel["price"], T.VOL_WINDOW)
    sig = tsmom_ensemble(panel, lookbacks=T.TSMOM_LOOKBACKS, vol_window=T.VOL_WINDOW).where(elig, 0.0)
    pnl = portfolio_returns_directional(sig, panel["fwd_ret"], T.BPS_LOW, accrual=accr, vol=vol,
                                        vol_target=T.VOL_TARGET, leverage_cap=T.LEVERAGE_CAP)
    return T.rescale(pnl).dropna()


def corr_block(series: dict[str, pd.Series], label: str) -> dict:
    df = pd.concat(series, axis=1).dropna()
    out = {"label": label, "days": len(df), "from": str(df.index.min().date()), "to": str(df.index.max().date()),
           "corr": df.corr().round(3).to_dict(), "stats": {}}
    for name in df:
        s = df[name]
        eq = (1 + s).cumprod()
        out["stats"][name] = dict(sharpe=float(s.mean() / s.std(ddof=1) * np.sqrt(365)),
                                  total_pct=float((eq.iloc[-1] - 1) * 100),
                                  vol_ann_pct=float(s.std(ddof=1) * np.sqrt(365) * 100),
                                  maxdd_pct=float(-(eq / eq.cummax() - 1).min() * 100))
    print(f"\n== {label}: {out['days']} days, {out['from']} .. {out['to']}")
    print(df.corr().round(3).to_string())
    for n, st in out["stats"].items():
        print(f"   {n:<7} Sharpe {st['sharpe']:+.2f}  vol {st['vol_ann_pct']:5.1f}%  total {st['total_pct']:+7.2f}%  maxDD {st['maxdd_pct']:.1f}%")
    # rolling 30d correlation ranges, pair by pair
    roll = {}
    cols = list(df.columns)
    for i, a in enumerate(cols):
        for b_ in cols[i + 1:]:
            r = df[a].rolling(30).corr(df[b_]).dropna()
            if len(r):
                roll[f"{a}~{b_}"] = dict(mean=float(r.mean()), min=float(r.min()), max=float(r.max()),
                                         share_below_0_3=float((r.abs() < 0.3).mean()))
                print(f"   rolling 30d {a}~{b_}: mean {r.mean():+.2f} range {r.min():+.2f}..{r.max():+.2f} "
                      f"|corr|<0.3 in {(r.abs() < 0.3).mean() * 100:.0f}% of windows")
    out["rolling30"] = roll
    return out


def blend(series: dict[str, pd.Series], label: str) -> dict:
    """Equal-risk mix (inverse volatility) of whatever is in `series`."""
    df = pd.concat(series, axis=1).dropna()
    w = (1 / df.std()) / (1 / df.std()).sum()
    mix = (df * w).sum(axis=1)
    eq = (1 + mix).cumprod()
    res = dict(weights=w.round(3).to_dict(), sharpe=float(mix.mean() / mix.std(ddof=1) * np.sqrt(365)),
               total_pct=float((eq.iloc[-1] - 1) * 100), vol_ann_pct=float(mix.std(ddof=1) * np.sqrt(365) * 100),
               maxdd_pct=float(-(eq / eq.cummax() - 1).min() * 100),
               diversification_ratio=float((df.std() * w).sum() / mix.std()))
    print(f"\n== equal-risk mix, {label}: weights {res['weights']}")
    print(f"   Sharpe {res['sharpe']:+.2f} vol {res['vol_ann_pct']:.1f}% total {res['total_pct']:+.2f}% "
          f"maxDD {res['maxdd_pct']:.1f}% | diversification ratio {res['diversification_ratio']:.2f}")
    return res


def main():
    p = X.build_panel()
    frab = frab_live()
    trend = trend_model(p)
    carry = T.carry_proxy(p)
    b_long = b_model(LONG_START, "2026-09-13")
    live_lo = str(frab.index.min().date())
    b_live = b_model(live_lo, "2026-09-13")

    res = {"live": corr_block({"FRAB": frab, "B_v2": b_live, "TREND": trend, "CARRY": carry}, "live window"),
           "long": corr_block({"B_v2": b_long, "TREND": trend, "CARRY": carry}, f"models, {LONG_START} .. 2026-09")}
    res["blend_long"] = blend({"B_v2": b_long, "TREND": trend, "CARRY": carry}, "long window")
    res["blend_long_no_carry"] = blend({"B_v2": b_long, "TREND": trend}, "long window, B + trend only")
    OUT.write_text(json.dumps(res, indent=1, default=float))


if __name__ == "__main__":
    main()
