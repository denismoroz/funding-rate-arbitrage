"""
Тестируем early-exit (stop-loss) для Стратегии А.

Идея: выходим из позиции даже до истечения min_hold если
кумулятивный нереализованный PnL этой сделки опустился ниже -$X.
"""

import numpy as np
import pandas as pd
from engine import (
    load_data, compute_metrics, STAKING_YIELD,
    POSITION_SIZE, TOTAL_CAPITAL, PERP_TAKER, SPOT_TAKER,
    HOURS_PER_YEAR,
)


def simulate_with_stoploss(df, entry_threshold, exit_threshold, min_hold,
                            stop_loss_usdc=None, signal_window=1):
    """Стратегия A_cycle с опциональным stop-loss по нереализованному PnL сделки."""
    close = df["close"].values
    rates = df["fundingRate"].values

    if signal_window > 1:
        sig = pd.Series(rates).rolling(signal_window, min_periods=1).mean().values * HOURS_PER_YEAR
    else:
        sig = rates * HOURS_PER_YEAR

    n = len(df)
    pnl_arr = np.zeros(n)
    trades = 0
    stop_outs = 0  # сколько раз сработал stop-loss
    in_pos = False
    short_size = 0.0
    units_spot = 0.0
    entry_price = 0.0
    hours_since = 0
    trade_start_cash = 0.0  # cash на момент входа в сделку

    cash = TOTAL_CAPITAL
    equity_prev = TOTAL_CAPITAL

    for i in range(n):
        P = close[i]
        rate = rates[i]
        annual_rate = rate * HOURS_PER_YEAR

        # funding
        if in_pos:
            cash += short_size * P * rate

        # ВХОД
        if not in_pos:
            if sig[i] > entry_threshold:
                units_spot = POSITION_SIZE / P
                cash -= POSITION_SIZE
                cash -= POSITION_SIZE * SPOT_TAKER
                short_size = POSITION_SIZE / P
                entry_price = P
                cash -= POSITION_SIZE * PERP_TAKER
                in_pos = True
                hours_since = 0
                trades += 1
                # снимок cash на момент входа (для подсчёта trade PnL)
                trade_start_cash = cash
        else:
            hours_since += 1

            # Вычисляем текущий PnL по этой сделке (нереализованный)
            # spot_pnl = units * (P - entry) ≈ -short_pnl, перекрываются
            # реальный PnL сделки = (cash - trade_start_cash) + funding_received
            # но funding уже в cash, спот/перп компенсируют → trade PnL = cash - trade_start_cash
            trade_pnl = cash - trade_start_cash

            # Условие выхода:
            normal_exit = hours_since >= min_hold and annual_rate < exit_threshold
            stop_hit    = stop_loss_usdc is not None and trade_pnl < -stop_loss_usdc

            if normal_exit or stop_hit:
                cash += short_size * (entry_price - P)
                cash -= short_size * P * PERP_TAKER
                cash += units_spot * P
                cash -= units_spot * P * SPOT_TAKER
                short_size = 0.0
                units_spot = 0.0
                entry_price = 0.0
                in_pos = False
                if stop_hit and not normal_exit:
                    stop_outs += 1

        short_pnl = short_size * (entry_price - P) if in_pos else 0.0
        equity_now = cash + units_spot * P + short_pnl
        pnl_arr[i] = equity_now - equity_prev
        equity_prev = equity_now

    # финальное закрытие
    if in_pos:
        P = close[-1]
        cash += short_size * (entry_price - P)
        cash -= short_size * P * PERP_TAKER
        cash += units_spot * P
        cash -= units_spot * P * SPOT_TAKER
        pnl_arr[-1] += cash - equity_prev

    return pnl_arr, {"trades": trades, "stop_outs": stop_outs}


def main():
    coins = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]

    param_sets = [
        ("COMBO (30/-15/120/sig12)",   0.30, -0.15, 120, 12),
        ("Baseline (20/-5/72)",         0.20, -0.05, 72,  1),
        ("Aggressive (20/-5/24)",       0.20, -0.05, 24,  1),
    ]

    for label, entry, exit_, hold, sig_w in param_sets:
        print("\n" + "="*100)
        print(f"STOP-LOSS под {label}")
        print("="*100)

        configs = [
            ("без stop-loss",       None),
            ("stop -$0.5",          0.5),
            ("stop -$1",            1),
            ("stop -$2",            2),
            ("stop -$5",            5),
            ("stop -$10",           10),
        ]
        rows = []
        for sl_label, sl in configs:
            ann_list, dd_list, cal_list, trade_list, stop_list = [], [], [], [], []
            for coin in coins:
                df = load_data(coin)
                if df.empty: continue
                pnl, info = simulate_with_stoploss(df, entry, exit_, hold,
                                                    stop_loss_usdc=sl, signal_window=sig_w)
                m = compute_metrics(pnl)
                ann_list.append(m["annual_pct"])
                dd_list.append(m["max_dd_pct"])
                cal_list.append(m["calmar"])
                trade_list.append(info["trades"])
                stop_list.append(info["stop_outs"])
            rows.append({
                "config":      sl_label,
                "annual_avg":  round(np.mean(ann_list), 2),
                "max_dd_avg":  round(np.mean(dd_list), 3),
                "calmar_avg":  round(np.mean(cal_list), 1),
                "trades":      sum(trade_list),
                "stop_outs":   sum(stop_list),
                "stop_pct":    round(sum(stop_list) / sum(trade_list) * 100, 1) if sum(trade_list) else 0,
            })
        print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
