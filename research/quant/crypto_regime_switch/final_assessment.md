# Final Assessment: Regime-Switch Trend vs Carry

**Period**: 2023-06-01 to 2026-06-01 (1097 days, ~3 years)
**Switching cost**: 5bps on |Δw_trend| per rebalance day
**ER window**: 30 days; **Momentum window**: 30 days

---

## Results Table

| Variant         | CAGR  | Sharpe | MDD    | Calmar | % In Trend | # Switches |
|-----------------|-------|--------|--------|--------|------------|------------|
| trend_only      | 34.3% | 0.99   | -33.9% | 1.01   | 100%       | —          |
| carry_only      | 7.3%  | 9.95   | -0.7%  | 10.72  | 0%         | —          |
| **static_50_50**| **22.0%** | **1.18** | **-17.1%** | **1.29** | 50% | —      |
| hard_er         | 25.6% | 1.01   | -28.1% | 0.91   | 40.6%      | 121        |
| soft_er         | 24.3% | 1.17   | -21.4% | 1.13   | 40.6%      | 121        |
| hard_mom        | 34.6% | 1.09   | -34.6% | 1.00   | 46.0%      | 66         |
| soft_mom        | 28.7% | 1.16   | -25.3% | 1.14   | 46.0%      | 66         |

**None of the four regime-switch variants beat the static 50/50 blend on both Calmar AND Sharpe net of switching costs.**

---

## Yearly Breakdown

### Static 50/50 (baseline)
| Year | CAGR   | Sharpe | Max DD  |
|------|--------|--------|---------|
| 2023 | 78.2%  | 3.72   | -4.5%   |
| 2024 | 31.5%  | 1.31   | -17.1%  |
| 2025 | 4.9%   | 0.36   | -11.4%  |
| 2026 | -14.1% | -2.13  | -7.2%   |

### Best Switch (soft_mom)
| Year | CAGR   | Sharpe | Max DD  |
|------|--------|--------|---------|
| 2023 | 119.5% | 3.47   | -6.9%   |
| 2024 | 52.8%  | 1.53   | -16.3%  |
| 2025 | -4.1%  | -0.08  | -18.6%  |
| 2026 | -18.6% | -2.64  | -9.5%   |

The switch variant amplifies both upside AND downside: better in the good years (2023-2024),
worse in the difficult years (2025-2026).

---

## Why Regime Switching Fails Here

### 1. The carry sleeve dominates by Calmar, not trend
Carry-only has a Calmar of 10.72 with near-zero drawdown. The static 50/50 wins vs
trend-only purely because carry's exceptional risk-adjusted returns swamp trend's volatility.
Any regime switch that reduces carry allocation (even temporarily) pays a Calmar penalty
that it can't recoup with better trend timing.

### 2. Switching costs are material relative to carry returns
The carry sleeve earns ~7% annualized in daily increments of ~0.02%. A 5bps switching cost
per rebalance equals ~2.5 days of carry returns. Hard switches (121 or 66 events over 3 years)
accumulate 6–60bps per year in friction — small in absolute terms, but meaningful relative to
the carry sleeve's thin daily returns.

### 3. The ER signal is nearly 50/50 by construction
The ER threshold is the expanding median of ER itself — by definition, ER is trending "trending"
~50% of the time. With the regime spending ~41% of time in trend vs 40-50% expected:
the filter doesn't meaningfully concentrate trend exposure in high-ER periods.

### 4. Strategy momentum adds noise more than signal
The soft_mom variant (best switch by Calmar, 1.14 vs static 1.29) looks better than hard_er but
still underperforms static. Momentum-based regime detection tends to be early in reversals and
late in recoveries — whipsawing between allocations.

### 5. "Best" switch outperforms in strong-trend years, fails in weak-trend years
2023: soft_mom CAGR 119% vs static 78% (good trend year — being more in trend helps)
2025: soft_mom CAGR -4% vs static +5% (weak trend year — switching into trend is wrong)
The benefit is cyclical and realized in-sample. No guarantee it repeats.

---

## Honest Verdict

**Static blend wins. Don't time regimes.**

The static 50/50 blend (CAGR 22%, Sharpe 1.18, Calmar 1.29) is unbeaten by any regime-switch
variant on both Calmar AND Sharpe simultaneously. The closest contender (soft_er) achieves
Sharpe 1.17 / Calmar 1.13 — nearly as good on Sharpe but materially worse on Calmar.

This is the expected result from the literature. Finominal's research suggests carry and trend
are regime-orthogonal, but that orthogonality is an argument for *blending* the two strategies
permanently, not for *timing* between them. The static blend captures the diversification benefit
every day; the regime switch sacrifices it trying to be clever — and pays switching costs for
the privilege.

The regime-switching hypothesis is rejected for this dataset.

**Recommendation**: maintain the static 50/50 (or nearby fixed) blend as the meta-strategy.
Do not introduce regime timing into the allocation between sleeves.

**CAGR vs 25% target**: static 50/50 delivers 22% — below the 25% target. To reach 25%,
increase trend weight toward 60-75% (see trend_carry_blend results), accepting higher MDD.
No regime-switch variant improves upon this tradeoff.
