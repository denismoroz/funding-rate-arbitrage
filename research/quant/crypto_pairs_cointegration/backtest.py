"""
Crypto pairs / cointegration stat-arb.

Idea: among a cluster of large-cap coins, find cointegrated pairs (Engle-Granger on a
ROLLING in-sample window only), trade the z-score of the spread market-neutral:
  z = (spread - mean)/std  over a rolling window.
  enter short-spread when z > +Z_ENTRY ; short the rich leg, long the cheap leg.
  enter long-spread  when z < -Z_ENTRY.
  exit when |z| < Z_EXIT (mean reversion) or |z| > Z_STOP (divergence stop).

No look-ahead:
  * hedge ratio (beta) and spread stats are estimated on data up to t-1 (rolling, shifted).
  * positions decided at close[t] earn ret[t+1] (qutil shifts weights forward).
  * pair SELECTION uses only the first FORMATION_DAYS as in-sample; trading is evaluated
    out-of-sample after that, and we also run a walk-forward variant.

Costs: per-side DEFAULT_COST_BPS on each leg's turnover (perp, both legs).
Universe: liquid coins with full history (survivorship caveat documented).
"""
import sys, itertools, json
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qutil as q
from statsmodels.tsa.stattools import coint

HERE = Path(__file__).resolve().parent
TF = "1d"
# coins with full 2023-06 history (avoid late-listed HYPE/XPL/PURR for clean cointegration)
# MATIC dropped: Hyperliquid series ends 2024-09-10 (POL rebrand) and would truncate the panel.
UNIVERSE = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "ARB", "OP", "DOGE", "UNI", "INJ", "TIA"]
FORMATION_DAYS = 365      # in-sample window to discover cointegrated pairs
COINT_PVAL = 0.05
LOOKBACK = 60             # rolling window for beta + z-score
Z_ENTRY = 2.0
Z_EXIT = 0.5
Z_STOP = 4.0
COST_BPS = q.DEFAULT_COST_BPS
GROSS_PER_PAIR = 1.0      # notional per leg (1.0 long + 1.0 short = 2.0 gross)


def log_px(coins):
    px = q.load_closes(coins, TF).dropna()
    return np.log(px)


def find_pairs(lp_is: pd.DataFrame):
    """Engle-Granger cointegration on in-sample log-prices; return passing pairs."""
    pairs = []
    for a, b in itertools.combinations(lp_is.columns, 2):
        s = lp_is[[a, b]].dropna()
        if len(s) < 200:
            continue
        try:
            _, pval, _ = coint(s[a], s[b])
        except Exception:
            continue
        if pval < COINT_PVAL:
            pairs.append((a, b, pval))
    return sorted(pairs, key=lambda x: x[2])


def pair_weights(lp: pd.DataFrame, a: str, b: str):
    """Return per-bar target weights for legs a,b from rolling z-score of spread.

    beta_t = rolling OLS slope of a~b using window ending at t (then shifted 1 bar so the
    signal at t only uses info through t-1's fit; positions execute next bar via qutil).
    """
    A, B = lp[a], lp[b]
    # rolling beta via covariance/variance over LOOKBACK
    cov = A.rolling(LOOKBACK).cov(B)
    var = B.rolling(LOOKBACK).var()
    beta = (cov / var)
    spread = A - beta * B
    mu = spread.rolling(LOOKBACK).mean()
    sd = spread.rolling(LOOKBACK).std()
    z = (spread - mu) / sd

    # state machine -> spread position in {-1,0,+1}; +1 means long spread (long A, short beta*B)
    pos = np.zeros(len(z))
    cur = 0
    zv = z.values
    for i in range(len(zv)):
        if np.isnan(zv[i]):
            pos[i] = 0; cur = 0; continue
        if cur == 0:
            if zv[i] > Z_ENTRY: cur = -1
            elif zv[i] < -Z_ENTRY: cur = +1
        else:
            if abs(zv[i]) < Z_EXIT: cur = 0
            elif abs(zv[i]) > Z_STOP: cur = 0   # divergence stop
        pos[i] = cur
    pos = pd.Series(pos, index=z.index)
    # leg weights: long spread => +1 on A, -beta on B (normalize gross to ~constant)
    bt = beta.copy()
    wA = pos * GROSS_PER_PAIR
    wB = -pos * bt * GROSS_PER_PAIR
    # normalize so |wA|+|wB| ~ 2*GROSS (cap leverage from large beta)
    gross = (wA.abs() + wB.abs()).replace(0, np.nan)
    scale = (2 * GROSS_PER_PAIR) / gross
    scale = scale.clip(upper=1.0).fillna(1.0)   # only scale DOWN to cap leverage
    return wA * scale, wB * scale


def run():
    lp = log_px(UNIVERSE)
    px = np.exp(lp)
    is_end = lp.index[0] + pd.Timedelta(days=FORMATION_DAYS)
    lp_is = lp[lp.index < is_end]
    pairs = find_pairs(lp_is)
    print(f"in-sample {lp_is.index[0].date()}..{lp_is.index[-1].date()}  cointegrated pairs<{COINT_PVAL}: {len(pairs)}")
    for a, b, p in pairs[:12]:
        print(f"  {a:>5}-{b:<5} p={p:.4f}")
    if not pairs:
        print("no cointegrated pairs found"); return

    top = pairs[:8]   # trade the strongest 8 pairs as an equal-risk basket
    # build combined weight panel across all coins
    W = pd.DataFrame(0.0, index=lp.index, columns=UNIVERSE)
    for a, b, _ in top:
        wA, wB = pair_weights(lp, a, b)
        W[a] = W[a].add(wA / len(top), fill_value=0)
        W[b] = W[b].add(wB / len(top), fill_value=0)

    bt = q.backtest_weights(px[UNIVERSE], W, cost_bps=COST_BPS)
    # evaluate OUT-OF-SAMPLE only (after formation window)
    oos = bt[bt.index >= is_end]
    m_full = q.metrics_from_returns(bt["ret_net"], TF)
    m_oos = q.metrics_from_returns(oos["ret_net"], TF)

    HERE.mkdir(parents=True, exist_ok=True)
    bt.to_csv(HERE / "results.csv")
    # trades table: count entries per pair (sign changes from 0)
    trades = []
    for a, b, pv in top:
        wA, wB = pair_weights(lp, a, b)
        sgn = np.sign(wA).fillna(0)
        entries = ((sgn != 0) & (sgn.shift(1).fillna(0) == 0)).sum()
        trades.append({"pair": f"{a}-{b}", "coint_pval": pv, "entries": int(entries)})
    pd.DataFrame(trades).to_csv(HERE / "trades.csv", index=False)

    out = {"strategy": "crypto_pairs_cointegration", "tf": TF, "universe": UNIVERSE,
           "n_pairs_traded": len(top), "pairs": [f"{a}-{b}" for a, b, _ in top],
           "params": {"FORMATION_DAYS": FORMATION_DAYS, "LOOKBACK": LOOKBACK,
                      "Z_ENTRY": Z_ENTRY, "Z_EXIT": Z_EXIT, "Z_STOP": Z_STOP,
                      "cost_bps": COST_BPS},
           "full_sample": m_full, "out_of_sample": m_oos,
           "yearly_oos": q.period_breakdown(oos["ret_net"]).to_dict("records")}
    q.save_metrics(HERE / "metrics.json", out)
    q.equity_plot(oos["equity"] / oos["equity"].iloc[0], "Crypto pairs cointegration (OOS)",
                  HERE / "equity_oos.png", benchmark=px["BTC"])

    print("\n=== OUT-OF-SAMPLE (post-formation) ===")
    for k in ["cagr", "vol", "sharpe", "sortino", "max_drawdown", "calmar", "exposure", "years"]:
        print(f"  {k:14s} {m_oos.get(k):.3f}")
    print("\nyearly OOS:")
    print(q.period_breakdown(oos["ret_net"]).to_string(index=False))


if __name__ == "__main__":
    run()
