"""
Fallback-entry расширение для simulate_multi_capped.

Идея: когда ВСЕ позиции закрыты и обычный top-K entry не сработал —
открыть ровно одну позицию по relaxed-порогу entry_threshold * fallback_ratio.

v2: добавлен fallback_min_hold — отдельный min_hold для позиций,
открытых через fallback-путь.
"""

import numpy as np
import pandas as pd
from pathlib import Path
from engine import (
    load_data,
    POSITION_SIZE, TOTAL_CAPITAL, PERP_TAKER, SPOT_TAKER,
    HOURS_PER_YEAR,
)
from concurrency_cap import metrics_on_capital


def simulate_multi_capped_fallback(
    coins,
    max_concurrent: int,
    entry_threshold: float = 0.20,
    exit_threshold:  float = -0.05,
    min_hold:        int   = 72,
    signal_window:   int   = 1,
    fallback_ratio:  float | None = None,
    fallback_min_hold: int | None = None,
):
    """
    Симулирует A_cycle по всем монетам с ограничением max_concurrent.

    Дополнение к simulate_multi_capped: если fallback_ratio is not None
    и после обычного entry-шага ни одна монета не in_position —
    открываем ровно одну монету с наибольшим сигналом > entry_threshold * fallback_ratio.

    fallback_min_hold: если задан — fallback-позиции используют его вместо min_hold
    при exit-проверке. None → то же поведение, что min_hold.

    Возвращает (pnl_per_hour, cap_per_hour, info).
    info содержит fallback_trades (int).
    """
    # Загрузка и выравнивание данных
    datas = {}
    for c in coins:
        df = load_data(c)
        if df.empty:
            continue
        datas[c] = df

    common_idx = sorted(set().union(*[set(df.index) for df in datas.values()]))
    common_idx = pd.DatetimeIndex(common_idx)
    n = len(common_idx)

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
            "rates":        rates,
            "close":        close,
            "signal":       sig,
            "valid":        ~np.isnan(close) & ~np.isnan(rates),
            "in_position":  False,
            "short_size":   0.0,
            "units_spot":   0.0,
            "entry_price":  0.0,
            "hours_since":  0,
            "cash":         TOTAL_CAPITAL,
            "equity_prev":  TOTAL_CAPITAL,
            "trades":       0,
            "hours_in":     0,
            "fallback_open": False,
        }

    pnl_per_hour = np.zeros(n)
    cap_per_hour = np.zeros(n)
    fallback_trades = 0

    for i in range(n):
        # 1) Funding для всех in-position
        for c, s in state.items():
            if not s["valid"][i]:
                continue
            if s["in_position"]:
                P = s["close"][i]
                r = s["rates"][i]
                s["cash"] += s["short_size"] * P * r

        # 2) Exit
        for c, s in state.items():
            if not s["valid"][i] or not s["in_position"]:
                continue
            s["hours_since"] += 1
            s["hours_in"]    += 1
            ar = s["rates"][i] * HOURS_PER_YEAR
            effective_min_hold = (
                fallback_min_hold
                if (s["fallback_open"] and fallback_min_hold is not None)
                else min_hold
            )
            if s["hours_since"] >= effective_min_hold and ar < exit_threshold:
                P = s["close"][i]
                s["cash"] += s["short_size"] * (s["entry_price"] - P)
                s["cash"] -= s["short_size"] * P * PERP_TAKER
                s["cash"] += s["units_spot"] * P
                s["cash"] -= s["units_spot"] * P * SPOT_TAKER
                s["short_size"]   = 0.0
                s["units_spot"]   = 0.0
                s["entry_price"]  = 0.0
                s["in_position"]  = False
                s["fallback_open"] = False

        # 3) Подсчёт активных позиций
        active     = [c for c, s in state.items() if s["in_position"]]
        slots_free = max_concurrent - len(active)

        # 4a) Обычный top-K entry
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
                s["units_spot"]   = POSITION_SIZE / P
                s["cash"]        -= POSITION_SIZE
                s["cash"]        -= POSITION_SIZE * SPOT_TAKER
                s["short_size"]   = POSITION_SIZE / P
                s["entry_price"]  = P
                s["cash"]        -= POSITION_SIZE * PERP_TAKER
                s["in_position"]  = True
                s["hours_since"]  = 0
                s["trades"]      += 1
                s["fallback_open"] = False

        # 4b) Fallback entry: ровно 1 позиция по relaxed-порогу, если никто не активен
        if fallback_ratio is not None:
            active_after = [c for c, s in state.items() if s["in_position"]]
            if len(active_after) == 0:
                relaxed_thr = entry_threshold * fallback_ratio
                fb_candidates = []
                for c, s in state.items():
                    if not s["valid"][i] or s["in_position"]:
                        continue
                    if s["signal"][i] > relaxed_thr:
                        fb_candidates.append((c, s["signal"][i]))
                fb_candidates.sort(key=lambda x: -x[1])
                if fb_candidates:
                    c, _ = fb_candidates[0]
                    s = state[c]
                    P = s["close"][i]
                    s["units_spot"]   = POSITION_SIZE / P
                    s["cash"]        -= POSITION_SIZE
                    s["cash"]        -= POSITION_SIZE * SPOT_TAKER
                    s["short_size"]   = POSITION_SIZE / P
                    s["entry_price"]  = P
                    s["cash"]        -= POSITION_SIZE * PERP_TAKER
                    s["in_position"]  = True
                    s["hours_since"]  = 0
                    s["trades"]      += 1
                    s["fallback_open"] = True
                    fallback_trades  += 1

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

    # Финал: закрыть все открытые позиции
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
        s["short_size"]  = 0.0
        s["units_spot"]  = 0.0
        equity_now = s["cash"]
        pnl_per_hour[-1] += equity_now - s["equity_prev"]
        s["equity_prev"] = equity_now

    info = {
        "trades_per_coin":             {c: state[c]["trades"] for c in state},
        "total_trades":                sum(s["trades"] for s in state.values()),
        "fallback_trades":             fallback_trades,
        "peak_capital":                cap_per_hour.max(),
        "avg_capital_when_active":     cap_per_hour[cap_per_hour > 0].mean() if (cap_per_hour > 0).any() else 0,
    }
    return pnl_per_hour, cap_per_hour, info


def main():
    coins = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
    K              = 3
    entry_threshold = 0.30
    exit_threshold  = -0.15
    min_hold        = 120
    signal_window   = 12

    fallback_ratios    = [None, 0.40, 0.50, 0.60]
    fallback_min_holds = [None, 1, 6, 24, 72]

    rows = []
    for fb_ratio in fallback_ratios:
        if fb_ratio is None:
            # Baseline: нет fallback — fb_min_hold не применяется, один прогон
            pnl, cap, info = simulate_multi_capped_fallback(
                coins,
                max_concurrent=K,
                entry_threshold=entry_threshold,
                exit_threshold=exit_threshold,
                min_hold=min_hold,
                signal_window=signal_window,
                fallback_ratio=None,
                fallback_min_hold=None,
            )
            n = len(pnl)
            peak = info["peak_capital"]
            m = metrics_on_capital(pnl, K * TOTAL_CAPITAL, n)
            time_in_market = round((cap > 0).mean() * 100, 1)
            rows.append({
                "fallback_ratio":     "None",
                "fb_min_hold":        "None",
                "peak_$":             int(peak),
                "annual_theory":      m["annual"],
                "max_dd_theory":      m["max_dd"],
                "calmar_theory":      m["calmar"],
                "sharpe_theory":      m["sharpe"],
                "total_trades":       info["total_trades"],
                "fallback_trades":    info["fallback_trades"],
                "time_in_market_pct": time_in_market,
            })
        else:
            for fb_mh in fallback_min_holds:
                pnl, cap, info = simulate_multi_capped_fallback(
                    coins,
                    max_concurrent=K,
                    entry_threshold=entry_threshold,
                    exit_threshold=exit_threshold,
                    min_hold=min_hold,
                    signal_window=signal_window,
                    fallback_ratio=fb_ratio,
                    fallback_min_hold=fb_mh,
                )
                n = len(pnl)
                peak = info["peak_capital"]
                m = metrics_on_capital(pnl, K * TOTAL_CAPITAL, n)
                time_in_market = round((cap > 0).mean() * 100, 1)
                rows.append({
                    "fallback_ratio":     fb_ratio,
                    "fb_min_hold":        fb_mh if fb_mh is not None else "None(=120)",
                    "peak_$":             int(peak),
                    "annual_theory":      m["annual"],
                    "max_dd_theory":      m["max_dd"],
                    "calmar_theory":      m["calmar"],
                    "sharpe_theory":      m["sharpe"],
                    "total_trades":       info["total_trades"],
                    "fallback_trades":    info["fallback_trades"],
                    "time_in_market_pct": time_in_market,
                })

    df_result = pd.DataFrame(rows)
    print("=" * 110)
    print(f"FALLBACK ENTRY 2D SWEEP  |  Coins: {coins}  |  K={K}")
    print(f"COMBO params: entry={entry_threshold}, exit={exit_threshold}, "
          f"min_hold={min_hold}, signal_window={signal_window}")
    print(f"Outer loop: fallback_ratio ∈ {fallback_ratios}")
    print(f"Inner loop: fallback_min_hold ∈ {fallback_min_holds}  (None = same as min_hold={min_hold})")
    print("=" * 110)
    print(df_result.to_string(index=False))

    out = Path(__file__).parent / "concurrency_cap_fallback_results.csv"
    df_result.to_csv(out, index=False)
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    main()
