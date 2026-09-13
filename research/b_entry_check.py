"""Strategy B: is the hedge ENTRY the problem? (2026-09-13)

Three reference points on the same simulator and honest settings:
  floor   - random entry with the same time-in-hedge (circular shifts of the real signal)
  actual  - mom14|mom30, entered 1h after the signal (as in the honest run)
  ceiling - ORACLE: hedge exactly when the next 14 / 30 days will fall (uses the future;
            unattainable, it only measures how much better entry could possibly be)
Plus timing sensitivity: the same signal entered 1 day / 3 days / 7 days late.
If a week of delay barely matters and actual sits far above random, entry precision is
not what limits B.
"""
import numpy as np, pandas as pd
from engine import STAKING_YIELD, load_data, HOURS_PER_YEAR, TOTAL_CAPITAL
from backtest_b_constdollar import simulate_constdollar, build_trend_up

COINS = ["BTC", "ETH", "SOL", "AVAX", "TIA", "INJ"]
CASH, SLIP, THR = 0.04, 0.0005, 0.20
data = {c: load_data(c) for c in COINS}

def mom_or(close):
    s = pd.Series(close)
    return ((s.pct_change(14*24).fillna(0) < 0) | (s.pct_change(30*24).fillna(0) < 0)).values

def oracle(close, days):
    s = pd.Series(close)
    return (s.shift(-days*24) / s - 1 < 0).fillna(False).values

def run(make_sig, lag=1):
    out = []
    for c in COINS:
        df = data[c]; close = df["close"].values
        pnl, info = simulate_constdollar(df, STAKING_YIELD.get(c, 0.0), make_sig(close), rebal_threshold=THR,
                                         risk_free_apr=CASH, refill_confirm=build_trend_up(close),
                                         signal_lag=lag, slippage=SLIP)
        yrs = len(df)/HOURS_PER_YEAR; eq = TOTAL_CAPITAL + np.cumsum(pnl)
        out.append(dict(cagr=((eq[-1]/TOTAL_CAPITAL)**(1/yrs)-1)*100, dd=((eq/np.maximum.accumulate(eq))-1).min()*100,
                        hedge=info["short_realized_pnl"]/TOTAL_CAPITAL/yrs*100,
                        fees=-(info["perp_fees_total"]+info["spot_fees_total"])/TOTAL_CAPITAL/yrs*100,
                        trades=info["trades"], on=float(np.mean(make_sig(close)))))
    d = pd.DataFrame(out, index=COINS)
    return d

def line(lab, d):
    print(f"  {lab:<36} CAGR {d.cagr.mean():+6.1f}%  DD {d.dd.mean():6.1f}%  hedge {d.hedge.mean():+6.1f}%/yr  "
          f"fees {d.fees.mean():+5.1f}%/yr  trades {int(d.trades.mean()):>4}  hedged {d.on.mean()*100:3.0f}% of time")
    return d

print("equal-weight 6 coins, each coin its own full history, honest costs\n")
print("REFERENCE POINTS")
act = line("actual: mom14|mom30, +1h", run(mom_or, 1))
rng = np.random.default_rng(0); nulls = []
for _ in range(12):
    sh = {c: int(rng.integers(30*24, len(data[c]) - 30*24)) for c in COINS}
    it = iter(COINS)
    def rnd(close, _sh=sh):
        c = next(k for k in COINS if len(data[k]) == len(close) and np.array_equal(data[k]["close"].values, close))
        return np.roll(mom_or(close), _sh[c])
    nulls.append(run(rnd, 1))
nh = np.array([n.hedge.mean() for n in nulls]); nc = np.array([n.cagr.mean() for n in nulls])
print(f"  {'floor: random entry, same time hedged':<36} CAGR {nc.mean():+6.1f}%  hedge {nh.mean():+6.1f}%/yr "
      f"(12 shifts, range {nh.min():+.1f}..{nh.max():+.1f})")
for days in (14, 30):
    line(f"ceiling: ORACLE next {days}d falls", run(lambda c, d=days: oracle(c, d), 0))

print("\nTIMING SENSITIVITY (same signal, entered later)")
for lag_h, lab in ((24, "+1 day"), (72, "+3 days"), (168, "+7 days")):
    line(f"mom14|mom30, {lab}", run(mom_or, lag_h))
