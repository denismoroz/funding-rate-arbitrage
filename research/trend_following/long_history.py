"""Trend-following on 6.7 years instead of 3: the question FINDINGS.md left open.

PRE-REGISTERED 2026-09-14, before any result of this script was seen.

Where it was left (research/trend_following/FINDINGS.md, 2026-09): the committed book — the equal-weight
TSMOM ensemble over lookbacks 30/60/90/120, vol-targeted 2%/day, gross capped at 3 — was characterised on
a 3-year HL-era panel (2023-06-08 .. 2026-06-13). Verdict NEEDS-LIVE-CONFIRMATION: standalone marginal
(DSR 0.81, PBO 0.63), correlation +0.40 with XSMOM, and — the headline caveat — no sustained bear market
in sample, so "crisis alpha" was suggested, not proven. The decision was to build it live only if it adds
to the carry + momentum basket.

Two things changed since:
  * XSMOM is OFF (its own rework found no honest edge), so "a moderately correlated cousin of XSMOM" is
    no longer a reason to reject anything. The basket to add to is now FRAB carry + B v2.
  * We now have the point-in-time Binance panel 2020-01 .. 2026-09 built for the XSMOM rework, delisted
    coins included. It covers the 2021 bull, the 2022 bear and the 2025-26 chop.

So: run the COMMITTED config, unchanged and without any re-fitting, on the years it has never seen.

Menu (unchanged, 8): TSMOM_L30/L60/L90/L120, TSMOM_ENS (the committed book), DONCH_N20/N55/N100.
Constants (unchanged): VOL_TARGET 0.02/day, LEVERAGE_CAP 3.0, vol window 30, accrual = the panel's own
long cash-flow, daily rebalance. Costs: 4.4 bps per leg (measured HL taker + slippage) and 8.5 bps
(the conservative number the earlier study used).
Universe: the point-in-time top-40 by turnover with at least 90 days of history — the same universe the
XSMOM rework calls honest. Robustness: the same run on coins that HL actually lists.

Windows:
  NEW    2020-01-01 .. 2023-06-07  — data the committed config has never seen (it was fixed on the panel
                                     that starts 2023-06-08). This is the real out-of-sample.
  OLD    2023-06-08 .. 2026-06-13  — the sample FINDINGS.md used.
  FRESH  2026-06-14 .. 2026-09-12  — since the study was written.
  plus a calendar-year table, and 2022 on its own (the bear).

Portfolio fit (the open question): daily correlation of the committed book with
  * B v2 — the paper book itself (4 coins, cold-wallet config, continuous run), and
  * a carry proxy for FRAB — a delta-neutral funding harvest on the same panel: every day hold the 8
    eligible coins with the highest trailing 7-day funding (spot long + perp short), earn their funding,
    pay 4.4 bps per leg on turnover.
Reported full-sample and as a rolling 90-day range, plus an inverse-vol blend of trend with B.

GO to a paper test (NOT to live) requires all four:
  1. NEW window: Sharpe >= 0.4 at 4.4 bps and total return > 0 at 8.5 bps;
  2. 2022 (the bear year): total return > 0 at 4.4 bps — the crisis-alpha claim, tested for the first time;
  3. |correlation| with B v2 <= 0.3 AND with the carry proxy <= 0.3 over the full overlap;
  4. on the full 2020-2026 sample: DSR (deflated over the menu of 8) >= 0.90 and PBO <= 0.5.
Anything else is NO-GO, and the report says which leg failed.
"""
import json, sys, time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
for p in ("research/trend_following", "research/cross_sectional", "research/xsmom_v2", "research/validation_harness",
          "research/strategy_b_v2", "src"):
    sys.path.insert(0, str(ROOT / p))

import engine as X                                   # xsmom_v2 point-in-time panel
import limits as B                                   # strategy B v2 book
from trend import realized_vol, tsmom_ensemble, tsmom_signal, donchian_signal, portfolio_returns_directional
from metrics import dsr_from_returns
from pbo import pbo
from splitter import cpcv

OUT = Path(__file__).with_name("long_history.json")
VOL_TARGET, LEVERAGE_CAP, VOL_WINDOW = 0.02, 3.0, 30
TSMOM_LOOKBACKS, DONCHIAN_CHANNELS = (30, 60, 90, 120), (20, 55, 100)
COMMITTED = "TSMOM_ENS"
BPS_LOW, BPS_HIGH = 4.4, 8.5
NEW = ("2020-01-01", "2023-06-07")
OLD = ("2023-06-08", "2026-06-13")
FRESH = ("2026-06-14", "2026-09-12")
HL_COINS = ["BTC", "ETH", "SOL", "AVAX", "BNB", "XRP", "DOGE", "LINK", "LTC", "ADA", "ARB", "OP", "SUI", "APT",
            "NEAR", "TIA", "INJ", "AAVE", "UNI", "MKR", "LDO", "CRV", "DYDX", "ATOM", "FIL", "ETC", "BCH", "TRX",
            "WLD", "SEI", "STX", "RUNE", "FTM", "GALA", "SAND", "MANA", "APE", "PEPE", "WIF", "ORDI", "TON", "HYPE"]


def stats(pnl):
    pnl = np.asarray(pnl, float)
    sd = pnl.std(ddof=1)
    eq = np.cumprod(1 + pnl)
    years = len(pnl) / 365
    return dict(sharpe=float(pnl.mean() / sd * np.sqrt(365)) if sd > 0 else 0.0,
                ann=float((eq[-1] ** (1 / years) - 1) * 100) if eq[-1] > 0 else -100.0,
                total=float((eq[-1] - 1) * 100), maxdd=float(-(eq / np.maximum.accumulate(eq) - 1).min() * 100),
                days=int(len(pnl)))


def to_trend_panel(p: X.Panel, hl_only=False):
    """xsmom_v2 point-in-time panel -> the dict trend.py expects, plus the eligibility mask."""
    dates, syms = p.dates, [s.replace("USDT", "") for s in p.syms]
    price = pd.DataFrame(p.close, index=dates, columns=syms)
    fwd = pd.DataFrame(p.fwd, index=dates, columns=syms)
    accr = pd.DataFrame(p.accr, index=dates, columns=syms)
    elig = pd.DataFrame(p.elig, index=dates, columns=syms)
    if hl_only:
        keep = [c for c in syms if c in HL_COINS]
        price, fwd, accr, elig = price[keep], fwd[keep], accr[keep], elig[keep]
    return dict(coins=list(price.columns), price=price, fwd_ret=fwd), accr, elig


def menu_pnls(panel, accr, elig, bps):
    """The 8 committed books as daily net pnl series (positions outside the universe are flat)."""
    price, fwd = panel["price"], panel["fwd_ret"]
    vol = realized_vol(price, VOL_WINDOW)
    out = {}
    for L in TSMOM_LOOKBACKS:
        out[f"TSMOM_L{L}"] = tsmom_signal(panel, lookback=L, vol_window=VOL_WINDOW)
    out[COMMITTED] = tsmom_ensemble(panel, lookbacks=TSMOM_LOOKBACKS, vol_window=VOL_WINDOW)
    for N in DONCHIAN_CHANNELS:
        out[f"DONCH_N{N}"] = donchian_signal(panel, channel=N)
    return {k: portfolio_returns_directional(sig.where(elig, 0.0), fwd, bps, accrual=accr, vol=vol,
                                             vol_target=VOL_TARGET, leverage_cap=LEVERAGE_CAP)
            for k, sig in out.items()}


def carry_proxy(p: X.Panel, top=8, bps=BPS_LOW):
    """FRAB-like delta-neutral funding harvest on the same panel: hold the top-8 funding coins, earn funding."""
    fund = pd.DataFrame(p.fund, index=p.dates, columns=p.syms).fillna(0.0)
    accr = pd.DataFrame(p.accr, index=p.dates, columns=p.syms).fillna(0.0)
    elig = pd.DataFrame(p.elig, index=p.dates, columns=p.syms)
    trail = fund.rolling(7).mean().where(elig)
    w_prev = pd.Series(0.0, index=fund.columns)
    rows = []
    for t in fund.index:
        s = trail.loc[t].dropna()
        s = s[s > 0].nlargest(top)
        w = pd.Series(0.0, index=fund.columns)
        if len(s):
            w[s.index] = 1.0 / len(s)
        cost = float((w - w_prev).abs().sum()) * 2 * bps / 1e4          # two legs: spot and perp
        rows.append(float(-(w * accr.loc[t]).sum()) - cost)             # short perp earns +funding
        w_prev = w
    return pd.Series(rows, index=fund.index)


def b_returns():
    """Strategy B v2, 4 coins, continuous run -> daily returns."""
    data = {c: B.load(c) for c in B.COINS}
    pf = B.portfolio(data, B.COINS, "2020-12-01", "2026-09-13")
    eq = pd.Series(pf["eq"], index=data["BTC"].index[B.idx_of(data["BTC"], "2020-12-01"):][:len(pf["eq"])])
    d = eq.resample("1D").last().dropna()
    return d.pct_change().dropna()


def window(s, lo, hi):
    return s[(s.index >= pd.Timestamp(lo, tz="UTC")) & (s.index <= pd.Timestamp(hi, tz="UTC"))]


def main():
    t0 = time.time()
    p = X.build_panel()
    panel, accr, elig = to_trend_panel(p)
    print(f"panel {len(p.dates)} days x {len(p.syms)} perps; eligible per day median {int(np.median(p.elig.sum(1)))}", flush=True)

    books = {bps: menu_pnls(panel, accr, elig, bps) for bps in (BPS_LOW, BPS_HIGH)}
    low, high = books[BPS_LOW], books[BPS_HIGH]
    res = {"windows": {}, "yearly": [], "menu": list(low)}

    for wname, (lo, hi) in {"NEW": NEW, "OLD": OLD, "FRESH": FRESH, "FULL": (NEW[0], FRESH[1])}.items():
        res["windows"][wname] = {k: stats(window(v, lo, hi)) for k, v in low.items()}
        res["windows"][wname + "_8.5bps"] = {COMMITTED: stats(window(high[COMMITTED], lo, hi))}
        c = res["windows"][wname][COMMITTED]; c85 = res["windows"][wname + "_8.5bps"][COMMITTED]
        best = max(low, key=lambda k: stats(window(low[k], lo, hi))["sharpe"])
        print(f"{wname:<6} {c['days']:>4}d | committed 4.4bps SR {c['sharpe']:+.2f} total {c['total']:+8.1f}% DD {c['maxdd']:.0f} "
              f"| 8.5bps SR {c85['sharpe']:+.2f} total {c85['total']:+8.1f}% | best of menu {best}", flush=True)

    for y in range(2020, 2027):
        s = low[COMMITTED][low[COMMITTED].index.year == y]
        if len(s) > 30:
            st = stats(s)
            res["yearly"].append(dict(year=y, **st))
            print(f"  {y}: SR {st['sharpe']:+.2f} total {st['total']:+8.1f}% DD {st['maxdd']:.0f}", flush=True)

    # ── validation on the full sample ──
    R = pd.DataFrame(low).dropna()
    names = list(R.columns)
    Rv = R.values
    sr_period = Rv.mean(0) / Rv.std(0, ddof=1)
    res["dsr"] = dsr_from_returns(Rv[:, names.index(COMMITTED)], sr_period)
    pr = pbo(Rv, S=16, names=names)
    res["pbo"] = dict(pbo=pr.pbo, n_splits=pr.n_splits, median_oos_rank=pr.median_oos_rank)
    tr = []
    for sp in cpcv(len(R), n_groups=6, k=2, purge=120, embargo=7):
        a, b_ = Rv[sp.train_idx], Rv[sp.test_idx]
        j = int(np.argmax(a.mean(0) / a.std(0, ddof=1)))
        tr.append(float(b_[:, j].mean() / b_[:, j].std(ddof=1) * np.sqrt(365)))
    res["cpcv_transfer"] = dict(median_test_sharpe=float(np.median(tr)), share_positive=float(np.mean(np.array(tr) > 0)))
    print(f"DSR {res['dsr']['dsr']:.3f} | PBO {pr.pbo:.3f} | CPCV median test Sharpe {np.median(tr):+.2f} "
          f"positive {np.mean(np.array(tr) > 0) * 100:.0f}%", flush=True)

    # ── portfolio fit ──
    carry = carry_proxy(p)
    bret = b_returns()
    trend = low[COMMITTED]
    fits = {}
    for name, other in (("B_v2", bret), ("carry_proxy", carry)):
        j = pd.concat([trend.rename("t"), other.rename("o")], axis=1).dropna()
        roll = j["t"].rolling(90).corr(j["o"]).dropna()
        fits[name] = dict(n=len(j), corr=float(j["t"].corr(j["o"])), roll90_min=float(roll.min()),
                          roll90_max=float(roll.max()), roll90_share_below_0_3=float((roll.abs() < 0.3).mean()),
                          other=stats(j["o"].values))
        print(f"corr trend vs {name}: {fits[name]['corr']:+.3f} (rolling 90d {fits[name]['roll90_min']:+.2f} .. "
              f"{fits[name]['roll90_max']:+.2f}, |corr|<0.3 in {fits[name]['roll90_share_below_0_3'] * 100:.0f}% of windows) "
              f"| {name} itself SR {fits[name]['other']['sharpe']:+.2f}", flush=True)
    j = pd.concat([trend.rename("t"), bret.rename("b")], axis=1).dropna()
    w = (1 / j.std()) / (1 / j.std()).sum()
    blend = (j * w).sum(axis=1)
    fits["blend_b_trend"] = dict(weights=w.round(3).to_dict(), blend=stats(blend.values), b_alone=stats(j["b"].values),
                                 trend_alone=stats(j["t"].values))
    print(f"inverse-vol blend B+trend {w.round(2).to_dict()}: SR {stats(blend.values)['sharpe']:+.2f} DD {stats(blend.values)['maxdd']:.0f} "
          f"| B alone SR {stats(j['b'].values)['sharpe']:+.2f} DD {stats(j['b'].values)['maxdd']:.0f}", flush=True)
    res["fits"] = fits

    # ── capacity: how many legs the book actually holds ──
    vol = realized_vol(panel["price"], VOL_WINDOW)
    sig = tsmom_ensemble(panel, lookbacks=TSMOM_LOOKBACKS, vol_window=VOL_WINDOW).where(elig, 0.0)
    scaled = (sig * (VOL_TARGET / vol)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    gross = scaled.abs().sum(axis=1)
    capped = scaled.div(np.maximum(gross / LEVERAGE_CAP, 1.0), axis=0)
    legs = (capped.abs() > 1e-9).sum(axis=1)
    smallest = capped.abs().replace(0, np.nan).min(axis=1)
    res["capacity"] = dict(median_legs=float(legs.median()), median_gross=float(capped.abs().sum(axis=1).median()),
                           median_smallest_leg_share=float(smallest.median()),
                           min_capital_usd_for_10_dollar_leg=float(10 / smallest.median()))
    print(f"capacity: median {legs.median():.0f} legs, gross {capped.abs().sum(axis=1).median():.2f}x, "
          f"smallest leg {smallest.median() * 100:.2f}% of capital -> ${10 / smallest.median():,.0f} minimum", flush=True)

    # ── robustness: only coins HL actually lists ──
    panel_hl, accr_hl, elig_hl = to_trend_panel(p, hl_only=True)
    hl = menu_pnls(panel_hl, accr_hl, elig_hl, BPS_LOW)[COMMITTED]
    res["hl_only"] = {k: stats(window(hl, *v)) for k, v in {"NEW": NEW, "OLD": OLD, "FULL": (NEW[0], FRESH[1])}.items()}
    print("HL-listed coins only:", {k: f"SR {v['sharpe']:+.2f} total {v['total']:+.0f}%" for k, v in res["hl_only"].items()}, flush=True)

    # ── pre-registered verdict ──
    new_low, new_high = res["windows"]["NEW"][COMMITTED], res["windows"]["NEW_8.5bps"][COMMITTED]
    y2022 = next((r for r in res["yearly"] if r["year"] == 2022), None)
    checks = dict(
        new_window=bool(new_low["sharpe"] >= 0.4 and new_high["total"] > 0),
        bear_2022=bool(y2022 and y2022["total"] > 0),
        uncorrelated=bool(abs(fits["B_v2"]["corr"]) <= 0.3 and abs(fits["carry_proxy"]["corr"]) <= 0.3),
        validation=bool(res["dsr"]["dsr"] >= 0.90 and pr.pbo <= 0.5))
    res["checks"] = checks
    res["verdict"] = "GO-TO-PAPER" if all(checks.values()) else "NO-GO"
    print(f"\nchecks {checks} -> {res['verdict']}  [{time.time() - t0:.0f}s]")
    OUT.write_text(json.dumps(res, indent=1, default=float))


if __name__ == "__main__":
    main()
