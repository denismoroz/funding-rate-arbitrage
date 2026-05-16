"""
Math-derived entry threshold — вход без хардкода entry_threshold.

Порог выводится из физики комиссий:
    natural_floor = safety_mult × 18.4 / cap_min_hold   (в долях, годовых)

На ставках ниже natural_floor невозможно окупить fees с заданным safety margin
даже на максимальном hold = cap_min_hold.

Это устраняет внутреннее противоречие dynamic_aggressive (entry=0.08, mult=5, cap=720):
при rate 8-12.8% формула хочет hold > 720, но cap режет до 720 — safety < 5x.
Math-derived гарантирует, что в момент входа safety_mult ВСЕГДА выполнима.
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
from dynamic_min_hold import simulate_dynamic_min_hold
from adaptive_entry import metrics_window


BREAKEVEN_CONST = 18.4  # = 0.0021 × 8760


def simulate_math_derived(
    coins,
    max_concurrent: int,
    exit_threshold: float,
    base_min_hold: int,
    signal_window: int,
    safety_mult: float,
    cap_min_hold: int,
):
    """
    Симулирует A_cycle с math-derived entry threshold.

    natural_floor = safety_mult * BREAKEVEN_CONST / cap_min_hold

    Открываемся ТОЛЬКО когда signal > natural_floor (т.е. safety_mult
    гарантированно достигается при hold <= cap_min_hold).

    После входа: dynamic min_hold = min(cap_min_hold, max(base_min_hold, safety_mult * 18.4 / rate))
    — идентично simulate_dynamic_min_hold, но entry_threshold = natural_floor.
    """
    natural_floor = safety_mult * BREAKEVEN_CONST / cap_min_hold

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
                s["cash"] += s["short_size"] * (s["entry_price"] - P)
                s["cash"] -= s["short_size"] * P * PERP_TAKER
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

        # 4) Кандидаты на вход — signal > natural_floor (math-derived!)
        if slots_free > 0:
            candidates = []
            for c, s in state.items():
                if not s["valid"][i] or s["in_position"]:
                    continue
                if s["signal"][i] > natural_floor:
                    candidates.append((c, s["signal"][i]))
            candidates.sort(key=lambda x: -x[1])  # сильнейший первым
            for c, _ in candidates[:slots_free]:
                s = state[c]
                P = s["close"][i]

                # Динамический min_hold на основе entry rate
                entry_rate = s["signal"][i]
                if entry_rate > 0:
                    breakeven_h = BREAKEVEN_CONST / entry_rate
                    pos_min_hold = int(min(cap_min_hold,
                                          max(base_min_hold,
                                              safety_mult * breakeven_h)))
                else:
                    pos_min_hold = cap_min_hold

                entry_rates_collected.append(entry_rate)
                holds_collected.append(pos_min_hold)

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
        "natural_floor":         natural_floor,
    }
    return pnl_per_hour, cap_per_hour, info


def main():
    U7  = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
    U11 = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE",
           "UNI", "ARB", "OP", "TIA"]

    K              = 3
    exit_threshold = -0.15
    signal_window  = 12
    base_min_hold  = 24
    capital_base   = K * TOTAL_CAPITAL   # $6000

    SAFETY_MULTS  = [2.0, 3.0, 5.0]
    CAP_MIN_HOLDS = [240, 480, 720, 1080]
    UNIVERSES     = [("U7", U7), ("U11", U11)]

    rows = []

    # ── Baselines ──────────────────────────────────────────────────────────────
    baselines = [
        ("dynamic_balanced",   U7,  {"entry_threshold": 0.15, "safety_mult": 3.0, "cap_min_hold": 720}),
        ("dynamic_aggressive", U7,  {"entry_threshold": 0.08, "safety_mult": 5.0, "cap_min_hold": 720}),
        ("dynamic_aggressive", U11, {"entry_threshold": 0.08, "safety_mult": 5.0, "cap_min_hold": 720}),
    ]

    for mode_label, coins, kwargs in baselines:
        univ_label = "U7" if coins == U7 else "U11"
        print(f"Running baseline {mode_label} {univ_label} ...")
        pnl, cap, info = simulate_dynamic_min_hold(
            coins,
            max_concurrent=K,
            exit_threshold=exit_threshold,
            base_min_hold=base_min_hold,
            signal_window=signal_window,
            **kwargs,
        )
        n        = len(pnl)
        opens_ph = info["opens_per_hour"]
        entry_thr_pct = kwargs["entry_threshold"] * 100

        for period, start, end in [
            ("full",     0,              n),
            ("last_90d", max(0, n-2160), n),
        ]:
            m = metrics_window(pnl, cap, opens_ph, capital_base, start, end)
            rows.append({
                "mode":             mode_label,
                "universe":         univ_label,
                "safety_mult":      kwargs["safety_mult"],
                "cap_min_hold":     kwargs["cap_min_hold"],
                "natural_floor_pct": round(entry_thr_pct, 2),  # hardcoded threshold as %
                "period":           period,
                "annual":           m["annual"],
                "max_dd":           m["max_dd"],
                "calmar":           m["calmar"],
                "sharpe":           m["sharpe"],
                "trades":           m["trades"],
                "tim_pct":          m["time_in_market_pct"],
                "median_wait_h":    m["median_wait_hours"],
            })

    # ── Math-derived sweep ─────────────────────────────────────────────────────
    total = len(SAFETY_MULTS) * len(CAP_MIN_HOLDS) * len(UNIVERSES)
    done  = 0
    for safety_mult in SAFETY_MULTS:
        for cap_min_hold in CAP_MIN_HOLDS:
            for univ_label, coins in UNIVERSES:
                done += 1
                natural_floor_pct = round(safety_mult * BREAKEVEN_CONST / cap_min_hold * 100, 2)
                print(f"[{done}/{total}] math_derived safety={safety_mult} cap={cap_min_hold}h "
                      f"floor={natural_floor_pct:.2f}% {univ_label} ...")
                pnl, cap, info = simulate_math_derived(
                    coins,
                    max_concurrent=K,
                    exit_threshold=exit_threshold,
                    base_min_hold=base_min_hold,
                    signal_window=signal_window,
                    safety_mult=safety_mult,
                    cap_min_hold=cap_min_hold,
                )
                n        = len(pnl)
                opens_ph = info["opens_per_hour"]

                for period, start, end in [
                    ("full",     0,              n),
                    ("last_90d", max(0, n-2160), n),
                ]:
                    m = metrics_window(pnl, cap, opens_ph, capital_base, start, end)
                    rows.append({
                        "mode":              "math_derived",
                        "universe":          univ_label,
                        "safety_mult":       safety_mult,
                        "cap_min_hold":      cap_min_hold,
                        "natural_floor_pct": natural_floor_pct,
                        "period":            period,
                        "annual":            m["annual"],
                        "max_dd":            m["max_dd"],
                        "calmar":            m["calmar"],
                        "sharpe":            m["sharpe"],
                        "trades":            m["trades"],
                        "tim_pct":           m["time_in_market_pct"],
                        "median_wait_h":     m["median_wait_hours"],
                    })

    # ── DataFrame ──────────────────────────────────────────────────────────────
    cols = [
        "mode", "universe", "safety_mult", "cap_min_hold", "natural_floor_pct",
        "period", "annual", "max_dd", "calmar", "sharpe",
        "trades", "tim_pct", "median_wait_h",
    ]
    df = pd.DataFrame(rows, columns=cols)

    out = Path(__file__).parent / "math_derived_results.csv"
    df.to_csv(out, index=False)
    print(f"\nСохранено: {out}")

    # ── Таблицы ────────────────────────────────────────────────────────────────
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 20)

    df_full = df[df["period"] == "full"].sort_values("calmar", ascending=False).reset_index(drop=True)
    df_90   = df[df["period"] == "last_90d"].sort_values("calmar", ascending=False).reset_index(drop=True)

    print("\n" + "="*160)
    print("FULL PERIOD — top 15 по calmar")
    print("="*160)
    print(df_full[cols].head(15).to_string(index=False))

    print("\n" + "="*160)
    print("LAST 90 DAYS — top 15 по calmar")
    print("="*160)
    print(df_90[cols].head(15).to_string(index=False))


if __name__ == "__main__":
    main()
