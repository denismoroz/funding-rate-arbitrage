# Crypto Volatility Risk Premium (VRP) Backtest

**Question**: Is selling implied vol on BTC/ETH options a real, additive edge versus
the existing carry+trend book?

**One-line answer**: Yes, VRP is a real and persistent premium on BTC — but it is
**not additive** to the existing book at current sizing. The 3-way blend (VRP + carry + trend)
has *lower* Calmar (5.9) than the existing 2-way blend (7.9), and VRP's modest size
contribution adds volatility without proportional return improvement.

## Files

| File | Description |
|------|-------------|
| `backtest.py` | Main backtest — run to reproduce all outputs |
| `trades.csv` | Per-tranche log: entry, IV, RV_fwd, VRP, P&L |
| `results.csv` | Daily return/equity series for all variants |
| `metrics.json` | Full metrics for all variants + additivity test |
| `equity.png` | BTC plain non-overlapping equity curve |
| `equity_laddered.png` | BTC laddered (smoother) equity curve |
| `equity_basket.png` | BTC+ETH 50/50 basket equity curve |
| `strategy_description.md` | Model details, caveats, pseudocode |
| `sources.md` | Academic and data references |
| `final_assessment.md` | Honest verdict |

## Running

```bash
source .venv/bin/activate
python research/quant/crypto_vol_risk_premium/backtest.py
```

## Key Results (2-vol-pt cost, 15% target vol)

### Per-Variant Summary

| Variant | CAGR | Sharpe | Max DD | Calmar | Win Rate | Worst Tranche |
|---------|------|--------|--------|--------|----------|---------------|
| BTC plain | 18.6% | 1.19 | -11.2% | 1.66 | 72.3% | -10.8% (Jan 2026) |
| ETH plain | 3.4% | 0.30 | -23.3% | 0.14 | 61.7% | -9.7% (Jan 2026) |
| BTC+ETH 50/50 basket | 10.9% | 0.80 | -12.9% | 0.84 | 72.3% | -10.8% (Jan 2026) |
| BTC size-scaled | 40.1% | 1.56 | -6.2% | 6.5 | 72.3% | — |
| BTC laddered | 1.5% | 0.42 | — | — | — | — |

### Conditional Filter (BTC, IV - RV_trailing > THRESH)

| Threshold | CAGR | Sharpe | Max DD | N kept / total |
|-----------|------|--------|--------|----------------|
| 0% | 9.7% | 0.74 | -12.1% | 37/47 |
| 5% | 6.9% | 0.58 | -13.6% | 31/47 |
| 10% | 2.8% | 0.31 | -16.7% | 19/47 |

The conditional filter consistently **hurts** performance — it removes profitable
tranches (high IV periods) and keeps those where the premium is marginal.

### Cost Sensitivity (BTC plain)

| Cost | CAGR | Sharpe | Calmar |
|------|------|--------|--------|
| 0 vol pts | 25.5% | 1.50 | 2.47 |
| 2 vol pts (default) | 18.6% | 1.19 | 1.66 |
| 4 vol pts | 12.1% | 0.84 | 1.00 |

VRP survives at 4 vol pts but barely (Calmar ≈ 1).

### VRP Persistence by Year (BTC, raw VRP in vol units)

| Year | N tranches | Mean VRP | Mean IV | Mean RV_fwd | Win Rate |
|------|-----------|---------|---------|-------------|----------|
| 2021 | 6 | +26.5% | 89.7% | 63.2% | 100% |
| 2022 | 10 | +7.4% | 75.1% | 67.7% | 50% |
| 2023 | 7 | +7.2% | 46.9% | 39.6% | 86% |
| 2024 | 9 | +6.2% | 58.2% | 52.0% | 78% |
| 2025 | 12 | +6.8% | 45.7% | 38.9% | 75% |
| 2026 | 3 | -9.6% | 48.8% | 58.5% | 33% |

VRP is positive and meaningful in 2021-2025. 2026 shows early compression /
negative VRP (crypto markets rallied hard → actual RV exceeded IV).

### Additivity (monthly returns, 37-month overlap 2023-06 to 2026-06)

| Pair | Correlation |
|------|------------|
| VRP vs Carry | +0.001 |
| VRP vs Trend | +0.032 |
| Carry vs Trend | +0.701 |

VRP is essentially **uncorrelated** to both carry and trend (< 0.05 in both cases).
This is the theoretically expected result — vol selling is a fundamentally different
risk factor from directional or carry exposure.

### Blend Test

| Blend | CAGR | Sharpe | Max DD | Calmar |
|-------|------|--------|--------|--------|
| Carry + Trend (2-way, inv-vol weighted) | 7.2% | 6.6 | -0.9% | 7.9 |
| Carry + Trend + VRP (3-way, inv-vol weighted) | 7.3% | 5.9 | -1.2% | 5.9 |

Adding VRP to the blend **increases CAGR marginally** (+0.1 pp) but **hurts
risk-adjusted returns** (Calmar falls by 2.1, Sharpe falls by 0.7). This happens
because VRP's vol (15% target) is vastly larger than carry/trend vol (1-2%), so
even with a small inv-vol weight (5.2%), it still dominates the tail.

## Critical Caveats

1. **This is a vol-swap proxy**, not actual short-options P&L. Real execution on
   Deribit requires delta hedging (gamma P&L), margin management, and will face
   wider spreads.
2. **2022 tail is understated**: The DVOL data gap (Dec 2022 – Jun 2023) causes
   the worst potential tranche (Nov 12, 2022, right after FTX collapse) to be
   excluded. Paradoxically, that specific tranche would have been a winner (DVOL
   103%, next-30-day RV only 32%), but the regime uncertainty around that period
   is underrepresented.
3. **Fat left tail**: The worst tranche (Jan 2026) lost 10.8% of capital in 30
   days. Real vol spikes can produce losses of 30-50% of a full position within
   days, requiring a tail hedge.
4. **Sizing is key**: 15% target vol / 0.25× leverage is already conservative.
   Any leverage above 1× on a short-vol position without a tail hedge is dangerous.
