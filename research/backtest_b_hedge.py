"""
Стратегия Б "stake & hedge": спот держим всегда (стейкинг капает),
шортим перп когда price-сигнал говорит "будет падение".

Funding-условие на вход НЕ требуется. Funding пока хеджированы — побочный
доход (может быть и отриц). Декомпозиция результата: funding / hedge price /
spot price / staking / fees — чтобы понять откуда реально приходит yield.

Расширенная сетка сигналов (per-coin поиск лучшего):
  MA cross    — close < MA(50 / 100 / 200)
  Momentum    — N-д price return < 0, N in {3, 7, 14, 21, 30}
  Drawdown    — DD от пика > X%, X in {5, 8, 10, 12, 15, 20}
  Combo       — mom14d OR dd10
"""

import numpy as np
import pandas as pd
from pathlib import Path
from engine import (
    STAKING_YIELD, load_data, simulate, buy_and_hold, compute_metrics,
    HOURS_PER_YEAR, TOTAL_CAPITAL,
)

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]


def build_signal_grid(df: pd.DataFrame) -> dict:
    """Возвращает {name: bool[n]} для всех сигналов хеджа."""
    close = df["close"].values
    signals = {}

    # MA cross
    for w in (50, 100, 200):
        ma = pd.Series(close).rolling(w, min_periods=w).mean().values
        sig = (close < ma) & ~np.isnan(ma)
        signals[f"ma{w}"] = sig

    # Momentum N-дней
    for d in (3, 7, 14, 21, 30):
        h = d * 24
        mom = pd.Series(close).pct_change(h).fillna(0).values
        signals[f"mom{d}d"] = (mom < 0)

    # Drawdown от running max
    running_max = np.maximum.accumulate(close)
    dd = (running_max - close) / running_max
    for x in (5, 8, 10, 12, 15, 20):
        signals[f"dd{x}"] = (dd > x / 100)

    # Простой combo
    signals["combo_mom14_dd10"] = signals["mom14d"] | signals["dd10"]

    # Адаптивные: hedge только когда краткосроч. И долгосроч. оба вниз.
    # "long-term regime" — 90д и 180д price return < 0
    for short_d in (14, 21):
        for long_d in (90, 180):
            sh = signals[f"mom{short_d}d"]
            lh_h = long_d * 24
            long_mom = pd.Series(close).pct_change(lh_h).fillna(0).values
            lt_down = (long_mom < 0)
            signals[f"mom{short_d}d_lt{long_d}d"] = sh & lt_down

    return signals


def run_one(df, staking, sig_arr, years):
    """Возвращает (metrics, info, decomposition_pct)."""
    pnl, info = simulate(df, staking, "B_hedge", hedge_signal=sig_arr)
    m = compute_metrics(pnl)

    # Декомпозиция в % годовых относительно TOTAL_CAPITAL
    def pct(x): return round(x / TOTAL_CAPITAL / years * 100, 2)

    decomp = {
        "funding_apr":   pct(info["funding_total"]),
        "hedge_apr":     pct(info["short_realized_pnl"]),
        "spot_price_apr": pct(info["spot_price_pnl"]),
        "staking_apr":   pct(info["spot_staking_pnl"]),
        "fees_apr":      pct(-(info["perp_fees_total"] + info["spot_fees_total"])),
    }
    return m, info, decomp


def main():
    rows = []
    best_per_coin = []

    for coin in COINS:
        df = load_data(coin)
        if df.empty:
            print(f"  {coin}: нет данных, пропускаю")
            continue
        staking = STAKING_YIELD.get(coin, 0.0)
        years = len(df) / HOURS_PER_YEAR

        # Buy & hold benchmark
        bh_pnl = buy_and_hold(df, staking)
        bh_m = compute_metrics(bh_pnl)

        signals = build_signal_grid(df)
        coin_rows = []
        for sig_name, sig_arr in signals.items():
            m, info, decomp = run_one(df, staking, sig_arr, years)
            row = {
                "coin":         coin,
                "signal":       sig_name,
                "staking":      f"{staking*100:.1f}%",
                "annual_pct":   m["annual_pct"],
                "max_dd_pct":   m["max_dd_pct"],
                "calmar":       m["calmar"],
                "pct_hedged":   round(sig_arr.mean() * 100, 1),
                "trades":       info["trades"],
                **decomp,
                "bh_annual":    bh_m["annual_pct"],
                "bh_max_dd":    bh_m["max_dd_pct"],
            }
            rows.append(row)
            coin_rows.append(row)

        # Лучший сигнал по Calmar для этой монеты
        coin_df = pd.DataFrame(coin_rows)
        best_by_calmar = coin_df.loc[coin_df["calmar"].idxmax()]
        best_by_annual = coin_df.loc[coin_df["annual_pct"].idxmax()]
        best_per_coin.append({
            "coin":            coin,
            "best_calmar_sig": best_by_calmar["signal"],
            "best_calmar":     best_by_calmar["calmar"],
            "best_calmar_apr": best_by_calmar["annual_pct"],
            "best_calmar_dd":  best_by_calmar["max_dd_pct"],
            "best_annual_sig": best_by_annual["signal"],
            "best_annual_apr": best_by_annual["annual_pct"],
            "best_annual_dd":  best_by_annual["max_dd_pct"],
            "bh_annual":       bh_m["annual_pct"],
            "bh_max_dd":       bh_m["max_dd_pct"],
        })

    df_res = pd.DataFrame(rows)
    df_best = pd.DataFrame(best_per_coin)

    print("\n" + "="*120)
    print("Б_hedge — лучший сигнал на КАЖДУЮ монету (per-coin optimization)")
    print("="*120)
    print(df_best.to_string(index=False))

    print("\n" + "="*120)
    print("Декомпозиция доходности для лучшего сигнала по каждой монете (по Calmar)")
    print("="*120)
    decomp_rows = []
    for _, b in df_best.iterrows():
        row = df_res[(df_res["coin"] == b["coin"]) & (df_res["signal"] == b["best_calmar_sig"])].iloc[0]
        decomp_rows.append({
            "coin":         b["coin"],
            "signal":       b["best_calmar_sig"],
            "staking":      row["staking"],
            "annual_pct":   row["annual_pct"],
            "funding_apr":  row["funding_apr"],
            "hedge_apr":    row["hedge_apr"],
            "spot_apr":     row["spot_price_apr"],
            "staking_apr":  row["staking_apr"],
            "fees_apr":     row["fees_apr"],
            "max_dd":       row["max_dd_pct"],
            "calmar":       row["calmar"],
            "bh_annual":    row["bh_annual"],
        })
    df_decomp = pd.DataFrame(decomp_rows)
    print(df_decomp.to_string(index=False))

    print("\n" + "="*120)
    print("Топ-3 сигнала на каждую монету (по Calmar)")
    print("="*120)
    for coin in df_res["coin"].unique():
        coin_df = df_res[df_res["coin"] == coin].sort_values("calmar", ascending=False).head(3)
        cols = ["signal", "annual_pct", "max_dd_pct", "calmar", "pct_hedged",
                "funding_apr", "hedge_apr", "spot_price_apr", "fees_apr"]
        print(f"\n{coin}:  buy&hold = {coin_df.iloc[0]['bh_annual']:.1f}% / DD {coin_df.iloc[0]['bh_max_dd']:.1f}%")
        print(coin_df[cols].to_string(index=False))

    # Portfolio-level: применить ОДИН сигнал ко всем монетам равномерно
    print("\n" + "="*120)
    print("Универсальный сигнал на ВСЕ монеты (равно-взвешенный портфель)")
    print("="*120)
    portfolio_rows = []
    sig_names = df_res["signal"].unique()
    for sig_name in sig_names:
        sub = df_res[df_res["signal"] == sig_name]
        avg_annual = sub["annual_pct"].mean()
        avg_dd     = sub["max_dd_pct"].mean()
        avg_calmar = avg_annual / avg_dd if avg_dd > 0 else 0
        portfolio_rows.append({
            "signal":         sig_name,
            "avg_annual":     round(avg_annual, 2),
            "avg_max_dd":     round(avg_dd, 2),
            "calmar":         round(avg_calmar, 2),
            "avg_funding":    round(sub["funding_apr"].mean(), 2),
            "avg_hedge_pnl":  round(sub["hedge_apr"].mean(), 2),
            "avg_fees":       round(sub["fees_apr"].mean(), 2),
        })
    df_port = pd.DataFrame(portfolio_rows).sort_values("calmar", ascending=False)
    bh_avg = df_best["bh_annual"].mean()
    bh_dd  = df_best["bh_max_dd"].mean()
    print(df_port.to_string(index=False))
    print(f"\nbuy & hold всех монет: annual={bh_avg:.2f}%, max_dd={bh_dd:.2f}%, calmar={bh_avg/bh_dd:.2f}")

    out = Path(__file__).parent / "backtest_b_hedge_results.csv"
    df_res.to_csv(out, index=False)
    out_best = Path(__file__).parent / "backtest_b_hedge_best.csv"
    df_best.to_csv(out_best, index=False)
    out_port = Path(__file__).parent / "backtest_b_hedge_portfolio.csv"
    df_port.to_csv(out_port, index=False)
    print(f"\nСохранено: {out}, {out_best}, {out_port}")


if __name__ == "__main__":
    main()
