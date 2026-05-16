"""
Two-Phase Exit стратегия.

Стратегия находится в одной из двух фаз:

Phase 1 — Break-even (gross_funding_so_far < total_fees_paid):
    Цель: окупить fees ($4.20 за цикл).
    Exit ТОЛЬКО если:
      a. consecutive_negative_hours > phase1_negative_patience — rate стабильно
         негативный, уже не верим что флипнет
      b. hours_to_breakeven_at_current_rate > phase1_breakeven_cap_hours — текущая
         ставка слишком мала чтобы окупить за разумное время
    Никаких выходов на «маленький положительный rate» — терпим.

Phase 2 — Profit (gross_funding_so_far >= total_fees_paid):
    Цель: максимизировать прибыль, не сидеть в плохой позиции.
    Exit когда:
      current_smoothed_rate < phase2_exit_threshold

Параметры:
    phase1_negative_patience    — ч. отрицательного rate подряд до сдачи
    phase1_breakeven_cap_hours  — макс. часов до break-even при текущем rate
    phase2_exit_threshold       — annualized rate ниже которого выходим из profit
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
from breakeven_exit import simulate_breakeven_exit
from adaptive_entry import metrics_window

# Полные fees за цикл (open + close обеих ног)
TOTAL_FEES_CYCLE = POSITION_SIZE * (PERP_TAKER + SPOT_TAKER) * 2  # $4.20


def simulate_two_phase_exit(
    coins,
    max_concurrent: int,
    entry_threshold: float,
    base_min_hold: int,
    signal_window: int,
    phase1_negative_patience: int,
    phase1_breakeven_cap_hours: int,
    phase2_exit_threshold: float,
):
    """
    Симулирует A_cycle с двухфазной exit-логикой.

    Phase 1: пока не окупили fees — терпим negative rate до patience часов,
             и выходим только если hours_to_breakeven > cap_hours.
    Phase 2: после окупания fees — выходим когда rate < phase2_exit_threshold.

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
            "rates":                rates,
            "close":                close,
            "signal":               sig,
            "valid":                ~np.isnan(close) & ~np.isnan(rates),
            "in_position":          False,
            "short_size":           0.0,
            "units_spot":           0.0,
            "entry_price":          0.0,
            "hours_since":          0,
            "cash":                 TOTAL_CAPITAL,
            "equity_prev":          TOTAL_CAPITAL,
            "trades":               0,
            "hours_in":             0,
            # Two-phase state
            "gross_funding_so_far": 0.0,
            "total_fees_paid":      0.0,
            "consec_negative":      0,
        }

    pnl_per_hour   = np.zeros(n)
    cap_per_hour   = np.zeros(n)
    opens_per_hour = np.zeros(n, dtype=int)
    closed_hold_hours = []

    # Счётчики для статистики
    phase1_exits = 0
    phase2_exits = 0

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

        # 2) Exit с двухфазной логикой
        for c, s in state.items():
            if not s["valid"][i] or not s["in_position"]:
                continue
            s["hours_since"] += 1
            s["hours_in"]    += 1

            # Минимум base_min_hold — не выходим
            if s["hours_since"] < base_min_hold:
                continue

            current_rate_annual  = s["signal"][i]   # annualized smoothed
            current_hourly_income = POSITION_SIZE * current_rate_annual / HOURS_PER_YEAR

            # Обновляем счётчик подряд-негативных часов
            if current_rate_annual < 0:
                s["consec_negative"] += 1
            else:
                s["consec_negative"] = 0

            # Определяем фазу
            in_profit = s["gross_funding_so_far"] >= s["total_fees_paid"]

            should_exit = False
            if not in_profit:
                # PHASE 1 — стараемся окупить fees
                if s["consec_negative"] > phase1_negative_patience:
                    # Rate устойчиво негативный, сдаёмся
                    should_exit = True
                elif current_hourly_income > 0:
                    remaining_to_breakeven = s["total_fees_paid"] - s["gross_funding_so_far"]
                    hours_to_breakeven = remaining_to_breakeven / current_hourly_income
                    if hours_to_breakeven > phase1_breakeven_cap_hours:
                        # При текущем rate слишком долго окупать — выходим
                        should_exit = True
                # Если rate=0 или чуть положительный, но в рамках patience — терпим
            else:
                # PHASE 2 — уже в плюсе, смотрим на threshold
                if current_rate_annual < phase2_exit_threshold:
                    should_exit = True

            if should_exit:
                P = s["close"][i]
                closed_hold_hours.append(s["hours_since"])
                # Запоминаем в какой фазе закрылись
                if not in_profit:
                    phase1_exits += 1
                else:
                    phase2_exits += 1
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
                s["consec_negative"]      = 0

        # 3) Подсчёт текущих позиций
        active     = [c for c, s in state.items() if s["in_position"]]
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
                s["units_spot"]           = POSITION_SIZE / P
                s["cash"]                -= POSITION_SIZE
                s["cash"]                -= POSITION_SIZE * SPOT_TAKER
                # Открыть short
                s["short_size"]           = POSITION_SIZE / P
                s["entry_price"]          = P
                s["cash"]                -= POSITION_SIZE * PERP_TAKER
                s["in_position"]          = True
                s["hours_since"]          = 0
                s["gross_funding_so_far"] = 0.0
                s["total_fees_paid"]      = TOTAL_FEES_CYCLE
                s["consec_negative"]      = 0
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
        s["cash"] += s["short_size"] * (s["entry_price"] - P)
        s["cash"] -= s["short_size"] * P * PERP_TAKER
        s["cash"] += s["units_spot"] * P
        s["cash"] -= s["units_spot"] * P * SPOT_TAKER
        s["short_size"] = 0.0
        s["units_spot"] = 0.0
        equity_now = s["cash"]
        pnl_per_hour[-1] += equity_now - s["equity_prev"]
        s["equity_prev"] = equity_now

    total_trades  = sum(s["trades"] for s in state.values())
    avg_hold_h    = round(float(np.mean(closed_hold_hours)), 1) if closed_hold_hours else 0.0

    info = {
        "total_trades":    total_trades,
        "peak_capital":    cap_per_hour.max(),
        "opens_per_hour":  opens_per_hour,
        "avg_hold_hours":  avg_hold_h,
        "phase1_exits":    phase1_exits,
        "phase2_exits":    phase2_exits,
    }
    return pnl_per_hour, cap_per_hour, info


def main():
    coins         = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
    K             = 3
    signal_window = 12
    base_min_hold = 24
    capital_base  = K * TOTAL_CAPITAL  # $6000

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
            "label":                    "baseline_COMBO",
            "entry_threshold":          0.30,
            "phase1_negative_patience": "n/a",
            "phase1_breakeven_cap_h":   "n/a",
            "phase2_exit_threshold":    "n/a",
            "period":                   period,
            "annual":                   m["annual"],
            "max_dd":                   m["max_dd"],
            "calmar":                   m["calmar"],
            "sharpe":                   m["sharpe"],
            "trades":                   m["trades"],
            "avg_hold_h":               120,
            "tim_pct":                  m["time_in_market_pct"],
            "phase1_exits":             "n/a",
            "phase2_exits":             "n/a",
        })

    print("Running baseline: dynamic_aggressive (entry=0.08, mult=5, cap=720) ...")
    pnl_b2, cap_b2, info_b2 = simulate_dynamic_min_hold(
        coins,
        max_concurrent=K,
        entry_threshold=0.08,
        exit_threshold=-0.15,
        base_min_hold=base_min_hold,
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
            "label":                    "baseline_DynAgg",
            "entry_threshold":          0.08,
            "phase1_negative_patience": "n/a",
            "phase1_breakeven_cap_h":   "n/a",
            "phase2_exit_threshold":    "n/a",
            "period":                   period,
            "annual":                   m["annual"],
            "max_dd":                   m["max_dd"],
            "calmar":                   m["calmar"],
            "sharpe":                   m["sharpe"],
            "trades":                   m["trades"],
            "avg_hold_h":               avg_hold_b2,
            "tim_pct":                  m["time_in_market_pct"],
            "phase1_exits":             "n/a",
            "phase2_exits":             "n/a",
        })

    print("Running baseline: breakeven_exit (entry=0.15, min_hold=48, cap=720) ...")
    pnl_b3, cap_b3, info_b3 = simulate_breakeven_exit(
        coins,
        max_concurrent=K,
        entry_threshold=0.15,
        base_min_hold=48,
        signal_window=signal_window,
        breakeven_cap_hours=720,
    )
    n = len(pnl_b3)
    opens_b3 = info_b3["opens_per_hour"]
    avg_hold_b3 = info_b3["avg_hold_hours"]
    for period, start_idx in [("full", 0), ("last_90d", max(0, n - 90 * 24))]:
        m = metrics_window(pnl_b3, cap_b3, opens_b3, capital_base, start_idx, n)
        rows.append({
            "label":                    "baseline_breakeven",
            "entry_threshold":          0.15,
            "phase1_negative_patience": "n/a",
            "phase1_breakeven_cap_h":   720,
            "phase2_exit_threshold":    "n/a",
            "period":                   period,
            "annual":                   m["annual"],
            "max_dd":                   m["max_dd"],
            "calmar":                   m["calmar"],
            "sharpe":                   m["sharpe"],
            "trades":                   m["trades"],
            "avg_hold_h":               avg_hold_b3,
            "tim_pct":                  m["time_in_market_pct"],
            "phase1_exits":             "n/a",
            "phase2_exits":             "n/a",
        })

    # ── Two-phase sweep ────────────────────────────────────────────────────────
    # Variant 1: entry=0.08, sweep phase1_neg × phase2_exit (фикс phase1_cap=480)
    # Variant 2: entry=0.15, sweep phase1_neg × phase2_exit (фикс phase1_cap=480)
    # Variant 3: entry=0.08, фикс phase1_neg=24, phase2=-0.05, sweep phase1_cap

    PHASE1_NEG_PATIENCE_LIST = [12, 24, 72]       # 0.5d / 1d / 3d
    PHASE2_EXIT_LIST         = [0.0, -0.05, -0.10]

    sweep_configs = []

    # Variant 1 & 2: entry × phase1_neg × phase2_exit
    for entry_thr in [0.08, 0.15]:
        for p1_neg in PHASE1_NEG_PATIENCE_LIST:
            for p2_exit in PHASE2_EXIT_LIST:
                sweep_configs.append({
                    "entry":    entry_thr,
                    "p1_neg":  p1_neg,
                    "p1_cap":  480,
                    "p2_exit": p2_exit,
                })

    # Variant 3: entry=0.08, sweep phase1_cap
    for p1_cap in [240, 480, 720]:
        sweep_configs.append({
            "entry":    0.08,
            "p1_neg":  24,
            "p1_cap":  p1_cap,
            "p2_exit": -0.05,
        })

    # Deduplicate
    seen = set()
    unique_configs = []
    for cfg in sweep_configs:
        key = (cfg["entry"], cfg["p1_neg"], cfg["p1_cap"], cfg["p2_exit"])
        if key not in seen:
            seen.add(key)
            unique_configs.append(cfg)

    total_configs = len(unique_configs)
    for idx, cfg in enumerate(unique_configs, 1):
        entry_thr = cfg["entry"]
        p1_neg    = cfg["p1_neg"]
        p1_cap    = cfg["p1_cap"]
        p2_exit   = cfg["p2_exit"]
        print(
            f"[{idx}/{total_configs}] two_phase "
            f"entry={entry_thr} p1_neg={p1_neg}h p1_cap={p1_cap}h p2_exit={p2_exit} ..."
        )
        pnl, cap, info = simulate_two_phase_exit(
            coins,
            max_concurrent=K,
            entry_threshold=entry_thr,
            base_min_hold=base_min_hold,
            signal_window=signal_window,
            phase1_negative_patience=p1_neg,
            phase1_breakeven_cap_hours=p1_cap,
            phase2_exit_threshold=p2_exit,
        )
        n = len(pnl)
        opens_ph  = info["opens_per_hour"]
        avg_hold  = info["avg_hold_hours"]
        p1_ex     = info["phase1_exits"]
        p2_ex     = info["phase2_exits"]

        for period, start_idx in [("full", 0), ("last_90d", max(0, n - 90 * 24))]:
            m = metrics_window(pnl, cap, opens_ph, capital_base, start_idx, n)
            rows.append({
                "label":                    "two_phase",
                "entry_threshold":          entry_thr,
                "phase1_negative_patience": p1_neg,
                "phase1_breakeven_cap_h":   p1_cap,
                "phase2_exit_threshold":    p2_exit,
                "period":                   period,
                "annual":                   m["annual"],
                "max_dd":                   m["max_dd"],
                "calmar":                   m["calmar"],
                "sharpe":                   m["sharpe"],
                "trades":                   m["trades"],
                "avg_hold_h":               avg_hold,
                "tim_pct":                  m["time_in_market_pct"],
                "phase1_exits":             p1_ex,
                "phase2_exits":             p2_ex,
            })

    # ── Сохранение ────────────────────────────────────────────────────────────
    df = pd.DataFrame(rows, columns=[
        "label", "entry_threshold",
        "phase1_negative_patience", "phase1_breakeven_cap_h", "phase2_exit_threshold",
        "period", "annual", "max_dd", "calmar", "sharpe",
        "trades", "avg_hold_h", "tim_pct", "phase1_exits", "phase2_exits",
    ])
    out = Path(__file__).parent / "two_phase_exit_results.csv"
    df.to_csv(out, index=False)
    print(f"\nСохранено: {out}")

    # ── Таблицы ───────────────────────────────────────────────────────────────
    pd.set_option("display.width", 260)
    pd.set_option("display.max_columns", 20)

    cols_show = [
        "label", "entry_threshold",
        "phase1_negative_patience", "phase1_breakeven_cap_h", "phase2_exit_threshold",
        "period", "annual", "max_dd", "calmar", "sharpe",
        "trades", "avg_hold_h", "tim_pct", "phase1_exits", "phase2_exits",
    ]

    df_full = df[df["period"] == "full"].sort_values("calmar", ascending=False).reset_index(drop=True)
    df_90   = df[df["period"] == "last_90d"].sort_values("calmar", ascending=False).reset_index(drop=True)

    print("\n" + "=" * 200)
    print("FULL PERIOD — top 15 по calmar")
    print("=" * 200)
    print(df_full[cols_show].head(15).to_string(index=False))

    print("\n" + "=" * 200)
    print("LAST 90 DAYS — top 15 по calmar")
    print("=" * 200)
    print(df_90[cols_show].head(15).to_string(index=False))


if __name__ == "__main__":
    main()
