"""
Стратегия Б_hedge + volatility targeting.

Идея: целевая годовая волатильность portfolio = X% (например 30%).
Размер spot-позиции масштабируется обратно пропорционально realized vol монеты.
В calm period — больше spot. В turbulent — меньше.

Реализация:
  - realized_vol = rolling 30-day stdev of hourly returns × sqrt(8760)
  - scale[i] = clip(target_vol / realized_vol[i], 0.2, 2.0)
  - На старте: buy spot = POSITION_SIZE × scale[0]
  - Ежемесячная ребалансировка (каждые 720 часов): продать/докупить spot до новой scale
  - Hedge (mom14d) применяется к ТЕКУЩЕМУ effective_position_size

Сравнение:
  - no_target  — обычный mom14d hedge (scale ≡ 1)
  - target 30/50/70% годовых vol
"""

import numpy as np
import pandas as pd
from pathlib import Path
from engine import (
    STAKING_YIELD, load_data, buy_and_hold, compute_metrics,
    POSITION_SIZE, TOTAL_CAPITAL, PERP_TAKER, SPOT_TAKER, HOURS_PER_YEAR,
)

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]
VOL_LOOKBACK_H  = 30 * 24
REBAL_PERIOD_H  = 30 * 24
SCALE_MIN, SCALE_MAX = 0.2, 2.0


def compute_scale(close: np.ndarray, target_vol: float | None) -> np.ndarray:
    """Возвращает массив scale[i]. Если target_vol=None — единичка везде."""
    n = len(close)
    if target_vol is None:
        return np.ones(n)
    rets = pd.Series(close).pct_change().fillna(0)
    rstd = rets.rolling(VOL_LOOKBACK_H, min_periods=VOL_LOOKBACK_H).std()
    rvol = (rstd * np.sqrt(HOURS_PER_YEAR)).values
    rvol = np.where(np.isnan(rvol), 0.6, rvol)  # warmup fallback = 60% годовых
    scale = np.clip(target_vol / rvol, SCALE_MIN, SCALE_MAX)
    return scale


def build_mom14d_signal(close: np.ndarray) -> np.ndarray:
    mom = pd.Series(close).pct_change(14 * 24).fillna(0).values
    return (mom < 0)


def simulate_voltgt(df, staking, target_vol, hedge_signal):
    close = df["close"].values
    rates = df["fundingRate"].values
    n = len(df)
    stk_ph = staking / HOURS_PER_YEAR
    scale = compute_scale(close, target_vol)

    cash         = TOTAL_CAPITAL
    P0           = float(close[0])
    pos_size     = POSITION_SIZE * scale[0]
    units_spot   = pos_size / P0
    units_init   = units_spot
    cash        -= pos_size
    cash        -= pos_size * SPOT_TAKER

    short_size  = 0.0
    entry_price = 0.0
    in_pos      = False
    trades      = 0
    rebals      = 0
    hours_in    = 0
    last_rebal  = 0

    pnl_arr = np.zeros(n)
    equity_prev = TOTAL_CAPITAL

    funding_total = 0.0
    short_realized = 0.0
    perp_fees_total = 0.0
    spot_fees_total = pos_size * SPOT_TAKER

    for i in range(n):
        P = float(close[i])
        rate = float(rates[i])

        # 1) Стейкинг
        if units_spot > 0:
            units_spot *= (1 + stk_ph)

        # 2) Funding
        if in_pos:
            f = short_size * P * rate
            cash += f
            funding_total += f
            hours_in += 1

        # 3) Ежемесячная ребалансировка spot до scale[i]
        if i - last_rebal >= REBAL_PERIOD_H:
            target_units = (POSITION_SIZE * scale[i]) / P
            delta_units = target_units - units_spot
            if abs(delta_units) > 1e-9:
                trade_notional = abs(delta_units) * P
                fee = trade_notional * SPOT_TAKER
                if delta_units > 0:
                    # докупаем
                    cash -= delta_units * P
                    cash -= fee
                else:
                    # продаём
                    cash += abs(delta_units) * P
                    cash -= fee
                spot_fees_total += fee
                units_spot = target_units
                rebals += 1
            last_rebal = i

        # 4) Hedge entry/exit (полный hedge на текущий spot dollar size)
        want_hedge = bool(hedge_signal[i])
        if not in_pos and want_hedge:
            # хедж на текущий spot value
            short_size = units_spot
            entry_price = P
            fee = short_size * P * PERP_TAKER
            cash -= fee
            perp_fees_total += fee
            in_pos = True
            trades += 1
        elif in_pos and not want_hedge:
            realized = short_size * (entry_price - P)
            cash += realized
            short_realized += realized
            fee = short_size * P * PERP_TAKER
            cash -= fee
            perp_fees_total += fee
            short_size = 0.0
            entry_price = 0.0
            in_pos = False

        # MTM equity
        short_pnl = short_size * (entry_price - P) if in_pos else 0.0
        equity_now = cash + units_spot * P + short_pnl
        pnl_arr[i] = equity_now - equity_prev
        equity_prev = equity_now

    # Финал
    P_final = float(close[-1])
    extra = 0.0
    if in_pos:
        realized = short_size * (entry_price - P_final)
        cash += realized
        short_realized += realized
        fee = short_size * P_final * PERP_TAKER
        cash -= fee
        perp_fees_total += fee
        extra -= fee
        short_size = 0.0
    if units_spot > 0:
        fee = units_spot * P_final * SPOT_TAKER
        cash -= fee
        spot_fees_total += fee
        extra -= fee
    pnl_arr[-1] += extra

    info = {
        "trades":             trades,
        "rebals":             rebals,
        "hours_in_position":  hours_in,
        "funding_total":      round(funding_total, 2),
        "short_realized_pnl": round(short_realized, 2),
        "perp_fees_total":    round(perp_fees_total, 2),
        "spot_fees_total":    round(spot_fees_total, 2),
        "avg_scale":          round(scale.mean(), 3),
        "min_scale":          round(scale.min(), 3),
        "max_scale":          round(scale.max(), 3),
    }
    return pnl_arr, info


def main():
    configs = [
        ("no_target", None),
        ("tgt_30",    0.30),
        ("tgt_50",    0.50),
        ("tgt_70",    0.70),
    ]
    rows = []
    for coin in COINS:
        df = load_data(coin)
        if df.empty:
            continue
        staking = STAKING_YIELD.get(coin, 0.0)
        years = len(df) / HOURS_PER_YEAR
        hedge_sig = build_mom14d_signal(df["close"].values)

        bh_pnl = buy_and_hold(df, staking)
        bh_m   = compute_metrics(bh_pnl)

        for cfg_name, tgt in configs:
            pnl, info = simulate_voltgt(df, staking, tgt, hedge_sig)
            m = compute_metrics(pnl)
            rows.append({
                "coin":       coin,
                "config":     cfg_name,
                "target_vol": tgt if tgt else "—",
                "annual":     m["annual_pct"],
                "max_dd":     m["max_dd_pct"],
                "calmar":     m["calmar"],
                "sharpe":     m["sharpe"],
                "trades":     info["trades"],
                "rebals":     info["rebals"],
                "avg_scale":  info["avg_scale"],
                "min_scale":  info["min_scale"],
                "max_scale":  info["max_scale"],
                "funding":    round(info["funding_total"] / TOTAL_CAPITAL / years * 100, 2),
                "hedge_pnl":  round(info["short_realized_pnl"] / TOTAL_CAPITAL / years * 100, 2),
                "fees":       round(-(info["perp_fees_total"] + info["spot_fees_total"]) / TOTAL_CAPITAL / years * 100, 2),
                "bh_annual":  bh_m["annual_pct"],
                "bh_dd":      bh_m["max_dd_pct"],
            })

    df_res = pd.DataFrame(rows)
    print("\n" + "="*130)
    print("Б_hedge + volatility targeting (mom14d hedge, monthly rebal)")
    print("="*130)

    for coin in df_res["coin"].unique():
        sub = df_res[df_res["coin"] == coin]
        bh_a = sub.iloc[0]["bh_annual"]
        bh_d = sub.iloc[0]["bh_dd"]
        print(f"\n{coin}:  buy & hold = {bh_a:.1f}% / DD {bh_d:.1f}% / Calmar {bh_a/bh_d:.2f}")
        cols = ["config", "annual", "max_dd", "calmar", "sharpe", "avg_scale",
                "funding", "hedge_pnl", "fees", "trades", "rebals"]
        print(sub[cols].to_string(index=False))

    print("\n" + "="*130)
    print("Усреднённый портфель (равно-взвешенно по 6 коинам)")
    print("="*130)
    avg = (df_res.groupby("config")
                 .agg(annual=("annual","mean"),
                      max_dd=("max_dd","mean"),
                      calmar=("calmar","mean"),
                      avg_scale=("avg_scale","mean"))
                 .round(2))
    avg["portfolio_calmar"] = (avg["annual"] / avg["max_dd"]).round(2)
    print(avg.to_string())

    out = Path(__file__).parent / "backtest_b_voltgt_results.csv"
    df_res.to_csv(out, index=False)
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    main()
