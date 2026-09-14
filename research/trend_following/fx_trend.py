"""Does the committed trend book work on FX too?

PRE-REGISTERED 2026-09-14, before any result of this script was seen.

Same book, different market: the equal-weight TSMOM ensemble over 30/60/90/120 day lookbacks, each
position scaled to a per-asset daily vol target, gross capped, and — the change made today — the whole
book scaled to its own 14%/yr volatility. Universe: the 9 G10 currencies against USD from
research/cross_sectional/fx (daily spot since the 1970s-90s depending on the pair, plus 3-month rates).

Why FX and not another crypto idea: the sleeves we run are all crypto, so their shared risk is crypto
itself. FX is the one liquid market where the same trend machinery has a documented history (AQR's
century of evidence covers currencies) and where our own earlier work already built the data.

Accrual: holding XXXUSD long earns the foreign 3-month rate and pays the USD rate (the carry that a
broker charges as swap). Applied per day held, as the crypto book applies funding.
Costs: 1 bp per leg (majors on a decent broker) and 3 bps as the pessimistic check — FX spreads are
much tighter than crypto perps, but NOK/SEK are wider than EUR/JPY.

Windows, fixed in advance: the whole common sample; 1990-2010 vs 2011-2026 (trend following in
developed markets is widely reported to have decayed after 2010 — our own indices+gold study found
exactly that); and 2020-2026 to compare with the crypto book on the same years.

Reported: Sharpe, return and drawdown at the same 14%/yr book-volatility target the paper test now
runs, the drawdown at a matched 15%/yr return, and the correlation with the crypto trend book.
No verdict thresholds: the question is whether FX is worth a paper test of its own, and that needs
the second half of the sample to look like the first.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
for p in ("cross_sectional", "cross_sectional/fx", "xsmom_v2", "validation_harness"):
    sys.path.insert(0, str(HERE.parent / p))

import fxdata                                          # noqa: E402
import long_history as L                               # noqa: E402
import risk_shaping as R                               # noqa: E402
import engine as X                                     # noqa: E402
from trend import portfolio_returns_directional        # noqa: E402

OUT = HERE / "fx_trend.json"
BPS_LOW, BPS_HIGH = 1.0, 3.0
BOOK_VOL = 0.14
WINDOWS = {"FULL": (None, None), "1990-2010": ("1990-01-01", "2010-12-31"),
           "2011-2026": ("2011-01-01", "2026-09-12"), "2020-2026": ("2020-01-01", "2026-09-12")}


def fx_panel() -> tuple[dict, pd.DataFrame]:
    p = fxdata.load_panel()
    price, fwd = p["price"], p["fwd_ret"]
    common = price.dropna(how="any").index
    price, fwd = price.loc[common], fwd.loc[common]
    # accrual: long XXXUSD earns the foreign rate and pays USD, per day held
    rate = p["short_rate"].loc[common] / 100.0
    usd = p["usd_rate"].loc[common].iloc[:, 0] / 100.0
    accr = (rate.sub(usd, axis=0)) / 252.0
    return dict(coins=list(price.columns), price=price, fwd_ret=fwd), accr


def window(s: pd.Series, lo, hi) -> pd.Series:
    if lo is None:
        return s
    return s[(s.index >= pd.Timestamp(lo)) & (s.index <= pd.Timestamp(hi))]


def main():
    panel, accr = fx_panel()
    price = panel["price"]
    print(f"FX panel: {len(price)} business days {price.index.min().date()} .. {price.index.max().date()}, "
          f"{len(panel['coins'])} currencies: {', '.join(panel['coins'])}", flush=True)

    elig = price.notna()
    pos = R.scaled_positions(panel, elig)
    res = {"windows": {}}
    books = {}
    for bps in (BPS_LOW, BPS_HIGH):
        pnl = portfolio_returns_directional(pos, panel["fwd_ret"], bps, accrual=accr)
        books[bps] = R.book_vol_scaled(pnl, target_ann=BOOK_VOL)

    for wname, (lo, hi) in WINDOWS.items():
        res["windows"][wname] = {}
        for bps, pnl in books.items():
            s = window(pnl, lo, hi)
            if len(s) < 200:
                continue
            st = R.stats_at(s)
            st15 = R.stats_at(s, 0.15)
            res["windows"][wname][f"{bps}bps"] = dict(**st, dd_at_15pct=st15["maxdd"])
            print(f"{wname:<10} {bps:>3.0f}bps  {st['ann']:+6.1f}%/yr  vol {s.std() * np.sqrt(252) * 100:5.1f}%  "
                  f"DD {st['maxdd']:5.1f}  Sharpe {st['sharpe']:+.2f}  | при 15%/год DD {st15['maxdd']:5.1f}%", flush=True)

    # correlation with the crypto trend book over the years they overlap
    p = X.build_panel()
    cpanel, caccr, celig = L.to_trend_panel(p, hl_only=True)
    crypto = R.book_vol_scaled(L.rescale(portfolio_returns_directional(
        R.scaled_positions(cpanel, celig), cpanel["fwd_ret"], L.BPS_LOW, accrual=caccr)), target_ann=BOOK_VOL)
    fx = books[BPS_LOW]
    j = pd.concat([fx.rename("fx"), crypto.rename("crypto")], axis=1).dropna()
    res["corr_with_crypto_trend"] = dict(days=len(j), corr=float(j["fx"].corr(j["crypto"])))
    both = (j["fx"] + j["crypto"]) / 2
    res["fifty_fifty"] = dict(**R.stats_at(both), dd_at_15pct=R.stats_at(both, 0.15)["maxdd"])
    print(f"\ncorr FX trend ~ crypto trend: {res['corr_with_crypto_trend']['corr']:+.3f} over {len(j)} common days")
    print(f"half FX + half crypto: {res['fifty_fifty']['ann']:+.1f}%/yr DD {res['fifty_fifty']['maxdd']:.1f} "
          f"Sharpe {res['fifty_fifty']['sharpe']:+.2f} | при 15%/год DD {res['fifty_fifty']['dd_at_15pct']:.1f}%")
    OUT.write_text(json.dumps(res, indent=1, default=float))


if __name__ == "__main__":
    main()
