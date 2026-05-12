"""
Сравнение Эксперимента А и лучшего варианта Б (s1_ma200 со стейкингом).
"""

import pandas as pd
import numpy as np
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
POSITION_SIZE  = 1000
TAKER_FEE      = 0.00035
HOURS_PER_YEAR = 8760

ENTRY_THRESHOLD = 0.20
EXIT_THRESHOLD  = -0.05
MIN_HOLD_HOURS  = 72

STAKING_YIELD = {
    "ETH":  0.035, "SOL":  0.085, "BTC":  0.0,   "ARB":  0.0,
    "OP":   0.0,   "AVAX": 0.065, "MATIC":0.04,  "DOGE": 0.0,
    "LINK": 0.0,   "UNI":  0.0,   "AAVE": 0.0,   "WIF":  0.0,
    "TIA":  0.14,  "INJ":  0.18,
}

COINS = ["BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "MATIC",
         "DOGE", "LINK", "UNI", "AAVE", "WIF", "TIA", "INJ"]


def load_funding(coin):
    df = pd.read_csv(DATA_DIR / f"{coin}.csv")
    df["time"] = pd.to_datetime(df["time"], format="ISO8601", utc=True).dt.floor("h")
    return df.set_index("time")[["fundingRate"]].sort_index()


def load_data(coin):
    funding = load_funding(coin)
    ohlcv = pd.read_csv(DATA_DIR / f"{coin}_1h.csv")
    ohlcv["time"] = pd.to_datetime(ohlcv["time"], format="ISO8601", utc=True).dt.floor("h")
    ohlcv = ohlcv.set_index("time")[["close"]].sort_index()
    df = funding.join(ohlcv, how="inner")
    df["price_return"] = df["close"].pct_change().fillna(0)
    df["ma200"] = df["close"].rolling(200, min_periods=1).mean()
    return df


def run_exp_a(df):
    """Эксперимент А: дельта-нейтральный, без спот риска."""
    rates = df["fundingRate"].values
    in_pos, pnl, trades, hours_in, hours_since = False, 0.0, 0, 0, 0
    for rate in rates:
        ar = rate * HOURS_PER_YEAR
        if not in_pos:
            if ar > ENTRY_THRESHOLD:
                in_pos = True; trades += 1; hours_since = 0
                pnl -= TAKER_FEE * POSITION_SIZE
        else:
            pnl += rate * POSITION_SIZE
            hours_in += 1; hours_since += 1
            if hours_since >= MIN_HOLD_HOURS and ar < EXIT_THRESHOLD:
                in_pos = False
                pnl -= TAKER_FEE * POSITION_SIZE
    if in_pos:
        pnl -= TAKER_FEE * POSITION_SIZE; trades += 1
    n = len(rates)
    return {
        "annual_pct": round((pnl / POSITION_SIZE) / (n / HOURS_PER_YEAR) * 100, 2),
        "pct_active": round(hours_in / n * 100, 1),
        "trades":     trades,
    }


def run_exp_b(df, staking):
    """Эксперимент Б: спот лонг всегда + шорт перп по s1_ma200."""
    rates      = df["fundingRate"].values
    price_ret  = df["price_return"].values
    close      = df["close"].values
    ma200      = df["ma200"].values
    stk_ph     = staking / HOURS_PER_YEAR

    in_pos, pnl, trades, hours_in, hours_since = False, 0.0, 0, 0, 0

    for i in range(len(rates)):
        rate = rates[i]; ar = rate * HOURS_PER_YEAR
        pnl += POSITION_SIZE * stk_ph

        if not in_pos:
            pnl += POSITION_SIZE * price_ret[i]
            below_ma = close[i] < ma200[i]
            if below_ma and ar > ENTRY_THRESHOLD:
                in_pos = True; trades += 1; hours_since = 0
                pnl -= TAKER_FEE * POSITION_SIZE
        else:
            pnl += rate * POSITION_SIZE
            hours_in += 1; hours_since += 1
            if hours_since >= MIN_HOLD_HOURS and ar < EXIT_THRESHOLD:
                in_pos = False
                pnl -= TAKER_FEE * POSITION_SIZE

    if in_pos:
        pnl -= TAKER_FEE * POSITION_SIZE; trades += 1

    n = len(rates)
    return {
        "annual_pct": round((pnl / POSITION_SIZE) / (n / HOURS_PER_YEAR) * 100, 2),
        "pct_active": round(hours_in / n * 100, 1),
        "trades":     trades,
    }


def run_buy_hold(df, staking):
    equity = POSITION_SIZE
    stk_ph = staking / HOURS_PER_YEAR
    for r in df["price_return"].values:
        equity *= (1 + r)
        equity += POSITION_SIZE * stk_ph
    pnl = equity - POSITION_SIZE
    return round((pnl / POSITION_SIZE) / (len(df) / HOURS_PER_YEAR) * 100, 2)


def main():
    rows = []
    for coin in COINS:
        if not (DATA_DIR / f"{coin}.csv").exists() or not (DATA_DIR / f"{coin}_1h.csv").exists():
            continue
        df      = load_data(coin)
        staking = STAKING_YIELD.get(coin, 0.0)
        a       = run_exp_a(df)
        b       = run_exp_b(df, staking)
        bh      = run_buy_hold(df, staking)
        rows.append({
            "coin":          coin,
            "stk%":          f"{staking*100:.0f}%",
            "buy&hold":      bh,
            "A  annual%":    a["annual_pct"],
            "A  active%":    a["pct_active"],
            "A  trades":     a["trades"],
            "B  annual%":    b["annual_pct"],
            "B  active%":    b["pct_active"],
            "B  trades":     b["trades"],
            "B-A":           round(b["annual_pct"] - a["annual_pct"], 2),
        })

    df_out = pd.DataFrame(rows).sort_values("B  annual%", ascending=False)

    print("\n" + "="*105)
    print("СРАВНЕНИЕ A vs B (Б = спот лонг + шорт перп по MA200, со стейкингом)")
    print("entry=20%  exit=-5%  min_hold=72ч")
    print("="*105)
    print(df_out.to_string(index=False))

    print("\n" + "="*105)
    print("ИТОГО (среднее по монетам)")
    print("="*105)
    print(f"  buy&hold:   {df_out['buy&hold'].mean():>6.2f}%")
    print(f"  Эксп А:     {df_out['A  annual%'].mean():>6.2f}%  (в позиции {df_out['A  active%'].mean():.1f}%  сделок {df_out['A  trades'].mean():.1f})")
    print(f"  Эксп Б:     {df_out['B  annual%'].mean():>6.2f}%  (в позиции {df_out['B  active%'].mean():.1f}%  сделок {df_out['B  trades'].mean():.1f})")

    out = Path(__file__).parent / "compare_ab_results.csv"
    df_out.to_csv(out, index=False)
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    main()
