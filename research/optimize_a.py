"""
Оптимизация Стратегии А:
  1. Мульти-монетная симуляция с cross-margin (peak/avg-capital учёт)
  2. Funding momentum signal — вход только когда funding >= среднего за окно
"""

import numpy as np
import pandas as pd
from pathlib import Path
from engine import (
    load_data, simulate, compute_metrics, STAKING_YIELD,
    POSITION_SIZE, TOTAL_CAPITAL, HOURS_PER_YEAR,
)


def multi_coin(coins, strategy, **kwargs):
    """Симулирует каждую монету независимо, агрегирует PnL и капитал по времени."""
    pnl_series = []
    cap_series = []
    for coin in coins:
        df = load_data(coin)
        if df.empty:
            continue
        st = STAKING_YIELD.get(coin, 0.0)
        pnl, info = simulate(df, st, strategy, track_capital=True, **kwargs)
        pnl_series.append(pd.Series(pnl, index=df.index, name=coin))
        cap_series.append(pd.Series(info["capital_arr"], index=df.index, name=coin))

    pnl_df = pd.concat(pnl_series, axis=1).fillna(0)
    cap_df = pd.concat(cap_series, axis=1).fillna(0)
    return pnl_df, cap_df


def metrics_on_capital(pnl_arr, capital_base, n_hours):
    """Считает метрики относительно фиксированного капитала."""
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


def run_multi(coins, strategy, label, **kwargs):
    pnl_df, cap_df = multi_coin(coins, strategy, **kwargs)
    total_pnl = pnl_df.sum(axis=1).values
    total_cap = cap_df.sum(axis=1).values
    n = len(total_pnl)

    peak = total_cap.max()
    avg_when_active = total_cap[total_cap > 0].mean() if (total_cap > 0).any() else 0
    naive = len(coins) * TOTAL_CAPITAL  # 7 × 2000 = 14000

    m_peak = metrics_on_capital(total_pnl, peak, n)
    return {
        "label":           label,
        "peak_$":          int(peak),
        "peak_%_of_naive": round(peak / naive * 100, 1),
        "annual_on_peak":  m_peak["annual"],
        "max_dd_peak":     m_peak["max_dd"],
        "calmar_peak":     m_peak["calmar"],
    }


def main():
    coins = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
    naive_capital = len(coins) * TOTAL_CAPITAL

    # ─── 1. Cross-margin capital efficiency ───────────────────────────────────
    print("="*100)
    print(f"ЭКСПЕРИМЕНТ 1: CROSS-MARGIN на {len(coins)} монетах")
    print(f"Naive capital (по {TOTAL_CAPITAL}$ на монету): ${naive_capital}")
    print("="*100)

    for strat in ["A_cycle", "A_spot_keep"]:
        pnl_df, cap_df = multi_coin(coins, strat,
                                    entry_threshold=0.20, exit_threshold=-0.05, min_hold=72)
        total_pnl = pnl_df.sum(axis=1).values
        total_cap = cap_df.sum(axis=1).values
        n = len(total_pnl)
        peak = total_cap.max()
        avg_active = total_cap[total_cap > 0].mean()

        m_naive = metrics_on_capital(total_pnl, naive_capital, n)
        m_peak  = metrics_on_capital(total_pnl, peak, n)
        m_avg   = metrics_on_capital(total_pnl, avg_active, n)

        print(f"\n--- {strat} ---")
        print(f"  Peak capital в работе:       ${peak:>7.0f}  ({peak/naive_capital*100:.0f}% от naive)")
        print(f"  Среднее когда хоть один in:  ${avg_active:>7.0f}  ({avg_active/naive_capital*100:.0f}% от naive)")
        print(f"  На naive ${naive_capital}:   annual={m_naive['annual']:>6.2f}%  max_dd={m_naive['max_dd']:>5.2f}%  Calmar={m_naive['calmar']}")
        print(f"  На peak  ${peak:.0f}:    annual={m_peak['annual']:>6.2f}%  max_dd={m_peak['max_dd']:>5.2f}%  Calmar={m_peak['calmar']}")
        print(f"  На avg   ${avg_active:.0f}:    annual={m_avg['annual']:>6.2f}%  max_dd={m_avg['max_dd']:>5.2f}%  Calmar={m_avg['calmar']}")

    # ─── 2. Funding momentum signal ───────────────────────────────────────────
    print("\n" + "="*100)
    print(f"ЭКСПЕРИМЕНТ 2: FUNDING MOMENTUM (A_cycle, top-7)")
    print("Вход разрешён только если текущий funding >= среднего за прошлое окно")
    print("="*100)

    print("\n--- Базовые параметры (entry=20%, exit=-5%, min_hold=72) ---")
    rows = []
    for mom_w in [0, 6, 12, 24, 48, 72, 168]:
        r = run_multi(coins, "A_cycle", f"mom={mom_w if mom_w else 'off'}",
                      entry_threshold=0.20, exit_threshold=-0.05, min_hold=72,
                      momentum_window=mom_w)
        rows.append(r)
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n--- COMBO 1 (entry=30%, exit=-15%, min_hold=120) ---")
    rows = []
    for mom_w in [0, 12, 24, 48, 72, 168]:
        r = run_multi(coins, "A_cycle", f"mom={mom_w if mom_w else 'off'}",
                      entry_threshold=0.30, exit_threshold=-0.15, min_hold=120,
                      momentum_window=mom_w)
        rows.append(r)
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n--- COMBO 5 (entry=30%, exit=-20%, min_hold=168, sig_ma=12) ---")
    rows = []
    for mom_w in [0, 12, 24, 48, 72, 168]:
        r = run_multi(coins, "A_cycle", f"mom={mom_w if mom_w else 'off'}",
                      entry_threshold=0.30, exit_threshold=-0.20, min_hold=168,
                      signal_window=12, momentum_window=mom_w)
        rows.append(r)
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
