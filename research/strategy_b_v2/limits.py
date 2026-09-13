"""Strategy B (spot + trend hedge, the paper "cold wallet" config) — limits, flaws, what to expect.

Long history: Binance spot 1h + Binance perp funding (hourly-spread proxy for HL), BTC/ETH from 2019,
SOL/AVAX from late 2020 (fetch_long.py). Everything before 2023-06 is data the rules were never fitted on.
The book is the production CoinBook (src/frab/strategy/b2), same config as the paper test.

Sections (all in limits.json):
  yearly     — calendar years, fresh $ start each Jan 1 (signals warm), strategy vs holding
  rolling    — every 12-month window starting on the 1st of a month: distribution of outcomes
  episodes   — each hedge episode on a continuous run: what it saved / what it cost, and the drop taken
               before the hedge switched on
  exposure   — does timing add anything over a constant spot share with the same average exposure?
  params     — sensitivity on the pre-2023 data (never used for fitting): sticky, lookbacks, ratchet
  capital    — how small capital degrades results through HL's $10 minimum order
"""
import json, sys
from dataclasses import replace
from pathlib import Path
import numpy as np, pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
import frab.strategy.b2.book as B
from frab.strategy.b2.book import CoinBook, start_book, step
from frab.strategy.b2.params import B2Params

DATA = Path("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/b_long")
OUT = Path(__file__).with_name("limits.json")
COINS = ["BTC", "ETH", "SOL", "AVAX"]
LEV = {"BTC": 3.0, "ETH": 2.0, "SOL": 1.5, "AVAX": 1.5}
MMR = {"BTC": 0.0125, "ETH": 0.02, "SOL": 0.025, "AVAX": 0.05}
WARM = 800


def params(coin, capital=1000.0, **kw):
    return B2Params(coins=(coin,), capital_usd=capital, carry_enabled=False, hedge_margin_headroom=1.5,
                    min_order_usd=10.0, short_leverage={coin: LEV[coin]}, maint_margin_rate={coin: MMR[coin]}, **kw)


def load(coin):
    df = pd.read_csv(DATA / f"{coin}.csv", parse_dates=["time"]).set_index("time")
    full = pd.date_range(df.index.min(), df.index.max(), freq="h")
    df = df[~df.index.duplicated()].reindex(full)
    df[["close", "high"]] = df[["close", "high"]].ffill()
    df["fundingRate"] = df["fundingRate"].fillna(0.0)
    return df


def simulate(df, coin, p, a, b, events=False):
    px = df["close"].tolist(); hi = df["high"].tolist(); fr = df["fundingRate"].tolist()
    hist = max(730, B.H30 + 10)                 # closes must reach back past the longest lookback
    warm = max(WARM, B.H30 + 50)
    book = CoinBook.new(coin, p)
    for i in range(max(0, a - warm), a):
        B.advance_signals(book, px[max(0, i - hist):i + 1], fr[max(0, i - 8):i + 1], p)
    start_book(book, bar_ms=a, price=px[a], params=p)
    n = b - a + 1
    eq = np.empty(n); net = np.empty(n); ev_all = []; liq_d = []
    for k, i in enumerate(range(a, b + 1)):
        ev = step(book, bar_ms=i, price=px[i], funding_rate=fr[i], closes=px[max(0, i - hist):i + 1],
                  funding_hist=fr[max(0, i - 8):i + 1], params=p, high=hi[i])
        eq[k] = book.equity(px[i])
        net[k] = (book.units_spot - book.hedge_units()) * px[i]
        liq = book.liquidation_price()
        if liq:
            liq_d.append(liq / hi[i] - 1)
        if events and ev:
            ev_all += [dict(e, i=i) for e in ev]
    return dict(eq=eq, net=net, book=book, events=ev_all, min_liq=min(liq_d) * 100 if liq_d else None,
                hold=np.array(px[a:b + 1]) / px[a])


def dd(path):
    path = np.asarray(path, float)
    return float(-(path / np.maximum.accumulate(path) - 1).min() * 100)


def idx_of(df, ts):
    ts = pd.Timestamp(ts)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return int(df.index.searchsorted(ts))


def portfolio(data, coins, lo, hi, p_of=params, events=False):
    """Equal $1000 books per coin, fresh start at lo. Returns strategy/hold equity paths (summed)."""
    runs = {}
    for c in coins:
        df = data[c]
        a, b = idx_of(df, lo), idx_of(df, hi) - 1
        if a < 24 * 35 or b <= a:
            return None
        runs[c] = simulate(df, c, p_of(c), a, b, events)
    n = min(len(r["eq"]) for r in runs.values())
    eq = sum(r["eq"][:n] for r in runs.values()); hold = sum(1000.0 * r["hold"][:n] for r in runs.values())
    net = sum(r["net"][:n] for r in runs.values())
    return dict(eq=eq, hold=hold, net=net, runs=runs, cap=1000.0 * len(coins))


def summary(eq, cap, hours):
    years = hours / 8760
    ret = eq[-1] / cap - 1
    return dict(ret=ret * 100, ann=((1 + ret) ** (1 / years) - 1) * 100 if years >= 0.5 else None,
                dd=dd(np.concatenate([[cap], eq])))


def yearly(data):
    out = []
    for y in range(2019, 2027):
        lo, hi = f"{y}-01-01", (f"{y + 1}-01-01" if y < 2026 else "2026-09-13")
        for label, coins in (("BTC+ETH", ["BTC", "ETH"]), ("4 coins", COINS)):
            pf = portfolio(data, coins, lo, hi)
            if pf is None:
                continue
            s, h = summary(pf["eq"], pf["cap"], len(pf["eq"])), summary(pf["hold"], pf["cap"], len(pf["eq"]))
            books = [r["book"] for r in pf["runs"].values()]
            out.append(dict(year=y, basket=label, ret=s["ret"], dd=s["dd"], hold_ret=h["ret"], hold_dd=h["dd"],
                            hedges=sum(b_.trades for b_ in books), top_ups=sum(b_.margin_rebals for b_ in books),
                            liquidations=sum(b_.liquidations for b_ in books),
                            hedge_funding=sum(b_.funding_total for b_ in books) / pf["cap"] * 100,
                            fees=sum(b_.perp_fees + b_.spot_fees + b_.margin_fees for b_ in books) / pf["cap"] * 100,
                            min_liq=min([r["min_liq"] for r in pf["runs"].values() if r["min_liq"] is not None], default=None),
                            avg_net_exposure=float(np.mean(pf["net"] / pf["eq"]) * 100)))
            print(f"{y} {label:<8} strat {s['ret']:+7.1f}% dd {s['dd']:5.1f} | hold {h['ret']:+7.1f}% dd {h['dd']:5.1f} "
                  f"| hedges {out[-1]['hedges']} top-ups {out[-1]['top_ups']} liq {out[-1]['liquidations']} "
                  f"fund {out[-1]['hedge_funding']:+.1f}% exp {out[-1]['avg_net_exposure']:.0f}%", flush=True)
    return out


def rolling(data):
    out = []
    for label, coins, first in (("BTC+ETH", ["BTC", "ETH"], "2019-11-01"), ("4 coins", COINS, "2020-12-01")):
        for lo in pd.date_range(first, "2025-09-01", freq="MS", tz="UTC"):
            hi = lo + pd.DateOffset(years=1)
            pf = portfolio(data, coins, lo, hi)
            if pf is None:
                continue
            s, h = summary(pf["eq"], pf["cap"], len(pf["eq"])), summary(pf["hold"], pf["cap"], len(pf["eq"]))
            out.append(dict(basket=label, start=str(lo)[:7], ret=s["ret"], dd=s["dd"], hold_ret=h["ret"], hold_dd=h["dd"]))
        rs = np.array([r["ret"] for r in out if r["basket"] == label]); ds = np.array([r["dd"] for r in out if r["basket"] == label])
        hs = np.array([r["hold_ret"] for r in out if r["basket"] == label])
        print(f"rolling 12m {label}: n={len(rs)} loss in {np.mean(rs < 0) * 100:.0f}% | p10 {np.percentile(rs, 10):+.1f}% "
              f"median {np.median(rs):+.1f}% p90 {np.percentile(rs, 90):+.1f}% worst {rs.min():+.1f}% best {rs.max():+.1f}% "
              f"| DD median {np.median(ds):.1f} max {ds.max():.1f} | hold loss in {np.mean(hs < 0) * 100:.0f}% median {np.median(hs):+.1f}%", flush=True)
    return out


def episodes(data):
    """Hedge episodes on one continuous 4-coin run 2020-12 .. 2026-09."""
    out = []
    for c in COINS:
        df = data[c]; a, b = idx_of(df, "2020-12-01"), len(df) - 1
        r = simulate(df, c, params(c), a, b, events=True)
        px = df["close"].values
        opens = [e for e in r["events"] if e["kind"] == "hedge_open"]
        closes = [e for e in r["events"] if e["kind"] in ("hedge_close", "liquidation")]
        tops = [e for e in r["events"] if e["kind"] == "margin_rebalance"]
        for o in opens:
            cl = next((x for x in closes if x["i"] > o["i"]), None)
            j = cl["i"] if cl else b
            realized = sum(x.get("realized", 0.0) for x in closes if x["i"] == j)
            topped = sum(1 for t in tops if o["i"] < t["i"] <= j)
            peak = px[max(0, o["i"] - 60 * 24):o["i"] + 1].max()
            out.append(dict(coin=c, open=str(df.index[o["i"]])[:13], hours=j - o["i"], move=(px[j] / px[o["i"]] - 1) * 100,
                            hedge_pnl_pct_of_notional=(o["price"] - px[j]) / o["price"] * 100, top_ups=topped,
                            drop_before=(px[o["i"]] / peak - 1) * 100))
    e = pd.DataFrame(out)
    win = e[e.move < 0]; lose = e[e.move >= 0]
    print(f"hedge episodes {len(e)}: price fell in {len(win)} (avg {win.move.mean():+.1f}%), rose in {len(lose)} "
          f"(avg {lose.move.mean():+.1f}%), median length {e.hours.median() / 24:.0f}d; drop from 60d high before the hedge "
          f"switched on: median {e.drop_before.median():.1f}% p90 {e.drop_before.quantile(0.1):.1f}%", flush=True)
    print("worst hedge episodes (price rose while hedged):")
    print(e.sort_values("move", ascending=False).head(6).to_string(index=False))
    return out


def exposure(data):
    """Constant-mix benchmark with the strategy's own average net exposure (rebalanced daily)."""
    out = []
    for label, coins, lo in (("BTC+ETH", ["BTC", "ETH"], "2019-11-01"), ("4 coins", COINS, "2020-12-01")):
        pf = portfolio(data, coins, lo, "2026-09-13")
        expo = float(np.mean(pf["net"] / pf["eq"]))
        daily_idx = np.arange(0, len(pf["eq"]), 24)
        hold_d = pf["hold"][daily_idx]; r = np.diff(hold_d) / hold_d[:-1]
        mix = pf["cap"] * np.cumprod(np.concatenate([[1.0], 1 + expo * r]))
        s = summary(pf["eq"], pf["cap"], len(pf["eq"])); m = summary(mix, pf["cap"], len(pf["eq"]))
        out.append(dict(basket=label, start=lo, avg_net_exposure=expo * 100, strat=s, constant_mix=m))
        print(f"exposure {label} from {lo}: avg net exposure {expo * 100:.0f}% | strategy {s['ann']:+.1f}%/y DD {s['dd']:.1f} "
              f"| constant {expo * 100:.0f}% spot {m['ann']:+.1f}%/y DD {m['dd']:.1f}", flush=True)
    return out


def sensitivity(data):
    """Pre-2023 only (2020-12 .. 2023-06), 4 coins: sticky x lookbacks x ratchet."""
    out = []
    base_h14, base_h30 = B.H14, B.H30
    try:
        for (l1, l2) in ((7, 14), (14, 30), (21, 45), (30, 60)):
            B.H14, B.H30 = l1 * 24, l2 * 24
            for sticky in (0, 12, 48):
                for thr in (0.25, 0.5, 1.0):
                    pf = portfolio(data, COINS, "2020-12-01", "2023-06-01",
                                   p_of=lambda c: params(c, sticky_exit_hours=sticky, ratchet_threshold=thr))
                    s = summary(pf["eq"], pf["cap"], len(pf["eq"]))
                    out.append(dict(lookbacks=f"{l1}/{l2}", sticky=sticky, ratchet=thr, ann=s["ann"], dd=s["dd"]))
                    print(f"  lb {l1}/{l2} sticky {sticky:>2} ratchet {thr:.2f}: {s['ann']:+6.1f}%/y DD {s['dd']:5.1f}", flush=True)
    finally:
        B.H14, B.H30 = base_h14, base_h30
    return out


def capital(data):
    out = []
    for cap_total in (60, 120, 257, 1000, 10000):
        pf = portfolio(data, COINS, "2020-12-01", "2026-09-13", p_of=lambda c: params(c, capital=cap_total / 4))
        s = summary(pf["eq"], cap_total / 4 * 4, len(pf["eq"]))
        blocked = sum(r["book"].hedge_limited for r in pf["runs"].values())
        hedges = sum(r["book"].trades for r in pf["runs"].values())
        out.append(dict(capital=cap_total, ann=s["ann"], dd=s["dd"], hedges=hedges))
        print(f"capital ${cap_total:>6}: {s['ann']:+.1f}%/y DD {s['dd']:.1f} hedges {hedges}", flush=True)
    return out


def main():
    data = {c: load(c) for c in COINS}
    res = {}
    print("== yearly"); res["yearly"] = yearly(data)
    print("== rolling"); res["rolling"] = rolling(data)
    print("== exposure"); res["exposure"] = exposure(data)
    print("== episodes"); res["episodes"] = episodes(data)
    print("== capital"); res["capital"] = capital(data)
    print("== sensitivity (pre-2023)"); res["params"] = sensitivity(data)
    OUT.write_text(json.dumps(res, indent=1, default=float))


if __name__ == "__main__":
    main()
