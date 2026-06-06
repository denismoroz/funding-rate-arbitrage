# Sources

## Internal

- **`research/quant/_wfo_probe/wfo_trend.py`** — trend ensemble construction (MA crossovers +
  TSMOM, BTC/ETH/SOL, 5bps cost, long/flat). Replicated exactly here.

- **`research/quant/crypto_funding_carry/results_funding_plus_staking.csv`** — pre-computed
  hourly net return series from the delta-neutral funding carry backtest (funding + staking,
  12-coin universe, full cost model, total-capital denominator).

- **`research/FINAL_REPORT.md`** — regime-orthogonality argument: "trend earns in trends,
  carry earns in chop" (Section: Strategy Synthesis / Regime Map).

## External

- **Finominal, "Carry versus Trend Following"** (2023):  
  https://insights.finominal.com/research-carry-versus-trend-following/  
  Empirical analysis across asset classes showing carry and trend have low/negative correlation
  in trending regimes and complementary performance; blending improves Sharpe.

- **Return Stacked, "Carry the Yield, Ride the Trend: A Strategic Partnership"**:  
  https://www.returnstacked.com/carry-the-yield-ride-the-trend-a-strategic-partnership/  
  Framework for capital-efficient carry+trend stacking; shows carry's low-vol / high-Sharpe
  profile complements trend's high-CAGR / high-vol / high-MDD profile.
