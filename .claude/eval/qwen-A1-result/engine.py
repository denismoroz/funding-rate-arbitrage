"""
Shared simulation engine for funding-rate strategies.

Модель:
    - Стартовый капитал TOTAL_CAPITAL = 2 x POSITION_SIZE
    - Spot держится в монетах (units_spot), стейкинг компаундится в монетах
    - Perp short фиксируется в монетах (short_size) на цене entry_price
    - Funding = short_size x P_now x rate (по текущей notional)
    - Equity = cash + units_spot x P + short_size x (entry_price - P)
    - PnL[i] = Equity[i] - Equity[i-1]

Стратегии:
  A_cycle        - обе ноги открываются/закрываются вместе. 4 комиссии за цикл.
  A_spot_keep    - спот куплен один раз и удерживается. Перп динамически.
                  Между циклами - спот риск + стейкинг.
  B              - то же что A_spot_keep, но вход в перп фильтруется regime_filter.
  B_hedge        - стейкинг + хедж просадок. Спот держим всегда (стинг капает),
                  перп шортим по hedge_signal[i] (без funding-условия).
                  Funding при хедже - побочный доход (может быть и отриц).
"""

import numpy as np
import pandas as pd
from pathlib import Path

DATA_DIR         = Path(__file__).parent / "data"
POSITION_SIZE    = 1000        # USDC notional on one leg
TOTAL_CAPITAL    = POSITION_SIZE * 2
PERP_TAKER       = 0.00035     # 0.035% Hyperliquid perp
SPOT_TAKER       = 0.00070     # 0.07%  Hyperliquid spot
HOURS_PER_YEAR   = 8760

ENTRY_THRESHOLD = 0.20
EXIT_THRESHOLD   = -0.05
MIN_HOLD_HOURS   = 72

STAKING_YIELD = {
     "ETH": 0.035, "SOL": 0.085, "BTC": 0.0,    "ARB": 0.0,
     "OP":   0.0,    "AVAX":0.065, "MATIC":0.04, "DOGE":0.0,
     "LINK":0.0,    "UNI": 0.0,    "AAVE": 0.0,   "WIF": 0.0,
     "TIA": 0.14,   "INJ": 0.18,
}

COINS = ["BTC","ETH","SOL","ARB","OP","AVAX","MATIC",
          "DOGE","LINK","UNI","AAVE","WIF","TIA","INJ"]

# Maintenance margin ratios (Hyperliquid approximations)
DEFAULT_MAINT_RATIO = {
     "BTC":    0.01,
     "ETH":    0.01,
     "SOL":    0.025,
     "AVAX": 0.025,
     "LINK": 0.025,
     "AAVE": 0.05,
     "DOGE": 0.05,
}


def load_data(coin: str, with_ohlcv: bool = True) -> pd.DataFrame:
     """
     Load funding + OHLCV for a coin.
     Cut off first week (funding was every 8h on Hyperliquid).
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
        df["ma200"]   = df["close"].rolling(200, min_periods=200).mean()
        df["mom_72h"]   = df["close"].pct_change(72)
        df["mom_168h"] = df["close"].pct_change(168)

    # Cut off first week
    df = df[df.index >= pd.Timestamp("2023-06-08", tz="UTC")]
    return df


def smooth_funding(rates: np.ndarray, window: int) -> np.ndarray:
     """Rolling average funding rate for entry signal."""
     if window <= 1:
         return rates
     s = pd.Series(rates).rolling(window, min_periods=1).mean()
     return s.values


def simulate(
    df: pd.DataFrame,
    staking_yield: float = 0.0,
    strategy: str = "A_cycle",
    regime_filter=None,            # callable(i, close, ma200, mom72, mom168) -> bool
    entry_threshold: float = ENTRY_THRESHOLD,
    exit_threshold:  float = EXIT_THRESHOLD,
    min_hold:        int    = MIN_HOLD_HOURS,
    cooldown_hours:  int    = 0,
    signal_window:   int    = 1,
    momentum_window: int    = 0,    # hours; enter only if funding >= avg over window
    track_capital:   bool   = False,
    hedge_signal:    np.ndarray = None,    # bool[n], only for strategy="B_hedge"
       # --- Margin model (keyword-only; backwards-compat when per_coin_leverage=None) ---
    coin: "str | None" = None,
    per_coin_leverage: "dict[str, float] | None" = None,
    per_coin_maint_ratio: "dict[str, float] | None" = None,
    margin_buffer_x: float = 3.0,
    top_up_trigger: float = 2.0,
    healthy_ratio: float = 3.0,
):
     """
     Return (pnl_arr, info_dict).
       pnl_arr - hourly dEquity (length = len(df))
       info_dict - trades, hours_in_position, capital_arr (if track_capital)

     Margin model activates when per_coin_leverage is not None.
     In that case separate spot_cash / perp_cash wallets are used
     with margin reservation, top-up logic, and forced-close on insolvency.
     Without margin model (per_coin_leverage=None) behavior is byte-identical to old code.
      """
     # Validate margin model params
    margin_active = per_coin_leverage is not None
    if margin_active:
        if coin is None:
            raise ValueError("coin must be specified when per_coin_leverage is provided")
        lev = per_coin_leverage[coin]
        maint_ratio_map = per_coin_maint_ratio if per_coin_maint_ratio is not None else DEFAULT_MAINT_RATIO
        mr = maint_ratio_map[coin]

     close = df["close"].values
    rates = df["fundingRate"].values
    ma200 = df["ma200"].values   if "ma200"   in df.columns else None
    mom72 = df["mom_72h"].values if "mom_72h" in df.columns else None
    m168   = df["mom_168h"].values if "mom_168h" in df.columns else None
    stk_ph = staking_yield / HOURS_PER_YEAR

     # entry signal (possibly smoothed)
    signal = smooth_funding(rates, signal_window) * HOURS_PER_YEAR

     # funding momentum
    if momentum_window > 0:
        baseline = pd.Series(rates).rolling(momentum_window, min_periods=1).mean().shift(1).fillna(0).values
        momentum_ok_arr = rates >= baseline
    else:
        momentum_ok_arr = np.ones(len(rates), dtype=bool)

    n = len(df)
    pnl_arr = np.zeros(n)
    capital_arr = np.zeros(n) if track_capital else None

     # Margin model counters
    if margin_active:
        n_liquidations            = 0
        n_top_ups                 = 0
        n_forced_closes           = 0
        n_skipped_opens_capital   = 0
        min_margin_ratio          = float("inf")
        peak_committed_capital    = 0.0
        spot_cash      = TOTAL_CAPITAL
        perp_cash      = 0.0
        cash           = 0.0     # sentinel never read in margin path
    else:
        cash           = TOTAL_CAPITAL

    units_spot    = 0.0
    short_size    = 0.0
    entry_price = 0.0
    hours_since = 0
    cooldown      = 0
    in_position = False
    trades       = 0
    hours_in     = 0

     # decompose returns
    funding_total       = 0.0    # cum. funding
    short_realized      = 0.0    # cum. realized P&L
    perp_fees_total     = 0.0
    spot_fees_total     = 0.0
    units_initial       = 0.0    # coin qty at start
    first_price         = float(close[0])

     # start: spot_keep / B / B_hedge buy spot immediately
    if strategy in ("A_spot_keep", "B", "B_hedge"):
        P0 = close[0]
        units_spot = POSITION_SIZE / P0
        units_initial = units_spot
        if margin_active:
            spot_cash -= POSITION_SIZE
            spot_cash -= POSITION_SIZE * SPOT_TAKER
        else:
            cash -= POSITION_SIZE
            cash -= POSITION_SIZE * SPOT_TAKER
        spot_fees_total += POSITION_SIZE * SPOT_TAKER

    equity_prev = TOTAL_CAPITAL

    for i in range(n):
        P = close[i]
        rate = rates[i]
        annual_signal = signal[i]
        annual_rate    = rate * HOURS_PER_YEAR

        if cooldown > 0:
            cooldown -= 1

         # 1) staking
         if units_spot > 0:
             units_spot *= (1 + stk_ph)

         # 2) funding (at current notional)
         if in_position:
             f = short_size * P * rate
             funding_total += f
             hours_in += 1
             if margin_active:
                 perp_cash += f

                  # margin checks
                 notional = short_size * P
                 maintenance = notional * mr
                 unrealized_perp_pnl = short_size * (entry_price - P)
                 perp_equity = perp_cash + unrealized_perp_pnl

                 if maintenance > 0:
                     margin_ratio = perp_equity / maintenance
                 else:
                     margin_ratio = float("inf")

                  # track min margin ratio
                 if not np.isfinite(min_margin_ratio) or margin_ratio < min_margin_ratio:
                     min_margin_ratio = margin_ratio

                  # liquidation check
                 if perp_equity <= maintenance:
                      # lose all margin
                     realized = -perp_cash
                     short_realized += realized
                     perp_cash = 0.0
                     short_size = 0.0
                     entry_price = 0.0
                     in_position = False
                     n_liquidations += 1
                      # unwind spot leg for A_cycle on liquidation
                     if strategy == "A_cycle":
                         spot_cash += units_spot * P
                         sfee = units_spot * P * SPOT_TAKER
                         spot_cash -= sfee
                         spot_fees_total += sfee
                         units_spot = 0.0

                 elif margin_ratio < top_up_trigger:
                      # attempt top-up
                     target_perp = healthy_ratio * maintenance
                     top_up = target_perp - perp_equity
                     if spot_cash >= top_up:
                         spot_cash -= top_up
                         perp_cash += top_up
                         n_top_ups += 1
                     else:
                          # partial top-up
                         avail = max(0.0, spot_cash)
                         spot_cash -= avail
                         perp_cash += avail
                          # re-compute margin ratio after partial top-up
                         perp_equity2 = perp_cash + unrealized_perp_pnl
                         if maintenance > 0:
                             margin_ratio2 = perp_equity2 / maintenance
                         else:
                             margin_ratio2 = float("inf")
                         if margin_ratio2 < 1.5:
                              # forced close
                             realized = short_size * (entry_price - P)
                             perp_cash += realized - short_size * P * PERP_TAKER
                             short_realized += realized - short_size * P * PERP_TAKER
                             spot_cash += perp_cash
                             perp_cash = 0.0
                             short_size = 0.0
                             entry_price = 0.0
                             in_position = False
                             n_forced_closes += 1
                              # unwind spot leg for A_cycle
                             if strategy == "A_cycle":
                                 spot_cash += units_spot * P
                                 sfee = units_spot * P * SPOT_TAKER
                                 spot_cash -= sfee
                                 spot_fees_total += sfee
                                 units_spot = 0.0
             else:
                 cash += f

         # 3) entry / exit
         if strategy == "B_hedge":
             want_hedge = bool(hedge_signal[i]) if hedge_signal is not None else False
             if not in_position and want_hedge:
                 perp_fee = POSITION_SIZE * PERP_TAKER
                 if margin_active:
                     required_margin = POSITION_SIZE / lev * margin_buffer_x
                     if spot_cash < required_margin + perp_fee:
                         n_skipped_opens_capital += 1
                     else:
                         spot_cash -= required_margin
                         perp_cash += required_margin
                         perp_cash -= perp_fee
                         perp_fees_total += perp_fee
                         short_size    = POSITION_SIZE / P
                         entry_price = P
                         in_position = True
                         trades += 1
                         hours_since = 0
                         committed = POSITION_SIZE + required_margin
                         if committed > peak_committed_capital:
                             peak_committed_capital = committed
                 else:
                     short_size    = POSITION_SIZE / P
                     entry_price = P
                     cash -= perp_fee
                     perp_fees_total += perp_fee
                     in_position = True
                     trades += 1
                     hours_since = 0
             elif in_position:
                 hours_since += 1
                 if not want_hedge:
                     realized = short_size * (entry_price - P)
                     short_realized += realized
                     fee = short_size * P * PERP_TAKER
                     perp_fees_total += fee
                     if margin_active:
                         perp_cash += realized
                         perp_cash -= fee
                         spot_cash += perp_cash
                         perp_cash = 0.0
                     else:
                         cash += realized
                         cash -= fee
                     short_size = 0.0
                     entry_price = 0.0
                     in_position = False
         elif not in_position:
             if cooldown == 0 and annual_signal > entry_threshold and momentum_ok_arr[i]:
                 regime_ok = True
                 if strategy == "B" and regime_filter is not None:
                     regime_ok = regime_filter(i, close, ma200, mom72, m168)
                 if regime_ok:
                     perp_fee = POSITION_SIZE * PERP_TAKER
                     if margin_active:
                         required_margin = POSITION_SIZE / lev * margin_buffer_x
                         if spot_cash < required_margin + perp_fee:
                             n_skipped_opens_capital += 1
                         else:
                             if strategy == "A_cycle":
                                 units_spot = POSITION_SIZE / P
                                 spot_cash -= POSITION_SIZE
                                 sfee = POSITION_SIZE * SPOT_TAKER
                                 spot_cash -= sfee
                                 spot_fees_total += sfee
                             spot_cash -= required_margin
                             perp_cash += required_margin
                             perp_cash -= perp_fee
                             perp_fees_total += perp_fee
                             short_size    = POSITION_SIZE / P
                             entry_price = P
                             in_position = True
                             trades += 1
                             hours_since = 0
                             committed = POSITION_SIZE + required_margin
                             if committed > peak_committed_capital:
                                 peak_committed_capital = committed
                     else:
                         if strategy == "A_cycle":
                             units_spot = POSITION_SIZE / P
                             cash -= POSITION_SIZE
                             cash -= POSITION_SIZE * SPOT_TAKER
                             spot_fees_total += POSITION_SIZE * SPOT_TAKER
                         short_size    = POSITION_SIZE / P
                         entry_price = P
                         cash -= perp_fee
                         perp_fees_total += perp_fee
                         in_position = True
                         trades += 1
                         hours_since = 0
         else:
             hours_since += 1
             if hours_since >= min_hold and annual_rate < exit_threshold:
                  # close short
                 realized = short_size * (entry_price - P)
                 short_realized += realized
                 fee = short_size * P * PERP_TAKER
                 perp_fees_total += fee
                 if margin_active:
                     perp_cash += realized
                     perp_cash -= fee
                     spot_cash += perp_cash
                     perp_cash = 0.0
                 else:
                     cash += realized
                     cash -= fee
                 if strategy == "A_cycle":
                     if margin_active:
                         spot_cash += units_spot * P
                         sfee = units_spot * P * SPOT_TAKER
                         spot_cash -= sfee
                         spot_fees_total += sfee
                     else:
                         cash += units_spot * P
                         sfee = units_spot * P * SPOT_TAKER
                         cash -= sfee
                         spot_fees_total += sfee
                     units_spot = 0.0
                 short_size = 0.0
                 entry_price = 0.0
                 in_position = False
                 cooldown = cooldown_hours

          # MTM equity
         short_pnl = short_size * (entry_price - P) if in_position else 0.0
         if margin_active:
             equity_now = spot_cash + perp_cash + units_spot * P + short_pnl
         else:
             equity_now = cash + units_spot * P + short_pnl
         pnl_arr[i] = equity_now - equity_prev
         equity_prev = equity_now

          # capital in work at this hour
         if track_capital:
             if strategy == "A_cycle":
                 capital_arr[i] = (POSITION_SIZE + POSITION_SIZE) if in_position else 0.0
             elif strategy in ("A_spot_keep", "B"):
                 capital_arr[i] = POSITION_SIZE + (POSITION_SIZE if in_position else 0.0)

      # final: close everything
     P_final = close[-1]
     extra = 0.0
     final_units_at_close = units_spot
     if in_position:
         realized = short_size * (entry_price - P_final)
         short_realized += realized
         fee = short_size * P_final * PERP_TAKER
         perp_fees_total += fee
         extra -= fee
         if margin_active:
             perp_cash += realized
             perp_cash -= fee
             spot_cash += perp_cash
             perp_cash = 0.0
         else:
             cash += realized
             cash -= fee
         short_size = 0.0
     if units_spot > 0:
         sfee = units_spot * P_final * SPOT_TAKER
         spot_fees_total += sfee
         extra -= sfee
         if margin_active:
             spot_cash -= sfee
         else:
             cash -= sfee
         units_spot = 0.0
     pnl_arr[-1] += extra

      # spot leg breakdown
     spot_price_pnl     = units_initial * (P_final - first_price)
     spot_staking_pnl   = (final_units_at_close - units_initial) * P_final   # extra coins x exit price

     info = {
          "trades":              trades,
          "hours_in_position":   hours_in,
          "funding_total":       round(funding_total, 2),
          "short_realized_pnl":  round(short_realized, 2),
          "perp_fees_total":     round(perp_fees_total, 2),
          "spot_fees_total":     round(spot_fees_total, 2),
          "spot_price_pnl":      round(spot_price_pnl, 2),
          "spot_staking_pnl":    round(spot_staking_pnl, 2),
       }
     if track_capital:
         info["capital_arr"] = capital_arr
     if margin_active:
         info["n_liquidations"]              = n_liquidations
         info["n_top_ups"]                   = n_top_ups
         info["n_forced_closes"]             = n_forced_closes
         info["n_skipped_opens_capital"]     = n_skipped_opens_capital
         inf_val = float("inf")
         info["min_margin_ratio"]            = min_margin_ratio if min_margin_ratio < inf_val else float("nan")
         info["peak_committed_capital"]      = peak_committed_capital
         info["final_spot_cash"]             = round(spot_cash, 4)
         info["final_perp_cash"]             = round(perp_cash, 4)
     return pnl_arr, info


def buy_and_hold(df: pd.DataFrame, staking_yield: float = 0.0) -> np.ndarray:
     """
     Pure buy & hold model: buy spot at TOTAL_CAPITAL, hold with staking.
     Returns pnl_arr.
      """
     close = df["close"].values
     stk_ph = staking_yield / HOURS_PER_YEAR
     n = len(df)
     pnl_arr = np.zeros(n)

     P0 = close[0]
     units_spot = TOTAL_CAPITAL / P0
     cash = -TOTAL_CAPITAL * SPOT_TAKER    # initial commission

     equity_prev = TOTAL_CAPITAL    # baseline

     for i in range(n):
         units_spot *= (1 + stk_ph)
         equity_now = cash + units_spot * close[i]
         pnl_arr[i] = equity_now - equity_prev
         equity_prev = equity_now

      # final sale
     pnl_arr[-1] -= units_spot * close[-1] * SPOT_TAKER

     return pnl_arr


def compute_metrics(pnl_arr: np.ndarray) -> dict:
     """Standard risk metrics from hourly P&L."""
     n = len(pnl_arr)
     equity = np.cumsum(pnl_arr)
     total_return = equity[-1] / TOTAL_CAPITAL
     annualized = total_return / (n / HOURS_PER_YEAR) * 100

     hr = pnl_arr / TOTAL_CAPITAL
     vol_annual = hr.std() * np.sqrt(HOURS_PER_YEAR) * 100

     mean_hr = hr.mean()
     std_hr   = hr.std()
     sharpe   = (mean_hr / std_hr * np.sqrt(HOURS_PER_YEAR)) if std_hr > 0 else 0

     downside = hr[hr < 0]
     if len(downside) > 0 and downside.std() > 0:
         sortino = mean_hr / downside.std() * np.sqrt(HOURS_PER_YEAR)
     else:
         sortino = float("nan") if mean_hr > 0 else 0

     peak = np.maximum.accumulate(equity)
     dd     = (equity - peak) / (TOTAL_CAPITAL + peak)
     max_dd = abs(dd.min()) * 100

     calmar   = (annualized / max_dd) if max_dd > 0 else 0
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


# -- Regime filters for Strategy B --

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
    nm     = (mom72 is not None) and (not np.isnan(mom72[i])) and mom72[i] < 0
    return below or nm

def regime_high_funding(i, close, ma200, mom72, mom168, threshold=0.50):
     # this function needs rate separately
    raise NotImplementedError("high_funding signal implemented via entry_threshold, not regime")
