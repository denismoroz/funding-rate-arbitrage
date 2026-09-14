"""Strategy B — re-fit the parameters every day / week on a rolling window of the last 3–12 months.

PRE-REGISTERED 2026-09-14, before any result of this script was seen.

Question (from the user): markets change, so a fixed parameter set should lose to parameters re-fitted often
on recent data, with a lookback between ~3 months and a year.

Parameter grid (54): signal windows short/long days 14/30, 7/14, 10/21, 21/45, 30/60, 45/90
  x sticky exit hours 12, 0, 48 x ratchet threshold 0.5, 0.25, 1.0. The listed order is the tie-break order;
  the production set 14/30 · 12h · 0.5 is first.
Book: the paper "cold wallet" book (limits.params), carry off, $1000 per coin.

Shadow books: every grid set runs continuously on every coin from 95 days after the coin's first bar.
Re-fit at 00:00 UTC (daily) or Monday 00:00 UTC (weekly), using shadow equity up to the previous hour only:
  lookback  90, 120, 180, 270, 365 days, or all history ("all" = anchored walk-forward)
  score     ret    = shadow PnL over the lookback
            calmar = PnL / max(max drawdown of the daily-sampled shadow equity in the lookback, $10)
  scope     coin   = each coin picks its own set
            basket = one set for all coins of the basket (scored on the summed shadow equity)
  -> 6 x 2 x 2 x 2 = 48 adaptive variants.
The chosen set applies from that hour on: the real book takes its hedge wish, trend flag and ratchet. Switching
sets mid-episode opens/closes the hedge as the new set says, so the cost of re-fitting is included.

Baselines: PROD (current set; fitted on 2023–25, so it has an edge on TEST), STATIC_SEL (the single fixed set
with the best mean Calmar on SELECT — the fair static without look-ahead), ORACLE (weekly, per coin, the set
with the best NEXT week — a look-ahead ceiling, not a candidate).

Windows (fresh start; every lookback is fully available at the window start):
  SELECT  BTC+ETH 2020-05-01 .. 2023-06-01, 4 coins 2022-01-01 .. 2023-06-01
  TEST    2023-06-01 .. 2026-09-13
  CHOP    2025-06-01 .. 2026-09-13
Selection: the adaptive variant with the highest mean Calmar over both baskets on SELECT.
PASS (all required):
  1. TEST: selected return > STATIC_SEL return and max DD <= STATIC_SEL DD + 2 pp, on both baskets;
  2. CHOP: selected return > STATIC_SEL return, on both baskets;
  3. breadth: on TEST at least half of the 48 adaptive variants beat STATIC_SEL's return, on each basket.
Descriptive: does the past-lookback ranking of the 54 sets predict the next 1 / 4 weeks (Spearman IC, where
the past-best set lands next month), switches per year, the whole static grid on TEST/CHOP, yearly table.

AMENDMENT 2026-09-14, after the first run: the user meant re-fitting the two signal windows themselves
(14/30 -> 7/20 -> 5/15 ...), not a mixed grid. `--windows` re-runs everything under the same rules on a
finer grid of 36 window pairs (short 3..30 d x long 10..90 d, long >= 1.5 x short), with the sticky exit
and the ratchet fixed at the production values -> rolling_reopt_windows.json.
"""
import json, sys, time
from itertools import product
from pathlib import Path
import numpy as np, pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import limits as L
from frab.strategy.b2.book import CoinBook, start_book, step

OUT = HERE / "rolling_reopt.json"
WINDOWS = [(14, 30), (7, 14), (10, 21), (21, 45), (30, 60), (45, 90)]
STICKIES = [12, 0, 48]
RATCHETS = [0.5, 0.25, 1.0]
GRID = list(product(WINDOWS, STICKIES, RATCHETS))          # GRID[0] is production
LOOKBACKS = [90, 120, 180, 270, 365, "all"]
FREQS, SCORES, SCOPES = ["D", "W"], ["ret", "calmar"], ["coin", "basket"]
MENU = list(product(LOOKBACKS, FREQS, SCORES, SCOPES))
BASKETS = {"BTC+ETH": ["BTC", "ETH"], "4 coins": L.COINS}
EVAL = {"SELECT": {"BTC+ETH": ("2020-05-01", "2023-06-01"), "4 coins": ("2022-01-01", "2023-06-01")},
        "TEST": {bk: ("2023-06-01", "2026-09-13") for bk in BASKETS},
        "CHOP": {bk: ("2025-06-01", "2026-09-13") for bk in BASKETS}}
SHADOW_DELAY = 95 * 24
DD_FLOOR = 10.0

if "--windows" in sys.argv:                                # amendment: re-fit the signal windows only
    GRID = [((14, 30), 12, 0.5)] + [((s, l), 12, 0.5) for s in (3, 5, 7, 10, 14, 21, 30)
                                    for l in (10, 15, 20, 30, 45, 60, 90) if l >= 1.5 * s and (s, l) != (14, 30)]
    OUT = HERE / "rolling_reopt_windows.json"


def vname(v):
    return f"L{v[0]}|{v[1]}|{v[2]}|{v[3]}"


def gname(g):
    (s, l), st, r = GRID[g]
    return f"{s}/{l}·{st}h·{r}"


# ── data and signals ─────────────────────────────────────────────────────────
def load_all():
    idx = L.load("BTC").index
    data = {}
    for c in L.COINS:
        df = L.load(c).reindex(idx)
        df["fundingRate"] = df["fundingRate"].fillna(0.0)
        data[c] = dict(df=df, px=df["close"].tolist(), hi=df["high"].tolist(), fr=df["fundingRate"].tolist(),
                       first=int(np.flatnonzero(df["close"].notna().values)[0]))
    return idx, data


def sticky_exit(raw, hours):
    if not hours:
        return raw.copy()
    out = np.zeros(len(raw), bool); on = False; off = 0
    for i, r in enumerate(raw.tolist()):
        if r:
            on, off = True, 0
        elif on:
            off += 1
            if off >= hours:
                on = False
        out[i] = on
    return out


def grid_signals(close):
    s = pd.Series(close)
    W = np.zeros((len(GRID), len(s)), bool); T = np.zeros((len(GRID), len(s)), bool); cache = {}
    for g, ((ws, wl), st, _) in enumerate(GRID):
        if (ws, wl, st) not in cache:
            ms, ml = s / s.shift(ws * 24) - 1, s / s.shift(wl * 24) - 1
            raw = (~((ms > 0) & (ml > 0)) & ml.notna()).values
            cache[(ws, wl, st)] = (sticky_exit(raw, st), (ms > 0).values)
        W[g], T[g] = cache[(ws, wl, st)]
    return W, T


# ── the real book, driven by an hourly choice of grid set ────────────────────
PIDX = np.array([RATCHETS.index(g[2]) for g in GRID])


def run(d, coin, sel, W, T, a, b, cap=1000.0):
    """Book from bar a to b; bar i uses set sel[i]: its hedge wish and trend of bar i-1 and its ratchet."""
    P = [L.params(coin, capital=cap, ratchet_threshold=r) for r in RATCHETS]
    px, hi, fr = d["px"], d["hi"], d["fr"]
    ii = np.arange(a, b + 1); s = sel[a:b + 1]
    wish, trend, pr = W[s, ii - 1].tolist(), T[s, ii - 1].tolist(), PIDX[s].tolist()
    book = CoinBook.new(coin, P[0])
    start_book(book, bar_ms=a, price=px[a], params=P[0])
    eq = np.empty(b - a + 1)
    for k, i in enumerate(range(a, b + 1)):
        book.hedge_prev, book.trend_up_prev = wish[k], trend[k]
        step(book, bar_ms=i, price=px[i], funding_rate=fr[i], closes=[px[i]], funding_hist=[fr[i]], params=P[pr[k]], high=hi[i])
        eq[k] = book.equity(px[i])
    switches = int(np.count_nonzero(np.diff(s)))
    return eq, book, switches


def shadows(data, sig, H):
    E = {}
    for c in L.COINS:
        a = data[c]["first"] + SHADOW_DELAY
        E[c] = np.full((len(GRID), H), np.nan)
        for g in range(len(GRID)):
            E[c][g, a:], _, _ = run(data[c], c, np.full(H, g, np.int16), *sig[c], a, H - 1)
        print(f"shadows {c} done", flush=True)
    return E


# ── re-fitting ───────────────────────────────────────────────────────────────
def to_hourly(dec_hours, choices, H):
    pos = np.searchsorted(dec_hours, np.arange(H), side="right") - 1
    return np.where(pos >= 0, choices[np.maximum(pos, 0)], 0).astype(np.int16)


def daily_choices(Es, days):
    """For each 00:00 UTC in `days`: chosen set per (lookback, score), from shadow equity up to the hour before."""
    D = Es[:, days - 1]
    valid = np.isfinite(D[0])
    first = int(np.flatnonzero(valid)[0])
    out = {}
    for lb in LOOKBACKS:
        ch = {"ret": np.zeros(len(days), np.int16), "calmar": np.zeros(len(days), np.int16)}
        for j in range(len(days)):
            start = first if lb == "all" else j - lb
            if start < first or j - start < 30:
                continue
            seg = D[:, start:j + 1]
            ret = seg[:, -1] - seg[:, 0]
            dd = (np.maximum.accumulate(seg, axis=1) - seg).max(axis=1)
            ch["ret"][j] = int(np.argmax(ret))
            ch["calmar"][j] = int(np.argmax(ret / np.maximum(dd, DD_FLOOR)))
        out[lb] = ch
    return out


def selections(idx, E):
    H = len(idx)
    days = np.flatnonzero(idx.hour == 0); days = days[days >= 1]
    mondays_mask = idx[days].dayofweek == 0
    scopes = {c: E[c] for c in L.COINS} | {bk: sum(E[c] for c in cs) for bk, cs in BASKETS.items()}
    SEL = {}
    for key, Es in scopes.items():
        t = time.time()
        ch = daily_choices(Es, days)
        for lb, f, sc in product(LOOKBACKS, FREQS, SCORES):
            dh, cc = (days, ch[lb][sc]) if f == "D" else (days[mondays_mask], ch[lb][sc][mondays_mask])
            SEL[(lb, f, sc, key)] = to_hourly(dh, cc, H)
        print(f"selections {key} {time.time() - t:.0f}s", flush=True)
    mondays = days[mondays_mask]
    for c in L.COINS:                                              # ORACLE: best NEXT week, look-ahead
        nxt = np.minimum(mondays + 167, H - 1)
        fut = E[c][:, nxt] - E[c][:, mondays - 1]
        ok = np.isfinite(fut[0])
        SEL[("ORACLE", c)] = to_hourly(mondays, np.where(ok, np.argmax(np.nan_to_num(fut, nan=-1e18), axis=0), 0), H)
    return SEL


# ── evaluation ───────────────────────────────────────────────────────────────
_cache = {}


def basket_run(data, sig, sel_for, key_for, window, bk):
    lo, hi = EVAL[window][bk]
    coins = BASKETS[bk]; eq_total = None; fees = 0.0; sw = 0
    for c in coins:
        df = data[c]["df"]; a, b = L.idx_of(df, lo), L.idx_of(df, hi) - 1
        ck = (key_for(c), c, a, b)
        if ck not in _cache:
            eq, book, s = run(data[c], c, sel_for(c), *sig[c], a, b)
            _cache[ck] = (eq, book.perp_fees + book.spot_fees + book.margin_fees, s)
        eq, f, s = _cache[ck]
        eq_total = eq if eq_total is None else eq_total + eq
        fees += f; sw += s
    cap = 1000.0 * len(coins); years = len(eq_total) / 8760
    s_ = L.summary(eq_total, cap, len(eq_total))
    return dict(ret=s_["ret"], ann=s_["ann"], dd=s_["dd"], calmar=s_["ann"] / s_["dd"] if s_["ann"] is not None and s_["dd"] > 0 else None,
                switches_per_coin_year=sw / len(coins) / years, fees_pct_per_year=fees / cap * 100 / years)


def static(g):
    return (lambda c: np.full(len(IDX), g, np.int16)), (lambda c: f"G{g}")


def adaptive(v, SEL, bk):
    lb, f, sc, scope = v
    if scope == "coin":
        return (lambda c: SEL[(lb, f, sc, c)]), (lambda c: vname(v))
    return (lambda c: SEL[(lb, f, sc, bk)]), (lambda c: vname(v) + "@" + bk)


def ic_diagnostic(idx, E):
    """Weekly: does the past-lookback PnL ranking of the 54 sets predict the next 1 / 4 weeks?"""
    days = np.flatnonzero(idx.hour == 0); days = days[days >= 1]
    mon = np.flatnonzero(idx[days].dayofweek == 0)
    out = {}
    for c in L.COINS:
        D = E[c][:, days - 1]; first = int(np.flatnonzero(np.isfinite(D[0]))[0])
        for lb in (90, 180, 365):
            ic7, ic28, pct, gain = [], [], [], []
            for j in mon:
                if j - lb < first or j + 28 >= D.shape[1]:
                    continue
                past = pd.Series(D[:, j] - D[:, j - lb]).rank()
                f7, f28 = pd.Series(D[:, j + 7] - D[:, j]), pd.Series(D[:, j + 28] - D[:, j])
                ic7.append(past.corr(f7.rank())); ic28.append(past.corr(f28.rank()))
                best = int(np.argmax(past.values))
                pct.append(float((f28.values < f28.values[best]).mean() + 0.5 * ((f28.values == f28.values[best]).sum() - 1) / len(f28)))
                gain.append(float(f28.values[best] - np.median(f28.values)))
            ic7, ic28 = np.array(ic7, float), np.array(ic28, float)
            ic28_nonoverlap = ic28[::4]
            out[f"{c}|L{lb}"] = dict(
                weeks=len(ic7), ic7_mean=float(np.nanmean(ic7)), ic7_t=float(np.nanmean(ic7) / (np.nanstd(ic7) / np.sqrt(np.isfinite(ic7).sum()))),
                ic28_mean=float(np.nanmean(ic28)), ic28_t=float(np.nanmean(ic28_nonoverlap) / (np.nanstd(ic28_nonoverlap) / np.sqrt(np.isfinite(ic28_nonoverlap).sum()))),
                past_best_next_month_percentile=float(np.mean(pct)),
                past_best_minus_median_next_month_pct_per_year=float(np.mean(gain)) / 1000 * 100 * 13)
            r = out[f"{c}|L{lb}"]
            print(f"IC {c:<4} L{lb:<3} weeks {r['weeks']} | IC 1w {r['ic7_mean']:+.3f} (t {r['ic7_t']:+.1f}) IC 4w {r['ic28_mean']:+.3f} "
                  f"(t {r['ic28_t']:+.1f}) | past-best next month at percentile {r['past_best_next_month_percentile']:.2f} "
                  f"| vs median set {r['past_best_minus_median_next_month_pct_per_year']:+.1f}%/yr", flush=True)
    return out


def main():
    global IDX
    t0 = time.time()
    IDX, data = load_all()
    sig = {c: grid_signals(np.array(data[c]["px"], float)) for c in L.COINS}

    # parity: PROD through this loop == the production book's own signals
    df = data["BTC"]["df"]; a, b = L.idx_of(df, "2022-01-01"), L.idx_of(df, "2022-07-01") - 1
    eq_ext, _, _ = run(data["BTC"], "BTC", np.zeros(len(IDX), np.int16), *sig["BTC"], a, b)
    err = float(np.abs(eq_ext - L.simulate(L.load("BTC"), "BTC", L.params("BTC"), a, b)["eq"]).max())
    print(f"parity PROD external loop vs production signals: max |Δequity| = {err:.2e}", flush=True)
    assert err < 1e-6

    E = shadows(data, sig, len(IDX)); print(f"shadows {time.time() - t0:.0f}s", flush=True)
    SEL = selections(IDX, E)
    res = dict(parity_err=err, grid=[gname(g) for g in range(len(GRID))], windows={})

    print("== IC diagnostic"); res["ic"] = ic_diagnostic(IDX, E)

    print("== static grid", flush=True)
    res["static_grid"] = {w: {bk: {gname(g): basket_run(data, sig, *static(g), w, bk) for g in range(len(GRID))} for bk in BASKETS}
                          for w in EVAL}
    sel_calmar = {g: np.mean([res["static_grid"]["SELECT"][bk][gname(g)]["calmar"] for bk in BASKETS]) for g in range(len(GRID))}
    g_sel = max(range(len(GRID)), key=lambda g: sel_calmar[g])
    res["static_sel"] = gname(g_sel)
    print(f"STATIC_SEL = {gname(g_sel)} (SELECT mean Calmar {sel_calmar[g_sel]:.2f}; PROD {sel_calmar[0]:.2f})", flush=True)

    print("== adaptive menu", flush=True)
    rows = {}
    for w in EVAL:
        rows[w] = {}
        for bk in BASKETS:
            rows[w][bk] = {"PROD": res["static_grid"][w][bk][gname(0)], "STATIC_SEL": res["static_grid"][w][bk][gname(g_sel)],
                           "ORACLE": basket_run(data, sig, lambda c: SEL[("ORACLE", c)], lambda c: "ORACLE", w, bk)}
            for v in MENU:
                rows[w][bk][vname(v)] = basket_run(data, sig, *adaptive(v, SEL, bk), w, bk)
            for k, r in rows[w][bk].items():
                print(f"{w:<6} {bk:<8} {k:<24} ret {r['ret']:+7.1f}% DD {r['dd']:5.1f} calmar "
                      f"{None if r['calmar'] is None else round(r['calmar'], 2)} switches/coin-yr {r['switches_per_coin_year']:.0f} "
                      f"fees {r['fees_pct_per_year']:.1f}%/yr", flush=True)
    res["windows"] = rows

    menu = [vname(v) for v in MENU]
    mean_cal = {k: float(np.mean([rows["SELECT"][bk][k]["calmar"] for bk in BASKETS])) for k in menu}
    chosen = max(menu, key=lambda k: mean_cal[k])
    t, ch = rows["TEST"], rows["CHOP"]
    ok_test = all(t[bk][chosen]["ret"] > t[bk]["STATIC_SEL"]["ret"] and t[bk][chosen]["dd"] <= t[bk]["STATIC_SEL"]["dd"] + 2 for bk in BASKETS)
    ok_chop = all(ch[bk][chosen]["ret"] > ch[bk]["STATIC_SEL"]["ret"] for bk in BASKETS)
    breadth = {bk: float(np.mean([t[bk][k]["ret"] > t[bk]["STATIC_SEL"]["ret"] for k in menu])) for bk in BASKETS}
    ok_breadth = all(x >= 0.5 for x in breadth.values())
    res.update(select_mean_calmar=mean_cal, selected=chosen, checks=dict(test=bool(ok_test), chop=bool(ok_chop), breadth=breadth),
               verdict="PASS" if ok_test and ok_chop and ok_breadth else "NO-GO")

    print("== yearly", flush=True)
    yearly = []
    named = {"PROD": static(0), "STATIC_SEL": static(g_sel)}
    for y in range(2021, 2027):
        for bk in BASKETS:
            lo = f"{y}-01-01"
            if pd.Timestamp(lo) < pd.Timestamp(EVAL["SELECT"][bk][0]):
                continue
            EVAL[f"Y{y}"] = {b_: (lo, f"{y + 1}-01-01" if y < 2026 else "2026-09-13") for b_ in BASKETS}
            row = dict(year=y, basket=bk)
            for k, fns in named.items():
                row[k] = basket_run(data, sig, *fns, f"Y{y}", bk)["ret"]
            row["SELECTED"] = basket_run(data, sig, *adaptive(next(v for v in MENU if vname(v) == chosen), SEL, bk), f"Y{y}", bk)["ret"]
            row["ORACLE"] = basket_run(data, sig, lambda c: SEL[("ORACLE", c)], lambda c: "ORACLE", f"Y{y}", bk)["ret"]
            yearly.append(row)
            print(row, flush=True)
    res["yearly"] = yearly

    print(f"\nselected {chosen} (SELECT mean Calmar {mean_cal[chosen]:.2f}) -> {res['verdict']} {res['checks']}  [{time.time() - t0:.0f}s]")
    OUT.write_text(json.dumps(res, indent=1, default=float))


if __name__ == "__main__":
    main()
