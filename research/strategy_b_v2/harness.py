"""Strategy B v2 — disciplined search for a profitable, HL-executable configuration.

PRE-REGISTERED (written before any result of this harness was seen):

Executable universe : BTC, ETH, SOL, AVAX (HL spot exists; TIA/INJ do not). No staking
                      (HL spot tokens are wrapped, earn nothing). Idle cash earns 0%.
Splits              : TRAIN 2023-06-01..2025-05-31 (selection only)
                      TEST  2025-06-01..2026-05-31 (evaluated once, after selection)
                      FORWARD 2026-06-01..2026-09-13 on fresh HL data (evaluated once)
Grid (144 configs)  : sticky exit hours {0,12,24,48}
                      hedge orientation a {0, .05, .10}: hedge = NOT(mom14 > a AND mom30 > a)
                      ratchet threshold {.10, .20, .50}
                      idle-cash carry overlay {off, on}
                      coin weights {equal, inverse-vol}
Costs               : taker + 5 bps slippage everywhere (maker left as upside, reported apart)
Selection           : best portfolio Calmar on TRAIN.
SUCCESS (all required): on TEST and on FORWARD — return > 0, max DD <= 15%, and better
                      return than the current HL-executable B (sticky 0, a 0, thr .20,
                      carry off, equal); plus better than current on >= 6/7 start paths.

Carry overlay model (fixed a priori): per coin a delta-neutral spot-long/perp-short book of
notional 0.6 x the $1000 cash reserve B never spends; enter when the trailing 8h mean HL
funding annualises above 10%, exit after 24 consecutive hours below 0; P&L = notional x
funding; each entry/exit pays spot taker + perp taker + 2x slippage. Spot/perp basis ignored.
"""
import sys, itertools
from pathlib import Path
import numpy as np, pandas as pd
R = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(R))
from engine import load_data, HOURS_PER_YEAR, TOTAL_CAPITAL, POSITION_SIZE, SPOT_TAKER, PERP_TAKER
from backtest_b_constdollar import build_trend_up
from b_sim_ext import simulate_ext
from b_improve_ideas import sticky

COINS = ["BTC", "ETH", "SOL", "AVAX"]
CASH_APR, SLIP, LAG = 0.0, 0.0005, 1
TRAIN = (pd.Timestamp("2023-06-01", tz="UTC"), pd.Timestamp("2025-05-31 23:00", tz="UTC"))
TEST = (pd.Timestamp("2025-06-01", tz="UTC"), pd.Timestamp("2026-05-31 23:00", tz="UTC"))
FWD = (pd.Timestamp("2026-06-01", tz="UTC"), pd.Timestamp("2026-09-13 23:00", tz="UTC"))
FWD_DIR = Path("/private/tmp/claude-501/-Users-d-prj-funding-rate-arbitrage/5f1f1c94-f80e-4bdd-88c5-68cfed37b7b4/scratchpad/b_forward")

GRID = dict(sticky=[0, 12, 24, 48], a=[0.0, 0.05, 0.10], thr=[0.10, 0.20, 0.50], carry=[False, True],
            weights=["equal", "invvol"])
CURRENT = dict(sticky=0, a=0.0, thr=0.20, carry=False, weights="equal")

def load(source):
    if source == "research":
        return {c: load_data(c) for c in COINS}
    return {c: pd.read_csv(FWD_DIR/f"{c}.csv", parse_dates=["time"]).set_index("time") for c in COINS}

def hedge_signal(close, a):
    s = pd.Series(close)
    up = (s.pct_change(14*24) > a) & (s.pct_change(30*24) > a)
    return (~up).values & s.pct_change(30*24).notna().values

def carry_pnl(df, slip=SLIP, notional=0.6*(TOTAL_CAPITAL-POSITION_SIZE)):
    f = df["fundingRate"].astype(float).fillna(0.0).values
    m8 = pd.Series(f).rolling(8).mean().values * HOURS_PER_YEAR
    on = False; below = 0; out = np.zeros(len(f)); cost = notional * (SPOT_TAKER + PERP_TAKER + 2*slip)
    for i in range(len(f)):
        j = i - LAG
        sig = m8[j] if j >= 0 else np.nan
        if not on and not np.isnan(sig) and sig > 0.10:
            on, below = True, 0; out[i] -= cost
        elif on:
            below = below + 1 if (not np.isnan(sig) and sig < 0) else 0
            if below >= 24:
                on = False; out[i] -= cost
        if on:
            out[i] += notional * f[i]
    return out

def coin_equity(data, sticky_h, a, thr, carry, slip=SLIP, start_h=0):
    eq = {}
    for c in COINS:
        df = data[c].iloc[start_h:]; close = df["close"].values
        sig = hedge_signal(close, a)
        if sticky_h: sig = sticky(sig, sticky_h)
        pnl, info = simulate_ext(df, 0.0, sig, rebal_threshold=thr, risk_free_apr=CASH_APR,
                                 refill_confirm=build_trend_up(close), signal_lag=LAG, slippage=slip)
        if carry: pnl = pnl + carry_pnl(df, slip)
        e = pd.Series(TOTAL_CAPITAL + np.cumsum(pnl), index=df.index)
        eq[c] = e.resample("D").last()
    return pd.DataFrame(eq).dropna()

def portfolio(eqdf, weights):
    r = eqdf.pct_change().fillna(0.0)
    if weights == "equal":
        w = pd.DataFrame(1.0/len(COINS), index=r.index, columns=r.columns)
    else:
        vol = r.rolling(90, min_periods=30).std()
        inv = (1.0/vol).replace([np.inf], np.nan)
        w = inv.div(inv.sum(axis=1), axis=0)
        w = w.resample("MS").first().reindex(r.index, method="ffill").shift(1).fillna(1.0/len(COINS))
    return (w * r).sum(axis=1)

def seg(rp, lo, hi):
    x = rp[(rp.index >= lo) & (rp.index <= hi)]
    if len(x) < 2: return dict(ret=np.nan, ann=np.nan, dd=np.nan, calmar=np.nan)
    eq = (1 + x).cumprod(); y = len(x)/365.0
    ret = (eq.iloc[-1]-1)*100; ann = ((eq.iloc[-1])**(1/y)-1)*100
    dd = -((eq/eq.cummax())-1).min()*100
    return dict(ret=ret, ann=ann, dd=dd, calmar=ann/dd if dd > 0 else np.nan)

def configs():
    keys = list(GRID)
    for vals in itertools.product(*[GRID[k] for k in keys]):
        yield dict(zip(keys, vals))
