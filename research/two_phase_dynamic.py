"""
Two-Phase Exit + Dynamic Min-Hold стратегия.

Комбинирует:
  - Two-phase exit логику из two_phase_exit.py
  - Per-position динамический min_hold из dynamic_min_hold.py

Идея: при низком entry rate (напр. 0.08) position_min_hold автоматически
вырастает до сотен часов (нужно долго держать чтобы окупить fees),
что предотвращает срабатывание phase1 exit через 24h на шуме.
Two-phase exit логика применяется ПОСЛЕ того как position_min_hold выполнен.

Формула min_hold:
    breakeven_h = 18.4 / entry_rate_annual   # 18.4 = 0.0021 × 8760 × (TOTAL_FEES_CYCLE / POSITION_SIZE) × HOURS_PER_YEAR
    position_min_hold = min(cap_min_hold, max(base_min_hold, safety_mult × breakeven_h))

При entry_rate <= 0: position_min_hold = cap_min_hold.
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
from dynamic_min_hold import simulate_dynamic_min_hold
from two_phase_exit import simulate_two_phase_exit
from adaptive_entry import metrics_window

# Полные fees за цикл (open + close обеих ног)
TOTAL_FEES_CYCLE = POSITION_SIZE * (PERP_TAKER + SPOT_TAKER) * 2  # $4.20

# Константа для расчёта breakeven: TOTAL_FEES_CYCLE / (POSITION_SIZE / HOURS_PER_YEAR)
# = (POSITION_SIZE * (PERP_TAKER + SPOT_TAKER) * 2) / (POSITION_SIZE / HOURS_PER_YEAR)
# = (PERP_TAKER + SPOT_TAKER) * 2 * HOURS_PER_YEAR
# = 0.00105 * 2 * 8760 = 18.396 ≈ 18.4
BREAKEVEN_CONST = (PERP_TAKER + SPOT_TAKER) * 2 * HOURS_PER_YEAR  # ≈ 18.4


def simulate_two_phase_dynamic(
    coins,
    max_concurrent: int,
    entry_threshold: float,
    signal_window: int,
    base_min_hold: int,
    safety_mult: float,
    cap_min_hold: int,
    phase1_negative_patience: int,
    phase1_breakeven_cap_hours: int,
    phase2_exit_threshold: float,
) -> tuple:
    """
    Симулирует A_cycle с двухфазной exit-логикой + динамическим per-position min_hold.

    Phase 1: пока не окупили fees — терпим negative rate до patience часов,
             и выходим только если hours_to_breakeven > cap_hours.
    Phase 2: после окупания fees — выходим когда rate < phase2_exit_threshold.

    position_min_hold рассчитывается при входе на основе entry rate:
        breakeven_h = BREAKEVEN_CONST / entry_rate
        pos_min_hold = min(cap_min_hold, max(base_min_hold, safety_mult * breakeven_h))

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
            # Dynamic min_hold (per-position)
            "position_min_hold":    0,
        }

    pnl_per_hour   = np.zeros(n)
    cap_per_hour   = np.zeros(n)
    opens_per_hour = np.zeros(n, dtype=int)
    closed_hold_hours = []
    holds_assigned    = []

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

        # 2) Exit с двухфазной логикой (ворота = position_min_hold)
        for c, s in state.items():
            if not s["valid"][i] or not s["in_position"]:
                continue
            s["hours_since"] += 1
            s["hours_in"]    += 1

            # Динамический min_hold — не выходим раньше него
            if s["hours_since"] < s["position_min_hold"]:
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
                s["position_min_hold"]    = 0

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

                # Динамический min_hold на основе entry rate
                entry_rate = s["signal"][i]  # annualized signal (12h MA × 8760)
                if entry_rate > 0:
                    breakeven_h = BREAKEVEN_CONST / entry_rate
                    pos_min_hold = int(min(cap_min_hold, max(base_min_hold, safety_mult * breakeven_h)))
                else:
                    pos_min_hold = cap_min_hold
                s["position_min_hold"] = pos_min_hold
                holds_assigned.append(pos_min_hold)

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
    avg_min_hold_assigned = round(float(np.mean(holds_assigned)), 1) if holds_assigned else 0.0

    info = {
        "total_trades":          total_trades,
        "peak_capital":          cap_per_hour.max(),
        "opens_per_hour":        opens_per_hour,
        "avg_hold_hours":        avg_hold_h,
        "phase1_exits":          phase1_exits,
        "phase2_exits":          phase2_exits,
        "holds_assigned":        holds_assigned,
        "avg_min_hold_assigned": avg_min_hold_assigned,
    }
    return pnl_per_hour, cap_per_hour, info


def main():
    coins         = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
    K             = 3
    signal_window = 12
    base_min_hold = 24
    capital_base  = K * TOTAL_CAPITAL  # $6000

    rows = []

    # ── Baseline 1: COMBO ──────────────────────────────────────────────────────
    print("Running baseline_COMBO (entry=0.30, min_hold=120) ...")
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
            "label":                     "baseline_COMBO",
            "entry_threshold":           0.30,
            "safety_mult":               "n/a",
            "cap_min_hold":              "n/a",
            "phase1_negative_patience":  "n/a",
            "phase1_breakeven_cap_h":    "n/a",
            "phase2_exit_threshold":     "n/a",
            "period":                    period,
            "annual":                    m["annual"],
            "max_dd":                    m["max_dd"],
            "calmar":                    m["calmar"],
            "sharpe":                    m["sharpe"],
            "trades":                    m["trades"],
            "avg_hold_h":                120,
            "avg_min_hold_assigned":     "n/a",
            "tim_pct":                   m["time_in_market_pct"],
            "phase1_exits":              "n/a",
            "phase2_exits":              "n/a",
        })

    # ── Baseline 2: DynAgg ────────────────────────────────────────────────────
    print("Running baseline_DynAgg (entry=0.08, mult=5.0, cap=720) ...")
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
            "label":                     "baseline_DynAgg",
            "entry_threshold":           0.08,
            "safety_mult":               "n/a",
            "cap_min_hold":              "n/a",
            "phase1_negative_patience":  "n/a",
            "phase1_breakeven_cap_h":    "n/a",
            "phase2_exit_threshold":     "n/a",
            "period":                    period,
            "annual":                    m["annual"],
            "max_dd":                    m["max_dd"],
            "calmar":                    m["calmar"],
            "sharpe":                    m["sharpe"],
            "trades":                    m["trades"],
            "avg_hold_h":                avg_hold_b2,
            "avg_min_hold_assigned":     "n/a",
            "tim_pct":                   m["time_in_market_pct"],
            "phase1_exits":              "n/a",
            "phase2_exits":              "n/a",
        })

    # ── Baseline 3: TwoPhase ──────────────────────────────────────────────────
    print("Running baseline_TwoPhase (entry=0.15, p1_neg=24, p1_cap=480, p2=-0.10) ...")
    pnl_b3, cap_b3, info_b3 = simulate_two_phase_exit(
        coins,
        max_concurrent=K,
        entry_threshold=0.15,
        base_min_hold=base_min_hold,
        signal_window=signal_window,
        phase1_negative_patience=24,
        phase1_breakeven_cap_hours=480,
        phase2_exit_threshold=-0.10,
    )
    n = len(pnl_b3)
    opens_b3 = info_b3["opens_per_hour"]
    avg_hold_b3 = info_b3["avg_hold_hours"]
    p1_ex_b3  = info_b3["phase1_exits"]
    p2_ex_b3  = info_b3["phase2_exits"]
    for period, start_idx in [("full", 0), ("last_90d", max(0, n - 90 * 24))]:
        m = metrics_window(pnl_b3, cap_b3, opens_b3, capital_base, start_idx, n)
        rows.append({
            "label":                     "baseline_TwoPhase",
            "entry_threshold":           0.15,
            "safety_mult":               "n/a",
            "cap_min_hold":              "n/a",
            "phase1_negative_patience":  "n/a",
            "phase1_breakeven_cap_h":    "n/a",
            "phase2_exit_threshold":     "n/a",
            "period":                    period,
            "annual":                    m["annual"],
            "max_dd":                    m["max_dd"],
            "calmar":                    m["calmar"],
            "sharpe":                    m["sharpe"],
            "trades":                    m["trades"],
            "avg_hold_h":                avg_hold_b3,
            "avg_min_hold_assigned":     "n/a",
            "tim_pct":                   m["time_in_market_pct"],
            "phase1_exits":              p1_ex_b3,
            "phase2_exits":              p2_ex_b3,
        })

    # ── Sweep комбинированной стратегии ───────────────────────────────────────
    ENTRIES       = [0.08, 0.10, 0.15]
    SAFETY_MULTS  = [3.0, 5.0]
    CAP_MIN_HOLDS = [720]
    P1_NEG_LIST   = [24, 72]
    P1_CAP_LIST   = [480, 720]
    P2_EXIT_LIST  = [-0.10]

    sweep_configs = []
    for entry in ENTRIES:
        for sm in SAFETY_MULTS:
            for cap_mh in CAP_MIN_HOLDS:
                for p1n in P1_NEG_LIST:
                    for p1c in P1_CAP_LIST:
                        for p2e in P2_EXIT_LIST:
                            sweep_configs.append({
                                "entry": entry,
                                "sm":    sm,
                                "cap_mh": cap_mh,
                                "p1n":   p1n,
                                "p1c":   p1c,
                                "p2e":   p2e,
                            })

    total_configs = len(sweep_configs)
    for idx, cfg in enumerate(sweep_configs, 1):
        entry  = cfg["entry"]
        sm     = cfg["sm"]
        cap_mh = cfg["cap_mh"]
        p1n    = cfg["p1n"]
        p1c    = cfg["p1c"]
        p2e    = cfg["p2e"]
        print(
            f"[{idx}/{total_configs}] two_phase_dynamic "
            f"entry={entry} sm={sm} cap={cap_mh} p1n={p1n}h p1c={p1c}h p2e={p2e} ..."
        )
        pnl, cap, info = simulate_two_phase_dynamic(
            coins,
            max_concurrent=K,
            entry_threshold=entry,
            signal_window=signal_window,
            base_min_hold=base_min_hold,
            safety_mult=sm,
            cap_min_hold=cap_mh,
            phase1_negative_patience=p1n,
            phase1_breakeven_cap_hours=p1c,
            phase2_exit_threshold=p2e,
        )
        n = len(pnl)
        opens_ph              = info["opens_per_hour"]
        avg_hold              = info["avg_hold_hours"]
        p1_ex                 = info["phase1_exits"]
        p2_ex                 = info["phase2_exits"]
        avg_min_hold_assigned = info["avg_min_hold_assigned"]

        for period, start_idx in [("full", 0), ("last_90d", max(0, n - 90 * 24))]:
            m = metrics_window(pnl, cap, opens_ph, capital_base, start_idx, n)
            rows.append({
                "label":                     "two_phase_dynamic",
                "entry_threshold":           entry,
                "safety_mult":               sm,
                "cap_min_hold":              cap_mh,
                "phase1_negative_patience":  p1n,
                "phase1_breakeven_cap_h":    p1c,
                "phase2_exit_threshold":     p2e,
                "period":                    period,
                "annual":                    m["annual"],
                "max_dd":                    m["max_dd"],
                "calmar":                    m["calmar"],
                "sharpe":                    m["sharpe"],
                "trades":                    m["trades"],
                "avg_hold_h":                avg_hold,
                "avg_min_hold_assigned":     avg_min_hold_assigned,
                "tim_pct":                   m["time_in_market_pct"],
                "phase1_exits":              p1_ex,
                "phase2_exits":              p2_ex,
            })

    # ── Сохранение ────────────────────────────────────────────────────────────
    df = pd.DataFrame(rows, columns=[
        "label", "entry_threshold", "safety_mult", "cap_min_hold",
        "phase1_negative_patience", "phase1_breakeven_cap_h", "phase2_exit_threshold",
        "period", "annual", "max_dd", "calmar", "sharpe",
        "trades", "avg_hold_h", "avg_min_hold_assigned", "tim_pct",
        "phase1_exits", "phase2_exits",
    ])
    out = Path(__file__).parent / "two_phase_dynamic_results.csv"
    df.to_csv(out, index=False)
    print(f"\nСохранено: {out}  ({len(df)} строк)")

    # ── Таблицы ───────────────────────────────────────────────────────────────
    pd.set_option("display.width", 260)
    pd.set_option("display.max_columns", 20)

    df_full = df[df["period"] == "full"].sort_values("calmar", ascending=False).reset_index(drop=True)
    df_90   = df[df["period"] == "last_90d"].sort_values("calmar", ascending=False).reset_index(drop=True)

    print("\n" + "=" * 200)
    print("FULL PERIOD — top 15 по calmar")
    print("=" * 200)
    print(df_full.head(15).to_string(index=False))

    print("\n" + "=" * 200)
    print("LAST 90 DAYS — top 15 по calmar")
    print("=" * 200)
    print(df_90.head(15).to_string(index=False))


if __name__ == "__main__":
    main()
