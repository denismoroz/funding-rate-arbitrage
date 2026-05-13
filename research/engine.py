"""
Shared simulation engine for funding-rate strategies.

Модель:
  - Стартовый капитал TOTAL_CAPITAL = 2 × POSITION_SIZE
  - Spot держится в монетах (units_spot), стейкинг компаундится в монетах
  - Perp short фиксируется в монетах (short_size) на цене entry_price
  - Funding = short_size × P_now × rate (по текущей notional)
  - Equity = cash + units_spot × P + short_size × (entry_price − P)
  - PnL[i] = Equity[i] − Equity[i−1]

Стратегии:
  A_cycle      — обе ноги открываются/закрываются вместе. 4 комиссии за цикл.
  A_spot_keep  — спот куплен один раз и удерживается. Перп динамически.
                 Между циклами — спот риск + стейкинг.
  B            — то же что A_spot_keep, но вход в перп фильтруется regime_filter.
"""

import numpy as np
import pandas as pd
from pathlib import Path

DATA_DIR        = Path(__file__).parent / "data"
POSITION_SIZE   = 1000      # USDC notional на одну ногу
TOTAL_CAPITAL   = POSITION_SIZE * 2
PERP_TAKER      = 0.00035   # 0.035% Hyperliquid perp
SPOT_TAKER      = 0.00070   # 0.07%  Hyperliquid spot
HOURS_PER_YEAR  = 8760

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


def load_data(coin: str, with_ohlcv: bool = True) -> pd.DataFrame:
    """
    Загружает funding + OHLCV для монеты.
    Отрезает первую неделю (там funding каждые 8ч на Hyperliquid).
    """
    funding = pd.read_csv(DATA_DIR / f"{coin}.csv")
    funding["time"] = pd.to_datetime(funding["time"], format="ISO8601", utc=True).dt.floor("h")
    funding = funding.set_index("time")[["fundingRate"]].sort_index()

    if not with_ohlcv:
        df = funding
    else:
        ohlcv = pd.read_csv(DATA_DIR / f"{coin}_1h.csv")
        ohlcv["time"] = pd.to_datetime(ohlcv["time"], format="ISO8601", utc=True).dt.floor("h")
        ohlcv = ohlcv.set_index("time")[["close"]].sort_index()
        df = funding.join(ohlcv, how="inner")
        df["price_return"] = df["close"].pct_change().fillna(0)
        df["ma200"]  = df["close"].rolling(200, min_periods=200).mean()
        df["mom_72h"]  = df["close"].pct_change(72)
        df["mom_168h"] = df["close"].pct_change(168)

    # Отрезаем первую неделю — funding был 8h, а модель предполагает 1h
    df = df[df.index >= pd.Timestamp("2023-06-08", tz="UTC")]
    return df


def smooth_funding(rates: np.ndarray, window: int) -> np.ndarray:
    """Скользящее среднее funding rate для сигнала входа."""
    if window <= 1:
        return rates
    s = pd.Series(rates).rolling(window, min_periods=1).mean()
    return s.values


def simulate(
    df: pd.DataFrame,
    staking_yield: float = 0.0,
    strategy: str = "A_cycle",
    regime_filter=None,           # callable(i, close, ma200, mom72, mom168) -> bool
    entry_threshold: float = ENTRY_THRESHOLD,
    exit_threshold:  float = EXIT_THRESHOLD,
    min_hold:        int   = MIN_HOLD_HOURS,
    cooldown_hours:  int   = 0,
    signal_window:   int   = 1,
    momentum_window: int   = 0,   # часы; вход только если funding >= среднего за окно
    track_capital:   bool  = False,
):
    """
    Возвращает (pnl_arr, info_dict).
      pnl_arr — почасовые ΔEquity (длина = len(df))
      info_dict — trades, hours_in_position, capital_arr (если track_capital)
    """
    close = df["close"].values
    rates = df["fundingRate"].values
    ma200 = df["ma200"].values   if "ma200"   in df.columns else None
    mom72 = df["mom_72h"].values if "mom_72h" in df.columns else None
    m168  = df["mom_168h"].values if "mom_168h" in df.columns else None
    stk_ph = staking_yield / HOURS_PER_YEAR

    # сигнал входа (возможно сглаженный)
    signal = smooth_funding(rates, signal_window) * HOURS_PER_YEAR

    # funding momentum: текущая ставка не ниже среднего за прошлое окно
    if momentum_window > 0:
        baseline = pd.Series(rates).rolling(momentum_window, min_periods=1).mean().shift(1).fillna(0).values
        momentum_ok_arr = rates >= baseline
    else:
        momentum_ok_arr = np.ones(len(rates), dtype=bool)

    n = len(df)
    pnl_arr = np.zeros(n)
    capital_arr = np.zeros(n) if track_capital else None

    cash        = TOTAL_CAPITAL
    units_spot  = 0.0
    short_size  = 0.0
    entry_price = 0.0
    hours_since = 0
    cooldown    = 0
    in_position = False
    trades      = 0
    hours_in    = 0

    # Старт: spot_keep / B покупают спот сразу
    if strategy in ("A_spot_keep", "B"):
        P0 = close[0]
        units_spot = POSITION_SIZE / P0
        cash -= POSITION_SIZE
        cash -= POSITION_SIZE * SPOT_TAKER

    equity_prev = TOTAL_CAPITAL

    for i in range(n):
        P = close[i]
        rate = rates[i]
        annual_signal = signal[i]
        annual_rate   = rate * HOURS_PER_YEAR

        if cooldown > 0:
            cooldown -= 1

        # 1) Стейкинг
        if units_spot > 0:
            units_spot *= (1 + stk_ph)

        # 2) Funding (по текущей notional)
        if in_position:
            cash += short_size * P * rate
            hours_in += 1

        # 3) Entry / exit
        if not in_position:
            if cooldown == 0 and annual_signal > entry_threshold and momentum_ok_arr[i]:
                regime_ok = True
                if strategy == "B" and regime_filter is not None:
                    regime_ok = regime_filter(i, close, ma200, mom72, m168)
                if regime_ok:
                    if strategy == "A_cycle":
                        units_spot = POSITION_SIZE / P
                        cash -= POSITION_SIZE
                        cash -= POSITION_SIZE * SPOT_TAKER
                    short_size  = POSITION_SIZE / P
                    entry_price = P
                    cash -= POSITION_SIZE * PERP_TAKER
                    in_position = True
                    trades += 1
                    hours_since = 0
        else:
            hours_since += 1
            if hours_since >= min_hold and annual_rate < exit_threshold:
                # close short
                cash += short_size * (entry_price - P)
                cash -= short_size * P * PERP_TAKER
                if strategy == "A_cycle":
                    cash += units_spot * P
                    cash -= units_spot * P * SPOT_TAKER
                    units_spot = 0.0
                short_size = 0.0
                entry_price = 0.0
                in_position = False
                cooldown = cooldown_hours

        # MTM equity
        short_pnl = short_size * (entry_price - P) if in_position else 0.0
        equity_now = cash + units_spot * P + short_pnl
        pnl_arr[i] = equity_now - equity_prev
        equity_prev = equity_now

        # Capital в работе на этом часу
        if track_capital:
            if strategy == "A_cycle":
                capital_arr[i] = (POSITION_SIZE + POSITION_SIZE) if in_position else 0.0
            elif strategy in ("A_spot_keep", "B"):
                capital_arr[i] = POSITION_SIZE + (POSITION_SIZE if in_position else 0.0)

    # Финал: закрыть всё
    P_final = close[-1]
    extra = 0.0
    if in_position:
        cash += short_size * (entry_price - P_final)
        cash -= short_size * P_final * PERP_TAKER
        extra -= short_size * P_final * PERP_TAKER
        short_size = 0.0
    if units_spot > 0:
        cash -= units_spot * P_final * SPOT_TAKER
        extra -= units_spot * P_final * SPOT_TAKER
        units_spot = 0.0
    pnl_arr[-1] += extra

    info = {"trades": trades, "hours_in_position": hours_in}
    if track_capital:
        info["capital_arr"] = capital_arr
    return pnl_arr, info


def buy_and_hold(df: pd.DataFrame, staking_yield: float = 0.0) -> np.ndarray:
    """
    Чистая buy & hold модель: купить спот на TOTAL_CAPITAL, держать со стейкингом.
    Возвращает pnl_arr.
    """
    close = df["close"].values
    stk_ph = staking_yield / HOURS_PER_YEAR
    n = len(df)
    pnl_arr = np.zeros(n)

    P0 = close[0]
    units_spot = TOTAL_CAPITAL / P0
    cash = -TOTAL_CAPITAL * SPOT_TAKER   # начальная комиссия

    equity_prev = TOTAL_CAPITAL  # baseline

    for i in range(n):
        units_spot *= (1 + stk_ph)
        equity_now = cash + units_spot * close[i]
        pnl_arr[i] = equity_now - equity_prev
        equity_prev = equity_now

    # финальная продажа
    pnl_arr[-1] -= units_spot * close[-1] * SPOT_TAKER

    return pnl_arr


def compute_metrics(pnl_arr: np.ndarray) -> dict:
    """Стандартные метрики риска по почасовому P&L."""
    n = len(pnl_arr)
    equity = np.cumsum(pnl_arr)
    total_return = equity[-1] / TOTAL_CAPITAL
    annualized = total_return / (n / HOURS_PER_YEAR) * 100

    hr = pnl_arr / TOTAL_CAPITAL
    vol_annual = hr.std() * np.sqrt(HOURS_PER_YEAR) * 100

    mean_hr = hr.mean()
    std_hr  = hr.std()
    sharpe  = (mean_hr / std_hr * np.sqrt(HOURS_PER_YEAR)) if std_hr > 0 else 0

    downside = hr[hr < 0]
    if len(downside) > 0 and downside.std() > 0:
        sortino = mean_hr / downside.std() * np.sqrt(HOURS_PER_YEAR)
    else:
        sortino = float("nan") if mean_hr > 0 else 0

    peak = np.maximum.accumulate(equity)
    dd   = (equity - peak) / (TOTAL_CAPITAL + peak)
    max_dd = abs(dd.min()) * 100

    calmar  = (annualized / max_dd) if max_dd > 0 else 0
    win_rate = (pnl_arr > 0).mean() * 100
    kelly_half = max(0, sharpe / (vol_annual / 100) * 0.5) if vol_annual > 0 else 0

    return {
        "annual_pct": round(annualized, 2),
        "vol_pct":    round(vol_annual, 2),
        "sharpe":     round(sharpe, 2),
        "sortino":    round(sortino, 2) if np.isfinite(sortino) else float("nan"),
        "max_dd_pct": round(max_dd, 2),
        "calmar":     round(calmar, 2),
        "win_rate":   round(win_rate, 1),
        "kelly_half": round(kelly_half, 2),
    }


# ── Regime filters для Стратегии Б ────────────────────────────────────────────

def regime_always(i, close, ma200, mom72, mom168):
    return True

def regime_below_ma(i, close, ma200, mom72, mom168):
    return (ma200 is not None) and (not np.isnan(ma200[i])) and close[i] < ma200[i]

def regime_neg_mom3d(i, close, ma200, mom72, mom168):
    return (mom72 is not None) and (not np.isnan(mom72[i])) and mom72[i] < 0

def regime_neg_mom7d(i, close, ma200, mom72, mom168):
    return (mom168 is not None) and (not np.isnan(mom168[i])) and mom168[i] < 0

def regime_combo(i, close, ma200, mom72, mom168):
    below = (ma200 is not None) and (not np.isnan(ma200[i])) and close[i] < ma200[i]
    nm    = (mom72 is not None) and (not np.isnan(mom72[i])) and mom72[i] < 0
    return below or nm

def regime_high_funding(i, close, ma200, mom72, mom168, threshold=0.50):
    # эта функция требует rate отдельно — не реализуем через regime, оставляем для другого места
    raise NotImplementedError("high_funding signal реализован через entry_threshold, не regime")
