# Final Assessment: Crypto Trend / TSMOM Strategy

## Target: 25% CAGR. Verdict: BELOW TARGET (default); APPROACHES TARGET (best grid configs)

---

## Default Configuration (50/200 SMA, vol-targeted, long/flat, 5bps)

| Metric | Strategy | Buy&Hold BTC |
|---|---|---|
| CAGR | **9.2%** | 38.7% |
| Annualised Vol | 27.1% | 57.5% |
| Sharpe | **0.46** | 0.93 |
| Sortino | **0.49** | — |
| Max Drawdown | **-27.7%** | -49.5% |
| Calmar | **0.33** | 0.78 |
| Exposure | 56.5% | 100% |
| Entry Trades | 9 (all coins, 3yr) | — |

The default config FAILS the 25% CAGR target by a wide margin (9.2% vs 25%).
It also underperforms buy-and-hold BTC on Sharpe (0.46 vs 0.93), which is
the more concerning finding — trend-following should at least beat passive
on a risk-adjusted basis.

### What went wrong with 50/200 SMA:
The 50/200 SMA is a very slow signal. Over a 3-year period with only ~9 total
entries across 3 coins, the strategy spends 43% of the time flat and earns
nothing during those periods. Critically, the signal missed the 2023-H2 rally
(200-day SMA takes 200 bars to initialise), then sat flat all of 2026 (correct)
but also missed any short-term rebounds. Vol-targeting further reduces position
size when crypto vol spikes, which helps drawdown but hurts CAGR.

### At 10bps costs: CAGR=8.9%, Sharpe=0.45, MDD=-28.1%
Cost sensitivity is minimal (turnover is low with only 9 entries), which is the
one positive property of this slow default.

---

## Yearly Regime Breakdown (default)

| Year | CAGR | Sharpe | MaxDD | Comment |
|---|---|---|---|---|
| 2023 | 12.5% | 1.54 | -2.7% | Bull year, strategy mostly flat (warmup period) |
| 2024 | 46.2% | 1.25 | -22.1% | Bull year, strategy caught the trend |
| 2025 | -17.0% | -0.46 | -26.7% | Crypto drawdown, strategy exited late |
| 2026 | 0.0% | — | 0.0% | All signals flat (50/200 SMA below for all coins) |

2024 was the strategy's shining year: 46.2% CAGR, Sharpe 1.25, in-trend position.
2025 demonstrates the core weakness: trend strategies exit late and take meaningful
losses before the signal triggers. The -17% in 2025 vs -27% for buy&hold is modest
benefit. 2026 shows the strategy correctly parked in cash while BTC remained below
its 200-day average.

**Regime dependence is severe.** The strategy is highly dependent on being in a
trending bull year. One bad year (2025) essentially erased 3 years of compounding.

---

## Sensitivity Grid Findings

### Best Performers (no vol-target, 5bps, long/flat):

| Signal | Params | CAGR | Sharpe | Sortino | MDD | Calmar |
|---|---|---|---|---|---|---|
| TSMOM | mom90d | 44.8% | 1.10 | 1.30 | -31.1% | 1.44 |
| TSMOM | mom30d | 45.8% | 1.16 | 1.48 | -37.7% | 1.21 |
| MA | sma10/50 | 37.8% | 1.02 | 1.22 | -37.6% | 1.01 |
| MA | sma20/100 | 36.0% | 0.98 | 1.11 | -43.7% | 0.82 |
| MA | sma50/200 | 5.0% | 0.33 | 0.35 | -45.0% | 0.11 |

TSMOM 90d (long/flat, no vol-target): CAGR 44.8%, Sharpe 1.10, MDD -31.1%.
This is the grid's best risk-adjusted result and **approaches 25% CAGR meaningfully**.
However, it was also the best in-sample pick — with only 3 years of data, that's
exactly 1 parameter's worth of data. Call this hopeful, not proven.

### Key Robustness Observations:
1. Shorter lookbacks dominate: 10/50 SMA >> 20/100 >> 50/200. The 50/200 chosen
   a priori is the worst in the MA family.
2. TSMOM generally beats MA crossover on this sample.
3. Long/short variants HURT vs long/flat: MDD balloons (-80% for 50/200 LS),
   confirming the standard finding that shorting crypto perps in a broadly bullish
   sample is costly.
4. Vol-targeting helps the 50/200 SMA (reduces MDD from -45% to -28%) but costs CAGR
   (5.0% → 9.2% CAGR; counter-intuitive gain is due to avoiding deeper vol spikes).

---

## Honest Verdict

### vs 25% CAGR Target:
- **Default config: BELOW TARGET** — 9.2% is far below 25%
- **Best grid configs: APPROACHES but does NOT reliably exceed** — TSMOM 30/90d
  and SMA 10/50 produce 37–46% CAGR, but this is in-sample (3yr) and cherry-picked
  from 12 combinations. With standard multiple-comparison caution, the "true" expected
  CAGR of the best config is probably somewhere between 10–30%.

### vs Buy&Hold BTC Benchmark (CAGR 38.7%, Sharpe 0.93, MDD -49.5%):
- The DEFAULT config fails to beat buy&hold on CAGR (9.2% vs 38.7%) or Sharpe (0.46 vs 0.93)
- It does succeed on MDD (-27.7% vs -49.5%) — this is the expected virtue of trend
- Shorter-lookback configs beat BTC on MDD and Sharpe, while roughly matching CAGR
- The 2023..2026 window is a poor test period: the dominant regime is bull/crash cycles
  that strongly reward buy&hold

### Data Limitations:
- Only 3 years of data (1097 daily bars) — grossly insufficient to distinguish signal
  from luck for any individual parameter combination
- All three assets (BTC, ETH, SOL) are highly correlated; this is not true
  diversification, so the basket is essentially a single-factor bet
- Survivorship: BTC/ETH/SOL are the three survivors of the 2022 bear market that were
  listed on HL from day one; backtest selects on a universe that did well
- No funding cost modeled (long perps pay funding when market is bullish; in reality
  this can drag 5–15% annualised, meaningfully eating into trend profits)

### Would This Strategy Be Deployed Live?
**No, not in current form.**
- Default CAGR (9.2%) is far below the 25% hurdle and below what a simpler buy&hold earns
- The signal that does work (shorter lookbacks, TSMOM) needs out-of-sample validation
  on a longer history and should account for perpetual funding costs
- Before live consideration: use 5+ years of data (including 2020-2022 bear/bull cycles),
  add funding cost drag, validate on at least 2 non-overlapping periods

### If forced to choose one signal for further investigation:
**TSMOM 90-day, long/flat, no vol-target, equal-weight BTC/ETH/SOL** is the most
promising: Sharpe 1.10, MDD -31.1%, Calmar 1.44, low turnover, robust across similar
lookbacks (60/90d both work). Needs longer-window validation before any live use.
