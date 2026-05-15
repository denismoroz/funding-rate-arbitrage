"""
Adaptive percentile-based entry threshold для funding-harvest стратегии.

Вместо фикс. entry_threshold = 0.30 — динамический per-coin per-hour порог:
    threshold[c][i] = max(hard_floor, np.percentile(signal[c][i-lookback*24 : i], 100 - top_x_pct))

Цель: стратегия, которая выживает в разных режимах funding (горячий 2023 vs холодный 2026).
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


def simulate_adaptive_entry(
    coins,
    max_concurrent: int,
    top_x_pct: float,        # 10/20/30 — верхние X% сигналов считаем "входными"
    lookback_days: int,      # 14/30/60/90 — окно в днях
    hard_floor: float,       # 0.05..0.15 — минимум порога
    exit_threshold: float = -0.15,
    min_hold: int = 120,
    signal_window: int = 12,
):
    """
    Симулирует A_cycle со всеми монетами и адаптивным percentile-based entry threshold.

    Возвращает (pnl_total, capital_total, info).
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

        # Предрасчёт адаптивного порога
        thr = np.full(n, np.nan)
        for i in range(lookback_h, n):
            window = sig[max(0, i - lookback_h):i]
            window = window[~np.isnan(window)]
            if len(window) > 0:
                p = np.percentile(window, 100 - top_x_pct)
                thr[i] = max(hard_floor, p)

        state[c] = {
            "rates": rates,
            "close": close,
            "signal": sig,
            "threshold": thr,
            "valid": ~np.isnan(close) & ~np.isnan(rates),
            "in_position":  False,
            "short_size":   0.0,
            "units_spot":   0.0,
            "entry_price":  0.0,
            "hours_since":  0,
            "cash":         TOTAL_CAPITAL,
            "equity_prev":  TOTAL_CAPITAL,
            "trades":       0,
            "hours_in":     0,
        }

    pnl_per_hour = np.zeros(n)
    cap_per_hour = np.zeros(n)
    opens_per_hour = np.zeros(n, dtype=int)

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
            if s["hours_since"] >= min_hold and ar < exit_threshold:
                P = s["close"][i]
                # close short
                s["cash"] += s["short_size"] * (s["entry_price"] - P)
                s["cash"] -= s["short_size"] * P * PERP_TAKER
                # sell spot (A_cycle)
                s["cash"] += s["units_spot"] * P
                s["cash"] -= s["units_spot"] * P * SPOT_TAKER
                s["short_size"] = 0.0
                s["units_spot"] = 0.0
                s["entry_price"] = 0.0
                s["in_position"] = False

        # 3) Подсчёт текущих in-position
        active = [c for c, s in state.items() if s["in_position"]]
        slots_free = max_concurrent - len(active)

        # 4) Кандидаты на вход — берём top-K по adaptive threshold
        if slots_free > 0:
            candidates = []
            for c, s in state.items():
                if not s["valid"][i] or s["in_position"]:
                    continue
                thr_i = s["threshold"][i]
                if not np.isnan(thr_i) and s["signal"][i] > thr_i:
                    candidates.append((c, s["signal"][i]))
            candidates.sort(key=lambda x: -x[1])  # сильнейший первым
            for c, _ in candidates[:slots_free]:
                s = state[c]
                P = s["close"][i]
                # Купить spot
                s["units_spot"]  = POSITION_SIZE / P
                s["cash"]       -= POSITION_SIZE
                s["cash"]       -= POSITION_SIZE * SPOT_TAKER
                # Открыть short
                s["short_size"] = POSITION_SIZE / P
                s["entry_price"]= P
                s["cash"]      -= POSITION_SIZE * PERP_TAKER
                s["in_position"]= True
                s["hours_since"]= 0
                s["trades"]    += 1
                opens_per_hour[i] += 1

        # 5) MTM equity per coin
        hour_pnl  = 0.0
        hour_cap  = 0.0
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
        "trades_per_coin":   {c: state[c]["trades"] for c in state},
        "total_trades":      sum(s["trades"] for s in state.values()),
        "peak_capital":      cap_per_hour.max(),
        "opens_per_hour":    opens_per_hour,
    }
    return pnl_per_hour, cap_per_hour, info


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
        "n_empty_windows":    len(runs),
    }


def main():
    coins = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
    K = 3
    exit_threshold = -0.15
    min_hold = 120
    signal_window = 12
    capital_base = K * TOTAL_CAPITAL  # $6000

    TOP_X_PCTS    = [10, 20, 30]
    LOOKBACK_DAYS = [30, 60, 90]
    HARD_FLOORS   = [0.08, 0.10, 0.15]

    rows = []

    # ── Baselines ──────────────────────────────────────────────────────────────
    for label, entry_thr in [("baseline_30", 0.30), ("baseline_10", 0.10)]:
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
        # baseline не имеет opens_per_hour — реконструируем из cap изменений
        # открытие = cap растёт на TOTAL_CAPITAL
        cap_diff = np.diff(cap, prepend=0)
        opens_ph = np.where(cap_diff > 0, (cap_diff / TOTAL_CAPITAL).astype(int), 0)

        # full period
        mf = metrics_window(pnl, cap, opens_ph, capital_base, 0, n)
        rows.append({
            "mode": label, "top_x": None, "lookback": None, "hard_floor": None,
            "period": "full",
            **mf,
        })
        # last 90d
        last_90d = 90 * 24
        start_90 = max(0, n - last_90d)
        m90 = metrics_window(pnl, cap, opens_ph, capital_base, start_90, n)
        rows.append({
            "mode": label, "top_x": None, "lookback": None, "hard_floor": None,
            "period": "last_90d",
            **m90,
        })

    # ── Adaptive sweep ─────────────────────────────────────────────────────────
    total_configs = len(TOP_X_PCTS) * len(LOOKBACK_DAYS) * len(HARD_FLOORS)
    done = 0
    for top_x in TOP_X_PCTS:
        for lookback in LOOKBACK_DAYS:
            for hfloor in HARD_FLOORS:
                done += 1
                print(f"[{done}/{total_configs}] adaptive top_x={top_x} lookback={lookback}d hard_floor={hfloor:.2f} ...")
                pnl, cap, info = simulate_adaptive_entry(
                    coins,
                    max_concurrent=K,
                    top_x_pct=top_x,
                    lookback_days=lookback,
                    hard_floor=hfloor,
                    exit_threshold=exit_threshold,
                    min_hold=min_hold,
                    signal_window=signal_window,
                )
                n = len(pnl)
                opens_ph = info["opens_per_hour"]

                # full period
                mf = metrics_window(pnl, cap, opens_ph, capital_base, 0, n)
                rows.append({
                    "mode": "adaptive",
                    "top_x": top_x,
                    "lookback": lookback,
                    "hard_floor": hfloor,
                    "period": "full",
                    **mf,
                })
                # last 90d
                last_90d = 90 * 24
                start_90 = max(0, n - last_90d)
                m90 = metrics_window(pnl, cap, opens_ph, capital_base, start_90, n)
                rows.append({
                    "mode": "adaptive",
                    "top_x": top_x,
                    "lookback": lookback,
                    "hard_floor": hfloor,
                    "period": "last_90d",
                    **m90,
                })

    df = pd.DataFrame(rows, columns=[
        "mode", "top_x", "lookback", "hard_floor", "period",
        "annual", "max_dd", "calmar", "sharpe", "trades",
        "time_in_market_pct", "median_wait_hours", "n_empty_windows",
    ])

    # Сохранить
    out = Path(__file__).parent / "adaptive_entry_results.csv"
    df.to_csv(out, index=False)
    print(f"\nСохранено: {out}")

    # ── Таблицы ────────────────────────────────────────────────────────────────
    df_full = df[df["period"] == "full"].sort_values("calmar", ascending=False).reset_index(drop=True)
    df_90   = df[df["period"] == "last_90d"].sort_values("calmar", ascending=False).reset_index(drop=True)

    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 20)

    print("\n" + "="*120)
    print("FULL PERIOD — top 10 по calmar")
    print("="*120)
    cols_show = ["mode", "top_x", "lookback", "hard_floor", "annual", "max_dd", "calmar", "sharpe", "trades", "time_in_market_pct", "median_wait_hours"]
    print(df_full[cols_show].head(10).to_string(index=False))

    print("\n" + "="*120)
    print("LAST 90 DAYS — top 10 по calmar")
    print("="*120)
    print(df_90[cols_show].head(10).to_string(index=False))


if __name__ == "__main__":
    main()
