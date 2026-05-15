"""
Universe sweep: dynamic_min_hold vs baseline на разных наборах монет.

Цель: найти universe, который оживляет стратегию в last_90d
без катастрофы на full history.
"""

import sys
import numpy as np
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dynamic_min_hold import simulate_dynamic_min_hold, metrics_window
from concurrency_cap import simulate_multi_capped
from engine import load_data, HOURS_PER_YEAR, TOTAL_CAPITAL

# ── Универсумы ─────────────────────────────────────────────────────────────────
UNIVERSES = {
    "U7_current":  ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"],
    "U8_uni":      ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE", "UNI"],
    "U11_no_meme": ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE", "UNI", "ARB", "OP", "TIA"],
    "U13_full":    ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE", "UNI", "ARB", "OP", "TIA", "INJ", "WIF"],
}

# ── Стратегии ──────────────────────────────────────────────────────────────────
# (name, fn_kind, params)
STRATEGIES = [
    ("dynamic_balanced",   "dynamic",   dict(entry_threshold=0.15, safety_mult=3.0)),
    ("dynamic_aggressive", "dynamic",   dict(entry_threshold=0.08, safety_mult=5.0)),
    ("baseline_30_120",    "baseline",  dict(entry_threshold=0.30, min_hold=120)),
    ("baseline_15_120",    "baseline",  dict(entry_threshold=0.15, min_hold=120)),
]

# ── Фиксированные параметры ────────────────────────────────────────────────────
K              = 3
EXIT_THRESHOLD = -0.15
SIGNAL_WINDOW  = 12
BASE_MIN_HOLD  = 24
CAP_MIN_HOLD   = 720
CAPITAL_BASE   = K * TOTAL_CAPITAL   # $6000


def run_universe_sweep():
    rows = []
    total = len(UNIVERSES) * len(STRATEGIES)
    done = 0

    for uni_name, universe in UNIVERSES.items():
        n_coins = len(universe)

        for strat_name, fn_kind, params in STRATEGIES:
            done += 1
            print(f"[{done}/{total}] {uni_name} ({n_coins}) × {strat_name} ...")

            if fn_kind == "dynamic":
                pnl, cap, info = simulate_dynamic_min_hold(
                    coins=universe,
                    max_concurrent=K,
                    entry_threshold=params["entry_threshold"],
                    exit_threshold=EXIT_THRESHOLD,
                    base_min_hold=BASE_MIN_HOLD,
                    signal_window=SIGNAL_WINDOW,
                    safety_mult=params["safety_mult"],
                    cap_min_hold=CAP_MIN_HOLD,
                )
                opens_ph = info["opens_per_hour"]

            else:  # baseline
                pnl, cap, info = simulate_multi_capped(
                    coins=universe,
                    max_concurrent=K,
                    entry_threshold=params["entry_threshold"],
                    exit_threshold=EXIT_THRESHOLD,
                    min_hold=params["min_hold"],
                    signal_window=SIGNAL_WINDOW,
                )
                # simulate_multi_capped не возвращает opens_per_hour —
                # реконструируем: изменение cap вверх → открытие(я)
                cap_diff = np.diff(cap, prepend=0)
                opens_ph = np.where(cap_diff > 0, (cap_diff / TOTAL_CAPITAL).astype(int), 0)

            n = len(pnl)

            # ── FULL ──
            mf = metrics_window(pnl, cap, opens_ph, CAPITAL_BASE, 0, n)

            # ── LAST 90d ──
            start_90 = max(0, n - 2160)
            # Для last_90d трейды baseline оцениваем через cap transitions
            if fn_kind == "baseline":
                cap_last = cap[start_90:n]
                transitions_90 = int(np.sum(np.diff((cap_last > 0).astype(int)) > 0))
                # Используем opens_ph нарезанный для корректного счёта трейдов
                # (opens_ph из cap_diff корректен, поэтому metrics_window считает верно)
            m90 = metrics_window(pnl, cap, opens_ph, CAPITAL_BASE, start_90, n)

            rows.append({
                "universe":    uni_name,
                "n_coins":     n_coins,
                "strategy":    strat_name,
                # full
                "ann_full":    mf["annual"],
                "cal_full":    mf["calmar"],
                "dd_full":     mf["max_dd"],
                "shr_full":    mf["sharpe"],
                "tim_full":    mf["time_in_market_pct"],
                "trades_full": mf["trades"],
                "wait_full":   mf["median_wait_hours"],
                # last 90d
                "ann_90d":     m90["annual"],
                "cal_90d":     m90["calmar"],
                "dd_90d":      m90["max_dd"],
                "shr_90d":     m90["sharpe"],
                "tim_90d":     m90["time_in_market_pct"],
                "trades_90d":  m90["trades"],
                "wait_90d":    m90["median_wait_hours"],
            })

    df = pd.DataFrame(rows, columns=[
        "universe", "n_coins", "strategy",
        "ann_full", "cal_full", "dd_full", "shr_full", "tim_full", "trades_full", "wait_full",
        "ann_90d",  "cal_90d",  "dd_90d",  "shr_90d",  "tim_90d",  "trades_90d",  "wait_90d",
    ])

    # ── Сохранить ──────────────────────────────────────────────────────────────
    out = Path(__file__).parent / "universe_sweep_results.csv"
    df.to_csv(out, index=False)
    print(f"\nСохранено: {out}")

    # ── Вывод ──────────────────────────────────────────────────────────────────
    pd.set_option("display.width", 240)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.float_format", "{:.2f}".format)

    print("\n" + "="*220)
    print("PIVOT: full vs last_90d side-by-side")
    print("="*220)

    # Отсортируем по universe (порядок UNIVERSES) и затем по cal_90d убыв.
    uni_order = list(UNIVERSES.keys())
    df["uni_ord"] = df["universe"].map({u: i for i, u in enumerate(uni_order)})
    df_sorted = df.sort_values(["uni_ord", "cal_90d"], ascending=[True, False]).drop(columns="uni_ord")

    cols_show = [
        "universe", "n_coins", "strategy",
        "ann_full", "cal_full", "tim_full",
        "ann_90d",  "cal_90d",  "tim_90d",
        "trades_full", "trades_90d",
    ]
    print(df_sorted[cols_show].to_string(index=False))
    print("="*220)

    # Лучшие конфиги по last_90d annual
    print("\nТОП-5 по ann_90d:")
    top5 = df_sorted.nlargest(5, "ann_90d")[cols_show]
    print(top5.to_string(index=False))

    return df_sorted


if __name__ == "__main__":
    run_universe_sweep()
