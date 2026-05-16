"""
Диагностика «холодного» периода dynamic_aggressive (last 90 days).

Проверяем три гипотезы:
  H1: Funding rates системно низкие в last_90d
  H2: Time-in-market 100% но funding income мал
  H3: Fees съедают значимую долю

Параметры стратегии dynamic_aggressive:
  entry=0.08, safety_mult=5, cap=720, K=3, signal_window=12, exit=-0.15
"""

import sys
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import timezone

sys.path.insert(0, str(Path(__file__).parent))

from engine import (
    load_data,
    POSITION_SIZE, TOTAL_CAPITAL, PERP_TAKER, SPOT_TAKER, HOURS_PER_YEAR,
)
from dynamic_min_hold import BREAKEVEN_CONST

# ── Параметры стратегии ────────────────────────────────────────────────────────
COINS          = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "DOGE"]
K              = 3
ENTRY_THR      = 0.08
EXIT_THR       = -0.15
SIGNAL_WINDOW  = 12
SAFETY_MULT    = 5.0
BASE_MIN_HOLD  = 24
CAP_MIN_HOLD   = 720
CAPITAL_BASE   = K * TOTAL_CAPITAL   # $6 000

DAYS_90 = 90

# ══════════════════════════════════════════════════════════════════════════════
# ТАБЛИЦА 1 + 3: Funding rate stats по периодам и % negative hours last_90d
# ══════════════════════════════════════════════════════════════════════════════

def compute_funding_stats():
    """
    Возвращает stats_rows (для таблицы 1) и negative_rows (таблица 3).
    Работает на 12h MA signal (annualized, %).
    """
    stats_rows    = []
    negative_rows = []

    period_defs = {
        "2023":     ("2023-01-01", "2024-01-01"),
        "2024":     ("2024-01-01", "2025-01-01"),
        "2025":     ("2025-01-01", "2026-01-01"),
        "2026":     ("2026-01-01", None),
        "full":     (None, None),
        "last_90d": (None, None),   # специальный
    }

    for coin in COINS:
        df = load_data(coin, with_ohlcv=False)
        rates = df["fundingRate"].values
        timestamps = df.index

        # 12h MA signal
        sig_annual = pd.Series(rates).rolling(12, min_periods=1).mean().values * HOURS_PER_YEAR * 100

        end_ts   = timestamps[-1]
        cut_90d  = end_ts - pd.Timedelta(days=DAYS_90)

        for period, (t_start, t_end) in period_defs.items():
            if period == "last_90d":
                mask = timestamps >= cut_90d
            else:
                if t_start is not None:
                    mask = timestamps >= pd.Timestamp(t_start, tz="UTC")
                    if t_end is not None:
                        mask &= timestamps < pd.Timestamp(t_end, tz="UTC")
                else:
                    mask = np.ones(len(timestamps), dtype=bool)

            subset = sig_annual[mask]
            if len(subset) < 10:
                continue

            stats_rows.append({
                "coin":   coin,
                "period": period,
                "n_hrs":  len(subset),
                "mean":   round(np.mean(subset), 2),
                "median": round(np.median(subset), 2),
                "p90":    round(np.percentile(subset, 90), 2),
                "p95":    round(np.percentile(subset, 95), 2),
                "max":    round(np.max(subset), 2),
            })

            if period == "last_90d":
                raw_annual = rates[mask] * HOURS_PER_YEAR * 100
                neg_pct = (raw_annual < 0).mean() * 100
                negative_rows.append({
                    "coin":       coin,
                    "n_hrs":      len(raw_annual),
                    "neg_pct":    round(neg_pct, 1),
                    "mean_raw":   round(np.mean(raw_annual), 2),
                    "median_raw": round(np.median(raw_annual), 2),
                })

    return pd.DataFrame(stats_rows), pd.DataFrame(negative_rows)


# ══════════════════════════════════════════════════════════════════════════════
# ТАБЛИЦА 2: Histogram funding buckets last_90d
# ══════════════════════════════════════════════════════════════════════════════

BUCKETS = [
    ("< 0%",    lambda x: x < 0),
    ("0–5%",    lambda x: (x >= 0) & (x < 5)),
    ("5–8%",    lambda x: (x >= 5) & (x < 8)),
    ("8–10%",   lambda x: (x >= 8) & (x < 10)),
    ("10–11%",  lambda x: (x >= 10) & (x < 11)),
    ("11–15%",  lambda x: (x >= 11) & (x < 15)),
    ("15–30%",  lambda x: (x >= 15) & (x < 30)),
    ("30%+",    lambda x: x >= 30),
]

def compute_histogram():
    all_rates_90d = []
    for coin in COINS:
        df = load_data(coin, with_ohlcv=False)
        end_ts  = df.index[-1]
        cut_90d = end_ts - pd.Timedelta(days=DAYS_90)
        mask    = df.index >= cut_90d
        # raw annualized %
        rates_ann = df["fundingRate"].values[mask] * HOURS_PER_YEAR * 100
        all_rates_90d.append(rates_ann)

    combined = np.concatenate(all_rates_90d)
    n_total  = len(combined)

    rows = []
    for label, cond in BUCKETS:
        cnt = int(cond(combined).sum())
        rows.append({
            "bucket":    label,
            "hours":     cnt,
            "pct_total": round(cnt / n_total * 100, 1),
        })
    return pd.DataFrame(rows), n_total


# ══════════════════════════════════════════════════════════════════════════════
# ТАБЛИЦЫ 4 + 5: P&L breakdown через simulate_with_breakdown
# ══════════════════════════════════════════════════════════════════════════════

def simulate_with_breakdown(coins, max_concurrent, entry_threshold, exit_threshold,
                             base_min_hold, signal_window, safety_mult, cap_min_hold):
    """
    Полная симуляция с записью трейдов и декомпозицией P&L.
    Возвращает (pnl_per_hour, cap_per_hour, trade_log, common_idx).

    trade_log — список dict с полями:
        coin, open_idx, close_idx, open_ts, close_ts,
        hold_hours, entry_price, exit_price,
        entry_rate_annual,
        gross_funding, fees_open, fees_close, net_pnl,
        spot_pnl, basis_pnl
    """
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
        df2  = df.reindex(common_idx)
        rates = df2["fundingRate"].values
        close = df2["close"].values
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
            # per-trade accumulators
            "open_idx":          0,
            "open_ts":           None,
            "entry_rate":        0.0,
            "cum_funding":       0.0,   # gross funding накопленный за текущий трейд
            "fees_open":         0.0,
        }

    pnl_per_hour   = np.zeros(n)
    cap_per_hour   = np.zeros(n)
    opens_per_hour = np.zeros(n, dtype=int)
    trade_log      = []

    for i in range(n):
        # 1) Funding
        for c, s in state.items():
            if not s["valid"][i]:
                continue
            if s["in_position"]:
                P = s["close"][i]
                r = s["rates"][i]
                f = s["short_size"] * P * r
                s["cash"] += f
                s["cum_funding"] += f

        # 2) Exit
        for c, s in state.items():
            if not s["valid"][i] or not s["in_position"]:
                continue
            s["hours_since"] += 1
            s["hours_in"]    += 1
            ar = s["rates"][i] * HOURS_PER_YEAR
            if s["hours_since"] >= s["position_min_hold"] and ar < exit_threshold:
                P       = s["close"][i]
                ep      = s["entry_price"]
                ss      = s["short_size"]
                us      = s["units_spot"]
                # close short
                short_realized = ss * (ep - P)
                fee_perp_close = ss * P * PERP_TAKER
                s["cash"] += short_realized
                s["cash"] -= fee_perp_close
                # sell spot
                fee_spot_close = us * P * SPOT_TAKER
                spot_proceeds  = us * P
                s["cash"] += spot_proceeds - fee_spot_close

                # decompose:
                gross_funding = s["cum_funding"]
                fees_open     = s["fees_open"]
                fees_close    = fee_perp_close + fee_spot_close
                # spot_pnl = mark-to-market движение спота
                spot_pnl  = us * (P - ep)
                # basis_pnl = realized short pnl (delta-hedged → должно ~= -spot_pnl)
                basis_pnl = short_realized
                net_pnl   = gross_funding - fees_open - fees_close + spot_pnl + basis_pnl

                trade_log.append({
                    "coin":               c,
                    "open_idx":           s["open_idx"],
                    "close_idx":          i,
                    "open_ts":            s["open_ts"],
                    "close_ts":           common_idx[i],
                    "hold_hours":         s["hours_since"],
                    "entry_price":        round(ep, 4),
                    "exit_price":         round(P, 4),
                    "entry_rate_annual":  round(s["entry_rate"] * 100, 2),
                    "gross_funding":      round(gross_funding, 4),
                    "fees_open":          round(fees_open, 4),
                    "fees_close":         round(fees_close, 4),
                    "spot_pnl":           round(spot_pnl, 4),
                    "basis_pnl":          round(basis_pnl, 4),
                    "net_pnl":            round(net_pnl, 4),
                })

                s["short_size"]        = 0.0
                s["units_spot"]        = 0.0
                s["entry_price"]       = 0.0
                s["in_position"]       = False
                s["position_min_hold"] = 0
                s["cum_funding"]       = 0.0
                s["fees_open"]         = 0.0

        # 3) Active count
        active     = [c for c, s in state.items() if s["in_position"]]
        slots_free = max_concurrent - len(active)

        # 4) Entry
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

                entry_rate = s["signal"][i]
                if entry_rate > 0:
                    breakeven_h = BREAKEVEN_CONST / entry_rate
                    pos_min_hold = int(min(cap_min_hold,
                                          max(base_min_hold,
                                              safety_mult * breakeven_h)))
                else:
                    pos_min_hold = cap_min_hold

                fee_spot_open = POSITION_SIZE * SPOT_TAKER
                fee_perp_open = POSITION_SIZE * PERP_TAKER

                s["units_spot"]        = POSITION_SIZE / P
                s["cash"]             -= POSITION_SIZE + fee_spot_open
                s["short_size"]        = POSITION_SIZE / P
                s["entry_price"]       = P
                s["cash"]             -= fee_perp_open
                s["in_position"]       = True
                s["hours_since"]       = 0
                s["position_min_hold"] = pos_min_hold
                s["trades"]           += 1
                s["open_idx"]          = i
                s["open_ts"]           = common_idx[i]
                s["entry_rate"]        = entry_rate
                s["cum_funding"]       = 0.0
                s["fees_open"]         = fee_spot_open + fee_perp_open
                opens_per_hour[i]     += 1

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

    # Финал
    for c, s in state.items():
        if not s["in_position"]:
            continue
        valid_close = s["close"][s["valid"]]
        if len(valid_close) == 0:
            continue
        P  = valid_close[-1]
        ep = s["entry_price"]
        ss = s["short_size"]
        us = s["units_spot"]
        short_realized    = ss * (ep - P)
        fee_perp_close    = ss * P * PERP_TAKER
        fee_spot_close    = us * P * SPOT_TAKER
        s["cash"] += short_realized - fee_perp_close + us * P - fee_spot_close
        s["short_size"] = 0.0
        s["units_spot"] = 0.0
        equity_now = s["cash"]
        pnl_per_hour[-1] += equity_now - s["equity_prev"]
        s["equity_prev"]  = equity_now

        # Незакрытый трейд — записываем тоже
        gross_funding = s["cum_funding"]
        fees_open     = s["fees_open"]
        fees_close    = fee_perp_close + fee_spot_close
        spot_pnl      = us * (P - ep)
        basis_pnl     = short_realized
        net_pnl       = gross_funding - fees_open - fees_close + spot_pnl + basis_pnl
        n_hours_held  = s["hours_since"]
        trade_log.append({
            "coin":               c,
            "open_idx":           s["open_idx"],
            "close_idx":          n - 1,
            "open_ts":            s["open_ts"],
            "close_ts":           common_idx[-1],
            "hold_hours":         n_hours_held,
            "entry_price":        round(ep, 4),
            "exit_price":         round(P, 4),
            "entry_rate_annual":  round(s["entry_rate"] * 100, 2),
            "gross_funding":      round(gross_funding, 4),
            "fees_open":          round(fees_open, 4),
            "fees_close":         round(fees_close, 4),
            "spot_pnl":           round(spot_pnl, 4),
            "basis_pnl":          round(basis_pnl, 4),
            "net_pnl":            round(net_pnl, 4),
        })

    return pnl_per_hour, cap_per_hour, pd.DataFrame(trade_log), common_idx


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.float_format", "{:.2f}".format)

    # Определяем cutoff last_90d на основе максимального timestamp в данных
    # (используем BTC как референс)
    ref_df   = load_data("BTC", with_ohlcv=False)
    end_ts   = ref_df.index[-1]
    cut_90d  = end_ts - pd.Timedelta(days=DAYS_90)

    print(f"Период анализа: конец={end_ts.date()}, cut_90d={cut_90d.date()}")
    print(f"Капитал на стратегию: ${CAPITAL_BASE:,} (K={K} × $2000)")
    print()

    # ── ТАБЛИЦА 1: Funding stats ───────────────────────────────────────────────
    print("=" * 100)
    print("ТАБЛИЦА 1: Avg/Median/P90/P95/Max annualized funding (12h MA signal, %) — по монетам и периодам")
    print("=" * 100)

    stats_df, negative_df = compute_funding_stats()

    periods_order = ["2023", "2024", "2025", "2026", "full", "last_90d"]
    pivot_mean = (stats_df
                  .pivot(index="coin", columns="period", values="mean")
                  .reindex(columns=[p for p in periods_order if p in stats_df["period"].unique()])
                  .round(2))

    print("\n  Mean funding (annualized %):")
    print(pivot_mean.to_string())

    pivot_median = (stats_df
                    .pivot(index="coin", columns="period", values="median")
                    .reindex(columns=[p for p in periods_order if p in stats_df["period"].unique()])
                    .round(2))
    print("\n  Median funding (annualized %):")
    print(pivot_median.to_string())

    pivot_p90 = (stats_df
                 .pivot(index="coin", columns="period", values="p90")
                 .reindex(columns=[p for p in periods_order if p in stats_df["period"].unique()])
                 .round(2))
    print("\n  P90 funding (annualized %):")
    print(pivot_p90.to_string())

    pivot_p95 = (stats_df
                 .pivot(index="coin", columns="period", values="p95")
                 .reindex(columns=[p for p in periods_order if p in stats_df["period"].unique()])
                 .round(2))
    print("\n  P95 funding (annualized %):")
    print(pivot_p95.to_string())

    pivot_max = (stats_df
                 .pivot(index="coin", columns="period", values="max")
                 .reindex(columns=[p for p in periods_order if p in stats_df["period"].unique()])
                 .round(2))
    print("\n  Max funding (annualized %):")
    print(pivot_max.to_string())

    # ── ТАБЛИЦА 2: Histogram ───────────────────────────────────────────────────
    print()
    print("=" * 100)
    print("ТАБЛИЦА 2: Гистограмма raw funding rate (annualized %) за last_90d — все U7-монеты суммарно")
    print("=" * 100)

    hist_df, n_total = compute_histogram()
    print(f"\n  Всего наблюдений (coin × hour): {n_total:,}")
    print(f"  HL default floor: 10.95% (= 0.0000125 × 8760 × 100)")
    print()
    print(hist_df.to_string(index=False))

    # ── ТАБЛИЦА 3: Negative funding hours ─────────────────────────────────────
    print()
    print("=" * 100)
    print("ТАБЛИЦА 3: % часов с negative funding в last_90d (raw rate < 0)")
    print("=" * 100)
    print()
    print(negative_df[["coin", "n_hrs", "neg_pct", "mean_raw", "median_raw"]].to_string(index=False))

    # ── P&L BREAKDOWN ─────────────────────────────────────────────────────────
    print()
    print("=" * 100)
    print("Симуляция с P&L breakdown (полная история) ...")
    print("=" * 100)

    pnl_arr, cap_arr, trade_df, common_idx = simulate_with_breakdown(
        COINS,
        max_concurrent   = K,
        entry_threshold  = ENTRY_THR,
        exit_threshold   = EXIT_THR,
        base_min_hold    = BASE_MIN_HOLD,
        signal_window    = SIGNAL_WINDOW,
        safety_mult      = SAFETY_MULT,
        cap_min_hold     = CAP_MIN_HOLD,
    )

    # Вычисляем full period metrics
    n_full      = len(pnl_arr)
    total_pnl_f = pnl_arr.sum()
    annual_f    = total_pnl_f / CAPITAL_BASE / (n_full / HOURS_PER_YEAR) * 100
    print(f"\n  Full period: total_pnl=${total_pnl_f:.2f}, annual={annual_f:.2f}%")
    print(f"  Total trades: {len(trade_df)}")

    # Срез last_90d: трейды чей close_ts >= cut_90d
    if not trade_df.empty:
        trade_df["open_ts"]  = pd.to_datetime(trade_df["open_ts"],  utc=True)
        trade_df["close_ts"] = pd.to_datetime(trade_df["close_ts"], utc=True)
        cut_ts = pd.Timestamp(cut_90d).tz_convert("UTC") if cut_90d.tzinfo else pd.Timestamp(cut_90d, tz="UTC")
        # Включаем трейд если он закрылся в last_90d
        # (вариант b из задачи: фильтрация по close_ts)
        trades_90 = trade_df[trade_df["close_ts"] >= cut_ts].copy()
    else:
        trades_90 = pd.DataFrame()

    print(f"  Трейдов с close_ts >= {cut_90d.date()}: {len(trades_90)}")

    # ── ТАБЛИЦА 4: P&L breakdown суммарно last_90d ────────────────────────────
    print()
    print("=" * 100)
    print("ТАБЛИЦА 4: P&L breakdown за last_90d (трейды с close_ts в last_90d)")
    print("=" * 100)

    if not trades_90.empty:
        gross_funding_sum = trades_90["gross_funding"].sum()
        fees_open_sum     = trades_90["fees_open"].sum()
        fees_close_sum    = trades_90["fees_close"].sum()
        fees_total_sum    = fees_open_sum + fees_close_sum
        spot_pnl_sum      = trades_90["spot_pnl"].sum()
        basis_pnl_sum     = trades_90["basis_pnl"].sum()
        net_pnl_sum       = trades_90["net_pnl"].sum()

        def pct_cap(x):
            return round(x / CAPITAL_BASE * 100, 3)

        breakdown = [
            ("gross_funding",     gross_funding_sum, pct_cap(gross_funding_sum)),
            ("fees_open",        -fees_open_sum,     pct_cap(-fees_open_sum)),
            ("fees_close",       -fees_close_sum,    pct_cap(-fees_close_sum)),
            ("fees_total",       -fees_total_sum,    pct_cap(-fees_total_sum)),
            ("spot_pnl",          spot_pnl_sum,      pct_cap(spot_pnl_sum)),
            ("basis_pnl",         basis_pnl_sum,     pct_cap(basis_pnl_sum)),
            ("spot+basis",        spot_pnl_sum + basis_pnl_sum,
                                                    pct_cap(spot_pnl_sum + basis_pnl_sum)),
            ("net_pnl",           net_pnl_sum,       pct_cap(net_pnl_sum)),
        ]

        # Вычисляем annual из pnl_arr за last_90d
        n_90d_hours = DAYS_90 * 24
        start_idx_90 = max(0, n_full - n_90d_hours)
        pnl_90_arr   = pnl_arr[start_idx_90:]
        total_pnl_90_arr = pnl_90_arr.sum()
        annual_90_arr    = total_pnl_90_arr / CAPITAL_BASE / (len(pnl_90_arr) / HOURS_PER_YEAR) * 100

        print(f"\n  Capital base: ${CAPITAL_BASE:,}")
        print(f"  Trades in last_90d: {len(trades_90)}")
        print(f"  Annual (from pnl_arr last_90d slice): {annual_90_arr:.2f}%")
        print(f"  Net PnL from trades_90 sum: ${net_pnl_sum:.2f}")
        print()

        hdr = f"  {'Component':<20}  {'$':>10}  {'% capital':>10}"
        print(hdr)
        print("  " + "-" * 45)
        for name, val, pct in breakdown:
            print(f"  {name:<20}  {val:>10.2f}  {pct:>10.3f}%")

        # Fees as % of gross funding
        if gross_funding_sum != 0:
            fees_pct_of_gross = fees_total_sum / abs(gross_funding_sum) * 100
            print(f"\n  Fees как % от gross_funding: {fees_pct_of_gross:.1f}%")

        # Аннуализированный доход только от funding для last_90d трейдов
        hold_hours_total = trades_90["hold_hours"].sum()
        avg_funding_rate = gross_funding_sum / hold_hours_total * HOURS_PER_YEAR / CAPITAL_BASE * 100 if hold_hours_total > 0 else 0
        print(f"  Avg implied funding rate (от капитала): {avg_funding_rate:.2f}% annual")
        print(f"  Total hold_hours across trades: {hold_hours_total}")
    else:
        print("  Нет трейдов в last_90d.")

    # ── ТАБЛИЦА 5: Trade-by-trade last_90d ────────────────────────────────────
    print()
    print("=" * 100)
    print("ТАБЛИЦА 5: Trade-by-trade breakdown last_90d")
    print("=" * 100)

    if not trades_90.empty:
        show_cols = [
            "coin", "open_ts", "close_ts", "hold_hours",
            "entry_rate_annual",
            "gross_funding", "fees_open", "fees_close",
            "spot_pnl", "basis_pnl", "net_pnl",
        ]
        t5 = trades_90[show_cols].copy()
        t5["open_ts"]  = t5["open_ts"].dt.strftime("%Y-%m-%d %H:%M")
        t5["close_ts"] = t5["close_ts"].dt.strftime("%Y-%m-%d %H:%M")
        t5 = t5.rename(columns={
            "entry_rate_annual": "entry_rate%",
            "gross_funding":     "gross_fund$",
            "fees_open":         "fees_open$",
            "fees_close":        "fees_cls$",
            "spot_pnl":          "spot_pnl$",
            "basis_pnl":         "basis_pnl$",
            "net_pnl":           "net_pnl$",
        })
        print()
        print(t5.to_string(index=False))

        # Avg breakeven hold hours vs actual
        if not trades_90.empty:
            entry_rates = trades_90["entry_rate_annual"].values / 100   # convert back to decimal
            breakevens  = np.where(entry_rates > 0, BREAKEVEN_CONST / entry_rates, 9999)
            avg_be      = breakevens.mean()
            avg_actual  = trades_90["hold_hours"].mean()
            print(f"\n  Avg breakeven hold (hours): {avg_be:.0f}")
            print(f"  Avg actual hold (hours):    {avg_actual:.0f}")
            print(f"  Ratio actual/breakeven:     {avg_actual/avg_be:.1f}×")
    else:
        print("  Нет трейдов в last_90d.")

    # ── Дополнительно: сравнение avg funding full vs last_90d ─────────────────
    print()
    print("=" * 100)
    print("ДОПОЛНИТЕЛЬНО: Сравнение средней реализованной ставки по трейдам — full vs last_90d")
    print("=" * 100)

    if not trade_df.empty:
        # Full period
        gf_full  = trade_df["gross_funding"].sum()
        hh_full  = trade_df["hold_hours"].sum()
        avg_f_full = gf_full / hh_full * HOURS_PER_YEAR / CAPITAL_BASE * 100 if hh_full > 0 else 0

        # last_90d
        avg_f_90 = avg_funding_rate if not trades_90.empty else 0

        print(f"\n  Full period — avg realized funding rate:   {avg_f_full:.2f}% annual")
        print(f"  Last 90d  — avg realized funding rate:    {avg_f_90:.2f}% annual")
        print(f"  Разница:                                  {avg_f_full - avg_f_90:.2f}pp")
        print()
        print(f"  Full period: {len(trade_df)} трейдов, {hh_full} hold_hours")
        if not trades_90.empty:
            print(f"  Last 90d:   {len(trades_90)} трейдов, {hold_hours_total} hold_hours")


if __name__ == "__main__":
    main()
