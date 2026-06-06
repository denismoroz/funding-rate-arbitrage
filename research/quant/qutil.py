"""
Shared backtest utilities for research/quant strategies.

Design principles (enforced repo-wide so all strategies are comparable):

  * NO LOOK-AHEAD. Signals are computed on bar close at time t. Execution happens on
    the NEXT bar. The canonical pattern is: position[t] is decided from data up to and
    including close[t]; the return earned is position[t] * ret[t+1]. Helper
    `shift_signal_to_returns` enforces this by shifting the target weight forward by 1.

  * COSTS ARE REAL. Turnover (abs change in weight) is charged round-trip-aware: each
    unit of |Δweight| pays `cost_bps` (one side). Holding a short perp across a funding
    stamp pays/earns funding where relevant (handled per-strategy).

  * METRICS ON A DAILY EQUITY CURVE. Everything is annualized from the equity curve's
    own bar frequency, inferred from the index.

Data: 1h Hyperliquid OHLCV in research/data/<COIN>_1h.csv, 2023-06-01..2026-06-01.
"""
from __future__ import annotations
import json
import math
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]          # repo root (quant->research->root)
DATA_DIR = ROOT / "research" / "data"               # crypto 1h OHLCV + HL funding
QUANT_DIR = ROOT / "research" / "quant"

# ---- cost constants (Hyperliquid-class venue, from research/engine.py) ----
PERP_TAKER_BPS = 3.5      # 0.035%
SPOT_TAKER_BPS = 7.0      # 0.07%
# default per-side cost for directional crypto backtests = taker + modest slippage
DEFAULT_COST_BPS = 5.0    # 3.5 fee + ~1.5 slippage on a liquid perp

BARS_PER_YEAR = {"1h": 24 * 365, "4h": 6 * 365, "1d": 365}


# ----------------------------------------------------------------------------
# Data loading
# ----------------------------------------------------------------------------
def load_ohlcv(coin: str, tf: str = "1d") -> pd.DataFrame:
    """Load 1h OHLCV and resample to tf in {'1h','4h','1d'}. UTC index.

    Resample is causal: a daily bar labelled D aggregates hours [D 00:00, D 23:00];
    its close is the 23:00 close. Signals from bar D execute on bar D+1.
    """
    fp = DATA_DIR / f"{coin}_1h.csv"
    df = pd.read_csv(fp, parse_dates=["time"]).set_index("time").sort_index()
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    if tf == "1h":
        return df
    rule = {"4h": "4h", "1d": "1D"}[tf]
    out = pd.DataFrame({
        "open": df["open"].resample(rule).first(),
        "high": df["high"].resample(rule).max(),
        "low": df["low"].resample(rule).min(),
        "close": df["close"].resample(rule).last(),
        "volume": df["volume"].resample(rule).sum(),
    }).dropna()
    return out


def load_closes(coins, tf: str = "1d") -> pd.DataFrame:
    """Aligned close-price panel for a list of coins (inner join on index)."""
    cols = {}
    for c in coins:
        try:
            cols[c] = load_ohlcv(c, tf)["close"]
        except FileNotFoundError:
            continue
    px = pd.DataFrame(cols).sort_index()
    return px


def infer_tf(index: pd.DatetimeIndex) -> str:
    """Guess bar frequency from the median spacing of an index."""
    sec = np.median(np.diff(index.values).astype("timedelta64[s]").astype(float))
    hours = sec / 3600.0
    if hours <= 1.5:
        return "1h"
    if hours <= 6:
        return "4h"
    return "1d"


# ----------------------------------------------------------------------------
# Execution / PnL
# ----------------------------------------------------------------------------
def backtest_weights(prices: pd.DataFrame | pd.Series,
                     target_weight: pd.DataFrame | pd.Series,
                     cost_bps: float = DEFAULT_COST_BPS,
                     funding: pd.DataFrame | pd.Series | None = None) -> pd.DataFrame:
    """Vectorized weight backtest with next-bar execution and turnover costs.

    prices         : close panel (T x N) or series.
    target_weight  : desired weight per asset DECIDED AT BAR t (T x N). Will be shifted
                     forward by 1 bar internally so it earns ret[t+1] (no look-ahead).
    cost_bps       : per-side cost charged on |Δweight| each bar.
    funding        : optional funding rate panel aligned to prices (fraction per bar);
                     a position w pays  -w * funding (long pays positive funding).

    Returns DataFrame with columns: ret_gross, cost, ret_funding, ret_net, equity.
    """
    if isinstance(prices, pd.Series):
        prices = prices.to_frame("x")
        target_weight = target_weight.to_frame("x")
        if funding is not None:
            funding = funding.to_frame("x")

    prices = prices.sort_index()
    w_decided = target_weight.reindex(prices.index).fillna(0.0)
    # position held over bar t+1 is the weight decided at t  -> shift forward
    w_held = w_decided.shift(1).fillna(0.0)

    asset_ret = prices.pct_change().fillna(0.0)
    ret_gross = (w_held * asset_ret).sum(axis=1)

    # turnover cost: weight actually changes at execution (between w_held bars)
    dw = w_held.diff().abs().fillna(w_held.abs())
    cost = dw.sum(axis=1) * (cost_bps / 1e4)

    if funding is not None:
        fnd = funding.reindex(prices.index).fillna(0.0)
        ret_funding = (-(w_held) * fnd).sum(axis=1)
    else:
        ret_funding = pd.Series(0.0, index=prices.index)

    ret_net = ret_gross - cost + ret_funding
    equity = (1.0 + ret_net).cumprod()
    return pd.DataFrame({
        "ret_gross": ret_gross, "cost": cost, "ret_funding": ret_funding,
        "ret_net": ret_net, "equity": equity,
    })


# ----------------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------------
def metrics_from_returns(ret_net: pd.Series, tf: str | None = None) -> dict:
    """Risk/return metrics from a per-bar net-return series."""
    ret_net = ret_net.dropna()
    if len(ret_net) < 2:
        return {}
    if tf is None:
        tf = infer_tf(ret_net.index)
    ann = BARS_PER_YEAR[tf]
    eq = (1.0 + ret_net).cumprod()
    n = len(ret_net)
    years = n / ann
    cagr = eq.iloc[-1] ** (1 / years) - 1 if years > 0 and eq.iloc[-1] > 0 else float("nan")
    vol = ret_net.std(ddof=1) * math.sqrt(ann)
    mean_ann = ret_net.mean() * ann
    sharpe = mean_ann / vol if vol > 0 else float("nan")
    downside = ret_net[ret_net < 0].std(ddof=1) * math.sqrt(ann)
    sortino = mean_ann / downside if downside > 0 else float("nan")
    roll_max = eq.cummax()
    dd = eq / roll_max - 1.0
    max_dd = dd.min()
    calmar = cagr / abs(max_dd) if max_dd < 0 else float("nan")
    exposure = float((ret_net != 0).mean())
    return {
        "cagr": float(cagr), "vol": float(vol), "sharpe": float(sharpe),
        "sortino": float(sortino), "max_drawdown": float(max_dd),
        "calmar": float(calmar), "mean_ann": float(mean_ann),
        "n_bars": int(n), "years": float(years), "exposure": float(exposure),
        "final_equity": float(eq.iloc[-1]),
    }


def trade_stats(trades: pd.DataFrame, pnl_col: str = "pnl") -> dict:
    """Win rate / profit factor / avg trade from a trades table with a pnl column."""
    if trades is None or len(trades) == 0:
        return {"n_trades": 0}
    p = trades[pnl_col].astype(float)
    wins, losses = p[p > 0], p[p < 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else float("inf")
    return {
        "n_trades": int(len(p)),
        "win_rate": float((p > 0).mean()),
        "profit_factor": float(pf),
        "avg_trade": float(p.mean()),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "best": float(p.max()), "worst": float(p.min()),
    }


def period_breakdown(ret_net: pd.Series) -> pd.DataFrame:
    """Yearly CAGR/Sharpe/MaxDD breakdown to expose regime dependence."""
    tf = infer_tf(ret_net.index)
    rows = []
    for yr, g in ret_net.groupby(ret_net.index.year):
        m = metrics_from_returns(g, tf)
        rows.append({"year": yr, "cagr": m.get("cagr"), "sharpe": m.get("sharpe"),
                     "max_dd": m.get("max_drawdown"), "n": len(g)})
    return pd.DataFrame(rows)


def save_metrics(path: Path, d: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(d, f, indent=2, default=float)


def equity_plot(eq: pd.Series, title: str, out: Path, benchmark: pd.Series | None = None):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return
    fig, ax = plt.subplots(2, 1, figsize=(11, 7), gridspec_kw={"height_ratios": [3, 1]})
    ax[0].plot(eq.index, eq.values, label="strategy", lw=1.3)
    if benchmark is not None:
        b = benchmark.reindex(eq.index).ffill()
        b = b / b.iloc[0]
        ax[0].plot(b.index, b.values, label="buy&hold BTC", lw=1.0, alpha=0.6)
    ax[0].set_yscale("log"); ax[0].legend(); ax[0].set_title(title); ax[0].grid(alpha=0.3)
    dd = eq / eq.cummax() - 1.0
    ax[1].fill_between(dd.index, dd.values, 0, color="red", alpha=0.4)
    ax[1].set_title("drawdown"); ax[1].grid(alpha=0.3)
    fig.tight_layout(); out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=90); plt.close(fig)
