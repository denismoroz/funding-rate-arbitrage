"""FX carry: what survives at futures-level costs, and what the emerging markets pay.

PRE-REGISTERED 2026-09-14, before any result of this script was seen.

The question behind it: trend is dead on FX after 2011 and B's rule has nothing to protect there, but carry
is a different animal — you are paid for holding, like FRAB in crypto. Our earlier FX study found the
carry edge real but mostly eaten by a retail broker's swap markup. So measure carry twice:
  A. G10, priced at what a CME futures account actually pays;
  B. emerging markets, where the rate gaps are multiples of G10's.

Book (identical for both, no timing, no parameter search):
  * monthly rebalance on the last business day;
  * long the 3 highest 3-month rates, short the 3 lowest, equal weight inside each side, gross 2;
  * a position earns the spot move plus the rate differential to USD, accrued daily;
  * the book is scaled to 10% annual volatility so the venues can be compared on the same risk.
EM also gets the version an actual account would run: long the 4 highest-yielding EM currencies funded
in USD, no short leg.

Costs, the point of the exercise:
  * trade cost per leg: 0.5 bp = CME FX futures (one tick on EUR is ~0.45 bp, commission ~0.15 bp),
    1.5 bp = spot at an ECN broker, 5 and 10 bp = emerging markets, where spreads are wide;
  * carry haircut: what a broker keeps of the rate differential — 0 (futures, where carry is in the
    basis and nobody skims it), 0.5% and 1.5% a year (typical retail swap markups), 3% (EM retail).

Windows fixed in advance: 2006-2010, 2011-2019, 2020-2026, full. Reported per window: return, volatility,
drawdown, Sharpe, and the three worst months — carry's risk is not volatility, it is the crash.

Data: G10 from research/cross_sectional/fx (spot + OECD 3-month rates). EM spot from FRED daily (MXN BRL
ZAR INR KRW) and Yahoo (PLN HUF CLP), EM rates from FRED's OECD monthly series. Every series is quoted as
USD per unit of foreign currency, so "long the currency" means the price goes up.
"""
from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
for p in ("cross_sectional/fx", "cross_sectional"):
    sys.path.insert(0, str(HERE.parent / p))
import fxdata                                          # noqa: E402

CACHE = Path("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/fx_em")
OUT = HERE / "carry_em.json"
EM = {                       # currency: (FRED rate series, spot source)
    "MXN": ("IR3TIB01MXM156N", ("fred", "DEXMXUS")),
    "BRL": ("IRSTCI01BRM156N", ("fred", "DEXBZUS")),
    "ZAR": ("IR3TIB01ZAM156N", ("fred", "DEXSFUS")),
    "INR": ("IRSTCI01INM156N", ("fred", "DEXINUS")),
    "KRW": ("IR3TIB01KRM156N", ("fred", "DEXKOUS")),
    "PLN": ("IR3TIB01PLM156N", ("yahoo", "USDPLN=X")),
    "HUF": ("IR3TIB01HUM156N", ("yahoo", "USDHUF=X")),
    "CLP": ("IR3TIB01CLM156N", ("yahoo", "USDCLP=X")),
    "IDR": ("IR3TIB01IDM156N", ("yahoo", "USDIDR=X")),
}
WINDOWS = {"FULL": (None, None), "2006-2010": ("2006-01-01", "2010-12-31"),
           "2011-2019": ("2011-01-01", "2019-12-31"), "2020-2026": ("2020-01-01", "2026-09-12")}
TARGET_VOL = 0.10


# ── data ─────────────────────────────────────────────────────────────────────
def _cached(name: str, fetch) -> pd.Series:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{name}.csv"
    if not f.exists():
        fetch().to_csv(f)
        time.sleep(0.3)
    s = pd.read_csv(f, index_col=0, parse_dates=[0]).iloc[:, 0]
    s.index = pd.to_datetime(s.index, utc=True)
    return s.dropna()


def fred(series: str) -> pd.Series:
    def go():
        r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}", timeout=30)
        df = pd.read_csv(io.StringIO(r.text))
        df.columns = ["date", "value"]
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        return df.dropna().set_index("date")["value"]
    return _cached(series, go)


def yahoo(symbol: str) -> pd.Series:
    def go():
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(symbol)}"
        r = requests.get(url, params=dict(period1=946684800, period2=int(time.time()), interval="1d"),
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        d = r.json()["chart"]["result"][0]
        s = pd.Series(d["indicators"]["quote"][0]["close"],
                      index=pd.to_datetime(d["timestamp"], unit="s", utc=True)).dropna()
        return s
    return _cached(symbol.replace("=", "_"), go)


def em_panel() -> tuple[pd.DataFrame, pd.DataFrame]:
    """USD per unit of foreign currency, and the foreign 3-month rate (% p.a.), daily."""
    price, rate = {}, {}
    for ccy, (rate_id, (src, sym)) in EM.items():
        quote = fred(sym) if src == "fred" else yahoo(sym)      # foreign per USD
        price[ccy] = 1.0 / quote[quote > 0]
        rate[ccy] = fred(rate_id)
    idx = pd.date_range(min(s.index.min() for s in price.values()),
                        max(s.index.max() for s in price.values()), freq="B", tz="UTC")
    px = pd.DataFrame({c: s.reindex(idx).ffill(limit=5) for c, s in price.items()})
    rt = pd.DataFrame({c: s.reindex(idx).ffill(limit=45) for c, s in rate.items()})
    return px, rt


# ── the book ─────────────────────────────────────────────────────────────────
def carry_book(price: pd.DataFrame, rate: pd.DataFrame, usd_rate: pd.Series, *, bps: float,
               haircut_ann: float, k: int = 3, long_only: int | None = None) -> pd.Series:
    """Monthly-rebalanced carry: long the k highest rates, short the k lowest (or long-only top-N)."""
    diff = rate.sub(usd_rate, axis=0) / 100.0
    fwd = price.pct_change().shift(-1)
    month_end = price.index.to_series().groupby([price.index.year, price.index.month]).transform("max") == price.index
    w = pd.DataFrame(0.0, index=price.index, columns=price.columns)
    cur = pd.Series(0.0, index=price.columns)
    for t in price.index:
        if month_end.loc[t]:
            d = diff.loc[t].dropna()
            d = d[price.loc[t].notna().reindex(d.index).fillna(False)]
            cur = pd.Series(0.0, index=price.columns)
            if len(d) >= (long_only or 2 * k):
                if long_only:
                    for c in d.nlargest(long_only).index:
                        cur[c] = 1.0 / long_only
                else:
                    for c in d.nlargest(k).index:
                        cur[c] = 0.5 / k
                    for c in d.nsmallest(k).index:
                        cur[c] = -0.5 / k
        w.loc[t] = cur
    turn = w.diff().abs().fillna(w.abs())
    earned = (diff - np.sign(w).mul(haircut_ann, axis=0).abs().where(w != 0, 0.0)) / 252.0
    pnl = (w * fwd.fillna(0.0)).sum(axis=1) + (w * earned.fillna(0.0)).sum(axis=1) - turn.sum(axis=1) * bps / 1e4
    return pnl.dropna()


def scale_to_vol(pnl: pd.Series, target: float = TARGET_VOL) -> pd.Series:
    v = pnl.std(ddof=1) * np.sqrt(252)
    return pnl * (target / v) if v > 0 else pnl


def stats(pnl: pd.Series) -> dict:
    eq = (1 + pnl).cumprod()
    years = len(pnl) / 252
    sd = pnl.std(ddof=1)
    monthly = pnl.groupby([pnl.index.year, pnl.index.month]).apply(lambda s: (1 + s).prod() - 1)
    worst = monthly.nsmallest(3)
    return dict(ann=float((eq.iloc[-1] ** (1 / years) - 1) * 100) if eq.iloc[-1] > 0 else -100.0,
                vol=float(sd * np.sqrt(252) * 100), sharpe=float(pnl.mean() / sd * np.sqrt(252)) if sd else 0.0,
                maxdd=float(-(eq / eq.cummax() - 1).min() * 100), days=int(len(pnl)),
                worst_months=[f"{y}-{m:02d}: {v * 100:.1f}%" for (y, m), v in worst.items()])


def window(s: pd.Series, lo, hi) -> pd.Series:
    if lo is None:
        return s
    return s[(s.index >= pd.Timestamp(lo, tz=s.index.tz)) & (s.index <= pd.Timestamp(hi, tz=s.index.tz))]


def report(name: str, pnl: pd.Series, res: dict) -> None:
    res[name] = {}
    print(f"\n{name}")
    for wname, (lo, hi) in WINDOWS.items():
        s = window(pnl, lo, hi)
        if len(s) < 200:
            continue
        st = stats(scale_to_vol(s))
        res[name][wname] = st
        print(f"   {wname:<10} {st['ann']:+6.1f}%/год  просадка {st['maxdd']:5.1f}%  Шарп {st['sharpe']:+.2f}"
              f"   худшие месяцы: {', '.join(st['worst_months'])}")


def main():
    res = {}
    g = fxdata.load_panel()
    price = g["price"].dropna(how="any")
    rate = g["short_rate"].loc[price.index]
    usd = g["usd_rate"].loc[price.index].iloc[:, 0]
    print(f"G10: {len(price)} дней {price.index.min().date()} .. {price.index.max().date()}")
    for label, bps, hc in (("G10, фьючерсы CME (0.5 б.п., без наценки)", 0.5, 0.0),
                           ("G10, спот ECN (1.5 б.п., наценка 0.5%)", 1.5, 0.005),
                           ("G10, розничный брокер (1.5 б.п., наценка 1.5%)", 1.5, 0.015)):
        report(label, carry_book(price, rate, usd, bps=bps, haircut_ann=hc), res)

    px, rt = em_panel()
    common = px.dropna(thresh=6).index
    px, rt = px.loc[common], rt.loc[common]
    usd_em = usd.reindex(px.index).ffill()
    ok = px.notna().sum()
    print(f"\nEM: {len(px)} дней {px.index.min().date()} .. {px.index.max().date()}; "
          f"валюты: {', '.join(f'{c}({int(n / 252)}л)' for c, n in ok.items())}")
    for label, bps, hc, lo_only in (("EM, длинно-короткая (5 б.п., без наценки)", 5.0, 0.0, None),
                                    ("EM, длинно-короткая (10 б.п., наценка 3%)", 10.0, 0.03, None),
                                    ("EM, только лонг топ-4 под доллар (5 б.п.)", 5.0, 0.0, 4),
                                    ("EM, только лонг топ-4, розница (10 б.п., наценка 3%)", 10.0, 0.03, 4)):
        report(label, carry_book(px, rt, usd_em, bps=bps, haircut_ann=hc, long_only=lo_only), res)

    OUT.write_text(json.dumps(res, indent=1, default=float))


if __name__ == "__main__":
    main()
