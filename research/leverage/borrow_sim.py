"""Borrowing stablecoins against BTC to fund the strategies: what survives 2020-2026?

Scenario simulation, not a hypothesis test: the question is not "does it earn more" (a positive spread
always earns more on average) but "at what loan-to-value does the whole construction survive the crashes
that actually happened" — March 2020 (-50% in two days) and 2021-11 .. 2022-11 (-77%).

Setup, one dollar of own capital = 1:
  * own capital is split between BTC held as collateral and the strategy account;
  * against the collateral we borrow stablecoins at the starting LTV, at 4% (today's Aave rate) or 8%
    (the bull-market level), accrued daily, never repaid voluntarily;
  * everything borrowed goes into the strategy account;
  * liquidation follows Aave: when debt exceeds 78% of the collateral's value, up to half the debt is
    repaid from the collateral with an 8% penalty, repeated daily until healthy again;
  * the strategy account runs the three sleeves, each scaled to 15%/yr over the sample so the experiment
    is about the loan, not about which sleeve is better: 40% funding carry (the FRAB stand-in),
    35% B v2 on BTC+ETH, 25% the trend book at its 14% book-volatility setting; weights are fixed and
    the account is not rebalanced between sleeves.
  * net worth = collateral + strategy account - debt.

AMENDMENT 2026-09-14, after the first run: starting in January 2020 answers nothing — BTC rose 11x from
there and the collateral never fell below its starting value, so no LTV ever liquidated. The honest
version rolls the start date: every month from 2020-01 to 2024-09, each run held for two years, and the
question becomes "what share of start dates ends in liquidation". The entry at the November 2021 top is
reported on its own, because that is the case worth planning for.
Reported per (collateral share, starting LTV): the share of starts that liquidate, the median and worst
two-year outcome, and what borrowing added or cost against the same start with no loan.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
for p in ("trend_following", "cross_sectional", "xsmom_v2", "validation_harness", "strategy_b_v2"):
    sys.path.insert(0, str(HERE.parent / p))

import engine as X                                      # noqa: E402
import limits as B                                      # noqa: E402
import long_history as L                                # noqa: E402
import risk_shaping as R                                # noqa: E402
from trend import portfolio_returns_directional         # noqa: E402

OUT = HERE / "borrow_sim.json"
START, END = "2020-01-02", "2026-09-12"
LIQ_THRESHOLD, LIQ_PENALTY, LIQ_CLOSE_FACTOR = 0.78, 0.08, 0.5
TARGET_ANN = 0.15
MIX = {"carry": 0.40, "b": 0.35, "trend": 0.25}
CRASHES = {"COVID 2020-02..04": ("2020-02-01", "2020-04-30"),
           "медведь 2021-11..2022-12": ("2021-11-01", "2022-12-31"),
           "пила 2025-06..2026-09": ("2025-06-01", "2026-09-12")}


def scale_to_ann(pnl: pd.Series, target: float = TARGET_ANN) -> pd.Series:
    years = len(pnl) / 365
    k = 1.0
    for _ in range(80):
        ann = (1 + pnl * k).prod() ** (1 / years) - 1
        if ann <= 0:
            return pnl * 0.0
        k *= (target / ann) ** 0.5
    return pnl * k


def sleeves() -> tuple[pd.Series, pd.Series]:
    """Daily return of the strategy mix, and BTC's daily return, on one common index."""
    p = X.build_panel()
    cpanel, caccr, celig = L.to_trend_panel(p, hl_only=True)
    trend = R.book_vol_scaled(L.rescale(portfolio_returns_directional(
        R.scaled_positions(cpanel, celig), cpanel["fwd_ret"], L.BPS_LOW, accrual=caccr)), target_ann=0.14)
    carry = L.carry_proxy(p)

    data = {c: B.load(c) for c in ("BTC", "ETH")}
    pf = B.portfolio(data, ["BTC", "ETH"], START, END)
    idx = data["BTC"].index[B.idx_of(data["BTC"], START):][:len(pf["eq"])]
    b = pd.Series(pf["eq"], index=idx).resample("1D").last().dropna().pct_change().dropna()
    btc = data["BTC"]["close"].resample("1D").last().dropna().pct_change().dropna()

    df = pd.concat({"carry": scale_to_ann(carry), "b": scale_to_ann(b), "trend": scale_to_ann(trend),
                    "btc": btc}, axis=1).dropna()
    mix = sum(df[k] * w for k, w in MIX.items())
    return mix, df["btc"]


def simulate(mix: pd.Series, btc: pd.Series, *, coll_share: float, ltv: float, rate: float) -> dict:
    """One dollar of own capital; returns the net-worth path and what the loan did to it."""
    coll = coll_share                       # dollars of BTC
    acct = 1.0 - coll_share                 # dollars in the strategy account
    debt = coll * ltv
    acct += debt
    units = coll / 1.0                      # BTC units, price normalised to 1 at the start
    price = 1.0
    net, liq_days, liq_cost = [], 0, 0.0
    for t in mix.index:
        price *= 1 + btc.loc[t]
        acct *= 1 + mix.loc[t]
        debt *= 1 + rate / 365
        value = units * price
        while debt > 0 and value > 0 and debt > value * LIQ_THRESHOLD:
            repay = debt * LIQ_CLOSE_FACTOR
            seized = min(repay * (1 + LIQ_PENALTY) / price, units)
            units -= seized
            debt -= min(repay, seized * price / (1 + LIQ_PENALTY))
            liq_cost += seized * price * LIQ_PENALTY / (1 + LIQ_PENALTY)
            liq_days += 1
            value = units * price
            if units <= 1e-12:
                debt = max(debt - value, 0.0)
                break
        net.append(units * price + acct - debt)
    path = pd.Series(net, index=mix.index)
    years = len(path) / 365
    dd = float(-(path / path.cummax() - 1).min() * 100)
    out = dict(final=float(path.iloc[-1]), cagr=float((path.iloc[-1] ** (1 / years) - 1) * 100),
               maxdd=float(dd), liq_days=int(liq_days), liq_cost_pct=float(liq_cost * 100),
               wiped=bool(path.iloc[-1] <= 0.05))
    for name, (lo, hi) in CRASHES.items():
        seg = path[(path.index >= pd.Timestamp(lo, tz=path.index.tz)) & (path.index <= pd.Timestamp(hi, tz=path.index.tz))]
        if len(seg) > 5:
            out[name] = float((seg.iloc[-1] / seg.iloc[0] - 1) * 100)
    return out


def rolling(mix, btc, *, coll_share, ltv, rate, horizon_days=730):
    """Every month as a start date, each run held for `horizon_days`."""
    starts = pd.date_range(mix.index.min(), mix.index.max() - pd.Timedelta(days=horizon_days), freq="MS",
                           tz=mix.index.tz)
    rows = []
    for s0 in starts:
        seg = mix[(mix.index >= s0)].iloc[:horizon_days]
        segb = btc.reindex(seg.index)
        if len(seg) < horizon_days * 0.9 or segb.isna().any():
            continue
        r = simulate(seg, segb, coll_share=coll_share, ltv=ltv, rate=rate)
        base = simulate(seg, segb, coll_share=coll_share, ltv=0.0, rate=rate)
        rows.append(dict(start=str(s0.date()), final=r["final"], liq=r["liq_days"] > 0,
                         vs_noloan=r["final"] - base["final"], maxdd=r["maxdd"]))
    df = pd.DataFrame(rows)
    return dict(starts=len(df), liquidated_share=float(df.liq.mean()), median_final=float(df.final.median()),
                worst_final=float(df.final.min()), median_vs_noloan=float(df.vs_noloan.median()),
                worst_vs_noloan=float(df.vs_noloan.min()), median_maxdd=float(df.maxdd.median()),
                worst_start=str(df.loc[df.final.idxmin(), "start"]))


def main():
    mix, btc = sleeves()
    print(f"выборка {mix.index.min().date()} .. {mix.index.max().date()} ({len(mix)} дней); "
          f"BTC за период {((1 + btc).prod() - 1) * 100:+.0f}%, худшая просадка BTC "
          f"{-((1 + btc).cumprod() / (1 + btc).cumprod().cummax() - 1).min() * 100:.0f}%", flush=True)

    res = {"single_start_2020": {}, "rolling": {}, "top_2021_11": {}}
    for rate in (0.04, 0.08):
        print(f"\n=== ставка займа {rate:.0%} ===")
        print(f"{'залог в BTC':>12}{'LTV':>7}{'итог x':>9}{'CAGR':>8}{'просадка':>10}{'ликвид.дней':>12}"
              f"{'потери на ликв.':>16}   COVID / медведь / пила")
        for coll_share in (0.0, 0.5, 1.0):
            for ltv in (0.0, 0.2, 0.3, 0.4, 0.5):
                if coll_share == 0.0 and ltv > 0:
                    continue
                r = simulate(mix, btc, coll_share=coll_share, ltv=ltv, rate=rate)
                res["single_start_2020"][f"{rate}|{coll_share}|{ltv}"] = r
                crashes = " / ".join(f"{r.get(k, float('nan')):+.0f}%" for k in CRASHES)
                print(f"{coll_share:>11.0%}{ltv:>7.0%}{r['final']:>9.2f}{r['cagr']:>7.1f}%{r['maxdd']:>9.1f}%"
                      f"{r['liq_days']:>12}{r['liq_cost_pct']:>15.1f}%   {crashes}"
                      + ("   ВСЁ" if r["wiped"] else ""), flush=True)
    print("\n=== скользящие старты: каждый месяц, срок 2 года, ставка 6% ===")
    print(f"{'залог':>7}{'LTV':>6}{'стартов':>9}{'ликвидировано':>15}{'медиана итога':>15}{'худший итог':>13}"
          f"{'медиана vs без займа':>22}{'худший vs без займа':>21}")
    for coll_share in (0.5, 1.0):
        for ltv in (0.2, 0.3, 0.4, 0.5, 0.6):
            r = rolling(mix, btc, coll_share=coll_share, ltv=ltv, rate=0.06)
            res["rolling"][f"{coll_share}|{ltv}"] = r
            print(f"{coll_share:>6.0%}{ltv:>6.0%}{r['starts']:>9}{r['liquidated_share']:>14.0%}"
                  f"{r['median_final']:>15.2f}{r['worst_final']:>13.2f}{r['median_vs_noloan']:>+22.2f}"
                  f"{r['worst_vs_noloan']:>+21.2f}   худший старт {r['worst_start']}", flush=True)

    print("\n=== вход на вершине: старт 2021-11-01, срок 2 года, ставка 6% ===")
    top = mix[(mix.index >= pd.Timestamp('2021-11-01', tz=mix.index.tz))].iloc[:730]
    topb = btc.reindex(top.index)
    print(f"{'залог':>7}{'LTV':>6}{'итог x':>9}{'просадка':>11}{'дней в ликвидации':>19}{'потери на ликв.':>17}")
    for coll_share in (0.5, 1.0):
        for ltv in (0.0, 0.2, 0.3, 0.4, 0.5, 0.6):
            r = simulate(top, topb, coll_share=coll_share, ltv=ltv, rate=0.06)
            res["top_2021_11"][f"{coll_share}|{ltv}"] = r
            print(f"{coll_share:>6.0%}{ltv:>6.0%}{r['final']:>9.2f}{r['maxdd']:>10.1f}%{r['liq_days']:>19}"
                  f"{r['liq_cost_pct']:>16.1f}%" + ("   ЗАЛОГ ПОТЕРЯН" if r["liq_days"] else ""), flush=True)
    OUT.write_text(json.dumps(res, indent=1, default=float))


if __name__ == "__main__":
    main()
