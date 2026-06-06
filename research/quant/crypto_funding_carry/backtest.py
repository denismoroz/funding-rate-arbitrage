"""
Crypto funding-rate carry (delta-neutral cash-and-carry) — BENCHMARK strategy.

This is the incumbent (the live CarryMesh project earns ~19-25% real APR). Here we build a
clean, self-contained, conservatively-costed version so it can be ranked head-to-head with the
directional strategies in research/quant/.

Position: long $1 spot + short $1 perp on the same coin => delta-neutral (price PnL cancels).
Income: short perp RECEIVES funding when funding>0; long spot may earn staking yield.
Capital: we charge the FULL hedged capital = $2 per position ($1 spot + $1 perp margin at 1x).
         => return on capital each hour = funding_rate/2 (+ staking/8760/2). This avoids the
         APR-inflation trap of crediting funding against only the perp margin.

Signal (per coin, hourly):
  ann = trailing SMOOTH-hour mean of fundingRate * 8760  (annualized, causal)
  enter (go delta-neutral) when ann > ENTRY_ANN ; exit to cash when ann < EXIT_ANN ; MIN_HOLD hours.
Costs: per leg taker on entry and exit (spot 7bps, perp 3.5bps), charged on the $1 notional each.

Portfolio: equal capital weight across the liquid universe; each coin sleeve is in/out on its own
signal. We report APR on TOTAL committed capital (idle sleeves drag) and on DEPLOYED capital.
No look-ahead: signal at hour t uses funding through t; PnL/funding accrues over t->t+1; entry/exit
costs paid at the bar the state changes.
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qutil as q

HERE = Path(__file__).resolve().parent
DATA = q.DATA_DIR
HOURS = 8760
SPOT_TAKER = 0.0007    # 0.07% Hyperliquid spot taker
PERP_TAKER = 0.00035   # 0.035% Hyperliquid perp taker
UNIVERSE = ["BTC", "ETH", "SOL", "AVAX", "LINK", "AAVE", "ARB", "OP", "DOGE", "UNI", "INJ", "TIA"]
STAKING = {"ETH": 0.035, "SOL": 0.085, "AVAX": 0.065, "TIA": 0.10, "INJ": 0.08,
           "BTC": 0.0, "LINK": 0.0, "AAVE": 0.0, "ARB": 0.0, "OP": 0.0, "DOGE": 0.0, "UNI": 0.0}

SMOOTH = 24          # hours of funding smoothing
ENTRY_ANN = 0.05     # enter when annualized funding > 5%
EXIT_ANN = 0.00      # exit when annualized funding < 0%
MIN_HOLD = 24        # hours


def load_funding(coin):
    df = pd.read_csv(DATA / f"{coin}.csv")
    df["time"] = pd.to_datetime(df["time"], format="mixed", utc=True)
    df = df.set_index("time").sort_index()
    # collapse to clean hourly grid
    r = df["fundingRate"].astype(float)
    r = r[~r.index.duplicated(keep="first")]
    return r


def sim_coin(rate: pd.Series, staking: float, with_staking: bool):
    """Return hourly net return-on-capital series for one coin's delta-neutral sleeve."""
    ann = (rate.rolling(SMOOTH).mean() * HOURS)
    idx = rate.index
    n = len(rate)
    state = np.zeros(n)          # 1 = delta-neutral position on, 0 = cash
    ret = np.zeros(n)
    held = 0
    hold_h = 0
    rv = rate.values
    av = ann.values
    stk_h = (staking / HOURS) if with_staking else 0.0
    for i in range(n):
        # accrue funding/staking for the position held INTO this hour (decided at i-1)
        if held:
            # income on $1 perp (funding) + $1 spot (staking), capital = $2
            ret[i] += (rv[i] + stk_h) / 2.0
        # decide state change at end of hour i (executes same bar cost)
        if np.isnan(av[i]):
            state[i] = held; continue
        if held:
            hold_h += 1
            if av[i] < EXIT_ANN and hold_h >= MIN_HOLD:
                # exit: pay taker on both legs over $2 capital
                ret[i] -= (SPOT_TAKER + PERP_TAKER) / 2.0
                held = 0; hold_h = 0
        else:
            if av[i] > ENTRY_ANN:
                ret[i] -= (SPOT_TAKER + PERP_TAKER) / 2.0
                held = 1; hold_h = 0
        state[i] = held
    return pd.Series(ret, index=idx), pd.Series(state, index=idx)


def run(with_staking=False, tag="funding_only"):
    sleeves = {}
    states = {}
    for c in UNIVERSE:
        try:
            r = load_funding(c)
        except FileNotFoundError:
            continue
        ret, st = sim_coin(r, STAKING.get(c, 0.0), with_staking)
        sleeves[c] = ret
        states[c] = st
    R = pd.DataFrame(sleeves).sort_index()
    S = pd.DataFrame(states).reindex(R.index).fillna(0)
    # equal capital weight across all sleeves (idle sleeves earn 0 -> drag = honest)
    port_total = R.mean(axis=1)                       # APR on total committed capital
    deployed_frac = S.mean(axis=1).replace(0, np.nan)
    port_deployed = (R.sum(axis=1) / S.sum(axis=1).replace(0, np.nan)).fillna(0)  # on deployed only

    m_total = q.metrics_from_returns(port_total, "1h")
    m_deployed = q.metrics_from_returns(port_deployed, "1h")
    eq = (1 + port_total).cumprod()

    HERE.mkdir(parents=True, exist_ok=True)
    out = pd.DataFrame({"ret_total": port_total, "ret_deployed": port_deployed,
                        "deployed_frac": S.mean(axis=1), "equity_total": eq})
    out.to_csv(HERE / f"results_{tag}.csv")
    res = {"strategy": "crypto_funding_carry", "variant": tag, "universe": UNIVERSE,
           "params": {"SMOOTH": SMOOTH, "ENTRY_ANN": ENTRY_ANN, "EXIT_ANN": EXIT_ANN,
                      "MIN_HOLD": MIN_HOLD, "capital_model": "2x (spot+perp), full hedge"},
           "avg_deployed_frac": float(S.mean(axis=1).mean()),
           "on_total_capital": m_total, "on_deployed_capital": m_deployed,
           "yearly_total": q.period_breakdown(port_total).to_dict("records")}
    q.save_metrics(HERE / f"metrics_{tag}.json", res)
    q.equity_plot(eq, f"Funding carry delta-neutral ({tag})", HERE / f"equity_{tag}.png")
    print(f"\n=== {tag} ===  avg deployed frac {S.mean(axis=1).mean():.2f}")
    print("  ON TOTAL CAPITAL:   CAGR %.1f%% vol %.1f%% Sharpe %.2f MDD %.2f%% Calmar %.1f" % (
        m_total['cagr']*100, m_total['vol']*100, m_total['sharpe'], m_total['max_drawdown']*100, m_total['calmar']))
    print("  ON DEPLOYED CAPITAL:CAGR %.1f%% vol %.1f%% Sharpe %.2f MDD %.2f%%" % (
        m_deployed['cagr']*100, m_deployed['vol']*100, m_deployed['sharpe'], m_deployed['max_drawdown']*100))
    print(q.period_breakdown(port_total).to_string(index=False))
    return res


if __name__ == "__main__":
    run(with_staking=False, tag="funding_only")
    run(with_staking=True, tag="funding_plus_staking")
