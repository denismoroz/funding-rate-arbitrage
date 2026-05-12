"""
Эксперимент А — дельта-нейтральный funding harvest.

Стратегия:
- Входим в шорт перп когда predicted funding rate > entry_threshold (годовых)
- Выходим когда funding rate < exit_threshold
- Лонг спот всегда компенсирует шорт — ценовой риск = 0
- P&L = сумма funding выплат минус комиссии на вход/выход

Упрощения:
- Используем исторический funding rate как реализованный (не predicted)
- Комиссия 0.035% за открытие + 0.035% за закрытие (taker)
- Нет ликвидаций (предполагаем достаточную маржу)
- Размер позиции = 1000 USDC notional
"""

import pandas as pd
import numpy as np
from pathlib import Path
from itertools import product

DATA_DIR = Path(__file__).parent / "data"
POSITION_SIZE = 1000  # USDC notional
TAKER_FEE = 0.00035  # 0.035%
HOURS_PER_YEAR = 8760


def load_funding(coin: str) -> pd.DataFrame:
    path = DATA_DIR / f"{coin}.csv"
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"], format="ISO8601", utc=True)
    df = df.set_index("time").sort_index()
    return df


def smooth_rates(rates: np.ndarray, window: int) -> np.ndarray:
    """Скользящее среднее для сигнала входа."""
    if window <= 1:
        return rates
    result = np.full_like(rates, np.nan)
    for i in range(len(rates)):
        start = max(0, i - window + 1)
        result[i] = rates[start:i+1].mean()
    return result


def run_backtest(
    df: pd.DataFrame,
    entry_threshold: float,
    exit_threshold: float,
    min_hold_hours: int = 0,
    cooldown_hours: int = 0,
    signal_window: int = 1,
) -> dict:
    """
    entry_threshold, exit_threshold — в долях годовых (0.20 = 20%).
    min_hold_hours  — минимум часов держать позицию после входа.
    cooldown_hours  — пауза после выхода перед следующим входом.
    signal_window   — MA окно для сигнала входа (часы).
    """
    rates = df["fundingRate"].values
    signal = smooth_rates(rates, signal_window) * HOURS_PER_YEAR  # годовые для входа

    in_position = False
    pnl = 0.0
    trades = 0
    hours_in_position = 0
    hours_since_entry = 0
    cooldown_left = 0

    for i, rate in enumerate(rates):
        annual_rate = rate * HOURS_PER_YEAR
        entry_signal = signal[i]

        if cooldown_left > 0:
            cooldown_left -= 1

        if not in_position:
            if cooldown_left == 0 and entry_signal > entry_threshold:
                in_position = True
                trades += 1
                hours_since_entry = 0
                pnl -= TAKER_FEE * POSITION_SIZE
        else:
            pnl += rate * POSITION_SIZE
            hours_in_position += 1
            hours_since_entry += 1

            can_exit = hours_since_entry >= min_hold_hours
            if can_exit and annual_rate < exit_threshold:
                in_position = False
                pnl -= TAKER_FEE * POSITION_SIZE
                cooldown_left = cooldown_hours

    if in_position:
        pnl -= TAKER_FEE * POSITION_SIZE
        trades += 1

    total_hours = len(rates)
    pct_in_position = hours_in_position / total_hours if total_hours > 0 else 0
    annualized_return = (pnl / POSITION_SIZE) / (total_hours / HOURS_PER_YEAR) * 100

    return {
        "pnl_usdc": round(pnl, 2),
        "annualized_pct": round(annualized_return, 2),
        "pct_in_position": round(pct_in_position * 100, 1),
        "trades": trades,
        "total_hours": total_hours,
    }


def main():
    coins = [
        "BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "MATIC",
        "DOGE", "LINK", "UNI", "AAVE", "WIF", "TIA", "INJ",
    ]

    # Базовые параметры (лучшие из предыдущего прогона)
    entry_threshold = 0.20
    exit_threshold = -0.05

    # Параметры фильтров
    min_hold_options   = [0, 24, 72, 168]       # 0, 1 день, 3 дня, 1 неделя
    cooldown_options   = [0, 24, 72, 168]       # то же
    signal_window_opts = [1, 6, 12, 24]         # 1ч (без MA), 6ч, 12ч, 24ч

    results = []

    for coin in coins:
        path = DATA_DIR / f"{coin}.csv"
        if not path.exists():
            print(f"Пропускаю {coin} — нет данных")
            continue

        df = load_funding(coin)
        print(f"{coin}: {len(df)} часов")

        # Базовый результат без фильтров
        base = run_backtest(df, entry_threshold, exit_threshold)
        results.append({"coin": coin, "filter": "baseline",
                        "param": 0, **base})

        # 1. Минимальное время в позиции
        for h in min_hold_options[1:]:
            res = run_backtest(df, entry_threshold, exit_threshold, min_hold_hours=h)
            results.append({"coin": coin, "filter": "min_hold_hours", "param": h, **res})

        # 2. Cooldown после выхода
        for h in cooldown_options[1:]:
            res = run_backtest(df, entry_threshold, exit_threshold, cooldown_hours=h)
            results.append({"coin": coin, "filter": "cooldown_hours", "param": h, **res})

        # 3. Сглаживание сигнала (MA)
        for w in signal_window_opts[1:]:
            res = run_backtest(df, entry_threshold, exit_threshold, signal_window=w)
            results.append({"coin": coin, "filter": "signal_ma", "param": w, **res})

    results_df = pd.DataFrame(results)

    for filter_name in ["min_hold_hours", "cooldown_hours", "signal_ma"]:
        print(f"\n{'='*80}")
        labels = {
            "min_hold_hours": "МИН. ВРЕМЯ В ПОЗИЦИИ (часы)",
            "cooldown_hours": "COOLDOWN ПОСЛЕ ВЫХОДА (часы)",
            "signal_ma":      "СГЛАЖИВАНИЕ СИГНАЛА MA (часы)",
        }
        print(f"{labels[filter_name]} vs baseline (entry=20%, exit=-5%)")
        print(f"{'='*80}")

        subset = results_df[results_df["filter"].isin(["baseline", filter_name])]
        summary = subset.groupby(["filter", "param"]).agg(
            avg_annual=("annualized_pct", "mean"),
            avg_in_pos=("pct_in_position", "mean"),
            avg_trades=("trades", "mean"),
        ).round(2)
        print(summary.to_string())

    # Сравнение по монетам: baseline vs лучший min_hold (72ч)
    print(f"\n{'='*80}")
    print("ПО МОНЕТАМ: baseline vs min_hold=72ч (entry=20%, exit=-5%)")
    print(f"{'='*80}")
    base = results_df[results_df["filter"] == "baseline"][
        ["coin", "annualized_pct", "pct_in_position", "trades", "pnl_usdc"]
    ].set_index("coin")
    hold72 = results_df[(results_df["filter"] == "min_hold_hours") & (results_df["param"] == 72)][
        ["coin", "annualized_pct", "pct_in_position", "trades", "pnl_usdc"]
    ].set_index("coin")

    compare = base.join(hold72, lsuffix="_base", rsuffix="_72h")
    compare["annual_diff"] = (compare["annualized_pct_72h"] - compare["annualized_pct_base"]).round(2)
    compare["trades_diff"] = compare["trades_72h"] - compare["trades_base"]
    compare = compare.sort_values("annualized_pct_72h", ascending=False)
    print(compare[[
        "annualized_pct_base", "trades_base",
        "annualized_pct_72h",  "trades_72h",
        "annual_diff", "trades_diff"
    ]].to_string())

    out_path = Path(__file__).parent / "backtest_a_results.csv"
    results_df.to_csv(out_path, index=False)
    print(f"\nПолные результаты: {out_path}")


if __name__ == "__main__":
    main()
