"""
Эксперимент Б — постоянный спот лонг + селективный шорт перп с regime filter.

Базовые параметры (лучшие из Эксперимента А): entry=20%, exit=-5%, min_hold=72ч

Режимы хеджирования:
  baseline   — хеджируем всегда когда funding высокий
  signal_1   — MA тренд: хеджируем только когда цена < MA(200ч)
  signal_2   — Momentum: хеджируем только когда цена упала за последние N дней
  signal_3   — Combo: funding высокий И (цена < MA ИЛИ momentum отрицательный)
  signal_4   — Funding как сигнал перегрева: хеджируем когда funding > высокий порог

Стейкинг учитывается во всех вариантах.
"""

import pandas as pd
import numpy as np
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
POSITION_SIZE  = 1000  # USDC notional per leg
TOTAL_CAPITAL  = POSITION_SIZE * 2  # 1000 spot + 1000 perp margin
TAKER_FEE      = 0.00035
HOURS_PER_YEAR = 8760

ENTRY_THRESHOLD = 0.20
EXIT_THRESHOLD  = -0.05
MIN_HOLD_HOURS  = 72

STAKING_YIELD = {
    "ETH":  0.035,
    "SOL":  0.085,
    "BTC":  0.0,
    "ARB":  0.0,
    "OP":   0.0,
    "AVAX": 0.065,
    "MATIC":0.04,
    "DOGE": 0.0,
    "LINK": 0.0,
    "UNI":  0.0,
    "AAVE": 0.0,
    "WIF":  0.0,
    "TIA":  0.14,
    "INJ":  0.18,
}


def load_data(coin: str) -> pd.DataFrame:
    funding = pd.read_csv(DATA_DIR / f"{coin}.csv")
    funding["time"] = pd.to_datetime(funding["time"], format="ISO8601", utc=True).dt.floor("h")
    funding = funding.set_index("time")[["fundingRate"]].sort_index()

    ohlcv = pd.read_csv(DATA_DIR / f"{coin}_1h.csv")
    ohlcv["time"] = pd.to_datetime(ohlcv["time"], format="ISO8601", utc=True).dt.floor("h")
    ohlcv = ohlcv.set_index("time")[["close"]].sort_index()

    df = funding.join(ohlcv, how="inner")
    df["price_return"] = df["close"].pct_change().fillna(0)

    # Precompute signals
    df["ma200"]      = df["close"].rolling(200, min_periods=1).mean()
    df["mom_72h"]    = df["close"].pct_change(72).fillna(0)   # 3-дневный моментум
    df["mom_168h"]   = df["close"].pct_change(168).fillna(0)  # 7-дневный моментум

    return df


def run_strategy(
    df: pd.DataFrame,
    staking_yield: float,
    regime_filter,          # callable(row_idx, df) -> bool: True = разрешено хеджировать
    entry_threshold: float = ENTRY_THRESHOLD,
    exit_threshold:  float = EXIT_THRESHOLD,
    min_hold:        int   = MIN_HOLD_HOURS,
) -> dict:
    rates        = df["fundingRate"].values
    price_ret    = df["price_return"].values
    staking_ph   = staking_yield / HOURS_PER_YEAR

    in_position      = False
    pnl              = 0.0
    trades           = 0
    hours_hedged     = 0
    hours_since_entry = 0

    for i in range(len(rates)):
        rate        = rates[i]
        annual_rate = rate * HOURS_PER_YEAR

        pnl += POSITION_SIZE * staking_ph

        if not in_position:
            pnl += POSITION_SIZE * price_ret[i]
            can_hedge = regime_filter(i, df)
            if can_hedge and annual_rate > entry_threshold:
                in_position = True
                trades += 1
                hours_since_entry = 0
                pnl -= TAKER_FEE * POSITION_SIZE
        else:
            pnl += rate * POSITION_SIZE
            hours_hedged      += 1
            hours_since_entry += 1

            if hours_since_entry >= min_hold and annual_rate < exit_threshold:
                in_position = False
                pnl -= TAKER_FEE * POSITION_SIZE

    if in_position:
        pnl -= TAKER_FEE * POSITION_SIZE
        trades += 1

    total_hours = len(df)
    annualized  = (pnl / TOTAL_CAPITAL) / (total_hours / HOURS_PER_YEAR) * 100

    return {
        "annualized_pct": round(annualized, 2),
        "pct_hedged":     round(hours_hedged / total_hours * 100, 1),
        "trades":         trades,
    }


def run_buy_hold(df: pd.DataFrame, staking_yield: float = 0.0) -> float:
    equity = POSITION_SIZE
    staking_ph = staking_yield / HOURS_PER_YEAR
    for r in df["price_return"].values:
        equity *= (1 + r)
        equity += POSITION_SIZE * staking_ph
    pnl = equity - POSITION_SIZE
    return round((pnl / POSITION_SIZE) / (len(df) / HOURS_PER_YEAR) * 100, 2)


# ── Regime filters ────────────────────────────────────────────────────────────

def always(i, df):
    return True

def signal_1_ma(i, df, window=200):
    """Хеджируем только когда цена ниже MA — рынок в нисходящем тренде."""
    return df["close"].iloc[i] < df["ma200"].iloc[i]

def signal_2_momentum(i, df, lookback="72h"):
    """Хеджируем только когда моментум отрицательный (цена упала за N часов)."""
    col = f"mom_{lookback}"
    return df[col].iloc[i] < 0

def signal_3_combo(i, df):
    """Хеджируем когда цена < MA ИЛИ 3-дн моментум отрицательный."""
    below_ma  = df["close"].iloc[i] < df["ma200"].iloc[i]
    neg_mom   = df["mom_72h"].iloc[i] < 0
    return below_ma or neg_mom

def signal_4_high_funding(i, df, high_threshold=0.50):
    """Хеджируем только при очень высоком funding (>50% годовых) — признак перегрева."""
    return df["fundingRate"].iloc[i] * HOURS_PER_YEAR > high_threshold


# ─────────────────────────────────────────────────────────────────────────────

def main():
    coins = [
        "BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "MATIC",
        "DOGE", "LINK", "UNI", "AAVE", "WIF", "TIA", "INJ",
    ]

    signals = {
        "baseline":  always,
        "s1_ma200":  signal_1_ma,
        "s2_mom3d":  lambda i, df: signal_2_momentum(i, df, "72h"),
        "s2_mom7d":  lambda i, df: signal_2_momentum(i, df, "168h"),
        "s3_combo":  signal_3_combo,
        "s4_hi_fund":signal_4_high_funding,
    }

    all_rows = []

    for coin in coins:
        if not (DATA_DIR / f"{coin}.csv").exists() or not (DATA_DIR / f"{coin}_1h.csv").exists():
            continue

        df      = load_data(coin)
        staking = STAKING_YIELD.get(coin, 0.0)
        bh      = run_buy_hold(df, staking_yield=staking)

        for sig_name, sig_fn in signals.items():
            res = run_strategy(df, staking, sig_fn)
            all_rows.append({
                "coin":    coin,
                "signal":  sig_name,
                "buy_hold_stake": bh,
                **res,
            })

    df_res = pd.DataFrame(all_rows)

    # Среднее по всем монетам для каждого сигнала
    print("\n" + "="*90)
    print("СРЕДНЕЕ ПО ВСЕМ МОНЕТАМ (со стейкингом)")
    print("="*90)
    avg = df_res.groupby("signal").agg(
        buy_hold=("buy_hold_stake", "mean"),
        hedged=("annualized_pct", "mean"),
        pct_hedged=("pct_hedged", "mean"),
        trades=("trades", "mean"),
    ).round(2).sort_values("hedged", ascending=False)
    print(avg.to_string())

    # По каждой монете — все сигналы
    print("\n" + "="*90)
    print("ПО МОНЕТАМ: annualized % (со стейкингом)")
    print("="*90)
    pivot = df_res.pivot(index="coin", columns="signal", values="annualized_pct")
    pivot["buy_hold"] = df_res.groupby("coin")["buy_hold_stake"].first()
    pivot = pivot[["buy_hold", "baseline", "s1_ma200", "s2_mom3d", "s2_mom7d", "s3_combo", "s4_hi_fund"]]
    pivot = pivot.sort_values("baseline", ascending=False)
    print(pivot.to_string())

    out = Path(__file__).parent / "backtest_b_results.csv"
    df_res.to_csv(out, index=False)
    print(f"\nПолные результаты: {out}")


if __name__ == "__main__":
    main()
