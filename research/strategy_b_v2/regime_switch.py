"""Strategy B — can we tell "trend" from "chop" in advance and switch the hedge rule accordingly?

PRE-REGISTERED 2026-09-14, before any result of this script was seen.

Part 1 (the real question): does a trend/chop detector say anything about the NEXT quarter?
  Detectors, daily, causal, lagged one day:
    ER60    Kaufman efficiency ratio over 60 days: |P_t - P_t-60| / sum |daily moves|  (high = trend)
    VR520   Lo-MacKinlay variance ratio, 5-day vs 1-day returns over 120 days          (>1 = trend)
    AC60    lag-1 autocorrelation of daily returns over 60 days                        (<0 = chop)
  Measured against, over the next 90 days: (a) the current rule's own PnL on a continuous run,
  (b) the realised efficiency ratio of the next 90 days (was it in fact a trend or a chop).
  Reported per coin and pooled: Spearman correlation, and the mean forward 90d PnL when the detector
  says chop vs when it says trend. If a detector carries no forward information, switching on it cannot help.

Part 2: switch the rule by regime. Chop = detector below its own median of the past 2 years (expanding,
at least 1 year of history); trend = otherwise. In trend every variant uses the current rule; in chop:
    OFF    no hedge at all
    SLOW   the 30/60-day windows instead of 14/30 (the slow pair looked better in the 2025-26 chop)
    CHAND  the Chandelier exit (the best of the published variants in that chop, section 8)
  3 detectors x 3 chop rules = 9 variants, plus BASE.

Book, windows and the decision rule are those of false_alarms.py / hedge_signals.py:
  SELECT  BTC+ETH 2019-11 .. 2023-05, 4 coins 2020-12 .. 2023-05 — picks the variant (highest mean Calmar
          over both baskets; it must beat BASE there)
  TEST    2023-06 .. 2026-09       CHOP  2025-06 .. 2026-09
PASS: on TEST the selected variant returns more than BASE with max DD no deeper than +2 pp, on BOTH baskets,
      and returns more than BASE on CHOP on both baskets. Otherwise NO-GO.
"""
import json, sys
from pathlib import Path
import numpy as np, pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import limits as L
from false_alarms import sticky_exit, raw_and_trend, BASKETS, SELECT_END, TEST, CHOP
from hedge_signals import basket_run, pair_wish, daily_frame, to_hourly, chandelier

OUT = HERE / "regime_switch.json"
DETECTORS = ["ER60", "VR520", "AC60"]
CHOP_RULES = ["OFF", "SLOW", "CHAND"]
VARIANTS = ["BASE"] + [f"{d}|{r}" for d in DETECTORS for r in CHOP_RULES]


# ── detectors (daily, causal) ────────────────────────────────────────────────
def detectors(daily_close):
    p = daily_close
    r = p.pct_change()
    er = (p - p.shift(60)).abs() / p.diff().abs().rolling(60).sum()
    var1 = r.rolling(120).var()
    var5 = (p / p.shift(5) - 1).rolling(120).var()
    vr = var5 / (5 * var1)
    ac = r.rolling(60).corr(r.shift(1))
    return {"ER60": er, "VR520": vr, "AC60": ac}


def is_chop(series):
    """Chop = the detector is below its own median of the past 2 years (at least 1 year of history)."""
    med = series.rolling(730, min_periods=365).median()
    return (series < med).where(med.notna(), False)


# ── part 1: does the detector know anything about the next quarter? ──────────
def diagnostic(data):
    out, pooled = {}, {d: [[], []] for d in DETECTORS}
    for c in L.COINS:
        df = data[c]
        a = L.idx_of(df, str(df.index[0] + pd.Timedelta(days=95))[:10])
        eq = L.simulate(df, c, L.params(c), a, len(df) - 1)["eq"]          # continuous BASE run
        eqs = pd.Series(eq, index=df.index[a:])
        d = daily_frame(df)["close"]
        det = detectors(d)
        fwd_pnl = (eqs.resample("1D").last().shift(-90) - eqs.resample("1D").last()) / 1000 * 100
        fwd_er = ((d.shift(-90) - d).abs() / d.diff().abs().rolling(90).sum().shift(-90))
        for name, s in det.items():
            s = s.shift(1)                                                  # yesterday's value
            ok = s.notna() & fwd_pnl.notna() & fwd_er.notna()
            weekly = ok & (np.arange(len(ok)) % 7 == 0)
            x, y, y2 = s[weekly], fwd_pnl[weekly], fwd_er[weekly]
            chop = is_chop(det[name].shift(1))[weekly].astype(bool)
            out[f"{c}|{name}"] = dict(
                n=int(weekly.sum()), ic_pnl=float(x.corr(y, method="spearman")), ic_er=float(x.corr(y2, method="spearman")),
                fwd_pnl_when_chop=float(y[chop].mean()), fwd_pnl_when_trend=float(y[~chop].mean()),
                fwd_er_when_chop=float(y2[chop].mean()), fwd_er_when_trend=float(y2[~chop].mean()),
                chop_share=float(chop.mean()))
            pooled[name][0] += list(x.values); pooled[name][1] += list(y.values)
            r_ = out[f"{c}|{name}"]
            print(f"{c:<4} {name:<6} n {r_['n']:>4} | связь с доходом след. 90д {r_['ic_pnl']:+.3f} с трендовостью {r_['ic_er']:+.3f} "
                  f"| доход след. 90д: пила {r_['fwd_pnl_when_chop']:+5.1f}% тренд {r_['fwd_pnl_when_trend']:+5.1f}% "
                  f"| трендовость след. 90д: {r_['fwd_er_when_chop']:.2f} / {r_['fwd_er_when_trend']:.2f} | пила {r_['chop_share'] * 100:.0f}% времени", flush=True)
    for name, (xs, ys) in pooled.items():
        s = pd.Series(xs).corr(pd.Series(ys), method="spearman")
        out[f"pooled|{name}"] = float(s)
        print(f"pooled {name}: связь детектора с доходом следующих 90 дней {s:+.3f}", flush=True)
    return out


# ── part 2: switch the rule by regime ────────────────────────────────────────
def wishes(coin, df):
    close = df["close"].values
    raw, trend = raw_and_trend(close)
    base = sticky_exit(raw)
    slow = pair_wish(close, 30, 60)
    chand = chandelier(df)
    det = detectors(daily_frame(df)["close"])
    legs = {"BASE": [(1.0, base)]}
    for dname, s in det.items():
        chop_h = to_hourly(is_chop(s), df.index)
        for rname, alt in (("OFF", np.zeros(len(base), bool)), ("SLOW", slow), ("CHAND", chand)):
            legs[f"{dname}|{rname}"] = [(1.0, np.where(chop_h, alt, base))]
    return trend, legs


def main():
    data = {c: L.load(c) for c in L.COINS}
    print("== part 1: детектор режима против следующих 90 дней", flush=True)
    res = {"diagnostic": diagnostic(data)}

    sig = {c: wishes(c, data[c]) for c in L.COINS}
    print("\n== part 2: переключение правила по режиму", flush=True)
    res["windows"] = {}
    for wname, (lo_fn, hi) in {"SELECT": (lambda first: first, SELECT_END), "TEST": (lambda first: TEST[0], TEST[1]),
                               "CHOP": (lambda first: CHOP[0], CHOP[1])}.items():
        res["windows"][wname] = {}
        for bname, (coins, first) in BASKETS.items():
            res["windows"][wname][bname] = {}
            for v in VARIANTS:
                r = basket_run(data, sig, v, coins, lo_fn(first), hi)
                res["windows"][wname][bname][v] = r
                print(f"{wname:<6} {bname:<8} {v:<12} ret {r['ret']:+7.1f}% DD {r['dd']:5.1f} calmar "
                      f"{None if r['calmar'] is None else round(r['calmar'], 2)} | alarms/coin-yr {r['alarms_per_coin_year']:.1f} "
                      f"false {r['false_share'] if r['false_share'] is None else round(r['false_share'] * 100)}% "
                      f"fees {r['fees_pct_per_year']:.1f}%/yr", flush=True)

    sel = res["windows"]["SELECT"]
    mean_calmar = {v: float(np.mean([sel[bk][v]["calmar"] for bk in BASKETS])) for v in VARIANTS}
    chosen = max(VARIANTS, key=lambda v: mean_calmar[v])
    res["select_mean_calmar"], res["selected"] = mean_calmar, chosen
    if chosen == "BASE":
        res["verdict"] = "NO IMPROVEMENT — nothing beats BASE on the selection window"
    else:
        t, ch = res["windows"]["TEST"], res["windows"]["CHOP"]
        ok_test = all(t[bk][chosen]["ret"] > t[bk]["BASE"]["ret"] and t[bk][chosen]["dd"] <= t[bk]["BASE"]["dd"] + 2 for bk in BASKETS)
        ok_chop = all(ch[bk][chosen]["ret"] > ch[bk]["BASE"]["ret"] for bk in BASKETS)
        res["checks"] = dict(test=bool(ok_test), chop=bool(ok_chop))
        res["verdict"] = "PASS" if ok_test and ok_chop else "NO-GO"
    print("\nselect mean Calmar:", {k: round(v, 2) for k, v in mean_calmar.items()},
          "\nselected", chosen, "->", res["verdict"], res.get("checks"))
    OUT.write_text(json.dumps(res, indent=1, default=float))


if __name__ == "__main__":
    main()
