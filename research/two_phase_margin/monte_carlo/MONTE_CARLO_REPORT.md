# Monte-Carlo Validation Report: two_phase_margin

_Generated: 2026-06-08 14:27 UTC_

## Run parameters

| Parameter | Value |
|-----------|-------|
| Generators | `bootstrap`, `parametric` |
| Paths per generator | 500 |
| Horizon | 365 d (8760 h) |
| Coins | `BTC,ETH,SOL,HYPE,PURR` |
| Margin buffer (mbuf) | 3.0× |
| Denominator | **Full-portfolio equity (~$1 000 budget)** — NOT occupied capital (see Rule-5 GAP below) |

## Single-path anchor (U-prod, buf=3×)

Source: `research/TWOPHASE_MARGIN_aggregate.csv`, row `universe=U-prod`, `margin_buffer_x=3.0`.  This is the SINGLE historical backtest that motivated the MC study — the MC distributions below show whether this result is a typical outcome or a right-tail artifact.

| Metric | Single-path (full-budget basis) |
|--------|--------------------------------|
| annual_pct (linear, %) | **2.5026%** |
| max_dd_pct (%) | **0.0782%** |
| Sharpe | 20.7388 |
| n_phase1_negstop_exits | 8 |
| n_phase2_exits | 7 |
| Period | 2025-11-06 → 2026-05-12 (4493 h) |

> **Calmar (single-path):** 32.0× (annual_pct / max_dd_pct — linear ratio, not CAGR/fraction)

## Distribution of MC paths (full-budget denominator)

> All metrics computed on full-portfolio equity (budget_cap_usdc ≈ $1 000, including idle cash). `annual` is CAGR-style; `max_dd` is a fraction (e.g. 0.0050 = 0.50%). `calmar` can be ∞ when max_dd = 0.

Both generators available — **side-by-side comparison** follows.  Divergence between parametric and bootstrap indicates model sensitivity (parametric can produce hot-regime paths; bootstrap is cold-history reshuffle).

### Parametric

**Generator:** `parametric` — parametric (log-level AR(1) funding, GBM+jumps price, hot/cold regime switching, cross-coin corr — includes hot & cold regimes)  
**Paths:** 500  |  **Horizon:** 365 d (8760 h)  |  **Coins:** `BTC,ETH,SOL,HYPE,PURR`  |  **mbuf:** 3.0×

> **Denominator note (Rule-5 GAP):** `annual` and `max_dd` below are computed on **full-portfolio equity** (~$1 000 budget incl. idle cash), NOT on deployed/occupied capital. APR on occupied capital is materially higher — see the _Occupied-capital reframe_ section.

| Metric | p05 | p25 | median | p75 | p95 | min | max |
|--------|-----|-----|--------|-----|-----|-----|-----|
| annual (CAGR, full-budget) | 4.4760% | 6.1650% | 8.0380% | 9.9265% | 13.3128% | 2.2271% | 20.7925% |
| max_dd (fraction, full-budget) | 0.0254% | 0.0437% | 0.0777% | 0.0948% | 0.1260% | 0.0135% | 0.1607% |
| Calmar | 57.9223 | 83.2771 | 112.5737 | 169.7465 | 279.8858 | 28.8854 | 511.6709 |
| Sharpe | 33.2336 | 40.3754 | 46.5374 | 51.7531 | 60.2732 | 9.9102 | 69.6655 |

**Risk probabilities:**

- P(annual < 0) = **0.0%** (0/500 paths)
- P(max_dd > 1%) = **0.0%** (0/500 paths)
- P(max_dd > 5%) = **0.0%** (0/500 paths)
- CVaR annual (worst-5% mean) = **3.7503%** (full-budget basis)

**Exit-mix (averages per path):**

| Exit type | Avg / path |
|-----------|------------|
| Phase-1 neg exits | 0.00 |
| Phase-1 cap exits | 0.04 |
| Phase-1 **NEGSTOP** exits | **0.00** |
| Phase-2 exits | 0.00 |
| Liquidations | 0.60 |
| Forced closes | 0.52 |

### Bootstrap

**Generator:** `bootstrap` — bootstrap (synchronous circular block-resample of real history — cold-only window by construction; see T4 caveat)  
**Paths:** 500  |  **Horizon:** 365 d (8760 h)  |  **Coins:** `BTC,ETH,SOL,HYPE,PURR`  |  **mbuf:** 3.0×

> **Denominator note (Rule-5 GAP):** `annual` and `max_dd` below are computed on **full-portfolio equity** (~$1 000 budget incl. idle cash), NOT on deployed/occupied capital. APR on occupied capital is materially higher — see the _Occupied-capital reframe_ section.

| Metric | p05 | p25 | median | p75 | p95 | min | max |
|--------|-----|-----|--------|-----|-----|-----|-----|
| annual (CAGR, full-budget) | 0.7436% | 1.1558% | 1.4998% | 1.8040% | 2.3352% | 0.2792% | 3.2155% |
| max_dd (fraction, full-budget) | 0.0393% | 0.0527% | 0.0639% | 0.0786% | 0.1106% | 0.0252% | 0.1500% |
| Calmar | 8.6118 | 15.6273 | 23.4700 | 32.1380 | 47.1095 | 2.2324 | 77.7422 |
| Sharpe | 7.8323 | 11.9001 | 14.8211 | 17.5673 | 21.3331 | 2.9980 | 26.3859 |

**Risk probabilities:**

- P(annual < 0) = **0.0%** (0/500 paths)
- P(max_dd > 1%) = **0.0%** (0/500 paths)
- P(max_dd > 5%) = **0.0%** (0/500 paths)
- CVaR annual (worst-5% mean) = **0.6000%** (full-budget basis)

**Exit-mix (averages per path):**

| Exit type | Avg / path |
|-----------|------------|
| Phase-1 neg exits | 0.01 |
| Phase-1 cap exits | 0.05 |
| Phase-1 **NEGSTOP** exits | **10.79** |
| Phase-2 exits | 11.99 |
| Liquidations | 0.01 |
| Forced closes | 0.00 |

### Quick comparison: median metrics

| Metric | parametric | bootstrap | note |
|--------|-----------|-----------|------|
| annual median | 8.0380% | 1.4998% | full-budget |
| max_dd median | 0.0777% | 0.0639% | full-budget |
| Calmar median | 112.5737 | 23.4700 | |
| Sharpe median | 46.5374 | 14.8211 | |
| P(annual < 0) | 0.0% | 0.0% | |
| CVaR annual worst-5% | 3.7503% | 0.6000% | full-budget |

### Single-path vs MC median contrast

| Metric | Single-path | Para median | Boot median |
|--------|-------------|-------------|-------------|
| annual (CAGR approx) | 2.5026% | 8.0380% | 1.4998% |
| max_dd | 0.0782% | 0.0777% | 0.0639% |

> Interpretation: if single-path annual >> MC median, the historical backtest was a favorable-path draw. If single-path Calmar >> MC Calmar distribution, it may be a right-tail artifact. Full verdict deferred to T7 (Opus).

## Occupied-capital reframe (для T7)

All metrics in this report use the **full-portfolio equity denominator** (budget_cap_usdc ≈ $1 000, including idle/undeployed USDC).  This understates APR on a per-deployed-capital basis.

The relationship is:  
```
APR_occupied = APR_full_budget × (budget / avg_deployed)
```
With 7 coins, position_size=\$100, concurrency_cap=K and typical occupancy, avg_deployed ≈ \$300–\$400 out of \$1 000 budget, so the occupied-capital APR is roughly **2.5–3.3× larger** than the full-budget figures shown in the tables.

**Exact multiplier:** requires average deployed notional tracked per hour across all MC paths.  This is NOT stored in the current result columns (T5 output).  The `total_funding` / `total_fees` / `final_equity` columns can be used to reconstruct funding-based APR but not the occupancy fraction directly.  **→ Deferred to T7 (Opus) for accurate reconstruction.**  Do NOT apply an invented multiplier to the tables above.

## Plots

![mc_hist_annual](mc_hist_annual.png)
![mc_hist_max_dd](mc_hist_max_dd.png)
![mc_hist_calmar](mc_hist_calmar.png)

---

_This report was generated by `research/two_phase_margin/monte_carlo/report.py` (T6). Verdict and interpretation deferred to T7 (Opus)._
