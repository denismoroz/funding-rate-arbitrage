"""Out-of-sample forward check of the pre-registered STICKY24 exit (2026-09-13).
Data the in-sample study never saw: HL hourly perp prices + funding, simulated from
2026-03-01 (30 days of signal warm-up and more), scored from 2026-06-01 only.
Short window -> low power: this checks DIRECTION and consistency, not a new estimate."""
import numpy as np, pandas as pd
from pathlib import Path
from engine import STAKING_YIELD, TOTAL_CAPITAL
from backtest_b_constdollar import build_trend_up
from b_sim_ext import simulate_ext
import b_entry_variants as V
from b_improve_ideas import sticky

D = Path("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/b_forward")
SCORE_FROM = pd.Timestamp("2026-06-01", tz="UTC")

def score(c, hours, slip=V.SLIP):
    df = pd.read_csv(D/f"{c}.csv", parse_dates=["time"]).set_index("time")
    close = df["close"].values; sig = V.base(close)
    if hours: sig = sticky(sig, hours)
    pnl, info = simulate_ext(df, STAKING_YIELD.get(c, 0.0), sig, rebal_threshold=V.THR, risk_free_apr=V.CASH,
                             refill_confirm=build_trend_up(close), signal_lag=V.LAG, slippage=slip)
    eq = pd.Series(TOTAL_CAPITAL + np.cumsum(pnl), index=df.index)
    e = eq[eq.index >= SCORE_FROM]
    ret = (e.iloc[-1] / e.iloc[0] - 1) * 100
    dd = ((e / e.cummax()) - 1).min() * 100
    on = pd.Series(sig, index=df.index)[df.index >= SCORE_FROM]
    tr = pd.Series(sig.astype(int), index=df.index).diff().clip(lower=0)[df.index >= SCORE_FROM].sum()
    spot = (df["close"][df.index >= SCORE_FROM].iloc[-1] / df["close"][df.index >= SCORE_FROM].iloc[0] - 1) * 100
    return ret, dd, int(tr), on.mean()*100, spot

print(f"forward window {SCORE_FROM:%Y-%m-%d} .. 2026-09-13 (never seen by the study)\n")
print(f"{'coin':<6}{'spot':>8} | {'current: ret':>13}{'DD':>7}{'hedges':>8} | {'+STICKY24: ret':>15}{'DD':>7}{'hedges':>8}")
agg = {"b": [], "s": [], "bd": [], "sd": []}
for c in V.COINS:
    rb, db, tb, ob, sp = score(c, 0); rs, ds, ts, os_, _ = score(c, 24)
    agg["b"].append(rb); agg["s"].append(rs); agg["bd"].append(db); agg["sd"].append(ds)
    print(f"{c:<6}{sp:>7.1f}% | {rb:>12.1f}%{db:>6.1f}%{tb:>8} | {rs:>14.1f}%{ds:>6.1f}%{ts:>8}")
print(f"{'MEAN':<6}{'':>8} | {np.mean(agg['b']):>12.1f}%{np.mean(agg['bd']):>6.1f}%{'':>8} | "
      f"{np.mean(agg['s']):>14.1f}%{np.mean(agg['sd']):>6.1f}%")
better_ret = sum(s > b for s, b in zip(agg["s"], agg["b"])); better_dd = sum(s > b for s, b in zip(agg["sd"], agg["bd"]))
print(f"\nSTICKY24 better return on {better_ret}/6 coins, shallower drawdown on {better_dd}/6 coins")
