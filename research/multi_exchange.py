"""
Сравнение Стратегии А на трёх биржах: Hyperliquid, Binance, Bybit.

Особенности:
  - HL: funding каждый час
  - Binance: funding каждые 8 часов
  - Bybit: funding каждые 8 часов

Поэтому каждое "iteration" симулятора = один funding period (1h или 8h).
min_hold задаём в ЧАСАХ — внутри конвертируем в количество периодов.

Комиссии taker (свежие данные с сайтов бирж, без VIP скидок):
  HL:      spot 0.07%, perp 0.035%
  Binance: spot 0.10%, perp 0.05%
  Bybit:   spot 0.10%, perp 0.055%
"""

import numpy as np
import pandas as pd
from pathlib import Path

DIR_HL      = Path(__file__).parent / "data"
DIR_BINANCE = Path(__file__).parent / "data_binance"
DIR_BYBIT   = Path(__file__).parent / "data_bybit"
DIR_DRIFT   = Path(__file__).parent / "data_drift"

POSITION_SIZE  = 1000
TOTAL_CAPITAL  = POSITION_SIZE * 2
HOURS_PER_YEAR = 8760

# Конфиги бирж: (data_dir, funding_interval_hours, spot_taker, perp_taker)
# Drift: spot есть только для SOL/USDC, для других монет — нужно идти за спотом на Jupiter.
# Используем те же fees как на HL для перпа (Drift Pro tier).
# Drift Pro perp taker = 0.05% (без скидок); spot — берём 0.07% как HL.
EXCHANGES = {
    "Hyperliquid": (DIR_HL,      1,  0.00070, 0.00035),
    "Binance":     (DIR_BINANCE, 8,  0.00100, 0.00050),
    "Bybit":       (DIR_BYBIT,   8,  0.00100, 0.00055),
    "Drift":       (DIR_DRIFT,   1,  0.00070, 0.00050),
}

COINS = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]


def load_funding(data_dir, coin, funding_interval_h):
    """Загружает funding history. Для HL отрезает первую неделю (8h funding period)."""
    path = data_dir / f"{coin}.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"], format="ISO8601", utc=True)
    df = df[["time", "fundingRate"]].sort_values("time").drop_duplicates("time").reset_index(drop=True)
    # Hyperliquid: первую неделю отрезаем (8h interval не подходит к 1h модели)
    if funding_interval_h == 1:
        df = df[df["time"] >= pd.Timestamp("2023-06-08", tz="UTC")].reset_index(drop=True)
    return df


def simulate_funding_only(
    rates: np.ndarray,
    funding_interval_h: int,
    spot_taker: float,
    perp_taker: float,
    entry_threshold: float = 0.30,
    exit_threshold:  float = -0.15,
    min_hold_hours:  int   = 120,
    signal_window_h: int   = 12,
):
    """
    Упрощённая funding-only симуляция A_cycle (без спот цены — delta-neutral по конструкции).

    Каждая итерация = funding period (1h на HL, 8h на Binance/Bybit).
    """
    periods_per_year = HOURS_PER_YEAR // funding_interval_h
    min_hold_periods = max(1, min_hold_hours // funding_interval_h)
    sig_w_periods    = max(1, signal_window_h // funding_interval_h)

    if sig_w_periods > 1:
        signal = pd.Series(rates).rolling(sig_w_periods, min_periods=1).mean().values * periods_per_year
    else:
        signal = rates * periods_per_year

    n = len(rates)
    cum_pnl = 0.0
    pnl_per_period = np.zeros(n)
    trades = 0
    periods_in_pos = 0
    in_pos = False
    periods_since = 0

    for i, rate in enumerate(rates):
        annual = rate * periods_per_year

        if in_pos:
            # Funding received на $1000 notional
            cum_pnl += POSITION_SIZE * rate
            periods_in_pos += 1
            periods_since += 1
            # Exit
            if periods_since >= min_hold_periods and annual < exit_threshold:
                # Закрытие: spot sell fee + perp close fee
                cum_pnl -= POSITION_SIZE * spot_taker
                cum_pnl -= POSITION_SIZE * perp_taker
                in_pos = False
        else:
            if signal[i] > entry_threshold:
                cum_pnl -= POSITION_SIZE * spot_taker  # buy
                cum_pnl -= POSITION_SIZE * perp_taker  # short open
                in_pos = True
                periods_since = 0
                trades += 1

        pnl_per_period[i] = cum_pnl - (pnl_per_period[:i].sum() if i > 0 else 0)

    # Финал
    if in_pos:
        cum_pnl -= POSITION_SIZE * spot_taker
        cum_pnl -= POSITION_SIZE * perp_taker

    # Реконструкция инкрементального PnL
    pnl_diffs = np.diff(np.concatenate([[0], np.cumsum(pnl_per_period)]))

    return {
        "total_pnl":     cum_pnl,
        "trades":        trades,
        "periods":       n,
        "periods_in":    periods_in_pos,
        "hours":         n * funding_interval_h,
        "pct_in_pos":    round(periods_in_pos / n * 100, 1) if n > 0 else 0,
    }


def main():
    print("="*100)
    print("СРАВНЕНИЕ A_cycle на 3-х биржах (entry=30%, exit=-15%, min_hold=120ч, sig_ma=12ч)")
    print(f"POSITION_SIZE=${POSITION_SIZE}, TOTAL_CAPITAL=${TOTAL_CAPITAL}")
    print("="*100)

    results = []
    for ex_name, (data_dir, interval_h, spot_fee, perp_fee) in EXCHANGES.items():
        for coin in COINS:
            df = load_funding(data_dir, coin, interval_h)
            if df is None or len(df) < 100:
                results.append({"exchange": ex_name, "coin": coin,
                                "annual": "no data", "trades": 0})
                continue
            rates = df["fundingRate"].values
            res = simulate_funding_only(
                rates, interval_h, spot_fee, perp_fee,
                entry_threshold=0.30, exit_threshold=-0.15,
                min_hold_hours=120, signal_window_h=12,
            )
            years = res["hours"] / HOURS_PER_YEAR
            annual_pct = res["total_pnl"] / TOTAL_CAPITAL / years * 100 if years > 0 else 0
            results.append({
                "exchange":   ex_name,
                "coin":       coin,
                "annual_%":   round(annual_pct, 2),
                "trades":     res["trades"],
                "pct_in_pos": res["pct_in_pos"],
                "periods":    res["periods"],
                "years_data": round(years, 1),
                "interval_h": interval_h,
            })

    df_res = pd.DataFrame(results)
    print("\n=== По монете × биржам ===")
    # Pivot для удобства
    pivot = df_res.pivot(index="coin", columns="exchange", values="annual_%")
    cols_order = [e for e in ["Hyperliquid", "Drift", "Binance", "Bybit"] if e in pivot.columns]
    pivot = pivot[cols_order]
    print(pivot.to_string())

    print("\n=== Среднее по корзине ===")
    avg = df_res.groupby("exchange").agg(
        annual_avg=("annual_%", lambda x: round(np.mean([v for v in x if isinstance(v, (int, float))]), 2)),
        trades_avg=("trades", "mean"),
        pct_in_pos_avg=("pct_in_pos", "mean"),
        years_avg=("years_data", "mean"),
    ).reindex(cols_order)
    print(avg.to_string())

    print("\n=== Детально по бирже × монете ===")
    print(df_res.to_string(index=False))

    df_res.to_csv(Path(__file__).parent / "multi_exchange_results.csv", index=False)


if __name__ == "__main__":
    main()
