"""
Б_hedge + constant-dollar rebalancing (ratchet) — несколько версий.

v1 — базовый:
  Цена выросла → продаём излишек spot выше $1000.
  Hedge закрывается → сразу доливаем spot обратно до $1000 за счёт cash.
  Cash под 5% (Aave).

v2 — asymmetric refill:
  Доливаем spot только когда тренд подтвердился (mom14d > 0 — цена идёт вверх).
  Это спасает на структурно-падающих монетах типа TIA.

v3 — v2 + per-coin LT hedge signal + cash под 10% (как будто работает в Стратегии А).
  BTC, INJ → mom14d_lt90d (LT-фильтр)
  ETH, SOL, AVAX, TIA → mom14d
"""

import numpy as np
import pandas as pd
from pathlib import Path
from engine import STAKING_YIELD, load_data, buy_and_hold, compute_metrics, HOURS_PER_YEAR, TOTAL_CAPITAL, POSITION_SIZE, SPOT_TAKER, PERP_TAKER

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]
REBAL_THRESHOLD = 0.05
RISK_FREE_APR   = 0.05
# Per-coin hedge: для BTC/INJ LT-фильтр улучшает Calmar, для остальных — нет
LT_FILTERED_COINS = {"BTC", "INJ"}


def build_mom14d(close: np.ndarray) -> np.ndarray:
    mom = pd.Series(close).pct_change(14 * 24).fillna(0).values
    return (mom < 0)


def build_mom14d_lt90d(close: np.ndarray) -> np.ndarray:
    s = pd.Series(close)
    mom14 = s.pct_change(14 * 24).fillna(0).values
    mom90 = s.pct_change(90 * 24).fillna(0).values
    return (mom14 < 0) & (mom90 < 0)


def build_trend_up(close: np.ndarray) -> np.ndarray:
    """Для refill confirm: mom14d > 0 (тренд развернулся вверх)."""
    mom = pd.Series(close).pct_change(14 * 24).fillna(0).values
    return (mom > 0)


def simulate_constdollar(df, staking, hedge_signal,
                          rebal_threshold=REBAL_THRESHOLD,
                          risk_free_apr=RISK_FREE_APR,
                          refill_confirm=None):
    close = df["close"].values
    rates = df["fundingRate"].values
    n = len(df)
    stk_ph = staking / HOURS_PER_YEAR
    rf_ph  = risk_free_apr / HOURS_PER_YEAR
    refill_pending = False  # для v2: ждать подтверждения тренда

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

            # После закрытия хеджа — пометить для refill (или сразу залить если v1)
            spot_value = units_spot * P
            if spot_value < POSITION_SIZE:
                if refill_confirm is None:
                    # v1: сразу заливаем
                    need = POSITION_SIZE - spot_value
                    buy = min(need, max(cash - POSITION_SIZE, 0))
                    if buy > 0:
                        buy_units = buy / P
                        fee = buy * SPOT_TAKER
                        cash -= buy + fee
                        spot_fees_total += fee
                        units_spot += buy_units
                        rebals += 1
                else:
                    # v2: ждём подтверждения тренда
                    refill_pending = True

        # 2) Если refill_pending и тренд развернулся — доливаем
        if refill_pending and not in_pos and refill_confirm is not None:
            if bool(refill_confirm[i]):
                spot_value = units_spot * P
                if spot_value < POSITION_SIZE:
                    need = POSITION_SIZE - spot_value
                    buy = min(need, max(cash - POSITION_SIZE, 0))
                    if buy > 0:
                        buy_units = buy / P
                        fee = buy * SPOT_TAKER
                        cash -= buy + fee
                        spot_fees_total += fee
                        units_spot += buy_units
                        rebals += 1
                refill_pending = False

        # 3) Rebalance вне хеджа: если spot value сильно выше $1000 — продать излишек
        if not in_pos:
            spot_value = units_spot * P
            if spot_value > POSITION_SIZE * (1 + rebal_threshold):
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


def run(df, staking, mode, hedge, trend_up, years, bh_m):
    """Запустить один конфиг и вернуть row."""
    from backtest_b_voltgt import simulate_voltgt

    if mode == "floating":
        pnl, info = simulate_voltgt(df, staking, None, hedge)
        rebals = 0
    elif mode == "const_v1":
        pnl, info = simulate_constdollar(df, staking, hedge,
                                          risk_free_apr=0.05, refill_confirm=None)
        rebals = info["rebals"]
    elif mode == "const_v2":
        pnl, info = simulate_constdollar(df, staking, hedge,
                                          risk_free_apr=0.05, refill_confirm=trend_up)
        rebals = info["rebals"]
    elif mode == "const_v3":
        pnl, info = simulate_constdollar(df, staking, hedge,
                                          risk_free_apr=0.10, refill_confirm=trend_up)
        rebals = info["rebals"]
    else:
        raise ValueError(mode)
    m = compute_metrics(pnl)
    return {
        "mode":      mode,
        "annual":    m["annual_pct"],
        "max_dd":    m["max_dd_pct"],
        "calmar":    m["calmar"],
        "sharpe":    m["sharpe"],
        "trades":    info["trades"],
        "rebals":    rebals,
        "funding":   round(info["funding_total"]/TOTAL_CAPITAL/years*100, 2),
        "hedge_pnl": round(info["short_realized_pnl"]/TOTAL_CAPITAL/years*100, 2),
        "fees":      round(-(info["perp_fees_total"]+info["spot_fees_total"])/TOTAL_CAPITAL/years*100, 2),
        "bh_annual": bh_m["annual_pct"],
        "bh_dd":     bh_m["max_dd_pct"],
    }


def main():
    rows = []
    for coin in COINS:
        df = load_data(coin)
        if df.empty:
            continue
        staking = STAKING_YIELD.get(coin, 0.0)
        years = len(df) / HOURS_PER_YEAR
        close = df["close"].values

        # Per-coin hedge сигнал
        if coin in LT_FILTERED_COINS:
            hedge = build_mom14d_lt90d(close)
        else:
            hedge = build_mom14d(close)
        trend_up = build_trend_up(close)

        bh_pnl = buy_and_hold(df, staking)
        bh_m   = compute_metrics(bh_pnl)

        for mode in ("floating", "const_v1", "const_v2", "const_v3"):
            r = run(df, staking, mode, hedge, trend_up, years, bh_m)
            r["coin"] = coin
            r["hedge"] = "mom14d_lt90d" if coin in LT_FILTERED_COINS else "mom14d"
            rows.append(r)

    df_res = pd.DataFrame(rows)

    print("\n" + "="*130)
    print("Constant-dollar v1/v2/v3 vs floating. Per-coin hedge: BTC/INJ=mom14d_lt90d, остальные=mom14d.")
    print("v1 = ratchet с мгновенным refill; v2 = с trend-confirm refill; v3 = v2 + cash под 10% (Стратегия A yield)")
    print("="*130)

    for coin in df_res["coin"].unique():
        sub = df_res[df_res["coin"] == coin]
        bh_a = sub.iloc[0]["bh_annual"]
        bh_d = sub.iloc[0]["bh_dd"]
        hedge_name = sub.iloc[0]["hedge"]
        print(f"\n{coin} ({hedge_name}): buy & hold = {bh_a:.1f}% / DD {bh_d:.1f}% / Calmar {bh_a/bh_d:.2f}")
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
