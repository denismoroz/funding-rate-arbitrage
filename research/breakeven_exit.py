"""
Continuous Break-Even Exit стратегия.

На каждом часу пересчитываем «сколько часов нужно чтобы окупить fees при текущем rate».
Выходим когда эта оценка уходит в бесконечность (rate <= 0) или превышает cap.

Математика:
    total_fees_cycle = POSITION_SIZE * (PERP_TAKER + SPOT_TAKER) * 2  = $4.20 на $1000
    remaining_to_breakeven = total_fees_cycle - gross_funding_so_far
    current_hourly_income  = POSITION_SIZE * current_smoothed_rate / HOURS_PER_YEAR
    hours_to_breakeven     = remaining / income  (inf если income <= 0)

Логика exit:
    - hours_since < base_min_hold              → keep
    - current_rate_annual <= 0                 → EXIT
    - remaining_to_breakeven <= 0             → keep (уже в плюсе, rate > 0)
    - hours_to_breakeven > breakeven_cap_hours → EXIT
    - else                                     → keep
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


# Полные fees за цикл (open + close обеих ног)
TOTAL_FEES_CYCLE = POSITION_SIZE * (PERP_TAKER + SPOT_TAKER) * 2  # $4.20


def simulate_breakeven_exit(
    coins,
    max_concurrent: int,
    entry_threshold: float,
    base_min_hold: int,
    signal_window: int,
    breakeven_cap_hours: int,
):
    """
    Симулирует A_cycle с continuous break-even exit логикой.

    На каждом часу считаем hours_to_breakeven при текущем smoothed rate.
    Выходим если rate <= 0 или hours_to_breakeven > breakeven_cap_hours.
    После достижения break-even продолжаем пока rate > 0 (чистая прибыль).

    Возвращает (pnl_per_hour, cap_per_hour, info).
    """
    # Загрузка данных
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

    # Инициализация state
    state = {}
    for c, df in datas.items():
        df2 = df.reindex(common_idx)
        rates = df2["fundingRate"].values
        close = df2["close"].values
        if signal_window > 1:
            sig = pd.Series(rates).rolling(signal_window, min_periods=1).mean().values * HOURS_PER_YEAR
        else:
            sig = rates * HOURS_PER_YEAR
        state[c] = {
            "rates":                 rates,
            "close":                 close,
            "signal":                sig,
            "valid":                 ~np.isnan(close) & ~np.isnan(rates),
            "in_position":           False,
            "short_size":            0.0,
            "units_spot":            0.0,
            "entry_price":           0.0,
            "hours_since":           0,
            "cash":                  TOTAL_CAPITAL,
            "equity_prev":           TOTAL_CAPITAL,
            "trades":                0,
            "hours_in":              0,
            # Break-even state
            "gross_funding_so_far":  0.0,
            "total_fees_paid":       0.0,
            # Статистика для pct_breakeven_reached
            "breakeven_reached_count": 0,
            "closed_trades_count":     0,
        }

    pnl_per_hour   = np.zeros(n)
    cap_per_hour   = np.zeros(n)
    opens_per_hour = np.zeros(n, dtype=int)
    closed_hold_hours = []

    for i in range(n):
        # 1) Funding для всех уже-в-позиции + обновляем gross_funding_so_far
        for c, s in state.items():
            if not s["valid"][i]:
                continue
            if s["in_position"]:
                P = s["close"][i]
                r = s["rates"][i]
                hourly_funding = s["short_size"] * P * r
                s["cash"]                += hourly_funding
                s["gross_funding_so_far"] += hourly_funding

        # 2) Exit с break-even логикой
        for c, s in state.items():
            if not s["valid"][i] or not s["in_position"]:
                continue
            s["hours_since"] += 1
            s["hours_in"]    += 1

            # Минимум base_min_hold — не выходим
            if s["hours_since"] < base_min_hold:
                continue

            current_rate_annual  = s["signal"][i]           # annualized (smoothed)
            current_rate_smoothed = current_rate_annual / HOURS_PER_YEAR  # почасовой
            current_hourly_income = POSITION_SIZE * current_rate_smoothed
            remaining_to_breakeven = s["total_fees_paid"] - s["gross_funding_so_far"]

            should_exit = False
            if current_rate_annual <= 0:
                # Rate отрицательный — точно не зарабатываем, выходим
                should_exit = True
            elif remaining_to_breakeven <= 0:
                # Уже в плюсе — продолжаем пока rate > 0 (уже прошли первый if)
                should_exit = False
            else:
                hours_to_breakeven = remaining_to_breakeven / current_hourly_income
                if hours_to_breakeven > breakeven_cap_hours:
                    should_exit = True

            if should_exit:
                P = s["close"][i]
                closed_hold_hours.append(s["hours_since"])
                # Статистика breakeven
                s["closed_trades_count"] += 1
                if s["gross_funding_so_far"] >= s["total_fees_paid"]:
                    s["breakeven_reached_count"] += 1
                # Закрыть short
                s["cash"] += s["short_size"] * (s["entry_price"] - P)
                s["cash"] -= s["short_size"] * P * PERP_TAKER
                # Продать spot
                s["cash"] += s["units_spot"] * P
                s["cash"] -= s["units_spot"] * P * SPOT_TAKER
                # Сброс state
                s["short_size"]           = 0.0
                s["units_spot"]           = 0.0
                s["entry_price"]          = 0.0
                s["in_position"]          = False
                s["gross_funding_so_far"] = 0.0
                s["total_fees_paid"]      = 0.0

        # 3) Подсчёт текущих позиций
        active    = [c for c, s in state.items() if s["in_position"]]
        slots_free = max_concurrent - len(active)

        # 4) Кандидаты на вход — top-K по signal
        if slots_free > 0:
            candidates = []
            for c, s in state.items():
                if not s["valid"][i] or s["in_position"]:
                    continue
                if s["signal"][i] > entry_threshold:
                    candidates.append((c, s["signal"][i]))
            candidates.sort(key=lambda x: -x[1])
            for c, _ in candidates[:slots_free]:
                s = state[c]
                P = s["close"][i]
                # Купить spot
                s["units_spot"]  = POSITION_SIZE / P
                s["cash"]       -= POSITION_SIZE
                s["cash"]       -= POSITION_SIZE * SPOT_TAKER
                # Открыть short
                s["short_size"]           = POSITION_SIZE / P
                s["entry_price"]          = P
                s["cash"]                -= POSITION_SIZE * PERP_TAKER
                s["in_position"]          = True
                s["hours_since"]          = 0
                s["gross_funding_so_far"] = 0.0
                s["total_fees_paid"]      = TOTAL_FEES_CYCLE
                s["trades"]              += 1
                opens_per_hour[i]        += 1

        # 5) MTM equity
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

    # Финал: закрыть всё открытое
    for c, s in state.items():
        if not s["in_position"]:
            continue
        valid_close = s["close"][s["valid"]]
        if len(valid_close) == 0:
            continue
        P = valid_close[-1]
        s["closed_trades_count"] += 1
        if s["gross_funding_so_far"] >= s["total_fees_paid"]:
            s["breakeven_reached_count"] += 1
        s["cash"] += s["short_size"] * (s["entry_price"] - P)
        s["cash"] -= s["short_size"] * P * PERP_TAKER
        s["cash"] += s["units_spot"] * P
        s["cash"] -= s["units_spot"] * P * SPOT_TAKER
        s["short_size"] = 0.0
        s["units_spot"] = 0.0
        equity_now = s["cash"]
        pnl_per_hour[-1] += equity_now - s["equity_prev"]
        s["equity_prev"] = equity_now

    total_trades          = sum(s["trades"] for s in state.values())
    total_closed          = sum(s["closed_trades_count"] for s in state.values())
    total_breakeven       = sum(s["breakeven_reached_count"] for s in state.values())
    pct_breakeven_reached = round(total_breakeven / total_closed * 100, 1) if total_closed > 0 else 0.0
    avg_hold_h = round(float(np.mean(closed_hold_hours)), 1) if closed_hold_hours else 0.0

    info = {
        "total_trades":          total_trades,
        "peak_capital":          cap_per_hour.max(),
        "opens_per_hour":        opens_per_hour,
        "avg_hold_hours":        avg_hold_h,
        "pct_breakeven_reached": pct_breakeven_reached,
    }
    return pnl_per_hour, cap_per_hour, info


def main():
    coins        = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
    K            = 3
    signal_window = 12
    capital_base  = K * TOTAL_CAPITAL   # $6000

    # ── Sweep параметры ────────────────────────────────────────────────────────
    ENTRY_THRESHOLDS    = [0.05, 0.08, 0.15]
    BASE_MIN_HOLDS      = [24, 48, 72]
    BREAKEVEN_CAP_HOURS = [120, 240, 480, 720]

    rows = []

    # ── Baselines ──────────────────────────────────────────────────────────────
    print("Running baseline: old-prod COMBO (entry=0.30, min_hold=120) ...")
    pnl_b1, cap_b1, info_b1 = simulate_multi_capped(
        coins,
        max_concurrent=K,
        entry_threshold=0.30,
        exit_threshold=-0.15,
        min_hold=120,
        signal_window=signal_window,
    )
    n = len(pnl_b1)
    cap_diff = np.diff(cap_b1, prepend=0)
    opens_b1 = np.where(cap_diff > 0, (cap_diff / TOTAL_CAPITAL).astype(int), 0)
    for period, start_idx in [("full", 0), ("last_90d", max(0, n - 90 * 24))]:
        m = metrics_window(pnl_b1, cap_b1, opens_b1, capital_base, start_idx, n)
        rows.append({
            "entry_threshold":       "baseline_COMBO",
            "base_min_hold":         120,
            "breakeven_cap_hours":   "n/a",
            "period":                period,
            "annual":                m["annual"],
            "max_dd":                m["max_dd"],
            "calmar":                m["calmar"],
            "sharpe":                m["sharpe"],
            "trades":                m["trades"],
            "avg_hold_h":            120,
            "tim_pct":               m["time_in_market_pct"],
            "pct_breakeven_reached": "n/a",
        })

    print("Running baseline: dynamic_aggressive (entry=0.08, mult=5, cap=720) ...")
    pnl_b2, cap_b2, info_b2 = simulate_dynamic_min_hold(
        coins,
        max_concurrent=K,
        entry_threshold=0.08,
        exit_threshold=-0.15,
        base_min_hold=24,
        signal_window=signal_window,
        safety_mult=5.0,
        cap_min_hold=720,
    )
    n = len(pnl_b2)
    opens_b2 = info_b2["opens_per_hour"]
    holds_b2 = info_b2["holds_collected"]
    avg_hold_b2 = round(float(np.mean(holds_b2)), 1) if holds_b2 else 0.0
    for period, start_idx in [("full", 0), ("last_90d", max(0, n - 90 * 24))]:
        m = metrics_window(pnl_b2, cap_b2, opens_b2, capital_base, start_idx, n)
        rows.append({
            "entry_threshold":       "baseline_DynAgg",
            "base_min_hold":         24,
            "breakeven_cap_hours":   "n/a",
            "period":                period,
            "annual":                m["annual"],
            "max_dd":                m["max_dd"],
            "calmar":                m["calmar"],
            "sharpe":                m["sharpe"],
            "trades":                m["trades"],
            "avg_hold_h":            avg_hold_b2,
            "tim_pct":               m["time_in_market_pct"],
            "pct_breakeven_reached": "n/a",
        })

    # ── Breakeven sweep ────────────────────────────────────────────────────────
    total_configs = len(ENTRY_THRESHOLDS) * len(BASE_MIN_HOLDS) * len(BREAKEVEN_CAP_HOURS)
    done = 0
    for entry_thr in ENTRY_THRESHOLDS:
        for base_mh in BASE_MIN_HOLDS:
            for cap_h in BREAKEVEN_CAP_HOURS:
                done += 1
                print(f"[{done}/{total_configs}] breakeven entry={entry_thr} min_hold={base_mh}h cap={cap_h}h ...")
                pnl, cap, info = simulate_breakeven_exit(
                    coins,
                    max_concurrent=K,
                    entry_threshold=entry_thr,
                    base_min_hold=base_mh,
                    signal_window=signal_window,
                    breakeven_cap_hours=cap_h,
                )
                n = len(pnl)
                opens_ph = info["opens_per_hour"]
                avg_hold = info["avg_hold_hours"]
                pct_be   = info["pct_breakeven_reached"]

                for period, start_idx in [("full", 0), ("last_90d", max(0, n - 90 * 24))]:
                    m = metrics_window(pnl, cap, opens_ph, capital_base, start_idx, n)
                    rows.append({
                        "entry_threshold":       entry_thr,
                        "base_min_hold":         base_mh,
                        "breakeven_cap_hours":   cap_h,
                        "period":                period,
                        "annual":                m["annual"],
                        "max_dd":                m["max_dd"],
                        "calmar":                m["calmar"],
                        "sharpe":                m["sharpe"],
                        "trades":                m["trades"],
                        "avg_hold_h":            avg_hold,
                        "tim_pct":               m["time_in_market_pct"],
                        "pct_breakeven_reached": pct_be,
                    })

    # ── Сохранение ────────────────────────────────────────────────────────────
    df = pd.DataFrame(rows, columns=[
        "entry_threshold", "base_min_hold", "breakeven_cap_hours", "period",
        "annual", "max_dd", "calmar", "sharpe", "trades",
        "avg_hold_h", "tim_pct", "pct_breakeven_reached",
    ])
    out = Path(__file__).parent / "breakeven_exit_results.csv"
    df.to_csv(out, index=False)
    print(f"\nСохранено: {out}")

    # ── Таблицы ───────────────────────────────────────────────────────────────
    pd.set_option("display.width", 240)
    pd.set_option("display.max_columns", 20)

    cols_show = [
        "entry_threshold", "base_min_hold", "breakeven_cap_hours", "period",
        "annual", "max_dd", "calmar", "sharpe", "trades",
        "avg_hold_h", "tim_pct", "pct_breakeven_reached",
    ]

    df_full = df[df["period"] == "full"].sort_values("calmar", ascending=False).reset_index(drop=True)
    df_90   = df[df["period"] == "last_90d"].sort_values("calmar", ascending=False).reset_index(drop=True)

    print("\n" + "=" * 170)
    print("FULL PERIOD — top 15 по calmar")
    print("=" * 170)
    print(df_full[cols_show].head(15).to_string(index=False))

    print("\n" + "=" * 170)
    print("LAST 90 DAYS — top 15 по calmar")
    print("=" * 170)
    print(df_90[cols_show].head(15).to_string(index=False))


if __name__ == "__main__":
    main()
