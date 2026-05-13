"""
Риск-анализ стратегий A и B по монетам.

Метрики:
  - Annualized return
  - Max Drawdown (максимальная просадка equity curve)
  - Sharpe Ratio (годовой, rf=0)
  - Sortino Ratio (только downside volatility)
  - Calmar Ratio (return / max_drawdown)
  - Win rate (% часов с положительным P&L)
  - Volatility (годовая)

На основе метрик — рекомендации по размеру позиции (Kelly criterion).
"""

import pandas as pd
import numpy as np
from pathlib import Path

DATA_DIR       = Path(__file__).parent / "data"
POSITION_SIZE  = 1000  # USDC notional per leg
TOTAL_CAPITAL  = POSITION_SIZE * 2  # 1000 spot + 1000 perp margin
TAKER_FEE      = 0.00035
HOURS_PER_YEAR = 8760
ENTRY_THRESHOLD = 0.20
EXIT_THRESHOLD  = -0.05
MIN_HOLD_HOURS  = 72

STAKING_YIELD = {
    "ETH": 0.035, "SOL": 0.085, "BTC": 0.0,   "ARB": 0.0,
    "OP":  0.0,   "AVAX":0.065, "MATIC":0.04, "DOGE":0.0,
    "LINK":0.0,   "UNI": 0.0,   "AAVE": 0.0,  "WIF": 0.0,
    "TIA": 0.14,  "INJ": 0.18,
}

COINS = ["BTC","ETH","SOL","ARB","OP","AVAX","MATIC",
         "DOGE","LINK","UNI","AAVE","WIF","TIA","INJ"]


def load_data(coin):
    funding = pd.read_csv(DATA_DIR / f"{coin}.csv")
    funding["time"] = pd.to_datetime(funding["time"], format="ISO8601", utc=True).dt.floor("h")
    funding = funding.set_index("time")[["fundingRate"]].sort_index()
    ohlcv = pd.read_csv(DATA_DIR / f"{coin}_1h.csv")
    ohlcv["time"] = pd.to_datetime(ohlcv["time"], format="ISO8601", utc=True).dt.floor("h")
    ohlcv = ohlcv.set_index("time")[["close"]].sort_index()
    df = funding.join(ohlcv, how="inner")
    df["price_return"] = df["close"].pct_change().fillna(0)
    df["ma200"] = df["close"].rolling(200, min_periods=1).mean()
    return df


def compute_metrics(hourly_pnl: np.ndarray, total_hours: int) -> dict:
    """Считает метрики по массиву почасового P&L."""
    equity = np.cumsum(hourly_pnl)  # equity curve относительно стартового POSITION_SIZE

    # Return
    total_return = equity[-1] / TOTAL_CAPITAL
    annualized   = total_return / (total_hours / HOURS_PER_YEAR) * 100

    # Hourly returns как % от общего капитала
    hr = hourly_pnl / TOTAL_CAPITAL

    # Volatility (годовая)
    vol_annual = hr.std() * np.sqrt(HOURS_PER_YEAR) * 100

    # Sharpe (rf=0)
    mean_hr   = hr.mean()
    std_hr    = hr.std()
    sharpe    = (mean_hr / std_hr * np.sqrt(HOURS_PER_YEAR)) if std_hr > 0 else 0

    # Sortino
    downside  = hr[hr < 0]
    std_down  = downside.std() if len(downside) > 0 else 1e-9
    sortino   = (mean_hr / std_down * np.sqrt(HOURS_PER_YEAR)) if std_down > 0 else 0

    # Max Drawdown
    peak      = np.maximum.accumulate(equity)
    dd        = (equity - peak) / (TOTAL_CAPITAL + peak)  # как доля от текущего peak+initial
    max_dd    = abs(dd.min()) * 100

    # Calmar
    calmar    = (annualized / max_dd) if max_dd > 0 else 0

    # Win rate
    win_rate  = (hourly_pnl > 0).mean() * 100

    # Half Kelly (упрощённый): f = Sharpe / vol * 0.5
    kelly_half = max(0, sharpe / (vol_annual / 100) * 0.5) if vol_annual > 0 else 0

    return {
        "annual_pct":  round(annualized, 2),
        "vol_pct":     round(vol_annual, 2),
        "sharpe":      round(sharpe, 2),
        "sortino":     round(sortino, 2),
        "max_dd_pct":  round(max_dd, 2),
        "calmar":      round(calmar, 2),
        "win_rate":    round(win_rate, 1),
        "kelly_half":  round(kelly_half, 2),
    }


def hourly_pnl_a(df) -> np.ndarray:
    """Эксперимент А: дельта-нейтральный."""
    rates = df["fundingRate"].values
    pnl_arr = np.zeros(len(rates))
    in_pos, hours_since = False, 0

    for i, rate in enumerate(rates):
        ar = rate * HOURS_PER_YEAR
        if not in_pos:
            if ar > ENTRY_THRESHOLD:
                in_pos = True; hours_since = 0
                pnl_arr[i] -= TAKER_FEE * POSITION_SIZE
        else:
            pnl_arr[i] += rate * POSITION_SIZE
            hours_since += 1
            if hours_since >= MIN_HOLD_HOURS and ar < EXIT_THRESHOLD:
                in_pos = False
                pnl_arr[i] -= TAKER_FEE * POSITION_SIZE

    if in_pos:
        pnl_arr[-1] -= TAKER_FEE * POSITION_SIZE
    return pnl_arr


def hourly_pnl_b(df, staking) -> np.ndarray:
    """Эксперимент Б: спот лонг + шорт по MA200 + стейкинг."""
    rates     = df["fundingRate"].values
    price_ret = df["price_return"].values
    close     = df["close"].values
    ma200     = df["ma200"].values
    stk_ph    = staking / HOURS_PER_YEAR
    pnl_arr   = np.zeros(len(rates))
    in_pos, hours_since = False, 0

    for i in range(len(rates)):
        rate = rates[i]; ar = rate * HOURS_PER_YEAR
        pnl_arr[i] += POSITION_SIZE * stk_ph

        if not in_pos:
            pnl_arr[i] += POSITION_SIZE * price_ret[i]
            if close[i] < ma200[i] and ar > ENTRY_THRESHOLD:
                in_pos = True; hours_since = 0
                pnl_arr[i] -= TAKER_FEE * POSITION_SIZE
        else:
            pnl_arr[i] += rate * POSITION_SIZE
            hours_since += 1
            if hours_since >= MIN_HOLD_HOURS and ar < EXIT_THRESHOLD:
                in_pos = False
                pnl_arr[i] -= TAKER_FEE * POSITION_SIZE

    if in_pos:
        pnl_arr[-1] -= TAKER_FEE * POSITION_SIZE
    return pnl_arr


def main():
    rows_a, rows_b = [], []

    for coin in COINS:
        if not (DATA_DIR / f"{coin}.csv").exists() or not (DATA_DIR / f"{coin}_1h.csv").exists():
            continue
        df      = load_data(coin)
        staking = STAKING_YIELD.get(coin, 0.0)
        n       = len(df)

        m_a = compute_metrics(hourly_pnl_a(df), n)
        m_b = compute_metrics(hourly_pnl_b(df, staking), n)

        rows_a.append({"coin": coin, **m_a})
        rows_b.append({"coin": coin, **m_b})

    df_a = pd.DataFrame(rows_a).sort_values("calmar", ascending=False)
    df_b = pd.DataFrame(rows_b).sort_values("calmar", ascending=False)

    cols = ["coin","annual_pct","vol_pct","sharpe","sortino","max_dd_pct","calmar","win_rate","kelly_half"]

    print("\n" + "="*100)
    print("ЭКСПЕРИМЕНТ А — риск-метрики (сортировка по Calmar)")
    print("="*100)
    print(df_a[cols].to_string(index=False))

    print("\n" + "="*100)
    print("ЭКСПЕРИМЕНТ Б (MA200 + стейкинг) — риск-метрики (сортировка по Calmar)")
    print("="*100)
    print(df_b[cols].to_string(index=False))

    # Сводная таблица: Sharpe A vs B
    print("\n" + "="*90)
    print("СРАВНЕНИЕ SHARPE и CALMAR: A vs B")
    print("="*90)
    merged = df_a[["coin","annual_pct","sharpe","calmar","max_dd_pct","kelly_half"]].merge(
        df_b[["coin","annual_pct","sharpe","calmar","max_dd_pct","kelly_half"]],
        on="coin", suffixes=("_A","_B")
    ).sort_values("sharpe_B", ascending=False)
    print(merged.to_string(index=False))

    # Kelly-based sizing
    print("\n" + "="*90)
    print("РЕКОМЕНДУЕМЫЙ РАЗМЕР ПОЗИЦИИ (Half Kelly, % от капитала)")
    print("Лучшая стратегия для каждой монеты + ограничение макс 25%")
    print("="*90)
    sizing = []
    for _, ra in df_a.iterrows():
        rb = df_b[df_b["coin"] == ra["coin"]].iloc[0]
        best = "A" if ra["calmar"] >= rb["calmar"] else "B"
        kelly = ra["kelly_half"] if best == "A" else rb["kelly_half"]
        sizing.append({
            "coin":        ra["coin"],
            "best_strat":  best,
            "calmar":      ra["calmar"] if best == "A" else rb["calmar"],
            "sharpe":      ra["sharpe"] if best == "A" else rb["sharpe"],
            "kelly_half%": min(kelly * 100, 25),
        })
    df_sz = pd.DataFrame(sizing).sort_values("kelly_half%", ascending=False)
    print(df_sz.to_string(index=False))

    total_kelly = df_sz["kelly_half%"].sum()
    print(f"\nСумма Kelly весов: {total_kelly:.1f}% (если >100% — нормируем)")
    if total_kelly > 100:
        df_sz["alloc%"] = (df_sz["kelly_half%"] / total_kelly * 100).round(1)
        print("\nНормированное распределение капитала:")
        print(df_sz[["coin","best_strat","alloc%"]].to_string(index=False))

    df_a.to_csv(Path(__file__).parent / "risk_a.csv", index=False)
    df_b.to_csv(Path(__file__).parent / "risk_b.csv", index=False)
    df_sz.to_csv(Path(__file__).parent / "risk_sizing.csv", index=False)


if __name__ == "__main__":
    main()
