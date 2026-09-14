"""Strategy B's rule on FX: hold the currency, step aside (hedge) when its trend turns down.

PRE-REGISTERED 2026-09-14, before any result of this script was seen.

The objection this answers: B's machinery does carry over to FX — you can short EURUSD in a MetaTrader
terminal exactly as you short a perp on HL, and no cold wallet is needed. True. So run B's ACTUAL rule
on FX and see what it earns.

B's rule, translated one-for-one:
  * hold the currency long (in crypto: spot);
  * switch the hedge on unless the 14-day AND the 30-day return are both positive — a hedge of the full
    position is economically the same as standing flat, which is what MT5 would do with a netting account;
  * sticky exit: the hedge stays on until the signal has been off for a full day (12h in the hourly
    crypto book);
  * equal weight over the 9 G10 currencies vs USD, daily decisions on daily closes.
Carry: a long XXXUSD earns the foreign 3-month rate and pays the USD rate; a hedged (flat) position earns
nothing. Costs 1 bp per leg, and 3 bps as the pessimistic check.

Compared against, over the same windows: buying and holding the same basket, and the long/short trend
book from ../trend_following/fx_trend.py.

Windows fixed in advance: the whole sample, 2006-2010, 2011-2026, 2020-2026. Also reported per currency,
because "EURUSD does not drift" is the claim being tested and a single pair may behave differently.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
for p in ("cross_sectional/fx", "cross_sectional", "trend_following"):
    sys.path.insert(0, str(HERE.parent / p))

import fxdata                                          # noqa: E402

OUT = HERE / "fx_check.json"
SHORT_D, LONG_D, STICKY_D = 14, 30, 1
WINDOWS = {"FULL": (None, None), "2006-2010": ("2006-01-01", "2010-12-31"),
           "2011-2026": ("2011-01-01", "2026-09-12"), "2020-2026": ("2020-01-01", "2026-09-12")}


def b_exposure(price: pd.Series) -> pd.Series:
    """1 while B would hold the currency, 0 while its hedge is on (sticky exit of one day)."""
    ms, ml = price / price.shift(SHORT_D) - 1, price / price.shift(LONG_D) - 1
    hedge_raw = ~((ms > 0) & (ml > 0)) | ml.isna()
    on, off, out = False, 0, []
    for r in hedge_raw.to_numpy():
        if r:
            on, off = True, 0
        elif on:
            off += 1
            if off >= STICKY_D:
                on = False
        out.append(0.0 if on else 1.0)
    return pd.Series(out, index=price.index).shift(1).fillna(0.0)      # decide on t, earn t -> t+1


def run(price: pd.DataFrame, fwd: pd.DataFrame, accr: pd.DataFrame, bps: float,
        always_long: bool = False) -> tuple[pd.Series, dict]:
    expo = pd.DataFrame({c: (pd.Series(1.0, index=price.index) if always_long else b_exposure(price[c]))
                         for c in price.columns})
    w = expo / len(price.columns)
    turn = w.diff().abs().fillna(w.abs())
    pnl = (w * fwd.fillna(0.0)).sum(axis=1) + (w * accr.fillna(0.0)).sum(axis=1) - turn.sum(axis=1) * bps / 1e4
    per_ccy = {c: (w[c] * fwd[c].fillna(0.0) + w[c] * accr[c].fillna(0.0) - turn[c] * bps / 1e4) * len(price.columns)
               for c in price.columns}
    return pnl, per_ccy


def stats(pnl: pd.Series) -> dict:
    eq = (1 + pnl).cumprod()
    years = len(pnl) / 252
    sd = pnl.std(ddof=1)
    return dict(ann=float((eq.iloc[-1] ** (1 / years) - 1) * 100) if eq.iloc[-1] > 0 else -100.0,
                total=float((eq.iloc[-1] - 1) * 100), vol=float(sd * np.sqrt(252) * 100),
                sharpe=float(pnl.mean() / sd * np.sqrt(252)) if sd else 0.0,
                maxdd=float(-(eq / eq.cummax() - 1).min() * 100), days=int(len(pnl)))


def window(s: pd.Series, lo, hi) -> pd.Series:
    if lo is None:
        return s
    tz = s.index.tz
    return s[(s.index >= pd.Timestamp(lo, tz=tz)) & (s.index <= pd.Timestamp(hi, tz=tz))]


def main():
    p = fxdata.load_panel()
    price = p["price"].dropna(how="any")
    fwd = p["fwd_ret"].loc[price.index]
    accr = (p["short_rate"].loc[price.index] / 100.0).sub(p["usd_rate"].loc[price.index].iloc[:, 0] / 100.0,
                                                          axis=0) / 252.0
    print(f"FX: {len(price)} days {price.index.min().date()} .. {price.index.max().date()}, "
          f"{len(price.columns)} currencies", flush=True)

    b_pnl, b_per = run(price, fwd, accr, 1.0)
    b_pnl3, _ = run(price, fwd, accr, 3.0)
    hold_pnl, _ = run(price, fwd, accr, 1.0, always_long=True)

    res = {"windows": {}, "per_currency": {}}
    for wname, (lo, hi) in WINDOWS.items():
        row = {"B_rule_1bps": stats(window(b_pnl, lo, hi)), "B_rule_3bps": stats(window(b_pnl3, lo, hi)),
               "buy_and_hold": stats(window(hold_pnl, lo, hi))}
        res["windows"][wname] = row
        print(f"\n{wname}")
        for k, v in row.items():
            print(f"   {k:<13} {v['ann']:+6.1f}%/yr  vol {v['vol']:5.1f}%  DD {v['maxdd']:5.1f}  Sharpe {v['sharpe']:+.2f}")

    print("\nпо валютам, правило B, 1 б.п. (весь период / с 2011):")
    for c, s in b_per.items():
        full, late = stats(s), stats(window(s, "2011-01-01", "2026-09-12"))
        res["per_currency"][c] = dict(full=full, since_2011=late)
        print(f"   {c}: {full['ann']:+5.1f}%/yr (Шарп {full['sharpe']:+.2f})  |  с 2011 {late['ann']:+5.1f}%/yr "
              f"(Шарп {late['sharpe']:+.2f})")
    OUT.write_text(json.dumps(res, indent=1, default=float))


if __name__ == "__main__":
    main()
