"""Strategy B — six published hedge-signal families we have never tried, against the current rule.

PRE-REGISTERED 2026-09-14, before any result of this script was seen.

Already tested and not repeated (June, 09-13, 09-14): momentum sign 3-30d and OR/AND/2-of-3, close<MA,
drawdown from peak, 90/180d regime filters, high-vol filter, funding/premium gates, BTC-down filter, Donchian,
cross-sectional weakness, continuous hedge ratio, go-to-cash, min-hold, HYST, TSTAT, efficiency ratio, breadth,
partial 50% hedge, BTC-perp hedge, spot 75%, sticky exit (adopted), staged entry, hedge stop, daily signal,
taker-flow gate (FLOW), and re-fitting the windows on a rolling window.

Candidates (each family gets ONE canonical construction, all with the current 12h sticky exit):
  BASE      current: hedge unless mom14 > 0 and mom30 > 0
  ENS6      six lookback pairs at once (7/14, 10/21, 14/30, 21/45, 30/60, 45/90), each hedging 1/6 of the
            book — multi-horizon trend ensemble (Hurst/Ooi/Pedersen, "A Century of Evidence"; Baltas-Kosowski)
  ENS3      the same idea, three pairs (7/20, 14/30, 30/60), 1/3 each
  JUMP      statistical jump model (Shu/Mulvey, arXiv 2402.05272): 2 regimes on daily [5d return, 20d vol,
            20d downside vol], fitted by dynamic programming with a switch penalty, refitted monthly on
            history to date, state assigned online; hedge in the bear regime
  MACRO     BASE or equity risk-off: VIX (previous day's close) above its 80th percentile of the past 252 days
  DVOL      BASE or an implied-volatility spike: Deribit DVOL (30d IV of BTC/ETH options; BTC's index for SOL
            and AVAX) above its 80th percentile of the past 90 days. Before 2021-03 there is no index = no spike
  CHAND     Chandelier exit (volatility-scaled Donchian): hedge when the close falls below 60d high - 3 x ATR14,
            un-hedge when it rises above 60d low + 3 x ATR14
  VOLSCALE  vol-managed exposure (Moreira-Muir): when BASE says no hedge, still hedge the fraction
            clamp(1 - median vol / current vol, 0, 1) of the book, in quarters (4 legs)

Book, windows and the decision rule are exactly those of false_alarms.py:
  SELECT  BTC+ETH 2019-11 .. 2023-05, 4 coins 2020-12 .. 2023-05  — picks the candidate (highest mean Calmar
          over both baskets; it must beat BASE there)
  TEST    2023-06 .. 2026-09   — BASE's own rules were fitted here, so BASE has the edge
  CHOP    2025-06 .. 2026-09
PASS: on TEST the selected candidate returns more than BASE with max DD no deeper than +2 pp, on BOTH baskets,
      and returns more than BASE on CHOP on both baskets. Otherwise NO-GO.
"""
import json, sys
from pathlib import Path
import numpy as np, pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import limits as L
from false_alarms import run_book, episodes, sticky_exit, raw_and_trend, BASKETS, SELECT_END, TEST, CHOP, STICKY

EXTRA = Path("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/b_extra")
OUT = HERE / "hedge_signals.json"
VARIANTS = ["BASE", "ENS6", "ENS3", "JUMP", "MACRO", "DVOL", "CHAND", "VOLSCALE"]
JUMP_LAMBDA = 5.0


# ── helpers ──────────────────────────────────────────────────────────────────
def pair_wish(close, short_d, long_d):
    s = pd.Series(close)
    ms, ml = s / s.shift(short_d * 24) - 1, s / s.shift(long_d * 24) - 1
    return sticky_exit((~((ms > 0) & (ml > 0)) & ml.notna()).values)


def daily_frame(df):
    """Daily bars from the hourly panel: high = max hourly high, low = min hourly close."""
    d = pd.DataFrame({"close": df["close"].resample("1D").last(), "high": df["high"].resample("1D").max(),
                      "low": df["close"].resample("1D").min()}).dropna()
    return d


def to_hourly(daily_flag, index, lag_days=1):
    return daily_flag.shift(lag_days).reindex(index, method="ffill").fillna(False).astype(bool).values


# ── JUMP: statistical jump model, refitted monthly on history to date ────────
def jump_fit(X, lam, iters=12, seed=0):
    """Two-state jump model: DP assignment with a switch penalty + centroid update."""
    rng = np.random.default_rng(seed)
    order = np.argsort(X[:, 0])
    mu = np.stack([X[order[:max(1, len(X) // 4)]].mean(0), X[order[-max(1, len(X) // 4):]].mean(0)])
    for _ in range(iters):
        s = jump_assign(X, mu, lam)
        for k in (0, 1):
            if (s == k).any():
                mu[k] = X[s == k].mean(0)
            else:
                mu[k] = X[rng.integers(len(X))]
    return mu


def jump_assign(X, mu, lam):
    d = ((X[:, None, :] - mu[None, :, :]) ** 2).sum(2)
    cost = d[0].copy(); back = np.zeros((len(X), 2), int)
    for t in range(1, len(X)):
        for k in (0, 1):
            stay, switch = cost[k], cost[1 - k] + lam
            back[t, k] = k if stay <= switch else 1 - k
            d[t, k] += min(stay, switch)
        cost = d[t]
    s = np.zeros(len(X), int); s[-1] = int(np.argmin(cost))
    for t in range(len(X) - 1, 0, -1):
        s[t - 1] = back[t, s[t]]
    return s


def jump_states(daily_close):
    r = daily_close.pct_change()
    feat = pd.DataFrame({"r5": daily_close / daily_close.shift(5) - 1,
                         "vol": r.rolling(20).std(),
                         "dvol": r.where(r < 0).rolling(20).std()}).fillna({"dvol": 0.0})
    feat = feat.dropna()
    bear = pd.Series(False, index=daily_close.index)
    months = pd.date_range(feat.index[0], feat.index[-1], freq="MS", tz="UTC")
    mu = None; prev = 0; bear_state = 0
    for i, m0 in enumerate(months):
        hist = feat[feat.index < m0]
        if len(hist) >= 250:
            z = (hist - hist.mean()) / hist.std()
            mu = jump_fit(z.values, JUMP_LAMBDA)
            bear_state = int(np.argmin(mu[:, 0]))
            mean, std = hist.mean(), hist.std()
        if mu is None:
            continue
        m1 = months[i + 1] if i + 1 < len(months) else feat.index[-1] + pd.Timedelta(days=1)
        chunk = feat[(feat.index >= m0) & (feat.index < m1)]
        for ts, row in chunk.iterrows():                       # online assignment, one day at a time
            x = ((row - mean) / std).values
            d = ((x[None, :] - mu) ** 2).sum(1)
            prev = int(np.argmin(d + JUMP_LAMBDA * (np.arange(2) != prev)))
            bear.loc[ts] = prev == bear_state
    return bear


# ── external series ──────────────────────────────────────────────────────────
def vix_riskoff(index):
    v = pd.read_csv(EXTRA / "VIXCLS.csv", parse_dates=["time"]).set_index("time")["value"]
    hot = v > v.rolling(252, min_periods=120).quantile(0.8)
    return to_hourly(hot, index, lag_days=1)


def dvol_spike(coin, index):
    src = coin if coin in ("BTC", "ETH") else "BTC"
    d = pd.read_csv(EXTRA / f"dvol_{src}.csv", parse_dates=["time"]).set_index("time")["dvol"]
    d = d[~d.index.duplicated()].reindex(index).ffill()
    spike = (d > d.rolling(2160, min_periods=720).quantile(0.8)).shift(1).fillna(False)
    return spike.values


def chandelier(df):
    d = daily_frame(df)
    atr = (d["high"] - d["low"]).rolling(14).mean()
    up, dn = d["high"].rolling(60).max() - 3 * atr, d["low"].rolling(60).min() + 3 * atr
    on = pd.Series(False, index=d.index); state = False
    for ts in d.index:
        if np.isfinite(atr.get(ts, np.nan)):
            if not state and d["close"][ts] < up[ts]:
                state = True
            elif state and d["close"][ts] > dn[ts]:
                state = False
        on[ts] = state
    return sticky_exit(to_hourly(on, df.index))


def vol_fraction(close):
    r = pd.Series(close).pct_change()
    vol = r.rolling(480).std() * np.sqrt(8760)
    target = vol.rolling(17520, min_periods=2160).median()
    f = (1 - target / vol).clip(0, 1).fillna(0.0)
    return f.values


# ── variants: list of (share of the book, hourly hedge wish) legs ────────────
def wishes(coin, df):
    close = df["close"].values
    raw, trend = raw_and_trend(close)
    base = sticky_exit(raw)
    ens6 = [(1 / 6, pair_wish(close, s, l)) for s, l in ((7, 14), (10, 21), (14, 30), (21, 45), (30, 60), (45, 90))]
    ens3 = [(1 / 3, pair_wish(close, s, l)) for s, l in ((7, 20), (14, 30), (30, 60))]
    jump = sticky_exit(to_hourly(jump_states(daily_frame(df)["close"]), df.index))
    macro = sticky_exit(raw | vix_riskoff(df.index))
    dvol = sticky_exit(raw | dvol_spike(coin, df.index))
    f = vol_fraction(close)
    volscale = [(0.25, base | (f >= k * 0.25)) for k in (1, 2, 3, 4)]
    return trend, {"BASE": [(1.0, base)], "ENS6": ens6, "ENS3": ens3, "JUMP": [(1.0, jump)],
                   "MACRO": [(1.0, macro)], "DVOL": [(1.0, dvol)], "CHAND": [(1.0, chandelier(df))],
                   "VOLSCALE": volscale}


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
            n, fr = episodes(wish, df["close"].values, a, b)
            n_ep += n * share; false_ep += (fr if np.isfinite(fr) else 0) * n * share
    years = len(eq_total) / 8760
    s = L.summary(eq_total, cap, len(eq_total))
    return dict(ret=s["ret"], ann=s["ann"], dd=s["dd"], calmar=(s["ann"] / s["dd"]) if s["ann"] is not None and s["dd"] > 0 else None,
                alarms_per_coin_year=n_ep / len(coins) / years, false_share=false_ep / n_ep if n_ep else None,
                fees_pct_per_year=fees / cap * 100 / years)


def main():
    data = {c: L.load(c) for c in L.COINS}
    sig = {c: wishes(c, data[c]) for c in L.COINS}
    print("signals built", flush=True)

    res = {"windows": {}}
    for wname, (lo_fn, hi) in {"SELECT": (lambda first: first, SELECT_END), "TEST": (lambda first: TEST[0], TEST[1]),
                               "CHOP": (lambda first: CHOP[0], CHOP[1])}.items():
        res["windows"][wname] = {}
        for bname, (coins, first) in BASKETS.items():
            res["windows"][wname][bname] = {}
            for v in VARIANTS:
                r = basket_run(data, sig, v, coins, lo_fn(first), hi)
                res["windows"][wname][bname][v] = r
                print(f"{wname:<6} {bname:<8} {v:<9} ret {r['ret']:+7.1f}% DD {r['dd']:5.1f} calmar "
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
