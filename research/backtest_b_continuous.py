"""
Strategy B — идея №1: КОНТИНУАЛЬНЫЙ hedge-ratio вместо бинарного on/off.

Гипотеза: масштабировать долю хеджа по силе тренда (ratio in [0,1]) менее
чувствительно к точному таймингу => стабильнее OOS, меньше churn.

ratio[i] = clip(-mom30 / H, 0, 1)   (H = просадка за 30д для полного хеджа)
Resize только если |target-short|*P > band*$1000 (deadband против churn).
Шорт переменного размера учитываем через почасовой MTM в cash (без VWAP-возни).

Сравнение — на ТЕХ ЖЕ walk-forward test-окнах, что бинарный (fixed mom14|mom30,
OOS Calmar 0.32). Никакого фита параметров в окне: H фиксирован => полностью OOS.
Косты: thr=0.20, slip=5bps, lag=1, cash=4%. Train=12мес/Test=3мес.
"""
import numpy as np
import pandas as pd
from pathlib import Path
from engine import (STAKING_YIELD, load_data, buy_and_hold, compute_metrics,
                    HOURS_PER_YEAR, TOTAL_CAPITAL, POSITION_SIZE, PERP_TAKER, SPOT_TAKER)
from backtest_b_constdollar import simulate_constdollar

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]
THR, SLIP, LAG, CASH = 0.20, 0.0005, 1, 0.04
MONTH_H = 730
TRAIN_H, TEST_H = 12 * MONTH_H, 3 * MONTH_H
H_GRID = [0.10, 0.15, 0.25]
RESIZE_BAND = 0.10


def cagr(pnl):
    years = len(pnl) / HOURS_PER_YEAR
    end = TOTAL_CAPITAL + float(np.sum(pnl))
    return (((end / TOTAL_CAPITAL) ** (1 / years) - 1) * 100) if end > 0 else -100.0


def calmar(pnl):
    dd = compute_metrics(pnl)["max_dd_pct"]
    return cagr(pnl) / dd if dd > 0 else 0


def hedge_ratio(close, H, window=30):
    mom = pd.Series(close).pct_change(window * 24).fillna(0).values
    return np.clip(-mom / H, 0.0, 1.0)


def simulate_cont(df, staking, ratio, rebal_threshold=THR, risk_free_apr=CASH,
                  signal_lag=LAG, slippage=SLIP, resize_band=RESIZE_BAND):
    close = df["close"].values
    rates = df["fundingRate"].values
    n = len(df)
    stk_ph, rf_ph = staking / HOURS_PER_YEAR, risk_free_apr / HOURS_PER_YEAR
    perp_cost, spot_cost = PERP_TAKER + slippage, SPOT_TAKER + slippage

    cash = TOTAL_CAPITAL - POSITION_SIZE
    P0 = float(close[0])
    units_spot = POSITION_SIZE / P0
    cash -= POSITION_SIZE * spot_cost
    short_size = 0.0          # units шорта (>=0)
    prev_P = P0
    short_pnl_total, perp_fees, spot_fees = 0.0, 0.0, POSITION_SIZE * spot_cost
    hours_hedged = 0.0
    pnl_arr = np.zeros(n)
    eq_prev = TOTAL_CAPITAL

    for i in range(n):
        P = float(close[i])
        # MTM шорта за бар (на размере, что держали)
        pnl_short = -short_size * (P - prev_P)
        cash += pnl_short
        short_pnl_total += pnl_short
        prev_P = P
        # стейкинг + cash yield
        if units_spot > 0:
            units_spot *= (1 + stk_ph)
        if cash > 0:
            cash *= (1 + rf_ph)
        # funding на текущий шорт
        cash += short_size * P * float(rates[i])
        if short_size > 0:
            hours_hedged += 1

        r = ratio[i - signal_lag] if i - signal_lag >= 0 else 0.0
        target = r * units_spot
        if abs(target - short_size) * P > resize_band * POSITION_SIZE:
            delta = target - short_size
            cash -= abs(delta) * P * perp_cost
            perp_fees += abs(delta) * P * perp_cost
            short_size = target

        # constant-dollar ratchet на спот (когда в основном НЕ хеджированы)
        if r < 0.5:
            sv = units_spot * P
            if sv > POSITION_SIZE * (1 + rebal_threshold):
                excess = sv - POSITION_SIZE
                cash += excess - excess * spot_cost
                spot_fees += excess * spot_cost
                units_spot -= excess / P
            elif sv < POSITION_SIZE and r < 0.3:
                need = POSITION_SIZE - sv
                buy = min(need, max(cash - POSITION_SIZE, 0))
                if buy > 0:
                    cash -= buy + buy * spot_cost
                    spot_fees += buy * spot_cost
                    units_spot += buy / P

        equity = cash + units_spot * P    # шорт промаркирован в cash => без unrealized
        pnl_arr[i] = equity - eq_prev
        eq_prev = equity

    # финальное закрытие
    extra = 0.0
    if short_size > 0:
        f = short_size * float(close[-1]) * perp_cost
        cash -= f; perp_fees += f; extra -= f
    if units_spot > 0:
        f = units_spot * float(close[-1]) * spot_cost
        cash -= f; spot_fees += f; extra -= f
    pnl_arr[-1] += extra

    years = n / HOURS_PER_YEAR
    info = {"short_realized_pnl": short_pnl_total,
            "fees": -(perp_fees + spot_fees),
            "pct_hedged": hours_hedged / n * 100}
    return pnl_arr, info


def run_wf(D):
    """Walk-forward OOS на test-окнах: continuous(H) vs binary vs BH."""
    streams = {f"cont H={H}": {} for H in H_GRID}
    streams["binary base"] = {}
    streams["buy & hold"] = {}
    for coin, df in D.items():
        close = df["close"].values
        stk = STAKING_YIELD.get(coin, 0.0)
        refill = (pd.Series(close).pct_change(14 * 24).fillna(0).values > 0)
        m14 = (pd.Series(close).pct_change(14 * 24).fillna(0).values < 0)
        m30 = (pd.Series(close).pct_change(30 * 24).fillna(0).values < 0)
        base_sig = m14 | m30
        ratios = {H: hedge_ratio(close, H) for H in H_GRID}
        n = len(df)
        buckets = {k: [] for k in streams}
        s = 0
        while s + TRAIN_H + TEST_H <= n:
            a, b = s + TRAIN_H, s + TRAIN_H + TEST_H
            for H in H_GRID:
                p, _ = simulate_cont(df.iloc[a:b], stk, ratios[H][a:b])
                buckets[f"cont H={H}"].append(p)
            pb, _ = simulate_constdollar(df.iloc[a:b], stk, base_sig[a:b],
                                         rebal_threshold=THR, risk_free_apr=CASH,
                                         refill_confirm=refill[a:b], signal_lag=LAG, slippage=SLIP)
            buckets["binary base"].append(pb)
            buckets["buy & hold"].append(buy_and_hold(df.iloc[a:b], stk))
            s += TEST_H
        for k in streams:
            streams[k][coin] = np.concatenate(buckets[k])
    return streams


def main():
    D = {c: load_data(c) for c in COINS}
    D = {c: df for c, df in D.items() if not df.empty}

    # 1) полный прогон (causal, fixed rule => OOS-эквивалент) для контекста
    print("=" * 84)
    print("CONTINUOUS hedge-ratio — полный прогон (fixed H, causal). Портфель equal-weight.")
    print("=" * 84)
    for H in H_GRID:
        cg, dd, hp = [], [], []
        for coin, df in D.items():
            p, info = simulate_cont(df, STAKING_YIELD.get(coin, 0.0), hedge_ratio(df["close"].values, H))
            cg.append(cagr(p)); dd.append(compute_metrics(p)["max_dd_pct"]); hp.append(info["pct_hedged"])
        print(f"  H={H:.2f}: CAGR={np.mean(cg):6.2f}%  avgDD={np.mean(dd):6.2f}%  "
              f"Calmar={np.mean(cg)/np.mean(dd):.2f}  pct_hedged={np.mean(hp):.1f}%")
    print("  (бинарный mom14|mom30 для справки: Calmar ~1.39)")

    # 2) walk-forward OOS, те же test-окна, что у бинарного (0.32)
    streams = run_wf(D)
    print("\n" + "=" * 84)
    print("WALK-FORWARD OOS (те же test-окна). Сравнение continuous vs binary vs BH.")
    print("=" * 84)
    rows = []
    for name, d in streams.items():
        cs = [cagr(v) for v in d.values()]
        ds = [compute_metrics(v)["max_dd_pct"] for v in d.values()]
        c, dd = np.mean(cs), np.mean(ds)
        rows.append({"stream": name, "OOS_CAGR": round(c, 2), "OOS_DD": round(dd, 2),
                     "OOS_Calmar": round(c / dd, 2)})
    res = pd.DataFrame(rows)
    print(res.to_string(index=False))
    out = Path(__file__).parent / "backtest_b_continuous_results.csv"
    res.to_csv(out, index=False)
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    main()
