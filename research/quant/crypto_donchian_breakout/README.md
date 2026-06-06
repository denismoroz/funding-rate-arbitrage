# Donchian Channel Breakout (Turtle Trading)

Classic Richard Donchian / Turtle breakout system applied to BTC, ETH, SOL daily bars.
Period: 2023-06-01 to 2026-06-01 (3 years).

## Quick Results (N=55/M=20, long-only, 5 bps/side)

| Metric       | Basket   | BTC Buy&Hold |
|--------------|----------|--------------|
| CAGR         | 28.5%    | 38.6%        |
| Sharpe       | 0.96     | 0.93         |
| Max Drawdown | -28.6%   | -49.5%       |
| Calmar       | 1.00     | 0.78         |
| Exposure     | 50.5%    | 100%         |

**Best grid cell:** N=55 / M=10 → CAGR 32.4%, Sharpe 1.13, MaxDD -26.7%, Calmar 1.21

## Files

| File                      | Contents                                              |
|---------------------------|-------------------------------------------------------|
| `backtest.py`             | Main backtest script (run directly)                   |
| `results.csv`             | Daily equity curve + returns (default config)         |
| `trades.csv`              | Trade log: entry/exit date+px, return%, bars held     |
| `trades_long_short.csv`   | Same for the long/short variant                       |
| `grid_search.csv`         | Grid N×M × cost_bps × direction summary               |
| `metrics.json`            | Full metrics: basket, per-coin, grid, yearly, L/S     |
| `equity.png`              | Equity curve vs BTC buy&hold                          |
| `strategy_description.md` | Rules, pseudocode, no-look-ahead protocol             |
| `sources.md`              | Academic and practitioner references                  |
| `final_assessment.md`     | Honest verdict vs 25% target + robustness caveats     |

## Running

```bash
source .venv/bin/activate
python research/quant/crypto_donchian_breakout/backtest.py
```

## Signal Logic

```
upper[t] = high.rolling(N).max().shift(1)   # prior N-day high
lower[t] = low.rolling(M).min().shift(1)    # prior M-day low

Enter LONG  if close[t] > upper[t]  (breakout above N-day high)
Exit  FLAT  if close[t] < lower[t]  (close below M-day low)
```

`qutil.backtest_weights` adds a further 1-bar shift for next-bar execution.
No look-ahead at any stage.

## Key Findings

- **25% CAGR hurdle: met on this sample (28.5%)**, but 2023 dominates.
- **Drawdown significantly reduced** vs buy&hold: -28.6% vs -49.5%.
- **Trade profile**: 52% win rate, 4.4× profit factor — textbook breakout signature.
- **Long/short hurts** in crypto: positive drift means short-side destroys alpha.
- **Cost-insensitive**: doubling costs (5→10 bps) changes CAGR by only 0.3 pp.
- **2025-2026 negative**: choppy regime kills trend systems. Forward confidence is limited.
- See `final_assessment.md` for full verdict.
