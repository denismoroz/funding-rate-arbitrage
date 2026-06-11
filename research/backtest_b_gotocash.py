"""
Strategy B — идея №3: GO-TO-CASH вместо шорта.

При risk-off сигнале (mom14|mom30) ПРОДАЁМ спот в cash, а не открываем шорт.
Нет перпа => нет funding, нет проскальзывания перпа. По сути long/flat
momentum на споте + constant-dollar $1000.

Сравнение на ТЕХ ЖЕ walk-forward test-окнах, что бинарный short-hedge
(OOS Calmar 0.32). Сигнал тот же => проверяем, меняет ли что-то иная механика.
Косты: thr=0.20, slip=5bps (на спот), lag=1, cash=4%. Train=12/Test=3 мес.
"""
import numpy as np
import pandas as pd
from pathlib import Path
from engine import (STAKING_YIELD, load_data, buy_and_hold, compute_metrics,
                    HOURS_PER_YEAR, TOTAL_CAPITAL, POSITION_SIZE, SPOT_TAKER)
from backtest_b_constdollar import simulate_constdollar

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]
THR, SLIP, LAG, CASH = 0.20, 0.0005, 1, 0.04
MONTH_H = 730
TRAIN_H, TEST_H = 12 * MONTH_H, 3 * MONTH_H


def cagr(pnl):
    years = len(pnl) / HOURS_PER_YEAR
    end = TOTAL_CAPITAL + float(np.sum(pnl))
    return (((end / TOTAL_CAPITAL) ** (1 / years) - 1) * 100) if end > 0 else -100.0


def simulate_gotocash(df, staking, riskoff, rebal_threshold=THR, risk_free_apr=CASH,
                      signal_lag=LAG, slippage=SLIP):
    close = df["close"].values
    n = len(df)
    stk_ph, rf_ph = staking / HOURS_PER_YEAR, risk_free_apr / HOURS_PER_YEAR
    spot_cost = SPOT_TAKER + slippage

    cash = TOTAL_CAPITAL - POSITION_SIZE
    P0 = float(close[0])
    units_spot = POSITION_SIZE / P0
    cash -= POSITION_SIZE * spot_cost
    spot_fees = POSITION_SIZE * spot_cost
    hours_flat = 0.0
    pnl_arr = np.zeros(n)
    eq_prev = TOTAL_CAPITAL

    for i in range(n):
        P = float(close[i])
        if units_spot > 0:
            units_spot *= (1 + stk_ph)
        if cash > 0:
            cash *= (1 + rf_ph)

        off = riskoff[i - signal_lag] if i - signal_lag >= 0 else False
        sv = units_spot * P
        if off:
            # уходим в кэш: продаём весь спот
            if units_spot > 0:
                cash += sv - sv * spot_cost
                spot_fees += sv * spot_cost
                units_spot = 0.0
            hours_flat += 1
        else:
            # risk-on: держим $1000 спота (constant-dollar)
            if sv > POSITION_SIZE * (1 + rebal_threshold):
                excess = sv - POSITION_SIZE
                cash += excess - excess * spot_cost
                spot_fees += excess * spot_cost
                units_spot -= excess / P
            elif sv < POSITION_SIZE:
                buy = min(POSITION_SIZE - sv, max(cash, 0))
                if buy > 0:
                    cash -= buy + buy * spot_cost
                    spot_fees += buy * spot_cost
                    units_spot += buy / P

        equity = cash + units_spot * P
        pnl_arr[i] = equity - eq_prev
        eq_prev = equity

    if units_spot > 0:
        f = units_spot * float(close[-1]) * spot_cost
        pnl_arr[-1] -= f
    return pnl_arr, {"pct_flat": hours_flat / n * 100, "fees": -spot_fees}


def main():
    D = {c: load_data(c) for c in COINS}
    D = {c: df for c, df in D.items() if not df.empty}

    # in-sample контекст
    print("=" * 80)
    print("GO-TO-CASH — полный прогон (causal). Портфель equal-weight.")
    print("=" * 80)
    cg, dd, fl = [], [], []
    for coin, df in D.items():
        close = df["close"].values
        m14 = (pd.Series(close).pct_change(14 * 24).fillna(0).values < 0)
        m30 = (pd.Series(close).pct_change(30 * 24).fillna(0).values < 0)
        p, info = simulate_gotocash(df, STAKING_YIELD.get(coin, 0.0), m14 | m30)
        cg.append(cagr(p)); dd.append(compute_metrics(p)["max_dd_pct"]); fl.append(info["pct_flat"])
    print(f"  CAGR={np.mean(cg):.2f}%  avgDD={np.mean(dd):.2f}%  Calmar={np.mean(cg)/np.mean(dd):.2f}  "
          f"pct_in_cash={np.mean(fl):.1f}%")
    print("  (бинарный short-hedge для справки: Calmar ~1.39)")

    # walk-forward OOS
    g2c, binb, bh = {}, {}, {}
    for coin, df in D.items():
        close = df["close"].values
        stk = STAKING_YIELD.get(coin, 0.0)
        m14 = (pd.Series(close).pct_change(14 * 24).fillna(0).values < 0)
        m30 = (pd.Series(close).pct_change(30 * 24).fillna(0).values < 0)
        base_sig = m14 | m30
        refill = (pd.Series(close).pct_change(14 * 24).fillna(0).values > 0)
        n = len(df)
        bg, bb, bbh, s = [], [], [], 0
        while s + TRAIN_H + TEST_H <= n:
            a, b = s + TRAIN_H, s + TRAIN_H + TEST_H
            pg, _ = simulate_gotocash(df.iloc[a:b], stk, base_sig[a:b])
            pb, _ = simulate_constdollar(df.iloc[a:b], stk, base_sig[a:b],
                                         rebal_threshold=THR, risk_free_apr=CASH,
                                         refill_confirm=refill[a:b], signal_lag=LAG, slippage=SLIP)
            bg.append(pg); bb.append(pb); bbh.append(buy_and_hold(df.iloc[a:b], stk))
            s += TEST_H
        g2c[coin] = np.concatenate(bg); binb[coin] = np.concatenate(bb); bh[coin] = np.concatenate(bbh)

    print("\n" + "=" * 80)
    print("WALK-FORWARD OOS (те же test-окна).")
    print("=" * 80)
    rows = []
    for name, d in [("go-to-cash", g2c), ("binary short-hedge", binb), ("buy & hold", bh)]:
        cs = [cagr(v) for v in d.values()]
        ds = [compute_metrics(v)["max_dd_pct"] for v in d.values()]
        c, ddv = np.mean(cs), np.mean(ds)
        rows.append({"stream": name, "OOS_CAGR": round(c, 2), "OOS_DD": round(ddv, 2),
                     "OOS_Calmar": round(c / ddv, 2)})
    res = pd.DataFrame(rows)
    print(res.to_string(index=False))
    print("\nper-coin OOS go-to-cash:")
    for coin in g2c:
        v = g2c[coin]
        print(f"  {coin:5s} CAGR={cagr(v):6.2f}%  DD={compute_metrics(v)['max_dd_pct']:6.2f}%")
    res.to_csv(Path(__file__).parent / "backtest_b_gotocash_results.csv", index=False)
    print("\nСохранено: backtest_b_gotocash_results.csv")


if __name__ == "__main__":
    main()
