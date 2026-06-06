# Sources — FX trend following (G10)

- Moskowitz, Ooi, Pedersen, "Time Series Momentum", J. Financial Economics (2012) — the canonical
  TSMOM result across futures incl. FX.
- Man Group, "A Trend Following Deep Dive: The Dynamics of Dispersion" (2024-25): longer-horizon
  trend held up; short/medium sleeves weaker — https://www.man.com/insights/deep-dive-trend-following
- Macrosynergy, "Diversified trend following in emerging FX markets" — edge needs breadth beyond G10:
  https://macrosynergy.com/research/diversified-trend-following-in-emerging-fx-markets/
- Quantpedia, FX Carry Trade (context on FX systematic premia): https://quantpedia.com/strategies/fx-carry-trade

Data: yfinance daily spot (EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, NZDUSD), 2004-2026,
saved under research/quant/data_fx/. Download script inline in backtest.py header / see git history.
Limitations: spot only (no carry/roll), G10 only, no commodity/rates markets that real CTAs trade.
