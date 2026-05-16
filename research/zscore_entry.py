"""
Z-score based entry для funding-harvest стратегии.

Идея: открываемся когда funding signal значимо отклоняется от своего rolling mean
(z-score > z_threshold), захватывая «funding events» (всплески), а не абсолютный
уровень. Это адаптация к режиму без хардкода.

Условие входа:
    z = (signal[i] - mean[lookback]) / std[lookback] > z_threshold
    AND signal[i] > min_floor  (защита от tiny-std / нулевых сигналов)

min_floor = safety_mult × 18.4 / cap_min_hold  (math-derived break-even)
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
from adaptive_entry import metrics_window


BREAKEVEN_CONST = 18.4  # = 0.0021 × 8760


def simulate_zscore_entry(
    coins,
    max_concurrent: int,
    exit_threshold: float,
    base_min_hold: int,
    signal_window: int,
    safety_mult: float,
    cap_min_hold: int,
    z_threshold: float,
    lookback_days: int,
):
    """
    Симулирует A_cycle с z-score based entry и динамическим min_hold.

    При открытии позиции кандидат проходит если:
        z = (signal[i] - rolling_mean[lookback]) / rolling_std[lookback] > z_threshold
        AND signal[i] > min_floor

    min_floor = safety_mult * BREAKEVEN_CONST / cap_min_hold

    Динамический min_hold как в simulate_dynamic_min_hold:
        breakeven_h = BREAKEVEN_CONST / entry_rate
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

    lookback_h = lookback_days * 24
    min_floor = safety_mult * BREAKEVEN_CONST / cap_min_hold

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

        # Предрасчёт rolling mean и std по lookback окну
        sig_series = pd.Series(sig)
        mean_arr = sig_series.rolling(lookback_h, min_periods=lookback_h).mean().values
        std_arr  = sig_series.rolling(lookback_h, min_periods=lookback_h).std().values

        state[c] = {
            "rates":             rates,
            "close":             close,
            "signal":            sig,
            "mean_arr":          mean_arr,
            "std_arr":           std_arr,
            "valid":             ~np.isnan(close) & ~np.isnan(rates),
            "in_position":       False,
            "short_size":        0.0,
            "units_spot":        0.0,
            "entry_price":       0.0,
            "hours_since":       0,
            "position_min_hold": 0,
            "cash":              TOTAL_CAPITAL,
            "equity_prev":       TOTAL_CAPITAL,
            "trades":            0,
            "hours_in":          0,
        }

    pnl_per_hour   = np.zeros(n)
    cap_per_hour   = np.zeros(n)
    opens_per_hour = np.zeros(n, dtype=int)

    entry_rates_collected = []
    holds_collected       = []

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
        active     = [c for c, s in state.items() if s["in_position"]]
        slots_free = max_concurrent - len(active)

        # 4) Кандидаты на вход — z-score фильтр
        if slots_free > 0:
            candidates = []
            for c, s in state.items():
                if not s["valid"][i] or s["in_position"]:
                    continue
                m  = s["mean_arr"][i]
                sd = s["std_arr"][i]
                if not np.isnan(m) and not np.isnan(sd) and sd > 0:
                    z = (s["signal"][i] - m) / sd
                    if z > z_threshold and s["signal"][i] > min_floor:
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

        # 5) MTM equity per coin
        hour_pnl = 0.0
        hour_cap = 0.0
        for c, s in state.items():
            if not s["valid"][i]:
                continue
            P = s["close"][i]
            short_pnl  = s["short_size"] * (s["entry_price"] - P) if s["in_position"] else 0.0
            equity_now = s["cash"] + s["units_spot"] * P + short_pnl
            hour_pnl  += equity_now - s["equity_prev"]
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
        "total_trades":          sum(s["trades"] for s in state.values()),
        "peak_capital":          cap_per_hour.max(),
        "opens_per_hour":        opens_per_hour,
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
    capital_base   = K * TOTAL_CAPITAL  # $6000

    Z_THRESHOLDS  = [0.5, 1.0, 1.5, 2.0]
    LOOKBACK_DAYS = [14, 30, 60]
    SAFETY_MULTS  = [3.0, 5.0]
    CAP_MIN_HOLDS = [480, 720]

    rows = []

    # ── Baselines ──────────────────────────────────────────────────────────────
    print("Running baseline_30_120 ...")
    pnl_b30, cap_b30, _ = simulate_multi_capped(
        coins,
        max_concurrent=K,
        entry_threshold=0.30,
        exit_threshold=exit_threshold,
        min_hold=120,
        signal_window=signal_window,
    )
    n_b30 = len(pnl_b30)
    cap_diff_b30 = np.diff(cap_b30, prepend=0)
    opens_b30 = np.where(cap_diff_b30 > 0, (cap_diff_b30 / TOTAL_CAPITAL).astype(int), 0)

    for period, start_idx in [("full", 0), ("last_90d", max(0, n_b30 - 2160))]:
        m = metrics_window(pnl_b30, cap_b30, opens_b30, capital_base, start_idx, n_b30)
        rows.append({
            "mode": "baseline_30_120",
            "z_threshold": None, "lookback_days": None,
            "safety_mult": None, "cap_min_hold": None,
            "period": period,
            **m,
        })

    print("Running dynamic_balanced (entry=0.15, mult=3.0, cap=720) ...")
    pnl_db, cap_db, info_db = simulate_dynamic_min_hold(
        coins, max_concurrent=K,
        entry_threshold=0.15, exit_threshold=exit_threshold,
        base_min_hold=base_min_hold, signal_window=signal_window,
        safety_mult=3.0, cap_min_hold=720,
    )
    n_db = len(pnl_db)
    for period, start_idx in [("full", 0), ("last_90d", max(0, n_db - 2160))]:
        m = metrics_window(pnl_db, cap_db, info_db["opens_per_hour"], capital_base, start_idx, n_db)
        rows.append({
            "mode": "dynamic_balanced",
            "z_threshold": None, "lookback_days": None,
            "safety_mult": 3.0, "cap_min_hold": 720,
            "period": period,
            **m,
        })

    print("Running dynamic_aggressive (entry=0.08, mult=5.0, cap=720) ...")
    pnl_da, cap_da, info_da = simulate_dynamic_min_hold(
        coins, max_concurrent=K,
        entry_threshold=0.08, exit_threshold=exit_threshold,
        base_min_hold=base_min_hold, signal_window=signal_window,
        safety_mult=5.0, cap_min_hold=720,
    )
    n_da = len(pnl_da)
    for period, start_idx in [("full", 0), ("last_90d", max(0, n_da - 2160))]:
        m = metrics_window(pnl_da, cap_da, info_da["opens_per_hour"], capital_base, start_idx, n_da)
        rows.append({
            "mode": "dynamic_aggressive",
            "z_threshold": None, "lookback_days": None,
            "safety_mult": 5.0, "cap_min_hold": 720,
            "period": period,
            **m,
        })

    # ── Z-score sweep ──────────────────────────────────────────────────────────
    total_configs = len(Z_THRESHOLDS) * len(LOOKBACK_DAYS) * len(SAFETY_MULTS) * len(CAP_MIN_HOLDS)
    done = 0
    for z_thr in Z_THRESHOLDS:
        for lookback in LOOKBACK_DAYS:
            for smult in SAFETY_MULTS:
                for cap_mh in CAP_MIN_HOLDS:
                    done += 1
                    print(f"[{done}/{total_configs}] zscore z={z_thr} look={lookback}d mult={smult} cap={cap_mh} ...")
                    pnl, cap, info = simulate_zscore_entry(
                        coins,
                        max_concurrent=K,
                        exit_threshold=exit_threshold,
                        base_min_hold=base_min_hold,
                        signal_window=signal_window,
                        safety_mult=smult,
                        cap_min_hold=cap_mh,
                        z_threshold=z_thr,
                        lookback_days=lookback,
                    )
                    n = len(pnl)
                    opens_ph = info["opens_per_hour"]

                    for period, start_idx in [("full", 0), ("last_90d", max(0, n - 2160))]:
                        m = metrics_window(pnl, cap, opens_ph, capital_base, start_idx, n)
                        rows.append({
                            "mode": "zscore",
                            "z_threshold": z_thr,
                            "lookback_days": lookback,
                            "safety_mult": smult,
                            "cap_min_hold": cap_mh,
                            "period": period,
                            **m,
                        })

    # ── DataFrame + CSV ────────────────────────────────────────────────────────
    df = pd.DataFrame(rows, columns=[
        "mode", "z_threshold", "lookback_days", "safety_mult", "cap_min_hold",
        "period", "annual", "max_dd", "calmar", "sharpe", "trades",
        "time_in_market_pct", "median_wait_hours",
    ])

    out = Path(__file__).parent / "zscore_entry_results.csv"
    df.to_csv(out, index=False)
    print(f"\nСохранено: {out}")

    # ── Таблицы ────────────────────────────────────────────────────────────────
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 20)

    cols_show = [
        "mode", "z_threshold", "lookback_days", "safety_mult", "cap_min_hold",
        "period", "annual", "max_dd", "calmar", "sharpe", "trades",
        "time_in_market_pct", "median_wait_hours",
    ]

    df_full = df[df["period"] == "full"].sort_values("calmar", ascending=False).reset_index(drop=True)
    df_90   = df[df["period"] == "last_90d"].sort_values("calmar", ascending=False).reset_index(drop=True)

    print("\n" + "=" * 160)
    print("FULL PERIOD — top 15 по calmar")
    print("=" * 160)
    print(df_full[cols_show].head(15).to_string(index=False))

    print("\n" + "=" * 160)
    print("LAST 90 DAYS — top 15 по calmar")
    print("=" * 160)
    print(df_90[cols_show].head(15).to_string(index=False))


if __name__ == "__main__":
    main()
