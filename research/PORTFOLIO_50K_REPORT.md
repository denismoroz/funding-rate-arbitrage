# $50k cross-venue + LST portfolio — APR model

**Date:** 2026-06-05 (corrected; original 2026-06-04)
**Correction:** Backpack funding inputs replaced by interval-aware values from
`CROSS_VENUE_BACKTEST_REPORT.md`. Staking yields updated to conservative values
from `research/staking/staking_inputs.csv`. Backpack is NEVER the best venue after
the fix. Old inflated values: LINK 19.94 (Backpack) → 11.21 (HL); DOGE 10.89 → 7.94
(Aster); ETH 8.25 (Backpack) → 8.06 (Aster). Staking: SOL 7.5→6.5, AVAX 5.0→4.5,
ETH 3.0→2.5, HYPE 2.5→2.2.

**Model:** `portfolio_50k_model.py`. Question: can a $50k+ system reach ~14% APR,
and where does 50/50 (half decoupled / half unified) land?

---

## Architectures

- **DECOUPLED (safe):** spot = LST off-venue (cold wallet/DeFi, accrues staking), short-perp
  + margin buffer on best-funding venue. Buffer is ADDITIVE capital → dilutes APR, but only
  buffer is at venue counterparty risk. Segregated margin → buffer 3× is the floor.
- **UNIFIED (efficient):** spot on-venue as collateral for the short (LST must be accepted as
  collateral). occupied ≈ notional, no additive buffer → higher APR, but full spot at venue risk.
- **BLEND x:** fraction x of capital in unified, (1−x) decoupled. x=0.5 = 50/50 idea.

APR on occupied capital: `income = (funding + staking − fee_drag) × N`;
decoupled occupied `= N(1 + buffer/lev)`, unified `≈ N(1 + 0.1)`.

---

## Results (cold window, buffer 3×, fee drag 1.5%, equal-weight 7 coins)

| Architecture | leverage 10× | leverage 20× |
|-------------|-------------|-------------|
| 100% decoupled | 8.54% | 9.65% |
| **50/50 blend** | **9.31%** | **9.87%** |
| 100% unified | 10.09% | — |
| 50/50 WITHOUT staking | 7.43% | — |

**Corrected 50/50 ≈ 9.3%** — noticeably below the original (inflated) 11.0% because
LINK and DOGE lost their Backpack-inflated funding and LINK has no staking yield.

### 50/50 — linear risk/return dial (with staking, 10×)

| unified fraction | APR | spot at venue risk |
|-----------------|-----|-------------------|
| 0%  | 8.54% | 0%  |
| 25% | 8.92% | 25% |
| 50% | 9.31% | 50% |
| 75% | 9.70% | 75% |
| 100%| 10.09%| 100%|

+25% unified = +0.38% APR at cost of +25% spot under counterparty risk.

### Per-coin (with staking, decoupled / unified, 10×)

| coin | venue   | funding% | staking% | decoupled | unified |
|------|---------|----------|----------|-----------|---------|
| BTC  | HL      | 9.23     | 0        | 5.95%     | 7.03%   |
| ETH  | Aster   | 8.06     | 2.5      | 6.97%     | 8.24%   |
| SOL  | Aster   | 6.14     | 6.5      | 8.57%     | 10.13%  |
| HYPE | HL      | 19.40    | 2.2      | 15.46%    | 18.27%  |
| AVAX | Aster   | 10.49    | 4.5      | 10.38%    | 12.26%  |
| LINK | HL      | 11.21    | 0        | 7.47%     | 8.83%   |
| DOGE | Aster   | 7.94     | 0        | 4.95%     | 5.85%   |

**Dual-income leaders (corrected):** HYPE 15.5–18.3%, AVAX 10.4–12.3%, SOL 8.6–10.1%.
LINK is middle-of-pack (7.5–8.8%) after losing its inflated Backpack funding and
having no staking. It is no longer a star.

---

## Verdict on 14%

**14% on a diversified equal-weight portfolio is NOT achievable.** Ceiling is
~10% (100% unified + LST). Corrected inputs are meaningfully weaker than the
old report because:
1. LINK dropped from apparent 14.2% (decoupled) to 7.5% — the biggest single drag.
2. DOGE dropped from 7.2% to 5.0% (decoupled).
3. Staking yields tightened modestly.

Reaching 14% requires:
1. **Dangerous concentration** in HYPE/AVAX/SOL AND 100% unified AND ≥20× leverage
   simultaneously — even then barely (see `YIELD_WEIGHTED_REPORT.md` sensitivity table).
2. Tilting safely (≤25%/coin, ≤50%/venue, 50/50 blend) only adds ~1.2 pp → reaches 10.5%.

**Diversified + safe = ~9–10%, not 14%.** This is the honest answer with corrected inputs.

---

## Caveats

- Static yield model on cold-regime mean funding + conservative staking estimates;
  not a full backtest with two-phase entries/exits and liquidations.
- Cold window is the weakest period; hot funding was ×2–3 → all metrics higher in those periods.
- LST depeg risk (wstETH had a 7% depeg in Jun 2022, now structurally fixed post-Shanghai;
  sAVAX thin secondary market; kHYPE/HYPE-LST unvalidated — flag as high uncertainty).
- APR on occupied capital; at partial deployment the APR on the full $50k budget is lower.
- HYPE dominates any tilted portfolio. If HYPE funding normalizes to market levels, the
  whole cross-venue thesis degrades materially.

## See also

- `YIELD_WEIGHTED_REPORT.md` — four allocation schemes with concentration metrics and verdict
- `CROSS_VENUE_BACKTEST_REPORT.md` — interval-aware backtest that produced the corrected funding
- `research/staking/staking_inputs.csv` — source of staking APRs
