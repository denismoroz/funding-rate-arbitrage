"""
Strategy B — RANDOM-SIGNAL NULL TEST (circular-shift permutation).

Вопрос: есть ли у сигнала mom14d реальный timing-эдж, или хедж зарабатывает
просто как short-beta (повезло, что монеты падали)?

Метод: берём РЕАЛЬНЫЙ hedge-сигнал и циклически сдвигаем его на случайный
лаг k. Сдвиг сохраняет ВСЁ — число сделок, длины эпизодов, время-в-рынке,
автокорреляцию сигнала — ломает только ПРИВЯЗКУ к движению цены.

Если реальный сигнал бьёт распределение случайных сдвигов по hedge_pnl/CAGR
=> тайминг настоящий. Если сидит в середине => эджа нет, это short-beta.

p_value = доля случайных сдвигов с метрикой >= реальной (one-sided).
Условия как в honest-прогоне: lag=1, slippage=5bps, cash=4%.
"""

import numpy as np
import pandas as pd
from pathlib import Path
from engine import STAKING_YIELD, load_data, compute_metrics, HOURS_PER_YEAR, TOTAL_CAPITAL
from backtest_b_constdollar import simulate_constdollar, build_mom14d, build_trend_up

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]
N_TRIALS = 1000
CASH_APR, SLIPPAGE, SIG_LAG = 0.04, 0.0005, 1
SEED = 42


def cagr(pnl):
    years = len(pnl) / HOURS_PER_YEAR
    end = TOTAL_CAPITAL + float(np.sum(pnl))
    return (((end / TOTAL_CAPITAL) ** (1 / years) - 1) * 100) if end > 0 else -100.0


def run_one(df, staking, hedge, trend_up):
    pnl, info = simulate_constdollar(
        df, staking, hedge, risk_free_apr=CASH_APR,
        refill_confirm=trend_up, signal_lag=SIG_LAG, slippage=SLIPPAGE,
    )
    years = len(pnl) / HOURS_PER_YEAR
    return {
        "cagr":      cagr(pnl),
        "hedge_pnl": info["short_realized_pnl"] / TOTAL_CAPITAL / years * 100,
        "calmar":    cagr(pnl) / compute_metrics(pnl)["max_dd_pct"],
    }


def main():
    rng = np.random.default_rng(SEED)
    rows = []
    for coin in COINS:
        df = load_data(coin)
        if df.empty:
            continue
        staking = STAKING_YIELD.get(coin, 0.0)
        close = df["close"].values
        n = len(close)
        hedge = build_mom14d(close)
        trend_up = build_trend_up(close)

        real = run_one(df, staking, hedge, trend_up)

        # NULL: циклический сдвиг hedge-сигнала на случайный k (trend_up для
        # refill оставляем реальным — рандомизируем только timing хеджа)
        rand = {"cagr": [], "hedge_pnl": [], "calmar": []}
        shifts = rng.integers(int(0.05 * n), int(0.95 * n), size=N_TRIALS)
        for k in shifts:
            r = run_one(df, staking, np.roll(hedge, int(k)), trend_up)
            for key in rand:
                rand[key].append(r[key])

        row = {"coin": coin}
        for key in ("hedge_pnl", "cagr", "calmar"):
            arr = np.array(rand[key])
            p = float(np.mean(arr >= real[key]))      # one-sided p-value
            z = (real[key] - arr.mean()) / (arr.std() + 1e-9)
            row[f"real_{key}"]   = round(real[key], 2)
            row[f"null_mean_{key}"] = round(arr.mean(), 2)
            row[f"null_p95_{key}"]  = round(float(np.percentile(arr, 95)), 2)
            row[f"p_{key}"] = round(p, 3)
            row[f"z_{key}"] = round(z, 2)
        rows.append(row)
        print(f"{coin:5s}  hedge_pnl real={row['real_hedge_pnl']:6.2f} "
              f"null_mean={row['null_mean_hedge_pnl']:6.2f} p={row['p_hedge_pnl']:.3f} z={row['z_hedge_pnl']:5.2f}  | "
              f"CAGR real={row['real_cagr']:6.2f} null_mean={row['null_mean_cagr']:6.2f} p={row['p_cagr']:.3f}")

    res = pd.DataFrame(rows)
    out = Path(__file__).parent / "backtest_b_nulltest_results.csv"
    res.to_csv(out, index=False)

    print("\n" + "=" * 100)
    print(f"NULL TEST ({N_TRIALS} circular shifts). p = доля случайных сдвигов >= реального.")
    print("p<=0.05 => тайминг есть; p~0.5 => эджа нет (short-beta); p>0.95 => сигнал ХУЖЕ случайного.")
    print("=" * 100)
    print(f"\nСреднее p по монетам:  hedge_pnl={res['p_hedge_pnl'].mean():.3f}  "
          f"CAGR={res['p_cagr'].mean():.3f}  calmar={res['p_calmar'].mean():.3f}")
    print(f"Монет с p<=0.05 (hedge_pnl): {(res['p_hedge_pnl']<=0.05).sum()}/{len(res)}  "
          f"| с p<=0.05 (CAGR): {(res['p_cagr']<=0.05).sum()}/{len(res)}")
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    main()
