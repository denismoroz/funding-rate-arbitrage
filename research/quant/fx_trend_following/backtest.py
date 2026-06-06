"""
FX trend following (G10, daily) — time-series momentum, vol-targeted basket.

Universe: EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, NZDUSD (yfinance daily, 2004-2026).
Trend is direction-agnostic to base/quote, so we apply the same signal to each quoted series and
go long/short.

Signal (per pair, decided at close[t], executed next bar via qutil shift):
  sig = sign( mean over horizons H of (close[t]/close[t-H] - 1) ),  H in {63,126,252} days
        i.e. blended 3/6/12-month time-series momentum (Moskowitz-Ooi-Pedersen).
Sizing: vol-target each pair to TARGET_VOL annual using trailing 60d realized vol; cap leverage.
Basket: equal weight across the 7 pairs (sum of vol-scaled signed weights).

Costs: FX majors are cheap; default 1.0 bp per side on |Δweight| (≈0.5-1 pip round trip), test 2.0.
We report full sample AND the recent decade (post-2015) because trend famously decayed post-2008.
"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qutil as q

HERE = Path(__file__).resolve().parent
FXDIR = HERE.parent / "data_fx"
PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"]
HORIZONS = [63, 126, 252]
VOL_LB = 60
TARGET_VOL = 0.10        # annual, per pair
MAX_LEV = 2.0            # cap per-pair leverage
COST_BPS = 1.0


def load_fx():
    cols = {}
    for p in PAIRS:
        df = pd.read_csv(FXDIR / f"{p}.csv")
        # yfinance multiindex header leftovers: find the date + close columns robustly
        date_col = df.columns[0]
        df[date_col] = pd.to_datetime(df[date_col], utc=True, errors="coerce")
        df = df.dropna(subset=[date_col]).set_index(date_col).sort_index()
        close = pd.to_numeric(df.iloc[:, -1] if "Close" not in df.columns else df["Close"], errors="coerce")
        cols[p] = close
    px = pd.DataFrame(cols).dropna(how="all").ffill().dropna()
    return px


def signal(px: pd.DataFrame):
    sigs = []
    for H in HORIZONS:
        sigs.append(np.sign(px / px.shift(H) - 1.0))
    s = sum(sigs) / len(sigs)            # in [-1,1], blended
    return np.sign(s)                    # net direction; ties->0


def vol_scaled_weights(px: pd.DataFrame):
    ret = px.pct_change()
    rv = ret.rolling(VOL_LB).std() * np.sqrt(252)
    raw = signal(px)
    scale = (TARGET_VOL / rv).clip(upper=MAX_LEV)
    w = raw * scale
    w = w / len(PAIRS)                   # equal-weight basket
    return w.fillna(0.0)


def run():
    px = load_fx()
    w = vol_scaled_weights(px)
    bt = q.backtest_weights(px, w, cost_bps=COST_BPS)
    bt2 = q.backtest_weights(px, w, cost_bps=2.0)

    m_full = q.metrics_from_returns(bt["ret_net"], "1d")
    recent = bt[bt.index >= "2015-01-01"]
    m_recent = q.metrics_from_returns(recent["ret_net"], "1d")
    m_2bps = q.metrics_from_returns(bt2["ret_net"], "1d")

    HERE.mkdir(parents=True, exist_ok=True)
    bt.to_csv(HERE / "results.csv")
    res = {"strategy": "fx_trend_following", "tf": "1d", "pairs": PAIRS,
           "params": {"HORIZONS": HORIZONS, "VOL_LB": VOL_LB, "TARGET_VOL": TARGET_VOL,
                      "MAX_LEV": MAX_LEV, "cost_bps": COST_BPS},
           "full_sample": m_full, "recent_2015plus": m_recent, "full_at_2bps": m_2bps,
           "yearly": q.period_breakdown(bt["ret_net"]).to_dict("records")}
    q.save_metrics(HERE / "metrics.json", res)
    q.equity_plot(bt["equity"], "FX trend following (G10 daily)", HERE / "equity.png")

    def show(tag, m):
        print(f"  {tag:18s} CAGR {m['cagr']*100:6.2f}%  vol {m['vol']*100:5.2f}%  Sharpe {m['sharpe']:.2f}  "
              f"Sortino {m['sortino']:.2f}  MDD {m['max_drawdown']*100:6.2f}%  Calmar {m['calmar']:.2f}")
    print("FX trend following (G10, daily, 2004-2026):")
    show("full @1bp", m_full)
    show("full @2bp", m_2bps)
    show("recent 2015+ @1bp", m_recent)
    print("\nyearly:")
    print(q.period_breakdown(bt["ret_net"]).to_string(index=False))


if __name__ == "__main__":
    run()
