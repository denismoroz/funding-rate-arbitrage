"""
Dynamic min_hold зависящий от funding rate на входе.

Гипотеза: при низких funding нужен длинный hold чтобы окупить fees,
при высоких — можно держать меньше.

Break-even: fees = 0.0021 (21bps per cycle)
hours_breakeven = 0.0021 × 8760 / annual_rate = 18.4 / annual_rate
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


BREAKEVEN_CONST = 18.4  # = 0.0021 × 8760


def metrics_window(pnl_arr, cap_per_hour, opens_per_hour, capital_base, start_idx, end_idx):
    """
    Считает метрики для указанного среза массивов.
    """
    pnl   = pnl_arr[start_idx:end_idx]
    cap   = cap_per_hour[start_idx:end_idx]
    opens = opens_per_hour[start_idx:end_idx]
    n_window = end_idx - start_idx

    total_pnl = pnl.sum()
    annual = total_pnl / capital_base / (n_window / HOURS_PER_YEAR) * 100

    equity = np.cumsum(pnl)
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / (capital_base + peak)
    max_dd = abs(dd.min()) * 100

    hr = pnl / capital_base
    sharpe = (hr.mean() / hr.std() * np.sqrt(HOURS_PER_YEAR)) if hr.std() > 0 else 0

    calmar = annual / max_dd if max_dd > 0 else 0
    time_in_market = (cap > 0).mean() * 100
    trades = int(opens.sum())

    # Empty runs → median wait hours
    empty = (cap == 0)
    runs = []
    run = 0
    for x in empty:
        if x:
            run += 1
        else:
            if run > 0:
                runs.append(run)
            run = 0
    if run > 0:
        runs.append(run)
    median_wait = int(np.median(runs)) if runs else 0

    return {
        "annual":             round(annual, 2),
        "max_dd":             round(max_dd, 2),
        "calmar":             round(calmar, 1),
        "sharpe":             round(sharpe, 2),
        "trades":             trades,
        "time_in_market_pct": round(time_in_market, 1),
        "median_wait_hours":  median_wait,
    }


def simulate_dynamic_min_hold(
    coins,
    max_concurrent: int,
    entry_threshold: float,
    exit_threshold: float,
    base_min_hold: int,
    signal_window: int,
    safety_mult: float,
    cap_min_hold: int,
):
    """
    Симулирует A_cycle с динамическим min_hold, зависящим от funding на входе.

    При открытии позиции:
        breakeven_h = 18.4 / entry_rate
        position_min_hold = min(cap_min_hold, max(base_min_hold, safety_mult * breakeven_h))

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
            "rates":             rates,
            "close":             close,
            "signal":            sig,
            "valid":             ~np.isnan(close) & ~np.isnan(rates),
            "in_position":       False,
            "short_size":        0.0,
            "units_spot":        0.0,
            "entry_price":       0.0,
            "hours_since":       0,
            "position_min_hold": 0,     # динамический min_hold для текущей позиции
            "cash":              TOTAL_CAPITAL,
            "equity_prev":       TOTAL_CAPITAL,
            "trades":            0,
            "hours_in":          0,
        }

    pnl_per_hour   = np.zeros(n)
    cap_per_hour   = np.zeros(n)
    opens_per_hour = np.zeros(n, dtype=int)

    entry_rates_collected = []  # annualized rate на каждом открытии
    holds_collected       = []  # назначенный min_hold на каждом открытии

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
            # Используем position_min_hold (динамический), а не глобальный min_hold
            if s["hours_since"] >= s["position_min_hold"] and ar < exit_threshold:
                P = s["close"][i]
                # close short
                s["cash"] += s["short_size"] * (s["entry_price"] - P)
                s["cash"] -= s["short_size"] * P * PERP_TAKER
                # sell spot
                s["cash"] += s["units_spot"] * P
                s["cash"] -= s["units_spot"] * P * SPOT_TAKER
                s["short_size"]        = 0.0
                s["units_spot"]        = 0.0
                s["entry_price"]       = 0.0
                s["in_position"]       = False
                s["position_min_hold"] = 0

        # 3) Подсчёт текущих in-position
        active    = [c for c, s in state.items() if s["in_position"]]
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

                # Динамический min_hold на основе entry rate
                entry_rate = s["signal"][i]  # annualized, сглаженный
                if entry_rate > 0:
                    breakeven_h = BREAKEVEN_CONST / entry_rate
                    pos_min_hold = int(min(cap_min_hold,
                                          max(base_min_hold,
                                              safety_mult * breakeven_h)))
                else:
                    pos_min_hold = cap_min_hold

                # Запись статистики
                entry_rates_collected.append(entry_rate)
                holds_collected.append(pos_min_hold)

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

        # 5) MTM equity per coin = cash + units_spot * P + short_pnl
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
        "total_trades":         sum(s["trades"] for s in state.values()),
        "peak_capital":         cap_per_hour.max(),
        "opens_per_hour":       opens_per_hour,
        "entry_rates_collected": entry_rates_collected,
        "holds_collected":       holds_collected,
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

    ENTRY_THRESHOLDS = [0.08, 0.10, 0.15, 0.20]
    SAFETY_MULTS     = [1.5, 2.0, 3.0, 5.0]

    rows = []

    # ── Baselines ──────────────────────────────────────────────────────────────
    baselines = [
        ("baseline_30_120", 0.30, 120),
        ("baseline_15_120", 0.15, 120),
        ("baseline_10_120", 0.10, 120),
        ("baseline_10_1",   0.10, 1),
    ]

    for label, entry_thr, min_hold in baselines:
        print(f"Running {label} ...")
        pnl, cap, info = simulate_multi_capped(
            coins,
            max_concurrent=K,
            entry_threshold=entry_thr,
            exit_threshold=exit_threshold,
            min_hold=min_hold,
            signal_window=signal_window,
        )
        n = len(pnl)
        # Реконструируем opens_per_hour из изменений cap
        cap_diff = np.diff(cap, prepend=0)
        opens_ph = np.where(cap_diff > 0, (cap_diff / TOTAL_CAPITAL).astype(int), 0)

        # full period
        mf = metrics_window(pnl, cap, opens_ph, capital_base, 0, n)
        rows.append({
            "mode":             label,
            "entry":            entry_thr,
            "safety_mult":      0,
            "period":           "full",
            "avg_hold_assigned": min_hold,
            **mf,
        })
        # last 90d
        start_90 = max(0, n - 2160)
        m90 = metrics_window(pnl, cap, opens_ph, capital_base, start_90, n)
        rows.append({
            "mode":             label,
            "entry":            entry_thr,
            "safety_mult":      0,
            "period":           "last_90d",
            "avg_hold_assigned": min_hold,
            **m90,
        })

    # ── Dynamic sweep ──────────────────────────────────────────────────────────
    total_configs = len(ENTRY_THRESHOLDS) * len(SAFETY_MULTS)
    done = 0
    for entry_thr in ENTRY_THRESHOLDS:
        for safety_mult in SAFETY_MULTS:
            done += 1
            print(f"[{done}/{total_configs}] dynamic entry={entry_thr:.2f} safety_mult={safety_mult} ...")
            pnl, cap, info = simulate_dynamic_min_hold(
                coins,
                max_concurrent=K,
                entry_threshold=entry_thr,
                exit_threshold=exit_threshold,
                base_min_hold=base_min_hold,
                signal_window=signal_window,
                safety_mult=safety_mult,
                cap_min_hold=cap_min_hold,
            )
            n = len(pnl)
            opens_ph = info["opens_per_hour"]
            holds    = info["holds_collected"]
            avg_hold = round(np.mean(holds), 1) if holds else 0

            # full period
            mf = metrics_window(pnl, cap, opens_ph, capital_base, 0, n)
            rows.append({
                "mode":             "dynamic",
                "entry":            entry_thr,
                "safety_mult":      safety_mult,
                "period":           "full",
                "avg_hold_assigned": avg_hold,
                **mf,
            })
            # last 90d
            start_90 = max(0, n - 2160)
            m90 = metrics_window(pnl, cap, opens_ph, capital_base, start_90, n)
            rows.append({
                "mode":             "dynamic",
                "entry":            entry_thr,
                "safety_mult":      safety_mult,
                "period":           "last_90d",
                "avg_hold_assigned": avg_hold,
                **m90,
            })

    # ── Сохранить полный результат ─────────────────────────────────────────────
    df = pd.DataFrame(rows, columns=[
        "mode", "entry", "safety_mult", "period",
        "annual", "max_dd", "calmar", "sharpe", "trades",
        "time_in_market_pct", "median_wait_hours", "avg_hold_assigned",
    ])
    out = Path(__file__).parent / "dynamic_min_hold_results.csv"
    df.to_csv(out, index=False)
    print(f"\nСохранено: {out}")

    # ── Таблицы ────────────────────────────────────────────────────────────────
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)

    cols_show = [
        "mode", "entry", "safety_mult", "period",
        "annual", "max_dd", "calmar", "sharpe", "trades",
        "time_in_market_pct", "median_wait_hours", "avg_hold_assigned",
    ]

    df_full = df[df["period"] == "full"].sort_values("calmar", ascending=False).reset_index(drop=True)
    df_90   = df[df["period"] == "last_90d"].sort_values("calmar", ascending=False).reset_index(drop=True)

    print("\n" + "="*140)
    print("FULL PERIOD — top 12 по calmar")
    print("="*140)
    print(df_full[cols_show].head(12).to_string(index=False))

    print("\n" + "="*140)
    print("LAST 90 DAYS — top 12 по calmar")
    print("="*140)
    print(df_90[cols_show].head(12).to_string(index=False))


if __name__ == "__main__":
    main()
