"""
Position concurrency cap для Стратегии A_cycle.

Идея: ограничить число одновременных позиций до K из N монет.
На каждом часу из кандидатов на вход выбираем top-K по силе funding сигнала.

Должно срезать peak capital с N×2000 до K×2000 без существенной потери доходности
(потому что мы и так выбираем лучшие сигналы).
"""

import numpy as np
import pandas as pd
from pathlib import Path
from engine import (
    load_data, compute_metrics,
    POSITION_SIZE, TOTAL_CAPITAL, PERP_TAKER, SPOT_TAKER,
    HOURS_PER_YEAR, ENTRY_THRESHOLD, EXIT_THRESHOLD, MIN_HOLD_HOURS,
    STAKING_YIELD,
)


def simulate_multi_capped(
    coins,
    max_concurrent: int,
    entry_threshold: float = ENTRY_THRESHOLD,
    exit_threshold:  float = EXIT_THRESHOLD,
    min_hold:        int   = MIN_HOLD_HOURS,
    signal_window:   int   = 1,
):
    """
    Симулирует A_cycle сразу по всем монетам с ограничением на конкурентные позиции.

    Возвращает (pnl_total, capital_total, info).
    """
    # Загрузка данных и выравнивание
    datas = {}
    for c in coins:
        df = load_data(c)
        if df.empty:
            continue
        datas[c] = df

    # Общий временной индекс
    common_idx = sorted(set().union(*[set(df.index) for df in datas.values()]))
    common_idx = pd.DatetimeIndex(common_idx)
    n = len(common_idx)

    # Подготовка массивов для каждой монеты
    state = {}
    for c, df in datas.items():
        df2 = df.reindex(common_idx)
        rates = df2["fundingRate"].values
        close = df2["close"].values
        # Сигнал входа (сглаженный)
        if signal_window > 1:
            sig = pd.Series(rates).rolling(signal_window, min_periods=1).mean().values * HOURS_PER_YEAR
        else:
            sig = rates * HOURS_PER_YEAR
        state[c] = {
            "rates": rates,
            "close": close,
            "signal": sig,
            "valid": ~np.isnan(close) & ~np.isnan(rates),
            "in_position":  False,
            "short_size":   0.0,
            "units_spot":   0.0,
            "entry_price":  0.0,
            "hours_since":  0,
            "cash":         TOTAL_CAPITAL,   # стартовый капитал для этой монеты
            "equity_prev":  TOTAL_CAPITAL,   # baseline equity
            "trades":       0,
            "hours_in":     0,
        }

    pnl_per_hour = np.zeros(n)
    cap_per_hour = np.zeros(n)

    for i in range(n):
        # 1) Funding для всех уже-в-позиции
        for c, s in state.items():
            if not s["valid"][i]:
                continue
            if s["in_position"]:
                P = s["close"][i]
                r = s["rates"][i]
                s["cash"] += s["short_size"] * P * r

        # 2) Exit для in-position если выполняются условия
        for c, s in state.items():
            if not s["valid"][i] or not s["in_position"]:
                continue
            s["hours_since"] += 1
            s["hours_in"]    += 1
            ar = s["rates"][i] * HOURS_PER_YEAR
            if s["hours_since"] >= min_hold and ar < exit_threshold:
                P = s["close"][i]
                # close short: cash получает реализованный PnL
                s["cash"] += s["short_size"] * (s["entry_price"] - P)
                s["cash"] -= s["short_size"] * P * PERP_TAKER
                # sell spot (A_cycle): cash получает выручку, теряем units_spot
                s["cash"] += s["units_spot"] * P
                s["cash"] -= s["units_spot"] * P * SPOT_TAKER
                s["short_size"] = 0.0
                s["units_spot"] = 0.0
                s["entry_price"] = 0.0
                s["in_position"] = False

        # 3) Подсчёт текущих in-position
        active = [c for c, s in state.items() if s["in_position"]]
        slots_free = max_concurrent - len(active)

        # 4) Кандидаты на вход — берём top-K по signal
        if slots_free > 0:
            candidates = []
            for c, s in state.items():
                if not s["valid"][i] or s["in_position"]:
                    continue
                if s["signal"][i] > entry_threshold:
                    candidates.append((c, s["signal"][i]))
            candidates.sort(key=lambda x: -x[1])  # сильнейший первым
            for c, _ in candidates[:slots_free]:
                s = state[c]
                P = s["close"][i]
                # Купить spot: тратим cash, получаем units
                s["units_spot"]  = POSITION_SIZE / P
                s["cash"]       -= POSITION_SIZE
                s["cash"]       -= POSITION_SIZE * SPOT_TAKER
                # Открыть short
                s["short_size"] = POSITION_SIZE / P
                s["entry_price"]= P
                s["cash"]      -= POSITION_SIZE * PERP_TAKER
                s["in_position"]= True
                s["hours_since"]= 0
                s["trades"]    += 1

        # 5) MTM equity per coin = cash + units_spot * P + short_pnl
        hour_pnl  = 0.0
        hour_cap  = 0.0
        for c, s in state.items():
            if not s["valid"][i]:
                continue
            P = s["close"][i]
            short_pnl = s["short_size"] * (s["entry_price"] - P) if s["in_position"] else 0.0
            equity_now = s["cash"] + s["units_spot"] * P + short_pnl
            hour_pnl += equity_now - s["equity_prev"]
            s["equity_prev"] = equity_now
            if s["in_position"]:
                hour_cap += TOTAL_CAPITAL
        pnl_per_hour[i] = hour_pnl
        cap_per_hour[i] = hour_cap

    # Финал: закрыть всё ещё открытое
    for c, s in state.items():
        if not s["in_position"]:
            continue
        valid_close = s["close"][s["valid"]]
        if len(valid_close) == 0:
            continue
        P = valid_close[-1]
        # close short
        s["cash"] += s["short_size"] * (s["entry_price"] - P)
        s["cash"] -= s["short_size"] * P * PERP_TAKER
        # sell spot
        s["cash"] += s["units_spot"] * P
        s["cash"] -= s["units_spot"] * P * SPOT_TAKER
        s["short_size"] = 0.0
        s["units_spot"] = 0.0
        equity_now = s["cash"]
        pnl_per_hour[-1] += equity_now - s["equity_prev"]
        s["equity_prev"] = equity_now

    info = {
        "trades_per_coin":   {c: state[c]["trades"] for c in state},
        "total_trades":      sum(s["trades"] for s in state.values()),
        "peak_capital":      cap_per_hour.max(),
        "avg_capital_when_active": cap_per_hour[cap_per_hour > 0].mean() if (cap_per_hour > 0).any() else 0,
    }
    return pnl_per_hour, cap_per_hour, info


def metrics_on_capital(pnl_arr, capital_base, n_hours):
    total_pnl = pnl_arr.sum()
    annualized = (total_pnl / capital_base) / (n_hours / HOURS_PER_YEAR) * 100
    equity = np.cumsum(pnl_arr)
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / (capital_base + peak)
    max_dd = abs(dd.min()) * 100
    hr = pnl_arr / capital_base
    sharpe = (hr.mean() / hr.std() * np.sqrt(HOURS_PER_YEAR)) if hr.std() > 0 else 0
    calmar = annualized / max_dd if max_dd > 0 else 0
    return {
        "annual": round(annualized, 2),
        "max_dd": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "calmar": round(calmar, 1),
    }


def main():
    coins = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
    n_coins = len(coins)

    print("="*100)
    print(f"POSITION CONCURRENCY CAP на {n_coins} монетах ({coins})")
    print(f"Naive (без cap): peak = {n_coins} × $2000 = ${n_coins*2000}")
    print("="*100)

    print("\n--- Базовые параметры (entry=20%, exit=-5%, min_hold=72) ---")
    rows = []
    for K in [1, 2, 3, 4, 5, 6, 7]:
        pnl, cap, info = simulate_multi_capped(coins, K,
            entry_threshold=0.20, exit_threshold=-0.05, min_hold=72)
        n = len(pnl)
        # peak capital для этой стратегии
        peak = info["peak_capital"]
        avg_active = info["avg_capital_when_active"]
        # доходность на peak капитал (реалистично — это что надо иметь на счёте)
        m_peak = metrics_on_capital(pnl, peak, n) if peak > 0 else None
        # доходность на K * $2000 (theoretical max при ограничении K)
        cap_theory = K * TOTAL_CAPITAL
        m_theory = metrics_on_capital(pnl, cap_theory, n)
        rows.append({
            "max_K":         K,
            "peak_$":        int(peak),
            "avg_active_$":  int(avg_active),
            "annual_peak":   m_peak["annual"] if m_peak else 0,
            "calmar_peak":   m_peak["calmar"] if m_peak else 0,
            "annual_theory": m_theory["annual"],
            "max_dd_theory": m_theory["max_dd"],
            "calmar_theory": m_theory["calmar"],
            "trades":        info["total_trades"],
        })
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n--- COMBO (entry=30%, exit=-15%, min_hold=120, sig_ma=12) ---")
    rows_combo = []
    for K in [1, 2, 3, 4, 5, 7]:
        pnl, cap, info = simulate_multi_capped(coins, K,
            entry_threshold=0.30, exit_threshold=-0.15, min_hold=120, signal_window=12)
        n = len(pnl)
        peak = info["peak_capital"]
        m_theory = metrics_on_capital(pnl, K * TOTAL_CAPITAL, n)
        m_peak = metrics_on_capital(pnl, peak, n) if peak > 0 else None
        rows_combo.append({
            "params":        "combo",
            "max_K":         K,
            "peak_$":        int(peak),
            "annual_theory": m_theory["annual"],
            "max_dd_theory": m_theory["max_dd"],
            "calmar_theory": m_theory["calmar"],
            "annual_peak":   m_peak["annual"] if m_peak else 0,
            "calmar_peak":   m_peak["calmar"] if m_peak else 0,
            "trades":        info["total_trades"],
        })
    print(pd.DataFrame(rows_combo).to_string(index=False))

    # Сохранить результаты
    out = Path(__file__).parent / "concurrency_cap_results.csv"
    all_rows = [{**r, "params": "baseline"} for r in rows] + rows_combo
    pd.DataFrame(all_rows).to_csv(out, index=False)
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    main()
