# Sources

## Primary References

### Carry versus Trend Following
- **Finominal Research**: "Carry versus Trend Following"
  https://insights.finominal.com/research-carry-versus-trend-following/
  - Documents the regime-orthogonality hypothesis: trend following earns in trending
    environments; carry earns in stable/choppy environments.
  - Motivates the meta-strategy of dynamically switching between the two sleeves.

### Kaufman Efficiency Ratio
- **Perry Kaufman**, "New Trading Systems and Methods" (Wiley Finance)
  - Original definition: ER = |directional move| / |total path length| over a window W.
  - Range [0, 1]; used in KAMA (Kaufman Adaptive Moving Average).
  - ER is an intuitive trending-vs-choppy classifier with no free threshold parameters
    when paired with a trailing median.
- **Investopedia summary**: https://www.investopedia.com/terms/k/kaufmansadaptivemovingaverage.asp

## Data Sources

- **BTC/ETH/SOL OHLCV**: Hyperliquid 1h bars, `research/data/<COIN>_1h.csv`
  - Period: 2023-06-01 to 2026-06-01
  - Resampled to daily for trend signals and regime detection.

- **Carry returns**: `research/quant/crypto_funding_carry/results_funding_plus_staking.csv`
  - Hourly `ret_total` = delta-neutral funding rate carry + staking yield
  - Compounded to daily: `(1 + r).resample('1D').prod() - 1`

## Related Internal Research

- `research/quant/crypto_trend_carry_blend/backtest.py` — static blend baseline
  (this study replicates the input series exactly from that file)
- `research/quant/crypto_trend_tsmom/` — underlying trend ensemble methodology
- `research/quant/crypto_funding_carry/` — carry sleeve methodology
