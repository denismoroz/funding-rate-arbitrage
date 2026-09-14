"""Strategy B — false hedge alarms: what they cost, and six NEW ways to make them cheaper or rarer.

PRE-REGISTERED 2026-09-14, before any result of this script was seen.

Already tested and not repeated (June + 2026-09-13): momentum sign 3-30d and OR/AND/2-of-3, close<MA,
drawdown from peak, 90/180d regime filters, high-vol filter, funding/premium gates, BTC-down filter,
Donchian, cross-sectional weakness, continuous hedge ratio, go-to-cash, min-hold, HYST, TSTAT,
efficiency ratio, breadth, partial 50% hedge, BTC-perp hedge, spot 75%, sticky exit (adopted).

Variants (paper "cold wallet" book, same margin model; carry off):
  BASE        current: hedge when NOT(mom14 > 0 AND mom30 > 0), exit after 12h off
  STAGED24    half the hedge at once; the other half only once the signal has been on 24h in a row
  STAGED_DD5  half at once; the other half once price is 5% below where the signal switched on
  STOP5       close the hedge if price rises 5% above where it was opened; re-open only if price returns
              to that level while the signal is still on (a fresh signal also re-opens)
  STOP10      same with 10%
  DAILY       the signal is read once a day at 00:00 UTC instead of every hour
  FLOW        a NEW hedge opens only if the last 72h of taker flow is net selling (taker-buy share
              below its 30-day median); exits as BASE. New information type (order flow).

Windows (fresh start, signals causal on the full history):
  SELECT  BTC+ETH 2019-11 .. 2023-05, 4 coins 2020-12 .. 2023-05   — chooses
  TEST    2023-06 .. 2026-09                                        — BASE rules were fitted here: edge to BASE
  CHOP    2025-06 .. 2026-09                                        — the regime false alarms hurt most
Selection: highest mean Calmar over both baskets on SELECT; it must beat BASE there.
PASS: on TEST the selected variant has a higher return than BASE and max DD not deeper by more than 2 pp,
      on BOTH baskets; and a higher return than BASE on CHOP on both baskets.
"""
import json, sys
from pathlib import Path
import numpy as np, pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import limits as L
from frab.strategy.b2.book import CoinBook, start_book, step

FLOW = Path("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/b_flow")
OUT = HERE / "false_alarms.json"
VARIANTS = ["BASE", "STAGED24", "STAGED_DD5", "STOP5", "STOP10", "DAILY", "FLOW"]
BASKETS = {"BTC+ETH": (["BTC", "ETH"], "2019-11-01"), "4 coins": (L.COINS, "2020-12-01")}
SELECT_END, TEST, CHOP = "2023-06-01", ("2023-06-01", "2026-09-13"), ("2025-06-01", "2026-09-13")
STICKY = 12


# ── signals ──────────────────────────────────────────────────────────────────
def raw_and_trend(close):
    s = pd.Series(close)
    m14, m30 = s / s.shift(336) - 1, s / s.shift(720) - 1
    raw = (~((m14 > 0) & (m30 > 0)) & m30.notna()).values
    return raw, (m14 > 0).values


def sticky_exit(raw, hours=STICKY):
    out = np.zeros(len(raw), bool); on = False; off = 0
    for i, r in enumerate(raw):
        if r:
            on, off = True, 0
        elif on:
            off += 1
            if off >= hours:
                on = False
        out[i] = on
    return out


def staged24(base):
    out = np.zeros(len(base), bool); run = 0
    for i, b in enumerate(base):
        run = run + 1 if b else 0
        out[i] = run >= 24
    return out


def staged_dd(base, close, dd=0.05):
    out = np.zeros(len(base), bool); ref = None
    for i, b in enumerate(base):
        if not b:
            ref = None; continue
        if ref is None:
            ref = close[i]
        out[i] = out[i - 1] if i and out[i - 1] else close[i] <= ref * (1 - dd)
    return out


def stop(base, close, x):
    out = np.zeros(len(base), bool); hedged = False; entry = None; locked = False
    for i, b in enumerate(base):
        if not b:
            hedged, locked, entry = False, False, None
        elif hedged:
            if close[i] >= entry * (1 + x):
                hedged, locked = False, True
        elif not locked:
            hedged, entry = True, close[i]
        elif close[i] <= entry:
            hedged, locked, entry = True, False, close[i]
        out[i] = hedged
    return out


def daily(raw, index):
    midnight = index.hour == 0
    last = np.maximum.accumulate(np.where(midnight, np.arange(len(raw)), -1))
    return sticky_exit(np.where(last >= 0, raw[np.maximum(last, 0)], False))


def flow_gate(raw, coin, index):
    f = pd.read_csv(FLOW / f"{coin}.csv", parse_dates=["time"]).set_index("time")
    f = f[~f.index.duplicated()].reindex(index)
    share = (f["taker_buy_volume"].rolling(72, min_periods=48).sum() / f["volume"].rolling(72, min_periods=48).sum())
    selling = (share < share.rolling(720, min_periods=360).median()).fillna(True).values
    out = np.zeros(len(raw), bool); on = False; off = 0
    for i, r in enumerate(raw):
        if r and (on or selling[i]):
            on, off = True, 0
        elif on:
            off += 1
            if off >= STICKY:
                on = False
        out[i] = on
    return out


def wishes(coin, df):
    close = df["close"].values
    raw, trend = raw_and_trend(close)
    base = sticky_exit(raw)
    return trend, {
        "BASE": [(1.0, base)],
        "STAGED24": [(0.5, base), (0.5, staged24(base))],
        "STAGED_DD5": [(0.5, base), (0.5, staged_dd(base, close))],
        "STOP5": [(1.0, stop(base, close, 0.05))],
        "STOP10": [(1.0, stop(base, close, 0.10))],
        "DAILY": [(1.0, daily(raw, df.index))],
        "FLOW": [(1.0, flow_gate(raw, coin, df.index))],
    }


# ── book with an externally supplied hedge wish ──────────────────────────────
def run_book(df, coin, cap, wish, trend, a, b):
    p = L.params(coin, capital=cap)
    px = df["close"].tolist(); hi = df["high"].tolist(); fr = df["fundingRate"].tolist()
    book = CoinBook.new(coin, p)
    start_book(book, bar_ms=a, price=px[a], params=p)
    eq = np.empty(b - a + 1)
    for k, i in enumerate(range(a, b + 1)):
        book.hedge_prev, book.trend_up_prev = bool(wish[i - 1]), bool(trend[i - 1])
        step(book, bar_ms=i, price=px[i], funding_rate=fr[i], closes=[px[i]], funding_hist=[fr[i]], params=p, high=hi[i])
        eq[k] = book.equity(px[i])
    return eq, book


def episodes(wish, close, a, b):
    w = wish[a:b + 1].astype(int); d = np.diff(np.concatenate([[0], w, [0]]))
    starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1) - 1
    moves = np.array([close[a + e] / close[a + s] - 1 for s, e in zip(starts, ends)])
    return len(moves), float((moves >= 0).mean()) if len(moves) else float("nan")


def basket_run(data, sig, variant, coins, lo, hi):
    eq_total, fees, n_ep, false_ep, cap = None, 0.0, 0, 0.0, 1000.0 * len(coins)
    for c in coins:
        df = data[c]
        a, b = L.idx_of(df, lo), L.idx_of(df, hi) - 1
        trend, legs = sig[c][0], sig[c][1][variant]
        for share, wish in legs:
            eq, book = run_book(df, c, 1000.0 * share, wish, trend, a, b)
            eq_total = eq if eq_total is None else eq_total[:len(eq)] + eq[:len(eq_total)]
            fees += book.perp_fees + book.spot_fees + book.margin_fees
            n, f = episodes(wish, df["close"].values, a, b)
            n_ep += n * share; false_ep += (f if np.isfinite(f) else 0) * n * share
    years = len(eq_total) / 8760
    s = L.summary(eq_total, cap, len(eq_total))
    return dict(ret=s["ret"], ann=s["ann"], dd=s["dd"], calmar=(s["ann"] / s["dd"]) if s["ann"] is not None and s["dd"] > 0 else None,
                alarms_per_coin_year=n_ep / len(coins) / years, false_share=false_ep / n_ep if n_ep else None,
                fees_pct_per_year=fees / cap * 100 / years)


def main():
    data = {c: L.load(c) for c in L.COINS}
    sig = {c: wishes(c, data[c]) for c in L.COINS}

    # parity: BASE through the external-wish loop == the production book's own signals
    df = data["BTC"]; a, b = L.idx_of(df, "2022-01-01"), L.idx_of(df, "2022-07-01") - 1
    eq_ext, _ = run_book(df, "BTC", 1000.0, sig["BTC"][1]["BASE"][0][1], sig["BTC"][0], a, b)
    eq_own = L.simulate(df, "BTC", L.params("BTC"), a, b)["eq"]
    err = float(np.abs(eq_ext - eq_own).max())
    print(f"parity external-wish BASE vs production signals: max |Δequity| = {err:.2e}")
    assert err < 1e-6, "external-wish loop must reproduce the production book"

    res = {"parity_err": err, "windows": {}}
    for wname, (lo_fn, hi) in {"SELECT": (lambda first: first, SELECT_END), "TEST": (lambda first: TEST[0], TEST[1]),
                               "CHOP": (lambda first: CHOP[0], CHOP[1])}.items():
        res["windows"][wname] = {}
        for bname, (coins, first) in BASKETS.items():
            res["windows"][wname][bname] = {}
            for v in VARIANTS:
                r = basket_run(data, sig, v, coins, lo_fn(first), hi)
                res["windows"][wname][bname][v] = r
                print(f"{wname:<6} {bname:<8} {v:<10} ret {r['ret']:+7.1f}% ann {r['ann'] if r['ann'] is None else round(r['ann'], 1)} "
                      f"DD {r['dd']:5.1f} calmar {None if r['calmar'] is None else round(r['calmar'], 2)} | alarms/coin-yr "
                      f"{r['alarms_per_coin_year']:.1f} false {r['false_share'] if r['false_share'] is None else round(r['false_share'] * 100)}% "
                      f"fees {r['fees_pct_per_year']:.1f}%/yr", flush=True)

    sel = res["windows"]["SELECT"]
    mean_calmar = {v: np.mean([sel[bk][v]["calmar"] for bk in BASKETS]) for v in VARIANTS}
    chosen = max(VARIANTS, key=lambda v: mean_calmar[v])
    res["select_mean_calmar"] = mean_calmar
    res["selected"] = chosen
    if chosen == "BASE":
        res["verdict"] = "NO IMPROVEMENT — nothing beats BASE on the selection window"
    else:
        t, ch = res["windows"]["TEST"], res["windows"]["CHOP"]
        ok_test = all(t[bk][chosen]["ret"] > t[bk]["BASE"]["ret"] and t[bk][chosen]["dd"] <= t[bk]["BASE"]["dd"] + 2 for bk in BASKETS)
        ok_chop = all(ch[bk][chosen]["ret"] > ch[bk]["BASE"]["ret"] for bk in BASKETS)
        res["checks"] = dict(test=bool(ok_test), chop=bool(ok_chop))
        res["verdict"] = "PASS" if ok_test and ok_chop else "NO-GO"
    print("\nselect mean Calmar:", {k: round(v, 2) for k, v in mean_calmar.items()}, "\nselected", chosen, "->", res["verdict"], res.get("checks"))
    OUT.write_text(json.dumps(res, indent=1, default=float))


if __name__ == "__main__":
    main()
