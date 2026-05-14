"""
Б_hedge + constant-dollar rebalancing (ratchet).

Механика:
  Старт:                $1000 spot + $1000 cash
  Цена выросла:         продаём излишек spot до $1000, излишек → cash (lock profit)
  Цена упала, no hedge: НЕ докупаем spot (просто фиксируем подешевевшую позицию)
  Сигнал хеджа True:    short = текущий dollar value spot
  Цена упала с hedge:   short в плюсе, spot в минусе — equity стабильна
  Hedge закрывается:    realize hedge profit → cash; rebalance spot обратно до $1000
                        (если не хватает кэша — оставляем меньше)

Rebalance происходит:
  - Каждый раз когда (no_hedge AND |spot_value − $1000| > REBAL_THRESHOLD * $1000)
  - При закрытии хеджа (top-up spot до $1000 за счёт cash)

Cash зарабатывает RISK_FREE_APR (~5% USDC в Aave).
"""

import numpy as np
import pandas as pd
from pathlib import Path
from engine import STAKING_YIELD, load_data, buy_and_hold, compute_metrics, HOURS_PER_YEAR, TOTAL_CAPITAL, POSITION_SIZE, SPOT_TAKER, PERP_TAKER

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]
REBAL_THRESHOLD = 0.05   # 5% отклонение spot value от $1000 → ребалансировка
RISK_FREE_APR   = 0.05   # cash под 5% годовых


def build_mom14d(close: np.ndarray) -> np.ndarray:
    mom = pd.Series(close).pct_change(14 * 24).fillna(0).values
    return (mom < 0)


def simulate_constdollar(df, staking, hedge_signal, rebal_threshold=REBAL_THRESHOLD):
    close = df["close"].values
    rates = df["fundingRate"].values
    n = len(df)
    stk_ph = staking / HOURS_PER_YEAR
    rf_ph  = RISK_FREE_APR / HOURS_PER_YEAR

    cash         = TOTAL_CAPITAL - POSITION_SIZE
    P0           = float(close[0])
    units_spot   = POSITION_SIZE / P0
    cash        -= POSITION_SIZE * SPOT_TAKER

    short_size   = 0.0
    entry_price  = 0.0
    in_pos       = False
    trades       = 0
    rebals       = 0
    hours_in     = 0

    pnl_arr = np.zeros(n)
    equity_prev = TOTAL_CAPITAL

    funding_total   = 0.0
    short_realized  = 0.0
    perp_fees_total = 0.0
    spot_fees_total = POSITION_SIZE * SPOT_TAKER

    for i in range(n):
        P = float(close[i])
        rate = float(rates[i])

        # Стейкинг spot + risk-free на cash
        if units_spot > 0:
            units_spot *= (1 + stk_ph)
        if cash > 0:
            cash *= (1 + rf_ph)

        # Funding
        if in_pos:
            f = short_size * P * rate
            cash += f
            funding_total += f
            hours_in += 1

        want_hedge = bool(hedge_signal[i])

        # 1) Hedge entry/exit
        if not in_pos and want_hedge:
            short_size = units_spot
            entry_price = P
            fee = short_size * P * PERP_TAKER
            cash -= fee
            perp_fees_total += fee
            in_pos = True
            trades += 1

        elif in_pos and not want_hedge:
            # Закрываем хедж
            realized = short_size * (entry_price - P)
            cash += realized
            short_realized += realized
            fee = short_size * P * PERP_TAKER
            cash -= fee
            perp_fees_total += fee
            short_size = 0.0
            entry_price = 0.0
            in_pos = False

            # После закрытия хеджа — top up spot до $1000 за счёт cash
            spot_value = units_spot * P
            if spot_value < POSITION_SIZE:
                need = POSITION_SIZE - spot_value
                buy = min(need, max(cash - POSITION_SIZE, 0))  # оставляем минимум на cash
                if buy > 0:
                    buy_units = buy / P
                    fee = buy * SPOT_TAKER
                    cash -= buy + fee
                    spot_fees_total += fee
                    units_spot += buy_units
                    rebals += 1

        # 2) Rebalance вне хеджа: если spot value сильно выше $1000 — продать излишек
        if not in_pos:
            spot_value = units_spot * P
            if spot_value > POSITION_SIZE * (1 + rebal_threshold):
                # продаём излишек
                excess = spot_value - POSITION_SIZE
                sell_units = excess / P
                fee = excess * SPOT_TAKER
                cash += excess - fee
                spot_fees_total += fee
                units_spot -= sell_units
                rebals += 1

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
    }
    return pnl_arr, info


def main():
    rows = []
    for coin in COINS:
        df = load_data(coin)
        if df.empty:
            continue
        staking = STAKING_YIELD.get(coin, 0.0)
        years = len(df) / HOURS_PER_YEAR
        hedge = build_mom14d(df["close"].values)

        bh_pnl = buy_and_hold(df, staking)
        bh_m   = compute_metrics(bh_pnl)

        # Сравнение: constant-dollar vs floating spot (текущий B_hedge)
        from backtest_b_voltgt import simulate_voltgt

        pnl_cd, info_cd = simulate_constdollar(df, staking, hedge)
        m_cd = compute_metrics(pnl_cd)

        pnl_fl, info_fl = simulate_voltgt(df, staking, None, hedge)
        m_fl = compute_metrics(pnl_fl)

        rows.append({"coin": coin, "mode": "constant_dollar",
                     "annual": m_cd["annual_pct"], "max_dd": m_cd["max_dd_pct"],
                     "calmar": m_cd["calmar"], "sharpe": m_cd["sharpe"],
                     "trades": info_cd["trades"], "rebals": info_cd["rebals"],
                     "funding":   round(info_cd["funding_total"]/TOTAL_CAPITAL/years*100, 2),
                     "hedge_pnl": round(info_cd["short_realized_pnl"]/TOTAL_CAPITAL/years*100, 2),
                     "fees":      round(-(info_cd["perp_fees_total"]+info_cd["spot_fees_total"])/TOTAL_CAPITAL/years*100, 2),
                     "bh_annual": bh_m["annual_pct"], "bh_dd": bh_m["max_dd_pct"]})
        rows.append({"coin": coin, "mode": "floating_spot",
                     "annual": m_fl["annual_pct"], "max_dd": m_fl["max_dd_pct"],
                     "calmar": m_fl["calmar"], "sharpe": m_fl["sharpe"],
                     "trades": info_fl["trades"], "rebals": 0,
                     "funding":   round(info_fl["funding_total"]/TOTAL_CAPITAL/years*100, 2),
                     "hedge_pnl": round(info_fl["short_realized_pnl"]/TOTAL_CAPITAL/years*100, 2),
                     "fees":      round(-(info_fl["perp_fees_total"]+info_fl["spot_fees_total"])/TOTAL_CAPITAL/years*100, 2),
                     "bh_annual": bh_m["annual_pct"], "bh_dd": bh_m["max_dd_pct"]})

    df_res = pd.DataFrame(rows)

    print("\n" + "="*130)
    print("Constant-dollar rebalancing (ratchet) vs floating spot (исправленный B_hedge)")
    print("Сигнал хеджа: mom14d. Cash под 5% годовых.")
    print("="*130)

    for coin in df_res["coin"].unique():
        sub = df_res[df_res["coin"] == coin]
        bh_a = sub.iloc[0]["bh_annual"]
        bh_d = sub.iloc[0]["bh_dd"]
        print(f"\n{coin}: buy & hold = {bh_a:.1f}% / DD {bh_d:.1f}% / Calmar {bh_a/bh_d:.2f}")
        cols = ["mode", "annual", "max_dd", "calmar", "sharpe", "trades", "rebals",
                "funding", "hedge_pnl", "fees"]
        print(sub[cols].to_string(index=False))

    print("\n" + "="*130)
    print("Усреднённый портфель")
    print("="*130)
    agg = (df_res.groupby("mode")
                 .agg(annual=("annual","mean"),
                      max_dd=("max_dd","mean"),
                      sharpe=("sharpe","mean"),
                      funding=("funding","mean"),
                      hedge_pnl=("hedge_pnl","mean"),
                      fees=("fees","mean"))
                 .round(2))
    agg["calmar"] = (agg["annual"] / agg["max_dd"]).round(2)
    print(agg[["annual","max_dd","calmar","sharpe","funding","hedge_pnl","fees"]].to_string())

    bh_a = df_res.groupby("coin")["bh_annual"].first().mean()
    bh_d = df_res.groupby("coin")["bh_dd"].first().mean()
    print(f"\nbuy & hold: annual={bh_a:.2f}%, max_dd={bh_d:.2f}%, calmar={bh_a/bh_d:.2f}")

    out = Path(__file__).parent / "backtest_b_constdollar_results.csv"
    df_res.to_csv(out, index=False)
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    main()
