"""
Staged (лесенка) entry thresholds для K=3 слотов.

Идея: вместо одного фиксированного entry_threshold — три разных порога по слотам.
  Slot 0 (первый занятый): низкий или "auto_30d" (per-coin rolling 30d avg signal)
  Slot 1:                  средний (например 0.15)
  Slot 2:                  высокий  (например 0.30)

Это позволяет всегда работать (slot 0 открывается при любом нагреве),
но масштабировать риск вверх только при реально горячем рынке.

Dynamic min_hold (из dynamic_min_hold.py) применяется на каждом открытии:
  position_min_hold = min(cap_min_hold, max(base_min_hold, safety_mult × 18.4 / entry_rate))
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
from concurrency_cap import simulate_multi_capped
from adaptive_entry import metrics_window
from dynamic_min_hold import simulate_dynamic_min_hold

BREAKEVEN_CONST = 18.4  # = 0.0021 × 8760


def simulate_staged_entry(
    coins,
    thresholds,           # list/tuple длины max_concurrent; элементы: float или "auto_30d"
    exit_threshold: float,
    base_min_hold: int,
    signal_window: int,
    safety_mult: float,
    cap_min_hold: int,
    hard_floor: float = 0.05,
):
    """
    Симулирует A_cycle с лесенкой порогов по слотам.

    thresholds[0] — порог для 1-й открытой позиции (самый мягкий)
    thresholds[1] — порог для 2-й позиции
    thresholds[2] — порог для 3-й позиции (самый жёсткий)

    Строка "auto_30d" → per-coin rolling 30d mean annualized signal,
    с floor = hard_floor (в warm-up периоде 720ч — тоже hard_floor).

    При открытии любой позиции dynamic min_hold:
      breakeven_h = 18.4 / entry_rate
      position_min_hold = min(cap_min_hold, max(base_min_hold, safety_mult × breakeven_h))

    Возвращает (pnl_per_hour, cap_per_hour, info).
    """
    max_concurrent = len(thresholds)
    ROLLING_30D_WINDOW = 30 * 24  # 720 часов

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

    has_auto = any(isinstance(t, str) and t == "auto_30d" for t in thresholds)

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

        entry = {
            "rates":             rates,
            "close":             close,
            "signal":            sig,
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

        # Предрасчёт rolling 30d avg signal (только если нужно)
        if has_auto:
            rolling_30d = (
                pd.Series(sig)
                .rolling(ROLLING_30D_WINDOW, min_periods=1)
                .mean()
                .values
                .copy()
            )
            # Warm-up: первые 720 часов — используем hard_floor
            rolling_30d[:ROLLING_30D_WINDOW] = np.nan
            # Для warm-up — floor
            rolling_30d_floored = np.where(
                np.isnan(rolling_30d),
                hard_floor,
                np.maximum(hard_floor, rolling_30d),
            )
            entry["rolling_30d"] = rolling_30d_floored

        state[c] = entry

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
                # Закрыть short
                s["cash"] += s["short_size"] * (s["entry_price"] - P)
                s["cash"] -= s["short_size"] * P * PERP_TAKER
                # Продать spot
                s["cash"] += s["units_spot"] * P
                s["cash"] -= s["units_spot"] * P * SPOT_TAKER
                s["short_size"]        = 0.0
                s["units_spot"]        = 0.0
                s["entry_price"]       = 0.0
                s["in_position"]       = False
                s["position_min_hold"] = 0

        # 3) Подсчёт текущих in-position
        active     = [c for c, s in state.items() if s["in_position"]]
        n_active   = len(active)
        slots_free = max_concurrent - n_active

        # 4) Staged entry — по одному слоту
        if slots_free > 0:
            for slot_idx in range(n_active, max_concurrent):
                thr_spec = thresholds[slot_idx]

                # Строим threshold_fn(c) → float для этого слота
                if isinstance(thr_spec, str) and thr_spec == "auto_30d":
                    def threshold_fn(c, _i=i, _state=state):
                        return _state[c]["rolling_30d"][_i]
                else:
                    thr_val = float(thr_spec)
                    def threshold_fn(c, _thr=thr_val, **_):
                        return _thr

                # Собираем кандидатов среди НЕ-в-позиции
                candidates = []
                for c, s in state.items():
                    if not s["valid"][i] or s["in_position"]:
                        continue
                    thr = threshold_fn(c)
                    if not np.isnan(thr) and s["signal"][i] > thr:
                        candidates.append((c, s["signal"][i]))

                if not candidates:
                    # Раз на этом слоте нет кандидатов — на более строгих тоже не будет
                    break

                candidates.sort(key=lambda x: -x[1])
                c, sig_val = candidates[0]
                s = state[c]
                P = s["close"][i]

                # Dynamic min_hold
                entry_rate = sig_val  # annualized signal
                if entry_rate > 0:
                    breakeven_h = BREAKEVEN_CONST / entry_rate
                    pos_min_hold = int(min(cap_min_hold,
                                          max(base_min_hold,
                                              safety_mult * breakeven_h)))
                else:
                    pos_min_hold = cap_min_hold

                entry_rates_collected.append(entry_rate)
                holds_collected.append(pos_min_hold)

                # Открыть позицию
                s["units_spot"]        = POSITION_SIZE / P
                s["cash"]             -= POSITION_SIZE
                s["cash"]             -= POSITION_SIZE * SPOT_TAKER
                s["short_size"]        = POSITION_SIZE / P
                s["entry_price"]       = P
                s["cash"]             -= POSITION_SIZE * PERP_TAKER
                s["in_position"]       = True
                s["hours_since"]       = 0
                s["position_min_hold"] = pos_min_hold
                s["trades"]           += 1
                opens_per_hour[i]     += 1

                # Обновляем счётчик активных для следующего слота
                n_active += 1

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
        "total_trades":          sum(s["trades"] for s in state.values()),
        "peak_capital":          cap_per_hour.max(),
        "opens_per_hour":        opens_per_hour,
        "entry_rates_collected": entry_rates_collected,
        "holds_collected":       holds_collected,
    }
    return pnl_per_hour, cap_per_hour, info


def fmt_thresholds(thresholds):
    """Форматирует список порогов в строку для таблицы."""
    parts = []
    for t in thresholds:
        if isinstance(t, str):
            parts.append(t)
        else:
            parts.append(f"{float(t):.2f}")
    return "[" + ", ".join(parts) + "]"


def main():
    coins         = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
    K             = 3
    exit_threshold = -0.15
    signal_window  = 12
    base_min_hold  = 24
    cap_min_hold   = 720
    capital_base   = K * TOTAL_CAPITAL   # $6000

    THRESHOLDS_VARIANTS = [
        ["auto_30d", 0.15, 0.30],   # основной вариант
        [0.08,       0.15, 0.30],   # фикс лесенка с floor
        [0.10,       0.20, 0.30],   # крутая лесенка
        [0.05,       0.15, 0.30],   # пологая
        ["auto_30d", "auto_30d", 0.30],  # двойной адаптив
    ]
    SAFETY_MULTS = [3.0, 5.0]

    rows = []

    # ── Baselines ─────────────────────────────────────────────────────────────
    print("Running baselines ...")

    # dynamic_only_balanced: entry=0.15, safety_mult=3.0
    pnl, cap, info = simulate_dynamic_min_hold(
        coins,
        max_concurrent=K,
        entry_threshold=0.15,
        exit_threshold=exit_threshold,
        base_min_hold=base_min_hold,
        signal_window=signal_window,
        safety_mult=3.0,
        cap_min_hold=cap_min_hold,
    )
    n = len(pnl)
    opens_ph = info["opens_per_hour"]
    for period, start in [("full", 0), ("last_90d", max(0, n - 2160))]:
        m = metrics_window(pnl, cap, opens_ph, capital_base, start, n)
        rows.append({
            "mode":        "dynamic_only_balanced",
            "thresholds":  "[0.15, 0.15, 0.15]",
            "safety_mult": 3.0,
            "period":      period,
            **m,
        })

    # dynamic_only_aggressive: entry=0.08, safety_mult=5.0
    pnl, cap, info = simulate_dynamic_min_hold(
        coins,
        max_concurrent=K,
        entry_threshold=0.08,
        exit_threshold=exit_threshold,
        base_min_hold=base_min_hold,
        signal_window=signal_window,
        safety_mult=5.0,
        cap_min_hold=cap_min_hold,
    )
    opens_ph = info["opens_per_hour"]
    for period, start in [("full", 0), ("last_90d", max(0, n - 2160))]:
        m = metrics_window(pnl, cap, opens_ph, capital_base, start, n)
        rows.append({
            "mode":        "dynamic_only_aggressive",
            "thresholds":  "[0.08, 0.08, 0.08]",
            "safety_mult": 5.0,
            "period":      period,
            **m,
        })

    # baseline_30_120
    pnl, cap, info = simulate_multi_capped(
        coins,
        max_concurrent=K,
        entry_threshold=0.30,
        exit_threshold=exit_threshold,
        min_hold=120,
        signal_window=signal_window,
    )
    cap_diff = np.diff(cap, prepend=0)
    opens_ph = np.where(cap_diff > 0, (cap_diff / TOTAL_CAPITAL).astype(int), 0)
    for period, start in [("full", 0), ("last_90d", max(0, n - 2160))]:
        m = metrics_window(pnl, cap, opens_ph, capital_base, start, n)
        rows.append({
            "mode":        "baseline_30_120",
            "thresholds":  "[0.30, 0.30, 0.30]",
            "safety_mult": 0,
            "period":      period,
            **m,
        })

    # baseline_15_120
    pnl, cap, info = simulate_multi_capped(
        coins,
        max_concurrent=K,
        entry_threshold=0.15,
        exit_threshold=exit_threshold,
        min_hold=120,
        signal_window=signal_window,
    )
    cap_diff = np.diff(cap, prepend=0)
    opens_ph = np.where(cap_diff > 0, (cap_diff / TOTAL_CAPITAL).astype(int), 0)
    for period, start in [("full", 0), ("last_90d", max(0, n - 2160))]:
        m = metrics_window(pnl, cap, opens_ph, capital_base, start, n)
        rows.append({
            "mode":        "baseline_15_120",
            "thresholds":  "[0.15, 0.15, 0.15]",
            "safety_mult": 0,
            "period":      period,
            **m,
        })

    # ── Staged sweep ──────────────────────────────────────────────────────────
    total_configs = len(THRESHOLDS_VARIANTS) * len(SAFETY_MULTS)
    done = 0
    for thresholds in THRESHOLDS_VARIANTS:
        for safety_mult in SAFETY_MULTS:
            done += 1
            label = fmt_thresholds(thresholds)
            print(f"[{done}/{total_configs}] staged thresholds={label} safety_mult={safety_mult} ...")

            pnl, cap, info = simulate_staged_entry(
                coins,
                thresholds=thresholds,
                exit_threshold=exit_threshold,
                base_min_hold=base_min_hold,
                signal_window=signal_window,
                safety_mult=safety_mult,
                cap_min_hold=cap_min_hold,
            )
            n = len(pnl)
            opens_ph = info["opens_per_hour"]

            for period, start in [("full", 0), ("last_90d", max(0, n - 2160))]:
                m = metrics_window(pnl, cap, opens_ph, capital_base, start, n)
                rows.append({
                    "mode":        "staged",
                    "thresholds":  label,
                    "safety_mult": safety_mult,
                    "period":      period,
                    **m,
                })

    # ── DataFrame и CSV ───────────────────────────────────────────────────────
    cols = [
        "mode", "thresholds", "safety_mult", "period",
        "annual", "max_dd", "calmar", "sharpe", "trades",
        "time_in_market_pct", "median_wait_hours",
    ]

    # metrics_window из adaptive_entry возвращает "n_empty_windows" тоже,
    # но мы его не включаем в итоговый DataFrame — просто исключаем
    df = pd.DataFrame([{k: r[k] for k in cols} for r in rows])

    out = Path(__file__).parent / "staged_entry_results.csv"
    df.to_csv(out, index=False)
    print(f"\nСохранено: {out}")

    # ── Таблицы ───────────────────────────────────────────────────────────────
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 20)
    pd.set_option("display.max_colwidth", 30)

    df_full = (
        df[df["period"] == "full"]
        .sort_values("calmar", ascending=False)
        .reset_index(drop=True)
    )
    df_90 = (
        df[df["period"] == "last_90d"]
        .sort_values("calmar", ascending=False)
        .reset_index(drop=True)
    )

    print("\n" + "=" * 160)
    print("FULL PERIOD — top 15 по calmar")
    print("=" * 160)
    print(df_full[cols].head(15).to_string(index=False))

    print("\n" + "=" * 160)
    print("LAST 90 DAYS — top 15 по calmar")
    print("=" * 160)
    print(df_90[cols].head(15).to_string(index=False))


if __name__ == "__main__":
    main()
