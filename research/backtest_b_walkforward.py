"""
Strategy B — WALK-FORWARD (rolling OOS signal selection).

Бьёт по селекшн-байасу: на каждом train-окне ВЫБИРАЕМ лучший сигнал из меню
по Calmar, применяем на следующем test-окне (out-of-sample), катим окно.
Если OOS-перформанс держится ~как in-sample — выбор робастный, не оверфит.

Сравниваем 3 потока на ОДНИХ И ТЕХ ЖЕ test-окнах:
  WF-select  — сигнал выбран на train (честный OOS)
  fixed base — всегда mom14|mom30 (наш дефолт)
  buy & hold

Косты фикс: thr=0.20, slip=5bps, lag=1, cash=4%.
Train=12мес, Test=3мес, шаг=3мес.
"""
import numpy as np
import pandas as pd
from collections import Counter
from pathlib import Path
from engine import STAKING_YIELD, load_data, buy_and_hold, compute_metrics, HOURS_PER_YEAR, TOTAL_CAPITAL
from backtest_b_constdollar import simulate_constdollar

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]
THR, SLIP, LAG, CASH = 0.20, 0.0005, 1, 0.04
MONTH_H = 730
TRAIN_H, TEST_H = 12 * MONTH_H, 3 * MONTH_H


def cagr(pnl):
    years = len(pnl) / HOURS_PER_YEAR
    end = TOTAL_CAPITAL + float(np.sum(pnl))
    return (((end / TOTAL_CAPITAL) ** (1 / years) - 1) * 100) if end > 0 else -100.0


def calmar(pnl):
    m = compute_metrics(pnl)
    return cagr(pnl) / m["max_dd_pct"] if m["max_dd_pct"] > 0 else 0


def menu(close):
    s = pd.Series(close)
    def m(d): return (s.pct_change(d * 24).fillna(0).values < 0)
    m7, m14, m21, m30 = m(7), m(14), m(21), m(30)
    return {
        "mom14d": m14, "mom30d": m30, "mom21d": m21,
        "mom14|mom30": m14 | m30, "mom14&mom30": m14 & m30,
        "2of3": (m7.astype(int) + m14.astype(int) + m30.astype(int)) >= 2,
    }


def sim_slice(df, stk, hedge, refill, a, b):
    pnl, info = simulate_constdollar(df.iloc[a:b], stk, hedge[a:b],
                                     rebal_threshold=THR, risk_free_apr=CASH,
                                     refill_confirm=refill[a:b], signal_lag=LAG, slippage=SLIP)
    return pnl, info


def main():
    picks = Counter()
    wf_pnl_all, fix_pnl_all, bh_pnl_all = {}, {}, {}
    for coin in COINS:
        df = load_data(coin)
        if df.empty:
            continue
        close = df["close"].values
        stk = STAKING_YIELD.get(coin, 0.0)
        sigs = menu(close)
        refill = (pd.Series(close).pct_change(14 * 24).fillna(0).values > 0)
        n = len(df)
        bh = buy_and_hold(df, stk)

        wf_pnl, fix_pnl, bh_pnl = [], [], []
        s = 0
        while s + TRAIN_H + TEST_H <= n:
            tr_a, tr_b = s, s + TRAIN_H
            te_a, te_b = tr_b, tr_b + TEST_H
            # выбор сигнала на train по Calmar
            best, best_c = None, -1e9
            for name, sig in sigs.items():
                p, _ = sim_slice(df, stk, sig, refill, tr_a, tr_b)
                c = calmar(p)
                if c > best_c:
                    best_c, best = c, name
            picks[best] += 1
            # OOS на test
            p_wf, _ = sim_slice(df, stk, sigs[best], refill, te_a, te_b)
            p_fx, _ = sim_slice(df, stk, sigs["mom14|mom30"], refill, te_a, te_b)
            p_bh = buy_and_hold(df.iloc[te_a:te_b], stk)   # свежий BH на test-окне
            wf_pnl.append(p_wf); fix_pnl.append(p_fx); bh_pnl.append(p_bh)
            s += TEST_H

        wf_pnl_all[coin]  = np.concatenate(wf_pnl)
        fix_pnl_all[coin] = np.concatenate(fix_pnl)
        bh_pnl_all[coin]  = np.concatenate(bh_pnl)

    def agg(d):
        cs = [cagr(v) for v in d.values()]
        ds = [compute_metrics(v)["max_dd_pct"] for v in d.values()]
        return np.mean(cs), np.mean(ds), np.mean(cs) / np.mean(ds)

    print("=" * 90)
    print(f"WALK-FORWARD OOS — train={TRAIN_H//MONTH_H}мес test={TEST_H//MONTH_H}мес. "
          f"Портфель equal-weight, только test-окна.")
    print("=" * 90)
    for name, d in [("WF-select (OOS)", wf_pnl_all), ("fixed mom14|mom30", fix_pnl_all), ("buy & hold", bh_pnl_all)]:
        c, dd, cal = agg(d)
        print(f"  {name:20s}  CAGR={c:6.2f}%  avgDD={dd:6.2f}%  Calmar={cal:.2f}")

    print("\nЧастота выбора сигнала на train-окнах (стабильность):")
    for name, cnt in picks.most_common():
        print(f"  {name:14s} {cnt}")

    # per-coin OOS WF
    print("\nPer-coin OOS (WF-select):")
    for coin in wf_pnl_all:
        v = wf_pnl_all[coin]
        print(f"  {coin:5s} CAGR={cagr(v):6.2f}%  DD={compute_metrics(v)['max_dd_pct']:6.2f}%  Calmar={calmar(v):.2f}")

    out = Path(__file__).parent / "backtest_b_walkforward_results.csv"
    pd.DataFrame([{"coin": c, "wf_cagr": round(cagr(wf_pnl_all[c]), 2),
                   "wf_dd": round(compute_metrics(wf_pnl_all[c])["max_dd_pct"], 2),
                   "wf_calmar": round(calmar(wf_pnl_all[c]), 2)} for c in wf_pnl_all]).to_csv(out, index=False)
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    main()
