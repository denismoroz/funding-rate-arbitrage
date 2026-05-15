"""
Combined adaptive entry threshold + dynamic min_hold для funding-harvest стратегии.

Комбинация двух адаптаций:
1. Adaptive percentile-based entry_threshold (per-coin per-hour):
       threshold[c][i] = max(hard_floor, np.percentile(signal[c][i-lookback_h:i], 100 - top_x_pct))
   В warm-up (threshold == NaN) — entry запрещён.

2. Dynamic min_hold (per-position на момент открытия):
       position_min_hold = int(min(cap_min_hold, max(base_min_hold, safety_mult × 18.4 / entry_rate)))
   Где entry_rate = signal[c][i] (annualized smoothed) на момент открытия.

Цель: конфигурация, которая работает в любом funding-режиме.
"""

import sys
import numpy as np
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from engine import (
    load_data,
    POSITION_SIZE, TOTAL_CAPITAL, PERP_TAKER, SPOT_TAKER, HOURS_PER_YEAR,
)
from concurrency_cap import simulate_multi_capped, metrics_on_capital
from dynamic_min_hold import simulate_dynamic_min_hold
from adaptive_entry import simulate_adaptive_entry, metrics_window


BREAKEVEN_CONST = 18.4  # = 0.0021 × 8760


def simulate_combined(
    coins,
    max_concurrent: int,
    top_x_pct: float,        # верхние X% сигналов считаем "входными"
    lookback_days: int,      # окно для percentile в днях
    hard_floor: float,       # минимум порога (annualized)
    exit_threshold: float,
    base_min_hold: int,      # минимальный min_hold (нижний предел)
    signal_window: int,      # MA window для сигнала
    safety_mult: float,      # множитель break-even для min_hold
    cap_min_hold: int,       # максимальный min_hold (верхний предел)
):
    """
    Симулирует A_cycle со всеми монетами, адаптивным percentile-based entry threshold
    И динамическим per-position min_hold.

    Возвращает (pnl_per_hour, cap_per_hour, info).
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

    lookback_h = lookback_days * 24

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

        # Предрасчёт адаптивного порога (как в adaptive_entry.py)
        thr = np.full(n, np.nan)
        for i in range(lookback_h, n):
            window = sig[max(0, i - lookback_h):i]
            window = window[~np.isnan(window)]
            if len(window) > 0:
                p = np.percentile(window, 100 - top_x_pct)
                thr[i] = max(hard_floor, p)

        state[c] = {
            "rates":             rates,
            "close":             close,
            "signal":            sig,
            "threshold":         thr,
            "valid":             ~np.isnan(close) & ~np.isnan(rates),
            "in_position":       False,
            "short_size":        0.0,
            "units_spot":        0.0,
            "entry_price":       0.0,
            "hours_since":       0,
            "position_min_hold": 0,    # динамический min_hold для текущей позиции
            "cash":              TOTAL_CAPITAL,
            "equity_prev":       TOTAL_CAPITAL,
            "trades":            0,
            "hours_in":          0,
        }

    pnl_per_hour   = np.zeros(n)
    cap_per_hour   = np.zeros(n)
    opens_per_hour = np.zeros(n, dtype=int)

    for i in range(n):
        # 1) Funding для всех уже-в-позиции
        for c, s in state.items():
            if not s["valid"][i]:
                continue
            if s["in_position"]:
                P = s["close"][i]
                r = s["rates"][i]
                s["cash"] += s["short_size"] * P * r

        # 2) Exit для in-position — используем position_min_hold (динамический)
        for c, s in state.items():
            if not s["valid"][i] or not s["in_position"]:
                continue
            s["hours_since"] += 1
            s["hours_in"]    += 1
            ar = s["rates"][i] * HOURS_PER_YEAR
            if s["hours_since"] >= s["position_min_hold"] and ar < exit_threshold:
                P = s["close"][i]
                # close short
                s["cash"] += s["short_size"] * (s["entry_price"] - P)
                s["cash"] -= s["short_size"] * P * PERP_TAKER
                # sell spot (A_cycle)
                s["cash"] += s["units_spot"] * P
                s["cash"] -= s["units_spot"] * P * SPOT_TAKER
                s["short_size"]        = 0.0
                s["units_spot"]        = 0.0
                s["entry_price"]       = 0.0
                s["in_position"]       = False
                s["position_min_hold"] = 0

        # 3) Подсчёт текущих in-position
        active     = [c for c, s in state.items() if s["in_position"]]
        slots_free = max_concurrent - len(active)

        # 4) Кандидаты на вход — adaptive threshold (NaN запрещает вход в warm-up)
        if slots_free > 0:
            candidates = []
            for c, s in state.items():
                if not s["valid"][i] or s["in_position"]:
                    continue
                thr_i = s["threshold"][i]
                if not np.isnan(thr_i) and s["signal"][i] > thr_i:
                    candidates.append((c, s["signal"][i]))
            candidates.sort(key=lambda x: -x[1])  # сильнейший первым
            for c, _ in candidates[:slots_free]:
                s = state[c]
                P = s["close"][i]

                # Динамический min_hold по entry rate (как в dynamic_min_hold.py)
                entry_rate = s["signal"][i]  # annualized, сглаженный
                if entry_rate > 0:
                    breakeven_h = BREAKEVEN_CONST / entry_rate
                    pos_min_hold = int(min(cap_min_hold,
                                          max(base_min_hold,
                                              safety_mult * breakeven_h)))
                else:
                    pos_min_hold = cap_min_hold

                # Купить spot
                s["units_spot"]        = POSITION_SIZE / P
                s["cash"]             -= POSITION_SIZE
                s["cash"]             -= POSITION_SIZE * SPOT_TAKER
                # Открыть short
                s["short_size"]        = POSITION_SIZE / P
                s["entry_price"]       = P
                s["cash"]             -= POSITION_SIZE * PERP_TAKER
                s["in_position"]       = True
                s["hours_since"]       = 0
                s["position_min_hold"] = pos_min_hold
                s["trades"]           += 1
                opens_per_hour[i]     += 1

        # 5) MTM equity per coin
        hour_pnl = 0.0
        hour_cap = 0.0
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
        s["cash"] += s["short_size"] * (s["entry_price"] - P)
        s["cash"] -= s["short_size"] * P * PERP_TAKER
        s["cash"] += s["units_spot"] * P
        s["cash"] -= s["units_spot"] * P * SPOT_TAKER
        s["short_size"] = 0.0
        s["units_spot"] = 0.0
        equity_now = s["cash"]
        pnl_per_hour[-1] += equity_now - s["equity_prev"]
        s["equity_prev"] = equity_now

    info = {
        "trades_per_coin": {c: state[c]["trades"] for c in state},
        "total_trades":    sum(s["trades"] for s in state.values()),
        "peak_capital":    cap_per_hour.max(),
        "opens_per_hour":  opens_per_hour,
    }
    return pnl_per_hour, cap_per_hour, info


def main():
    coins         = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
    K             = 3
    exit_threshold = -0.15
    signal_window  = 12
    base_min_hold  = 24
    cap_min_hold   = 720
    capital_base   = K * TOTAL_CAPITAL   # $6000

    rows = []

    # ── Baselines ──────────────────────────────────────────────────────────────
    baselines = [
        ("baseline_30_120", lambda: simulate_multi_capped(
            coins, K, entry_threshold=0.30, exit_threshold=exit_threshold,
            min_hold=120, signal_window=signal_window)),
        ("baseline_15_120", lambda: simulate_multi_capped(
            coins, K, entry_threshold=0.15, exit_threshold=exit_threshold,
            min_hold=120, signal_window=signal_window)),
        ("dynamic_only", lambda: simulate_dynamic_min_hold(
            coins, K, entry_threshold=0.15, exit_threshold=exit_threshold,
            base_min_hold=base_min_hold, signal_window=signal_window,
            safety_mult=3.0, cap_min_hold=cap_min_hold)),
        ("dynamic_only_aggressive", lambda: simulate_dynamic_min_hold(
            coins, K, entry_threshold=0.08, exit_threshold=exit_threshold,
            base_min_hold=base_min_hold, signal_window=signal_window,
            safety_mult=5.0, cap_min_hold=cap_min_hold)),
        ("adaptive_only", lambda: simulate_adaptive_entry(
            coins, K, top_x_pct=10, lookback_days=90, hard_floor=0.10,
            exit_threshold=exit_threshold, min_hold=120, signal_window=signal_window)),
    ]

    n_total = len(baselines)
    for idx, (label, fn) in enumerate(baselines, 1):
        print(f"[baseline {idx}/{n_total}] {label} ...")
        pnl, cap, info = fn()
        n = len(pnl)

        # opens_per_hour: для simulate_multi_capped нет прямого поля — реконструируем
        if "opens_per_hour" in info:
            opens_ph = info["opens_per_hour"]
        else:
            cap_diff = np.diff(cap, prepend=0)
            opens_ph = np.where(cap_diff > 0, (cap_diff / TOTAL_CAPITAL).astype(int), 0)

        # full period
        mf = metrics_window(pnl, cap, opens_ph, capital_base, 0, n)
        rows.append({
            "mode": label, "top_x": None, "lookback": None,
            "hard_floor": None, "safety_mult": None,
            "period": "full", **mf,
        })
        # last 90d
        start_90 = max(0, n - 90 * 24)
        m90 = metrics_window(pnl, cap, opens_ph, capital_base, start_90, n)
        rows.append({
            "mode": label, "top_x": None, "lookback": None,
            "hard_floor": None, "safety_mult": None,
            "period": "last_90d", **m90,
        })

    # ── Combined sweep ─────────────────────────────────────────────────────────
    TOP_X_PCTS    = [10, 20, 30]
    LOOKBACK_DAYS = [60, 90]
    HARD_FLOORS   = [0.08, 0.10]
    SAFETY_MULTS  = [3.0, 5.0]

    total_configs = len(TOP_X_PCTS) * len(LOOKBACK_DAYS) * len(HARD_FLOORS) * len(SAFETY_MULTS)
    done = 0
    for top_x in TOP_X_PCTS:
        for lookback in LOOKBACK_DAYS:
            for hfloor in HARD_FLOORS:
                for safety_mult in SAFETY_MULTS:
                    done += 1
                    print(f"[{done}/{total_configs}] combined top_x={top_x} lookback={lookback}d "
                          f"hard_floor={hfloor:.2f} safety_mult={safety_mult} ...")
                    pnl, cap, info = simulate_combined(
                        coins,
                        max_concurrent=K,
                        top_x_pct=top_x,
                        lookback_days=lookback,
                        hard_floor=hfloor,
                        exit_threshold=exit_threshold,
                        base_min_hold=base_min_hold,
                        signal_window=signal_window,
                        safety_mult=safety_mult,
                        cap_min_hold=cap_min_hold,
                    )
                    n = len(pnl)
                    opens_ph = info["opens_per_hour"]

                    # full period
                    mf = metrics_window(pnl, cap, opens_ph, capital_base, 0, n)
                    rows.append({
                        "mode": "combined",
                        "top_x": top_x,
                        "lookback": lookback,
                        "hard_floor": hfloor,
                        "safety_mult": safety_mult,
                        "period": "full",
                        **mf,
                    })
                    # last 90d
                    start_90 = max(0, n - 90 * 24)
                    m90 = metrics_window(pnl, cap, opens_ph, capital_base, start_90, n)
                    rows.append({
                        "mode": "combined",
                        "top_x": top_x,
                        "lookback": lookback,
                        "hard_floor": hfloor,
                        "safety_mult": safety_mult,
                        "period": "last_90d",
                        **m90,
                    })

    # ── DataFrame ──────────────────────────────────────────────────────────────
    df = pd.DataFrame(rows, columns=[
        "mode", "top_x", "lookback", "hard_floor", "safety_mult", "period",
        "annual", "max_dd", "calmar", "sharpe", "trades",
        "time_in_market_pct", "median_wait_hours", "n_empty_windows",
    ])

    # Переименовываем колонки для вывода
    df_out = df.rename(columns={
        "time_in_market_pct": "tim_pct",
        "median_wait_hours":  "median_wait_h",
    })

    # Сохранить полный df
    out = Path(__file__).parent / "combined_adaptive_results.csv"
    df_out.to_csv(out, index=False)
    print(f"\nСохранено: {out}")

    # ── Таблицы ────────────────────────────────────────────────────────────────
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)

    cols_show = [
        "mode", "top_x", "lookback", "hard_floor", "safety_mult",
        "annual", "max_dd", "calmar", "sharpe", "trades",
        "tim_pct", "median_wait_h",
    ]

    df_full = df_out[df_out["period"] == "full"].sort_values("calmar", ascending=False).reset_index(drop=True)
    df_90   = df_out[df_out["period"] == "last_90d"].sort_values("calmar", ascending=False).reset_index(drop=True)

    print("\n" + "=" * 140)
    print("FULL PERIOD — top 15 по calmar")
    print("=" * 140)
    print(df_full[cols_show].head(15).to_string(index=False))

    print("\n" + "=" * 140)
    print("LAST 90 DAYS — top 15 по calmar")
    print("=" * 140)
    print(df_90[cols_show].head(15).to_string(index=False))


if __name__ == "__main__":
    main()
