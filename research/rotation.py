"""
Rotation: ротация позиций по сигналу.

После min_hold позиция закрывается не только по exit_threshold,
но и если есть монета с заметно лучшим funding signal.

rotation_factor: 1.5 = ротировать если best_other > current × 1.5
rotation_margin_pp: минимальная абс. разница сигналов (п.п. годовых)

Выключено если rotation_factor <= 1.0.
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
from dynamic_min_hold import simulate_dynamic_min_hold, metrics_window


BREAKEVEN_CONST = 18.4  # = 0.0021 × 8760


def simulate_rotation(
    coins,
    max_concurrent: int,
    entry_threshold: float,
    exit_threshold: float,
    base_min_hold: int,
    signal_window: int,
    safety_mult: float,
    cap_min_hold: int,
    rotation_factor: float,
    rotation_margin_pp: float,
):
    """
    Расширение simulate_dynamic_min_hold с ротацией позиций по сигналу.

    После position_min_hold, если есть незанятая монета с сигналом:
      best_other_signal > current_signal * rotation_factor
      И best_other_signal - current_signal > rotation_margin_pp
    — закрываем текущую позицию и открываем новую.

    rotation_factor <= 1.0 — ротация выключена.
    rotation_margin_pp >= 999 — ротация выключена (sentinel).

    Возвращает (pnl_per_hour, cap_per_hour, info).
    """
    rotation_enabled = (rotation_factor > 1.0) and (rotation_margin_pp < 999.0)

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
    rotation_count        = [0]  # list для мутации внутри цикла

    coin_list = list(state.keys())  # фиксированный порядок

    for i in range(n):
        # 1) Funding для всех уже-в-позиции
        for c in coin_list:
            s = state[c]
            if not s["valid"][i]:
                continue
            if s["in_position"]:
                P = s["close"][i]
                r = s["rates"][i]
                s["cash"] += s["short_size"] * P * r

        # 2) Стандартный exit по exit_threshold
        for c in coin_list:
            s = state[c]
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

        # 3) Ротация: отдельный цикл после стандартных exit-ов
        if rotation_enabled:
            for c in coin_list:
                s = state[c]
                if not s["valid"][i] or not s["in_position"]:
                    continue
                if s["hours_since"] < s["position_min_hold"]:
                    continue

                current_signal = s["signal"][i]

                # Найти лучшего конкурента (не в позиции, не та же монета)
                best_alt        = None
                best_alt_signal = -np.inf
                for c2 in coin_list:
                    if c2 == c:
                        continue
                    s2 = state[c2]
                    if not s2["valid"][i] or s2["in_position"]:
                        continue
                    if s2["signal"][i] > best_alt_signal:
                        best_alt_signal = s2["signal"][i]
                        best_alt        = c2

                if best_alt is None:
                    continue

                # Оба условия ротации
                factor_ok = best_alt_signal > current_signal * rotation_factor
                margin_ok = (best_alt_signal - current_signal) > rotation_margin_pp
                if not (factor_ok and margin_ok):
                    continue

                # Закрыть текущую позицию (аналог exit-блока, без hours_since — уже учли выше)
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

                # Открыть новую позицию в best_alt
                s2  = state[best_alt]
                P2  = s2["close"][i]
                s2["units_spot"]        = POSITION_SIZE / P2
                s2["cash"]             -= POSITION_SIZE
                s2["cash"]             -= POSITION_SIZE * SPOT_TAKER
                s2["short_size"]        = POSITION_SIZE / P2
                s2["entry_price"]       = P2
                s2["cash"]             -= POSITION_SIZE * PERP_TAKER
                s2["in_position"]       = True
                s2["hours_since"]       = 0
                s2["trades"]           += 1

                entry_rate = best_alt_signal
                if entry_rate > 0:
                    breakeven_h = BREAKEVEN_CONST / entry_rate
                    pos_min_hold = int(min(cap_min_hold,
                                          max(base_min_hold,
                                              safety_mult * breakeven_h)))
                else:
                    pos_min_hold = cap_min_hold

                s2["position_min_hold"]  = pos_min_hold
                entry_rates_collected.append(entry_rate)
                holds_collected.append(pos_min_hold)
                opens_per_hour[i]       += 1
                rotation_count[0]       += 1

        # 4) Подсчёт текущих in-position
        active     = [c for c in coin_list if state[c]["in_position"]]
        slots_free = max_concurrent - len(active)

        # 5) Кандидаты на вход — берём top-K по signal
        if slots_free > 0:
            candidates = []
            for c in coin_list:
                s = state[c]
                if not s["valid"][i] or s["in_position"]:
                    continue
                if s["signal"][i] > entry_threshold:
                    candidates.append((c, s["signal"][i]))
            candidates.sort(key=lambda x: -x[1])
            for c, _ in candidates[:slots_free]:
                s = state[c]
                P = s["close"][i]

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

        # 6) MTM equity per coin
        hour_pnl = 0.0
        hour_cap = 0.0
        for c in coin_list:
            s = state[c]
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
    for c in coin_list:
        s = state[c]
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
        s["equity_prev"]  = equity_now

    total_trades = sum(s["trades"] for s in state.values())
    info = {
        "total_trades":          total_trades,
        "rotation_trades":       rotation_count[0],
        "peak_capital":          cap_per_hour.max(),
        "opens_per_hour":        opens_per_hour,
        "entry_rates_collected": entry_rates_collected,
        "holds_collected":       holds_collected,
    }
    return pnl_per_hour, cap_per_hour, info


def main():
    coins          = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
    K              = 3
    exit_threshold = -0.15
    signal_window  = 12
    base_min_hold  = 24
    cap_min_hold   = 720
    capital_base   = K * TOTAL_CAPITAL  # $6000

    ENTRY_THRESHOLDS    = [0.10, 0.15]
    SAFETY_MULTS        = [3.0, 5.0]
    ROTATION_FACTORS    = [1.0, 1.5, 2.0, 3.0]
    ROTATION_MARGINS    = [0.10, 0.15, 0.20]

    rows = []

    # ── Baselines ────────────────────────────────────────────────────────────────
    print("Running baseline_30_120 ...")
    pnl_b, cap_b, info_b = simulate_multi_capped(
        coins, max_concurrent=K,
        entry_threshold=0.30, exit_threshold=exit_threshold,
        min_hold=120, signal_window=signal_window,
    )
    n_b = len(pnl_b)
    cap_diff_b = np.diff(cap_b, prepend=0)
    opens_ph_b = np.where(cap_diff_b > 0, (cap_diff_b / TOTAL_CAPITAL).astype(int), 0)
    for period, s_idx in [("full", 0), ("last_90d", max(0, n_b - 2160))]:
        m = metrics_window(pnl_b, cap_b, opens_ph_b, capital_base, s_idx, n_b)
        rows.append({
            "mode": "baseline_30_120", "entry": 0.30, "safety_mult": 0,
            "rotation_factor": 0.0, "rotation_margin_pp": 0.0,
            "period": period,
            "annual": m["annual"], "max_dd": m["max_dd"],
            "calmar": m["calmar"], "sharpe": m["sharpe"],
            "trades": m["trades"], "rotation_trades": 0,
            "tim_pct": m["time_in_market_pct"],
            "median_wait_h": m["median_wait_hours"],
        })

    print("Running dynamic_balanced (entry=0.15 safety=3.0) ...")
    pnl_db, cap_db, info_db = simulate_dynamic_min_hold(
        coins, max_concurrent=K,
        entry_threshold=0.15, exit_threshold=exit_threshold,
        base_min_hold=base_min_hold, signal_window=signal_window,
        safety_mult=3.0, cap_min_hold=cap_min_hold,
    )
    n_db = len(pnl_db)
    opens_ph_db = info_db["opens_per_hour"]
    for period, s_idx in [("full", 0), ("last_90d", max(0, n_db - 2160))]:
        m = metrics_window(pnl_db, cap_db, opens_ph_db, capital_base, s_idx, n_db)
        rows.append({
            "mode": "dynamic_balanced", "entry": 0.15, "safety_mult": 3.0,
            "rotation_factor": 0.0, "rotation_margin_pp": 0.0,
            "period": period,
            "annual": m["annual"], "max_dd": m["max_dd"],
            "calmar": m["calmar"], "sharpe": m["sharpe"],
            "trades": m["trades"], "rotation_trades": 0,
            "tim_pct": m["time_in_market_pct"],
            "median_wait_h": m["median_wait_hours"],
        })

    print("Running dynamic_aggressive (entry=0.08 safety=5.0) ...")
    pnl_da, cap_da, info_da = simulate_dynamic_min_hold(
        coins, max_concurrent=K,
        entry_threshold=0.08, exit_threshold=exit_threshold,
        base_min_hold=base_min_hold, signal_window=signal_window,
        safety_mult=5.0, cap_min_hold=cap_min_hold,
    )
    n_da = len(pnl_da)
    opens_ph_da = info_da["opens_per_hour"]
    for period, s_idx in [("full", 0), ("last_90d", max(0, n_da - 2160))]:
        m = metrics_window(pnl_da, cap_da, opens_ph_da, capital_base, s_idx, n_da)
        rows.append({
            "mode": "dynamic_aggressive", "entry": 0.08, "safety_mult": 5.0,
            "rotation_factor": 0.0, "rotation_margin_pp": 0.0,
            "period": period,
            "annual": m["annual"], "max_dd": m["max_dd"],
            "calmar": m["calmar"], "sharpe": m["sharpe"],
            "trades": m["trades"], "rotation_trades": 0,
            "tim_pct": m["time_in_market_pct"],
            "median_wait_h": m["median_wait_hours"],
        })

    # ── Rotation sweep ───────────────────────────────────────────────────────────
    # Для factor=1.0 (off) — одна строка с margin=0.10; для factor>1.0 — все margins.
    configs = []
    for entry_thr in ENTRY_THRESHOLDS:
        for safety_mult in SAFETY_MULTS:
            for rot_factor in ROTATION_FACTORS:
                if rot_factor <= 1.0:
                    configs.append((entry_thr, safety_mult, rot_factor, 0.10))
                else:
                    for rot_margin in ROTATION_MARGINS:
                        configs.append((entry_thr, safety_mult, rot_factor, rot_margin))

    total = len(configs)
    for idx, (entry_thr, safety_mult, rot_factor, rot_margin) in enumerate(configs, 1):
        rot_label = f"rot={rot_factor:.1f}/mg={rot_margin:.2f}"
        print(f"[{idx}/{total}] entry={entry_thr:.2f} sfty={safety_mult:.1f} {rot_label} ...")
        pnl, cap, info = simulate_rotation(
            coins, max_concurrent=K,
            entry_threshold=entry_thr,
            exit_threshold=exit_threshold,
            base_min_hold=base_min_hold,
            signal_window=signal_window,
            safety_mult=safety_mult,
            cap_min_hold=cap_min_hold,
            rotation_factor=rot_factor,
            rotation_margin_pp=rot_margin,
        )
        n = len(pnl)
        opens_ph = info["opens_per_hour"]
        rot_cnt  = info["rotation_trades"]
        mode_str = f"rot_f{rot_factor:.1f}_m{rot_margin:.2f}" if rot_factor > 1.0 else "no_rotation"

        for period, s_idx in [("full", 0), ("last_90d", max(0, n - 2160))]:
            m = metrics_window(pnl, cap, opens_ph, capital_base, s_idx, n)
            rows.append({
                "mode":               mode_str,
                "entry":              entry_thr,
                "safety_mult":        safety_mult,
                "rotation_factor":    rot_factor,
                "rotation_margin_pp": rot_margin,
                "period":             period,
                "annual":             m["annual"],
                "max_dd":             m["max_dd"],
                "calmar":             m["calmar"],
                "sharpe":             m["sharpe"],
                "trades":             m["trades"],
                "rotation_trades":    rot_cnt,
                "tim_pct":            m["time_in_market_pct"],
                "median_wait_h":      m["median_wait_hours"],
            })

    # ── DataFrame и сохранение ───────────────────────────────────────────────────
    cols = [
        "mode", "entry", "safety_mult", "rotation_factor", "rotation_margin_pp",
        "period", "annual", "max_dd", "calmar", "sharpe",
        "trades", "rotation_trades", "tim_pct", "median_wait_h",
    ]
    df = pd.DataFrame(rows, columns=cols)

    out = Path(__file__).parent / "rotation_results.csv"
    df.to_csv(out, index=False)
    print(f"\nСохранено: {out}")

    # ── Таблицы ──────────────────────────────────────────────────────────────────
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 20)

    df_full = df[df["period"] == "full"].sort_values("calmar", ascending=False).reset_index(drop=True)
    df_90   = df[df["period"] == "last_90d"].sort_values("calmar", ascending=False).reset_index(drop=True)

    print("\n" + "=" * 160)
    print("FULL PERIOD — top-15 по calmar")
    print("=" * 160)
    print(df_full[cols[:-1]].head(15).to_string(index=False))

    print("\n" + "=" * 160)
    print("LAST 90 DAYS — top-15 по calmar")
    print("=" * 160)
    print(df_90[cols[:-1]].head(15).to_string(index=False))


if __name__ == "__main__":
    main()
