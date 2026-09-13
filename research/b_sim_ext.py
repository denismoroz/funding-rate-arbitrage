"""Extended Strategy B simulator (2026-09-13). Line-for-line copy of
backtest_b_constdollar.simulate_constdollar with four OPTIONAL levers, all off by default:

  hedge_ratio   fraction of spot units shorted (1.0 = original full hedge)
  hedge_close   / hedge_rates / beta   hedge with ANOTHER perp (e.g. BTC), sized by beta
  position_size spot allocation out of TOTAL_CAPITAL (1000 = original 50/50);
                the refill reserve generalises to TOTAL_CAPITAL - position_size, which
                equals the original POSITION_SIZE reserve at the default
Defaults reproduce the original bit-exact — asserted in selftest().
"""
import numpy as np
from engine import HOURS_PER_YEAR, TOTAL_CAPITAL, POSITION_SIZE, SPOT_TAKER, PERP_TAKER
from backtest_b_constdollar import REBAL_THRESHOLD, RISK_FREE_APR


def simulate_ext(df, staking, hedge_signal, rebal_threshold=REBAL_THRESHOLD, risk_free_apr=RISK_FREE_APR,
                 refill_confirm=None, signal_lag=0, slippage=0.0,
                 hedge_ratio=1.0, hedge_close=None, hedge_rates=None, beta=None,
                 position_size=POSITION_SIZE):
    close = df["close"].values
    rates = df["fundingRate"].values
    n = len(df)
    stk_ph = staking / HOURS_PER_YEAR
    rf_ph = risk_free_apr / HOURS_PER_YEAR
    refill_pending = False
    perp_cost = PERP_TAKER + slippage
    spot_cost = SPOT_TAKER + slippage
    hc = close if hedge_close is None else np.asarray(hedge_close, dtype=float)
    hr = rates if hedge_rates is None else np.asarray(hedge_rates, dtype=float)
    reserve = TOTAL_CAPITAL - position_size

    def sig_at(arr, idx):
        j = idx - signal_lag
        return bool(arr[j]) if j >= 0 else False

    cash = TOTAL_CAPITAL - position_size
    P0 = float(close[0])
    units_spot = position_size / P0
    cash -= position_size * spot_cost
    short_size = 0.0; entry_price = 0.0; in_pos = False
    trades = 0; rebals = 0; hours_in = 0
    pnl_arr = np.zeros(n); equity_prev = TOTAL_CAPITAL
    funding_total = 0.0; short_realized = 0.0; perp_fees_total = 0.0
    spot_fees_total = position_size * spot_cost

    for i in range(n):
        P = float(close[i]); Ph = float(hc[i]); rate = float(hr[i])
        if units_spot > 0:
            units_spot *= (1 + stk_ph)
        if cash > 0:
            cash *= (1 + rf_ph)
        if in_pos:
            f = short_size * Ph * rate
            cash += f; funding_total += f; hours_in += 1
        want_hedge = sig_at(hedge_signal, i)
        if not in_pos and want_hedge:
            b = 1.0 if beta is None else float(beta[i])
            short_size = hedge_ratio * b * units_spot * P / Ph
            entry_price = Ph
            fee = short_size * Ph * perp_cost
            cash -= fee; perp_fees_total += fee
            in_pos = True; trades += 1
        elif in_pos and not want_hedge:
            realized = short_size * (entry_price - Ph)
            cash += realized; short_realized += realized
            fee = short_size * Ph * perp_cost
            cash -= fee; perp_fees_total += fee
            short_size = 0.0; entry_price = 0.0; in_pos = False
            spot_value = units_spot * P
            if spot_value < position_size:
                if refill_confirm is None:
                    need = position_size - spot_value
                    buy = min(need, max(cash - reserve, 0))
                    if buy > 0:
                        fee = buy * spot_cost
                        cash -= buy + fee; spot_fees_total += fee
                        units_spot += buy / P; rebals += 1
                else:
                    refill_pending = True
        if refill_pending and not in_pos and refill_confirm is not None:
            if sig_at(refill_confirm, i):
                spot_value = units_spot * P
                if spot_value < position_size:
                    need = position_size - spot_value
                    buy = min(need, max(cash - reserve, 0))
                    if buy > 0:
                        fee = buy * spot_cost
                        cash -= buy + fee; spot_fees_total += fee
                        units_spot += buy / P; rebals += 1
                refill_pending = False
        if not in_pos:
            spot_value = units_spot * P
            if spot_value > position_size * (1 + rebal_threshold):
                excess = spot_value - position_size
                fee = excess * spot_cost
                cash += excess - fee; spot_fees_total += fee
                units_spot -= excess / P; rebals += 1
        short_pnl = short_size * (entry_price - Ph) if in_pos else 0.0
        equity_now = cash + units_spot * P + short_pnl
        pnl_arr[i] = equity_now - equity_prev
        equity_prev = equity_now

    P_final = float(close[-1]); Ph_final = float(hc[-1]); extra = 0.0
    if in_pos:
        realized = short_size * (entry_price - Ph_final)
        cash += realized; short_realized += realized
        fee = short_size * Ph_final * perp_cost
        cash -= fee; perp_fees_total += fee; extra -= fee
    if units_spot > 0:
        fee = units_spot * P_final * spot_cost
        cash -= fee; spot_fees_total += fee; extra -= fee
    pnl_arr[-1] += extra
    return pnl_arr, {"trades": trades, "rebals": rebals, "hours_in_position": hours_in,
                     "funding_total": funding_total, "short_realized_pnl": short_realized,
                     "perp_fees_total": perp_fees_total, "spot_fees_total": spot_fees_total}


def selftest():
    """Defaults must reproduce the original simulator exactly."""
    from engine import STAKING_YIELD, load_data
    from backtest_b_constdollar import simulate_constdollar, build_trend_up
    import pandas as pd
    worst = 0.0
    for c in ("BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"):
        df = load_data(c); close = df["close"].values
        s = pd.Series(close)
        sig = ((s.pct_change(336) < 0) | (s.pct_change(720) < 0)).fillna(False).values
        kw = dict(rebal_threshold=0.20, risk_free_apr=0.04, refill_confirm=build_trend_up(close),
                  signal_lag=1, slippage=0.0005)
        a, ia = simulate_constdollar(df, STAKING_YIELD.get(c, 0.0), sig, **kw)
        b, ib = simulate_ext(df, STAKING_YIELD.get(c, 0.0), sig, **kw)
        worst = max(worst, float(np.max(np.abs(a - b))))
        assert ia["trades"] == ib["trades"], c
    assert worst < 1e-9, f"ext simulator diverges from original by {worst}"
    print(f"selftest OK: defaults reproduce original on 6 coins (max diff {worst:.1e})")


if __name__ == "__main__":
    selftest()
